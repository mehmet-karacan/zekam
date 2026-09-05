"""WP16 branch probes for application validation and fail-closed boundaries."""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast
from uuid import UUID, uuid4

import pytest

from zekam.application import doctor_repair_runtime as doctor_module
from zekam.application import lifecycle_runtime_template_prepare as lifecycle_module
from zekam.application import recovery_reconciliation as recovery_module
from zekam.application.diagnostic_trace import (
    AesGcmTraceCipher,
    DiagnosticTraceRetentionService,
    DiagnosticTraceWriter,
    TraceWriteResult,
    _reduced_node_kind,
    _sanitize,
    decode_trace_key,
    export_trace_graph,
)
from zekam.application.doctor_repair_runtime import (
    _apply_step,
    _assert_actor_and_project,
    _effect_request,
    apply_doctor_repair_with_runtime,
)
from zekam.application.governance import EffectRequest
from zekam.application.lifecycle_runtime_template_prepare import (
    LifecycleRuntimeTemplatePrepareService,
    LifecycleTemplatePreparePlan,
    materialize_lifecycle_template,
    run_lifecycle_template_prepare_once,
)
from zekam.application.local_continuity import ContinuityBinding, ContinuityTail
from zekam.application.local_continuity_close import CloseSummary
from zekam.application.local_continuity_v4_writer import (
    CanonicalManifestProvenance,
    CurrentSourceSnapshot,
    ExactResolvedRecovery,
    FinalizeClosedWriteRequest,
    FrozenCloseWriteRequest,
    FrozenProjectionSnapshot,
    FrozenSpoolSnapshot,
    ResolvedManifestFragment,
    VerifiedManifest,
    VerifiedManifestSelection,
    _exact_binding,
    _key,
    _whole_second,
    revision_digest,
    verify_persisted_context_manifest,
)
from zekam.application.opencode_benchmark_campaign import (
    AUDIO_EXCLUSION_REASON,
    OpenCodeCampaignScope,
    ScopeTarget,
    ScopeVerifier,
    _exact_mapping,
    _operation,
    _operation_endpoint,
    _provider_payload,
    _validate_target_record,
    load_campaign_scope,
    normalize_provider_response,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.diagnostic_trace import (
    DiagnosticTracePolicy,
    ReducedTrace,
    TraceEventType,
    TracePurgeCandidate,
    TraceVisibility,
)
from zekam.domain.errors import (
    AuthorizationRequired,
    ConfigurationError,
    NotFound,
    PolicyViolation,
    ValidationFailed,
)
from zekam.domain.model_inventory import Modality
from zekam.domain.realm import ActorKind, LifecycleStatus
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import JobKind, JobState, ReceiptStatus
from zekam.domain.work import EffectKind, WorkState

pytestmark = pytest.mark.unit

NOW = "2026-09-03T12:00:00+00:00"


def _binding() -> ContinuityBinding:
    return ContinuityBinding(
        "018f0000-0000-7000-8000-000000000001",
        "external-session",
        "018f0000-0000-7000-8000-000000000002",
        "018f0000-0000-7000-8000-000000000003",
        "codex",
        "macbook",
        "018f0000-0000-7000-8000-000000000004",
        digest("task"),
        digest("plan"),
        digest("policy"),
    )


def _provenance(candidate: str = "candidate") -> CanonicalManifestProvenance:
    body = {"id": candidate, "kind": "knowledge"}
    return CanonicalManifestProvenance(candidate, canonical_json(body), digest(body))


def _summary() -> CloseSummary:
    return CloseSummary(
        ("done",),
        (),
        (),
        ("next",),
        "continue",
        (("src/example.py", digest("source")),),
        (("context/example", digest("context")),),
    )


def _freeze_request(**changes: object) -> FrozenCloseWriteRequest:
    values: dict[str, object] = {
        "binding": _binding(),
        "expected_attachment_revision_digest": digest("revision"),
        "expected_process_generation_digest": digest("generation"),
        "expected_tail": ContinuityTail(1, digest("tail")),
        "active_manifest_digest": digest("manifest"),
        "checkpoint_idempotency_key": "checkpoint",
        "operation_key": "operation",
        "summary": _summary(),
        "candidates": None,
        "observed_at": NOW,
    }
    values.update(changes)
    return FrozenCloseWriteRequest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("factory", "args"),
    [
        (CurrentSourceSnapshot, ("snapshot", "revision", 1)),
        (CanonicalManifestProvenance, ("candidate", 1, digest({}))),
        (CanonicalManifestProvenance, ("candidate", "", digest({}))),
        (CanonicalManifestProvenance, ("candidate", "[]", digest([]))),
        (CanonicalManifestProvenance, ("candidate", "{} ", digest({}))),
        (CanonicalManifestProvenance, ("candidate", "{}", digest("wrong"))),
        (ResolvedManifestFragment, ("candidate", None)),
        (ResolvedManifestFragment, ("candidate", "")),
        (VerifiedManifestSelection, ("candidate", "source", 1, _provenance())),
        (VerifiedManifestSelection, ("candidate", "source", digest("x"), object())),
        (VerifiedManifestSelection, ("other", "source", digest("x"), _provenance())),
    ],
)
def test_writer_value_objects_reject_noncanonical_values(
    factory: Any, args: tuple[object, ...]
) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        factory(*args)


def test_writer_value_objects_cover_utf8_and_size_bounds() -> None:
    with pytest.raises(ValidationFailed, match="UTF-8"):
        CanonicalManifestProvenance("candidate", "\ud800", digest({}))
    with pytest.raises(ValidationFailed, match="byte bound"):
        CanonicalManifestProvenance("candidate", "{" + "x" * 1_048_576, digest({}))
    with pytest.raises(ValidationFailed, match="UTF-8"):
        ResolvedManifestFragment("candidate", "\ud800")
    with pytest.raises(ValidationFailed, match="exceeds"):
        ResolvedManifestFragment("candidate", "x" * 1_048_577)


@pytest.mark.parametrize(
    "changes",
    [
        {"body_digest": 1},
        {"checkpoint_digest": 1},
        {"token_budget": 0},
        {"token_count": -1},
        {"token_budget": 1, "token_count": 2},
        {"selected": []},
        {"selected": (object(),)},
        {"fragments": (("candidate",),)},
        {"fragments": ((1, "text"),)},
        {"fragments": (("candidate", 1),)},
        {"fragments": (("candidate", "text"), ("candidate", "again"))},
    ],
)
def test_verified_manifest_rejects_every_mutable_or_drifting_partition(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "body_digest": digest("body"),
        "checkpoint_digest": None,
        "token_budget": 10,
        "token_count": 0,
        "selected": (),
        "fragments": (),
    }
    values.update(changes)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        VerifiedManifest(**values)  # type: ignore[arg-type]


def test_writer_snapshot_and_revision_fail_closed_matrix() -> None:
    with pytest.raises(ValidationFailed):
        FrozenSpoolSnapshot("session", "external", "codex", [])  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        FrozenSpoolSnapshot("session", "external", "codex", ())
    with pytest.raises(ValidationFailed):
        FrozenSpoolSnapshot("session", "external", "codex", (1,))  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        FrozenSpoolSnapshot("session", "external", "codex", (digest("a"), digest("a")))
    with pytest.raises(ValidationFailed):
        FrozenProjectionSnapshot([])  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        FrozenProjectionSnapshot(({"portable_ref": "a"},))
    with pytest.raises(ValidationFailed):
        FrozenProjectionSnapshot(
            ({"portable_ref": "a", "content_digest": 1, "bytes_digest": digest("b")},)  # type: ignore[dict-item]
        )
    with pytest.raises(ValidationFailed):
        FrozenProjectionSnapshot(
            tuple(
                {"portable_ref": ref, "content_digest": digest(ref), "bytes_digest": digest(ref)}
                for ref in ("z", "a")
            )
        )
    with pytest.raises(ValidationFailed, match="omit"):
        revision_digest({"revision_digest": digest("x")})


def test_writer_private_canonical_guards_cover_exact_types_and_bounds() -> None:
    with pytest.raises(ValidationFailed):
        _key(1, "key")
    with pytest.raises(ValidationFailed):
        _key("x" * 513, "key")
    with pytest.raises(ValidationFailed):
        _whole_second(1)
    with pytest.raises(ValidationFailed):
        _whole_second("2026-09-03T12:00:00Z")

    class Text(str):
        pass

    malformed = dataclasses.replace(
        _binding(),
        work_item_id=Text("018f0000-0000-7000-8000-000000000007"),
        run_id=Text("018f0000-0000-7000-8000-000000000008"),
    )
    with pytest.raises(ValidationFailed, match="optional"):
        _exact_binding(malformed)


@pytest.mark.parametrize(
    "changes",
    [
        {"binding": object()},
        {"expected_attachment_revision_digest": 1},
        {"expected_tail": object()},
        {"summary": object()},
        {"candidates": object()},
    ],
)
def test_freeze_request_rejects_exact_contract_drift(changes: dict[str, object]) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        _freeze_request(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"binding": object()},
        {"request_digest": 1},
        {"recovery": object()},
    ],
)
def test_finalize_request_rejects_exact_contract_drift(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "binding": _binding(),
        "request_digest": digest("request"),
        "expected_frozen_revision_digest": digest("frozen"),
        "operation_key": "finalize",
        "finalized_at": NOW,
        "recovery": None,
    }
    values.update(changes)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        FinalizeClosedWriteRequest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["partitions", "metrics", "checkpoint"])
def test_persisted_manifest_rejects_deeper_partition_shapes(kind: str) -> None:
    compiler: dict[str, object] = {
        "selected": [],
        "omitted": [],
        "compiler_metrics": {
            "selected_count": 0,
            "selected_tokens": 0,
            "token_budget": 1,
            "omitted_count": 0,
        },
        "token_budget": 1,
    }
    context: dict[str, object] = {
        "compiler": compiler,
        "ranking_request": {},
        "fragments": {},
        "selected_provenance": [],
    }
    if kind == "partitions":
        compiler["selected"] = {}
    elif kind == "metrics":
        compiler["compiler_metrics"] = []
    else:
        context["checkpoint_digest"] = digest("checkpoint")
    body = {"context": context, "checkpoint_digest": context.get("checkpoint_digest")}
    body_json = canonical_json(body)
    with pytest.raises(PolicyViolation):
        verify_persisted_context_manifest(
            binding=_binding(),
            manifest_digest=digest(body),
            row_columns={"token_count": 0},
            body_json=body_json,
            active_hydration_receipt={"idempotency_key": "key"},
            db_source_revision="revision",
            port_source_revision="revision",
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"predecessor_revision_digest": 1},
        {"recovery_case_kind": 1},
        {"recovery_case_id": 1},
        {"recovery_resolution_id": 1},
        {"outcome": 1},
        {"recovered_at": 1},
        {"recovered_at": "2026-09-03T12:00:00.1+00:00"},
    ],
)
def test_exact_recovery_rejects_type_and_time_drift(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "predecessor_revision_digest": digest("predecessor"),
        "recovery_case_kind": "hook",
        "recovery_case_id": "018f0000-0000-7000-8000-000000000005",
        "recovery_resolution_id": "018f0000-0000-7000-8000-000000000006",
        "outcome": "restored",
        "recovered_at": NOW,
    }
    values.update(kwargs)
    with pytest.raises(ValidationFailed):
        ExactResolvedRecovery(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"manifest_digest": 1},
        {"db_source_revision": 1},
        {"body_json": ""},
        {"body_json": "[]"},
        {"body_json": "{} "},
        {"body_json": "{"},
        {"body_json": canonical_json({"context": {}})},
    ],
)
def test_persisted_manifest_early_fail_closed_paths(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "binding": _binding(),
        "manifest_digest": digest("manifest"),
        "row_columns": {},
        "body_json": canonical_json({}),
        "active_hydration_receipt": {},
        "db_source_revision": "revision",
        "port_source_revision": "revision",
    }
    values.update(kwargs)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        verify_persisted_context_manifest(**values)  # type: ignore[arg-type]


def _scope_target(modality: Modality = Modality.CHAT, **changes: object) -> ScopeTarget:
    values: dict[str, object] = {
        "configured_model_id": "configured",
        "canonical_model_ids": ("canonical",),
        "modality": modality,
        "workload": "workload",
    }
    values.update(changes)
    return ScopeTarget(**values)  # type: ignore[arg-type]


def test_campaign_scope_value_objects_fail_closed_matrix() -> None:
    for verifier_values in (("", "exec"), ("model", "")):
        with pytest.raises(ValidationFailed):
            ScopeVerifier(*verifier_values)
    scope_changes: tuple[dict[str, object], ...] = (
        {"configured_model_id": ""},
        {"canonical_model_ids": ()},
        {"canonical_model_ids": ("a", "a")},
        {"canonical_model_ids": ("a", "b")},
        {"excluded_reason": "hidden"},
    )
    for changes in scope_changes:
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _scope_target(**changes)  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation):
        _scope_target(Modality.AUDIO_TRANSCRIPTION)
    assert (
        _scope_target(
            Modality.AUDIO_TRANSCRIPTION, excluded_reason=AUDIO_EXCLUSION_REASON
        ).excluded_reason
        == AUDIO_EXCLUSION_REASON
    )
    verifier = ScopeVerifier("model", "exec")
    good = _scope_target()
    campaign_changes: tuple[dict[str, object], ...] = (
        {"version": 0},
        {"repetitions": 4},
        {"provider_id": "other"},
        {"provider_family": "other"},
        {"scope_policy": "other"},
        {"targets": (good, good)},
        {"targets": (good, dataclasses.replace(good, configured_model_id="two"))},
    )
    for changes in campaign_changes:
        scope_values: dict[str, object] = {
            "version": 1,
            "provider_id": "litellm",
            "provider_family": "aihub",
            "scope_policy": "configured-canonical-all",
            "repetitions": 5,
            "verifier": verifier,
            "targets": (good,),
        }
        scope_values.update(changes)
        with pytest.raises((ValidationFailed, PolicyViolation)):
            OpenCodeCampaignScope(**scope_values)  # type: ignore[arg-type]


@pytest.mark.parametrize("modality", list(Modality))
def test_campaign_operation_and_payload_matrix(modality: Modality) -> None:
    artifact = cast(
        Any,
        SimpleNamespace(
            payload={
                "messages": [],
                "instruction": "code",
                "prompt": "text",
                "input": ["one"],
                "query": "q",
                "documents": ["d"],
                "question": "q",
                "samples": ["safe"],
            }
        ),
    )
    if modality in {Modality.AUDIO_TRANSCRIPTION, Modality.UNKNOWN}:
        with pytest.raises(PolicyViolation):
            _operation(modality)
        with pytest.raises(PolicyViolation):
            _provider_payload(modality, backend_model="model", artifact=artifact)
    else:
        _operation(modality)
        _provider_payload(modality, backend_model="model", artifact=artifact)


def test_campaign_endpoint_and_mapping_validation() -> None:
    assert _exact_mapping({}, label="test") == {}
    with pytest.raises(ConfigurationError):
        _exact_mapping([], label="test")
    with pytest.raises(ConfigurationError):
        _operation_endpoint("https://host/v1/chat", "/chat/completions")
    assert _operation_endpoint("https://host/v1/embeddings", "/rerank")[1] == "/v1/rerank"


@pytest.mark.parametrize(
    "document",
    ["[]", "{}", "schema: wrong", "schema: zekam-opencode-benchmark-scope/v1\ntargets: []"],
)
def test_campaign_scope_parser_rejects_partial_documents(tmp_path: Path, document: str) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_campaign_scope(path)


@pytest.mark.parametrize(
    "document",
    [
        (
            "schema: zekam-opencode-benchmark-scope/v1\nversion: 1\n"
            "provider_id: litellm\nprovider_family: aihub\n"
            "scope_policy: configured-canonical-all\nrepetitions: 5\n"
            "verifier: []\ntargets: [x]"
        ),
        (
            "schema: zekam-opencode-benchmark-scope/v1\nversion: 1\n"
            "provider_id: litellm\nprovider_family: aihub\n"
            "scope_policy: configured-canonical-all\nrepetitions: 5\n"
            "verifier: {model_id: a}\ntargets: [x]"
        ),
        (
            "schema: zekam-opencode-benchmark-scope/v1\nversion: 1\n"
            "provider_id: litellm\nprovider_family: aihub\n"
            "scope_policy: configured-canonical-all\nrepetitions: 5\n"
            "verifier: {model_id: a, execution_identity: b}\ntargets: x"
        ),
        (
            "schema: zekam-opencode-benchmark-scope/v1\nversion: 1\n"
            "provider_id: litellm\nprovider_family: aihub\n"
            "scope_policy: configured-canonical-all\nrepetitions: 5\n"
            "verifier: {model_id: a, execution_identity: b}\n"
            "targets: [{configured_model_id: a}]"
        ),
        (
            "schema: zekam-opencode-benchmark-scope/v1\nversion: 1\n"
            "provider_id: litellm\nprovider_family: aihub\n"
            "scope_policy: configured-canonical-all\nrepetitions: 5\n"
            "verifier: {model_id: a, execution_identity: b}\n"
            "targets: [{configured_model_id: a, canonical_model_ids: x, "
            "modality: chat, workload: w}]"
        ),
    ],
)
def test_campaign_scope_parser_rejects_nested_shape_drift(tmp_path: Path, document: str) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_campaign_scope(path)


def test_campaign_response_normalization_all_modalities(monkeypatch: pytest.MonkeyPatch) -> None:
    module = __import__("zekam.application.opencode_benchmark_campaign", fromlist=["x"])
    monkeypatch.setattr(module, "openai_chat_text", lambda _: '{"ok":true}')
    monkeypatch.setattr(module, "openai_embeddings", lambda _: ((1.0,),))
    monkeypatch.setattr(module, "openai_rerank_scores", lambda _: (0.5,))
    monkeypatch.setattr(module, "openai_vision_objects", lambda _: ("object",))
    monkeypatch.setattr(
        module, "openai_guardrail_labels", lambda _r, expected_count: ("safe",) * expected_count
    )
    artifact = cast(Any, SimpleNamespace(payload={"samples": ["one"]}))
    for modality in (
        Modality.CHAT,
        Modality.CODE,
        Modality.COMPLETION,
        Modality.EMBEDDING,
        Modality.RERANK,
        Modality.VISION_LANGUAGE,
        Modality.GUARDRAIL,
    ):
        assert normalize_provider_response(modality, {}, artifact=artifact)
    monkeypatch.setattr(module, "openai_chat_text", lambda _: "not-json")
    assert normalize_provider_response(Modality.CHAT, {}, artifact=artifact) == {"json": None}
    with pytest.raises(PolicyViolation):
        normalize_provider_response(Modality.AUDIO_TRANSCRIPTION, {}, artifact=artifact)


@pytest.mark.parametrize("case", ["route", "modality", "conflict", "disabled"])
def test_campaign_inventory_record_validation_rejects_drift(case: str) -> None:
    target = _scope_target()
    record = SimpleNamespace(
        access_name="configured",
        backend_model="backend",
        modality=Modality.CHAT,
        modality_conflict=None,
        enabled=True,
    )
    if case == "route":
        record.access_name = "other"
    elif case == "modality":
        record.modality = Modality.CODE
    elif case == "conflict":
        record.modality_conflict = "ambiguous"
    elif case == "disabled":
        record.enabled = False
    with pytest.raises(PolicyViolation):
        _validate_target_record(target, cast(Any, record))


def test_campaign_inventory_record_validation_accepts_exact_record() -> None:
    record = SimpleNamespace(
        access_name="configured",
        backend_model="backend",
        modality=Modality.CHAT,
        modality_conflict=None,
        enabled=True,
    )
    _validate_target_record(_scope_target(), cast(Any, record))


def test_trace_key_cipher_sanitize_and_result_fail_closed() -> None:
    key = b"k" * 32
    assert decode_trace_key(key.hex()) == key
    assert decode_trace_key(base64.b64encode(key).decode()) == key
    for raw in ("not base64", base64.b64encode(b"short").decode()):
        with pytest.raises(ValidationFailed):
            decode_trace_key(raw)
    cipher = AesGcmTraceCipher(lambda size: b"n" * size)
    with pytest.raises(PolicyViolation):
        cipher.encrypt(b"x", key=b"bad", aad=b"a")
    with pytest.raises(ValidationFailed):
        AesGcmTraceCipher(lambda size: b"n").encrypt(b"x", key=key, aad=b"a")
    with pytest.raises(PolicyViolation):
        cipher.decrypt(b"short", key=key, aad=b"a")
    encrypted = cipher.encrypt(b"x", key=key, aad=b"a")
    assert cipher.decrypt(encrypted, key=key, aad=b"a") == b"x"
    assert _sanitize({"password": "secret", "items": (None, True, 1, 1.5)}) == {
        "password": "[REDACTED]",
        "items": [None, True, 1, 1.5],
    }
    assert _sanitize("person@example.com") == "[REDACTED]"
    assert _sanitize("/Users/example/private") == "[REDACTED]"
    assert _sanitize("safe") == "safe"
    with pytest.raises(ValidationFailed):
        _sanitize(object())
    with pytest.raises(ValidationFailed):
        TraceWriteResult("unknown", None, None, None)
    with pytest.raises(PolicyViolation):
        TraceWriteResult("recorded", None, None, None, grants_authority=True)


@pytest.mark.parametrize(
    ("event_type", "visibility", "expected"),
    [
        (TraceEventType.MODEL_REQUEST, TraceVisibility.MODEL_VISIBLE, "ConversationItem"),
        (TraceEventType.MODEL_REQUEST_PREPARED, TraceVisibility.RUNTIME_ONLY, "InferenceCall"),
        (TraceEventType.TOOL_REQUEST, TraceVisibility.RUNTIME_ONLY, "ToolCall"),
        (TraceEventType.TERMINAL_OUTPUT, TraceVisibility.RUNTIME_ONLY, "TerminalOperation"),
        (TraceEventType.AGENT_SPAWN, TraceVisibility.RUNTIME_ONLY, "AgentThread"),
        (TraceEventType.COMPACTION_REQUESTED, TraceVisibility.RUNTIME_ONLY, "Compaction"),
        (TraceEventType.ENVIRONMENT_PROBED, TraceVisibility.RUNTIME_ONLY, "EnvironmentSnapshot"),
        (TraceEventType.RUNTIME_STATE, TraceVisibility.RUNTIME_ONLY, "RawPayloadRef"),
    ],
)
def test_trace_reduced_node_kind_matrix(
    event_type: TraceEventType, visibility: TraceVisibility, expected: str
) -> None:
    assert _reduced_node_kind(event_type, visibility) == expected


def test_trace_retention_and_export_boundaries() -> None:
    service = DiagnosticTraceRetentionService(cast(Any, object()), cast(Any, object()))
    for now, authorization_ref, limit in (
        (dt.datetime(2026, 1, 1), "auth", 1),
        (dt.datetime(2026, 1, 1, tzinfo=dt.UTC), " ", 1),
        (dt.datetime(2026, 1, 1, tzinfo=dt.UTC), "auth", 0),
    ):
        with pytest.raises(ValidationFailed):
            service.purge_expired(
                now=now,
                authorization_ref=authorization_ref,
                limit=limit,
            )
    policy = DiagnosticTracePolicy(enabled=False)
    reduced = cast(ReducedTrace, SimpleNamespace(body=lambda: {}, output_digest=digest("x")))
    with pytest.raises(PolicyViolation):
        export_trace_graph(reduced, policy=policy, authorization_ref="auth")


class _TraceRepo:
    def __init__(self, usage: tuple[int, int] = (0, 0)) -> None:
        self._usage = usage

    def usage(self, bundle_id: UUID) -> tuple[int, int]:
        return self._usage

    def append_event(self, bundle: Any, metadata: Any) -> Any:
        return SimpleNamespace(id=metadata.id, event_digest=digest("event"))


class _TraceStore:
    def __init__(self, *, exists: bool = True, mismatch: str | None = None) -> None:
        self._exists = exists
        self._mismatch = mismatch
        self.payload = b""
        self.receipt = SimpleNamespace(
            digest=digest("stored"), as_dict=lambda: {"digest": digest("stored")}
        )

    def put(self, payload: bytes, **kwargs: object) -> Any:
        self.payload = payload
        return self.receipt

    def exists(self, value: str) -> bool:
        return self._exists

    def stat(self, value: str) -> Any:
        return object() if self._mismatch == "stat" else self.receipt

    def get(self, value: str) -> bytes:
        return b"wrong" if self._mismatch == "bytes" else self.payload


def _trace_bundle() -> tuple[DiagnosticTracePolicy, Any]:
    policy = DiagnosticTracePolicy(enabled=True, encryption_key_ref="secretref:key")
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    bundle = SimpleNamespace(
        id=uuid4(), policy=policy, state="open", expires_at=now + dt.timedelta(days=7)
    )
    return policy, bundle


@pytest.mark.parametrize(
    "store",
    [_TraceStore(exists=False), _TraceStore(mismatch="stat"), _TraceStore(mismatch="bytes")],
)
def test_trace_writer_rejects_missing_or_mismatched_durability(store: _TraceStore) -> None:
    policy, bundle = _trace_bundle()
    writer = DiagnosticTraceWriter(
        cast(Any, _TraceRepo()),
        cast(Any, store),
        AesGcmTraceCipher(lambda _: b"n" * 12),
        lambda _: b"k" * 32,
    )
    with pytest.raises(PolicyViolation):
        writer.write(
            bundle=cast(Any, bundle),
            policy=policy,
            event_type=TraceEventType.RUNTIME_STATE,
            visibility=TraceVisibility.RUNTIME_ONLY,
            payload={"ok": True},
            correlation={"id": "one"},
            occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )


def test_trace_writer_success_and_best_effort_failure_paths() -> None:
    policy, bundle = _trace_bundle()
    writer = DiagnosticTraceWriter(
        cast(Any, _TraceRepo()),
        cast(Any, _TraceStore()),
        AesGcmTraceCipher(lambda _: b"n" * 12),
        lambda _: b"k" * 32,
    )
    kwargs = {
        "bundle": cast(Any, bundle),
        "policy": policy,
        "event_type": TraceEventType.RUNTIME_STATE,
        "visibility": TraceVisibility.RUNTIME_ONLY,
        "payload": {"ok": True},
        "correlation": {"id": "one"},
        "occurred_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    }
    assert writer.write_best_effort(**kwargs).state == "recorded"
    assert writer.write_best_effort(**(kwargs | {"payload": object()})).state == "failed"


@pytest.mark.parametrize("case", ["missing-bundle", "closed", "oversize"])
def test_trace_writer_rejects_binding_state_and_payload_boundaries(case: str) -> None:
    policy, bundle = _trace_bundle()
    selected_bundle: Any = bundle
    payload: Any = {"ok": True}
    if case == "missing-bundle":
        selected_bundle = None
    elif case == "closed":
        bundle.state = "closed"
    else:
        policy = DiagnosticTracePolicy(
            enabled=True, encryption_key_ref="secretref:key", max_payload_bytes=1
        )
        bundle.policy = policy
        payload = {"too": "large"}
    writer = DiagnosticTraceWriter(
        cast(Any, _TraceRepo()),
        cast(Any, _TraceStore()),
        AesGcmTraceCipher(lambda _: b"n" * 12),
        lambda _: b"k" * 32,
    )
    with pytest.raises((PolicyViolation, ValidationFailed)):
        writer.write(
            bundle=selected_bundle,
            policy=policy,
            event_type=TraceEventType.RUNTIME_STATE,
            visibility=TraceVisibility.RUNTIME_ONLY,
            payload=payload,
            correlation={"id": "one"},
            occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )


class _PurgeRepo:
    def __init__(self, candidate: TracePurgeCandidate) -> None:
        self.candidate = candidate

    def expired_candidates(self, **kwargs: object) -> tuple[TracePurgeCandidate, ...]:
        return (self.candidate,)


def test_trace_retention_rejects_current_candidate() -> None:
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    candidate = TracePurgeCandidate(uuid4(), (), now + dt.timedelta(seconds=1))
    service = DiagnosticTraceRetentionService(cast(Any, _PurgeRepo(candidate)), cast(Any, object()))
    with pytest.raises(PolicyViolation, match="Current"):
        service.purge_expired(now=now, authorization_ref="auth")


def test_doctor_effect_request_fail_closed_and_valid_branches() -> None:
    project = uuid4()
    git = SimpleNamespace(state=SimpleNamespace(remote=None, remote_branch=None, branch="main"))
    plan = cast(Any, SimpleNamespace(git=git, migrations=None, routines=None))
    with pytest.raises(PolicyViolation):
        _effect_request(plan, project_id=project, step="git-fast-forward")
    git.state.remote = "origin"
    git.state.remote_branch = "main"
    resources = _effect_request(plan, project_id=project, step="git-fast-forward")[0]
    assert resources == (
        f"project:{project}:source",
        f"path:{project}:.git/refs/heads/main",
        "provider:git:origin:main",
    )
    assert tuple(item.resource.text for item in parse_requests(write=resources))
    with pytest.raises(PolicyViolation):
        _effect_request(plan, project_id=project, step="postgres-migration-upgrade")
    with pytest.raises(PolicyViolation):
        _effect_request(plan, project_id=project, step="routine-repair")
    plan.routines = SimpleNamespace(status=SimpleNamespace(migration_head="head"))
    assert _effect_request(plan, project_id=project, step="routine-repair")[0]


@pytest.mark.parametrize(
    ("remote", "remote_branch", "local_branch"),
    [
        ("origin secret", "main", "main"),
        ("origin", "../main", "main"),
        ("origin", "main", "../main"),
        ("origin\\escape", "main", "main"),
    ],
)
def test_doctor_git_resource_payload_rejects_noncanonical_values_before_enqueue(
    remote: str, remote_branch: str, local_branch: str
) -> None:
    git = SimpleNamespace(
        state=SimpleNamespace(remote=remote, remote_branch=remote_branch, branch=local_branch)
    )
    plan = cast(Any, SimpleNamespace(git=git, migrations=None, routines=None))
    with pytest.raises(ValidationFailed):
        _effect_request(plan, project_id=uuid4(), step="git-fast-forward")


def test_doctor_apply_step_git_and_routine_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = SimpleNamespace(as_dict=lambda: {"ok": True})
    monkeypatch.setattr(doctor_module, "apply_git_fast_forward", lambda *a, **k: result)
    context = SimpleNamespace(core_path=tmp_path)
    realm = SimpleNamespace(connection=object(), realm_id=uuid4())
    plan = cast(Any, SimpleNamespace(git=SimpleNamespace(plan_digest="plan"), routines=None))
    assert _apply_step(
        cast(Any, realm), cast(Any, context), repair_plan=plan, step="git-fast-forward"
    ) == {"ok": True}
    with pytest.raises(PolicyViolation):
        _apply_step(cast(Any, realm), cast(Any, context), repair_plan=plan, step="routine-repair")
    plan.routines = SimpleNamespace(plan_digest="routine")
    calls: list[str] = []

    def maintenance(name: str, *args: object, **kwargs: object) -> object:
        calls.append(name)
        return result

    monkeypatch.setattr(doctor_module, "legacy_database_maintenance", maintenance)
    assert _apply_step(
        cast(Any, realm), cast(Any, context), repair_plan=plan, step="routine-repair"
    ) == {"ok": True}
    assert calls == ["session-reset-role", "routine-repair", "session-configure"]


def _recovery_graph() -> tuple[Any, Any, Any, Any]:
    project, work, task, job_id, claim_id = (uuid4() for _ in range(5))
    plan = SimpleNamespace(
        project_id=project,
        work_item_id=work,
        task_plan_id=task,
        task_plan_digest=digest("task-plan"),
        checkpoint=SimpleNamespace(source_revision="source", plan_steps=("step",)),
        old_completion=SimpleNamespace(
            job_id=job_id,
            claim_id=claim_id,
            attempt_id=uuid4(),
            fencing_token=2,
            claim_digest=digest("claim"),
            effect_digest=digest("effect"),
            authorization_digest=digest("authorization"),
        ),
    )
    task_plan = SimpleNamespace(
        id=task,
        project_id=project,
        plan_digest=plan.task_plan_digest,
        source_revision="source",
        execution_order=("step",),
    )
    job = SimpleNamespace(
        id=job_id,
        project_id=project,
        work_item_id=work,
        plan_id=task,
        step_id="step",
        state=JobState.RECOVERY_REQUIRED,
    )
    claim = SimpleNamespace(
        id=claim_id,
        attempt_id=plan.old_completion.attempt_id,
        fencing_token=2,
        claim_digest=digest("claim"),
        effect_digest=digest("effect"),
        authorization_digest=digest("authorization"),
    )
    return plan, task_plan, job, claim


def _install_recovery_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: Any,
    task_plan: Any,
    job: Any,
    claims: tuple[Any, ...],
    receipt: Any = None,
    work_project: UUID | None = None,
) -> None:
    def repository(kind: str, *args: object, **kwargs: object) -> Any:
        if kind == "work_item":
            return SimpleNamespace(
                get=lambda _: SimpleNamespace(project_id=work_project or plan.project_id)
            )
        if kind == "task_plan":
            return SimpleNamespace(history=lambda _: (task_plan,))
        raise AssertionError(kind)

    ledger = SimpleNamespace(
        claims_for_job=lambda _: claims,
        receipt_for_claim=lambda _: receipt,
    )
    host = SimpleNamespace(jobs=SimpleNamespace(get=lambda _: job), ledger=ledger)
    monkeypatch.setattr(recovery_module, "legacy_repository", repository)
    monkeypatch.setattr(recovery_module, "ExecutionHost", lambda *a, **k: host)


@pytest.mark.parametrize(
    "mutation",
    [
        "work-project",
        "missing-plan",
        "plan-project",
        "plan-digest",
        "source",
        "steps",
        "job-project",
        "job-work",
        "job-plan",
        "job-step",
        "job-state",
        "missing-claim",
        "attempt",
        "fence",
        "claim-digest",
        "effect-digest",
        "authorization-digest",
        "has-receipt",
    ],
)
def test_recovery_reconciliation_validate_rejects_each_graph_drift(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    plan, task_plan, job, claim = _recovery_graph()
    work_project = plan.project_id
    claims: tuple[Any, ...] = (claim,)
    receipt: Any = None
    if mutation == "work-project":
        work_project = uuid4()
    elif mutation == "missing-plan":
        task_plan.id = uuid4()
    elif mutation == "plan-project":
        task_plan.project_id = uuid4()
    elif mutation == "plan-digest":
        task_plan.plan_digest = digest("wrong")
    elif mutation == "source":
        task_plan.source_revision = "other"
    elif mutation == "steps":
        task_plan.execution_order = ("other",)
    elif mutation == "job-project":
        job.project_id = uuid4()
    elif mutation == "job-work":
        job.work_item_id = uuid4()
    elif mutation == "job-plan":
        job.plan_id = uuid4()
    elif mutation == "job-step":
        job.step_id = "other"
    elif mutation == "job-state":
        job.state = JobState.COMPLETED
    elif mutation == "missing-claim":
        claims = ()
    elif mutation == "attempt":
        claim.attempt_id = uuid4()
    elif mutation == "fence":
        claim.fencing_token = 3
    elif mutation == "claim-digest":
        claim.claim_digest = digest("wrong")
    elif mutation == "effect-digest":
        claim.effect_digest = digest("wrong")
    elif mutation == "authorization-digest":
        claim.authorization_digest = digest("wrong")
    elif mutation == "has-receipt":
        receipt = object()
    _install_recovery_fakes(
        monkeypatch,
        plan=plan,
        task_plan=task_plan,
        job=job,
        claims=claims,
        receipt=receipt,
        work_project=work_project,
    )
    service = recovery_module.RecoveryReconciliationService(
        object(), cast(Any, SimpleNamespace(id=uuid4()))
    )
    with pytest.raises((PolicyViolation, NotFound)):
        service.validate(cast(Any, plan))


def test_recovery_reconciliation_validate_accepts_exact_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, task_plan, job, claim = _recovery_graph()
    _install_recovery_fakes(monkeypatch, plan=plan, task_plan=task_plan, job=job, claims=(claim,))
    service = recovery_module.RecoveryReconciliationService(
        object(), cast(Any, SimpleNamespace(id=uuid4()))
    )
    assert service.validate(cast(Any, plan)) is task_plan


class _RecoveryConnection:
    def transaction(self) -> Any:
        return nullcontext()


def _recovery_apply_plan(outcome: str = "completed") -> Any:
    old_job, old_claim, work, task = uuid4(), uuid4(), uuid4(), uuid4()
    resource = "db-object:recovery:unit"
    request = EffectRequest(
        action="recover", effects=(EffectKind.DATABASE_WRITE,), resources=(resource,)
    )
    return SimpleNamespace(
        project_id=uuid4(),
        work_item_id=work,
        task_plan_id=task,
        plan_digest=digest("recovery-plan"),
        effect_request=request,
        resource=resource,
        adapter_digest=digest("adapter"),
        evidence_digest=digest("evidence"),
        outcome=outcome,
        checkpoint=SimpleNamespace(
            checkpoint_digest=digest("checkpoint"),
            source_revision="source",
            journal_head_digest=digest("journal"),
        ),
        old_completion=SimpleNamespace(
            job_id=old_job,
            claim_id=old_claim,
            attempt_id=uuid4(),
            fencing_token=1,
            claim_digest=digest("claim"),
            effect_digest=digest("effect"),
            authorization_digest=digest("auth"),
            result_digest=digest("result"),
            adapter_evidence_digest=digest("adapter-evidence"),
        ),
    )


def _install_recovery_apply_runtime(
    monkeypatch: pytest.MonkeyPatch, plan: Any, *, case: str = "success"
) -> None:
    authorization = SimpleNamespace(
        id=uuid4(),
        plan_digest=plan.plan_digest,
        work_item_id=plan.work_item_id,
        plan_id=plan.task_plan_id,
        authorization_digest=digest("authorization"),
    )
    if case == "auth":
        authorization.plan_digest = digest("wrong")
    governance = SimpleNamespace(
        authorizations=SimpleNamespace(get=lambda _: authorization),
        require_authorized=lambda *a, **k: authorization,
        evaluate=lambda *a, **k: SimpleNamespace(denial_reason="authorization-required"),
        issue_authorization=lambda **k: authorization,
    )
    monkeypatch.setattr(recovery_module, "GovernanceService", lambda *a, **k: governance)
    monkeypatch.setattr(
        recovery_module.RecoveryReconciliationService, "validate", lambda self, value: object()
    )
    recovery_job = SimpleNamespace(id=uuid4())
    work = SimpleNamespace(job=recovery_job, attempt_id=uuid4())
    recovery_claim = SimpleNamespace(id=uuid4())
    old_claim = SimpleNamespace(id=plan.old_completion.claim_id)
    old_receipt = SimpleNamespace(id=uuid4(), result_digest=digest("result"))
    jobs = SimpleNamespace(
        enqueue=lambda _: (recovery_job, case != "created"),
        get=lambda _: SimpleNamespace(run_id=None, step_id="step"),
    )
    host = SimpleNamespace(
        jobs=jobs,
        ledger=SimpleNamespace(
            claims_for_job=lambda _: () if case == "oldclaims" else (old_claim,)
        ),
        acquire_work=lambda **k: None if case == "work" else work,
        claim_effect=lambda *a, **k: recovery_claim,
        record_failure=lambda *a, **k: old_receipt,
        record_success=lambda *a, **k: old_receipt,
        finalize_reconciled_failure=lambda *a, **k: SimpleNamespace(receipt=old_receipt),
        finalize_reconciled_completion=lambda *a, **k: SimpleNamespace(receipt=old_receipt),
        finish=lambda *a, **k: case != "finish",
    )
    monkeypatch.setattr(recovery_module, "ExecutionHost", lambda *a, **k: host)
    monkeypatch.setattr(
        recovery_module,
        "legacy_repository",
        lambda kind, *a, **k: SimpleNamespace(store_checkpoint=lambda *a, **k: uuid4()),
    )


@pytest.mark.parametrize("case", ["auth", "created", "work", "oldclaims", "finish"])
def test_recovery_apply_rejects_partial_runtime_graph(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    plan = _recovery_apply_plan()
    _install_recovery_apply_runtime(monkeypatch, plan, case=case)
    service = recovery_module.RecoveryReconciliationService(
        _RecoveryConnection(), cast(Any, SimpleNamespace(id=uuid4()))
    )
    with pytest.raises((PolicyViolation, AuthorizationRequired)):
        service.apply(cast(Any, plan), authorization_id=uuid4())


@pytest.mark.parametrize("outcome", ["completed", "failed-no-effect"])
def test_recovery_apply_accepts_both_terminal_outcomes_and_callbacks(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    plan = _recovery_apply_plan(outcome)
    _install_recovery_apply_runtime(monkeypatch, plan)
    calls: list[str] = []
    service = recovery_module.RecoveryReconciliationService(
        _RecoveryConnection(), cast(Any, SimpleNamespace(id=uuid4()))
    )
    result = service.apply(
        cast(Any, plan),
        authorization_id=uuid4(),
        before_old_finalization=lambda *a: calls.append("before"),
        after_old_finalization=lambda *a: calls.append("after"),
    )
    assert result.result_digest.startswith("sha256:")
    assert calls == ["before", "after"]


def test_recovery_issue_authorization_requires_authorization_only_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _recovery_apply_plan()
    governance = SimpleNamespace(evaluate=lambda *a, **k: SimpleNamespace(denial_reason="policy"))
    monkeypatch.setattr(
        recovery_module.RecoveryReconciliationService, "validate", lambda self, value: object()
    )
    monkeypatch.setattr(recovery_module, "GovernanceService", lambda *a, **k: governance)
    service = recovery_module.RecoveryReconciliationService(
        object(), cast(Any, SimpleNamespace(id=uuid4()))
    )
    with pytest.raises(PolicyViolation, match="policy/capability"):
        service.issue_authorization(cast(Any, plan), actor_id=uuid4())


def test_recovery_issue_authorization_accepts_exact_denial_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _recovery_apply_plan()
    authorization = object()
    governance = SimpleNamespace(
        evaluate=lambda *a, **k: SimpleNamespace(denial_reason="authorization-required"),
        issue_authorization=lambda **kwargs: authorization,
    )
    monkeypatch.setattr(
        recovery_module.RecoveryReconciliationService, "validate", lambda self, value: object()
    )
    monkeypatch.setattr(recovery_module, "GovernanceService", lambda *a, **k: governance)
    service = recovery_module.RecoveryReconciliationService(
        object(), cast(Any, SimpleNamespace(id=uuid4()))
    )
    assert service.issue_authorization(cast(Any, plan), actor_id=uuid4()) is authorization


@pytest.mark.parametrize(
    "mutation", ["job", "claims", "receipt-none", "receipt-id", "receipt-status", "failure"]
)
def test_failed_receipt_prepare_rejects_partial_terminal_graph(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    job_id, claim_id, receipt_id = uuid4(), uuid4(), uuid4()
    job = SimpleNamespace(
        id=job_id,
        state=JobState.RECOVERY_REQUIRED,
        project_id=uuid4(),
        work_item_id=uuid4(),
        plan_id=uuid4(),
    )
    claim = SimpleNamespace(
        id=claim_id,
        attempt_id=uuid4(),
        fencing_token=1,
        claim_digest=digest("claim"),
        effect_digest=digest("effect"),
        authorization_digest=digest("auth"),
    )
    receipt: Any = SimpleNamespace(
        id=receipt_id, status=ReceiptStatus.FAILED, failure_digest=digest("failure")
    )
    claims: tuple[Any, ...] = (claim,)
    if mutation == "job":
        job.state = JobState.COMPLETED
    elif mutation == "claims":
        claims = ()
    elif mutation == "receipt-none":
        receipt = None
    elif mutation == "receipt-id":
        receipt.id = uuid4()
    elif mutation == "receipt-status":
        receipt.status = ReceiptStatus.COMPLETED
    elif mutation == "failure":
        receipt.failure_digest = None
    host = SimpleNamespace(
        jobs=SimpleNamespace(get=lambda _: job),
        ledger=SimpleNamespace(
            claims_for_job=lambda _: claims, receipt_for_claim=lambda _: receipt
        ),
    )
    monkeypatch.setattr(recovery_module, "ExecutionHost", lambda *a, **k: host)
    service = recovery_module.FailedReceiptReconciliationService(
        object(), cast(Any, SimpleNamespace(id=uuid4()))
    )
    with pytest.raises((PolicyViolation, NotFound)):
        service.prepare(job_id=job_id, claim_id=claim_id, receipt_id=receipt_id)


def test_failed_receipt_prepare_accepts_exact_terminal_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, claim_id, receipt_id = uuid4(), uuid4(), uuid4()
    job = SimpleNamespace(
        id=job_id,
        state=JobState.RECOVERY_REQUIRED,
        project_id=uuid4(),
        work_item_id=uuid4(),
        plan_id=uuid4(),
    )
    claim = SimpleNamespace(
        id=claim_id,
        attempt_id=uuid4(),
        fencing_token=1,
        claim_digest=digest("claim"),
        effect_digest=digest("effect"),
        authorization_digest=digest("auth"),
    )
    receipt = SimpleNamespace(
        id=receipt_id, status=ReceiptStatus.FAILED, failure_digest=digest("failure")
    )
    host = SimpleNamespace(
        jobs=SimpleNamespace(get=lambda _: job),
        ledger=SimpleNamespace(
            claims_for_job=lambda _: (claim,), receipt_for_claim=lambda _: receipt
        ),
    )
    monkeypatch.setattr(recovery_module, "ExecutionHost", lambda *a, **k: host)
    service = recovery_module.FailedReceiptReconciliationService(
        object(), cast(Any, SimpleNamespace(id=uuid4()))
    )
    result = service.prepare(job_id=job_id, claim_id=claim_id, receipt_id=receipt_id)
    assert result.project_id == job.project_id


def _lifecycle_plan(adopt: bool = False) -> LifecycleTemplatePreparePlan:
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    return LifecycleTemplatePreparePlan(
        uuid4(),
        uuid4(),
        uuid4(),
        1,
        uuid4(),
        "source",
        digest("policy"),
        adopt,
        now,
        now + dt.timedelta(minutes=30),
    )


def test_lifecycle_plan_authority_body_adoption_partition() -> None:
    assert "adopt_existing" not in _lifecycle_plan().authority_body()
    assert _lifecycle_plan(True).authority_body()["adopt_existing"] is True


@pytest.mark.parametrize(
    "case",
    [
        "actor-kind",
        "actor-state",
        "work-project",
        "work-state",
        "verification-no-adopt",
        "verification-no-plan",
        "adopt-nonverification",
        "no-policy",
        "source-drift",
    ],
)
def test_lifecycle_prepare_rejects_authority_and_snapshot_drift(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    project, work_id, actor_id = uuid4(), uuid4(), uuid4()
    actor = SimpleNamespace(kind=ActorKind.HUMAN, status=LifecycleStatus.ACTIVE)
    work = SimpleNamespace(project_id=project, state=WorkState.ACTIVE, revision=1)
    current_plan: Any = SimpleNamespace(id=uuid4())
    policy: Any = SimpleNamespace(policy_digest=digest("policy"))
    source = "source"
    adopt = False
    if case == "actor-kind":
        actor.kind = ActorKind.AGENT
    elif case == "actor-state":
        actor.status = LifecycleStatus.SUSPENDED
    elif case == "work-project":
        work.project_id = uuid4()
    elif case == "work-state":
        work.state = WorkState.READY
    elif case == "verification-no-adopt":
        work.state = WorkState.VERIFICATION
    elif case == "verification-no-plan":
        work.state, adopt, current_plan = WorkState.VERIFICATION, True, None
    elif case == "adopt-nonverification":
        adopt = True
    elif case == "no-policy":
        policy = None
    elif case == "source-drift":
        source = "other"
    template_repo = SimpleNamespace(
        current_source_revision=lambda _: source,
        assert_legacy_adoption_admissible=lambda *a, **k: None,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "legacy_repository",
        lambda kind, *a, **k: (
            SimpleNamespace(get=lambda _: actor) if kind == "actor" else template_repo
        ),
    )
    graph = SimpleNamespace(
        items=SimpleNamespace(get=lambda _: work),
        plans=SimpleNamespace(current=lambda _: current_plan),
    )
    monkeypatch.setattr(lifecycle_module, "WorkGraphService", lambda *a, **k: graph)
    monkeypatch.setattr(
        lifecycle_module,
        "GovernanceService",
        lambda *a, **k: SimpleNamespace(policies=SimpleNamespace(current=lambda _: policy)),
    )
    service = LifecycleRuntimeTemplatePrepareService(object(), SimpleNamespace(id=uuid4()))
    with pytest.raises(PolicyViolation):
        service.prepare(
            project_id=project,
            work_item_id=work_id,
            actor_id=actor_id,
            source_revision="source",
            adopt_existing=adopt,
        )


def test_lifecycle_prepare_accepts_exact_active_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    project, work_id, actor_id = uuid4(), uuid4(), uuid4()
    actor = SimpleNamespace(kind=ActorKind.HUMAN, status=LifecycleStatus.ACTIVE)
    work = SimpleNamespace(project_id=project, state=WorkState.ACTIVE, revision=4)
    monkeypatch.setattr(
        lifecycle_module,
        "legacy_repository",
        lambda kind, *a, **k: (
            SimpleNamespace(get=lambda _: actor)
            if kind == "actor"
            else SimpleNamespace(current_source_revision=lambda _: "source")
        ),
    )
    monkeypatch.setattr(
        lifecycle_module,
        "WorkGraphService",
        lambda *a, **k: SimpleNamespace(items=SimpleNamespace(get=lambda _: work)),
    )
    monkeypatch.setattr(
        lifecycle_module,
        "GovernanceService",
        lambda *a, **k: SimpleNamespace(
            policies=SimpleNamespace(
                current=lambda _: SimpleNamespace(policy_digest=digest("policy"))
            )
        ),
    )
    service = LifecycleRuntimeTemplatePrepareService(object(), SimpleNamespace(id=uuid4()))
    assert (
        service.prepare(
            project_id=project, work_item_id=work_id, actor_id=actor_id, source_revision="source"
        ).work_revision
        == 4
    )


def test_lifecycle_worker_rejects_none_and_malformed_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    host = SimpleNamespace(acquire_work=lambda **kwargs: None)
    monkeypatch.setattr(lifecycle_module, "ExecutionHost", lambda *a, **k: host)
    realm = SimpleNamespace(id=uuid4())
    assert run_lifecycle_template_prepare_once(object(), realm) is None
    host.acquire_work = lambda **kwargs: SimpleNamespace(
        job=SimpleNamespace(
            payload={}, max_attempts=2, kind=None, work_item_id=None, plan_id=None, run_id=None
        )
    )
    with pytest.raises(PolicyViolation, match="contract drift"):
        run_lifecycle_template_prepare_once(object(), realm)


def test_lifecycle_worker_rejects_payload_plan_digest_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _lifecycle_plan()
    payload = {
        "schema": "zekam-lifecycle-template-prepare-job/v1",
        "target_work_item_id": str(plan.work_item_id),
        "target_work_revision": 1,
        "actor_id": str(plan.actor_id),
        "source_revision": plan.source_revision,
        "policy_digest": plan.policy_digest,
        "prepared_at": plan.prepared_at.isoformat(),
        "expires_at": plan.expires_at.isoformat(),
        "plan_digest": digest("wrong"),
        "authorization_id": str(uuid4()),
        "effect_digest": digest("effect"),
        "run_id": str(uuid4()),
    }
    job = SimpleNamespace(
        payload=payload,
        max_attempts=1,
        kind=JobKind.MUTATION,
        work_item_id=uuid4(),
        plan_id=uuid4(),
        run_id=uuid4(),
        project_id=plan.project_id,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "ExecutionHost",
        lambda *a, **k: SimpleNamespace(acquire_work=lambda **kwargs: SimpleNamespace(job=job)),
    )
    with pytest.raises(PolicyViolation, match="plan digest drift"):
        run_lifecycle_template_prepare_once(object(), SimpleNamespace(id=plan.realm_id))


def test_lifecycle_materializer_rejects_inventory_and_source_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _lifecycle_plan()
    binding = SimpleNamespace(model_id="model")
    monkeypatch.setattr(
        lifecycle_module,
        "load_provider_bindings",
        lambda: SimpleNamespace(for_modality=lambda _: binding),
    )
    monkeypatch.setattr(
        lifecycle_module,
        "load_inventory",
        lambda: SimpleNamespace(records=(), snapshot_digest=digest("inventory")),
    )
    with pytest.raises(PolicyViolation, match="inventory"):
        materialize_lifecycle_template(object(), SimpleNamespace(id=plan.realm_id), plan)
    record = SimpleNamespace(model_id="model", enabled=True)
    monkeypatch.setattr(
        lifecycle_module,
        "load_inventory",
        lambda: SimpleNamespace(records=(record,), snapshot_digest=digest("inventory")),
    )
    monkeypatch.setattr(lifecycle_module, "ProjectIntegrationService", lambda *a, **k: object())
    monkeypatch.setattr(
        lifecycle_module,
        "prepare_project_context",
        lambda *a, **k: SimpleNamespace(context=SimpleNamespace(source_revision="other")),
    )
    with pytest.raises(PolicyViolation, match="source drift"):
        materialize_lifecycle_template(object(), SimpleNamespace(id=plan.realm_id), plan)


def test_lifecycle_apply_rejects_digest_expiry_and_reprepared_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LifecycleRuntimeTemplatePrepareService(object(), SimpleNamespace(id=uuid4()))
    plan = _lifecycle_plan()
    with pytest.raises(PolicyViolation, match="exact plan digest"):
        service.apply(plan, supplied_plan_digest=digest("wrong"))
    with pytest.raises(PolicyViolation, match="suresi"):
        service.apply(plan, supplied_plan_digest=plan.plan_digest)
    future = dataclasses.replace(
        plan,
        prepared_at=dt.datetime.now(dt.UTC),
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30),
    )
    monkeypatch.setattr(
        LifecycleRuntimeTemplatePrepareService,
        "prepare",
        lambda self, **kwargs: SimpleNamespace(plan_digest=digest("drift")),
    )
    with pytest.raises(PolicyViolation, match="plan drift"):
        service.apply(future, supplied_plan_digest=future.plan_digest)


def test_lifecycle_bind_rejects_template_override_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _lifecycle_plan()
    claimed = SimpleNamespace(
        job=SimpleNamespace(
            work_item_id=uuid4(),
            plan_id=uuid4(),
            run_id=uuid4(),
            assignment_id=uuid4(),
        )
    )
    template = SimpleNamespace(
        project_id=uuid4(),
        source_revision=plan.source_revision,
        policy_digest=plan.policy_digest,
    )
    monkeypatch.setattr(lifecycle_module, "legacy_repository", lambda *a, **k: object())
    with pytest.raises(PolicyViolation, match="override binding"):
        lifecycle_module._bind_prepare_runtime(
            connection=object(),
            realm=SimpleNamespace(id=plan.realm_id),
            claimed=claimed,
            plan=plan,
            authorization=object(),
            result={},
            now=plan.prepared_at,
            template_override=template,
        )


def test_doctor_runtime_rejects_plan_state_and_missing_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = SimpleNamespace(
        plan_digest=digest("plan"),
        next_step="routine-repair",
        blocked_reasons=(),
        routines=None,
        git=None,
        migrations=None,
    )
    realm = SimpleNamespace(connection=object(), realm=object(), realm_id=uuid4())
    context = SimpleNamespace(core_path=tmp_path)
    for plan, supplied in (
        (base, digest("wrong")),
        (SimpleNamespace(**(vars(base) | {"next_step": None})), base.plan_digest),
        (SimpleNamespace(**(vars(base) | {"blocked_reasons": ("blocked",)})), base.plan_digest),
    ):
        with pytest.raises(PolicyViolation):
            apply_doctor_repair_with_runtime(
                cast(Any, realm),
                cast(Any, context),
                repair_plan=cast(Any, plan),
                plan_digest=supplied,
                actor_id=uuid4(),
                project_id=uuid4(),
            )
    monkeypatch.setattr(doctor_module, "_assert_actor_and_project", lambda *a, **k: None)
    monkeypatch.setattr(
        doctor_module,
        "GovernanceService",
        lambda *a, **k: SimpleNamespace(policies=SimpleNamespace(current=lambda _: None)),
    )
    with pytest.raises(PolicyViolation, match="current policy"):
        apply_doctor_repair_with_runtime(
            cast(Any, realm),
            cast(Any, context),
            repair_plan=cast(Any, base),
            plan_digest=base.plan_digest,
            actor_id=uuid4(),
            project_id=uuid4(),
        )


@pytest.mark.parametrize("case", ["actor-kind", "actor-state", "project-state", "source"])
def test_doctor_actor_project_authority_rejects_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    actor = SimpleNamespace(kind=ActorKind.HUMAN, status=LifecycleStatus.ACTIVE)
    project = SimpleNamespace(status=LifecycleStatus.ACTIVE)
    source = tmp_path
    if case == "actor-kind":
        actor.kind = ActorKind.AGENT
    elif case == "actor-state":
        actor.status = LifecycleStatus.SUSPENDED
    elif case == "project-state":
        project.status = LifecycleStatus.SUSPENDED
    elif case == "source":
        source = tmp_path / "other"
    monkeypatch.setattr(
        doctor_module, "legacy_repository", lambda *a, **k: SimpleNamespace(get=lambda _: actor)
    )
    monkeypatch.setattr(
        doctor_module,
        "ProjectIntegrationService",
        lambda *a, **k: SimpleNamespace(
            projects=SimpleNamespace(get=lambda _: project),
            resolve_source_root=lambda _: source,
        ),
    )
    realm = SimpleNamespace(connection=object(), realm=object(), realm_id=uuid4())
    with pytest.raises(PolicyViolation):
        _assert_actor_and_project(
            cast(Any, realm),
            cast(Any, SimpleNamespace(core_path=tmp_path)),
            actor_id=uuid4(),
            project_id=uuid4(),
        )


def test_doctor_actor_project_authority_accepts_exact_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    actor = SimpleNamespace(kind=ActorKind.HUMAN, status=LifecycleStatus.ACTIVE)
    project = SimpleNamespace(status=LifecycleStatus.ACTIVE)
    monkeypatch.setattr(
        doctor_module, "legacy_repository", lambda *a, **k: SimpleNamespace(get=lambda _: actor)
    )
    monkeypatch.setattr(
        doctor_module,
        "ProjectIntegrationService",
        lambda *a, **k: SimpleNamespace(
            projects=SimpleNamespace(get=lambda _: project), resolve_source_root=lambda _: tmp_path
        ),
    )
    realm = SimpleNamespace(connection=object(), realm=object(), realm_id=uuid4())
    _assert_actor_and_project(
        cast(Any, realm),
        cast(Any, SimpleNamespace(core_path=tmp_path)),
        actor_id=uuid4(),
        project_id=uuid4(),
    )


def _install_doctor_runtime_prefix(
    monkeypatch: pytest.MonkeyPatch, *, created: bool, claimed: Any
) -> Any:
    authorization = SimpleNamespace(id=uuid4(), authorization_digest=digest("authorization"))
    governance = SimpleNamespace(
        policies=SimpleNamespace(current=lambda _: SimpleNamespace(policy_digest=digest("policy"))),
        issue_authorization=lambda **kwargs: authorization,
        evaluate=lambda *a, **k: SimpleNamespace(
            allowed=True, gates=SimpleNamespace(decisions=(), first_denial=None)
        ),
        revoke_authorization=lambda *a, **k: None,
    )
    work = SimpleNamespace(id=uuid4())
    task_plan = SimpleNamespace(id=uuid4(), execution_order=("step",))
    graph = SimpleNamespace(
        create_item=lambda **kwargs: work,
        set_intent=lambda *a, **k: None,
        transition=lambda *a, **k: work,
        create_plan=lambda *a, **k: task_plan,
    )
    job = SimpleNamespace(id=uuid4())
    host = SimpleNamespace(
        jobs=SimpleNamespace(
            enqueue=lambda _: (job, created),
            mark_recovery_required=lambda *a, **k: None,
        ),
        acquire_work=lambda **kwargs: claimed,
    )
    monkeypatch.setattr(doctor_module, "_assert_actor_and_project", lambda *a, **k: None)
    monkeypatch.setattr(doctor_module, "GovernanceService", lambda *a, **k: governance)
    monkeypatch.setattr(doctor_module, "WorkGraphService", lambda *a, **k: graph)
    monkeypatch.setattr(doctor_module, "ExecutionHost", lambda *a, **k: host)
    return host


@pytest.mark.parametrize("step", ["routine-repair", "git-fast-forward"])
def test_doctor_runtime_rejects_duplicate_job_for_both_step_shapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, step: str
) -> None:
    _install_doctor_runtime_prefix(monkeypatch, created=False, claimed=None)
    git = SimpleNamespace(
        state=SimpleNamespace(remote="origin", remote_branch="main", branch="main")
    )
    routines = SimpleNamespace(status=SimpleNamespace(migration_head="head"))
    plan = SimpleNamespace(
        plan_digest=digest("plan"),
        next_step=step,
        blocked_reasons=(),
        routines=routines,
        git=git,
        migrations=None,
    )
    realm = SimpleNamespace(connection=_RecoveryConnection(), realm=object(), realm_id=uuid4())
    with pytest.raises(PolicyViolation, match="replay"):
        apply_doctor_repair_with_runtime(
            cast(Any, realm),
            cast(Any, SimpleNamespace(core_path=tmp_path)),
            repair_plan=cast(Any, plan),
            plan_digest=plan.plan_digest,
            actor_id=uuid4(),
            project_id=uuid4(),
        )


@pytest.mark.parametrize("step", ["routine-repair", "git-fast-forward"])
def test_doctor_runtime_rejects_missing_selected_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, step: str
) -> None:
    _install_doctor_runtime_prefix(monkeypatch, created=True, claimed=None)
    git = SimpleNamespace(
        state=SimpleNamespace(remote="origin", remote_branch="main", branch="main")
    )
    plan = SimpleNamespace(
        plan_digest=digest("plan"),
        next_step=step,
        blocked_reasons=(),
        routines=SimpleNamespace(status=SimpleNamespace(migration_head="head")),
        git=git,
        migrations=None,
    )
    realm = SimpleNamespace(connection=_RecoveryConnection(), realm=object(), realm_id=uuid4())
    with pytest.raises(PolicyViolation, match="claim"):
        apply_doctor_repair_with_runtime(
            cast(Any, realm),
            cast(Any, SimpleNamespace(core_path=tmp_path)),
            repair_plan=cast(Any, plan),
            plan_digest=plan.plan_digest,
            actor_id=uuid4(),
            project_id=uuid4(),
        )


def test_doctor_runtime_propagates_enqueue_failure_before_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _install_doctor_runtime_prefix(monkeypatch, created=True, claimed=None)

    def fail_enqueue(_: object) -> NoReturn:
        raise RuntimeError("enqueue unavailable")

    host.jobs.enqueue = fail_enqueue
    applied = False

    def apply_step(*args: object, **kwargs: object) -> object:
        nonlocal applied
        applied = True
        return object()

    monkeypatch.setattr(doctor_module, "_apply_step", apply_step)
    plan = SimpleNamespace(
        plan_digest=digest("plan"),
        next_step="git-fast-forward",
        blocked_reasons=(),
        routines=None,
        git=SimpleNamespace(
            state=SimpleNamespace(remote="origin", remote_branch="main", branch="main")
        ),
        migrations=None,
    )
    realm = SimpleNamespace(connection=_RecoveryConnection(), realm=object(), realm_id=uuid4())
    with pytest.raises(RuntimeError, match="enqueue unavailable"):
        apply_doctor_repair_with_runtime(
            cast(Any, realm),
            cast(Any, SimpleNamespace(core_path=tmp_path)),
            repair_plan=cast(Any, plan),
            plan_digest=plan.plan_digest,
            actor_id=uuid4(),
            project_id=uuid4(),
        )
    assert not applied


@pytest.mark.parametrize("case", ["stale", "applied", "recorded", "success"])
def test_doctor_migration_apply_exact_before_after_matrix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    target = SimpleNamespace(version=2, name="two", checksum="sum", has_down=True, label="2-two")
    status = SimpleNamespace(head=1, applied=(), pending=(target,), drift=())
    migration = SimpleNamespace(next_migration=target, status=status)
    plan = cast(Any, SimpleNamespace(migrations=migration))
    before = status
    applied: list[Any] = [target]
    after = SimpleNamespace(head=2, applied=(target,), pending=(), drift=())
    if case == "stale":
        before = SimpleNamespace(head=0, applied=(), pending=(target,), drift=())
    elif case == "applied":
        applied = []
    elif case == "recorded":
        after = SimpleNamespace(head=2, applied=(), pending=(), drift=())

    def maintenance(name: str, *args: object, **kwargs: object) -> Any:
        return {
            "migration-status": before if not hasattr(maintenance, "seen") else after,
            "migration-upgrade": applied,
            "session-reset-role": None,
            "session-configure": None,
        }[name]

    # The second status call must expose the post-apply snapshot.
    count = {"status": 0}

    def sequenced(name: str, *args: object, **kwargs: object) -> Any:
        if name == "migration-status":
            count["status"] += 1
            return before if count["status"] == 1 else after
        return maintenance(name, *args, **kwargs)

    monkeypatch.setattr(doctor_module, "legacy_database_maintenance", sequenced)
    realm = SimpleNamespace(connection=object(), realm_id=uuid4())
    context = SimpleNamespace(core_path=tmp_path)
    if case == "success":
        assert (
            _apply_step(
                cast(Any, realm),
                cast(Any, context),
                repair_plan=plan,
                step="postgres-migration-upgrade",
            )["verified"]
            is True
        )
    else:
        with pytest.raises(PolicyViolation):
            _apply_step(
                cast(Any, realm),
                cast(Any, context),
                repair_plan=plan,
                step="postgres-migration-upgrade",
            )
