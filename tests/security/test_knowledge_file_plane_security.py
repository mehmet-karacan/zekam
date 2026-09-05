"""WP-05 adversarial owner, privacy, filesystem and raw-SQL boundary tests."""

from __future__ import annotations

import os
import secrets
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zekam.application.home import HomeLayout
from zekam.application.knowledge_file_plane import (
    ArtifactPutPlan,
    KnowledgeClassification,
    KnowledgeNoteManifest,
    KnowledgePolicyProfile,
    SyncProfile,
    assert_public_safe_projection,
    note_content_digest,
    validate_owner_scope,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import LayoutError, PolicyViolation, ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileIssue, KnowledgeFileStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.security

PROJECT_ID = "018f0000-0000-7000-8000-000000000001"
REALM_ID = "018f0000-0000-7000-8000-000000000002"


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


@pytest.mark.parametrize(
    "scope",
    [
        None,
        "",
        "global",
        "project:",
        "project:not-a-uuid",
        "project:018f0000-0000-7000-8000-000000000001-extra",
        "tenant:018f0000-0000-7000-8000-000000000001",
        f"project:{PROJECT_ID}:other",
    ],
)
def test_owner_scope_parser_is_exact(scope: object) -> None:
    with pytest.raises(ValidationFailed):
        validate_owner_scope(scope)


@pytest.mark.parametrize(
    "payload",
    [
        b"owner: user@example.com\n",
        b"phone: +90 532 123 45 67\n",
        b"path: /Users/private/project\n",
        b"pass" + b"word = 'p9x7m2q4v8n6'\n",
        b"Author" + b"ization: Bearer abcdefghijklmnopqrstuvwxyz\n",
        b"postgresql://person:unsafe-password@database.example/app\n",
        b"TCKN: 10000000146\n",
        b"IBAN: TR330006100519786457841326\n",
        b"card: 4111 1111 1111 1111\n",
    ],
)
def test_public_projection_rejects_pii_secret_path_and_credentials(payload: bytes) -> None:
    with pytest.raises(PolicyViolation, match="secret/PII"):
        assert_public_safe_projection(payload, relative_path="public/projection.md")


def test_sync_profile_is_fail_closed_for_wrong_classification() -> None:
    public = KnowledgePolicyProfile(
        "018f0000-0000-7000-8000-000000000002",
        SyncProfile.PUBLIC_SAFE,
        "public/projection",
    )
    disabled = KnowledgePolicyProfile(
        "018f0000-0000-7000-8000-000000000003",
        SyncProfile.NONE,
        "disabled/projection",
    )
    public.assert_projection_allowed(KnowledgeClassification.PUBLIC)
    for classification in (
        KnowledgeClassification.INTERNAL,
        KnowledgeClassification.CONFIDENTIAL_CORPORATE,
        KnowledgeClassification.RESTRICTED,
        KnowledgeClassification.SECRET,
        KnowledgeClassification.LOCAL_PRIVATE,
    ):
        with pytest.raises(PolicyViolation):
            public.assert_projection_allowed(classification)
    with pytest.raises(PolicyViolation):
        disabled.assert_projection_allowed(KnowledgeClassification.PUBLIC)


def test_text_artifact_and_public_note_fail_closed_before_persistence(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation, match="secret taramasini"):
        ArtifactPutPlan.create(
            b"api_" + b"key = 'a8b7c6d5e4f3'\n",
            media_type="text/plain",
            classification=KnowledgeClassification.INTERNAL,
        )
    with pytest.raises(PolicyViolation, match="secret/PII"):
        ArtifactPutPlan.create(
            b"owner@example.com\n",
            media_type="application/octet-stream",
            classification=KnowledgeClassification.PUBLIC,
        )
    home = tmp_path / "home"
    HomeLayout(home).ensure().ensure_project("akilli-kasa")
    store = KnowledgeFileStore(home)
    payload = b"# Public\n\nowner@example.com\n"
    manifest = KnowledgeNoteManifest(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="note",
        authorship="user",
        classification=KnowledgeClassification.PUBLIC,
        portable_ref="projeler/akilli-kasa/notlar/user/public.md",
        content_digest=note_content_digest(payload),
        project_slug="akilli-kasa",
    )
    with pytest.raises(PolicyViolation, match="secret/PII"):
        store.create_note(manifest, payload)
    assert not (home / manifest.portable_ref).exists()


def test_note_path_with_spaces_and_non_ascii_is_portable_on_macos(tmp_path: Path) -> None:
    home = tmp_path / "home with spaces"
    HomeLayout(home).ensure().ensure_project("akilli-kasa")
    store = KnowledgeFileStore(home)
    payload = "# Ödeme terminali\n".encode()
    manifest = KnowledgeNoteManifest(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="note",
        authorship="user",
        classification=KnowledgeClassification.LOCAL_PRIVATE,
        portable_ref="projeler/akilli-kasa/notlar/user/ödeme notu.md",
        content_digest=note_content_digest(payload),
        project_slug="akilli-kasa",
    )

    assert store.create_note(manifest, payload).read_bytes() == payload


def test_existing_symlink_leaf_and_parent_never_escape_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    HomeLayout(home).ensure().ensure_project("akilli-kasa")
    store = KnowledgeFileStore(home)
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = b"# no escape\n"
    parent_link = home / "projeler" / "akilli-kasa" / "notlar" / "user"
    _directory_link(parent_link, outside)
    manifest = KnowledgeNoteManifest(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="note",
        authorship="user",
        classification=KnowledgeClassification.INTERNAL,
        portable_ref="projeler/akilli-kasa/notlar/user/a.md",
        content_digest=note_content_digest(payload),
        project_slug="akilli-kasa",
    )

    with pytest.raises(LayoutError, match="symlink"):
        store.create_note(manifest, payload)
    assert not (outside / "a.md").exists()


def test_parent_swap_after_handle_open_cannot_write_outside_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    HomeLayout(home).ensure().ensure_project("akilli-kasa")
    store = KnowledgeFileStore(home)
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = b"# pinned parent handle\n"
    manifest = KnowledgeNoteManifest(
        owner_scope=f"project:{PROJECT_ID}",
        note_kind="note",
        authorship="user",
        classification=KnowledgeClassification.INTERNAL,
        portable_ref="projeler/akilli-kasa/notlar/user/a.md",
        content_digest=note_content_digest(payload),
        project_slug="akilli-kasa",
    )
    user_root = home / "projeler" / "akilli-kasa" / "notlar" / "user"
    user_root.mkdir()
    displaced = user_root.with_name("user-displaced")
    called = False

    def swap_parent(_length: int) -> str:
        nonlocal called
        if not called:
            called = True
            user_root.rename(displaced)
            _directory_link(user_root, outside)
        return "a" * 24

    monkeypatch.setattr(secrets, "token_hex", swap_parent)
    with pytest.raises(LayoutError, match=r"symlink|identity drift"):
        store.create_note(manifest, payload)

    assert not (outside / "a.md").exists()
    if os.name == "nt":
        assert not (displaced / "a.md").exists()
    else:
        assert (displaced / "a.md").read_bytes() == payload


def test_audit_reports_replaced_note_and_cas_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    HomeLayout(home).ensure().ensure_project("akilli-kasa")
    store = KnowledgeFileStore(home)
    outside = tmp_path / "outside-audit-root"
    outside.mkdir()

    global_root = home / "global"
    global_root.rename(home / "global-displaced")
    _directory_link(global_root, outside)

    cas_root = home / "artifacts" / "sha256"
    cas_root.mkdir(parents=True, exist_ok=True)
    cas_root.rename(home / "artifacts" / "sha256-displaced")
    _directory_link(cas_root, outside)

    issues = store.audit(notes=(), artifacts=())

    assert KnowledgeFileIssue("unsafe-note-root", "global") in issues
    assert KnowledgeFileIssue("unsafe-cas-root", "artifacts/sha256") in issues


def _database(tmp_path: Path) -> tuple[Path, SQLiteOperationalStore]:
    path = tmp_path / "operational.db"
    bootstrap(path)
    return path, SQLiteOperationalStore(path)


def _two_notes(store: SQLiteOperationalStore) -> tuple[str, str, str]:
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="akilli-kasa", display_name="Akilli Kasa")
        first = uow.register_knowledge_note(
            realm_id=REALM_ID,
            project_id=project.id,
            owner_scope=f"project:{project.id}",
            portable_ref="projeler/akilli-kasa/notlar/user/a.md",
            note_kind="note",
            authorship="user",
            classification="internal",
            content_digest=digest("a"),
        )
        second = uow.register_knowledge_note(
            realm_id=REALM_ID,
            project_id=project.id,
            owner_scope=f"project:{project.id}",
            portable_ref="projeler/akilli-kasa/notlar/generated/b.md",
            note_kind="note",
            authorship="generated",
            classification="public",
            content_digest=digest("b"),
        )
        uow.confirm_knowledge_note(
            note_id=first.id,
            expected_content_digest=first.content_digest,
            evidence_digest=digest("security-first-materialized"),
        )
        uow.confirm_knowledge_note(
            note_id=second.id,
            expected_content_digest=second.content_digest,
            evidence_digest=digest("security-second-materialized"),
        )
        uow.commit()
    return first.id, second.id, project.id


def test_raw_sql_cannot_bypass_owner_authorship_or_verified_relation(tmp_path: Path) -> None:
    path, store = _database(tmp_path)
    first, second, project_id = _two_notes(store)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into knowledge_note(id,realm_id,project_id,project_slug,owner_scope,"
                "portable_ref,note_kind,authorship,classification,content_digest,materialized,"
                "materialization_evidence_digest,state,archived_ref,created_at,updated_at) "
                "values(?,?,?,?,?,?,?,?,?,?,1,?,'active',null,?,?)",
                (
                    "invalid-owner",
                    REALM_ID,
                    None,
                    None,
                    "global-user",
                    "projeler/akilli-kasa/notlar/user/invalid.md",
                    "note",
                    "user",
                    "public",
                    digest("invalid-owner"),
                    digest("invalid-owner-materialized"),
                    "now",
                    "now",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into knowledge_note(id,realm_id,project_id,project_slug,owner_scope,"
                "portable_ref,note_kind,authorship,classification,content_digest,materialized,"
                "materialization_evidence_digest,state,archived_ref,created_at,updated_at) "
                "values(?,?,?,?,?,?,?,?,?,?,1,?,'active',null,?,?)",
                (
                    "invalid-scope",
                    REALM_ID,
                    project_id,
                    "akilli-kasa",
                    "project:not-a-uuid",
                    "projeler/akilli-kasa/notlar/user/invalid-scope.md",
                    "note",
                    "user",
                    "public",
                    digest("invalid-scope"),
                    digest("invalid-scope-materialized"),
                    "now",
                    "now",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into knowledge_note(id,realm_id,project_id,project_slug,owner_scope,"
                "portable_ref,note_kind,authorship,classification,content_digest,materialized,"
                "materialization_evidence_digest,state,archived_ref,created_at,updated_at) "
                "values(?,?,?,?,?,?,?,?,?,?,1,?,'active',null,?,?)",
                (
                    "mixed-authorship",
                    REALM_ID,
                    project_id,
                    "akilli-kasa",
                    f"project:{project_id}",
                    "projeler/akilli-kasa/notlar/user/generated/raw.md",
                    "note",
                    "user",
                    "public",
                    digest("mixed-authorship"),
                    digest("mixed-authorship-materialized"),
                    "now",
                    "now",
                ),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="exact single authorship segment required"
        ):
            connection.execute(
                "insert into knowledge_note(id,realm_id,project_id,project_slug,owner_scope,"
                "portable_ref,note_kind,authorship,classification,content_digest,materialized,"
                "materialization_evidence_digest,state,archived_ref,created_at,updated_at) "
                "values(?,?,?,?,?,?,?,?,?,?,1,?,'active',null,?,?)",
                (
                    "duplicate-authorship",
                    REALM_ID,
                    project_id,
                    "akilli-kasa",
                    f"project:{project_id}",
                    "projeler/akilli-kasa/notlar/user/nested/user/raw.md",
                    "note",
                    "user",
                    "public",
                    digest("duplicate-authorship"),
                    digest("duplicate-authorship-materialized"),
                    "now",
                    "now",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into knowledge_relation(id,from_note_id,to_note_id,relation_kind,"
                "source_digest,verified,created_at) values(?,?,?,?,?,0,?)",
                ("unverified", first, second, "related-to", digest("source"), "now"),
            )


def test_raw_sql_relation_to_archived_note_is_rejected(tmp_path: Path) -> None:
    path, store = _database(tmp_path)
    first, second, project_id = _two_notes(store)
    with store.unit_of_work() as uow:
        with pytest.raises(ValidationFailed, match="owner scope"):
            uow.archive_knowledge_note(
                note_id=first,
                expected_content_digest=digest("a"),
                archived_ref=f"archive/project/{PROJECT_ID}/wrong.md",
            )
        uow.archive_knowledge_note(
            note_id=first,
            expected_content_digest=digest("a"),
            archived_ref=f"archive/project/{project_id}/a.md",
        )
        uow.commit()

    with (
        sqlite3.connect(path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="active same-realm notes required"),
    ):
        connection.execute(
            "insert into knowledge_relation(id,from_note_id,to_note_id,relation_kind,"
            "source_digest,verified,created_at) values(?,?,?,?,?,1,?)",
            ("archived", first, second, "related-to", digest("source"), "now"),
        )


def test_work_run_and_session_owner_cannot_cross_project_boundary(tmp_path: Path) -> None:
    path, store = _database(tmp_path)
    with store.unit_of_work() as uow:
        project_a = uow.create_project(slug="project-a", display_name="Project A")
        project_b = uow.create_project(slug="project-b", display_name="Project B")
        work = uow.create_work(
            project_id=project_a.id,
            kind="task",
            title="Owned by A",
            state="ready",
        )
        config = uow.activate_config(
            config_digest=digest({}),
            task_digest=digest("owner-boundary-task"),
            sanitized_config={},
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            plan_digest=digest("owner-boundary-plan"),
            budget={},
        )
        session = uow.open_session(
            client_id="test-client", device_id="test-device", project_id=project_a.id
        )
        uow.register_knowledge_note(
            realm_id=REALM_ID,
            project_id=project_b.id,
            owner_scope=f"project:{project_b.id}",
            portable_ref="projeler/project-b/notlar/user/realm-binding.md",
            note_kind="note",
            authorship="user",
            classification="internal",
            content_digest=digest("project-b-realm-binding"),
        )
        owner_scopes = (f"work:{work.id}", f"run:{run.id}", f"session:{session.id}")
        for index, owner_scope in enumerate(owner_scopes):
            with pytest.raises(ValidationFailed, match="exact project binding"):
                uow.register_knowledge_note(
                    realm_id=REALM_ID,
                    project_id=project_b.id,
                    owner_scope=owner_scope,
                    portable_ref=f"projeler/project-b/notlar/user/cross-{index}.md",
                    note_kind="note",
                    authorship="user",
                    classification="internal",
                    content_digest=digest(f"cross-project-{index}"),
                )
        uow.commit()

    with sqlite3.connect(path) as connection:
        for index, owner_scope in enumerate(owner_scopes):
            with pytest.raises(
                sqlite3.IntegrityError, match="exact owner/project binding required"
            ):
                connection.execute(
                    "insert into knowledge_note(id,realm_id,project_id,project_slug,owner_scope,"
                    "portable_ref,note_kind,authorship,classification,content_digest,materialized,"
                    "materialization_evidence_digest,state,archived_ref,created_at,updated_at) "
                    "values(?,?,?,?,?,?,?,?,?,?,0,null,'active',null,?,?)",
                    (
                        f"raw-cross-project-{index}",
                        REALM_ID,
                        project_b.id,
                        "project-b",
                        owner_scope,
                        f"projeler/project-b/notlar/user/raw-cross-{index}.md",
                        "note",
                        "user",
                        "internal",
                        digest(f"raw-cross-project-{index}"),
                        "now",
                        "now",
                    ),
                )


def test_operational_boundary_rejects_secret_file_and_cas_refs(tmp_path: Path) -> None:
    path, store = _database(tmp_path)
    with store.unit_of_work() as uow:
        with pytest.raises(ValidationFailed, match="Secret artifact"):
            uow.register_artifact(
                artifact_digest=digest("secret-artifact"),
                media_type="application/octet-stream",
                size_bytes=1,
                classification="secret",
            )
        with pytest.raises(ValidationFailed, match="Secret note"):
            uow.register_knowledge_note(
                realm_id=REALM_ID,
                project_id=None,
                owner_scope="global-user",
                portable_ref="global/referanslar/user/secret.md",
                note_kind="note",
                authorship="user",
                classification="secret",
                content_digest=digest("secret-note"),
            )
    with (
        sqlite3.connect(path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="secret backend"),
    ):
        connection.execute(
            "insert into artifact_ref(digest,media_type,size_bytes,classification,created_at)"
            " values(?,?,?,'secret',?)",
            (digest("raw-secret"), "text/plain", 1, "now"),
        )
    with sqlite3.connect(path) as connection:
        for media_type, size in (("", 1), ("TEXT/PLAIN", 1), ("text/plain", 67108865)):
            with pytest.raises(sqlite3.IntegrityError, match="media/size contract"):
                connection.execute(
                    "insert into artifact_ref(digest,media_type,size_bytes,classification,"
                    "created_at) values(?,?,?,'internal',?)",
                    (digest(f"{media_type}:{size}"), media_type, size, "now"),
                )


def test_concurrent_identical_note_registration_has_one_identity(tmp_path: Path) -> None:
    _, store = _database(tmp_path)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="akilli-kasa", display_name="Akilli Kasa")
        uow.commit()

    def register(_: int) -> str:
        with store.unit_of_work() as uow:
            note = uow.register_knowledge_note(
                realm_id="018f0000-0000-7000-8000-000000000002",
                project_id=project.id,
                owner_scope=f"project:{project.id}",
                portable_ref="projeler/akilli-kasa/notlar/user/concurrent.md",
                note_kind="note",
                authorship="user",
                classification="internal",
                content_digest=digest("same-concurrent-content"),
            )
            uow.commit()
        return note.id

    with ThreadPoolExecutor(max_workers=8) as executor:
        identities = tuple(executor.map(register, range(24)))

    assert len(set(identities)) == 1
