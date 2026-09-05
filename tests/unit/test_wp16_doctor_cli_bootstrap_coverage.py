from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from typer.testing import CliRunner

from zekam.application import doctor_repair_runtime as repair_runtime
from zekam.application import fresh_bootstrap
from zekam.application.doctor_repair import DoctorRepairPlan, GitFastForwardPlan, GitRepositoryState
from zekam.application.fresh_bootstrap import (
    FreshBootstrapPlan,
    _lock_is_stale,
    detect_legacy_postgresql_config,
    plan_fresh_bootstrap,
)
from zekam.application.setup import SetupStep
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation
from zekam.domain.model_inventory import CANONICAL_MODEL_COUNT
from zekam.domain.realm import ActorKind, LifecycleStatus
from zekam.domain.scheduler import REQUIRED_JOBS
from zekam.infrastructure.doctor import runtime_checks
from zekam.infrastructure.doctor.runtime_checks import (
    ClientsCheck,
    CommandSurfaceCheck,
    ModelInventoryCheck,
    OpenCodeSpoolCheck,
    PolicyCheck,
    QueueCheck,
    SchedulerCheck,
)
from zekam.infrastructure.sqlite.operational_schema import SQLiteOperationalSchema
from zekam.interfaces.cli import main as cli

REALM_ID = UUID("018f0000-0000-7000-8000-000000000001")
PROJECT_ID = UUID("018f0000-0000-7000-8000-000000000002")
ACTOR_ID = UUID("018f0000-0000-7000-8000-000000000003")
AUTHORITY = "sha256:" + "a" * 64
RUNNER = CliRunner()


class _Rows:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def __enter__(self) -> _Rows:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _Rows:
        return self

    def execute(self, _query: str, _parameters: object = None) -> None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows

    def fetchone(self) -> tuple[object, ...] | None:
        return None if not self.rows else self.rows[0]


def _settings() -> Any:
    return cast(Any, SimpleNamespace())


def test_runtime_checks_are_truthful_without_legacy_database() -> None:
    checks = (
        QueueCheck(_settings()),
        ModelInventoryCheck(_settings()),
        PolicyCheck(_settings()),
        SchedulerCheck(_settings()),
    )
    for check in checks:
        result = check.run()
        assert result.status.value == "skipped"
        assert result.evidence == {"reason": "Legacy runtime adapter etkin degil"}


def test_queue_check_reports_capacity_recovery_and_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_checks, "PSYCOPG_AVAILABLE", True)
    monkeypatch.setattr(runtime_checks, "connect", lambda _settings: _Rows([("ready", 101)]))
    monkeypatch.setattr(runtime_checks, "_resolved_campaign_recovery_count", lambda _c: 0)
    monkeypatch.setattr(
        runtime_checks,
        "_recovery_breakdown",
        lambda _c: {
            "no_claim": 1,
            "claim_without_receipt": 0,
            "failed_receipt": 0,
            "completed_receipt": 0,
        },
    )
    result = QueueCheck(_settings()).run()
    assert result.status.value == "passed"
    assert [finding.code for finding in result.findings] == ["runtime.queue-depth"]

    monkeypatch.setattr(
        runtime_checks,
        "connect",
        lambda _settings: _Rows([("running", 2), ("recovery-required", 3)]),
    )
    result = QueueCheck(_settings()).run()
    assert result.status.value == "degraded"
    assert result.evidence["recovery"] == 3
    assert {finding.code for finding in result.findings} == {"runtime.recovery-required"}

    monkeypatch.setattr(runtime_checks, "_resolved_campaign_recovery_count", lambda _c: 4)
    result = QueueCheck(_settings()).run()
    assert result.status.value == "skipped"
    assert result.evidence["reason"] == "resolved-recovery-count-drift"


def test_runtime_check_failure_and_inventory_policy_scheduler_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_checks, "PSYCOPG_AVAILABLE", True)

    def broken(_settings: Any) -> Any:
        raise RuntimeError("secret must not escape")

    monkeypatch.setattr(runtime_checks, "connect", broken)
    queue = QueueCheck(_settings()).run()
    assert queue.status.value == "skipped"
    assert queue.evidence == {"reason": "RuntimeError"}

    values = iter((0, CANONICAL_MODEL_COUNT - 1, CANONICAL_MODEL_COUNT))
    monkeypatch.setattr(runtime_checks, "_scalar", lambda *_a, **_kw: next(values))
    assert ModelInventoryCheck(_settings()).run().summary == "Model envanteri bos"
    assert "model iceri alinmis" in ModelInventoryCheck(_settings()).run().summary
    assert ModelInventoryCheck(_settings()).run().status.value == "passed"

    monkeypatch.setattr(runtime_checks, "connect", lambda _settings: _Rows([(0,)]))
    assert PolicyCheck(_settings()).run().status.value == "degraded"
    monkeypatch.setattr(runtime_checks, "connect", lambda _settings: _Rows([(2,)]))
    assert PolicyCheck(_settings()).run().status.value == "passed"

    monkeypatch.setattr(runtime_checks, "connect", lambda _settings: _Rows([]))
    assert SchedulerCheck(_settings()).run().status.value == "degraded"
    monkeypatch.setattr(
        runtime_checks,
        "connect",
        lambda _settings: _Rows([(name,) for name in REQUIRED_JOBS]),
    )
    assert SchedulerCheck(_settings()).run().status.value == "passed"
    assert CommandSurfaceCheck().run().status.value == "passed"


def test_client_and_spool_checks_cover_missing_present_and_sanitized_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = ClientsCheck().run()
    assert empty.status.value == "skipped"
    executable = tmp_path / "codex"
    executable.write_bytes(b"bounded")
    present = ClientsCheck((("codex", str(executable)),)).run()
    assert present.status.value == "passed"
    missing = ClientsCheck((("codex", str(tmp_path / "missing")),)).run()
    assert missing.status.value == "degraded"
    assert missing.evidence["missing"] == ["codex"]

    status = SimpleNamespace(
        queued=2,
        lock_present=True,
        legacy_candidates=3,
        eligible_legacy_candidates=1,
        invalid_legacy_candidates=2,
        unrecognized_entries=1,
        quarantine=4,
        as_dict=lambda: {"queued": 2, "quarantine": 4},
    )
    monkeypatch.setattr(runtime_checks, "inspect_spool", lambda _home: status)
    result = OpenCodeSpoolCheck(tmp_path).run()
    assert result.status.value == "degraded"
    assert {item.code for item in result.findings} == {
        "runtime.opencode-spool-queued",
        "runtime.opencode-spool-lock",
        "runtime.opencode-spool-legacy-candidates",
        "runtime.opencode-spool-unrecognized",
    }
    monkeypatch.setattr(
        runtime_checks,
        "inspect_spool",
        lambda _home: (_ for _ in ()).throw(PermissionError("private")),
    )
    failed = OpenCodeSpoolCheck(tmp_path).run()
    assert failed.status.value == "skipped"
    assert failed.evidence == {"reason": "PermissionError"}


def _git_plan(*, required: bool, blocked: tuple[str, ...] = ()) -> DoctorRepairPlan:
    state = GitRepositoryState(
        root=Path("/tmp/repository"),
        branch="main",
        head="a" * 40,
        upstream="origin/main",
        upstream_ref="refs/remotes/origin/main",
        upstream_head="b" * 40,
        remote="origin",
        remote_branch="main",
        ahead=0,
        behind=1 if required else 0,
        dirty_paths=(),
    )
    return DoctorRepairPlan(GitFastForwardPlan(state, blocked, required), None, None)


def test_doctor_runtime_rejects_plan_empty_and_blocked_before_mutation() -> None:
    context = cast(Any, SimpleNamespace())
    realm = cast(Any, SimpleNamespace())
    ready = _git_plan(required=True)
    with pytest.raises(PolicyViolation, match="digest exact"):
        repair_runtime.apply_doctor_repair_with_runtime(
            realm,
            context,
            repair_plan=ready,
            plan_digest=digest("wrong"),
            actor_id=ACTOR_ID,
            project_id=PROJECT_ID,
        )
    empty = _git_plan(required=False)
    with pytest.raises(PolicyViolation, match="uygulanacak adim yok"):
        repair_runtime.apply_doctor_repair_with_runtime(
            realm,
            context,
            repair_plan=empty,
            plan_digest=empty.plan_digest,
            actor_id=ACTOR_ID,
            project_id=PROJECT_ID,
        )
    blocked = _git_plan(required=True, blocked=("worktree-dirty",))
    with pytest.raises(PolicyViolation, match="worktree-dirty"):
        repair_runtime.apply_doctor_repair_with_runtime(
            realm,
            context,
            repair_plan=blocked,
            plan_digest=blocked.plan_digest,
            actor_id=ACTOR_ID,
            project_id=PROJECT_ID,
        )


def test_doctor_effect_and_step_bindings_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _git_plan(required=True)
    resources, request = repair_runtime._effect_request(
        plan, project_id=PROJECT_ID, step="git-fast-forward"
    )
    assert resources == (
        f"project:{PROJECT_ID}:source",
        f"path:{PROJECT_ID}:.git/refs/heads/main",
        "provider:git:origin:main",
    )
    assert request.touches_external_system is True

    no_remote = DoctorRepairPlan(
        GitFastForwardPlan(replace_git(plan.git.state, remote=None), (), True), None, None
    )
    with pytest.raises(PolicyViolation, match="remote/branch"):
        repair_runtime._effect_request(no_remote, project_id=PROJECT_ID, step="git-fast-forward")
    with pytest.raises(PolicyViolation, match="Routine repair plani yok"):
        repair_runtime._effect_request(plan, project_id=PROJECT_ID, step="other")

    expected = {"verified": True}
    monkeypatch.setattr(
        repair_runtime,
        "apply_git_fast_forward",
        lambda *_a, **_kw: SimpleNamespace(as_dict=lambda: expected),
    )
    result = repair_runtime._apply_step(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(core_path=Path("/tmp/repository"))),
        repair_plan=plan,
        step="git-fast-forward",
    )
    assert result == expected


def replace_git(state: GitRepositoryState, **changes: object) -> GitRepositoryState:
    values = {name: getattr(state, name) for name in state.__dataclass_fields__}
    values.update(changes)
    return GitRepositoryState(**cast(Any, values))


def test_doctor_actor_project_source_identity_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    core = Path("/tmp/repository")
    realm = cast(Any, SimpleNamespace(connection=object(), realm_id=REALM_ID, realm=object()))
    context = cast(Any, SimpleNamespace(core_path=core))
    actor = SimpleNamespace(kind=ActorKind.HUMAN, status=LifecycleStatus.ACTIVE)
    project = SimpleNamespace(status=LifecycleStatus.ACTIVE)

    def repository(_name: str, *_args: Any) -> Any:
        return SimpleNamespace(get=lambda _id: actor)

    integration = SimpleNamespace(
        projects=SimpleNamespace(get=lambda _id: project),
        resolve_source_root=lambda _id: core,
    )
    monkeypatch.setattr(repair_runtime, "legacy_repository", repository)
    monkeypatch.setattr(repair_runtime, "ProjectIntegrationService", lambda *_a: integration)
    repair_runtime._assert_actor_and_project(
        realm, context, actor_id=ACTOR_ID, project_id=PROJECT_ID
    )
    actor.kind = ActorKind.SYSTEM
    with pytest.raises(PolicyViolation, match="aktif human"):
        repair_runtime._assert_actor_and_project(
            realm, context, actor_id=ACTOR_ID, project_id=PROJECT_ID
        )
    actor.kind = ActorKind.HUMAN
    integration.resolve_source_root = lambda _id: Path("/tmp/other")
    with pytest.raises(PolicyViolation, match="exact Zekam source"):
        repair_runtime._assert_actor_and_project(
            realm, context, actor_id=ACTOR_ID, project_id=PROJECT_ID
        )


def test_cli_setup_json_dry_run_failure_and_success(monkeypatch: pytest.MonkeyPatch) -> None:
    steps = (
        SetupStep("first", ("first", "--apply"), True, "first"),
        SetupStep("second", ("second",), False, "second"),
    )
    monkeypatch.setattr(cli, "build_setup_plan", lambda **_: steps)
    dry = RUNNER.invoke(cli.app, ["setup", "--json"])
    assert dry.exit_code == 0
    dry_document = json.loads(dry.stdout)
    assert dry_document["apply"] is False

    calls: list[tuple[str, ...]] = []

    def failure(argv: tuple[str, ...], **_kwargs: object) -> Any:
        calls.append(tuple(argv))
        return SimpleNamespace(returncode=17)

    monkeypatch.setattr(cast(Any, cli).subprocess, "run", failure)
    failed = RUNNER.invoke(
        cli.app,
        [
            "setup",
            "--uygula",
            "--plan-digest",
            dry_document["plan_digest"],
            "--json",
        ],
    )
    assert failed.exit_code == 17
    assert json.loads(failed.stdout)["status"] == "failed"
    assert len(calls) == 1

    monkeypatch.setattr(
        cast(Any, cli).subprocess,
        "run",
        lambda argv, **_kwargs: SimpleNamespace(returncode=0),
    )
    completed = RUNNER.invoke(
        cli.app,
        [
            "setup",
            "--uygula",
            "--plan-digest",
            dry_document["plan_digest"],
            "--json",
        ],
    )
    assert completed.exit_code == 0
    assert json.loads(completed.stdout)["status"] == "completed"


def test_cli_helpers_project_selection_executable_and_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = cast(Any, SimpleNamespace(settings=SimpleNamespace(clients=()), core_path=tmp_path))
    monkeypatch.setattr(cast(Any, cli).shutil, "which", lambda _name: None)
    assert cli._opencode_executable(context) is None
    executable = tmp_path / "opencode"
    executable.write_bytes(b"local")
    monkeypatch.setattr(cast(Any, cli).shutil, "which", lambda _name: str(executable))
    assert cli._opencode_executable(context) == executable.resolve()
    with pytest.raises(Exception) as exit_info:
        cli._version_callback(True)
    assert getattr(exit_info.value, "exit_code", None) == 0


def test_fresh_bootstrap_malformed_config_path_and_stale_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    assert detect_legacy_postgresql_config(home).detected is False
    home.mkdir()
    (home / "config.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="mapping"):
        detect_legacy_postgresql_config(home)
    (home / "config.yaml").write_text(
        "database:\n  backend: sqlite\n  host: forbidden\n", encoding="utf-8"
    )
    detection = detect_legacy_postgresql_config(home)
    assert detection.reasons == ("legacy-connection-metadata-present",)

    core = tmp_path / "core"
    core.mkdir()
    regular = tmp_path / "regular-home"
    regular.write_bytes(b"user")
    with pytest.raises(ConfigurationError, match="regular directory"):
        plan_fresh_bootstrap(
            home=regular,
            core_root=core,
            authority_digest=AUTHORITY,
            schema=SQLiteOperationalSchema(),
        )
    assert regular.read_bytes() == b"user"

    plan = FreshBootstrapPlan(home, core, "create", AUTHORITY, digest("plan"))
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema": fresh_bootstrap.LOCK_SCHEMA,
                "home_name": home.name,
                "plan_digest": plan.plan_digest,
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )
    assert _lock_is_stale(lock, plan) is True
    current_pid = os.getpid()
    monkeypatch.setattr(cast(Any, fresh_bootstrap).os, "getpid", lambda: current_pid + 100_000)
    monkeypatch.setattr(
        cast(Any, fresh_bootstrap).os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert _lock_is_stale(lock, plan) is True
