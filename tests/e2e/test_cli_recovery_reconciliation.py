"""Exact authorization'li recovery CLI'nin gercek gecici PostgreSQL kabul testi."""

from __future__ import annotations

import datetime as dt
import json
import secrets
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zekam.application.config import DatabaseSettings
from zekam.application.execution import ExecutionHost
from zekam.application.governance import GovernanceService, default_capabilities
from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.canonical import digest
from zekam.domain.realm import Actor, ActorKind, Realm
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import Job, JobKind
from zekam.domain.work import EffectKind, PlanStep, TaskPlan, WorkItem, WorkType
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import ActorRepository, RealmRepository
from zekam.infrastructure.postgres.work_repository import TaskPlanRepository, WorkItemRepository
from zekam.interfaces.cli.main import app

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]

runner = CliRunner()
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


@pytest.fixture
def cli_home(
    tmp_path: Path, migrated_database: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> Path:
    home = tmp_path / "zekam-home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\n"
        "database:\n"
        f"  host: {migrated_database.host}\n"
        f"  port: {migrated_database.port}\n"
        f"  name: {migrated_database.name}\n"
        f"  user: {migrated_database.user}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZEKAM_HOME", str(home))
    return home


@pytest.fixture
def recovery_case(
    cli_home: Path,
    migrated_database: DatabaseSettings,
    tmp_path: Path,
) -> dict[str, Any]:
    del cli_home
    moment = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    realm = Realm.create(slug=f"recovery-{secrets.token_hex(4)}", now=moment)
    source = tmp_path / "source"
    source.mkdir()
    with connect(migrated_database) as connection:
        RealmRepository(connection).create(realm)
        configure_session(connection, realm_id=realm.id)
        project = ProjectIntegrationService(connection, realm).register(
            source_path=source, now=moment
        )
        governance = GovernanceService(connection, realm)
        policy = governance.ensure_default_policy(now=moment)
        for capability in default_capabilities(realm.id):
            governance.capabilities.append(capability)
        actor = Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="recovery-owner", now=moment)
        ActorRepository(connection, realm.id).add(actor)
        item = WorkItem.create(
            realm_id=realm.id,
            project_id=project.id,
            type=WorkType.TASK,
            title="P08 recovery",
            now=moment,
        )
        WorkItemRepository(connection, realm.id).add(item)
        plan = TaskPlan.create(
            work_item=item,
            revision=1,
            source_revision="source-revision-p08",
            policy_digest=policy.policy_digest,
            steps=(
                PlanStep(
                    "ZEKAM-P08-T01",
                    "P08 effect",
                    EffectKind.DATABASE_WRITE,
                    (f"project:{project.id}",),
                ),
                PlanStep(
                    "ZEKAM-P08-TEST",
                    "P08 verifier",
                    EffectKind.PROCESS_RUN,
                    (f"project:{project.id}",),
                    ("ZEKAM-P08-T01",),
                ),
            ),
            now=moment,
        )
        TaskPlanRepository(connection, realm.id).append(plan)
        host = ExecutionHost(connection, realm.id, worker_label="old-p08-worker")
        old_job, _ = host.jobs.enqueue(
            Job.create(
                realm_id=realm.id,
                project_id=project.id,
                kind=JobKind.MUTATION,
                idempotency_key=f"old-p08-{secrets.token_hex(4)}",
                resources=parse_requests(write=(f"project:{project.id}",)),
                required_capabilities=("database.write",),
                work_item_id=item.id,
                plan_id=plan.id,
                step_id="ZEKAM-P08-T01",
                now=moment,
            )
        )
        work = host.acquire_work(capabilities=("database.write",), lease_seconds=1, now=moment)
        assert work is not None and work.job.id == old_job.id
        old_claim = host.claim_effect(
            work,
            operation="apply-zekam-p08",
            effect_digest=DIGEST_A,
            authorization_digest=DIGEST_B,
            resources=work.job.resources,
            adapter_digest=DIGEST_A,
            now=moment,
        )

    document = {
        "schema": "zekam-recovery-reconciliation/v1",
        "project_id": str(project.id),
        "work_item_id": str(item.id),
        "task_plan_id": str(plan.id),
        "task_plan_digest": plan.plan_digest,
        "old_completion": {
            "job_id": str(old_job.id),
            "attempt_id": str(work.attempt_id),
            "claim_id": str(old_claim.id),
            "fencing_token": work.lease.fencing_token,
            "claim_digest": old_claim.claim_digest,
            "effect_digest": old_claim.effect_digest,
            "authorization_digest": old_claim.authorization_digest,
            "result_digest": digest("verified-p08-effect"),
        },
        "checkpoint": {
            "checkpoint_id": "recovery/p08/checkpoint-1",
            "source_revision": plan.source_revision,
            "plan_steps": list(plan.execution_order),
            "completed_steps": list(plan.execution_order),
            "pending_steps": [],
            "step_results": {
                "ZEKAM-P08-T01": digest("p08-source-effect"),
                "ZEKAM-P08-TEST": digest("p08-test-gates"),
            },
            "context_manifest_digest": digest("p08-context-manifest"),
            "journal_head_digest": digest("p08-journal-head"),
            "next_safe_action": "global-dod/provider-contracts",
            "created_at": moment.isoformat(),
        },
        "evidence_refs": [
            {
                "kind": "commit",
                "ref": "git/commit/p08-effect",
                "digest": digest("p08-commit"),
            },
            {
                "kind": "test",
                "ref": "quality/p08/gates",
                "digest": digest("p08-quality-gates"),
            },
        ],
    }
    input_file = tmp_path / "recovery.json"
    input_file.write_text(json.dumps(document), encoding="utf-8")
    return {
        "realm": realm,
        "actor": actor,
        "old_job": old_job,
        "old_claim": old_claim,
        "input_file": input_file,
        "database": migrated_database,
    }


def _args(case: dict[str, Any], cli_home: Path) -> list[str]:
    return [
        "--girdi",
        str(case["input_file"]),
        "--realm",
        case["realm"].slug,
        "--home",
        str(cli_home),
        "--json",
    ]


def test_recovery_cli_requires_auth_then_atomically_finalizes(
    recovery_case: dict[str, Any], cli_home: Path
) -> None:
    dry = runner.invoke(app, ["worker", "reconcile-recovery", *_args(recovery_case, cli_home)])
    assert dry.exit_code == 0, dry.stderr
    prepared = json.loads(dry.stdout)
    assert prepared["dry_run"] is True
    assert prepared["authorization_scope"]["max_uses"] == 1
    assert prepared["evidence_refs"][0]["ref"].startswith("git/")
    assert ":\\" not in dry.stdout and "C:\\" not in dry.stdout

    denied = runner.invoke(
        app,
        ["worker", "reconcile-recovery", *_args(recovery_case, cli_home), "--uygula"],
    )
    assert denied.exit_code != 0
    assert "authorization-id" in denied.stderr

    issued = runner.invoke(
        app,
        [
            "worker",
            "recovery-authorize",
            *_args(recovery_case, cli_home),
            "--actor-id",
            str(recovery_case["actor"].id),
            "--uygula",
        ],
    )
    assert issued.exit_code == 0, issued.stderr
    authorization_id = json.loads(issued.stdout)["authorization_id"]
    applied = runner.invoke(
        app,
        [
            "worker",
            "reconcile-recovery",
            *_args(recovery_case, cli_home),
            "--authorization-id",
            authorization_id,
            "--uygula",
        ],
    )
    assert applied.exit_code == 0, applied.stderr
    result = json.loads(applied.stdout)
    assert result["dry_run"] is False
    assert result["old_receipt_created"] is True

    with connect(recovery_case["database"]) as connection:
        configure_session(connection, realm_id=recovery_case["realm"].id)
        with connection.cursor() as cursor:
            cursor.execute(
                "select state, consumed_by from security.authorization where id = %s",
                (authorization_id,),
            )
            assert cursor.fetchone() == ("consumed", "cli:worker-reconcile-recovery")
            cursor.execute(
                "select state from runtime.job where id in (%s, %s) order by id",
                (recovery_case["old_job"].id, result["recovery_job_id"]),
            )
            assert [row[0] for row in cursor.fetchall()] == ["completed", "completed"]
            cursor.execute(
                "select count(*) from work.checkpoint where job_id = %s",
                (recovery_case["old_job"].id,),
            )
            assert int(cursor.fetchone()[0]) == 1
            cursor.execute(
                "select count(*) from runtime.effect_receipt where claim_id in (%s, %s)",
                (recovery_case["old_claim"].id, result["recovery_claim_id"]),
            )
            assert int(cursor.fetchone()[0]) == 2

    replay = runner.invoke(
        app,
        [
            "worker",
            "reconcile-recovery",
            *_args(recovery_case, cli_home),
            "--authorization-id",
            authorization_id,
            "--uygula",
        ],
    )
    assert replay.exit_code != 0


def test_recovery_cli_rejects_absolute_evidence_ref_before_mutation(
    recovery_case: dict[str, Any], cli_home: Path
) -> None:
    document = json.loads(recovery_case["input_file"].read_text(encoding="utf-8"))
    document["evidence_refs"][0]["ref"] = "C:\\sensitive\\evidence.json"
    recovery_case["input_file"].write_text(json.dumps(document), encoding="utf-8")

    result = runner.invoke(
        app, ["worker", "recovery-authorize", *_args(recovery_case, cli_home), "--uygula"]
    )
    assert result.exit_code != 0
    assert "absolute path" in result.stderr
