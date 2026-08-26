from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from zekam.application.memory_policy import default_memory_policy_file, load_memory_policy
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory_policy import (
    ClassificationPolicy,
    HumanReviewLevel,
    MemoryContinuityMode,
    MemoryRetentionClass,
    MemoryStorageClass,
)
from zekam.domain.session_continuity import DataClassification


def test_default_memory_policy_is_shadow_exact_and_authority_free() -> None:
    policy = load_memory_policy()
    assert policy.initial_mode is MemoryContinuityMode.SHADOW
    assert policy.remote_calls_default is False
    assert len(policy.classifications) == len(DataClassification) == 10
    assert policy.as_dict()["grants_authority"] is False


def test_only_public_is_projection_eligible_and_secret_is_denied() -> None:
    policy = load_memory_policy()
    for classification in DataClassification:
        item = policy.policy_for(classification)
        assert item.projection_eligible is (classification is DataClassification.PUBLIC)
    secret = policy.policy_for(DataClassification.SECRET)
    assert secret.storage_class is MemoryStorageClass.DENIED
    assert secret.human_review is HumanReviewLevel.CRITICAL
    with pytest.raises(PolicyViolation, match="compiler"):
        policy.assert_compiler_eligible(DataClassification.SECRET)


def test_raw_transcript_is_local_candidate_only_and_never_remote() -> None:
    policy = load_memory_policy()
    raw = policy.policy_for(DataClassification.RAW_TRANSCRIPT)
    assert raw.storage_class is MemoryStorageClass.LOCAL_CAS
    assert raw.compiler_eligible is True
    with pytest.raises(PolicyViolation, match="remote model"):
        policy.assert_remote_model_eligible(DataClassification.RAW_TRANSCRIPT)


def test_confidential_classification_cannot_be_remote_eligible() -> None:
    with pytest.raises(PolicyViolation, match="remote model"):
        ClassificationPolicy(
            classification=DataClassification.CONFIDENTIAL,
            storage_class=MemoryStorageClass.POSTGRESQL_RLS,
            retention_class=MemoryRetentionClass.BOUNDED,
            retention_days=180,
            projection_eligible=False,
            remote_model_eligible=True,
            cross_project_access=False,
            compiler_eligible=True,
            human_review=HumanReviewLevel.REQUIRED,
        )


def test_missing_classification_or_unknown_field_is_rejected(tmp_path: Path) -> None:
    document = yaml.safe_load(default_memory_policy_file().read_text(encoding="utf-8"))
    document["classifications"].pop("pii")
    document["unexpected"] = True
    target = tmp_path / "memory-policy.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValidationFailed, match="exact shape"):
        load_memory_policy(target)
