"""Reviewed model-family policy; family values are never inferred at route time."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from zekam.application.model_registry import load_inventory
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_routing import ModelFamilyPolicy

POLICY_SCHEMA = "zekam-model-family-policy/v1"


def default_family_policy_file() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "model_family_policy.yaml"


def load_model_family_policy(path: Path | None = None) -> ModelFamilyPolicy:
    candidate = path or default_family_policy_file()
    if candidate.is_symlink():
        raise PolicyViolation("Model family policy symlink olamaz")
    target = candidate.resolve(strict=True)
    if not target.is_file() or target.stat().st_size > 64 * 1024:
        raise PolicyViolation("Model family policy guvenli regular file olmali")
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "same_family_allowed_risks",
        "models",
    }:
        raise ValidationFailed("Model family policy exact shape ister")
    risks = document["same_family_allowed_risks"]
    models = document["models"]
    if (
        document["schema"] != POLICY_SCHEMA
        or not isinstance(risks, list)
        or not isinstance(models, dict)
    ):
        raise ValidationFailed("Model family policy schema gecersiz")
    inventory_ids = {item.model_id for item in load_inventory().records}
    if set(models) != inventory_ids:
        raise ValidationFailed("Model family policy exact kanonik model setini ister")
    return ModelFamilyPolicy(
        model_families=tuple(sorted((str(key), str(value)) for key, value in models.items())),
        same_family_allowed_risks=tuple(str(value) for value in risks),
    )


def sanitized_family_policy(policy: ModelFamilyPolicy) -> dict[str, Any]:
    return {
        "schema": POLICY_SCHEMA,
        "model_count": len(policy.model_families),
        "family_count": len({family for _, family in policy.model_families}),
        "same_family_allowed_risks": list(policy.same_family_allowed_risks),
        "policy_digest": policy.policy_digest,
    }
