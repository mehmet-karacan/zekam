from __future__ import annotations

import datetime as dt
import json
import sys
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from zekam.application import oracle_metadata_index as oracle
from zekam.application import project_integration as integration
from zekam.application.high_risk_autofill_guard import (
    AutofillEffectPlan,
    AutofillEffectReceipt,
    AutofillOperation,
    FieldEvidence,
    FieldEvidenceStatus,
    FormFieldSpec,
    HighRiskAutofillGuard,
    build_autofill_preview,
    prepare_submit_plan,
)
from zekam.application.loop_orchestrator import (
    DurableLoopOrchestrator,
    LoopAttemptAdmissionControl,
    measured_loop_effect_scope_digest,
)
from zekam.application.mutation_admission import (
    ActiveRuntimeContinuityIdentity,
    CliMutationAdmission,
    CliMutationAdmissionRegistry,
    CliMutationEvidence,
    CliMutationRule,
    CliMutationTargetHints,
    MutationAdmissionExemption,
    _advance_gate_a_source_capability,
    assert_cli_mutation_admission,
    assert_full_continuity_backend,
    assert_local_effect_admission,
)
from zekam.application.project_integration import (
    IntegrationReport,
    ProjectIntegrationService,
    _next_action,
    locator_digest_for,
)
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import (
    AuthorizationRequired,
    ConfigurationError,
    NotFound,
    PolicyViolation,
    ValidationFailed,
    ZekamError,
)
from zekam.domain.loop_policy import LoopDeltaKind, LoopEffectClass, LoopPolicy, LoopTerminalState
from zekam.domain.optimization import MetricDirection, MetricRole, MetricSpec, OptimizationObjective
from zekam.domain.project import IntegrationStage, Project, SourceBinding, SourceRevision
from zekam.domain.realm import Realm
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.session_continuity import DataClassification

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(int=value) for value in range(1, 30))


def _evidence(
    status: FieldEvidenceStatus = FieldEvidenceStatus.VERIFIED,
    *,
    classification: DataClassification = DataClassification.PII,
    expires_at: dt.datetime | None = None,
) -> FieldEvidence:
    value = "Ada Lovelace" if status is FieldEvidenceStatus.VERIFIED else None
    return FieldEvidence(
        "full_name",
        value,
        value,
        "memory:profile-name",
        digest("profile-name"),
        "revision-3",
        NOW,
        0.99,
        classification,
        ("non_empty",),
        status,
        expires_at,
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: FormFieldSpec("Bad"),
        lambda: FormFieldSpec("captcha", manual_only=False),
        lambda: replace(_evidence(), extracted_at=NOW.replace(tzinfo=None)),
        lambda: replace(_evidence(), confidence=1.1),
        lambda: replace(_evidence(), validation_rules=("z", "a")),
        lambda: replace(_evidence(), source_digest=None),
        lambda: replace(_evidence(), source_ref="https://bad.example"),
        lambda: replace(_evidence(), normalized_value=""),
        lambda: replace(_evidence(FieldEvidenceStatus.UNKNOWN), normalized_value="x"),
    ],
)
def test_autofill_evidence_and_schema_reject_malformed_inputs(factory: Any) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        factory()


def test_autofill_preview_masks_values_and_fails_closed_for_missing_expired_manual() -> None:
    preview = build_autofill_preview(
        form_ref="form:visa",
        fields=(
            FormFieldSpec("full_name"),
            FormFieldSpec("nickname", required=False),
            FormFieldSpec("signature", manual_only=True, required=False),
        ),
        evidence=(_evidence(expires_at=NOW + dt.timedelta(minutes=1)),),
        now=NOW,
    )
    assert preview.submit_eligible
    assert preview._payload() == {"full_name": "Ada Lovelace"}
    assert "Ada Lovelace" not in canonical_json(preview.body())
    expired = build_autofill_preview(
        form_ref="form:visa",
        fields=(FormFieldSpec("full_name"),),
        evidence=(_evidence(expires_at=NOW + dt.timedelta(seconds=1)),),
        now=NOW + dt.timedelta(seconds=1),
    )
    assert not expired.submit_eligible
    assert expired.fields[0].status is FieldEvidenceStatus.EXPIRED
    assert expired._payload() == {}
    with pytest.raises(ValidationFailed, match="tekil"):
        build_autofill_preview(
            form_ref="form:x",
            fields=(FormFieldSpec("full_name"), FormFieldSpec("full_name")),
            evidence=(),
            now=NOW,
        )
    with pytest.raises(ValidationFailed, match="schema disinda"):
        build_autofill_preview(
            form_ref="form:x",
            fields=(FormFieldSpec("nickname"),),
            evidence=(_evidence(),),
            now=NOW,
        )


def test_autofill_plan_receipt_and_guard_preserve_exact_one_shot_integrity() -> None:
    preview = build_autofill_preview(
        form_ref="form:visa",
        fields=(FormFieldSpec("full_name"),),
        evidence=(_evidence(),),
        now=NOW,
    )
    fill = AutofillEffectPlan.fill(IDS[0], preview)
    receipt = AutofillEffectReceipt(
        AutofillOperation.FILL,
        fill.plan_digest,
        preview.preview_digest,
        IDS[1],
        "completed",
        digest("fill-result"),
        NOW,
    )
    submit = prepare_submit_plan(
        IDS[0], preview, fill_receipt_ref="receipt:fill", fill_receipt=receipt
    )
    assert fill.effect_kind == "process-run"
    assert submit.effect_kind == "network-call"
    assert receipt.receipt_digest == digest(receipt.body())
    forged_plan = replace(fill, plan_digest=digest("wrong"))
    with pytest.raises(PolicyViolation):
        forged_plan.assert_integrity()
    for changes in (
        {"effect_digest": digest("wrong")},
        {"fill_receipt_ref": None},
    ):
        with pytest.raises(PolicyViolation):
            replace(fill if "effect_digest" in changes else submit, **changes)
    with pytest.raises(PolicyViolation, match="ayni preview"):
        prepare_submit_plan(
            IDS[0],
            preview,
            fill_receipt_ref="receipt:bad",
            fill_receipt=replace(receipt, preview_digest=digest("wrong")),
        )

    authorization = Authorization.issue(
        realm_id=IDS[0],
        actor_id=IDS[2],
        plan_digest=fill.plan_digest,
        effect_digest=fill.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(fill.resource,), allowed_effects=(fill.effect_kind,)
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )

    class Store:
        consumed = False

        def get(self, authorization_id: UUID) -> Authorization:
            assert authorization_id == authorization.id
            return authorization

        def consume(self, *_args: Any, **_kwargs: Any) -> Any:
            self.consumed = True
            return SimpleNamespace(consumed=True)

    class Adapter:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def fill(self, form_ref: str, values: dict[str, str]) -> dict[str, Any]:
            self.calls.append((form_ref, values))
            return {"filled": True}

        def submit(self, form_ref: str) -> dict[str, Any]:
            raise AssertionError(form_ref)

    store, adapter = Store(), Adapter()
    terminal = HighRiskAutofillGuard(cast(Any, store)).apply(
        fill, preview, authorization_id=authorization.id, adapter=adapter, now=NOW
    )
    assert terminal.status == "completed" and store.consumed
    assert adapter.calls == [("form:visa", {"full_name": "Ada Lovelace"})]
    with pytest.raises(AuthorizationRequired):
        HighRiskAutofillGuard(cast(Any, Store())).apply(
            fill,
            preview,
            authorization_id=authorization.id,
            adapter=adapter,
            now=NOW + dt.timedelta(minutes=6),
        )


def test_mutation_target_hints_merge_receipt_and_reject_drift(tmp_path: Path) -> None:
    receipt = tmp_path / "close.json"
    receipt.write_text(
        json.dumps(
            {
                "project_id": str(IDS[0]),
                "work_item_id": str(IDS[1]),
                "run_id": str(IDS[2]),
                "session_id": "session-one",
                "client_id": "codex",
            }
        ),
        encoding="utf-8",
    )
    hints = CliMutationTargetHints.from_parameters(
        ("close", "apply"), {"apply": True, "input_file": receipt}
    )
    assert hints.run_ref == str(IDS[2]) and hints.client_ref == "codex"
    assert hints.merge_exact(CliMutationTargetHints(run_ref=str(IDS[2]))) == hints
    with pytest.raises(PolicyViolation, match="uyusmuyor"):
        hints.merge_exact(CliMutationTargetHints(run_ref=str(IDS[3])))
    with pytest.raises(PolicyViolation, match="uyusmuyor"):
        CliMutationTargetHints.from_parameters(
            ("close", "apply"),
            {"apply": True, "input_file": receipt, "run_id": IDS[3]},
        )
    receipt.write_text("[]", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="object"):
        CliMutationTargetHints.from_parameters(
            ("close", "apply"), {"apply": True, "input_file": receipt}
        )


def test_mutation_registry_unknown_exempt_readonly_and_backend_boundaries() -> None:
    registry = CliMutationAdmissionRegistry()
    unknown = registry.classify(("new", "write"), {"apply": True})
    assert unknown.mutating and unknown.requires_existing_hydration
    family = registry.classify(("memory", "new-leaf"), {})
    assert not family.mutating and family.requires_full_continuity
    init_read = registry.classify(("init",), {"dry_run": True})
    init_write = registry.classify(("init",), {})
    assert not init_read.mutating
    assert init_write.exemption is MutationAdmissionExemption.BOOTSTRAP
    local = assert_local_effect_admission(("backup", "create"))
    assert local.mutating and not local.requires_full_continuity
    with pytest.raises(PolicyViolation):
        assert_local_effect_admission(("unknown",))
    assert_full_continuity_backend(
        backend="sqlite", supports_full_continuity=True, admission=unknown
    )
    with pytest.raises(ZekamError, match="fallback yok"):
        assert_cli_mutation_admission(
            backend="sqlite", supports_full_continuity=False, admission=unknown
        )
    with pytest.raises(ZekamError, match="Realm session"):
        assert_full_continuity_backend(
            backend="sqlite",
            supports_full_continuity=False,
            admission=registry.classify(("read",), {}),
            realm_session_required=True,
        )


def test_mutation_dataclass_and_gate_capability_forgery_fail_closed() -> None:
    with pytest.raises(PolicyViolation):
        CliMutationTargetHints(session_ref=" ")
    with pytest.raises(PolicyViolation):
        CliMutationEvidence(" ", digest("x"))
    with pytest.raises(PolicyViolation):
        CliMutationEvidence("x", digest("x"), sequence=0)
    with pytest.raises(PolicyViolation):
        CliMutationEvidence("x", digest("x"), grants_authority=True)
    with pytest.raises(PolicyViolation):
        CliMutationRule((), True)
    with pytest.raises(PolicyViolation):
        CliMutationRule(("x",), True, read_only_parameter="dry")
    with pytest.raises(PolicyViolation):
        CliMutationAdmission(("x",), True, False, True, None, CliMutationTargetHints())
    with pytest.raises(PolicyViolation):
        ActiveRuntimeContinuityIdentity(IDS[0], IDS[1], IDS[2], IDS[3], "", "codex")
    with pytest.raises(PolicyViolation, match="state rejected"):
        _advance_gate_a_source_capability(object(), "INPUTS_VALID", "FIRST_CAPTURED")


def _oracle_config(*, url: str = "jdbc:oracle:thin:@//db.internal:1521/GPU") -> str:
    return f"""
spring:
  datasource:
    url: {url}
    username: GPU_APP
    password: private-value
app:
  schema:
    name: GPU_APP
"""


def test_oracle_config_secure_loader_and_sanitization_boundaries(tmp_path: Path) -> None:
    config = tmp_path / "app.yaml"
    config.write_text(_oracle_config(), encoding="utf-8")
    datasource = oracle.load_project_oracle_datasource(tmp_path, "app.yaml")
    assert datasource.dsn == "db.internal:1521/GPU"
    assert datasource.password == "private-value"
    assert "private-value" not in repr(datasource)
    assert "db.internal" not in str(datasource.sanitized())

    cases = {
        "duplicate.yaml": _oracle_config() + "app: {}\n",
        "mapping.yaml": "spring: []\napp: {}\n",
        "padded.yaml": _oracle_config().replace("username: GPU_APP", 'username: " GPU_APP"'),
        "nested.yaml": _oracle_config(url="jdbc:oracle:thin:@https://bad"),
        "schema.yaml": _oracle_config().replace("name: GPU_APP", "name: 9BAD"),
    }
    for name, body in cases.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
        with pytest.raises(ConfigurationError):
            oracle.load_project_oracle_datasource(tmp_path, name)
    with pytest.raises(PolicyViolation):
        oracle.load_project_oracle_datasource(tmp_path, "../outside.yaml")


class _OracleCursor:
    def __init__(self, *, empty: bool = False, secret: bool = False) -> None:
        self.statement = ""
        self.empty = empty
        self.secret = secret

    def __enter__(self) -> _OracleCursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, statement: str, *_args: Any, **_kwargs: Any) -> None:
        self.statement = statement

    def fetchone(self) -> Any:
        if "sys_context" in self.statement:
            return ("GPU", "PDB", "GPU_APP")
        if "all_users" in self.statement:
            return ("GPU_APP",)
        if "get_ddl" in self.statement:
            ddl = "CREATE TABLE GPU_APP.PRODUCT (ID NUMBER)"
            if self.secret:
                ddl += " password=supersecret"
            return (ddl,)
        return None

    def fetchall(self) -> list[tuple[str, str, str, str]]:
        if self.empty:
            return []
        return [("PRODUCT", "TABLE", "VALID", "2026-09-04T12:00:00")]


class _OracleConnection:
    def __init__(self, cursor: _OracleCursor) -> None:
        self._cursor = cursor
        self.closed = False
        self.call_timeout = 0

    def cursor(self) -> _OracleCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def test_oracle_client_collects_metadata_only_and_closes_on_terminal_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasource = oracle.OracleDatasource(
        "GPU_APP", digest("connection"), "app.yaml", "db:1521/GPU", "GPU_APP", "secret"
    )
    connection = _OracleConnection(_OracleCursor())
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(connect=lambda **_kw: connection))
    snapshot = oracle.OracleMetadataClient().collect(datasource)
    assert snapshot.objects[0].object_name == "PRODUCT"
    assert snapshot.sanitized()["object_count"] == 1
    assert connection.closed and connection.call_timeout == 120_000

    empty = _OracleConnection(_OracleCursor(empty=True))
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(connect=lambda **_kw: empty))
    with pytest.raises(ValidationFailed, match="nesne"):
        oracle.OracleMetadataClient().collect(datasource)
    assert empty.closed

    monkeypatch.setitem(
        sys.modules,
        "oracledb",
        SimpleNamespace(connect=lambda **_kw: (_ for _ in ()).throw(TimeoutError("late"))),
    )
    with pytest.raises(ConfigurationError, match="TimeoutError"):
        oracle.OracleMetadataClient(connect_timeout_seconds=1).collect(datasource)


def test_oracle_plan_chunks_long_ddl_and_is_replay_deterministic() -> None:
    ddl = "CREATE VIEW V AS SELECT 1 X FROM DUAL\n" + "X" * 6100
    item = oracle.OracleDdlObject(
        "GPU_APP", "V", "VIEW", "INVALID", "2026-09-04T12:00:00", digest_of_bytes(ddl.encode()), ddl
    )
    snapshot = oracle.OracleMetadataSnapshot("GPU_APP", digest("conn"), digest("db"), (item,), 2)
    first = oracle.build_oracle_metadata_index_plan(
        project_id=IDS[0], project_slug="gpu-app", snapshot=snapshot
    )
    replay = oracle.build_oracle_metadata_index_plan(
        project_id=IDS[0], project_slug="gpu-app", snapshot=snapshot
    )
    assert len(first.chunks) >= 2
    assert first.plan_digest == replay.plan_digest
    assert first.as_dict()["row_data_included"] is False
    assert snapshot.sanitized()["invalid_object_count"] == 1
    with pytest.raises(ValidationFailed, match="chunk"):
        oracle.build_oracle_metadata_index_plan(
            project_id=IDS[0],
            project_slug="gpu-app",
            snapshot=replace(snapshot, objects=()),
        )


@dataclass
class _Projects:
    project: Project
    aliases: list[Any] = field(default_factory=list)

    def add(self, project: Project) -> None:
        self.project = project

    def add_alias(self, alias: Any) -> None:
        self.aliases.append(alias)

    def get(self, project_id: UUID) -> Project:
        assert project_id == self.project.id
        return self.project


@dataclass
class _Bindings:
    bindings: list[SourceBinding] = field(default_factory=list)
    paths: dict[UUID, Path] = field(default_factory=dict)
    revision: SourceRevision | None = None

    def bind(self, binding: SourceBinding, *, absolute_path: Path) -> None:
        self.bindings = [binding]
        self.paths[binding.id] = absolute_path

    def for_project(self, _project_id: UUID) -> list[SourceBinding]:
        return self.bindings

    def local_path(self, binding_id: UUID) -> Path | None:
        return self.paths.get(binding_id)

    def latest_revision(self, _binding_id: UUID) -> SourceRevision | None:
        return self.revision

    def rebind(self, binding_id: UUID, *, absolute_path: Path, locator_digest: str) -> None:
        current = self.bindings[0]
        self.bindings[0] = replace(current, locator_digest=locator_digest)
        self.paths[binding_id] = absolute_path

    def get(self, _binding_id: UUID) -> SourceBinding:
        return self.bindings[0]


@dataclass
class _States:
    stage: IntegrationStage = IntegrationStage.REGISTERED
    detail: dict[str, Any] = field(default_factory=dict)

    def set(self, _project_id: UUID, *, stage: IntegrationStage, **values: Any) -> None:
        self.stage = stage
        self.detail = values.get("detail", self.detail)

    def get(self, _project_id: UUID) -> tuple[IntegrationStage, None, dict[str, Any]]:
        return self.stage, None, self.detail


def _integration_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[ProjectIntegrationService, _Projects, _Bindings, _States]:
    realm = Realm.create(now=NOW)
    initial = Project.create(realm=realm, slug="placeholder", now=NOW)
    projects, bindings, states = _Projects(initial), _Bindings(), _States()
    repos = {
        "project": projects,
        "source_binding": bindings,
        "project_capability_profile": SimpleNamespace(store=lambda **_kw: None),
        "integration_state": states,
    }
    monkeypatch.setattr(integration, "legacy_repository", lambda kind, *_args: repos[kind])
    return ProjectIntegrationService(object(), realm), projects, bindings, states


def test_project_integration_register_rebind_evaluate_and_action_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, projects, bindings, states = _integration_service(monkeypatch, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    project = service.register(source_path=source, aliases=("Source", "cash-register"), now=NOW)
    assert project.slug == "source"
    assert len(projects.aliases) == 2
    assert bindings.bindings[0].access_mode == "read-only"
    assert service.resolve_source_root(project.id) == source
    assert locator_digest_for(source) == bindings.bindings[0].locator_digest

    moved = tmp_path / "moved"
    moved.mkdir()
    rebound = service.rebind(project.id, source_path=moved)
    assert rebound.locator_digest == locator_digest_for(moved)
    assert states.stage is IntegrationStage.BOUND
    with pytest.raises(PolicyViolation):
        service.rebind(project.id, source_path=tmp_path / "absent")

    bindings.bindings.clear()
    report = service.evaluate(project.id)
    assert report.stage is IntegrationStage.REGISTERED
    assert report.blockers == ("source-binding-missing",)
    with pytest.raises(NotFound):
        service.resolve_source_root(project.id)

    actions = {
        stage: _next_action(stage, is_stale=False, blockers=()) for stage in IntegrationStage
    }
    assert "scan" in actions[IntegrationStage.BOUND]
    assert "current" in actions[IntegrationStage.PROFILED]
    assert "yeniden" in actions[IntegrationStage.STALE]
    assert "rebind" in _next_action(
        IntegrationStage.CURRENT, is_stale=True, blockers=("source-moved",)
    )


def test_project_report_currentness_and_knowledge_readiness() -> None:
    realm = Realm.create(now=NOW)
    project = Project.create(realm=realm, slug="cash", now=NOW)
    current = IntegrationReport(
        project,
        IntegrationStage.CURRENT,
        "done",
        False,
        None,
        None,
        None,
        digest("profile"),
        knowledge_index={"state": "ready"},
    )
    pending = replace(current, knowledge_index={"state": "pending"})
    stale = replace(current, is_stale=True)
    assert current.is_current and current.is_fully_integrated
    assert not pending.is_fully_integrated and not stale.is_current
    assert current.as_dict()["is_fully_integrated"] is True


def _loop_policy() -> LoopPolicy:
    return LoopPolicy(
        id=IDS[5],
        realm_id=IDS[0],
        project_id=IDS[1],
        work_item_id=IDS[2],
        plan_id=IDS[3],
        step_id="build",
        assignment_id=IDS[6],
        context_manifest_id=IDS[7],
        validator_assignment_id=IDS[8],
        max_attempts=3,
        max_tokens=10_000,
        max_cost_micros=10_000,
        deadline=NOW + dt.timedelta(hours=1),
        validator_spec_digest=digest("validator"),
        required_delta=(LoopDeltaKind.NEW_EVIDENCE,),
        forbidden_effects=(LoopEffectClass.DEPLOY,),
        terminal_states=tuple(sorted(LoopTerminalState, key=str)),
        source_revision="git:abc",
        context_manifest_digest=digest("context"),
        plan_digest=digest("plan"),
        policy_revision_digest=digest("policy"),
        canonical_effect_kind="file-write",
        created_at=NOW,
    )


def _objective(policy: LoopPolicy) -> OptimizationObjective:
    return OptimizationObjective(
        objective_id=IDS[9],
        realm_id=policy.realm_id,
        project_id=policy.project_id,
        work_item_id=policy.work_item_id,
        plan_id=policy.plan_id,
        step_id=policy.step_id,
        artifact_ref="logical:artifact",
        artifact_baseline_digest=digest("base"),
        measurement_plan_digest=digest("measure"),
        validator_asset_manifest_digest=digest("assets"),
        metric_specs=(
            MetricSpec(
                "quality",
                "Quality",
                "points",
                MetricDirection.MAXIMIZE,
                MetricRole.PRIMARY,
                "validator",
                target_value=10.0,
                minimum_meaningful_delta=0.5,
            ),
        ),
        max_attempts=3,
        max_tokens=10_000,
        max_cost_micros=10_000,
        deadline=policy.deadline,
        reversibility_class="inverse-patch",
        created_at=NOW,
    )


@dataclass
class _Measured:
    realm_id: UUID
    terminal: bool = False
    bindings: int = 0

    def unit_of_work(self) -> Any:
        return nullcontext()

    def assert_loop_open(self, _loop_id: UUID) -> None:
        if self.terminal:
            raise PolicyViolation("terminal")

    def bind_loop_attempt_job(self, **_kwargs: Any) -> bool:
        self.bindings += 1
        return self.bindings == 1


@dataclass
class _Jobs:
    realm_id: UUID
    jobs: dict[str, Any] = field(default_factory=dict)

    def enqueue(self, job: Any) -> tuple[Any, bool]:
        prior = self.jobs.get(job.idempotency_key)
        if prior is not None:
            return prior, False
        self.jobs[job.idempotency_key] = job
        return job, True


def test_loop_orchestrator_deterministic_replay_controls_and_terminal_integrity() -> None:
    policy = _loop_policy()
    objective = _objective(policy)
    measured, jobs = _Measured(policy.realm_id), _Jobs(policy.realm_id)
    service = DurableLoopOrchestrator(cast(Any, measured), cast(Any, jobs))
    plan = service.plan_attempt(
        objective=objective,
        policy=policy,
        attempt_ordinal=1,
        predecessor_attempt_id=None,
        progress_packet=None,
        now=NOW,
    )
    replay = service.plan_attempt(
        objective=objective,
        policy=policy,
        attempt_ordinal=1,
        predecessor_attempt_id=None,
        progress_packet=None,
        now=NOW,
    )
    assert plan.job.id == replay.job.id
    assert service.enqueue_attempt(plan).job_created
    assert not service.enqueue_attempt(replay).job_created
    measured.terminal = True
    with pytest.raises(PolicyViolation, match="terminal"):
        service.enqueue_attempt(plan)
    for control in (
        LoopAttemptAdmissionControl(paused=True),
        LoopAttemptAdmissionControl(draining=True),
        LoopAttemptAdmissionControl(cancelled=True),
    ):
        with pytest.raises(PolicyViolation):
            service.plan_attempt(
                objective=objective,
                policy=policy,
                attempt_ordinal=1,
                predecessor_attempt_id=None,
                progress_packet=None,
                control=control,
            )


def test_loop_production_authorization_and_scope_fail_closed() -> None:
    policy = _loop_policy()
    objective = _objective(policy)
    service = DurableLoopOrchestrator(
        cast(Any, _Measured(policy.realm_id)), cast(Any, _Jobs(policy.realm_id))
    )
    with pytest.raises(ValidationFailed, match="cift"):
        service.plan_attempt(
            objective=objective,
            policy=policy,
            attempt_ordinal=1,
            predecessor_attempt_id=None,
            progress_packet=None,
            effect_authorization_id=IDS[10],
        )
    with pytest.raises(ValidationFailed, match="uclu"):
        service.plan_attempt(
            objective=objective,
            policy=policy,
            attempt_ordinal=1,
            predecessor_attempt_id=None,
            progress_packet=None,
            topology_decision_id=IDS[10],
        )
    with pytest.raises(PolicyViolation, match="bounded-loop"):
        service.plan_attempt(
            objective=objective,
            policy=policy,
            attempt_ordinal=1,
            predecessor_attempt_id=None,
            progress_packet=None,
            topology_decision_id=IDS[10],
            topology_decision_digest=digest("topology"),
            topology_pattern="wide-loop",
        )
    production = service.plan_attempt(
        objective=objective,
        policy=policy,
        attempt_ordinal=1,
        predecessor_attempt_id=None,
        progress_packet=None,
        topology_decision_id=IDS[10],
        topology_decision_digest=digest("topology"),
        topology_pattern="bounded-loop",
        production_driver_digest=digest("driver"),
        now=NOW,
    )
    assert production.effect_scope_digest == measured_loop_effect_scope_digest(
        job_id=production.job.id,
        loop_id=policy.id,
        attempt_id=production.attempt_id,
        driver_digest=digest("driver"),
        source_revision=policy.source_revision,
        topology_decision_id=IDS[10],
        topology_decision_digest=digest("topology"),
        resources=(),
    )
    with pytest.raises(PolicyViolation, match="attached"):
        service.enqueue_attempt(production)
    with pytest.raises(PolicyViolation, match="scope"):
        service.attach_effect_authorization(production, IDS[11], digest("wrong"))
    attached = service.attach_effect_authorization(
        production, IDS[11], production.effect_scope_digest or ""
    )
    assert service.enqueue_attempt(attached).job_created
    with pytest.raises(PolicyViolation, match="zaten"):
        service.attach_effect_authorization(attached, IDS[12], attached.effect_scope_digest or "")
