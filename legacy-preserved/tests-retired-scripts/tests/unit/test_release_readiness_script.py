from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from scripts import surum_hazirligi


def _digest(index: int) -> str:
    return f"sha256:{index:064x}"


def _valid_evidence() -> dict[str, object]:
    return {
        "schema": "zekam-opencode-benchmark-campaign-acceptance/v2",
        "status": "verified",
        "campaign_key": "opencode-aihub",
        "outcome_status": "passed",
        "configured_model_count": 17,
        "canonical_target_count": 18,
        "audio_excluded_count": 1,
        "eligible_model_count": 17,
        "health_result_count": 17,
        "provider_call_budget": 102,
        "tested_call_budget": 85,
        "actual_tested_call_count": 85,
        "actual_provider_call_count": 102,
        "qualified_model_count": 17,
        "disqualified_model_count": 0,
        "calls": [
            {
                "call_id": f"health-{index}",
                "authorization_id": str(uuid4()),
                "claim_id": str(uuid4()),
                "receipt_id": str(uuid4()),
                "receipt_status": "completed",
                "response_digest": _digest(100 + index),
                "provider_evidence_digest": _digest(200 + index),
                "plan_digest": _digest(300 + index),
            }
            for index in range(102)
        ],
        "members": [
            {
                "model_id": f"model-{index}",
                "health_status": "passed",
                "benchmark_status": "passed",
                "repetitions": 5,
                "qualification": "qualified",
                "health_evidence_digest": _digest(400 + index),
                "result_evidence_digest": _digest(500 + index),
            }
            for index in range(17)
        ],
        "runtime": {
            "job_state": "completed",
            "attempt_outcome": "succeeded",
            "receiptless_claim_count": 0,
            "open_lease_count": 0,
            "open_resource_lock_count": 0,
            "checkpoint_complete": True,
            "job_id": str(uuid4()),
            "attempt_id": str(uuid4()),
            "campaign_claim_id": str(uuid4()),
            "campaign_receipt_id": str(uuid4()),
            "checkpoint_record_id": str(uuid4()),
            "checkpoint_key": "opencode-aihub-campaign-test",
        },
        "campaign_id": str(uuid4()),
        "outcome_id": str(uuid4()),
        "work_item_id": str(uuid4()),
        "task_plan_id": str(uuid4()),
        "source_revision": "source-revision",
        "source_digest": _digest(690),
        "catalog_digest": _digest(691),
        "endpoint_identity_digest": _digest(692),
        "inventory_digest": _digest(693),
        "policy_digest": _digest(694),
        "fixture_registry_digest": _digest(695),
        "verifier_provenance_digest": _digest(696),
        "outcome_evidence_digest": _digest(697),
        "campaign_digest": _digest(700),
        "outcome_digest": _digest(701),
        "qualification_set_digest": _digest(702),
        "runtime_evidence_digest": _digest(703),
        "verifier": {
            "verified": True,
            "identity": "release-readiness-canonical-db/v2",
            "tested_model_verifier_identity": "independent-verifier",
            "provenance_digest": _digest(696),
            "evidence_digest": _digest(704),
        },
    }


def _write(path: Path, evidence: dict[str, object]) -> None:
    path.write_text(json.dumps(evidence), encoding="utf-8")


def test_provider_acceptance_gate_requires_complete_exact_evidence(
    tmp_path: Path, monkeypatch: object
) -> None:
    evidence_path = tmp_path / "live.json"
    monkeypatch.setattr(surum_hazirligi, "PROVIDER_ACCEPTANCE_PATH", evidence_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(surum_hazirligi, "_canonical_provider_acceptance", lambda _: [])  # type: ignore[attr-defined]
    _write(evidence_path, _valid_evidence())

    assert surum_hazirligi.provider_acceptance_gate() == {"passed": True, "reasons": []}


def test_provider_acceptance_gate_rejects_reused_authority_and_member_drift(
    tmp_path: Path, monkeypatch: object
) -> None:
    evidence_path = tmp_path / "live.json"
    monkeypatch.setattr(surum_hazirligi, "PROVIDER_ACCEPTANCE_PATH", evidence_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(surum_hazirligi, "_canonical_provider_acceptance", lambda _: [])  # type: ignore[attr-defined]
    evidence = _valid_evidence()
    calls = evidence["calls"]
    members = evidence["members"]
    assert isinstance(calls, list) and isinstance(members, list)
    calls[1]["authorization_id"] = calls[0]["authorization_id"]
    members[0]["repetitions"] = 4
    _write(evidence_path, evidence)

    result = surum_hazirligi.provider_acceptance_gate()

    assert result["passed"] is False
    assert "distinct-authorization_id-required" in result["reasons"]
    assert "member-health-benchmark-state-invalid" in result["reasons"]


def test_provider_acceptance_gate_fails_closed_without_evidence(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        surum_hazirligi, "PROVIDER_ACCEPTANCE_PATH", tmp_path / "missing.json"
    )

    assert surum_hazirligi.provider_acceptance_gate() == {
        "passed": False,
        "reasons": ["opencode-aihub-campaign-evidence-missing"],
    }


def test_provider_acceptance_gate_rejects_non_object_json(
    tmp_path: Path, monkeypatch: object
) -> None:
    evidence_path = tmp_path / "live.json"
    monkeypatch.setattr(surum_hazirligi, "PROVIDER_ACCEPTANCE_PATH", evidence_path)  # type: ignore[attr-defined]
    evidence_path.write_text("[]", encoding="utf-8")

    assert surum_hazirligi.provider_acceptance_gate() == {
        "passed": False,
        "reasons": ["live-evidence-object-required"],
    }


def test_provider_acceptance_gate_rejects_shape_valid_but_noncanonical_evidence(
    tmp_path: Path, monkeypatch: object
) -> None:
    evidence_path = tmp_path / "live.json"
    monkeypatch.setattr(surum_hazirligi, "PROVIDER_ACCEPTANCE_PATH", evidence_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        surum_hazirligi,
        "_canonical_provider_acceptance",
        lambda _: ["canonical-campaign-outcome-missing"],
    )
    _write(evidence_path, _valid_evidence())

    result = surum_hazirligi.provider_acceptance_gate()

    assert result == {
        "passed": False,
        "reasons": ["canonical-campaign-outcome-missing"],
    }


def test_v3_canonical_gate_requires_exact_recomputed_projection(monkeypatch: object) -> None:
    campaign_id = uuid4()
    evidence = {
        "schema": "zekam-opencode-benchmark-campaign-acceptance/v3",
        "campaign_id": str(campaign_id),
        "parent_campaign_id": None,
        "status": "verified",
    }
    canonical = dict(evidence)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        surum_hazirligi,
        "_build_canonical_provider_acceptance_v3",
        lambda _: dict(canonical),
    )

    assert surum_hazirligi._canonical_provider_acceptance(evidence) == []
    evidence["status"] = "tampered"
    assert surum_hazirligi._canonical_provider_acceptance(evidence) == [
        "canonical-terminal-evidence-mismatch"
    ]


def test_v3_continuation_mismatch_keeps_specific_reason(monkeypatch: object) -> None:
    evidence = {
        "schema": "zekam-opencode-benchmark-campaign-acceptance/v3",
        "campaign_id": str(uuid4()),
        "parent_campaign_id": str(uuid4()),
        "status": "tampered",
    }
    canonical = {**evidence, "status": "verified"}
    monkeypatch.setattr(  # type: ignore[attr-defined]
        surum_hazirligi,
        "_build_canonical_provider_acceptance_v3",
        lambda _: canonical,
    )

    assert surum_hazirligi._canonical_provider_acceptance(evidence) == [
        "canonical-continuation-evidence-mismatch"
    ]
