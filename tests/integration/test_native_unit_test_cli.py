"""`zekam test plan --native / measure / evidence` CLI yuzeyi: gercek Maven + gercek ledger.

A25 (OpenCode executable yok), A26 (eski explicit OpenCode batch yolu korunur) ve A27 (basari
uretmeyen durumlar) CLI sinirinda dogrulanir. Model/provider/ag cagrisi yoktur.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from tests.integration.native_unit_test_support import (
    SOURCE,
    Env,
    make_bindings,
    make_env,
)
from typer.testing import CliRunner

from zekam.interfaces.cli.main import app

runner = CliRunner()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("mvn") is None, reason="Maven bu cihazda kurulu degil"),
]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


def _plan_args(
    env: Env, bindings, *, percent: str = "50", work_item_id: str | None = None
) -> list[str]:
    return [
        "--work-item-id",
        work_item_id or bindings.work_item_id,
        "--project-id",
        env.project_id,
        "--source-binding-id",
        bindings.source_binding_id,
        "--source-revision",
        env.revision,
        "--source",
        SOURCE,
        "--percent",
        percent,
        "--max-attempts",
        "2",
        "--process-timeout-seconds",
        "180",
        "--total-elapsed-seconds",
        "400",
        "--project-root",
        str(env.root),
        "--source-snapshot-id",
        bindings.source_snapshot_id,
        "--graph-generation-digest",
        bindings.graph_generation_digest,
    ]


def _json(result) -> dict:
    return json.loads(result.stdout.strip().splitlines()[-1])


def _plan(
    env: Env,
    bindings,
    *,
    percent: str = "50",
    native: bool = True,
    work_item_id: str | None = None,
) -> dict:
    args = ["test", "plan", *_plan_args(env, bindings, percent=percent, work_item_id=work_item_id)]
    if native:
        args.append("--native")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def _measure_args(
    env: Env, bindings, plan: dict, *, percent: str = "50", work_item_id: str | None = None
) -> list[str]:
    return [
        "test",
        "measure",
        *_plan_args(env, bindings, percent=percent, work_item_id=work_item_id),
        "--plan-digest",
        plan["plan_digest"],
        "--maven-plan-digest",
        plan["execution"]["plan_digest"],
        "--realm-id",
        bindings.realm_id,
        "--project-uuid",
        env.project_id,
        "--home",
        str(env.home),
    ]


def test_help_lists_native_helper_commands() -> None:
    root_help = runner.invoke(app, ["test", "--help"])
    assert root_help.exit_code == 0
    for name in ("measure", "evidence", "plan", "run"):
        assert name in root_help.stdout
    measure_help = runner.invoke(app, ["test", "measure", "--help"])
    assert measure_help.exit_code == 0
    for option in ("--plan-digest", "--authorize", "--build", "--maven-plan-digest"):
        assert option in measure_help.stdout
    assert "--native" in runner.invoke(app, ["test", "plan", "--help"]).stdout


def test_native_plan_is_distinct_and_needs_no_opencode(env: Env, tmp_path: Path) -> None:
    bindings = make_bindings(env, tmp_path)
    native = _plan(env, bindings)
    batch = _plan(env, bindings, native=False)
    assert native["mode"] == "native-helper"
    assert native["opencode_required"] is False and native["provider_calls"] == 0
    assert native["authorization"] == "not-granted"
    assert "mode" not in batch  # eski plan digest sozlesmesi degismez
    assert native["plan_digest"] != batch["plan_digest"]
    assert native["request_digest"] != batch["request_digest"]
    assert native["execution"]["status"] == "ready"
    assert native["execution"]["target_modules"] == [""]


def test_measure_gates_fail_closed_before_any_effect(env: Env, tmp_path: Path) -> None:
    bindings = make_bindings(env, tmp_path)
    plan = _plan(env, bindings)
    base = _measure_args(env, bindings, plan)

    def run(extra: list[str]) -> tuple[int, dict]:
        result = runner.invoke(app, extra)
        return result.exit_code, _json(result)

    code, doc = run(base)
    assert code == 77 and "explicit-measure-authorization-missing" in doc["details"]
    code, doc = run([*base, "--authorize"])
    assert code == 77 and "build-approval-missing" in doc["details"]
    code, doc = run(
        [*base[: base.index("--plan-digest")], *base[base.index("--plan-digest") + 2 :]]
    )
    assert code == 77 and "exact-plan-digest-missing" in doc["details"]
    tampered = [*base]
    tampered[tampered.index("--percent") + 1] = "60"
    code, doc = run([*tampered, "--authorize", "--build"])
    assert code == 77 and "exact-plan-digest-mismatch" in doc["details"]
    # Hicbir kapi gecmedi: ledger'da request/attempt yok.
    status = runner.invoke(app, ["test", "status", "--home", str(env.home)])
    assert json.loads(status.stdout)["requests"] == []


def test_measure_rejects_stale_work_binding(env: Env, tmp_path: Path) -> None:
    bindings = make_bindings(env, tmp_path)
    unknown_work = "01a10959-24e8-7f06-8e51-d4ca73b9f21b"
    plan = _plan(env, bindings, work_item_id=unknown_work)
    args = _measure_args(env, bindings, plan, work_item_id=unknown_work)
    result = runner.invoke(app, [*args, "--authorize", "--build", "--allow-network"])
    assert result.exit_code == 30, result.stdout
    assert "binding stale or mismatched" in result.stdout


def test_measure_cli_records_evidence_and_remeasure_is_budgeted(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A25: PATH'te OpenCode yok; gercek olcum + kayit + kanit okuma + butceli tekrar."""

    monkeypatch.setattr(shutil, "which", _which_without_opencode(shutil.which))
    bindings = make_bindings(env, tmp_path)
    plan = _plan(env, bindings, percent="80")
    base = _measure_args(env, bindings, plan, percent="80")
    no_network = runner.invoke(app, [*base, "--authorize", "--build"])
    assert no_network.exit_code == 77
    assert "network-authorization-missing" in no_network.stdout
    args = [*base, "--authorize", "--build", "--allow-network"]

    first = runner.invoke(app, args)
    first_doc = _json(first)
    assert first_doc["outcome"] == "below-target", first.stdout
    assert first.exit_code == 22
    assert first_doc["model_calls"] == 0 and first_doc["opencode_required"] is False
    assert first_doc["receipt_recorded"] is True and first_doc["terminal_recorded"] is False

    evidence = runner.invoke(
        app, ["test", "evidence", plan["request_digest"], "--home", str(env.home)]
    )
    assert evidence.exit_code == 0, evidence.stdout
    evidence_doc = json.loads(evidence.stdout)
    (attempt,) = evidence_doc["attempts"]
    assert evidence_doc["native_request"] is True and evidence_doc["terminals"] == []
    assert attempt["receipt"]["status"] == "completed"
    assert attempt["receipt"]["evidence"]["verified"] is True
    assert attempt["receipt"]["evidence"]["outcome"] == "below-target"
    assert attempt["receipt"]["evidence"]["freshness"] == "fresh"
    assert attempt["receipt"]["observations"]

    # Test ekle (native ajan rolu) -> kontrollu tekrar olcum -> hedef.
    from tests.integration.native_unit_test_support import EXTRA_TEST, EXTRA_TEST_BODY

    (env.root / EXTRA_TEST).write_text(EXTRA_TEST_BODY, encoding="utf-8")
    second = runner.invoke(app, args)
    second_doc = _json(second)
    assert second_doc["outcome"] == "target-met", second.stdout
    assert second.exit_code == 0 and second_doc["remaining_attempts"] == 0
    assert second_doc["ordinal"] == 2

    third = runner.invoke(app, args)
    assert _json(third)["outcome"] == "budget-exhausted"
    assert third.exit_code == 20

    report = runner.invoke(app, ["test", "report", plan["request_digest"], "--home", str(env.home)])
    assert json.loads(report.stdout)["attempt_count"] == 2


def test_explicit_opencode_batch_path_keeps_its_prerequisite(env: Env, tmp_path: Path) -> None:
    """A26: eski explicit batch kompozisyonu hala OpenCode executable ister ve gateway kurar."""

    from uuid import uuid4

    from zekam.application.unit_test_agents import CanonicalUnitTestAgentGateway
    from zekam.application.unit_test_runtime import (
        UnitTestRuntimeBinding,
        compose_unit_test_runtime,
    )
    from zekam.domain.canonical import digest
    from zekam.domain.errors import ConfigurationError
    from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

    kwargs = {
        "realm_id": uuid4(),
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "coordinator_assignment_id": uuid4(),
        "project_root": env.root,
        "object_store_root": env.home / "artifacts" / "sha256",
        "lock_dir": env.home / "runtime" / "unit-test-locks",
        "approved_maven_plan_digest": digest("maven-plan"),
    }
    with pytest.raises(ConfigurationError, match="OpenCode executable"):
        UnitTestRuntimeBinding(opencode_executable=tmp_path / "yok.exe", **kwargs)
    executable = tmp_path / "opencode.exe"
    executable.write_bytes(b"fixture")
    binding = UnitTestRuntimeBinding(
        opencode_executable=executable,
        model_id="fixture/builder-model",
        verifier_model_id="fixture/verifier-model",
        **kwargs,
    )
    request = env.request()
    store = SQLiteOperationalStore(env.database)
    with store.unit_of_work() as uow:
        runtime = compose_unit_test_runtime(
            request,
            ledger=uow.unit_test_ledger(),
            binding=binding,
            artifact_registrar=lambda *_args: None,
        )
        uow.rollback()
    assert isinstance(runtime.gateway, CanonicalUnitTestAgentGateway)
    assert runtime.loop is not None


def _which_without_opencode(original):
    def which(name, *args, **kwargs):
        if str(name).lower().startswith("opencode"):
            return None
        return original(name, *args, **kwargs)

    return which


def test_real_native_measurement_is_bound_to_job_checkpoint_and_exact_cas_replay(tmp_path: Path):
    from zekam.domain.canonical import digest
    from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
    from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

    env = make_env(tmp_path, binding_schema=True)
    SQLiteLocalRuntimeStore(env.database)
    bindings = make_bindings(env, tmp_path)
    initial = runner.invoke(
        app, ["test", "plan", *_plan_args(env, bindings), "--native", "--canonical-source"]
    )
    assert initial.exit_code == 0, initial.stdout
    observed = _json(initial)
    store = SQLiteOperationalStore(env.database)
    with store.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest({}), task_digest=digest("task"), sanitized_config={}
        )
        snapshot = uow.capture_source_snapshot(
            source_binding_id=bindings.source_binding_id,
            revision_ref=env.revision,
            tree_digest=digest("production-scope"),
            content_digest=observed["source_content_digest"],
            config_digest=config.config_digest,
        )
        run = uow.create_run(
            work_item_id=bindings.work_item_id,
            config_revision_id=config.id,
            plan_digest=observed["execution"]["plan_digest"],
            budget={"seconds": 180},
            source_snapshot_id=snapshot.id,
        )
        uow.commit()
    bindings.source_snapshot_id = snapshot.id
    bound_plan = runner.invoke(
        app,
        [
            "test",
            "plan",
            *_plan_args(env, bindings),
            "--native",
            "--run-id",
            run.id,
            "--canonical-work-revision",
            "1",
            "--home",
            str(env.home),
        ],
    )
    assert bound_plan.exit_code == 0, bound_plan.stdout
    plan = _json(bound_plan)
    args = [
        *_measure_args(env, bindings, plan),
        "--run-id",
        run.id,
        "--canonical-work-revision",
        "1",
        "--canonical-binding-digest",
        plan["work_binding"]["binding_digest"],
        "--authorize",
        "--build",
        "--allow-network",
    ]
    measured = runner.invoke(app, args)
    result = _json(measured)
    assert result["receipt_recorded"] is True, measured.stdout
    assert result["canonical_replayed"] is False and result["canonical_job_id"]
    with store.unit_of_work() as uow:
        assert len(uow.list_checkpoints(run.id)) == 1
        assert len(uow.unit_test_ledger().list_attempts(plan["request_digest"])) == 1
        uow.activate_config(
            config_digest=digest({"revision": 2}),
            task_digest=digest("new-task"),
            sanitized_config={"revision": 2},
        )
        uow.commit()
    source_path = env.root / SOURCE
    source_path.write_text(source_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    replayed = runner.invoke(app, args)
    replay = _json(replayed)
    assert replay["canonical_replayed"] is True, replayed.stdout
    assert replay["evidence_digest"] == result["evidence_digest"]
    queue = SQLiteLocalRuntimeStore(env.database, existing_only=True)
    status = queue.job_snapshot(result["canonical_job_id"])
    assert status["attempt_count"] == 1 and len(status["effects"]) == 1
