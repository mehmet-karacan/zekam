from __future__ import annotations

from dataclasses import replace

import pytest

from zekam.application.continuity_projection import (
    ClassifiedProjectionRecord,
    HydrationCategory,
    HydrationItem,
    ProjectionAudience,
    build_continuity_projection_recipe,
    build_hydration_recipe,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.markdown_projection import ProjectionRecord, ProjectionSourceRef
from zekam.domain.session_continuity import DataClassification, DigestReference, TruthClass


def _record(entity_id: str, summary: str = "safe summary") -> ProjectionRecord:
    source = ProjectionSourceRef("work", entity_id, "rev-1", digest(entity_id))
    return ProjectionRecord(
        entity_type="work",
        entity_id=entity_id,
        title=f"Work {entity_id}",
        status="active",
        summary=summary,
        source_refs=(source,),
    )


def _hydration(item_id: str, category: HydrationCategory, tokens: int) -> HydrationItem:
    return HydrationItem(
        item_id=item_id,
        category=category,
        content_ref=f"cas:{item_id}",
        source=DigestReference(f"context:{item_id}", digest(item_id), TruthClass.REPO_FACT),
        classification=DataClassification.INTERNAL,
        token_cost=tokens,
    )


def test_projection_excludes_private_records_and_is_repeatable() -> None:
    records = (
        ClassifiedProjectionRecord(
            _record("private", "private material"), DataClassification.LOCAL_ONLY
        ),
        ClassifiedProjectionRecord(_record("public"), DataClassification.PUBLIC),
    )
    arguments = {
        "source_head": "abc123",
        "migration_head": "55",
        "database_revision_digest": digest("db"),
        "expected_source_head": "abc123",
        "expected_migration_head": "55",
        "expected_database_revision_digest": digest("db"),
    }
    first = build_continuity_projection_recipe("zekam", records, **arguments)
    second = build_continuity_projection_recipe("zekam", tuple(reversed(records)), **arguments)

    assert first.bundle.projection_digest == second.bundle.projection_digest
    assert first.receipt.receipt_digest == second.receipt.receipt_digest
    assert first.receipt.fresh is True
    assert first.receipt.excluded_by_classification == 1
    rendered = first.bundle.files[0].payload.decode()
    assert "private material" not in rendered
    assert first.receipt.as_dict()["read_only"] is True
    assert first.receipt.as_dict()["grants_authority"] is False


def test_projection_freshness_is_exact_across_head_migration_and_db_revision() -> None:
    recipe = build_continuity_projection_recipe(
        "zekam",
        (ClassifiedProjectionRecord(_record("one"), DataClassification.INTERNAL),),
        source_head="old",
        migration_head="55",
        database_revision_digest=digest("db"),
        expected_source_head="new",
        expected_migration_head="55",
        expected_database_revision_digest=digest("db"),
        audience=ProjectionAudience.LOCAL_INTERNAL,
    )
    assert recipe.receipt.fresh is False


def test_projection_content_policy_excludes_misclassified_sensitive_summary() -> None:
    records = (
        ClassifiedProjectionRecord(_record("safe"), DataClassification.PUBLIC),
        ClassifiedProjectionRecord(
            _record("leak", "api_key=TOPSECRET123456"),
            DataClassification.PUBLIC,
        ),
    )
    recipe = build_continuity_projection_recipe(
        "zekam",
        records,
        source_head="abc",
        migration_head="55",
        database_revision_digest=digest("db"),
        expected_source_head="abc",
        expected_migration_head="55",
        expected_database_revision_digest=digest("db"),
    )
    assert recipe.receipt.record_count == 1
    assert recipe.exclusions[0].reason_code == "content-policy-excluded"
    assert "TOPSECRET" not in recipe.bundle.files[0].payload.decode()


def test_hydration_required_set_is_never_silently_truncated() -> None:
    required = _hydration("work", HydrationCategory.ACTIVE_WORK, 80)
    with pytest.raises(PolicyViolation, match="silent truncation"):
        build_hydration_recipe((required,), token_budget=79)


def test_hydration_priority_omissions_are_deterministic_and_private_safe() -> None:
    items = (
        _hydration("raw", HydrationCategory.RAW_TRANSCRIPT, 1),
        _hydration("article", HydrationCategory.KNOWLEDGE_ARTICLE, 1),
        _hydration("decision", HydrationCategory.HUMAN_DECISION, 30),
        _hydration("work", HydrationCategory.ACTIVE_WORK, 80),
        _hydration("skill", HydrationCategory.VALIDATED_SKILL, 30),
    )
    recipe = build_hydration_recipe(items, token_budget=110)
    assert [item.item_id for item in recipe.selected] == ["work", "decision"]
    reasons = {item.item_id: item.reason_code for item in recipe.omissions}
    assert reasons == {
        "raw": "never-auto-load",
        "article": "retrieve-on-demand",
        "skill": "optional-budget-exhausted",
    }
    assert recipe.tokens_used == 110
    assert recipe.as_dict()["grants_authority"] is False


def test_hydration_classification_gate_blocks_required_and_omits_optional() -> None:
    required_private = replace(
        _hydration("private-work", HydrationCategory.ACTIVE_WORK, 10),
        classification=DataClassification.RESTRICTED,
    )
    with pytest.raises(PolicyViolation, match="classification"):
        build_hydration_recipe((required_private,), token_budget=20)

    optional_private = replace(
        _hydration("private-rule", HydrationCategory.DURABLE_RULE, 10),
        classification=DataClassification.RESTRICTED,
    )
    recipe = build_hydration_recipe((optional_private,), token_budget=20)
    assert recipe.selected == ()
    assert recipe.omissions[0].reason_code == "classification-excluded"
