from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from tests.integration.test_local_evidence_routing import NOW, _activate, _request, _world

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite import local_evidence_routing as routing


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("policy-body", "policy body drift"),
        ("policy-candidates", "policy candidate identity drift"),
        ("candidate-missing", "candidate evidence incomplete"),
        ("candidate-body", "candidate body drift"),
        ("snapshot-body", "discovery snapshot body drift"),
        ("event-body", "reconcile event body drift"),
        ("health-body", "health body digest drift"),
        ("profile-body", "capability profile body drift"),
    ),
)
def test_current_epoch_rejects_each_stored_identity_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    router, bindings, snapshot = _world(tmp_path)
    policy = _activate(router, bindings)
    request = _request(router, policy, snapshot)

    if case == "policy-body":
        with sqlite3.connect(router.path) as db:
            db.execute("drop trigger policy_revision_no_update")
            db.execute(
                "update policy_revision set body_json=? where policy_digest=?",
                (canonical_json({"drift": True}), policy),
            )
            monkeypatch.setattr(routing, "LOCAL_ROUTING_SCHEMA_DIGEST", routing._schema_digest(db))
    elif case == "policy-candidates":
        body = {
            "schema": "zekam-local-route-policy/v2",
            "candidate_digests": [],
        }
        changed_policy = digest(body)
        with sqlite3.connect(router.path) as db:
            db.execute("drop trigger policy_revision_no_update")
            db.execute(
                "update policy_revision set policy_digest=?,body_json=? where policy_digest=?",
                (changed_policy, canonical_json(body), policy),
            )
            monkeypatch.setattr(routing, "LOCAL_ROUTING_SCHEMA_DIGEST", routing._schema_digest(db))
        request = replace(request, policy_digest=changed_policy)
    elif case in {"candidate-missing", "candidate-body"}:
        with sqlite3.connect(router.path) as db:
            if case == "candidate-missing":
                db.execute("drop trigger candidate_no_delete")
                db.execute(
                    "delete from candidate where candidate_digest=?",
                    (bindings[0].candidate_digest,),
                )
            else:
                db.execute("drop trigger candidate_no_update")
                db.execute(
                    "update candidate set body_json=? where candidate_digest=?",
                    (canonical_json({"drift": True}), bindings[0].candidate_digest),
                )
            monkeypatch.setattr(routing, "LOCAL_ROUTING_SCHEMA_DIGEST", routing._schema_digest(db))
    else:
        trigger, table, key_column, key_value = {
            "snapshot-body": (
                "discovery_snapshot_no_update",
                "discovery_snapshot",
                "snapshot_digest",
                bindings[0].snapshot_digest,
            ),
            "event-body": (
                "reconcile_event_no_update",
                "reconcile_event",
                "event_digest",
                None,
            ),
            "health-body": (
                "health_observation_no_update",
                "health_observation",
                "health_digest",
                bindings[0].health_digest,
            ),
            "profile-body": (
                "local_profile_report_no_update",
                "local_profile_report",
                "report_digest",
                None,
            ),
        }[case]
        with sqlite3.connect(router.registry_path) as db:
            db.execute(f"drop trigger {trigger}")
            if case == "profile-body":
                row = db.execute(
                    "select report_digest,body_json from local_profile_report "
                    "where json_extract(body_json,'$.exact_id')=? limit 1",
                    (bindings[0].exact_id,),
                ).fetchone()
                assert row is not None
                changed_profile = routing._document(row[1])
                changed_profile["family_id"] = "corrupted-family"
                db.execute(
                    "update local_profile_report set body_json=? where report_digest=?",
                    (canonical_json(changed_profile), row[0]),
                )
            else:
                if key_value is None:
                    key_value = db.execute(f"select {key_column} from {table} limit 1").fetchone()[
                        0
                    ]
                db.execute(
                    f"update {table} set body_json=? where {key_column}=?",
                    (canonical_json({"drift": True}), key_value),
                )
            monkeypatch.setattr(routing, "REGISTRY_SCHEMA_DIGEST", routing._schema_digest(db))

    with pytest.raises(PolicyViolation, match=message):
        router.current_evidence_epoch_digest(request)
    if case == "candidate-missing":
        with pytest.raises(PolicyViolation, match="candidate evidence incomplete"):
            router.policy_activation_spec(
                1,
                tuple(sorted(item.candidate_digest for item in bindings)),
                "independent-reviewer",
            )


def test_current_epoch_validation_covers_wrong_request_and_timestamp_parse(
    tmp_path: Path,
) -> None:
    router, _, _ = _world(tmp_path)
    with pytest.raises(ValidationFailed, match="Exact local route request"):
        router.current_evidence_epoch_digest(object())  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation, match="timestamp drift"):
        routing._parse_instant("not-an-instant")


def test_candidate_and_policy_stage_replay_drift_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, bindings, _ = _world(tmp_path)
    binding = bindings[0]
    original_schema_digest = routing.LOCAL_ROUTING_SCHEMA_DIGEST
    with sqlite3.connect(router.path) as db:
        db.execute("drop trigger candidate_no_update")
        db.execute(
            "update candidate set body_json=? where candidate_digest=?",
            (canonical_json({"drift": True}), binding.candidate_digest),
        )
        monkeypatch.setattr(routing, "LOCAL_ROUTING_SCHEMA_DIGEST", routing._schema_digest(db))
    with pytest.raises(ConcurrencyConflict, match="candidate replay drift"):
        router.register_candidate(binding, device_id="mac-device", client_id="opencode")
    monkeypatch.setattr(routing, "LOCAL_ROUTING_SCHEMA_DIGEST", original_schema_digest)

    stage_root = tmp_path / "stage"
    stage_root.mkdir()
    other, other_bindings, _ = _world(stage_root)
    candidates = tuple(sorted(item.candidate_digest for item in other_bindings))
    evidence = digest("offline")
    other.record_policy_stage(1, candidates, "offline-replay", evidence, "offline-runner")
    with sqlite3.connect(other.path) as db:
        db.execute("drop trigger policy_stage_no_update")
        db.execute(
            "update policy_stage set body_json=? where revision=1 and ordinal=1",
            (canonical_json({"drift": True}),),
        )
        monkeypatch.setattr(routing, "LOCAL_ROUTING_SCHEMA_DIGEST", routing._schema_digest(db))
    with pytest.raises(ConcurrencyConflict, match="policy stage replay drift"):
        other.record_policy_stage(1, candidates, "offline-replay", evidence, "offline-runner")


@pytest.mark.parametrize("case", ("missing", "body-drift"))
def test_decide_rechecks_active_policy_after_epoch_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    router, bindings, snapshot = _world(tmp_path)
    if case == "missing":
        policy = digest("missing-policy")
        request = _request(router, policy, snapshot)
        message = "active policy missing"
    else:
        policy = _activate(router, bindings)
        request = _request(router, policy, snapshot)
        with sqlite3.connect(router.path) as db:
            db.execute("drop trigger policy_revision_no_update")
            db.execute(
                "update policy_revision set body_json=? where policy_digest=?",
                (canonical_json({"drift": True}), policy),
            )
            monkeypatch.setattr(routing, "LOCAL_ROUTING_SCHEMA_DIGEST", routing._schema_digest(db))
        message = "policy body drift"
    monkeypatch.setattr(
        routing.SQLiteLocalEvidenceRouter,
        "current_evidence_epoch_digest",
        lambda _self, value: value.evidence_epoch_digest,
    )
    with pytest.raises(PolicyViolation, match=message):
        router.decide(request, now=NOW)
