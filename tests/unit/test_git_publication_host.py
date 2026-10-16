"""Native host source validator composition; upstream readers remain fixture boundaries."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.unit.test_git_publication import world as world
from zekam.application import git_publication_host as subject
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation


@pytest.fixture
def host_world(world, monkeypatch, tmp_path):
    source = {
        "job_id": str(uuid4()),
        "source_evidence_digest": digest("admission"),
        "bundle": {"plan": world.plan.body(), "verification_producer_job_id": str(uuid4())},
    }
    review = {
        "subject_digest": world.plan.subject_digest,
        "target": world.plan.target.body(),
        "test_job_id": str(uuid4()),
        "test_source_evidence_digest": digest("test-source"),
        "test_execution_id": str(world.test.execution_id),
        "execution_id": str(world.review.execution_id),
        "test_request_digest": digest("test-request"),
        "test_measurement_digest": digest("measurement"),
        "test_source_report_digest": digest("test-report"),
    }
    proof = {
        "schema": "zekam-publication-test-source/v1",
        "subject_digest": world.plan.subject_digest,
        "publication_binding_digest": world.plan.binding.binding_digest,
        "target": world.plan.target.body(),
        "source_job_id": review["test_job_id"],
        "source_evidence_digest": review["test_source_evidence_digest"],
        "execution_id": review["test_execution_id"],
        "request_digest": review["test_request_digest"],
        "measurement_record_digest": review["test_measurement_digest"],
        "source_report_digest": review["test_source_report_digest"],
        "current_source_verified": True,
        "raw_reports_verified": True,
        "grants_authority": False,
    }
    proof["report_digest"] = digest(proof)
    reads, drift = [], [False]
    monkeypatch.setattr(subject, "read_publication_admission", lambda *_args: deepcopy(source))
    monkeypatch.setattr(subject, "_ledger", lambda *_args: (None, world.ledger))
    monkeypatch.setattr(subject, "resolve_project_source", lambda *_args: tmp_path)
    monkeypatch.setattr(subject, "BoundGitPushTransport", lambda _root: world.transport)
    monkeypatch.setattr(
        subject,
        "read_verifier_producer_result",
        lambda *_args: {"envelope": {"source_job_id": str(uuid4())}},
    )
    monkeypatch.setattr(subject, "read_publication_review", lambda *_args: review)

    def current(*_args, **_kwargs):
        reads.append(True)
        if drift[0]:
            raise PolicyViolation("fixture ignored test tree/report drift")
        return proof

    monkeypatch.setattr(subject, "read_publication_test_source", current)
    return SimpleNamespace(world=world, source=source, proof=proof, drift=drift, reads=reads)


def apply(w, **kwargs):
    return subject.apply_admitted_publication(
        w.world.home,
        reference=w.source["job_id"],
        authorization_id=w.world.ledger.authorization.id,
        plan_digest=kwargs.get("plan_digest", w.world.plan.plan_digest),
    )


def test_native_host_uses_actual_plan_approval_and_current_sources(host_world):
    w = host_world
    assert subject.publication_from_admission(w.world.home, w.source["job_id"]) == w.world.plan
    assert not w.reads and w.world.transport.calls == 0
    result = apply(w)
    assert result["remote_oid"] == w.world.plan.target.head
    assert w.reads and w.world.transport.calls == 1
    before = len(w.reads)
    w.drift[0] = True
    assert apply(w)["canonical_replayed"] is True
    assert len(w.reads) == before and w.world.transport.calls == 1


def test_native_host_wrong_plan_or_stale_source_cannot_consume_or_push(host_world):
    w = host_world
    with pytest.raises(PolicyViolation, match="plan digest"):
        apply(w, plan_digest=digest("different-plan"))
    w.drift[0] = True
    with pytest.raises(PolicyViolation, match="ignored test tree/report"):
        apply(w)
    assert w.world.transport.calls == 0 and w.world.ledger.consumes == 0


def test_native_source_is_rechecked_after_authorization_consumption(host_world, monkeypatch):
    w = host_world
    original = w.world.ledger.consume

    def consume(*args, **kwargs):
        result = original(*args, **kwargs)
        w.drift[0] = True
        return result

    monkeypatch.setattr(w.world.ledger, "consume", consume)
    with pytest.raises(PolicyViolation, match="ignored test tree/report"):
        apply(w)
    assert w.world.ledger.consumes == 1 and w.world.transport.calls == 0
    assert w.world.runtime.status().recovery_jobs == 1


def test_native_receipt_change_cannot_be_hidden_during_effect(host_world, monkeypatch):
    w = host_world
    original = w.world.ledger.consume

    def consume(*args, **kwargs):
        result = original(*args, **kwargs)
        w.source["source_evidence_digest"] = digest("substituted-admission")
        return result

    monkeypatch.setattr(w.world.ledger, "consume", consume)
    with pytest.raises(PolicyViolation, match="admission/plan changed"):
        apply(w)
    assert w.world.transport.calls == 0


def test_post_push_source_drift_leaves_unknown_receipt_without_second_push(host_world, monkeypatch):
    w = host_world
    original = w.world.transport.push

    def push(*args, **kwargs):
        result = original(*args, **kwargs)
        w.drift[0] = True
        return result

    monkeypatch.setattr(w.world.transport, "push", push)
    with pytest.raises(PolicyViolation, match="ignored test tree/report"):
        apply(w)
    assert w.world.transport.calls == 1 and w.world.runtime.status().recovery_jobs == 1


def test_service_completed_replay_never_invokes_new_source_validator(world):
    first = world.service.apply(world.plan, authorization_id=world.ledger.authorization.id)

    def forbidden(_plan):
        pytest.fail("completed replay revalidated current source")

    service = replace(world.service, validate_evidence=forbidden)
    result = service.apply(world.plan, authorization_id=world.ledger.authorization.id)
    assert result["canonical_replayed"] is True
    assert result["canonical_job_id"] == first["canonical_job_id"] and world.transport.calls == 1


def test_native_cli_preview_needs_no_approval_and_apply_cannot_mint_one(host_world):
    import json
    from typer.testing import CliRunner
    from zekam.interfaces.cli.git_publication import app

    w = host_world
    args = [
        "apply-admission",
        w.source["job_id"],
        "--plan-digest",
        w.world.plan.plan_digest,
        "--home",
        str(w.world.home),
    ]
    runner = CliRunner()
    preview = runner.invoke(app, args)
    assert preview.exit_code == 0
    assert json.loads(preview.output)["publication_authorized"] is False
    assert not w.reads and w.world.transport.calls == 0
    missing_approval = runner.invoke(app, args + ["--uygula"])
    assert missing_approval.exit_code != 0
    assert w.world.transport.calls == 0 and w.world.ledger.consumes == 0
