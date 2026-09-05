"""WP-05 knowledge file plane, CAS, privacy and relation projection tests."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from zekam.application.home import HomeLayout
from zekam.application.knowledge_file_plane import (
    ArtifactPutPlan,
    KnowledgeClassification,
    KnowledgeNoteManifest,
    KnowledgePolicyProfile,
    ProjectProjection,
    SyncProfile,
    WikiLinkNote,
    WikiLinkRelation,
    assert_public_safe_projection,
    generated_note_bytes,
    note_content_digest,
    render_wikilink_projection,
    validate_generated_note,
    validate_note_ownership_path,
    validate_owner_scope,
    validate_portable_relative,
)
from zekam.application.operational_store import ArtifactRefRecord, KnowledgeNoteRecord
from zekam.domain.canonical import digest
from zekam.domain.errors import ConcurrencyConflict, LayoutError, PolicyViolation, ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.unit

PROJECT_ID = "018f0000-0000-7000-8000-000000000001"
NOTE_A = "018f0000-0000-7000-8000-00000000000a"
NOTE_B = "018f0000-0000-7000-8000-00000000000b"
RELATION_ID = "018f0000-0000-7000-8000-00000000000c"
REALM_ID = "018f0000-0000-7000-8000-00000000000d"


def _directory_link(link: Path, target: Path) -> None:
    if os.name != "nt":
        link.symlink_to(target, target_is_directory=True)
        return
    command = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "cmd.exe"
    created = subprocess.run(
        [str(command), "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert created.returncode == 0, created.stderr


def _file_store(tmp_path: Path) -> KnowledgeFileStore:
    home = tmp_path / "home"
    HomeLayout(home).ensure().ensure_project("akilli-kasa")
    return KnowledgeFileStore(home)


def _user_manifest(payload: bytes, *, state: str = "active") -> KnowledgeNoteManifest:
    return KnowledgeNoteManifest(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="note",
        authorship="user",
        classification=KnowledgeClassification.LOCAL_PRIVATE,
        portable_ref="projeler/akilli-kasa/notlar/user/odeme-akisi.md",
        content_digest=note_content_digest(payload),
        project_slug="akilli-kasa",
        state=state,
    )


def test_project_projection_is_deterministic_portable_and_binding_guarded(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    projection = ProjectProjection.create(
        project_id=PROJECT_ID,
        slug="akilli-kasa",
        display_name="Akilli Kasa",
        status="active",
        source_bindings=("github:mehmet-karacan/akilli-kasa",),
        technologies=("python", "sqlite"),
        knowledge_scopes=(f"project:{PROJECT_ID}",),
        last_source_snapshot=digest("akilli-kasa-small-fixture"),
    )

    target = store.publish_project_projection(projection)
    assert store.publish_project_projection(projection) == target
    assert b"/Users/" not in target.read_bytes()

    drift = ProjectProjection.create(
        project_id="018f0000-0000-7000-8000-000000000099",
        slug="akilli-kasa",
        display_name="Different",
        status="active",
        source_bindings=(),
        last_source_snapshot=digest("other"),
    )
    with pytest.raises(PolicyViolation, match="binding drift"):
        store.publish_project_projection(drift)


@pytest.mark.parametrize(
    ("owner", "authorship", "portable_ref"),
    [
        ("global-user", "user", "projeler/akilli-kasa/notlar/user/a.md"),
        (f"project:{PROJECT_ID}", "generated", "global/raporlar/generated/a.md"),
        (f"project:{PROJECT_ID}", "user", "projeler/akilli-kasa/notlar/generated/a.md"),
        (f"project:{PROJECT_ID}", "user", "projeler/akilli-kasa/notlar/user/a.txt"),
        (f"project:{PROJECT_ID}", "user", "inbox/generated/user/a.md"),
        (f"project:{PROJECT_ID}", "user", "projeler/other/notlar/user/a.md"),
        (f"work:{PROJECT_ID}", "user", "inbox/user/a.md"),
        (f"session:{PROJECT_ID}", "generated", "projeler/other/notlar/generated/a.md"),
    ],
)
def test_note_manifest_rejects_ambiguous_owner_or_authorship_path(
    owner: str, authorship: str, portable_ref: str
) -> None:
    with pytest.raises(ValidationFailed):
        KnowledgeNoteManifest(
            owner_scope=owner,
            note_kind="note",
            authorship=authorship,
            classification=KnowledgeClassification.INTERNAL,
            portable_ref=portable_ref,
            content_digest=digest("content"),
            project_slug="akilli-kasa",
        )


@pytest.mark.parametrize(
    "value",
    (None, "", "unknown", "project", "unknown:018f0000-0000-7000-8000-000000000001"),
)
def test_owner_scope_rejects_wrong_types_and_unknown_kinds(value: object) -> None:
    with pytest.raises(ValidationFailed):
        validate_owner_scope(value)


@pytest.mark.parametrize("value", (None, "", "bad\\path", "../escape", "/absolute"))
def test_portable_path_rejects_wrong_types_and_traversal(value: object) -> None:
    with pytest.raises(ValidationFailed):
        validate_portable_relative(value)


@pytest.mark.parametrize(
    ("owner", "path", "authorship", "slug"),
    (
        ("global-user", "global/notlar/user/a.md", "other", None),
        ("global-user", "global/notlar/user/a.txt", "user", None),
        ("global-user", "global/notlar/a.md", "user", None),
        ("global-user", "global/user/generated/a.md", "user", None),
        ("global-user", "global/notlar/user/a.md", "user", "project"),
        (f"project:{PROJECT_ID}", "projeler/project/notlar/user/a.md", "user", None),
    ),
)
def test_note_ownership_rejects_each_ambiguous_path_contract(
    owner: object, path: object, authorship: object, slug: object
) -> None:
    with pytest.raises(ValidationFailed):
        validate_note_ownership_path(owner, path, authorship, project_slug=slug)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("display_name", ""),
        ("status", "unknown"),
        ("source_bindings", ["source:a"]),
        ("source_bindings", ("source:a", "source:a")),
        ("related_projects", (PROJECT_ID,)),
        ("related_projects", (NOTE_A, NOTE_A)),
        ("technologies", ("python", "python")),
        ("knowledge_scopes", ("global-user", "global-user")),
    ),
)
def test_project_projection_rejects_noncanonical_collections(field: str, value: object) -> None:
    values: dict[str, object] = {
        "project_id": PROJECT_ID,
        "slug": "project",
        "display_name": "Project",
        "status": "active",
        "source_bindings": ("source:a",),
        "last_source_snapshot": digest("snapshot"),
    }
    values[field] = value
    with pytest.raises(ValidationFailed):
        ProjectProjection.create(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("note_kind", "unknown"),
        ("classification", "internal"),
        ("state", "unknown"),
    ),
)
def test_note_manifest_rejects_untyped_or_unknown_fields(field: str, value: object) -> None:
    manifest = _user_manifest(b"valid")
    with pytest.raises(ValidationFailed):
        replace(manifest, **cast(Any, {field: value}))


def test_user_and_generated_notes_are_distinct_immutable_files(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    user_payload = b"# Kullanici notu\n\nOdeme terminali offline davranir.\n"
    user = _user_manifest(user_payload)
    assert store.create_note(user, user_payload).read_bytes() == user_payload
    assert store.create_note(user, user_payload).read_bytes() == user_payload
    drift_payload = user_payload + b"drift"
    drift_manifest = KnowledgeNoteManifest(
        owner_scope=user.owner_scope,
        note_kind=user.note_kind,
        authorship=user.authorship,
        classification=user.classification,
        portable_ref=user.portable_ref,
        content_digest=note_content_digest(drift_payload),
        project_slug=user.project_slug,
    )
    with pytest.raises(PolicyViolation, match="overwrite"):
        store.create_note(drift_manifest, drift_payload)

    generated_payload = generated_note_bytes(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="report",
        classification=KnowledgeClassification.INTERNAL,
        source_refs=("source:akilli-kasa/docs/offline.md",),
        source_digests=(digest("offline-source"),),
        generated_at="2026-09-02T00:00:00Z",
        generator_version="wp05-test-v1",
        body="# Offline raporu\n",
        project_slug="akilli-kasa",
    )
    generated = KnowledgeNoteManifest(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="report",
        authorship="generated",
        classification=KnowledgeClassification.INTERNAL,
        portable_ref="projeler/akilli-kasa/raporlar/generated/offline.md",
        content_digest=note_content_digest(generated_payload),
        project_slug="akilli-kasa",
    )
    assert store.create_note(generated, generated_payload).read_bytes() == generated_payload


def test_generated_note_rejects_noncanonical_or_duplicate_source_metadata() -> None:
    payload = generated_note_bytes(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="report",
        classification=KnowledgeClassification.INTERNAL,
        source_refs=("source:a", "source:b"),
        source_digests=(digest("a"), digest("b")),
        generated_at="2026-09-02T00:00:00Z",
        generator_version="test-v1",
        body="# Report\n",
        project_slug="akilli-kasa",
    )
    assert validate_generated_note(payload)["source_refs"] == ["source:a", "source:b"]
    duplicate_digest = payload.replace(f"- {digest('b')}".encode(), f"- {digest('a')}".encode())
    with pytest.raises(ValidationFailed, match="canonical unique"):
        validate_generated_note(duplicate_digest)
    unsorted_refs = payload.replace(b"- source:a\n- source:b", b"- source:b\n- source:a")
    with pytest.raises(ValidationFailed, match="canonical sirada"):
        validate_generated_note(unsorted_refs)
    missing_digest = payload.replace(f"- {digest('b')}\n".encode(), b"")
    with pytest.raises(ValidationFailed, match="cardinality"):
        validate_generated_note(missing_digest)
    duplicate_key = payload.replace(
        f"owner_scope: project:{PROJECT_ID}\n".encode(),
        (f"owner_scope: project:{PROJECT_ID}\nowner_scope: project:{PROJECT_ID}\n").encode(),
    )
    with pytest.raises(ValidationFailed, match="YAML bozuk"):
        validate_generated_note(duplicate_key)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("project_slug", None),
        ("note_kind", "unknown"),
        ("classification", "internal"),
        ("source_refs", ("source:a", "source:a")),
        ("source_digests", (digest("a"), digest("a"))),
        ("source_digests", ()),
        ("generated_at", None),
        ("generated_at", "2026-09-02T00:00:00"),
        ("generated_at", "2026-09-02T03:00:00+03:00"),
        ("generator_version", ""),
        ("body", None),
    ),
)
def test_generated_note_constructor_rejects_noncanonical_contract(
    field: str, value: object
) -> None:
    values: dict[str, object] = {
        "owner_scope": f"project:{PROJECT_ID}",
        "note_kind": "report",
        "classification": KnowledgeClassification.INTERNAL,
        "source_refs": ("source:a",),
        "source_digests": (digest("a"),),
        "generated_at": "2026-09-02T00:00:00Z",
        "generator_version": "test-v1",
        "body": "# Report\n",
        "project_slug": "akilli-kasa",
    }
    values[field] = value
    with pytest.raises(ValidationFailed):
        generated_note_bytes(**values)  # type: ignore[arg-type]


def test_global_generated_note_rejects_project_slug() -> None:
    with pytest.raises(ValidationFailed, match="Global"):
        generated_note_bytes(
            owner_scope="global-user",
            note_kind="report",
            classification=KnowledgeClassification.INTERNAL,
            source_refs=("source:a",),
            source_digests=(digest("a"),),
            generated_at="2026-09-02T00:00:00Z",
            generator_version="test-v1",
            body="# Report\n",
            project_slug="akilli-kasa",
        )


def test_identical_note_create_and_archive_are_concurrency_safe(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    payload = b"# Concurrent note\n"
    manifest = _user_manifest(payload)
    with ThreadPoolExecutor(max_workers=8) as executor:
        created = tuple(executor.map(lambda _: store.create_note(manifest, payload), range(24)))
    assert len(set(created)) == 1

    with ThreadPoolExecutor(max_workers=8) as executor:
        archived = tuple(executor.map(lambda _: store.archive_note(manifest), range(24)))
    assert len(set(archived)) == 1
    assert (store.home / archived[0]).read_bytes() == payload


def test_cas_is_content_addressed_idempotent_and_concurrency_safe(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    payload = b"small-akilli-kasa-artifact"
    plan = ArtifactPutPlan.create(
        payload,
        media_type="application/octet-stream",
        classification=KnowledgeClassification.LOCAL_PRIVATE,
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _: store.put_artifact(plan, payload), range(24)))

    assert len(set(results)) == 1
    assert results[0].read_bytes() == payload
    with pytest.raises(ConcurrencyConflict, match="payload drift"):
        store.put_artifact(plan, b"different")


def test_secret_payloads_are_rejected_from_normal_file_plane(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    with pytest.raises(PolicyViolation, match="secret backend"):
        ArtifactPutPlan.create(
            b"credential-value",
            media_type="text/plain",
            classification=KnowledgeClassification.SECRET,
        )
    payload = b"# secret ref only\n"
    manifest = KnowledgeNoteManifest(
        owner_scope="global-user",
        note_kind="reference",
        authorship="user",
        classification=KnowledgeClassification.SECRET,
        portable_ref="global/referanslar/user/secret.md",
        content_digest=note_content_digest(payload),
    )
    with pytest.raises(PolicyViolation, match="secret backend"):
        store.create_note(manifest, payload)


@pytest.mark.parametrize(
    ("payload", "media_type", "classification"),
    (
        ("text", "text/plain", KnowledgeClassification.INTERNAL),
        (b"text", "", KnowledgeClassification.INTERNAL),
        (b"text", "plain", KnowledgeClassification.INTERNAL),
        (b"text", "text/plain", "internal"),
        (b"\xff", "text/plain", KnowledgeClassification.INTERNAL),
        (b"AKIA" + b"A" * 16, "text/plain", KnowledgeClassification.INTERNAL),
        (b"person@example.com", "text/plain", KnowledgeClassification.PUBLIC),
    ),
)
def test_artifact_plan_rejects_untyped_or_unsafe_payloads(
    payload: object, media_type: object, classification: object
) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        ArtifactPutPlan.create(
            cast(Any, payload),
            media_type=cast(Any, media_type),
            classification=cast(Any, classification),
        )


@pytest.mark.parametrize(
    ("sync_profile", "classification", "allowed"),
    (
        (SyncProfile.NONE, KnowledgeClassification.PUBLIC, False),
        (SyncProfile.PRIVATE_LOCAL, KnowledgeClassification.LOCAL_PRIVATE, True),
        (SyncProfile.PUBLIC_SAFE, KnowledgeClassification.INTERNAL, False),
        (SyncProfile.CORPORATE_REVIEWED, KnowledgeClassification.RESTRICTED, False),
    ),
)
def test_sync_profiles_enforce_each_classification_boundary(
    sync_profile: SyncProfile,
    classification: KnowledgeClassification,
    allowed: bool,
) -> None:
    profile = KnowledgePolicyProfile(REALM_ID, sync_profile, "projection/root")
    if allowed:
        profile.assert_projection_allowed(classification)
    else:
        with pytest.raises(PolicyViolation):
            profile.assert_projection_allowed(classification)


def test_policy_profile_rejects_untyped_sync_profile() -> None:
    with pytest.raises(ValidationFailed):
        KnowledgePolicyProfile(REALM_ID, "public-safe", "projection/root")  # type: ignore[arg-type]


@pytest.mark.parametrize("title", (None, "", " padded", "bad\nline", "[[markup]]"))
def test_wikilink_note_rejects_ambiguous_titles(title: object) -> None:
    with pytest.raises(ValidationFailed):
        WikiLinkNote(
            NOTE_A,
            REALM_ID,
            f"project:{PROJECT_ID}",
            "akilli-kasa",
            "generated",
            cast(Any, title),
            "projeler/akilli-kasa/notlar/generated/a.md",
            KnowledgeClassification.PUBLIC,
            digest("note"),
        )


def test_wikilink_note_and_relation_require_typed_verified_evidence() -> None:
    with pytest.raises(ValidationFailed):
        WikiLinkNote(
            NOTE_A,
            REALM_ID,
            f"project:{PROJECT_ID}",
            "akilli-kasa",
            "generated",
            "Title",
            "projeler/akilli-kasa/notlar/generated/a.md",
            "public",  # type: ignore[arg-type]
            digest("note"),
        )
    with pytest.raises(ValidationFailed, match="self"):
        WikiLinkRelation(RELATION_ID, NOTE_A, NOTE_A, "related-to", digest("r"), True)
    with pytest.raises(ValidationFailed, match="verified"):
        WikiLinkRelation(RELATION_ID, NOTE_A, NOTE_B, "related-to", digest("r"), False)


def test_audit_detects_half_transaction_orphans_corruption_and_public_pii(
    tmp_path: Path,
) -> None:
    store = _file_store(tmp_path)
    note_payload = b"# Public\n\nPublic-safe note.\n"
    note_manifest = KnowledgeNoteManifest(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="note",
        authorship="user",
        classification=KnowledgeClassification.PUBLIC,
        portable_ref="projeler/akilli-kasa/notlar/user/public.md",
        content_digest=note_content_digest(note_payload),
        project_slug="akilli-kasa",
    )
    note_target = store.create_note(note_manifest, note_payload)
    note_record = KnowledgeNoteRecord(
        NOTE_A,
        note_manifest.owner_scope,
        note_manifest.portable_ref,
        note_manifest.note_kind,
        note_manifest.authorship,
        note_manifest.classification.value,
        note_manifest.content_digest,
        note_manifest.state,
        REALM_ID,
        PROJECT_ID,
        "akilli-kasa",
        True,
    )
    artifact_payload = b"registered-cas"
    artifact_plan = ArtifactPutPlan.create(
        artifact_payload,
        media_type="application/octet-stream",
        classification=KnowledgeClassification.INTERNAL,
    )
    artifact_target = store.put_artifact(artifact_plan, artifact_payload)
    artifact_record = ArtifactRefRecord(
        artifact_plan.digest,
        artifact_plan.media_type,
        artifact_plan.size_bytes,
        artifact_plan.classification.value,
    )
    orphan_note = store.home / "inbox" / "user" / "orphan.md"
    orphan_note.parent.mkdir(parents=True)
    orphan_note.write_text("# unmanifested\n", encoding="utf-8")
    orphan_payload = b"orphan-cas"
    orphan_plan = ArtifactPutPlan.create(
        orphan_payload,
        media_type="application/octet-stream",
        classification=KnowledgeClassification.INTERNAL,
    )
    store.put_artifact(orphan_plan, orphan_payload)
    outside = tmp_path / "audit-outside"
    outside.mkdir()
    _directory_link(store.home / "global" / "linked", outside)
    deep_cas = store.home / "artifacts" / "sha256" / "bad" / "deep" / "object"
    deep_cas.parent.mkdir(parents=True)
    deep_cas.write_bytes(b"deep-orphan")
    note_target.write_bytes(note_payload + b"owner: kisi@example.com\n")
    artifact_target.write_bytes(b"tampered")

    issues = store.audit(notes=(note_record,), artifacts=(artifact_record,))
    kinds = {issue.kind for issue in issues}

    assert {
        "corrupt-note",
        "public-projection-unsafe",
        "unmanifested-note",
        "corrupt-cas-object",
        "orphan-cas-object",
        "unsafe-note-path",
        "unsafe-cas-path",
    } <= kinds


def test_archive_is_restart_idempotent_and_detects_duplicate_authority(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    payload = b"# Arsivlenecek\n"
    manifest = _user_manifest(payload)
    source = store.create_note(manifest, payload)
    archived_ref = store.archive_note(manifest)
    assert not source.exists()
    assert (store.home / archived_ref).read_bytes() == payload
    assert store.archive_note(manifest) == archived_ref

    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    assert store.archive_note(manifest) == archived_ref
    assert not source.exists()


def test_inbox_note_has_explicit_owner_and_archives_without_losing_content(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    payload = b"# Inbox candidate\n"
    manifest = KnowledgeNoteManifest(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="idea",
        authorship="user",
        classification=KnowledgeClassification.LOCAL_PRIVATE,
        portable_ref="inbox/user/akilli-kasa/candidate.md",
        content_digest=note_content_digest(payload),
        project_slug="akilli-kasa",
        state="inbox",
    )

    store.create_note(manifest, payload)
    archived_ref = store.archive_note(manifest)

    assert archived_ref.startswith(f"archive/project/{PROJECT_ID}/")
    assert (store.home / archived_ref).read_bytes() == payload


def test_atomic_write_handles_short_writes_and_cleans_failed_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _file_store(tmp_path)
    payload = b"0123456789" * 10
    plan = ArtifactPutPlan.create(
        payload,
        media_type="application/octet-stream",
        classification=KnowledgeClassification.INTERNAL,
    )
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda descriptor, data: real_write(descriptor, data[:3]))
    assert store.put_artifact(plan, payload).read_bytes() == payload

    failed_payload = b"will-fail"
    failed_plan = ArtifactPutPlan.create(
        failed_payload,
        media_type="application/octet-stream",
        classification=KnowledgeClassification.INTERNAL,
    )
    monkeypatch.setattr(os, "write", lambda _descriptor, _data: 0)
    with pytest.raises(OSError, match="short write"):
        store.put_artifact(failed_plan, failed_payload)
    assert not (store.home / failed_plan.relative_path).exists()
    assert not tuple((store.home / failed_plan.relative_path).parent.glob("*.stage-*"))


def test_symlink_parent_and_binary_or_empty_note_are_rejected(tmp_path: Path) -> None:
    store = _file_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = store.home / "projeler" / "linked"
    _directory_link(link, outside)
    manifest = KnowledgeNoteManifest(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="note",
        authorship="user",
        classification=KnowledgeClassification.INTERNAL,
        portable_ref="projeler/linked/notlar/user/a.md",
        content_digest=note_content_digest(b"a"),
        project_slug="linked",
    )
    with pytest.raises(LayoutError, match="symlink"):
        store.create_note(manifest, b"a")
    for payload in (b"", b"\xff"):
        with pytest.raises(ValidationFailed):
            note_content_digest(payload)


def test_public_safe_wikilink_projection_filters_private_targets_and_scans_pii() -> None:
    public = WikiLinkNote(
        note_id=NOTE_A,
        realm_id=REALM_ID,
        owner_scope=f"project:{PROJECT_ID}",
        project_slug="akilli-kasa",
        authorship="generated",
        title="Offline Odeme",
        portable_ref="projeler/akilli-kasa/notlar/generated/offline.md",
        classification=KnowledgeClassification.PUBLIC,
        content_digest=digest("public-note"),
    )
    public_target = WikiLinkNote(
        note_id=NOTE_B,
        realm_id=REALM_ID,
        owner_scope=f"project:{PROJECT_ID}",
        project_slug="akilli-kasa",
        authorship="generated",
        title="Kuyruk",
        portable_ref="projeler/akilli-kasa/notlar/generated/kuyruk.md",
        classification=KnowledgeClassification.PUBLIC,
        content_digest=digest("public-target"),
    )
    private_target = WikiLinkNote(
        note_id="018f0000-0000-7000-8000-00000000000e",
        realm_id=REALM_ID,
        owner_scope=f"project:{PROJECT_ID}",
        project_slug="akilli-kasa",
        authorship="user",
        title="Yerel Not",
        portable_ref="projeler/akilli-kasa/notlar/user/yerel.md",
        classification=KnowledgeClassification.LOCAL_PRIVATE,
        content_digest=digest("private-target"),
    )
    relations = (
        WikiLinkRelation(RELATION_ID, NOTE_A, NOTE_B, "depends-on", digest("r1"), True),
        WikiLinkRelation(
            "018f0000-0000-7000-8000-00000000000f",
            NOTE_A,
            private_target.note_id,
            "related-to",
            digest("r2"),
            True,
        ),
    )
    policy = KnowledgePolicyProfile(REALM_ID, SyncProfile.PUBLIC_SAFE, "public/projection")

    rendered = render_wikilink_projection(
        source_note_id=NOTE_A,
        notes=(public, public_target, private_target),
        relations=relations,
        policy=policy,
    )

    assert b"[[projeler/akilli-kasa/notlar/generated/kuyruk|Kuyruk]]" in rendered
    assert b"Yerel Not" not in rendered
    assert b"grants_authority: false" in rendered
    foreign_target = WikiLinkNote(
        note_id="018f0000-0000-7000-8000-000000000011",
        realm_id="018f0000-0000-7000-8000-000000000012",
        owner_scope=f"project:{PROJECT_ID}",
        project_slug="akilli-kasa",
        authorship="generated",
        title="Foreign",
        portable_ref="projeler/akilli-kasa/notlar/generated/foreign.md",
        classification=KnowledgeClassification.PUBLIC,
        content_digest=digest("foreign"),
    )
    foreign_relation = WikiLinkRelation(
        "018f0000-0000-7000-8000-000000000013",
        NOTE_A,
        foreign_target.note_id,
        "related-to",
        digest("foreign-relation"),
        True,
    )
    with pytest.raises(PolicyViolation, match="cross-realm"):
        render_wikilink_projection(
            source_note_id=NOTE_A,
            notes=(public, foreign_target),
            relations=(foreign_relation,),
            policy=policy,
        )
    with pytest.raises(PolicyViolation, match="secret/PII"):
        assert_public_safe_projection(
            b"owner: kisi@example.com\n", relative_path="public/example.md"
        )
    with pytest.raises(PolicyViolation, match="secret/PII"):
        assert_public_safe_projection(
            b"password = 'real-secret-value'\n", relative_path="public/example.md"
        )


def test_operational_note_relation_archive_are_transactional_and_append_only(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operational.db"
    bootstrap(database)
    store = SQLiteOperationalStore(database)
    payload_a = b"# A\n"
    payload_b = b"# B\n"
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="akilli-kasa", display_name="Akilli Kasa")
        artifact = uow.register_artifact(
            artifact_digest=digest(payload_a.decode()),
            media_type="text/markdown",
            size_bytes=len(payload_a),
            classification="internal",
        )
        first = uow.register_knowledge_note(
            realm_id=REALM_ID,
            project_id=project.id,
            owner_scope=f"project:{project.id}",
            portable_ref="projeler/akilli-kasa/notlar/user/a.md",
            note_kind="note",
            authorship="user",
            classification="internal",
            content_digest=note_content_digest(payload_a),
        )
        second = uow.register_knowledge_note(
            realm_id=REALM_ID,
            project_id=project.id,
            owner_scope=f"project:{project.id}",
            portable_ref="projeler/akilli-kasa/notlar/generated/b.md",
            note_kind="note",
            authorship="generated",
            classification="public",
            content_digest=note_content_digest(payload_b),
        )
        first = uow.confirm_knowledge_note(
            note_id=first.id,
            expected_content_digest=first.content_digest,
            evidence_digest=digest("first-materialized"),
        )
        second = uow.confirm_knowledge_note(
            note_id=second.id,
            expected_content_digest=second.content_digest,
            evidence_digest=digest("second-materialized"),
        )
        relation = uow.relate_knowledge_notes(
            from_note_id=first.id,
            to_note_id=second.id,
            relation_kind="depends-on",
            source_digest=digest("verified-source"),
            verified=True,
        )
        archived = uow.archive_knowledge_note(
            note_id=first.id,
            expected_content_digest=first.content_digest,
            archived_ref=f"archive/project/{project.id}/a.md",
        )
        uow.commit()

    assert artifact.classification == "internal"
    assert relation.verified is True
    assert archived.state == "archived"
    with sqlite3.connect(database) as connection:
        for statement in (
            "update artifact_ref set media_type='text/plain'",
            "delete from knowledge_note",
            "update knowledge_relation set relation_kind='other'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into knowledge_relation(id,from_note_id,to_note_id,relation_kind,"
                "source_digest,verified,created_at) values(?,?,?,?,?,0,?)",
                ("raw", second.id, first.id, "raw", digest("raw"), "now"),
            )


def test_operational_note_duplicate_content_and_half_transaction_are_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operational.db"
    bootstrap(database)
    store = SQLiteOperationalStore(database)
    content = digest("same")
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="akilli-kasa", display_name="Akilli Kasa")
        uow.register_knowledge_note(
            realm_id=REALM_ID,
            project_id=project.id,
            owner_scope=f"project:{project.id}",
            portable_ref="projeler/akilli-kasa/notlar/user/a.md",
            note_kind="note",
            authorship="user",
            classification="internal",
            content_digest=content,
        )
        with pytest.raises(ValidationFailed, match="authority drift"):
            uow.register_knowledge_note(
                realm_id=REALM_ID,
                project_id=project.id,
                owner_scope=f"project:{project.id}",
                portable_ref="projeler/akilli-kasa/notlar/user/b.md",
                note_kind="note",
                authorship="user",
                classification="internal",
                content_digest=content,
            )

    with sqlite3.connect(database) as connection:
        assert connection.execute("select count(*) from knowledge_note").fetchone()[0] == 0
