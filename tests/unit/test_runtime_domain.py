"""Runtime alan modeli: job, lease, claim, receipt ve recovery karari."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.resources import LockMode, ResourceRequest, parse_requests
from zekam.domain.runtime import (
    TERMINAL_JOB_STATES,
    AttemptOutcome,
    EffectClaim,
    EffectReceipt,
    FailureCategory,
    Job,
    JobKind,
    JobState,
    Lease,
    ReceiptStatus,
    ReconciledCompletionRequest,
    RecoveryOutcome,
    assert_no_silent_retry,
    assess_recovery,
    new_owner_token,
    owner_digest,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)
REALM = uuid4()
PROJECT = uuid4()
DIGEST = "sha256:" + "a" * 64


def _job(**overrides: object) -> Job:
    defaults: dict[str, object] = {
        "realm_id": REALM,
        "project_id": PROJECT,
        "kind": JobKind.MUTATION,
        "idempotency_key": "job-1",
        "resources": parse_requests(write=("path:zekam:a.py",)),
        "now": NOW,
    }
    defaults.update(overrides)
    return Job.create(**defaults)  # type: ignore[arg-type]


def _claim(**overrides: object) -> EffectClaim:
    defaults: dict[str, object] = {
        "realm_id": REALM,
        "job_id": uuid4(),
        "attempt_id": uuid4(),
        "operation": "file-write",
        "effect_digest": DIGEST,
        "authorization_digest": DIGEST,
        "idempotency_key": "claim-1",
        "resources": parse_requests(write=("path:zekam:a.py",)),
        "execution_identity": "worker-1:1",
        "fencing_token": 1,
        "adapter_digest": DIGEST,
        "now": NOW,
    }
    defaults.update(overrides)
    return EffectClaim.create(**defaults)  # type: ignore[arg-type]


# -- job ------------------------------------------------------------------------------


def test_new_job_is_ready_with_zero_fence() -> None:
    job = _job()
    assert job.state is JobState.READY
    assert job.fencing_token == 0
    assert job.attempt_count == 0
    assert not job.is_terminal


def test_terminal_states_are_closed_set() -> None:
    assert set(TERMINAL_JOB_STATES) == {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}


def test_read_only_job_produces_no_effect() -> None:
    assert not _job(kind=JobKind.READ_ONLY).produces_effect
    assert _job(kind=JobKind.PROVIDER_CALL).produces_effect


def test_write_resources_are_reported() -> None:
    job = _job(resources=parse_requests(read=("path:zekam:r.py",), write=("path:zekam:w.py",)))
    assert [item.resource.text for item in job.write_resources] == ["path:zekam:w.py"]


def test_resources_are_stored_in_lock_order() -> None:
    job = _job(resources=parse_requests(write=("path:zekam:z.py", "path:zekam:a.py")))
    assert [item.resource.text for item in job.resources] == [
        "path:zekam:a.py",
        "path:zekam:z.py",
    ]


def test_blank_idempotency_key_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        _job(idempotency_key="   ")


def test_attempts_left_respects_limit() -> None:
    job = _job()
    assert job.has_attempts_left()
    exhausted = Job(
        id=job.id,
        realm_id=job.realm_id,
        project_id=job.project_id,
        kind=job.kind,
        state=job.state,
        idempotency_key=job.idempotency_key,
        attempt_count=3,
        max_attempts=3,
        created_at=NOW,
        available_at=NOW,
    )
    assert not exhausted.has_attempts_left()


# -- lease ------------------------------------------------------------------------------


def _lease(**overrides: object) -> Lease:
    token = str(overrides.pop("token", "gizli-token"))
    defaults: dict[str, object] = {
        "id": uuid4(),
        "realm_id": REALM,
        "job_id": uuid4(),
        "attempt_id": uuid4(),
        "owner_digest": owner_digest(token),
        "fencing_token": 1,
        "expires_at": NOW + dt.timedelta(seconds=60),
        "heartbeat_at": NOW,
        "worker_label": "worker-1",
    }
    defaults.update(overrides)
    return Lease(**defaults)  # type: ignore[arg-type]


def test_owner_token_is_never_stored_in_plaintext() -> None:
    token = new_owner_token()
    lease = _lease(token=token)
    assert token not in repr(lease.as_dict())
    assert lease.owner_digest == owner_digest(token)


def test_lease_matches_only_with_token_and_fence() -> None:
    lease = _lease(token="dogru-token", fencing_token=5)
    assert lease.matches(token="dogru-token", fencing_token=5)
    assert not lease.matches(token="yanlis-token", fencing_token=5)
    assert not lease.matches(token="dogru-token", fencing_token=4)


def test_lease_expiry_is_respected() -> None:
    lease = _lease()
    assert lease.is_valid_at(NOW)
    assert not lease.is_valid_at(NOW + dt.timedelta(seconds=61))


def test_zero_fencing_token_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        _lease(fencing_token=0)


def test_empty_owner_token_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        owner_digest("")


# -- claim ve receipt ---------------------------------------------------------------------


def test_claim_digest_is_deterministic() -> None:
    claim = _claim()
    assert claim.claim_digest == claim.claim_digest


def test_claim_requires_operation_and_key() -> None:
    with pytest.raises(ValidationFailed):
        _claim(operation="  ")
    with pytest.raises(ValidationFailed):
        _claim(idempotency_key="")


def test_completed_receipt_requires_result_digest() -> None:
    claim = _claim()
    with pytest.raises(ValidationFailed):
        EffectReceipt(
            id=uuid4(),
            realm_id=REALM,
            claim_id=claim.id,
            status=ReceiptStatus.COMPLETED,
        )


def test_failed_receipt_requires_category() -> None:
    claim = _claim()
    with pytest.raises(ValidationFailed):
        EffectReceipt(id=uuid4(), realm_id=REALM, claim_id=claim.id, status=ReceiptStatus.FAILED)


def test_completed_receipt_cannot_carry_failure() -> None:
    claim = _claim()
    with pytest.raises(ValidationFailed):
        EffectReceipt(
            id=uuid4(),
            realm_id=REALM,
            claim_id=claim.id,
            status=ReceiptStatus.COMPLETED,
            result_digest=DIGEST,
            failure_category=FailureCategory.TIMEOUT,
        )


def test_failed_receipt_cannot_carry_result() -> None:
    claim = _claim()
    with pytest.raises(ValidationFailed):
        EffectReceipt(
            id=uuid4(),
            realm_id=REALM,
            claim_id=claim.id,
            status=ReceiptStatus.FAILED,
            failure_category=FailureCategory.ADAPTER,
            result_digest=DIGEST,
        )


def test_negative_measurements_are_rejected() -> None:
    claim = _claim()
    with pytest.raises(ValidationFailed):
        EffectReceipt.completed(realm_id=REALM, claim=claim, result_digest=DIGEST, token_count=-1)


def test_receipt_helpers_build_valid_records() -> None:
    claim = _claim()
    completed = EffectReceipt.completed(realm_id=REALM, claim=claim, result_digest=DIGEST)
    failed = EffectReceipt.failed(realm_id=REALM, claim=claim, category=FailureCategory.PROVIDER)
    assert completed.status is ReceiptStatus.COMPLETED
    assert failed.status is ReceiptStatus.FAILED
    assert completed.is_terminal and failed.is_terminal


# -- recovery ------------------------------------------------------------------------------


def test_no_claim_means_nothing_to_recover() -> None:
    assessment = assess_recovery(claim=None, receipt=None)
    assert assessment.outcome is RecoveryOutcome.NOTHING_TO_RECOVER
    assert assessment.silent_retry_allowed


def test_claim_without_receipt_is_recovery_required() -> None:
    assessment = assess_recovery(claim=_claim(), receipt=None)
    assert assessment.outcome is RecoveryOutcome.RECOVERY_REQUIRED
    assert not assessment.silent_retry_allowed
    assert "sessiz retry yasak" in assessment.next_safe_action.lower()


def test_claim_with_completed_receipt_is_reconciled() -> None:
    claim = _claim()
    receipt = EffectReceipt.completed(realm_id=REALM, claim=claim, result_digest=DIGEST)
    assessment = assess_recovery(claim=claim, receipt=receipt)
    assert assessment.outcome is RecoveryOutcome.RECONCILED_COMPLETED
    assert not assessment.silent_retry_allowed


def test_claim_with_failed_receipt_is_reconciled_failed() -> None:
    claim = _claim()
    receipt = EffectReceipt.failed(realm_id=REALM, claim=claim, category=FailureCategory.TIMEOUT)
    assert assess_recovery(claim=claim, receipt=receipt).outcome is (
        RecoveryOutcome.RECONCILED_FAILED
    )


def test_adapter_evidence_can_reconcile_a_missing_receipt() -> None:
    claim = _claim()
    completed = assess_recovery(claim=claim, receipt=None, adapter_evidence=ReceiptStatus.COMPLETED)
    failed = assess_recovery(claim=claim, receipt=None, adapter_evidence=ReceiptStatus.FAILED)
    assert completed.outcome is RecoveryOutcome.RECONCILED_COMPLETED
    assert failed.outcome is RecoveryOutcome.RECONCILED_FAILED
    assert not completed.silent_retry_allowed


def test_reconciled_completion_request_binds_every_exact_identity() -> None:
    request = ReconciledCompletionRequest(
        job_id=uuid4(),
        attempt_id=uuid4(),
        claim_id=uuid4(),
        fencing_token=7,
        claim_digest=DIGEST,
        effect_digest=DIGEST,
        authorization_digest=DIGEST,
        result_digest=DIGEST,
        adapter_evidence_digest=DIGEST,
    )

    assert request.as_dict()["fencing_token"] == 7
    assert request.as_dict()["adapter_evidence_digest"] == DIGEST


@pytest.mark.parametrize(
    ("field", "value"),
    (("fencing_token", 0), ("claim_digest", "not-a-digest")),
)
def test_reconciled_completion_request_rejects_unsafe_identity(field: str, value: object) -> None:
    values: dict[str, object] = {
        "job_id": uuid4(),
        "attempt_id": uuid4(),
        "claim_id": uuid4(),
        "fencing_token": 1,
        "claim_digest": DIGEST,
        "effect_digest": DIGEST,
        "authorization_digest": DIGEST,
        "result_digest": DIGEST,
        "adapter_evidence_digest": DIGEST,
    }
    values[field] = value
    with pytest.raises(ValidationFailed):
        ReconciledCompletionRequest(**values)  # type: ignore[arg-type]


def test_silent_retry_is_rejected_for_every_non_trivial_outcome() -> None:
    for assessment in (
        assess_recovery(claim=_claim(), receipt=None),
        assess_recovery(
            claim=_claim(),
            receipt=EffectReceipt.completed(realm_id=REALM, claim=_claim(), result_digest=DIGEST),
        ),
    ):
        with pytest.raises(PolicyViolation, match="Sessiz retry yasak"):
            assert_no_silent_retry(assessment)


def test_silent_retry_is_allowed_when_there_is_no_claim() -> None:
    assert_no_silent_retry(assess_recovery(claim=None, receipt=None))


def test_attempt_outcomes_are_closed_set() -> None:
    assert {item.value for item in AttemptOutcome} == {
        "succeeded",
        "failed",
        "abandoned",
        "recovery-required",
    }


def test_resource_request_roundtrips_through_claim() -> None:
    claim = _claim(resources=parse_requests(write=("path:zekam:b.py", "path:zekam:a.py")))
    assert [item.resource.text for item in claim.resources] == [
        "path:zekam:a.py",
        "path:zekam:b.py",
    ]
    assert all(item.mode is LockMode.WRITE for item in claim.resources)


def test_claim_is_not_proof_of_effect() -> None:
    """Sozlesme: claim yalnizca niyeti kanitlar."""
    claim = _claim()
    assert "receipt" not in claim.as_dict()
    assert assess_recovery(claim=claim, receipt=None).outcome is (RecoveryOutcome.RECOVERY_REQUIRED)


def test_unknown_resource_request_type_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        ResourceRequest.parse("bilinmeyen:zekam:a", LockMode.WRITE)
