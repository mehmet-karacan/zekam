"""Adversarial branch coverage for the knowledge file-plane auditor."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from zekam.application.home import HomeLayout
from zekam.application.knowledge_file_plane import (
    ArtifactPutPlan,
    KnowledgeClassification,
    KnowledgeNoteManifest,
    note_content_digest,
)
from zekam.application.operational_store import ArtifactRefRecord, KnowledgeNoteRecord
from zekam.domain.canonical import digest
from zekam.domain.errors import ConcurrencyConflict, LayoutError, PolicyViolation
from zekam.infrastructure import knowledge_files
from zekam.infrastructure.knowledge_files import KnowledgeFileStore

pytestmark = pytest.mark.unit

PROJECT_ID = "018f0000-0000-7000-8000-000000000001"
REALM_ID = "018f0000-0000-7000-8000-000000000002"


def _store(tmp_path: Path) -> KnowledgeFileStore:
    home = tmp_path / "home"
    HomeLayout(home).ensure().ensure_project("akilli-kasa")
    return KnowledgeFileStore(home)


def _note(
    *,
    note_id: str = "018f0000-0000-7000-8000-000000000003",
    portable_ref: str = "projeler/akilli-kasa/notlar/user/note.md",
    classification: str = "internal",
    content_digest: str | None = None,
    state: str = "active",
    materialized: bool = True,
    archived_ref: str | None = None,
) -> KnowledgeNoteRecord:
    return KnowledgeNoteRecord(
        note_id,
        f"project:{PROJECT_ID}",
        portable_ref,
        "note",
        "user",
        classification,
        content_digest or digest("missing"),
        state,
        REALM_ID,
        PROJECT_ID,
        "akilli-kasa",
        materialized,
        archived_ref,
    )


def _manifest(
    payload: bytes,
    *,
    classification: KnowledgeClassification = KnowledgeClassification.INTERNAL,
    state: str = "active",
) -> KnowledgeNoteManifest:
    return KnowledgeNoteManifest(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="note",
        authorship="user",
        classification=classification,
        portable_ref="projeler/akilli-kasa/notlar/user/branch.md",
        content_digest=note_content_digest(payload),
        project_slug="akilli-kasa",
        state=state,
    )


def test_store_rejects_non_directory_relative_and_symlink_homes(tmp_path: Path) -> None:
    relative = Path("relative-home")
    regular = tmp_path / "regular"
    regular.write_text("not a directory", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)

    for candidate in (relative, regular, linked):
        with pytest.raises(LayoutError, match="regular directory"):
            KnowledgeFileStore(candidate)


def test_atomic_replace_and_publish_identity_drift_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    target = store.home / "inbox" / "user" / "replace.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old")
    assert (
        store._atomic_write("inbox/user/replace.md", b"new", replace_existing=True).read_bytes()
        == b"new"
    )

    monkeypatch.setattr(store, "_read_optional", lambda *_args, **_kwargs: b"drift")
    with pytest.raises(LayoutError, match="publish path identity drift"):
        store._atomic_write("inbox/user/drift.md", b"expected", replace_existing=False)


def test_put_artifact_detects_plan_disk_and_race_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    payload = b"artifact"
    plan = ArtifactPutPlan.create(
        payload,
        media_type="application/octet-stream",
        classification=KnowledgeClassification.INTERNAL,
    )
    with pytest.raises(ConcurrencyConflict, match="plan payload drift"):
        store.put_artifact(replace(plan, size_bytes=plan.size_bytes + 1), payload)

    target = store.home / plan.relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"corrupt")
    with pytest.raises(ConcurrencyConflict, match="CAS target digest drift"):
        store.put_artifact(plan, payload)

    target.unlink()
    monkeypatch.setattr(
        store,
        "_atomic_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConcurrencyConflict("race")),
    )
    monkeypatch.setattr(store, "_read_optional", lambda *_args, **_kwargs: payload)
    assert store.put_artifact(plan, payload) == store.home / plan.relative_path
    monkeypatch.setattr(store, "_read_optional", lambda *_args, **_kwargs: None)
    with pytest.raises(ConcurrencyConflict, match="race"):
        store.put_artifact(plan, payload)


def test_create_note_rejects_secret_digest_overwrite_and_race_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    payload = b"# note\n"
    with pytest.raises(PolicyViolation, match="secret backend"):
        store.create_note(
            _manifest(payload, classification=KnowledgeClassification.SECRET), payload
        )
    with pytest.raises(ConcurrencyConflict, match="content digest drift"):
        store.create_note(_manifest(payload), b"# changed\n")

    manifest = _manifest(payload)
    target = store.home / manifest.portable_ref
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"different")
    with pytest.raises(PolicyViolation, match="overwrite"):
        store.create_note(manifest, payload)

    target.unlink()
    monkeypatch.setattr(
        store,
        "_atomic_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConcurrencyConflict("race")),
    )
    race_reads = iter((None, b"other"))
    monkeypatch.setattr(store, "_read_optional", lambda *_args, **_kwargs: next(race_reads))
    with pytest.raises(ConcurrencyConflict, match="race"):
        store.create_note(manifest, payload)


def test_archive_rejects_invalid_state_and_each_digest_drift(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = b"# archive\n"
    manifest = _manifest(payload)
    with pytest.raises(PolicyViolation, match="archive edilebilir"):
        store.archive_note(replace(manifest, state="archived"))

    target_ref = f"archive/project/{PROJECT_ID}/{manifest.content_digest[7:]}-branch.md"
    target = store.home / target_ref
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"bad target")
    with pytest.raises(ConcurrencyConflict, match="target content digest drift"):
        store.archive_note(manifest)

    target.write_bytes(payload)
    source = store.home / manifest.portable_ref
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"bad source")
    with pytest.raises(ConcurrencyConflict, match="duplicate source content digest drift"):
        store.archive_note(manifest)

    target.unlink()
    with pytest.raises(ConcurrencyConflict, match="source content digest drift"):
        store.archive_note(manifest)


def test_audit_fail_closed_note_manifest_and_duplicate_paths(tmp_path: Path) -> None:
    store = _store(tmp_path)
    duplicate = _note(materialized=False)
    invalid = _note(
        note_id="018f0000-0000-7000-8000-000000000004",
        portable_ref="../escape.md",
        classification="not-a-classification",
    )
    outside_archive = _note(
        note_id="018f0000-0000-7000-8000-000000000005",
        state="archived",
        archived_ref="projeler/akilli-kasa/notlar/user/not-archive.md",
    )

    kinds = {
        issue.kind
        for issue in store.audit(
            notes=(duplicate, duplicate, invalid, outside_archive), artifacts=()
        )
    }

    assert {
        "pending-note-materialization",
        "duplicate-note-ref",
        "invalid-note-manifest",
        "missing-note",
    } <= kinds


def test_audit_classifies_unreadable_secret_and_public_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    secret_payload = b"# secret\n"
    public_payload = b"# public\nemail: owner@example.com\n"
    secret = _note(
        portable_ref="projeler/akilli-kasa/notlar/user/secret.md",
        classification="secret",
        content_digest=note_content_digest(secret_payload),
    )
    public = _note(
        note_id="018f0000-0000-7000-8000-000000000006",
        portable_ref="projeler/akilli-kasa/notlar/user/public.md",
        classification="public",
        content_digest=note_content_digest(public_payload),
    )
    unreadable = _note(
        note_id="018f0000-0000-7000-8000-000000000007",
        portable_ref="projeler/akilli-kasa/notlar/user/unreadable.md",
    )
    for record, payload in ((secret, secret_payload), (public, public_payload)):
        target = store.home / record.portable_ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    real_read = store._read_optional

    def fail_one(relative: str, *, max_bytes: int) -> bytes | None:
        if relative == unreadable.portable_ref:
            raise OSError("forced read failure")
        return real_read(relative, max_bytes=max_bytes)

    monkeypatch.setattr(store, "_read_optional", fail_one)
    kinds = {issue.kind for issue in store.audit(notes=(secret, public, unreadable), artifacts=())}
    assert {
        "secret-in-normal-file-plane",
        "public-projection-unsafe",
        "unreadable-note",
    } <= kinds


def test_audit_bounds_walk_and_rejects_unsafe_note_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    global_root = store.home / "global"
    if global_root.exists():
        global_root.rename(store.home / "global-real")
    global_root.symlink_to(outside, target_is_directory=True)
    inbox = store.home / "inbox" / "user"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "first.md").write_text("# first\n", encoding="utf-8")
    (inbox / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    monkeypatch.setattr(knowledge_files, "_AUDIT_LIMIT", 0)

    kinds = {issue.kind for issue in store.audit(notes=(), artifacts=())}
    assert {"unsafe-note-root", "audit-limit-exceeded"} <= kinds


def test_audit_artifact_duplicate_secret_missing_unreadable_and_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    secret_payload = b"secret artifact"
    secret_digest = f"sha256:{hashlib.sha256(secret_payload).hexdigest()}"
    public_payload = b"email owner@example.com"
    public_digest = f"sha256:{hashlib.sha256(public_payload).hexdigest()}"
    secret_ref = f"artifacts/sha256/{secret_digest[7:9]}/{secret_digest[7:]}"
    secret_target = store.home / secret_ref
    secret_target.parent.mkdir(parents=True, exist_ok=True)
    secret_target.write_bytes(secret_payload)
    public_ref = f"artifacts/sha256/{public_digest[7:9]}/{public_digest[7:]}"
    public_target = store.home / public_ref
    public_target.parent.mkdir(parents=True, exist_ok=True)
    public_target.write_bytes(public_payload)
    secret = ArtifactRefRecord(
        secret_digest,
        "application/octet-stream",
        len(secret_payload),
        KnowledgeClassification.SECRET.value,
    )
    public = ArtifactRefRecord(
        public_digest,
        "text/plain",
        len(public_payload),
        KnowledgeClassification.PUBLIC.value,
    )
    missing = ArtifactRefRecord(digest("absent"), "application/octet-stream", 1, "internal")
    unreadable = ArtifactRefRecord(digest("unreadable"), "application/octet-stream", 1, "internal")
    unreadable_ref = f"artifacts/sha256/{unreadable.digest[7:9]}/{unreadable.digest[7:]}"
    real_read = store._read_optional

    def fail_one(relative: str, *, max_bytes: int) -> bytes | None:
        if relative == unreadable_ref:
            raise OSError("forced CAS read failure")
        return real_read(relative, max_bytes=max_bytes)

    monkeypatch.setattr(store, "_read_optional", fail_one)
    kinds = {
        issue.kind
        for issue in store.audit(notes=(), artifacts=(secret, secret, public, missing, unreadable))
    }
    assert {
        "duplicate-artifact-ref",
        "secret-in-normal-cas",
        "public-cas-unsafe",
        "missing-cas-object",
        "unreadable-cas-object",
    } <= kinds


def test_audit_rejects_cas_root_symlink(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cas_root = store.home / "artifacts" / "sha256"
    if cas_root.exists():
        cas_root.rmdir()
    outside = tmp_path / "outside-cas"
    outside.mkdir()
    cas_root.parent.mkdir(parents=True, exist_ok=True)
    cas_root.symlink_to(outside, target_is_directory=True)

    issues = store.audit(notes=(), artifacts=())
    assert any(issue.kind == "unsafe-cas-root" for issue in issues)


def test_home_identity_drift_and_unlink_missing_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    (store.home / "inbox" / "user").mkdir(parents=True, exist_ok=True)
    store._unlink("inbox/user/missing.md")
    real_fstat = os.fstat

    class _Identity:
        st_dev = -1
        st_ino = -1

    monkeypatch.setattr(os, "fstat", lambda _descriptor: _Identity())
    with pytest.raises(LayoutError, match="identity drift"):
        store._open_home()
    monkeypatch.setattr(os, "fstat", real_fstat)
