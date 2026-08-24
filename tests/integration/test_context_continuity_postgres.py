"""P08 context continuity PostgreSQL kabul testleri."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from zekam.application.context_continuity_service import ContextContinuityService
from zekam.application.execution import ExecutionHost
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    Checkpoint,
    ContextCandidate,
    ContinuitySnapshot,
    EvidenceReference,
    FinalizedHandoff,
    JournalEntry,
    compile_context,
)
from zekam.domain.errors import ConcurrencyConflict
from zekam.domain.runtime import AttemptOutcome, Job, JobKind
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)


def test_context_manifest_identity_is_scoped_to_exact_work(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "scoped-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    work = WorkGraphService(connection, realm)
    first_work = work.create_item(project_id=project.id, type=WorkType.TASK, title="First")
    second_work = work.create_item(project_id=project.id, type=WorkType.TASK, title="Second")
    manifest = compile_context(
        (
            ContextCandidate(
                "same-candidate",
                AuthorityLevel.VERIFIED,
                NOW,
                "revision-1",
                digest("same-evidence"),
                10,
                True,
            ),
        ),
        token_budget=20,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )

    first_id = ContextContinuityRepository(
        connection, realm.id, project.id, first_work.id
    ).store_manifest(manifest)
    second_id = ContextContinuityRepository(
        connection, realm.id, project.id, second_work.id
    ).store_manifest(manifest)

    assert first_id != second_id
    with connection.cursor() as cursor:
        cursor.execute(
            "select work_item_id from work.context_manifest"
            " where realm_id = %s and manifest_digest = %s order by work_item_id",
            (realm.id, manifest.manifest_digest),
        )
        assert {row[0] for row in cursor.fetchall()} == {first_work.id, second_work.id}


def test_context_continuity_repository_chain_checkpoint_handoff_and_terminal_gate(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    work = WorkGraphService(connection, realm)
    item = work.create_item(project_id=project.id, type=WorkType.TASK, title="Continuity")
    plan = work.create_plan(
        item.id,
        source_revision="revision-1",
        policy_digest=digest("policy"),
        steps=(
            PlanStep("read", "Read", EffectKind.NONE),
            PlanStep("build", "Build", EffectKind.FILE_WRITE, ("path:zekam:p08",), ("read",)),
        ),
    )
    repository = ContextContinuityRepository(connection, realm.id, project.id, item.id)
    manifest = compile_context(
        (
            ContextCandidate(
                "benchmark:model-decision",
                AuthorityLevel.VERIFIED,
                NOW,
                "revision-1",
                digest("decision-evidence-ref"),
                20,
                True,
            ),
        ),
        token_budget=50,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    repository.store_manifest(manifest)
    first = JournalEntry(1, str(item.id), "step-started", digest("one"), None, False, NOW)
    repository.append_journal(first, expected_head=None)
    second = JournalEntry(
        2, str(item.id), "step-summary", digest("two"), first.entry_digest, True, NOW
    )
    repository.append_journal(second, expected_head=first.entry_digest)
    with pytest.raises(ConcurrencyConflict):
        repository.append_journal(second, expected_head=first.entry_digest)

    host = ExecutionHost(connection, realm.id, worker_label="continuity-worker")
    job, _ = host.jobs.enqueue(
        Job.create(
            realm_id=realm.id,
            project_id=project.id,
            work_item_id=item.id,
            plan_id=plan.id,
            step_id="read",
            kind=JobKind.READ_ONLY,
            idempotency_key="continuity-meaningful",
            payload={"meaningful_step": "true"},
        )
    )
    claimed = host.acquire_work(capabilities=())
    assert claimed is not None and claimed.job.id == job.id
    with pytest.raises(Exception, match="requires checkpoint"):
        host.finish(claimed, outcome=AttemptOutcome.SUCCEEDED, result_digest=digest("result"))

    checkpoint = Checkpoint(
        str(item.id) + "-checkpoint",
        str(project.id),
        str(item.id),
        str(plan.id),
        "revision-1",
        ("read", "build"),
        ("read",),
        ("build",),
        (("read", digest("result")),),
        manifest.manifest_digest,
        second.entry_digest,
        "reacquire-work",
        NOW,
    )
    with pytest.raises(Exception, match="plan/source partition mismatch"):
        repository.store_checkpoint(
            replace(checkpoint, source_revision="revision-2"),
            task_plan_id=plan.id,
            job_id=job.id,
        )
    checkpoint_id = repository.store_checkpoint(checkpoint, task_plan_id=plan.id, job_id=job.id)
    host.finish(claimed, outcome=AttemptOutcome.SUCCEEDED, result_digest=digest("result"))
    snapshot = ContinuitySnapshot(
        str(project.id),
        str(item.id),
        checkpoint.checkpoint_digest,
        second.entry_digest,
        manifest.manifest_digest,
        "revision-1",
        ("docs/MODEL_BENCHMARK_VE_ROUTING.md",),
        ("reacquire-work",),
        (EvidenceReference("benchmark", "model-decision:latest", digest("decision-ref")),),
        NOW,
    )
    snapshot_id = repository.store_snapshot(snapshot, checkpoint_id=checkpoint_id)
    handoff = FinalizedHandoff(
        "codex",
        "claude",
        "model-ref-a",
        "model-ref-b",
        snapshot.snapshot_digest,
        checkpoint.checkpoint_digest,
        "revision-1",
        NOW,
    )
    repository.store_handoff(handoff, snapshot_id=snapshot_id)
    loaded_handoff, loaded_snapshot, loaded_checkpoint = repository.load_resume_bundle(
        handoff.handoff_digest
    )
    resumed = ContextContinuityService().resume(
        handoff=loaded_handoff,
        snapshot=loaded_snapshot,
        checkpoint=loaded_checkpoint,
        current_source_revision="revision-1",
    )
    assert resumed.client == "claude"
    assert resumed.model_ref == "model-ref-b"
    assert resumed.reacquire_work is True
    assert loaded_checkpoint.plan_steps == ("read", "build")
    with connection.cursor() as cursor:
        cursor.execute(
            "select state from runtime.job where id = %s",
            (job.id,),
        )
        assert cursor.fetchone()[0] == "completed"
        cursor.execute(
            "select count(*) from work.finalized_handoff where work_item_id = %s",
            (item.id,),
        )
        assert cursor.fetchone()[0] == 1
    with (
        pytest.raises(Exception, match=r"append-only|permission denied"),
        connection.cursor() as cursor,
    ):
        cursor.execute("delete from work.work_journal_entry where work_item_id = %s", (item.id,))
