from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from zekam.application.model_family_policy import (
    default_family_policy_file,
    load_model_family_policy,
    sanitized_family_policy,
)
from zekam.domain.errors import ValidationFailed


def test_default_family_policy_covers_exact_inventory_without_inference() -> None:
    policy = load_model_family_policy()
    report = sanitized_family_policy(policy)
    assert report["model_count"] == 20
    assert report["family_count"] == 10
    assert policy.same_family_allowed_risks == ("low", "medium")
    assert policy.family_for("2d13d348-ab24-4738-a19c-6b4be323f836") == "qwen"


def test_missing_model_family_binding_is_rejected(tmp_path: Path) -> None:
    document = yaml.safe_load(default_family_policy_file().read_text(encoding="utf-8"))
    document["models"].pop(next(iter(document["models"])))
    target = tmp_path / "family.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValidationFailed, match="exact kanonik model"):
        load_model_family_policy(target)


def test_duplicate_or_unknown_risk_is_rejected(tmp_path: Path) -> None:
    document = yaml.safe_load(default_family_policy_file().read_text(encoding="utf-8"))
    document["same_family_allowed_risks"] = ["low", "low", "unknown"]
    target = tmp_path / "family.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValidationFailed, match="risk policy"):
        load_model_family_policy(target)
