from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from zekam.application.config import DiagnosticTraceSettings
from zekam.application.diagnostic_trace import (
    AesGcmTraceCipher,
    DiagnosticTraceReducer,
    DiagnosticTraceRetentionService,
    DiagnosticTraceWriter,
    TraceEventMetadata,
    export_trace_graph,
)
from zekam.application.diagnostic_trace_composition import (
    compose_diagnostic_trace_purge_handler,
    compose_diagnostic_trace_sink,
)
from zekam.domain.canonical import digest
from zekam.domain.diagnostic_trace import (
    DiagnosticTracePolicy,
    ReducedTrace,
    TraceBundle,
    TraceEventRecord,
    TraceEventType,
    TracePurgeCandidate,
    TraceVisibility,
    trace_event_body,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

NOW = dt.datetime(2026, 8, 25, 8, tzinfo=dt.UTC)
KEY = b"k" * 32


def test_production_composition_disabled_is_noop_without_storage(tmp_path: Path) -> None:
    assert (
        compose_diagnostic_trace_sink(
            connection=object(),
            realm_id=uuid4(),
            home=tmp_path,
            settings=DiagnosticTraceSettings(enabled=False),
            environ={},
        )
        is None
    )
    assert (
        compose_diagnostic_trace_purge_handler(
            connection=object(), realm_id=uuid4(), home=tmp_path, environ={}
        )
        is None
    )


def test_enabled_production_composition_requires_exact_runtime_binding(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation, match="ZEKAM_DIAGNOSTIC_TRACE_ID"):
        compose_diagnostic_trace_sink(
            connection=object(),
            realm_id=uuid4(),
            home=tmp_path,
            settings=DiagnosticTraceSettings(
                enabled=True, encryption_key_ref="secretref:trace-key"
            ),
            environ={},
        )


class MemoryTraceRepository:
    def __init__(self) -> None:
        self.bundles: dict[UUID, TraceBundle] = {}
        self.events: dict[UUID, list[TraceEventRecord]] = {}
        self.reductions: dict[str, ReducedTrace] = {}
        self.purged: dict[UUID, str] = {}

    def create_bundle(self, bundle: TraceBundle) -> tuple[UUID, bool]:
        created = bundle.id not in self.bundles
        self.bundles[bundle.id] = bundle
        self.events.setdefault(bundle.id, [])
        return bundle.id, created

    def usage(self, bundle_id: UUID) -> tuple[int, int]:
        events = self.events.get(bundle_id, [])
        return len(events), sum(item.payload_size_bytes for item in events)

    def append_event(self, bundle: TraceBundle, metadata: TraceEventMetadata) -> TraceEventRecord:
        prior = self.events[bundle.id][-1] if self.events[bundle.id] else None
        sequence = len(self.events[bundle.id]) + 1
        previous = None if prior is None else prior.event_digest
        body = trace_event_body(
            id=metadata.id,
            realm_id=bundle.realm_id,
            bundle_id=bundle.id,
            sequence=sequence,
            event_type=metadata.event_type,
            visibility=metadata.visibility,
            occurred_at=metadata.occurred_at,
            correlation=metadata.correlation,
            payload_ref=metadata.payload_ref,
            payload_cipher_digest=metadata.payload_cipher_digest,
            payload_plain_digest=metadata.payload_plain_digest,
            payload_size_bytes=metadata.payload_size_bytes,
            encryption_key_ref=metadata.encryption_key_ref,
            redaction_digest=metadata.redaction_digest,
            previous_event_digest=previous,
        )
        event = TraceEventRecord(
            id=metadata.id,
            realm_id=bundle.realm_id,
            bundle_id=bundle.id,
            sequence=sequence,
            event_type=metadata.event_type,
            visibility=metadata.visibility,
            occurred_at=metadata.occurred_at,
            correlation=metadata.correlation,
            payload_ref=metadata.payload_ref,
            payload_cipher_digest=metadata.payload_cipher_digest,
            payload_plain_digest=metadata.payload_plain_digest,
            payload_size_bytes=metadata.payload_size_bytes,
            encryption_key_ref=metadata.encryption_key_ref,
            redaction_digest=metadata.redaction_digest,
            previous_event_digest=previous,
            event_digest=digest(body),
        )
        self.events[bundle.id].append(event)
        return event

    def list_events(
        self, bundle_id: UUID, *, authorization_ref: str | None = None
    ) -> tuple[TraceEventRecord, ...]:
        del authorization_ref
        return tuple(self.events[bundle_id])

    def store_reduction(
        self, reduced: ReducedTrace, *, authorization_ref: str | None = None
    ) -> tuple[UUID, bool]:
        del authorization_ref
        created = reduced.output_digest not in self.reductions
        self.reductions[reduced.output_digest] = reduced
        return uuid4(), created

    def expired_candidates(
        self, *, now: dt.datetime, limit: int
    ) -> tuple[TracePurgeCandidate, ...]:
        return tuple(
            TracePurgeCandidate(
                item.id,
                tuple(event.payload_ref for event in self.events[item.id]),
                item.expires_at,
            )
            for item in tuple(self.bundles.values())[:limit]
            if item.expires_at <= now and item.id not in self.purged
        )

    def mark_purged(
        self,
        bundle_id: UUID,
        *,
        purged_at: dt.datetime,
        purge_receipt_digest: str,
        authorization_ref: str,
    ) -> None:
        del purged_at, authorization_ref
        self.purged[bundle_id] = purge_receipt_digest


def _runtime(tmp_path: Path, policy: DiagnosticTracePolicy | None = None):  # type: ignore[no-untyped-def]
    repository = MemoryTraceRepository()
    store = LocalContentAddressedStore(tmp_path / "trace-cas").ensure()
    cipher = AesGcmTraceCipher(lambda size: b"n" * size)
    selected = policy or DiagnosticTracePolicy(
        enabled=True,
        encryption_key_ref="secretref:trace-key-v1",
        export_allowed=True,
    )
    writer = DiagnosticTraceWriter(repository, store, cipher, lambda _: KEY)
    bundle = writer.open_bundle(
        realm_id=uuid4(),
        trace_ref="trace-unit",
        policy=selected,
        project_id=None,
        work_item_id=None,
        run_id=None,
        root_assignment_id=None,
        root_client_session_id="session-unit",
        now=NOW,
    )
    return repository, store, cipher, selected, writer, bundle


def test_disabled_trace_is_true_noop_and_never_touches_store(tmp_path: Path) -> None:
    policy = DiagnosticTracePolicy(enabled=False)
    repository, store, _, _, writer, bundle = _runtime(tmp_path, policy)
    assert bundle is None
    result = writer.write(
        bundle=None,
        policy=policy,
        event_type=TraceEventType.RUNTIME_STATE,
        visibility=TraceVisibility.DIAGNOSTIC_ONLY,
        payload={"state": "ignored"},
        correlation={"session": "one"},
        occurred_at=NOW,
    )
    assert result.state == "disabled" and result.grants_authority is False
    assert repository.bundles == {}
    assert tuple(store.iter_objects()) == ()


def test_writer_redacts_then_encrypts_before_metadata_reference(tmp_path: Path) -> None:
    repository, store, cipher, policy, writer, bundle = _runtime(tmp_path)
    assert bundle is not None
    result = writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.MODEL_REQUEST,
        visibility=TraceVisibility.MODEL_VISIBLE,
        payload={
            "password": "never-store-me",
            "path": r"C:\Users\mkaracan\private\file.txt",
            "email": "person@example.com",
            "content": "safe",
        },
        correlation={"session": "one"},
        occurred_at=NOW,
    )
    event = repository.events[bundle.id][0]
    ciphertext = store.get(event.payload_ref)
    assert b"never-store-me" not in ciphertext
    assert b"mkaracan" not in ciphertext
    plaintext = cipher.decrypt(ciphertext, key=KEY, aad=f"{bundle.id}:{event.id}".encode())
    assert b"never-store-me" not in plaintext
    assert b"mkaracan" not in plaintext
    assert b"person@example.com" not in plaintext
    assert b"[REDACTED]" in plaintext
    assert result.state == "recorded"


def test_only_final_provider_bytes_can_be_model_visible(tmp_path: Path) -> None:
    _, _, _, policy, writer, bundle = _runtime(tmp_path)
    assert bundle is not None
    with pytest.raises(PolicyViolation, match="final provider"):
        writer.write(
            bundle=bundle,
            policy=policy,
            event_type=TraceEventType.TOOL_RESULT,
            visibility=TraceVisibility.MODEL_VISIBLE,
            payload={"result": "runtime-only"},
            correlation={"session": "one"},
            occurred_at=NOW,
        )


def test_reducer_is_deterministic_and_keeps_runtime_objects_separate(tmp_path: Path) -> None:
    repository, store, cipher, policy, writer, bundle = _runtime(tmp_path)
    assert bundle is not None
    first = writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.MODEL_REQUEST,
        visibility=TraceVisibility.MODEL_VISIBLE,
        payload={"content": "serialized provider bytes"},
        correlation={"session": "one"},
        occurred_at=NOW,
    )
    writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.TOOL_RESULT,
        visibility=TraceVisibility.RUNTIME_ONLY,
        payload={"result": "terminal evidence"},
        correlation={"session": "one", "parent_event_id": str(first.event_id)},
        occurred_at=NOW + dt.timedelta(seconds=1),
    )
    reducer = DiagnosticTraceReducer(repository, store, cipher, lambda _: KEY)
    closed = replace(bundle, state="closed")
    one = reducer.reduce(closed, reduced_at=NOW + dt.timedelta(minutes=1))
    two = reducer.reduce(closed, reduced_at=NOW + dt.timedelta(minutes=2))
    assert one.output_digest == two.output_digest
    assert [node.kind for node in one.nodes] == ["ConversationItem", "ToolCall"]
    assert one.edges[-1].kind == "caused"
    assert one.grants_authority is False
    exported = export_trace_graph(one, policy=policy, authorization_ref="auth:exact")
    assert b"serialized provider bytes" not in exported
    assert b"terminal evidence" not in exported


def test_reducer_fails_closed_for_gap_reorder_and_missing_payload(tmp_path: Path) -> None:
    repository, store, cipher, policy, writer, bundle = _runtime(tmp_path)
    assert bundle is not None
    writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.RUNTIME_STATE,
        visibility=TraceVisibility.RUNTIME_ONLY,
        payload={"state": "one"},
        correlation={"session": "one"},
        occurred_at=NOW,
    )
    writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.RUNTIME_STATE,
        visibility=TraceVisibility.RUNTIME_ONLY,
        payload={"state": "two"},
        correlation={"session": "one"},
        occurred_at=NOW + dt.timedelta(seconds=1),
    )
    originals = list(repository.events[bundle.id])
    repository.events[bundle.id].reverse()
    reducer = DiagnosticTraceReducer(repository, store, cipher, lambda _: KEY)
    closed = replace(bundle, state="closed")
    with pytest.raises(ValidationFailed, match="gap/reorder"):
        reducer.reduce(closed, reduced_at=NOW)
    repository.events[bundle.id] = originals
    store.delete(originals[0].payload_ref)
    with pytest.raises(ValidationFailed, match="payload object missing"):
        reducer.reduce(closed, reduced_at=NOW)


def test_reducer_normalizes_wrong_key_and_cipher_tamper_to_validation_failure(
    tmp_path: Path,
) -> None:
    repository, store, cipher, policy, writer, bundle = _runtime(tmp_path)
    assert bundle is not None
    writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.RUNTIME_STATE,
        visibility=TraceVisibility.RUNTIME_ONLY,
        payload={"state": "one"},
        correlation={"session": "one"},
        occurred_at=NOW,
    )
    closed = replace(bundle, state="closed")
    wrong_key_reducer = DiagnosticTraceReducer(repository, store, cipher, lambda _: b"x" * 32)
    with pytest.raises(ValidationFailed, match="authentication"):
        wrong_key_reducer.reduce(closed, reduced_at=NOW)

    event = repository.events[bundle.id][0]
    ciphertext = store.get(event.payload_ref)
    content_path, _ = store._paths(event.payload_ref)
    content_path.write_bytes(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
    with pytest.raises(ValidationFailed, match="digest ile uyusmuyor"):
        DiagnosticTraceReducer(repository, store, cipher, lambda _: KEY).reduce(
            closed, reduced_at=NOW
        )


def test_best_effort_failure_cannot_escape_into_canonical_flow(tmp_path: Path) -> None:
    _, store, _, policy, writer, bundle = _runtime(tmp_path)
    assert bundle is not None

    def unavailable(_: str) -> bytes:
        raise RuntimeError("key broker unavailable")

    writer.key_resolver = unavailable
    result = writer.write_best_effort(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.ERROR,
        visibility=TraceVisibility.DIAGNOSTIC_ONLY,
        payload={"category": "safe"},
        correlation={"session": "one"},
        occurred_at=NOW,
    )
    assert result.state == "failed" and result.error_category == "RuntimeError"
    assert tuple(store.iter_objects()) == ()


def test_retention_service_deletes_expired_ciphertext_and_records_receipt(tmp_path: Path) -> None:
    repository, store, _, policy, writer, bundle = _runtime(tmp_path)
    assert bundle is not None
    writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.ERROR,
        visibility=TraceVisibility.DIAGNOSTIC_ONLY,
        payload={"category": "temporary"},
        correlation={"session": "one"},
        occurred_at=NOW,
    )
    result = DiagnosticTraceRetentionService(repository, store).purge_expired(
        now=bundle.expires_at + dt.timedelta(seconds=1),
        authorization_ref="auth:purge-one",
    )
    assert result.purged_trace_ids == (bundle.id,)
    assert result.deleted_payload_count == 1
    assert result.missing_payload_count == 0
    assert tuple(store.iter_objects()) == ()
    assert repository.purged[bundle.id].startswith("sha256:")


def test_trace_quota_and_export_policy_fail_closed(tmp_path: Path) -> None:
    policy = DiagnosticTracePolicy(
        enabled=True,
        max_payload_bytes=64,
        max_events=1,
        max_total_bytes=64,
        encryption_key_ref="secretref:key",
    )
    _, _, _, _, writer, bundle = _runtime(tmp_path, policy)
    assert bundle is not None
    writer.write(
        bundle=bundle,
        policy=policy,
        event_type=TraceEventType.ERROR,
        visibility=TraceVisibility.DIAGNOSTIC_ONLY,
        payload={"x": "one"},
        correlation={"session": "one"},
        occurred_at=NOW,
    )
    with pytest.raises(PolicyViolation, match="kotasi"):
        writer.write(
            bundle=bundle,
            policy=policy,
            event_type=TraceEventType.ERROR,
            visibility=TraceVisibility.DIAGNOSTIC_ONLY,
            payload={"x": "two"},
            correlation={"session": "one"},
            occurred_at=NOW,
        )
