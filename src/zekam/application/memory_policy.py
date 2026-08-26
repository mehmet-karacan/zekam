"""Strict loader for the reviewed Memory Continuity privacy policy."""

from __future__ import annotations

from pathlib import Path

import yaml

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory_policy import (
    MEMORY_POLICY_SCHEMA,
    ClassificationPolicy,
    HumanReviewLevel,
    MemoryContinuityMode,
    MemoryContinuityPolicy,
    MemoryRetentionClass,
    MemoryStorageClass,
)
from zekam.domain.session_continuity import DataClassification

_CLASSIFICATION_FIELDS = {
    "storage_class",
    "retention_class",
    "retention_days",
    "projection_eligible",
    "remote_model_eligible",
    "cross_project_access",
    "compiler_eligible",
    "human_review",
}


def default_memory_policy_file() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "memory_continuity_policy.yaml"


def load_memory_policy(path: Path | None = None) -> MemoryContinuityPolicy:
    candidate = path or default_memory_policy_file()
    if candidate.is_symlink():
        raise PolicyViolation("Memory continuity policy symlink olamaz")
    target = candidate.resolve(strict=True)
    if not target.is_file() or target.stat().st_size > 64 * 1024:
        raise PolicyViolation("Memory continuity policy guvenli regular file olmali")
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "revision",
        "initial_mode",
        "remote_calls_default",
        "classifications",
    }:
        raise ValidationFailed("Memory continuity policy exact shape ister")
    raw_classifications = document["classifications"]
    if (
        document["schema"] != MEMORY_POLICY_SCHEMA
        or not isinstance(raw_classifications, dict)
        or set(raw_classifications) != {item.value for item in DataClassification}
    ):
        raise ValidationFailed("Memory continuity policy classification seti gecersiz")
    classifications: list[ClassificationPolicy] = []
    for classification in DataClassification:
        raw = raw_classifications[classification.value]
        if not isinstance(raw, dict) or set(raw) != _CLASSIFICATION_FIELDS:
            raise ValidationFailed("Memory classification policy exact shape ister")
        try:
            retention_days = raw["retention_days"]
            classifications.append(
                ClassificationPolicy(
                    classification=classification,
                    storage_class=MemoryStorageClass(str(raw["storage_class"])),
                    retention_class=MemoryRetentionClass(str(raw["retention_class"])),
                    retention_days=(None if retention_days is None else int(retention_days)),
                    projection_eligible=raw["projection_eligible"] is True,
                    remote_model_eligible=raw["remote_model_eligible"] is True,
                    cross_project_access=raw["cross_project_access"] is True,
                    compiler_eligible=raw["compiler_eligible"] is True,
                    human_review=HumanReviewLevel(str(raw["human_review"])),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValidationFailed("Memory classification policy degeri gecersiz") from exc
    try:
        revision = int(document["revision"])
        initial_mode = MemoryContinuityMode(str(document["initial_mode"]))
    except (TypeError, ValueError) as exc:
        raise ValidationFailed("Memory continuity policy version/mode gecersiz") from exc
    return MemoryContinuityPolicy(
        revision=revision,
        initial_mode=initial_mode,
        remote_calls_default=document["remote_calls_default"] is True,
        classifications=tuple(classifications),
    )
