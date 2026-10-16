from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application.skill_evidence import load_skill_evidence, skill_evidence_record_plan
from zekam.domain.canonical import digest
from zekam.domain.errors import ZekamError
from zekam.domain.personal_skill import SkillEvaluationV2


def _evaluation() -> dict[str, object]:
    return SkillEvaluationV2(
        digest("revision"),
        digest("plan"),
        "equal",
        5,
        "evaluator",
        "verifier",
        digest("observed-a"),
        digest("observed-b"),
        "execution-a",
        "execution-b",
        {"quality": {"baseline": 1.0, "candidate": 1.0}},
        {"sample_size": 5},
    ).body()


@pytest.mark.parametrize(
    "change",
    [
        {"trials": True},
        {"activatable": 0},
        {"grants_authority": 0},
        {"extra": "ignored"},
        {"state": "improved"},
        {"uncertainty": {"sample_size": 5, "deviation": float("inf")}},
        {"evaluator_ref": "person@example.com"},
    ],
)
def test_evidence_rejects_drift_and_sensitive_fields(tmp_path: Path, change: dict) -> None:
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(_evaluation() | change), encoding="utf-8")
    with pytest.raises((ZekamError, TypeError)):
        load_skill_evidence(path, kind="evaluation")


def test_evidence_strict_duplicate_size_and_scope_binding(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(_evaluation()), encoding="utf-8")
    body = load_skill_evidence(path, kind="evaluation")
    first = skill_evidence_record_plan(body, kind="evaluation", project_ref="one")
    second = skill_evidence_record_plan(body, kind="evaluation", project_ref="two")
    assert first["plan_digest"] != second["plan_digest"]
    assert first["provider_calls"] == 0
    path.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")
    with pytest.raises(ZekamError, match="duplicate"):
        load_skill_evidence(path, kind="evaluation")
    path.write_bytes(b" " * 32769)
    with pytest.raises(OSError):
        load_skill_evidence(path, kind="evaluation")


@pytest.mark.parametrize("approved", [0, 1, "true", None])
def test_review_requires_exact_boolean(tmp_path: Path, approved: object) -> None:
    body = {
        "schema": "zekam-personal-skill-review-input/v2",
        "revision_digest": digest("revision"),
        "evaluation_digest": digest("evaluation"),
        "reviewer_ref": "reviewer",
        "reviewer_execution_identity": "execution-review",
        "reviewer_evidence_digest": digest("review-evidence"),
        "approved": approved,
        "reason": "Measured review",
        "grants_authority": False,
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ZekamError, match="bool"):
        load_skill_evidence(path, kind="review")
