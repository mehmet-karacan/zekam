from __future__ import annotations

import datetime as dt
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest

import zekam.application.legacy_repository_provider as legacy
from zekam.application.agent_residency import AgentResidencyManager
from zekam.application.control_plane_completion import (
    ControlPlaneCompletionRequest,
    ControlPlaneCompletionResult,
    ControlPlaneCompletionService,
    control_plane_completion_resource,
)
from zekam.application.lifecycle_runtime_template import template_source_revision
from zekam.application.memory_control import (
    MemoryControlOperation,
    MemoryControlPlan,
    MemoryControlService,
)
from zekam.application.memory_hooks import memory_hook_bundle
from zekam.application.model_report import ModelHealthReport, ModelHealthSummary
from zekam.application.realm_context import attach_realm, bootstrap_realm, find_realm_id, load_realm
from zekam.domain.canonical import digest
from zekam.domain.errors import (
    AuthorizationRequired,
    ConfigurationError,
    NotFound,
    PolicyViolation,
    ValidationFailed,
)
from zekam.domain.realm import LifecycleStatus
from zekam.domain.work import EvidenceRef

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 20))
DIGEST = digest("evidence")


class _RowsConnection:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, object]] = []

    def cursor(self) -> _RowsConnection:
        return self

    def __enter__(self) -> _RowsConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, params: object = None) -> None:
        self.executed.append((statement, params))

    def fetchone(self) -> object:
        return self.rows.pop(0)


def test_residency_manager_exact_realm_and_all_transitions() -> None:
    store = Mock()
    store.register_loaded.return_value = (IDS[2], True)
    store.reload.return_value = "reload-result"
    store.get.return_value = {"state": "loaded"}
    manager = AgentResidencyManager(IDS[0], cast(Any, store))
    snapshot = SimpleNamespace(realm_id=IDS[0])
    assert manager.register(cast(Any, snapshot), runtime_session_ref="runtime:one") == (
        IDS[2],
        True,
    )
    snapshot.realm_id = IDS[1]
    with pytest.raises(ValueError, match="realm scope"):
        manager.register(cast(Any, snapshot), runtime_session_ref="runtime:one")

    request = SimpleNamespace(realm_id=IDS[0])
    assert cast(Any, manager.reload(cast(Any, request))) == "reload-result"
    request.realm_id = IDS[1]
    with pytest.raises(ValueError, match="realm scope"):
        manager.reload(cast(Any, request))

    assert manager.evict(IDS[2], occurred_at=NOW)
    assert manager.mark_idle(IDS[2], occurred_at=NOW)
    assert manager.begin_close(IDS[2], occurred_at=NOW)
    assert manager.mark_dead(IDS[2], occurred_at=NOW, reason="process-exit")
    assert manager.status(IDS[2]) == {"state": "loaded"}


def test_template_source_revision_accepts_only_exact_dirty_git_identity() -> None:
    revision = "a" * 40
    state = "b" * 64
    assert template_source_revision(f"git:{revision};state:sha256:{state}") == revision
    for candidate in (
        f"svn:{revision};state:sha256:{state}",
        f"git:{revision[:-1]};state:sha256:{state}",
        f"git:{revision[:-1]}z;state:sha256:{state}",
        f"git:{revision};state:sha256:{state[:-1]}",
        f"git:{revision};state:sha256:{state[:-1]}z",
        "git:clean",
    ):
        assert template_source_revision(candidate) == candidate


def test_legacy_repository_provider_is_fail_closed_and_class_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy, "_PROVIDER", None)
    with pytest.raises(ConfigurationError, match="composition root"):
        legacy.legacy_repository("job", object(), IDS[0])
    with pytest.raises(ConfigurationError, match="composition root"):
        legacy.legacy_database_maintenance("verify", object())

    class First:
        def build(self, *args: object, **kwargs: object) -> object:
            return (args, kwargs)

        def maintain(self, *args: object, **kwargs: object) -> object:
            return (args, kwargs)

    class Second(First):
        pass

    legacy.install_legacy_repository_provider(cast(Any, First()))
    legacy.install_legacy_repository_provider(cast(Any, First()))
    assert legacy.legacy_repository("job", "connection", IDS[0], "extra", flag=True)
    assert legacy.legacy_database_maintenance("verify", "connection", flag=True)
    with pytest.raises(ConfigurationError, match="degistirilemez"):
        legacy.install_legacy_repository_provider(cast(Any, Second()))


def test_realm_queries_and_attachment_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    assert find_realm_id(_RowsConnection([None]), "default") is None
    assert find_realm_id(_RowsConnection([(None,)]), "default") is None
    assert find_realm_id(_RowsConnection([(IDS[0],)]), "default") == IDS[0]

    with pytest.raises(NotFound, match="bulunamadi"):
        load_realm(_RowsConnection([None]), IDS[0])
    row = (IDS[0], "default", "Default", LifecycleStatus.ACTIVE.value, 3, NOW)
    assert load_realm(_RowsConnection([row]), IDS[0]).revision == 3

    operations = Mock()
    connection = object()
    monkeypatch.setattr("zekam.application.realm_context.find_realm_id", lambda *_a: None)
    with pytest.raises(NotFound, match="yerel"):
        attach_realm(connection, operations)
    monkeypatch.setattr("zekam.application.realm_context.ensure_realm_id", lambda *_a: IDS[0])
    monkeypatch.setattr(
        "zekam.application.realm_context.load_realm",
        lambda *_a: SimpleNamespace(id=IDS[0]),
    )
    context = bootstrap_realm(connection, operations, now=NOW)
    assert context.realm_id == IDS[0]
    operations.configure.assert_called()
    operations.set_realm.assert_called_with(connection, IDS[0])


def _completion_request() -> ControlPlaneCompletionRequest:
    return ControlPlaneCompletionRequest(
        project_id=IDS[0],
        work_item_id=IDS[1],
        task_plan_id=IDS[2],
        job_id=IDS[3],
        attempt_id=IDS[4],
        checkpoint_id=IDS[5],
        source_authorization_id=IDS[6],
        source_authorization_digest=digest("authorization"),
        source_claim_id=IDS[7],
        source_claim_digest=digest("claim"),
        source_effect_receipt_id=IDS[8],
        source_operation="database-write",
        source_consumed_by="worker/v1",
        source_effect_digest=digest("effect"),
        source_adapter_digest=digest("adapter"),
        source_adapter_evidence_digest=digest("adapter-evidence"),
        source_resources=("resource:a",),
        source_effects=("database-write",),
        source_data_classifications=("internal",),
        evidence=(EvidenceRef("runtime-receipt", str(IDS[8]), DIGEST),),
    )


def _completion_result(request: ControlPlaneCompletionRequest) -> ControlPlaneCompletionResult:
    return ControlPlaneCompletionResult(
        work_item_id=request.work_item_id,
        work_revision=2,
        work_record_digest=digest("work"),
        authorization_id=IDS[9],
        claim_id=IDS[10],
        effect_receipt_id=IDS[11],
        admission_id=IDS[12],
        checkpoint_id=request.checkpoint_id,
        result_digest=digest("result"),
        request_digest=request.request_digest,
        evidence_digest=request.evidence_digest,
        source_authorization_id=request.source_authorization_id,
        source_claim_id=request.source_claim_id,
        source_effect_receipt_id=request.source_effect_receipt_id,
    )


def test_control_plane_completion_request_and_result_fail_closed() -> None:
    request = _completion_request()
    assert request.body()["grants_authority"] is False
    assert control_plane_completion_resource(IDS[0], IDS[1]).endswith(":control-plane-completion")
    for changes, message in (
        ({"source_operation": " "}, "operation/consumer"),
        ({"source_consumed_by": ""}, "operation/consumer"),
        ({"source_resources": ()}, "resource"),
        ({"source_resources": ("b", "a")}, "resource"),
        ({"source_effects": ("x", "x")}, "effect"),
        ({"source_data_classifications": ()}, "classification"),
        ({"evidence": ()}, "acceptance evidence"),
        ({"evidence": (EvidenceRef("artifact", "other", DIGEST),)}, "exact source receipt"),
    ):
        with pytest.raises(PolicyViolation, match=message):
            replace(request, **cast(Any, changes))

    result = _completion_result(request)
    store = SimpleNamespace(complete=Mock(return_value=result), readback=Mock(return_value=result))
    service = ControlPlaneCompletionService(cast(Any, store))
    assert service.complete(request) == result
    assert service.readback(request).as_dict()["grants_authority"] is False
    with pytest.raises(PolicyViolation, match="identity drift"):
        service._verify(request, replace(result, work_item_id=IDS[18]))
    with pytest.raises(PolicyViolation, match="identity drift"):
        service._verify(request, replace(result, grants_authority=True))
    binding_changes: tuple[dict[str, Any], ...] = (
        {"request_digest": digest("wrong")},
        {"evidence_digest": digest("wrong")},
        {"source_authorization_id": IDS[18]},
        {"source_claim_id": IDS[18]},
        {"source_effect_receipt_id": IDS[18]},
    )
    for changes in binding_changes:
        with pytest.raises(PolicyViolation, match="binding drift"):
            service._verify(request, replace(result, **cast(Any, changes)))


class _MemoryRepository:
    realm_id = IDS[0]

    def __init__(self, state: str, current_digest: str = DIGEST) -> None:
        self.state = state
        self.current_digest = current_digest
        self.connection = SimpleNamespace(transaction=nullcontext)
        self.applied: list[MemoryControlPlan] = []

    def read_control_state(
        self, _operation: MemoryControlOperation, _subject_id: str
    ) -> tuple[str, str]:
        return self.state, self.current_digest

    def apply_control(self, plan: MemoryControlPlan, **_kwargs: object) -> bool:
        self.applied.append(plan)
        return True


def _memory_plan(
    operation: MemoryControlOperation, state: str, target: str
) -> tuple[MemoryControlService, MemoryControlPlan, _MemoryRepository, Mock]:
    repository = _MemoryRepository(state)
    authorizations = Mock()
    service = MemoryControlService(cast(Any, repository), cast(Any, authorizations))
    plan = service.prepare(
        operation=operation,
        subject_id="subject-1",
        evidence_ref="artifact:one",
        evidence_digest=DIGEST,
        target_state=target,
    )
    return service, plan, repository, authorizations


def test_memory_control_prepare_all_operations_and_portable_validation() -> None:
    cases = (
        (MemoryControlOperation.GAP_REPAIR, "open", "resolved"),
        (MemoryControlOperation.GAP_REPAIR, "recovery-required", "resolved"),
        (MemoryControlOperation.CANDIDATE_PROMOTE, "reviewed", "promoted"),
        (MemoryControlOperation.CLOSE_FINALIZE, "pending", "completed"),
        (MemoryControlOperation.CLOSE_FINALIZE, "processing", "failed"),
        (MemoryControlOperation.CLOSE_FINALIZE, "pending", "recovery-required"),
    )
    for operation, state, target in cases:
        _, plan, _, _ = _memory_plan(operation, state, target)
        plan.assert_integrity()
        assert plan.grants_authority is False

    for operation, state, target in (
        (MemoryControlOperation.GAP_REPAIR, "closed", "resolved"),
        (MemoryControlOperation.CANDIDATE_PROMOTE, "open", "promoted"),
        (MemoryControlOperation.CLOSE_FINALIZE, "pending", "unknown"),
    ):
        service = MemoryControlService(cast(Any, _MemoryRepository(state)), cast(Any, Mock()))
        with pytest.raises(PolicyViolation, match="transition"):
            service.prepare(
                operation=operation,
                subject_id="subject-1",
                evidence_ref="artifact:one",
                evidence_digest=DIGEST,
                target_state=target,
            )

    _, plan, _, _ = _memory_plan(MemoryControlOperation.GAP_REPAIR, "open", "resolved")
    for field, value, error in (
        ("subject_id", "", ValidationFailed),
        ("resource", " spaced ", ValidationFailed),
        ("current_state", "x" * 513, ValidationFailed),
        ("evidence_ref", "/absolute", PolicyViolation),
        ("target_state", "a/../b", PolicyViolation),
        ("resource", "a\\b", PolicyViolation),
    ):
        values = plan.body()
        values.pop("schema")
        values.pop("grants_authority")
        values.pop("effect_digest")
        values[field] = value
        with pytest.raises(error):
            MemoryControlPlan.create(**cast(Any, values))
    with pytest.raises(PolicyViolation, match="digest mismatch"):
        replace(plan, plan_digest=digest("wrong")).assert_integrity()


def test_memory_control_apply_rejects_drift_authority_and_consumption() -> None:
    service, plan, repository, authorizations = _memory_plan(
        MemoryControlOperation.GAP_REPAIR, "open", "resolved"
    )
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        service.apply(plan, authorization_id=IDS[2], now=NOW.replace(tzinfo=None))
    repository.state = "closed"
    with pytest.raises(PolicyViolation, match="state drift"):
        service.apply(plan, authorization_id=IDS[2], now=NOW)
    repository.state = "open"

    scope = SimpleNamespace(
        covers_effect=lambda value: value == "database-write",
        covers_resource=lambda value: value == plan.resource,
    )
    authorization = SimpleNamespace(
        realm_id=plan.realm_id,
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=scope,
        rejection_reason=lambda _now: None,
    )
    authorizations.get.return_value = authorization
    for field, value in (
        ("realm_id", IDS[1]),
        ("plan_digest", digest("wrong")),
        ("effect_digest", digest("wrong")),
    ):
        original = getattr(authorization, field)
        setattr(authorization, field, value)
        with pytest.raises(AuthorizationRequired, match="exact authorization"):
            service.apply(plan, authorization_id=IDS[2], now=NOW)
        setattr(authorization, field, original)

    authorizations.consume.return_value = SimpleNamespace(consumed=False, reason="replayed")
    with pytest.raises(AuthorizationRequired, match="replayed"):
        service.apply(plan, authorization_id=IDS[2], now=NOW)
    authorizations.consume.return_value = SimpleNamespace(consumed=True)
    receipt = service.apply(plan, authorization_id=IDS[2], now=NOW)
    assert receipt.created and repository.applied == [plan]


def test_memory_hook_bundle_is_deterministic_and_rejects_non_mapping() -> None:
    first = memory_hook_bundle(IDS[0])
    second = memory_hook_bundle(IDS[0])
    assert first.bundle_digest == second.bundle_digest
    assert len(first.specs) == len(first.runtimes) == len(first.adapters) > 0
    with pytest.raises(TypeError, match="mapping"):
        first.adapters[0].invoke([])


def test_model_health_report_optional_sections_and_digests() -> None:
    rows = (
        ModelHealthSummary(
            "model-a", "a", "chat", "healthy", "passed", True, False, True, 2, "ok", False
        ),
        ModelHealthSummary(
            "model-b",
            "b",
            "embedding",
            "quarantined",
            "failed",
            False,
            True,
            False,
            0,
            None,
            True,
        ),
    )
    report = ModelHealthReport("schema/v1", NOW.date(), rows, 2, 1)
    assert report.counts == {"healthy": 1, "quarantined": 1}
    assert report.profile_gap == 1
    assert report.quarantined == (rows[1],)
    assert report.stale == (rows[1],)
    assert report.missing_technical_profile == (rows[1],)
    markdown = report.as_markdown()
    assert "Teknik profili olmayan" in markdown and "Karantinadaki" in markdown
    assert report.markdown_digest == digest(markdown)
    assert report.json_digest == digest(report.as_json())
    assert report.summary()["quarantined"] == ["model-b"]

    empty = ModelHealthReport("schema/v1", NOW.date(), (), 0, 0)
    assert "Teknik profili olmayan" not in empty.as_markdown()
    assert "Karantinadaki" not in empty.as_markdown()
