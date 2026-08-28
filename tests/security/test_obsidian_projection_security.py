from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from dataclasses import replace
from uuid import UUID

import pytest

from zekam.application.obsidian_projection import (
    build_obsidian_projection,
    validate_obsidian_projection_bundle,
)
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed
from zekam.domain.markdown_projection import (
    ObsidianNoteKind,
    ObsidianProfile,
    ObsidianProjectionFile,
    ObsidianProjectionRecord,
    ProjectionRecord,
    ProjectionSourceRef,
)
from zekam.domain.session_continuity import DataClassification, TruthClass
from zekam.infrastructure.storage.obsidian_projection_store import (
    LocalObsidianProjectionStore,
    StagedObsidianProjection,
)

NOW = dt.datetime(2026, 8, 28, 10, 0, tzinfo=dt.UTC)
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000101")
OTHER_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000102")


def _bundle(
    *,
    project_id: UUID = PROJECT_ID,
    realm_slug: str = "yerel",
    profile: ObsidianProfile = ObsidianProfile.PUBLIC_SAFE,
):  # type: ignore[no-untyped-def]
    record = ProjectionRecord(
        "work",
        "00000000-0000-0000-0000-000000000001",
        "Safe source",
        "active",
        "No private data.",
        (
            ProjectionSourceRef(
                "work-item",
                "00000000-0000-0000-0000-000000000001",
                "revision-1",
                digest("safe-source"),
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


@pytest.mark.parametrize("path", ("../escape.md", r"C:\escape.md", r"notes\evil.md"))
def test_projection_rejects_nonportable_paths(path: str) -> None:
    with pytest.raises(ValidationFailed, match="portable"):
        ObsidianProjectionFile(path, b"unsafe", "text/markdown; charset=utf-8")


def test_verifier_rejects_unmanifested_file_without_moving_current(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    bundle = _bundle()
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
    (generation / "UNMANIFESTED.md").write_text("forged", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="unmanifested"):
        store.verify_current(
            "yerel",
            PROJECT_ID,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )


def test_missing_status_check_is_read_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "does-not-exist"
    store = LocalObsidianProjectionStore(root)
    bundle = _bundle()
    with pytest.raises(NotFound):
        store.verify_current(
            "yerel",
            PROJECT_ID,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )
    assert not root.exists()


def test_current_pointer_rejects_cross_project_binding(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    bundle = _bundle()
    store.publish(store.stage(bundle))
    pointer = (
        tmp_path
        / "obsidian"
        / "yerel"
        / str(PROJECT_ID)
        / "public-safe"
        / "CURRENT.json"
    )
    body = json.loads(pointer.read_text(encoding="utf-8"))
    body["project_id"] = str(OTHER_PROJECT_ID)
    pointer.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(PolicyViolation, match="project binding"):
        store.verify_current(
            "yerel",
            PROJECT_ID,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )


def test_secret_and_raw_classifications_never_render() -> None:
    source = ProjectionRecord(
        "memory",
        "00000000-0000-0000-0000-000000000009",
        "Sensitive source",
        "active",
        "Should not render.",
        (
            ProjectionSourceRef(
                "memory-record",
                "00000000-0000-0000-0000-000000000009",
                "revision-1",
                digest("sensitive"),
            ),
        ),
    )
    for classification in (
        DataClassification.SECRET,
        DataClassification.RAW_TRANSCRIPT,
        DataClassification.DIAGNOSTIC_PAYLOAD,
    ):
        typed = ObsidianProjectionRecord(
            source,
            ObsidianNoteKind.KNOWLEDGE,
            "yerel",
            PROJECT_ID,
            TruthClass.UNKNOWN,
            classification,
            NOW,
        )
        bundle = build_obsidian_projection(
            (typed,),
            project_id=PROJECT_ID,
            profile=ObsidianProfile.PRIVATE_LOCAL,
            policy_digest=digest("policy"),
        )
        assert bundle.exclusions[0].reason_code == "classification-prohibited"
        assert b"Should not render" not in b"\n".join(item.payload for item in bundle.files)


def test_write_boundary_repeats_privacy_scan_before_authority_consumption() -> None:
    bundle = _bundle()
    target = next(item for item in bundle.files if item.relative_path.endswith(".md"))
    forged = replace(target, payload=target.payload + b"\noperator@example.test\n")
    changed = replace(
        bundle,
        files=tuple(forged if item is target else item for item in bundle.files),
    )
    with pytest.raises(PolicyViolation, match="privacy scan"):
        validate_obsidian_projection_bundle(changed)


def test_publish_rejects_forged_generation_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    bundle = _bundle()
    staged = store.stage(bundle)
    forged = StagedObsidianProjection(bundle, staged.staging_root, "../escape")
    with pytest.raises(PolicyViolation, match="exact projection digest"):
        store.publish(forged)


def test_publish_revalidates_staged_tree_before_current_swap(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    bundle = _bundle()
    staged = store.stage(bundle)
    target = next(staged.staging_root.rglob("*.md"))
    target.write_bytes(target.read_bytes() + b"\nforged\n")
    with pytest.raises(PolicyViolation, match="file digest drift"):
        store.publish(staged)
    assert not (
        tmp_path
        / "obsidian"
        / "yerel"
        / str(PROJECT_ID)
        / "public-safe"
        / "CURRENT.json"
    ).exists()


def test_live_manifest_binding_rejects_coordinated_projection_forge(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    bundle = _bundle()
    store.publish(store.stage(bundle))
    profile_root = (
        tmp_path / "obsidian" / "yerel" / str(PROJECT_ID) / "public-safe"
    )
    generation = profile_root / "generations" / bundle.projection_digest.removeprefix(
        "sha256:"
    )
    target = next(
        item
        for item in generation.rglob("*.md")
        if item.name not in {"README.md"}
    )
    target.write_bytes(target.read_bytes() + b"\noperator@example.test\n")

    manifest_path = generation / "_META" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for row in manifest["files"]:
        if row["relative_path"] == target.relative_to(generation).as_posix():
            row["content_digest"] = digest_of_bytes(target.read_bytes())
            break
    manifest.pop("manifest_digest")
    manifest_digest = digest(manifest)
    manifest_path.write_bytes(
        canonical_bytes(manifest | {"manifest_digest": manifest_digest})
    )

    receipt_path = generation / "_META" / "projection-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.pop("receipt_digest")
    receipt["manifest_digest"] = manifest_digest
    receipt_digest = digest(receipt)
    receipt_path.write_bytes(canonical_bytes(receipt | {"receipt_digest": receipt_digest}))

    pointer_path = profile_root / "CURRENT.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["manifest_digest"] = manifest_digest
    pointer["receipt_digest"] = receipt_digest
    pointer_path.write_bytes(canonical_bytes(pointer))

    with pytest.raises(PolicyViolation, match="live manifest/receipt binding"):
        store.verify_current(
            "yerel",
            PROJECT_ID,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )


@pytest.mark.parametrize(
    ("realm_slug", "profile"),
    (
        ("kurumsal", ObsidianProfile.PUBLIC_SAFE),
        ("yerel", ObsidianProfile.PRIVATE_LOCAL),
    ),
)
def test_verifier_rejects_generation_copied_across_realm_or_profile(
    tmp_path,
    realm_slug: str,
    profile: ObsidianProfile,
) -> None:  # type: ignore[no-untyped-def]
    store = LocalObsidianProjectionStore(tmp_path / "obsidian")
    source = _bundle()
    store.publish(store.stage(source))
    source_root = (
        tmp_path / "obsidian" / "yerel" / str(PROJECT_ID) / "public-safe"
    )
    target_root = tmp_path / "obsidian" / realm_slug / str(PROJECT_ID) / profile.value
    target_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, target_root)
    expected = _bundle(realm_slug=realm_slug, profile=profile)
    with pytest.raises(PolicyViolation, match="realm/profile binding"):
        store.verify_current(
            realm_slug,
            PROJECT_ID,
            profile,
            expected_projection_digest=expected.projection_digest,
            expected_manifest_digest=expected.manifest_digest,
            expected_receipt_digest=expected.receipt_digest,
        )


def test_store_rejects_real_symlink_realm_escape(tmp_path) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "obsidian"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        os.symlink(outside, root / "yerel", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink olusturulamadi: {exc}")
    with pytest.raises(PolicyViolation, match="symlink|reparse"):
        LocalObsidianProjectionStore(root).stage(_bundle())
