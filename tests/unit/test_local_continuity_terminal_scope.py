"""Independent close-boundary regression checks using only disposable fixtures."""

from __future__ import annotations

import sqlite3
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity import NOW
from tests.unit.test_local_continuity import continuity as continuity
from tests.unit.test_local_continuity_close import OWNER, _drain_runtime, _freeze
from tests.unit.test_local_continuity_close import close as close

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


def _complete(close: dict[str, Any], request: Any) -> None:
    close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    close["service"].deliver_once(close["binding"], request.request_digest, **OWNER)
    _drain_runtime(close)
    close["service"].finalize(close["binding"], request.request_digest)
    assert close["store"].load(close["binding"], request.request_digest).state == "complete"


@pytest.mark.parametrize("terminal", [False, True], ids=["closing", "closed"])
@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"operation": "test-effect"},
        {"operation": "continuity.compile"},
        {
            "operation": "continuity.compile",
            "purpose": "repair-generated-candidates",
            "request_digest": digest("unbound-close"),
            "original_job_id": "unbound-original-job",
        },
    ],
    ids=["missing-operation", "ordinary", "compile-label", "foreign-repair"],
)
def test_closed_or_closing_arbitrary_job_admission_is_atomic(
    close: dict[str, Any], terminal: bool, extra: dict[str, str]
) -> None:
    request = _freeze(close)
    if terminal:
        _complete(close, request)
    binding = close["binding"]
    payload = {
        "session_id": binding.session_id,
        "binding_digest": binding.binding_digest,
        "run_id": binding.run_id,
        **extra,
    }
    with sqlite3.connect(close["base"].path) as db:
        before = (
            db.execute("select count(*) from local_job").fetchone()[0],
            db.execute("select count(*) from local_outbox").fetchone()[0],
        )
    with pytest.raises((sqlite3.IntegrityError, PolicyViolation, ValidationFailed)):
        close["runtime"].enqueue(idempotency_key="rejected-adapter-job", payload=payload)
    with sqlite3.connect(close["base"].path) as db:
        db.execute("pragma foreign_keys=on")
        with pytest.raises(sqlite3.IntegrityError, match="job admission frozen"):
            db.execute(
                "insert into local_job(id,idempotency_key,payload_json,state,attempt_count,"
                "max_attempts,available_at,created_at,updated_at)"
                " values(?,?,?,'ready',0,1,?,?,?)",
                (
                    str(uuid4()),
                    "rejected-raw-job",
                    canonical_json(payload),
                    NOW.isoformat(),
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
        after = (
            db.execute("select count(*) from local_job").fetchone()[0],
            db.execute("select count(*) from local_outbox").fetchone()[0],
        )
        assert after == before


@pytest.mark.parametrize("terminal", [False, True], ids=["closing", "closed"])
def test_prior_terminal_effect_cannot_attach_or_create_execution_after_freeze(
    close: dict[str, Any], terminal: bool
) -> None:
    binding = close["binding"]
    old_job, _ = close["runtime"].enqueue(
        idempotency_key="completed-before-close",
        payload={
            "session_id": binding.session_id,
            "binding_digest": binding.binding_digest,
            "run_id": binding.run_id,
        },
    )
    work = close["runtime"].claim_next(**OWNER, lease_seconds=30)
    assert work is not None
    assert work.job.id == old_job.id
    claim, _ = close["runtime"].claim_effect(
        work,
        operation="test-effect",
        effect_digest=digest("prior-effect"),
        idempotency_key="prior-effect",
    )
    close["runtime"].record_receipt(
        claim, status="completed", evidence_digest=digest("prior-completed")
    )
    close["runtime"].finish(work, state="completed", evidence_digest=digest("prior-completed"))
    _drain_runtime(close)
    request = _freeze(close)
    if terminal:
        _complete(close, request)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        close["base"].bind_effect(binding, claim.id)
    with sqlite3.connect(close["base"].path) as db:
        db.execute("pragma foreign_keys=on")
        with pytest.raises(sqlite3.IntegrityError, match="exact effect scope"):
            db.execute(
                "insert into continuity_effect_binding values(?,?,?,?)",
                (claim.id, binding.session_id, old_job.id, digest("raw-stale-binding")),
            )
        with pytest.raises(sqlite3.IntegrityError, match="execution admission frozen"):
            db.execute(
                "insert into local_lease values(?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    old_job.id,
                    "raw-worker",
                    42,
                    "raw-incarnation",
                    2,
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="execution admission frozen"):
            db.execute(
                "insert into local_effect_claim values(?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    old_job.id,
                    work.lease.id,
                    work.lease.fencing_token,
                    "test-effect",
                    digest("raw-stale-effect"),
                    "raw-stale-effect",
                    NOW.isoformat(),
                ),
            )
        assert (
            db.execute(
                "select count(*) from continuity_effect_binding where claim_id=?", (claim.id,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("running", [False, True], ids=["ready", "running"])
def test_effectless_preexisting_session_job_blocks_freeze(
    close: dict[str, Any], running: bool
) -> None:
    binding = close["binding"]
    job, _ = close["runtime"].enqueue(
        idempotency_key="preexisting-session-job",
        payload={
            "session_id": binding.session_id,
            "binding_digest": binding.binding_digest,
            "run_id": binding.run_id,
        },
    )
    if running:
        work = close["runtime"].claim_next(**OWNER, lease_seconds=30)
        assert work is not None and work.job.id == job.id
    _drain_runtime(close)
    with pytest.raises(PolicyViolation, match="pending"):
        _freeze(close)
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from local_effect_claim").fetchone()[0] == 0
        assert db.execute("select count(*) from continuity_close_request").fetchone()[0] == 0
        assert db.execute("select status from session").fetchone()[0] == "open"


def test_genuine_close_effect_replay_does_not_create_new_terminal_authority(
    close: dict[str, Any],
) -> None:
    request = _freeze(close)
    _complete(close, request)
    with sqlite3.connect(close["base"].path) as db:
        claim_id = db.execute(
            "select id from local_effect_claim where job_id=?", (request.job_id,)
        ).fetchone()[0]
        before = db.execute("select count(*) from continuity_effect_binding").fetchone()[0]
    close["base"].bind_effect(close["binding"], claim_id)
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from continuity_effect_binding").fetchone()[0] == before


def test_genuine_explicit_repair_remains_admitted_while_closing(
    close: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _freeze(close)
    verifier = close["service"].verify_projection

    def interrupted_verification(manifest: Any, payload: bytes) -> None:
        raise OSError("independent partial-close interruption")

    monkeypatch.setattr(close["service"], "verify_projection", interrupted_verification)
    with pytest.raises(OSError, match="partial-close"):
        close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    monkeypatch.setattr(close["service"], "verify_projection", verifier)
    close["service"].repair_generated_candidates(
        close["binding"], request.request_digest, repair_key="independent-approved-repair", **OWNER
    )
    _complete(close, request)
