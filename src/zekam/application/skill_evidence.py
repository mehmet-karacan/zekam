"""Strict, bounded receipt-consumption documents; never mint execution evidence."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from zekam.application.bounded_file import read_bounded_text
from zekam.application.knowledge_file_plane import assert_public_safe_projection
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.personal_skill import SkillEvaluationV2


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise PolicyViolation("Skill evidence duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise PolicyViolation("Skill evidence non-finite JSON number")


def load_skill_evidence(path: Path, *, kind: str) -> dict[str, Any]:
    """Read a public-safe, exact typed input without DB or provider effects."""
    if kind not in {"evaluation", "review"}:
        raise PolicyViolation("Skill evidence kind invalid")
    raw = read_bounded_text(path, max_bytes=32_768)
    try:
        body = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError) as exc:
        raise PolicyViolation("Skill evidence JSON malformed") from exc
    if not isinstance(body, dict):
        raise PolicyViolation("Skill evidence public-safe object required")
    # Scan decoded values too: JSON unicode escapes cannot hide PII or secrets.
    assert_public_safe_projection(
        canonical_json(body).encode("utf-8"), relative_path="skill-evidence.json"
    )
    if kind == "evaluation":
        keys = {item.name for item in fields(SkillEvaluationV2)}
        if set(body) != keys | {"schema", "activatable", "grants_authority"}:
            raise PolicyViolation("Skill evaluation exact fields required")
        for key in keys - {"trials", "metrics", "uncertainty"}:
            if type(body[key]) is not str:
                raise PolicyViolation("Skill evaluation exact text fields required")
        if (
            type(body["trials"]) is not int
            or type(body["metrics"]) is not dict
            or type(body["uncertainty"]) is not dict
        ):
            raise PolicyViolation("Skill evaluation exact trial/metric types required")
        evaluation = SkillEvaluationV2(**{key: body[key] for key in keys})
        expected = evaluation.body()
    else:
        keys = {
            "schema",
            "revision_digest",
            "evaluation_digest",
            "reviewer_ref",
            "reviewer_execution_identity",
            "reviewer_evidence_digest",
            "approved",
            "reason",
            "grants_authority",
        }
        if set(body) != keys:
            raise PolicyViolation("Skill review exact fields required")
        for key in ("revision_digest", "evaluation_digest", "reviewer_evidence_digest"):
            if type(body[key]) is not str:
                raise PolicyViolation("Skill review digest type invalid")
            parse_digest(body[key])
        for key, limit in (
            ("reviewer_ref", 512),
            ("reviewer_execution_identity", 512),
            ("reason", 2048),
        ):
            if (
                type(body[key]) is not str
                or not body[key].strip()
                or len(body[key].encode("utf-8")) > limit
            ):
                raise PolicyViolation("Skill review bounded text required")
        if type(body["approved"]) is not bool:
            raise PolicyViolation("Skill review exact approval bool required")
        expected = body | {
            "schema": "zekam-personal-skill-review-input/v2",
            "grants_authority": False,
        }
    # Canonical comparison distinguishes true/1 and false/0, unlike dict equality.
    if canonical_json(body) != canonical_json(expected):
        raise PolicyViolation("Skill evidence schema/derived field drift")
    return body


def skill_evidence_record_plan(
    body: dict[str, Any], *, kind: str, project_ref: str
) -> dict[str, Any]:
    if kind not in {"evaluation", "review"}:
        raise PolicyViolation("Skill evidence kind invalid")
    effect = {"operation": f"skill.{kind}.record-v2", "document": body, "project_ref": project_ref}
    plan = {
        "schema": "zekam-skill-evidence-record-plan/v2",
        "effect": effect,
        "effect_digest": digest(effect),
        "provider_calls": 0,
        "grants_authority": False,
    }
    return plan | {"plan_digest": digest(plan)}
