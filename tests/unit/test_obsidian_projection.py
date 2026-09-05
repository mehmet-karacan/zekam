from __future__ import annotations

import datetime as dt
from uuid import UUID

import pytest

from zekam.application.obsidian_projection import (
    ObsidianApplyPlan,
    build_obsidian_projection,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.markdown_projection import (
    ObsidianNoteKind,
    ObsidianProfile,
    ObsidianProjectionRecord,
    ProjectionRecord,
    ProjectionRelationRef,
    ProjectionSourceRef,
)
from zekam.domain.session_continuity import DataClassification, TruthClass

NOW = dt.datetime(2026, 8, 28, 10, 0, tzinfo=dt.UTC)
POLICY_DIGEST = digest("memory-policy")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000101")
OTHER_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000102")


def _record(
    entity_id: str,
    *,
    project_id: UUID = PROJECT_ID,
    classification: DataClassification = DataClassification.PUBLIC,
    related_id: str | None = None,
) -> ObsidianProjectionRecord:
    relations: tuple[ProjectionRelationRef, ...] = ()
    related: tuple[str, ...] = ()
    if related_id is not None:
        relations = (
            ProjectionRelationRef(
                f"relation:{entity_id}",
                "outgoing",
                "relates-to",
                related_id,
                digest({"from": entity_id, "to": related_id}),
            ),
        )
        related = (related_id,)
    source_digest = digest({"id": entity_id})
    return ObsidianProjectionRecord(
        ProjectionRecord(
            "work",
            entity_id,
            f"Work {entity_id}",
            "active",
            "Evidence-bound canonical summary.",
            (ProjectionSourceRef("work-item", entity_id, "revision-1", source_digest),),
            related,
            relations,
        ),
        ObsidianNoteKind.WORK,
        "yerel",
        project_id,
        TruthClass.REPO_FACT,
        classification,
        NOW,
    )


def test_obsidian_generation_is_byte_deterministic_and_links_are_controlled() -> None:
    first = _record(
        "00000000-0000-0000-0000-000000000001",
        related_id="00000000-0000-0000-0000-000000000002",
    )
    second = _record("00000000-0000-0000-0000-000000000002")
    left = build_obsidian_projection(
        (first, second),
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=POLICY_DIGEST,
    )
    right = build_obsidian_projection(
        (second, first),
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=POLICY_DIGEST,
    )
    assert left.manifest_bytes() == right.manifest_bytes()
    assert left.receipt_bytes() == right.receipt_bytes()
    assert left.projection_digest == right.projection_digest
    assert left.link_check_digest == right.link_check_digest
    rendered = b"\n".join(item.payload for item in left.files).decode()
    assert "[[01_ACTIVE/CALISMA_OGELERI/work-00000000-0000-0000-0000-000000000002" in rendered
    assert "read_only_projection: true" in rendered
    assert "grants_authority: false" in rendered
    assert "source_refs:" in rendered
    assert 'source_type: "work-item"' in rendered
    assert f'project_id: "{PROJECT_ID}"' in rendered


def test_profiles_are_physically_distinct_and_public_safe_excludes_internal() -> None:
    public = _record("00000000-0000-0000-0000-000000000001")
    internal = _record(
        "00000000-0000-0000-0000-000000000002",
        classification=DataClassification.INTERNAL,
    )
    private = build_obsidian_projection(
        (public, internal),
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PRIVATE_LOCAL,
        policy_digest=POLICY_DIGEST,
    )
    safe = build_obsidian_projection(
        (public, internal),
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=POLICY_DIGEST,
    )
    assert private.projection_digest != safe.projection_digest
    assert len(private.exclusions) == 0
    assert len(safe.exclusions) == 1
    assert safe.exclusions[0].reason_code == "classification-excluded"
    assert not any(b"00000000-0000-0000-0000-000000000002" in item.payload for item in safe.files)


def test_privacy_filter_excludes_source_before_render() -> None:
    unsafe = _record("00000000-0000-0000-0000-000000000003")
    unsafe = ObsidianProjectionRecord(
        record=ProjectionRecord(
            "work",
            unsafe.record.entity_id,
            unsafe.record.title,
            unsafe.record.status,
            "Contact operator@example.test before publishing.",
            unsafe.record.source_refs,
        ),
        note_kind=unsafe.note_kind,
        realm_slug=unsafe.realm_slug,
        project_id=unsafe.project_id,
        truth_class=unsafe.truth_class,
        classification=unsafe.classification,
        observed_at=unsafe.observed_at,
    )
    bundle = build_obsidian_projection(
        (unsafe,),
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=POLICY_DIGEST,
    )
    assert bundle.exclusions[0].reason_code == "pii-email"
    assert b"operator@example.test" not in b"\n".join(item.payload for item in bundle.files)


def test_empty_snapshot_requires_and_preserves_exact_realm() -> None:
    with pytest.raises(ValidationFailed, match="exact realm"):
        build_obsidian_projection(
            (),
            project_id=PROJECT_ID,
            profile=ObsidianProfile.PUBLIC_SAFE,
            policy_digest=POLICY_DIGEST,
        )
    bundle = build_obsidian_projection(
        (),
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=POLICY_DIGEST,
        realm_slug="yerel",
    )
    assert bundle.realm_slug == "yerel"
    assert bundle.project_id == PROJECT_ID


def test_project_identity_separates_equal_and_empty_snapshots() -> None:
    first_record = _record("00000000-0000-0000-0000-000000000001")
    second_record = _record(
        "00000000-0000-0000-0000-000000000001",
        project_id=OTHER_PROJECT_ID,
    )
    first = build_obsidian_projection(
        (first_record,),
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=POLICY_DIGEST,
    )
    second = build_obsidian_projection(
        (second_record,),
        project_id=OTHER_PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=POLICY_DIGEST,
    )
    empty_first = build_obsidian_projection(
        (),
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=POLICY_DIGEST,
        realm_slug="yerel",
    )
    empty_second = build_obsidian_projection(
        (),
        project_id=OTHER_PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=POLICY_DIGEST,
        realm_slug="yerel",
    )
    assert first.source_snapshot_digest != second.source_snapshot_digest
    assert first.projection_digest != second.projection_digest
    assert first.manifest_digest != second.manifest_digest
    assert first.receipt_digest != second.receipt_digest
    assert empty_first.source_snapshot_digest == empty_second.source_snapshot_digest
    assert empty_first.projection_digest != empty_second.projection_digest
    realm_id = UUID("00000000-0000-0000-0000-000000000001")
    first_plan = ObsidianApplyPlan.create(
        realm_id, first, store_identity_digest=digest("shared-store")
    )
    second_plan = ObsidianApplyPlan.create(
        realm_id, second, store_identity_digest=digest("shared-store")
    )
    assert str(PROJECT_ID) in first_plan.resource
    assert str(OTHER_PROJECT_ID) in second_plan.resource
    assert first_plan.resource != second_plan.resource
    assert first_plan.effect_digest != second_plan.effect_digest
    assert first_plan.plan_digest != second_plan.plan_digest


def test_apply_plan_binds_exact_physical_store_identity() -> None:
    bundle = build_obsidian_projection(
        (_record("00000000-0000-0000-0000-000000000001"),),
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=POLICY_DIGEST,
    )
    realm_id = UUID("00000000-0000-0000-0000-000000000001")
    first = ObsidianApplyPlan.create(
        realm_id,
        bundle,
        store_identity_digest=digest("store-a"),
    )
    second = ObsidianApplyPlan.create(
        realm_id,
        bundle,
        store_identity_digest=digest("store-b"),
    )
    assert first.plan_digest != second.plan_digest


def test_projection_rejects_cross_project_record_laundering() -> None:
    record = _record("00000000-0000-0000-0000-000000000001")
    with pytest.raises(PolicyViolation, match="requested project"):
        build_obsidian_projection(
            (record,),
            project_id=OTHER_PROJECT_ID,
            profile=ObsidianProfile.PUBLIC_SAFE,
            policy_digest=POLICY_DIGEST,
        )
