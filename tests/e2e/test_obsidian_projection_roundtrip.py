from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from zekam.application.obsidian_projection import (
    ObsidianApplyPlan,
    ObsidianProjectionService,
    build_obsidian_projection,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.markdown_projection import (
    ObsidianNoteKind,
    ObsidianProfile,
    ObsidianProjectionRecord,
    ProjectionRecord,
    ProjectionSourceRef,
)
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.session_continuity import DataClassification, TruthClass
from zekam.infrastructure.storage.obsidian_projection_store import (
    LocalObsidianProjectionStore,
)

NOW = dt.datetime(2026, 8, 28, 10, 0, tzinfo=dt.UTC)
REALM_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000101")
OTHER_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000102")


class _Authorizations:
    def __init__(self, authorization: Authorization) -> None:
        self.authorization = authorization
        self.consumed = False

    def get(self, authorization_id: UUID) -> Authorization:
        assert authorization_id == self.authorization.id
        return self.authorization

    def consume(
        self,
        authorization_id: UUID,
        *,
        effect_digest: str,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> SimpleNamespace:
        assert authorization_id == self.authorization.id
        assert effect_digest == self.authorization.effect_digest
        assert consumed_by and now is not None
        self.consumed = True
        return SimpleNamespace(consumed=True)


def _bundle(
    summary: str,
    project_id: UUID = PROJECT_ID,
    profile: ObsidianProfile = ObsidianProfile.PUBLIC_SAFE,
    realm_slug: str = "yerel",
):  # type: ignore[no-untyped-def]
    record = ProjectionRecord(
        "work",
        "00000000-0000-0000-0000-000000000001",
        "Roundtrip work",
        "active",
        summary,
        (
            ProjectionSourceRef(
                "work-item",
                "00000000-0000-0000-0000-000000000001",
                "revision-1",
                digest(summary),
            ),
        ),
    )
    typed = ObsidianProjectionRecord(
        record,
        ObsidianNoteKind.WORK,
        realm_slug,
        project_id,
        TruthClass.REPO_FACT,
        DataClassification.PUBLIC,
        NOW,
    )
    return build_obsidian_projection(
        (typed,),
        project_id=project_id,
        profile=profile,
        policy_digest=digest("policy"),
    )


def test_immutable_generation_and_atomic_current_roundtrip(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    first = _bundle("First canonical state.")
    published = store.publish(store.stage(first))
    verified = store.verify_current(
        "yerel",
        PROJECT_ID,
        ObsidianProfile.PUBLIC_SAFE,
        expected_projection_digest=first.projection_digest,
        expected_manifest_digest=first.manifest_digest,
        expected_receipt_digest=first.receipt_digest,
    )
    assert verified["status"] == "passed"
    assert published.projection_digest == first.projection_digest
    second = _bundle("Second canonical state.")
    with pytest.raises(PolicyViolation, match="stale"):
        store.verify_current(
            "yerel",
            PROJECT_ID,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=second.projection_digest,
            expected_manifest_digest=second.manifest_digest,
            expected_receipt_digest=second.receipt_digest,
        )
    store.publish(store.stage(second))
    assert (
        store.verify_current(
            "yerel",
            PROJECT_ID,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=second.projection_digest,
            expected_manifest_digest=second.manifest_digest,
            expected_receipt_digest=second.receipt_digest,
        )["projection_digest"]
        == second.projection_digest
    )
    first_generation = (
        tmp_path
        / "obsidian"
        / "yerel"
        / str(PROJECT_ID)
        / "public-safe"
        / "generations"
        / first.projection_digest.removeprefix("sha256:")
    )
    assert first_generation.is_dir()


def test_existing_generation_is_reused_only_when_manifest_matches(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    bundle = _bundle("Immutable source.")
    store.publish(store.stage(bundle))
    generation = (
        tmp_path
        / "obsidian"
        / "yerel"
        / str(PROJECT_ID)
        / "public-safe"
        / "generations"
        / bundle.projection_digest.removeprefix("sha256:")
    )
    manifest = generation / "_META" / "manifest.json"
    manifest.write_bytes(b"forged")
    with pytest.raises((PolicyViolation, ValidationFailed)):
        store.publish(store.stage(bundle))


def test_projection_service_requires_exact_store_bound_authorization(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    bundle = _bundle("Authorized publication.")
    plan = ObsidianApplyPlan.create(
        REALM_ID,
        bundle,
        store_identity_digest=store.identity_digest,
    )
    authorization = Authorization.issue(
        realm_id=REALM_ID,
        actor_id=UUID("00000000-0000-0000-0000-000000000002"),
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(plan.resource,),
            allowed_effects=("file-write",),
        ),
        risk="medium",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    authorizations = _Authorizations(authorization)
    result = ObsidianProjectionService(store, authorizations).apply(
        plan,
        authorization_id=authorization.id,
        now=NOW,
    )
    assert authorizations.consumed
    assert result["status"] == "completed"
    assert result["project_id"] == str(PROJECT_ID)
    assert result["projection_digest"] == bundle.projection_digest
    assert result["store_identity_digest"] == store.identity_digest
    assert f":{PROJECT_ID}:public-safe:" in result["current_ref"]
    assert result["result_digest"].startswith("sha256:")


def test_projects_cannot_overwrite_each_others_current_pointer(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    first = _bundle("Equal canonical state.", PROJECT_ID)
    second = _bundle("Equal canonical state.", OTHER_PROJECT_ID)
    store.publish(store.stage(first))
    first_before = store.verify_current(
        "yerel",
        PROJECT_ID,
        ObsidianProfile.PUBLIC_SAFE,
        expected_projection_digest=first.projection_digest,
        expected_manifest_digest=first.manifest_digest,
        expected_receipt_digest=first.receipt_digest,
    )
    store.publish(store.stage(second))
    first_after = store.verify_current(
        "yerel",
        PROJECT_ID,
        ObsidianProfile.PUBLIC_SAFE,
        expected_projection_digest=first.projection_digest,
        expected_manifest_digest=first.manifest_digest,
        expected_receipt_digest=first.receipt_digest,
    )
    second_current = store.verify_current(
        "yerel",
        OTHER_PROJECT_ID,
        ObsidianProfile.PUBLIC_SAFE,
        expected_projection_digest=second.projection_digest,
        expected_manifest_digest=second.manifest_digest,
        expected_receipt_digest=second.receipt_digest,
    )
    assert first.projection_digest != second.projection_digest
    assert first_before == first_after
    assert first_after["project_id"] == str(PROJECT_ID)
    assert second_current["project_id"] == str(OTHER_PROJECT_ID)


def test_empty_project_snapshots_publish_to_distinct_generations(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    first = build_obsidian_projection(
        (),
        project_id=PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=digest("policy"),
        realm_slug="yerel",
    )
    second = build_obsidian_projection(
        (),
        project_id=OTHER_PROJECT_ID,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=digest("policy"),
        realm_slug="yerel",
    )
    store.publish(store.stage(first))
    store.publish(store.stage(second))
    assert first.projection_digest != second.projection_digest
    assert store.verify_current(
        "yerel",
        PROJECT_ID,
        ObsidianProfile.PUBLIC_SAFE,
        expected_projection_digest=first.projection_digest,
        expected_manifest_digest=first.manifest_digest,
        expected_receipt_digest=first.receipt_digest,
    )["project_id"] == str(PROJECT_ID)
    assert store.verify_current(
        "yerel",
        OTHER_PROJECT_ID,
        ObsidianProfile.PUBLIC_SAFE,
        expected_projection_digest=second.projection_digest,
        expected_manifest_digest=second.manifest_digest,
        expected_receipt_digest=second.receipt_digest,
    )["project_id"] == str(OTHER_PROJECT_ID)


def test_private_and_public_profiles_have_distinct_physical_current_pointers(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    public = _bundle("Shared public source.")
    private = _bundle(
        "Shared public source.",
        profile=ObsidianProfile.PRIVATE_LOCAL,
    )
    store.publish(store.stage(public))
    store.publish(store.stage(private))
    public_pointer = (
        tmp_path / "obsidian" / "yerel" / str(PROJECT_ID) / "public-safe" / "CURRENT.json"
    )
    private_pointer = (
        tmp_path / "obsidian" / "yerel" / str(PROJECT_ID) / "private-local" / "CURRENT.json"
    )
    assert public_pointer.is_file() and private_pointer.is_file()
    assert public_pointer != private_pointer
    assert (
        store.verify_current(
            "yerel",
            PROJECT_ID,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=public.projection_digest,
            expected_manifest_digest=public.manifest_digest,
            expected_receipt_digest=public.receipt_digest,
        )["profile"]
        == ObsidianProfile.PUBLIC_SAFE.value
    )
    assert (
        store.verify_current(
            "yerel",
            PROJECT_ID,
            ObsidianProfile.PRIVATE_LOCAL,
            expected_projection_digest=private.projection_digest,
            expected_manifest_digest=private.manifest_digest,
            expected_receipt_digest=private.receipt_digest,
        )["profile"]
        == ObsidianProfile.PRIVATE_LOCAL.value
    )


def test_current_swap_failure_preserves_previous_valid_pointer(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    first = _bundle("First stable state.")
    second = _bundle("Second state must not become CURRENT.")
    store.publish(store.stage(first))
    staged = store.stage(second)
    original_replace = Path.replace

    def fail_current_swap(path: Path, target: Path) -> Path:
        if path.name.startswith(".CURRENT-"):
            raise OSError("injected CURRENT swap failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_current_swap)
    with pytest.raises(OSError, match="injected CURRENT"):
        store.publish(staged)
    verified = store.verify_current(
        "yerel",
        PROJECT_ID,
        ObsidianProfile.PUBLIC_SAFE,
        expected_projection_digest=first.projection_digest,
        expected_manifest_digest=first.manifest_digest,
        expected_receipt_digest=first.receipt_digest,
    )
    assert verified["projection_digest"] == first.projection_digest
