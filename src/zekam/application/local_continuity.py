"""Storage-neutral, authority-free contracts for durable local continuity."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from uuid import UUID

from zekam.application.context_ranking import ContextRankingRequest, count_context_tokens
from zekam.application.secret_detection import SECRET_RULES, scan_text
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.context_continuity import ContextCandidate, ContextCandidateKind, ContextManifest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import assert_portable

EVENT_KINDS = frozenset(
    {
        "SESSION_START",
        "USER_TURN_COMMITTED",
        "ASSISTANT_TURN_COMMITTED",
        "TOOL_EFFECT_CLAIMED",
        "TOOL_EFFECT_COMPLETED",
        "CHECKPOINT_REQUESTED",
        "PRE_COMPACTION",
        "POST_COMPACTION",
        "PRE_CLOSE",
        "SESSION_CLOSED",
        "CRASH_RECOVERED",
    }
)
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,511}$")


def logical(value: object, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValidationFailed(f"{label} bounded logical reference required")
    assert_portable(value)
    if scan_text(value, relative_path="continuity/reference", rules=SECRET_RULES):
        raise PolicyViolation(f"{label} secret reference content forbidden")
    return value


def uuid_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(f"{label} canonical UUID required")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise ValidationFailed(f"{label} canonical UUID required") from exc
    return value


def digest_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationFailed("Continuity digest text required")
    parse_digest(value)
    return value


def bounded_int(value: object, *, maximum: int, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationFailed("Continuity integer outside exact bounds")
    return value


def timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationFailed("Continuity canonical timestamp required")
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.astimezone(dt.UTC).isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise ValidationFailed("Continuity canonical UTC timestamp required") from exc
    return value


@dataclass(frozen=True, slots=True)
class ContinuityBinding:
    session_id: str
    external_session_id: str
    project_id: str
    realm_id: str
    client_id: str
    device_id: str
    source_snapshot_id: str
    task_digest: str
    plan_digest: str
    policy_digest: str
    work_item_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("session_id", "project_id", "realm_id", "source_snapshot_id"):
            uuid_text(getattr(self, name), name)
        for name in ("external_session_id", "client_id", "device_id"):
            logical(getattr(self, name), name)
        for name in ("task_digest", "plan_digest", "policy_digest"):
            digest_text(getattr(self, name))
        if (self.work_item_id is None) != (self.run_id is None):
            raise ValidationFailed("Continuity work/run pair required")
        if self.run_id is not None:
            uuid_text(self.run_id, "run")
            uuid_text(self.work_item_id, "work")

    @property
    def binding_digest(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True, slots=True)
class ContinuityEvent:
    kind: str
    idempotency_key: str
    occurred_at: str
    source_refs: tuple[str, ...] = ()
    evidence_digests: tuple[str, ...] = ()
    spool_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in EVENT_KINDS:
            raise ValidationFailed("Unknown continuity event kind")
        logical(self.idempotency_key, "Event key")
        timestamp(self.occurred_at)
        for values, validator in (
            (self.source_refs, lambda value: logical(value, "Event source")),
            (self.evidence_digests, digest_text),
        ):
            if not isinstance(values, tuple) or len(values) > 32:
                raise ValidationFailed("Event refs bounded tuple required")
            for value in values:
                validator(value)
            if len(set(values)) != len(values):
                raise ValidationFailed("Event duplicate reference")
        if self.spool_digest is not None:
            digest_text(self.spool_digest)

    def body(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContinuityTail:
    sequence: int
    event_digest: str | None

    def __post_init__(self) -> None:
        bounded_int(self.sequence, minimum=0, maximum=2**63 - 1)
        if (self.sequence == 0) != (self.event_digest is None):
            raise ValidationFailed("Event tail sequence/digest mismatch")
        if self.event_digest is not None:
            digest_text(self.event_digest)


@dataclass(frozen=True, slots=True)
class LocalContext:
    """Exact selected fragments; the full candidate corpus is never persisted here."""

    manifest: ContextManifest
    fragments: tuple[tuple[str, str], ...]
    ranking_request: ContextRankingRequest
    selected_provenance: tuple[ContextCandidate, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ContextManifest) or self.manifest.compiler_version != 2:
            raise ValidationFailed("Local context requires compiler v2 manifest")
        self.manifest.__post_init__()
        bounded_int(self.manifest.token_budget, maximum=131072)
        if not isinstance(self.ranking_request, ContextRankingRequest):
            raise ValidationFailed("Context ranking request provenance required")
        self.ranking_request.__post_init__()
        if self.manifest.ranking_snapshot_digest != digest(self.ranking_request.body()):
            raise PolicyViolation("Context ranking request digest mismatch")
        if not isinstance(self.selected_provenance, tuple) or any(
            not isinstance(item, ContextCandidate) for item in self.selected_provenance
        ):
            raise ValidationFailed("Context typed candidate provenance required")
        selected_ids = [item.candidate_id for item in self.manifest.selected]
        omitted_ids = [item.candidate_id for item in self.manifest.omitted]
        if (
            len(selected_ids) != len(set(selected_ids))
            or len(omitted_ids) != len(set(omitted_ids))
            or set(selected_ids) & set(omitted_ids)
        ):
            raise PolicyViolation("Context manifest duplicate/overlapping partition")
        metrics = self.manifest.compiler_metrics
        if (
            metrics is None
            or metrics.selected_count != len(selected_ids)
            or metrics.selected_tokens != sum(item.token_count for item in self.manifest.selected)
            or metrics.omitted_count != len(omitted_ids)
            or metrics.token_budget != self.manifest.token_budget
        ):
            raise PolicyViolation("Context compiler metrics mismatch")
        if not isinstance(self.fragments, tuple) or len(self.fragments) > 256:
            raise ValidationFailed("Context fragments bounded tuple required")
        by_id = {}
        for fragment in self.fragments:
            if not isinstance(fragment, tuple) or len(fragment) != 2:
                raise ValidationFailed("Context fragment exact pair required")
            identifier, text = fragment
            logical(identifier, "Context fragment id")
            if identifier in by_id or not isinstance(text, str) or not text:
                raise ValidationFailed("Context fragment duplicate/empty/type mismatch")
            if scan_text(text, relative_path="continuity/fragment", rules=SECRET_RULES):
                raise PolicyViolation("Context fragment secret rejected")
            by_id[identifier] = text
        if set(by_id) != {item.candidate_id for item in self.manifest.selected}:
            raise PolicyViolation("Context exact selected fragment partition required")
        candidates = {item.candidate_id: item for item in self.selected_provenance}
        if len(candidates) != len(self.selected_provenance) or set(candidates) != set(selected_ids):
            raise PolicyViolation("Context exact provenance partition required")
        for item in self.manifest.selected:
            bounded_int(item.token_count, maximum=131072)
            candidate = candidates[item.candidate_id]
            if (
                candidate.candidate_digest != item.candidate_digest
                or candidate.content_digest != item.content_digest
                or candidate.source_ref != item.source_ref
                or candidate.source_revision != item.source_revision
                or candidate.token_count != item.token_count
                or candidate.kind != item.kind
                or candidate.authority != item.authority
            ):
                raise PolicyViolation("Context selection provenance drift")
            text = by_id[item.candidate_id]
            if (
                digest(text) != item.content_digest
                or count_context_tokens(text) != item.token_count
            ):
                raise PolicyViolation("Context content digest/token drift")
            logical(item.source_ref, "Context source ref")
        if len(canonical_json(self.body()).encode("utf-8")) > 1_048_576:
            raise ValidationFailed("Context document byte bound exceeded")

    def body(self) -> dict[str, Any]:
        return {
            "compiler": self.manifest.body(),
            "fragments": dict(self.fragments),
            "ranking_request": self.ranking_request.body(),
            "selected_provenance": [item.provenance_body for item in self.selected_provenance],
            "grants_authority": False,
            "approval_inherited": False,
        }

    def assert_scope(self, binding: ContinuityBinding) -> None:
        expected_work = None if binding.work_item_id is None else f"work/{binding.work_item_id}"
        request = self.ranking_request
        if (
            request.project_scope_ref != f"project/{binding.project_id}"
            or request.realm_scope_ref != f"realm/{binding.realm_id}"
            or request.work_scope_ref != expected_work
            or request.step_scope_ref is not None
        ):
            raise PolicyViolation("Context exact project/realm/work scope mismatch")
        allowed = {
            request.project_scope_ref,
            request.realm_scope_ref,
            f"session/{binding.session_id}",
        }
        if expected_work is not None:
            allowed.add(expected_work)
        if binding.run_id is not None:
            allowed.add(f"run/{binding.run_id}")
        if any(
            item.scope_ref not in allowed
            and not (
                item.scope_ref == "global-user" and item.kind is ContextCandidateKind.SYSTEM_POLICY
            )
            and not (
                item.scope_ref == "global-user"
                and item.kind is ContextCandidateKind.KNOWLEDGE
                and "global-user" in request.additional_scope_refs
            )
            for item in self.selected_provenance
        ):
            raise PolicyViolation("Context selected candidate scope mismatch")


class ContinuitySourceResolver(Protocol):
    def __call__(self, binding: ContinuityBinding, provenance: dict[str, Any]) -> str: ...


class LocalContinuityStore(Protocol):
    def inspect(self, binding: ContinuityBinding) -> dict[str, Any]: ...

    def spool_digests(self, binding: ContinuityBinding) -> tuple[str, ...]: ...

    def source_content_digest(self, binding: ContinuityBinding) -> str: ...

    def bind_session(self, binding: ContinuityBinding) -> str: ...

    def append_event(
        self, binding: ContinuityBinding, event: ContinuityEvent, *, expected_tail: ContinuityTail
    ) -> ContinuityTail: ...

    def tail(self, binding: ContinuityBinding) -> ContinuityTail: ...

    def hydrate(
        self,
        binding: ContinuityBinding,
        context: LocalContext,
        *,
        idempotency_key: str,
        checkpoint_digest: str | None = None,
    ) -> str: ...

    def checkpoint(
        self,
        binding: ContinuityBinding,
        *,
        expected_tail: ContinuityTail,
        context_digest: str,
        idempotency_key: str,
        spool_digests: tuple[str, ...],
    ) -> str: ...

    def resume(self, binding: ContinuityBinding, checkpoint_digest: str) -> dict[str, Any]: ...
