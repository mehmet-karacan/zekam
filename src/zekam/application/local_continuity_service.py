"""Generic local lifecycle bridge with a real spool/DB checkpoint barrier."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool, LifecycleSpoolEntry
from zekam.application.local_continuity import (
    EVENT_KINDS,
    ContinuityBinding,
    ContinuityEvent,
    LocalContext,
    LocalContinuityStore,
)
from zekam.application.local_continuity_close import (
    CloseCandidateBundle,
    CloseSummary,
    FrozenClose,
    LocalCloseStore,
)
from zekam.application.local_continuity_control import LocalContinuityControl
from zekam.domain.errors import PolicyViolation, ValidationFailed

_KINDS = {
    "session_start": "SESSION_START",
    "pre_compaction": "PRE_COMPACTION",
    "post_compaction": "POST_COMPACTION",
    "pre_close": "PRE_CLOSE",
    "post_close": "SESSION_CLOSED",
}


class LocalLifecycleContinuity:
    """No provider calls; the source probe observes the exact bound local corpus."""

    def __init__(
        self,
        store: LocalContinuityStore,
        spool: ClientLifecycleSpool,
        binding: ContinuityBinding,
        *,
        source_probe: Callable[[], str],
        controls: LocalContinuityControl | None = None,
        entry_validator: Callable[[LifecycleSpoolEntry], None] | None = None,
    ) -> None:
        self.store = store
        self.spool = spool
        self.binding = binding
        self.source_probe = source_probe
        self.controls = controls
        self.entry_validator = entry_validator

    def assert_current_source(self) -> None:
        if self.source_probe() != self.store.source_content_digest(self.binding):
            raise PolicyViolation("Continuity actual source content drift")

    def doctor(self) -> dict[str, Any]:
        if self.controls is not None and self.controls.is_frozen(self.binding):
            report = self.controls.inspect(self.binding)
            try:
                self.assert_current_source()
            except PolicyViolation:
                report["issues"].append("current-source-or-authority-stale")
                report["state"] = "attention-required"
            else:
                report["current_source_verified"] = True
            return report
        self.assert_current_source()
        report = self.store.inspect(self.binding)
        entries = self.spool.read_session_entries(
            client_id=self.binding.client_id, session_id=self.binding.external_session_id
        )
        persisted = self.store.spool_digests(self.binding)
        expected = tuple(item.entry_digest for item in entries)
        issues = list(report["issues"])
        if not entries or self._event(entries[0]).kind != "SESSION_START":
            issues.append("missing-required-hook-events")
        if expected[: len(persisted)] != persisted:
            issues.append("spool-persistence-chain-gap")
        elif len(expected) > len(persisted):
            issues.append("unpersisted-spool-delta")
        return report | {
            "issues": issues,
            "spool_event_count": len(entries),
            "persisted_spool_count": len(persisted),
            "state": "attention-required" if issues else "healthy",
        }

    def _event(self, entry: LifecycleSpoolEntry) -> ContinuityEvent:
        entry.assert_integrity()
        if self.entry_validator is not None:
            self.entry_validator(entry)
        if (
            entry.client_id != self.binding.client_id
            or entry.session_id != self.binding.external_session_id
        ):
            raise PolicyViolation("Lifecycle spool external session/client mismatch")
        kind = _KINDS.get(entry.internal_event_type, entry.internal_event_type)
        if kind not in EVENT_KINDS:
            raise PolicyViolation("Lifecycle unknown event requires review")
        return ContinuityEvent(
            kind,
            entry.delivery_id,
            entry.occurred_at.isoformat(),
            (),
            (entry.observation_digest,),
            entry.entry_digest,
        )

    def drain(self) -> int:
        if self.controls is not None and self.controls.is_frozen(self.binding):
            return self.controls.drain(self.binding)
        self.assert_current_source()
        with self.spool.frozen_session_entries(
            client_id=self.binding.client_id, session_id=self.binding.external_session_id
        ) as entries:
            for entry in entries:
                self.store.append_event(
                    self.binding, self._event(entry), expected_tail=self.store.tail(self.binding)
                )
            self.assert_current_source()
            return len(entries)

    def hydrate(
        self, context: LocalContext, *, key: str, checkpoint_digest: str | None = None
    ) -> str:
        self.assert_current_source()
        return self.store.hydrate(
            self.binding, context, idempotency_key=key, checkpoint_digest=checkpoint_digest
        )

    def pre_compaction(self, *, context_digest: str, key: str) -> str:
        self.assert_current_source()
        with self.spool.frozen_session_entries(
            client_id=self.binding.client_id, session_id=self.binding.external_session_id
        ) as entries:
            if not entries or self._event(entries[0]).kind != "SESSION_START":
                raise PolicyViolation("Required SESSION_START hook evidence missing")
            if not entries or self._event(entries[-1]).kind != "PRE_COMPACTION":
                raise PolicyViolation("Required PRE_COMPACTION hook evidence missing")
            # No implicit drain here: missing persistence must block, never ACK.
            result = self.store.checkpoint(
                self.binding,
                expected_tail=self.store.tail(self.binding),
                context_digest=context_digest,
                idempotency_key=key,
                spool_digests=tuple(item.entry_digest for item in entries),
            )
            self.assert_current_source()
            return result

    def pre_close(
        self, close_store: LocalCloseStore, summary: CloseSummary, *, context_digest: str, key: str
    ) -> FrozenClose:
        """Freeze the durable PRE_CLOSE boundary, never perform long compilation in a hook."""
        self.assert_current_source()
        with self.spool.frozen_session_entries(
            client_id=self.binding.client_id, session_id=self.binding.external_session_id
        ) as entries:
            if not entries or self._event(entries[0]).kind != "SESSION_START":
                raise PolicyViolation("Required SESSION_START hook evidence missing")
            if not entries or self._event(entries[-1]).kind != "PRE_CLOSE":
                raise PolicyViolation("Required PRE_CLOSE hook evidence missing")
            if self.store.spool_digests(self.binding) != tuple(
                entry.entry_digest for entry in entries
            ):
                raise PolicyViolation("Close unpersisted spool delta blocks freeze/replay")
            existing = self.store.inspect(self.binding).get("close_request_digest")
            if existing is None:
                tail = self.store.tail(self.binding)
                checkpoint = self.store.checkpoint(
                    self.binding,
                    expected_tail=tail,
                    context_digest=context_digest,
                    idempotency_key=key,
                    spool_digests=tuple(item.entry_digest for item in entries),
                )
            else:
                frozen = close_store.load(self.binding, existing)
                checkpoint = frozen.input_body["checkpoint_digest"]
                tail = self.store.tail(self.binding)
            self.assert_current_source()
            return close_store.freeze(
                self.binding,
                summary,
                checkpoint_digest=checkpoint,
                manifest_digest=context_digest,
                expected_tail=tail,
            )

    def pre_close_v2(
        self,
        close_store: LocalCloseStore,
        summary: CloseSummary,
        candidates: CloseCandidateBundle,
        *,
        context_digest: str,
        key: str,
    ) -> FrozenClose:
        """Freeze explicit v2 candidates behind the same durable PRE_CLOSE barrier."""
        if type(summary) is not CloseSummary or type(candidates) is not CloseCandidateBundle:
            raise ValidationFailed("Close v2 requires exact typed summary and candidate bundle")
        summary.__post_init__()
        candidates.__post_init__()
        allowed_sources = set(summary.sources)
        allowed_evidence = set(summary.evidence)
        for category in ("memory", "decision", "skill", "failure"):
            for claim in getattr(candidates, category):
                if (
                    not set(claim.source_refs) <= allowed_sources
                    or not set(claim.evidence_refs) <= allowed_evidence
                ):
                    raise PolicyViolation("Close candidate refs lack admitted summary provenance")
        self.assert_current_source()
        with self.spool.frozen_session_entries(
            client_id=self.binding.client_id, session_id=self.binding.external_session_id
        ) as entries:
            if not entries or self._event(entries[0]).kind != "SESSION_START":
                raise PolicyViolation("Required SESSION_START hook evidence missing")
            if not entries or self._event(entries[-1]).kind != "PRE_CLOSE":
                raise PolicyViolation("Required PRE_CLOSE hook evidence missing")
            if self.store.spool_digests(self.binding) != tuple(
                entry.entry_digest for entry in entries
            ):
                raise PolicyViolation("Close unpersisted spool delta blocks freeze/replay")
            existing = self.store.inspect(self.binding).get("close_request_digest")
            if existing is None:
                tail = self.store.tail(self.binding)
                checkpoint = self.store.checkpoint(
                    self.binding,
                    expected_tail=tail,
                    context_digest=context_digest,
                    idempotency_key=key,
                    spool_digests=tuple(item.entry_digest for item in entries),
                )
            else:
                frozen = close_store.load(self.binding, existing)
                checkpoint = frozen.input_body["checkpoint_digest"]
                tail = self.store.tail(self.binding)
            self.assert_current_source()
            return close_store.freeze_v2(
                self.binding,
                summary,
                candidates,
                checkpoint_digest=checkpoint,
                manifest_digest=context_digest,
                expected_tail=tail,
            )
