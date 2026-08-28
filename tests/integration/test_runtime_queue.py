"""Durable queue, lease, kilit, claim ve receipt davranisi."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.integration.test_agent_residency_postgres import residency_scope as _residency_scope

from zekam.application.execution import (
    AdmissionDecision,
    AdmissionState,
    ExecutionHost,
    check_admission,
    summarize,
)
from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation
from zekam.domain.realm import Realm
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import (
    AttemptOutcome,
    FailureCategory,
    Job,
    JobKind,
    JobState,
    ReceiptStatus,
    ReconciledCompletionRequest,
    ReconciledFailureRequest,
    RecoveryOutcome,
)
from zekam.infrastructure.postgres import runtime_repository as runtime_repository_module

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

DIGEST = "sha256:" + "a" * 64
EVIDENCE_DIGEST = "sha256:" + "b" * 64
RECOVERY_NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def project_id(realm_session: tuple[Realm, Any], tmp_path: Path):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    root = tmp_path / "kaynak"
    root.mkdir()
    return ProjectIntegrationService(connection, realm).register(source_path=root).id


@pytest.fixture
def host(realm_session: tuple[Realm, Any]) -> ExecutionHost:
    realm, connection = realm_session
    return ExecutionHost(connection, realm.id, worker_label="worker-1")


def _job(project_id: Any, realm: Realm, **overrides: Any) -> Job:
    defaults: dict[str, Any] = {
        "realm_id": realm.id,
        "project_id": project_id,
        "kind": JobKind.MUTATION,
        "idempotency_key": f"job-{uuid4().hex[:8]}",
        "resources": parse_requests(write=("path:zekam:a.py",)),
        "required_capabilities": ("sandbox.write",),
    }
    defaults.update(overrides)
    return Job.create(**defaults)


def _recovery_request(work: Any, claim: Any, **overrides: Any) -> ReconciledCompletionRequest:
    values: dict[str, Any] = {
        "job_id": work.job.id,
        "attempt_id": work.attempt_id,
        "claim_id": claim.id,
        "fencing_token": work.lease.fencing_token,
        "claim_digest": claim.claim_digest,
        "effect_digest": claim.effect_digest,
        "authorization_digest": claim.authorization_digest,
        "result_digest": DIGEST,
        "adapter_evidence_digest": EVIDENCE_DIGEST,
    }
    values.update(overrides)
    return ReconciledCompletionRequest(**values)


def _failure_recovery_request(work: Any, claim: Any, receipt: Any) -> ReconciledFailureRequest:
    return ReconciledFailureRequest(
        job_id=work.job.id,
        attempt_id=work.attempt_id,
        claim_id=claim.id,
        receipt_id=receipt.id,
        fencing_token=work.lease.fencing_token,
        claim_digest=claim.claim_digest,
        effect_digest=claim.effect_digest,
        authorization_digest=claim.authorization_digest,
        failure_digest=receipt.failure_digest,
    )


# -- enqueue -----------------------------------------------------------------------------------


def test_enqueue_creates_job_and_outbox_event(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, connection = realm_session
    job, created = host.jobs.enqueue(_job(project_id, realm))
    assert created
    assert host.jobs.get(job.id).state is JobState.READY

    with connection.cursor() as cursor:
        cursor.execute("select count(*) from runtime.outbox_event where job_id = %s", (job.id,))
        assert int(cursor.fetchone()[0]) == 1


def test_duplicate_enqueue_creates_a_single_job(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, connection = realm_session
    first, created_first = host.jobs.enqueue(_job(project_id, realm, idempotency_key="ayni"))
    second, created_second = host.jobs.enqueue(_job(project_id, realm, idempotency_key="ayni"))
    assert created_first and not created_second
    assert first.id == second.id

    with connection.cursor() as cursor:
        cursor.execute("select count(*) from runtime.job where idempotency_key = 'ayni'")
        assert int(cursor.fetchone()[0]) == 1


def test_cross_realm_enqueue_is_rejected(host: ExecutionHost, project_id: Any) -> None:
    foreign = Job.create(
        realm_id=uuid4(),
        project_id=project_id,
        kind=JobKind.READ_ONLY,
        idempotency_key="yabanci",
    )
    with pytest.raises(PolicyViolation, match="Cross-realm"):
        host.jobs.enqueue(foreign)


# -- claim -------------------------------------------------------------------------------------


def test_claim_increments_fence_and_creates_attempt(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, connection = realm_session
    job, _ = host.jobs.enqueue(_job(project_id, realm))
    work = host.jobs.claim_next(worker_label="worker-1", capabilities=("sandbox.write",))

    assert work is not None
    assert work.job.id == job.id
    assert work.lease.fencing_token == 1
    assert work.job.state is JobState.RUNNING

    with connection.cursor() as cursor:
        cursor.execute(
            "select attempt_number, fencing_token from runtime.job_attempt where job_id = %s",
            (job.id,),
        )
        assert cursor.fetchone() == (1, 1)


def test_claim_requires_matching_capabilities(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm, required_capabilities=("gpu.render",)))
    assert host.jobs.claim_next(worker_label="w", capabilities=("sandbox.write",)) is None
    assert host.jobs.claim_next(worker_label="w", capabilities=("gpu.render",)) is not None


def test_claim_respects_available_at(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    host.jobs.enqueue(_job(project_id, realm, available_at=later))
    assert host.jobs.claim_next(worker_label="w", capabilities=("sandbox.write",)) is None


def test_claim_returns_none_on_empty_queue(host: ExecutionHost) -> None:
    assert host.jobs.claim_next(worker_label="w", capabilities=()) is None


def test_owner_token_is_never_persisted(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, connection = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.jobs.claim_next(worker_label="w", capabilities=("sandbox.write",))
    assert work is not None
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from runtime.lease where owner_digest like %s",
            (f"%{work.owner_token}%",),
        )
        assert int(cursor.fetchone()[0]) == 0


# -- heartbeat ve tamamlama --------------------------------------------------------------------


def test_heartbeat_extends_the_lease(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.jobs.claim_next(worker_label="w", capabilities=("sandbox.write",))
    assert work is not None
    assert host.jobs.heartbeat(
        work.job.id, token=work.owner_token, fencing_token=work.lease.fencing_token
    )


def test_heartbeat_with_wrong_token_fails(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.jobs.claim_next(worker_label="w", capabilities=("sandbox.write",))
    assert work is not None
    assert not host.jobs.heartbeat(
        work.job.id, token="yanlis-token", fencing_token=work.lease.fencing_token
    )


def test_heartbeat_with_stale_fence_fails(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.jobs.claim_next(worker_label="w", capabilities=("sandbox.write",))
    assert work is not None
    assert not host.jobs.heartbeat(work.job.id, token=work.owner_token, fencing_token=99)


def test_complete_releases_locks_and_lease(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, connection = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.acquire_work(capabilities=("sandbox.write",))
    assert work is not None
    assert host.locks.held_by(work.job.id)

    assert host.jobs.complete(
        work.job.id,
        token=work.owner_token,
        fencing_token=work.lease.fencing_token,
        outcome=AttemptOutcome.SUCCEEDED,
        result_digest=DIGEST,
    )
    assert host.jobs.get(work.job.id).state is JobState.COMPLETED
    assert host.locks.held_by(work.job.id) == ()
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from runtime.lease where job_id = %s", (work.job.id,))
        assert int(cursor.fetchone()[0]) == 0


def test_complete_with_stale_fence_is_rejected(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.jobs.claim_next(worker_label="w", capabilities=("sandbox.write",))
    assert work is not None
    assert not host.jobs.complete(
        work.job.id,
        token=work.owner_token,
        fencing_token=work.lease.fencing_token + 1,
        outcome=AttemptOutcome.SUCCEEDED,
        result_digest=DIGEST,
    )
    assert host.jobs.get(work.job.id).state is JobState.RUNNING


def test_attempt_result_is_immutable_once_terminal(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, connection = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.jobs.claim_next(worker_label="w", capabilities=("sandbox.write",))
    assert work is not None
    host.jobs.complete(
        work.job.id,
        token=work.owner_token,
        fencing_token=work.lease.fencing_token,
        outcome=AttemptOutcome.SUCCEEDED,
        result_digest=DIGEST,
    )
    with (
        pytest.raises(Exception, match="terminal attempt sonucu degistirilemez"),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "update runtime.job_attempt set outcome = 'failed' where id = %s", (work.attempt_id,)
        )


# -- kilitler ----------------------------------------------------------------------------------


def test_conflicting_write_lock_is_rejected(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    first, _ = host.jobs.enqueue(
        _job(project_id, realm, resources=parse_requests(write=("path:zekam:src",)))
    )
    second, _ = host.jobs.enqueue(
        _job(project_id, realm, resources=parse_requests(write=("path:zekam:src/a.py",)))
    )
    host.locks.acquire(first.id, parse_requests(write=("path:zekam:src",)))
    with pytest.raises(Exception, match="kilit catismasi"):
        host.locks.acquire(second.id, parse_requests(write=("path:zekam:src/a.py",)))


def test_two_read_locks_coexist(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    first, _ = host.jobs.enqueue(_job(project_id, realm))
    second, _ = host.jobs.enqueue(_job(project_id, realm))
    host.locks.acquire(first.id, parse_requests(read=("path:zekam:src/a.py",)))
    host.locks.acquire(second.id, parse_requests(read=("path:zekam:src/a.py",)))
    assert len(host.locks.all_locks()) == 2


def test_project_lock_conflicts_with_path_lock(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    first, _ = host.jobs.enqueue(_job(project_id, realm))
    second, _ = host.jobs.enqueue(_job(project_id, realm))
    host.locks.acquire(first.id, parse_requests(write=("project:zekam",)))
    with pytest.raises(Exception, match="kilit catismasi"):
        host.locks.acquire(second.id, parse_requests(read=("path:zekam:a.py",)))


def test_different_projects_do_not_conflict(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    first, _ = host.jobs.enqueue(_job(project_id, realm))
    second, _ = host.jobs.enqueue(_job(project_id, realm))
    host.locks.acquire(first.id, parse_requests(write=("path:zekam:a.py",)))
    host.locks.acquire(second.id, parse_requests(write=("path:gpu:a.py",)))
    assert len(host.locks.all_locks()) == 2


def test_same_job_can_reacquire_its_own_lock(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    job, _ = host.jobs.enqueue(_job(project_id, realm))
    host.locks.acquire(job.id, parse_requests(write=("path:zekam:a.py",)))
    host.locks.acquire(job.id, parse_requests(write=("path:zekam:a.py",)))
    assert len(host.locks.held_by(job.id)) == 1


def test_release_all_frees_the_resource(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    first, _ = host.jobs.enqueue(_job(project_id, realm))
    second, _ = host.jobs.enqueue(_job(project_id, realm))
    host.locks.acquire(first.id, parse_requests(write=("path:zekam:a.py",)))
    host.locks.release_all(first.id)
    host.locks.acquire(second.id, parse_requests(write=("path:zekam:a.py",)))
    assert len(host.locks.held_by(second.id)) == 1


# -- claim ve receipt --------------------------------------------------------------------------


def test_claim_then_receipt_completes_the_ledger(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.acquire_work(capabilities=("sandbox.write",))
    assert work is not None

    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=parse_requests(write=("path:zekam:a.py",)),
        adapter_digest=DIGEST,
    )
    assert host.pending_claims(work.job.id) == (claim,)

    host.record_success(claim, result_digest=DIGEST)
    assert host.pending_claims(work.job.id) == ()
    assert host.assess(work.job.id).outcome is RecoveryOutcome.RECONCILED_COMPLETED


def test_duplicate_claim_for_same_effect_is_rejected(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.acquire_work(capabilities=("sandbox.write",))
    assert work is not None
    host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
    )
    with pytest.raises(Exception, match="claim_idempotency_unique"):
        host.claim_effect(
            work,
            operation="file-write",
            effect_digest=DIGEST,
            authorization_digest=DIGEST,
            resources=(),
            adapter_digest=DIGEST,
        )


def test_second_receipt_for_same_claim_is_rejected(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.acquire_work(capabilities=("sandbox.write",))
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
    )
    host.record_success(claim, result_digest=DIGEST)
    with pytest.raises(Exception, match="receipt_claim_unique"):
        host.record_failure(claim, category=FailureCategory.ADAPTER)


def test_claim_and_receipt_are_append_only(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, connection = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.acquire_work(capabilities=("sandbox.write",))
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
    )
    host.record_success(claim, result_digest=DIGEST)
    for table in ("runtime.effect_claim", "runtime.effect_receipt"):
        with (
            pytest.raises(Exception, match=r"append-only|permission denied"),
            connection.cursor() as cursor,
        ):
            cursor.execute(f"delete from {table}")


# -- recovery ----------------------------------------------------------------------------------


def test_claim_without_receipt_blocks_completion(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.acquire_work(capabilities=("sandbox.write",))
    assert work is not None
    host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
    )
    with pytest.raises(PolicyViolation, match="Terminal receipt"):
        host.finish(work, outcome=AttemptOutcome.SUCCEEDED, result_digest=DIGEST)


def test_expired_lease_with_pending_claim_becomes_recovery_required(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.acquire_work(capabilities=("sandbox.write",), lease_seconds=1)
    assert work is not None
    host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
    )

    later = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5)
    assert host.jobs.reclaim_expired(now=later) == (work.job.id,)
    assert host.jobs.get(work.job.id).state is JobState.RECOVERY_REQUIRED


def test_expired_lease_without_claim_returns_to_ready(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm, kind=JobKind.READ_ONLY))
    work = host.acquire_work(capabilities=("sandbox.write",), lease_seconds=1)
    assert work is not None

    later = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5)
    host.jobs.reclaim_expired(now=later)
    assert host.jobs.get(work.job.id).state is JobState.READY


def test_recovery_marks_job_and_reports_next_action(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.acquire_work(capabilities=("sandbox.write",))
    assert work is not None
    host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
    )
    assessment = host.recover(work.job.id)
    assert assessment.outcome is RecoveryOutcome.RECOVERY_REQUIRED
    assert not assessment.silent_retry_allowed
    assert host.jobs.get(work.job.id).state is JobState.RECOVERY_REQUIRED


def test_adapter_evidence_reconciles_the_claim(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.acquire_work(capabilities=("sandbox.write",))
    assert work is not None
    host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
    )
    assessment = host.recover(work.job.id, adapter_evidence=ReceiptStatus.COMPLETED)
    assert assessment.outcome is RecoveryOutcome.RECONCILED_COMPLETED


def test_failed_receipt_reconciles_terminal_recovery_attempt(
    host: ExecutionHost,
    realm_session: tuple[Realm, Any],
    project_id: Any,
) -> None:
    realm, connection = realm_session
    failure_digest = "sha256:" + "c" * 64
    host.jobs.enqueue(_job(project_id, realm, now=RECOVERY_NOW))
    work = host.acquire_work(capabilities=("sandbox.write",), now=RECOVERY_NOW)
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
        now=RECOVERY_NOW,
    )
    receipt = host.record_failure(
        claim,
        category=FailureCategory.ADAPTER,
        failure_digest=failure_digest,
        now=RECOVERY_NOW,
    )
    assert host.finish(
        work,
        outcome=AttemptOutcome.RECOVERY_REQUIRED,
        result_digest=failure_digest,
        failure_category=FailureCategory.ADAPTER,
        now=RECOVERY_NOW,
    )

    request = _failure_recovery_request(work, claim, receipt)
    finalized = host.finalize_reconciled_failure(
        request, now=RECOVERY_NOW + dt.timedelta(seconds=1)
    )
    assert not finalized.created
    assert host.jobs.get(work.job.id).state is JobState.FAILED
    replay = host.finalize_reconciled_failure(request, now=RECOVERY_NOW + dt.timedelta(seconds=2))
    assert not replay.created
    with connection.cursor() as cursor:
        cursor.execute(
            "select outcome, failure_category, result_digest from runtime.job_attempt"
            " where id = %s",
            (work.attempt_id,),
        )
        assert cursor.fetchone() == (
            AttemptOutcome.RECOVERY_REQUIRED.value,
            FailureCategory.ADAPTER.value,
            failure_digest,
        )


@pytest.mark.parametrize("mark_recovery", (False, True))
def test_reconciled_completion_atomically_finalizes_expired_exact_claim(
    host: ExecutionHost,
    realm_session: tuple[Realm, Any],
    project_id: Any,
    mark_recovery: bool,
) -> None:
    realm, connection = realm_session
    host.jobs.enqueue(_job(project_id, realm, now=RECOVERY_NOW))
    work = host.acquire_work(capabilities=("sandbox.write",), lease_seconds=1, now=RECOVERY_NOW)
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
        now=RECOVERY_NOW,
    )
    if mark_recovery:
        host.jobs.mark_recovery_required(work.job.id, "adapter reconciliation bekleniyor")

    request = _recovery_request(work, claim)
    result = host.finalize_reconciled_completion(
        request, now=RECOVERY_NOW + dt.timedelta(seconds=2)
    )

    assert result.created
    assert result.receipt.status is ReceiptStatus.COMPLETED
    assert result.receipt.adapter_evidence_digest == EVIDENCE_DIGEST
    assert host.jobs.get(work.job.id).state is JobState.COMPLETED
    replay = host.finalize_reconciled_completion(
        request, now=RECOVERY_NOW + dt.timedelta(seconds=3)
    )
    assert not replay.created
    assert replay.receipt.id == result.receipt.id
    with connection.cursor() as cursor:
        cursor.execute(
            "select outcome, result_digest, finished_at is not null"
            " from runtime.job_attempt where id = %s",
            (work.attempt_id,),
        )
        assert cursor.fetchone() == ("succeeded", DIGEST, True)
        cursor.execute("select count(*) from runtime.lease where job_id = %s", (work.job.id,))
        assert int(cursor.fetchone()[0]) == 0
        cursor.execute(
            "select count(*) from runtime.resource_lock where job_id = %s", (work.job.id,)
        )
        assert int(cursor.fetchone()[0]) == 0
        cursor.execute(
            "select payload from runtime.execution_event"
            " where job_id = %s and event_type = 'job.reconciled-completed'",
            (work.job.id,),
        )
        assert dict(cursor.fetchone()[0]) == {
            "claim_id": str(claim.id),
            "receipt_id": str(result.receipt.id),
            "fencing_token": work.lease.fencing_token,
            "claim_digest": claim.claim_digest,
            "effect_digest": claim.effect_digest,
            "authorization_digest": claim.authorization_digest,
            "result_digest": DIGEST,
            "adapter_evidence_digest": EVIDENCE_DIGEST,
        }


def test_reconciled_completion_rejects_unexpired_lease_without_writes(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm, now=RECOVERY_NOW))
    work = host.acquire_work(capabilities=("sandbox.write",), lease_seconds=60, now=RECOVERY_NOW)
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
        now=RECOVERY_NOW,
    )

    with pytest.raises(PolicyViolation, match="henuz sona ermedi"):
        host.finalize_reconciled_completion(
            _recovery_request(work, claim), now=RECOVERY_NOW + dt.timedelta(seconds=2)
        )
    assert host.ledger.receipt_for_claim(claim.id) is None
    assert host.jobs.get(work.job.id).state is JobState.RUNNING


def test_reconciled_completion_accepts_exact_reclaimed_receiptless_claim(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, connection = realm_session
    host.jobs.enqueue(_job(project_id, realm, now=RECOVERY_NOW))
    work = host.acquire_work(capabilities=("sandbox.write",), lease_seconds=1, now=RECOVERY_NOW)
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
        now=RECOVERY_NOW,
    )
    later = RECOVERY_NOW + dt.timedelta(seconds=2)
    assert host.jobs.reclaim_expired(now=later) == (work.job.id,)
    assert host.jobs.get(work.job.id).state is JobState.RECOVERY_REQUIRED
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from runtime.lease where job_id = %s", (work.job.id,))
        assert int(cursor.fetchone()[0]) == 0

    finalized = host.finalize_reconciled_completion(
        _recovery_request(work, claim), now=later + dt.timedelta(seconds=1)
    )

    assert finalized.created
    assert finalized.receipt.status is ReceiptStatus.COMPLETED
    assert host.jobs.get(work.job.id).state is JobState.COMPLETED
    replayed = host.finalize_reconciled_completion(
        _recovery_request(work, claim), now=RECOVERY_NOW + dt.timedelta(seconds=3)
    )
    assert replayed.created is False
    assert replayed.receipt == finalized.receipt
    assert host.ledger.receipt_for_claim(claim.id) == finalized.receipt


def test_reconciled_completion_accepts_worker_terminal_recovery_attempt(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, connection = realm_session
    host.jobs.enqueue(_job(project_id, realm, now=RECOVERY_NOW))
    work = host.acquire_work(capabilities=("sandbox.write",), now=RECOVERY_NOW)
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
        now=RECOVERY_NOW,
    )
    assert host.finish(
        work,
        outcome=AttemptOutcome.RECOVERY_REQUIRED,
        result_digest=DIGEST,
        now=RECOVERY_NOW + dt.timedelta(seconds=1),
    )
    assert host.jobs.get(work.job.id).state is JobState.RECOVERY_REQUIRED
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from runtime.lease where job_id = %s", (work.job.id,))
        assert int(cursor.fetchone()[0]) == 0

    finalized = host.finalize_reconciled_completion(
        _recovery_request(work, claim), now=RECOVERY_NOW + dt.timedelta(seconds=2)
    )

    assert finalized.created
    assert finalized.receipt.status is ReceiptStatus.COMPLETED
    assert host.jobs.get(work.job.id).state is JobState.COMPLETED

    replayed = host.finalize_reconciled_completion(
        _recovery_request(work, claim), now=RECOVERY_NOW + dt.timedelta(seconds=3)
    )
    assert replayed.created is False
    assert replayed.receipt == finalized.receipt


def test_run_bound_pre_envelope_claim_reconciles_as_failed_no_effect(
    realm_session: tuple[Realm, Any], tmp_path: Path
) -> None:
    scope = _residency_scope.__wrapped__(realm_session, tmp_path)  # type: ignore[attr-defined]
    realm, connection = realm_session
    run = scope["run"]
    host = ExecutionHost(connection, realm.id, worker_label="no-effect-recovery-worker")
    job, created = host.jobs.enqueue(
        _job(
            run.project_id,
            realm,
            resources=(),
            work_item_id=run.work_item_id,
            plan_id=run.plan_id,
            step_id="build",
            assignment_id=scope["child_id"],
            run_id=run.id,
            now=RECOVERY_NOW,
        )
    )
    assert created
    work = host.acquire_work(capabilities=("sandbox.write",), now=RECOVERY_NOW)
    assert work is not None and work.job.id == job.id
    claim = host.claim_effect(
        work,
        operation="pre-envelope-bootstrap",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
        now=RECOVERY_NOW,
    )
    assert host.finish(
        work,
        outcome=AttemptOutcome.RECOVERY_REQUIRED,
        result_digest=DIGEST,
        now=RECOVERY_NOW + dt.timedelta(seconds=1),
    )
    failed_receipt = host.record_failure(
        claim,
        category=FailureCategory.ADAPTER,
        failure_digest=DIGEST,
        now=RECOVERY_NOW + dt.timedelta(seconds=2),
    )
    finalized = host.finalize_reconciled_failure(
        _failure_recovery_request(work, claim, failed_receipt),
        now=RECOVERY_NOW + dt.timedelta(seconds=3),
    )

    assert finalized.created is False
    assert finalized.receipt == failed_receipt
    assert host.jobs.get(job.id).state is JobState.FAILED
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from runtime.execution_envelope where realm_id=%s and job_id=%s",
            (realm.id, job.id),
        )
        assert int(cursor.fetchone()[0]) == 0


def test_reconciled_completion_closes_crash_after_terminal_receipt_without_duplicate(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, connection = realm_session
    host.jobs.enqueue(_job(project_id, realm, now=RECOVERY_NOW))
    work = host.acquire_work(capabilities=("sandbox.write",), lease_seconds=1, now=RECOVERY_NOW)
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
        now=RECOVERY_NOW,
    )
    committed_receipt = host.record_success(
        claim,
        result_digest=DIGEST,
        adapter_evidence_digest=EVIDENCE_DIGEST,
        now=RECOVERY_NOW + dt.timedelta(milliseconds=500),
    )

    finalized = host.finalize_reconciled_completion(
        _recovery_request(work, claim), now=RECOVERY_NOW + dt.timedelta(seconds=2)
    )

    assert not finalized.created
    assert finalized.receipt.id == committed_receipt.id
    assert host.jobs.get(work.job.id).state is JobState.COMPLETED
    assert len(host.ledger.claims_for_job(work.job.id)) == 1
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from runtime.effect_receipt where claim_id = %s",
            (claim.id,),
        )
        assert int(cursor.fetchone()[0]) == 1


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    (
        ({"job_id": uuid4()}, NotFound, "job bulunamadi"),
        ({"attempt_id": uuid4()}, NotFound, "claim bulunamadi"),
        ({"claim_id": uuid4()}, NotFound, "claim bulunamadi"),
        ({"fencing_token": 2}, ConcurrencyConflict, "job fencing token"),
        ({"claim_digest": "sha256:" + "c" * 64}, PolicyViolation, "claim digest"),
        ({"effect_digest": "sha256:" + "c" * 64}, PolicyViolation, "effect digest"),
        (
            {"authorization_digest": "sha256:" + "c" * 64},
            PolicyViolation,
            "authorization digest",
        ),
    ),
)
def test_reconciled_completion_rejects_exact_identity_drift(
    host: ExecutionHost,
    realm_session: tuple[Realm, Any],
    project_id: Any,
    overrides: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm, now=RECOVERY_NOW))
    work = host.acquire_work(capabilities=("sandbox.write",), lease_seconds=1, now=RECOVERY_NOW)
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
        now=RECOVERY_NOW,
    )

    with pytest.raises(error, match=message):
        host.finalize_reconciled_completion(
            _recovery_request(work, claim, **overrides),
            now=RECOVERY_NOW + dt.timedelta(seconds=2),
        )
    assert host.ledger.receipt_for_claim(claim.id) is None


def test_reconciled_completion_rejects_idempotent_replay_result_drift(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, connection = realm_session
    host.jobs.enqueue(_job(project_id, realm, now=RECOVERY_NOW))
    work = host.acquire_work(capabilities=("sandbox.write",), lease_seconds=1, now=RECOVERY_NOW)
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
        now=RECOVERY_NOW,
    )
    request = _recovery_request(work, claim)
    created = host.finalize_reconciled_completion(
        request, now=RECOVERY_NOW + dt.timedelta(seconds=2)
    )
    assert created.created

    with pytest.raises(PolicyViolation, match="result veya adapter evidence drift"):
        host.finalize_reconciled_completion(
            _recovery_request(work, claim, result_digest="sha256:" + "c" * 64),
            now=RECOVERY_NOW + dt.timedelta(seconds=3),
        )
    assert host.ledger.receipt_for_claim(claim.id) == created.receipt

    with connection.cursor() as cursor:
        cursor.execute(
            "insert into runtime.execution_event"
            " (id, realm_id, job_id, attempt_id, event_type, payload, occurred_at)"
            " select %s, realm_id, job_id, attempt_id, event_type, payload, occurred_at"
            " from runtime.execution_event"
            " where job_id = %s and event_type = 'job.reconciled-completed'",
            (uuid4(), work.job.id),
        )
    with pytest.raises(PolicyViolation, match="terminal execution event drift"):
        host.finalize_reconciled_completion(request, now=RECOVERY_NOW + dt.timedelta(seconds=4))


def test_reconciled_completion_rolls_back_every_write_when_terminal_event_fails(
    host: ExecutionHost,
    realm_session: tuple[Realm, Any],
    project_id: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    realm, connection = realm_session
    host.jobs.enqueue(_job(project_id, realm, now=RECOVERY_NOW))
    work = host.acquire_work(capabilities=("sandbox.write",), lease_seconds=1, now=RECOVERY_NOW)
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
        now=RECOVERY_NOW,
    )
    host.jobs.mark_recovery_required(work.job.id, "terminal event rollback testi")
    with connection.cursor() as cursor:
        cursor.execute(
            "select id from runtime.execution_event where job_id = %s order by occurred_at limit 1",
            (work.job.id,),
        )
        existing_event_id = cursor.fetchone()[0]
    monkeypatch.setattr(runtime_repository_module, "new_uuid7", lambda **_kwargs: existing_event_id)

    with pytest.raises(Exception, match="execution_event_pkey"):
        host.finalize_reconciled_completion(
            _recovery_request(work, claim), now=RECOVERY_NOW + dt.timedelta(seconds=2)
        )

    assert host.ledger.receipt_for_claim(claim.id) is None
    assert host.jobs.get(work.job.id).state is JobState.RECOVERY_REQUIRED
    with connection.cursor() as cursor:
        cursor.execute("select outcome from runtime.job_attempt where id = %s", (work.attempt_id,))
        assert cursor.fetchone()[0] is None
        cursor.execute("select count(*) from runtime.lease where job_id = %s", (work.job.id,))
        assert int(cursor.fetchone()[0]) == 1
        cursor.execute(
            "select count(*) from runtime.resource_lock where job_id = %s", (work.job.id,)
        )
        assert int(cursor.fetchone()[0]) == 1


def test_meaningful_recovery_requires_existing_canonical_checkpoint(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm, now=RECOVERY_NOW, payload={"meaningful_step": True}))
    work = host.acquire_work(capabilities=("sandbox.write",), lease_seconds=1, now=RECOVERY_NOW)
    assert work is not None
    claim = host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
        now=RECOVERY_NOW,
    )

    with pytest.raises(PolicyViolation, match="checkpoint ister"):
        host.finalize_reconciled_completion(
            _recovery_request(work, claim), now=RECOVERY_NOW + dt.timedelta(seconds=2)
        )
    assert host.ledger.receipt_for_claim(claim.id) is None
    assert host.jobs.get(work.job.id).state is JobState.RUNNING


def test_summary_flags_pending_claims(
    host: ExecutionHost, realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    host.jobs.enqueue(_job(project_id, realm))
    work = host.acquire_work(capabilities=("sandbox.write",))
    assert work is not None
    host.claim_effect(
        work,
        operation="file-write",
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        resources=(),
        adapter_digest=DIGEST,
    )
    summary = summarize(host)
    assert summary.pending_claims == 1
    assert summary.needs_attention


def test_missing_job_raises_not_found(host: ExecutionHost) -> None:
    with pytest.raises(NotFound):
        host.jobs.get(uuid4())


# -- admission ---------------------------------------------------------------------------------


def test_admission_defers_when_draining(realm_session: tuple[Realm, Any], project_id: Any) -> None:
    realm, _ = realm_session
    job = _job(project_id, realm)
    assert check_admission(job, AdmissionState(draining=True)).decision is (AdmissionDecision.DEFER)


def test_admission_defers_on_concurrency_limit(
    realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    job = _job(project_id, realm)
    state = AdmissionState(running_jobs=8, project_concurrency_limit=8)
    assert check_admission(job, state).reason == "project-concurrency-limit"


def test_admission_defers_provider_call_without_quota(
    realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    job = _job(project_id, realm, kind=JobKind.PROVIDER_CALL)
    assert check_admission(job, AdmissionState(quota_available=False)).reason == (
        "provider-quota-exhausted"
    )


def test_admission_rejects_exhausted_attempts(
    realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    job = _job(project_id, realm)
    exhausted = Job(
        id=job.id,
        realm_id=job.realm_id,
        project_id=job.project_id,
        kind=job.kind,
        state=job.state,
        idempotency_key=job.idempotency_key,
        attempt_count=3,
        max_attempts=3,
    )
    assert check_admission(exhausted, AdmissionState()).decision is AdmissionDecision.REJECT


def test_admission_admits_a_healthy_system(
    realm_session: tuple[Realm, Any], project_id: Any
) -> None:
    realm, _ = realm_session
    assert check_admission(_job(project_id, realm), AdmissionState()).admitted
