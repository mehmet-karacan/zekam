"""Recoverable production lifecycle tests for the WP-05 knowledge plane."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zekam.application.home import HomeLayout
from zekam.application.knowledge_file_plane import (
    ArtifactPutPlan,
    KnowledgeClassification,
    KnowledgeNoteManifest,
    note_content_digest,
)
from zekam.application.knowledge_plane_service import KnowledgePlaneService
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.unit

REALM_ID = "018f0000-0000-7000-8000-00000000000d"


def _runtime(
    tmp_path: Path,
) -> tuple[Path, SQLiteOperationalStore, KnowledgeFileStore, str]:
    database = tmp_path / "operational.db"
    bootstrap(database)
    operational = SQLiteOperationalStore(database)
    with operational.unit_of_work() as uow:
        project = uow.create_project(slug="akilli-kasa", display_name="Akilli Kasa")
        uow.commit()
    home = tmp_path / "home"
    HomeLayout(home).ensure().ensure_project("akilli-kasa")
    return database, operational, KnowledgeFileStore(home), project.id


def _manifest(project_id: str, payload: bytes) -> KnowledgeNoteManifest:
    return KnowledgeNoteManifest(
        owner_scope=f"project:{project_id}",
        note_kind="note",
        authorship="user",
        classification=KnowledgeClassification.LOCAL_PRIVATE,
        portable_ref="projeler/akilli-kasa/notlar/user/recovery.md",
        content_digest=note_content_digest(payload),
        project_slug="akilli-kasa",
    )


def test_materialization_failure_remains_pending_and_restart_replay_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, operational, files, project_id = _runtime(tmp_path)
    payload = b"# Recovery\n\nDurable note.\n"
    manifest = _manifest(project_id, payload)
    original_create = files.create_note
    failed = False

    def fail_once(candidate: KnowledgeNoteManifest, body: bytes) -> Path:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated file-plane interruption")
        return original_create(candidate, body)

    monkeypatch.setattr(files, "create_note", fail_once)
    service = KnowledgePlaneService(operational, files)
    with pytest.raises(OSError, match="interruption"):
        service.materialize_note(
            realm_id=REALM_ID,
            project_id=project_id,
            manifest=manifest,
            payload=payload,
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute("select materialized from knowledge_note").fetchone() == (0,)
    assert not (files.home / manifest.portable_ref).exists()

    restarted = KnowledgePlaneService(
        SQLiteOperationalStore(database), KnowledgeFileStore(files.home)
    )
    recovered = restarted.materialize_note(
        realm_id=REALM_ID,
        project_id=project_id,
        manifest=manifest,
        payload=payload,
    )
    replay = restarted.materialize_note(
        realm_id=REALM_ID,
        project_id=project_id,
        manifest=manifest,
        payload=payload,
    )

    assert recovered.record.materialized is True
    assert replay.record.id == recovered.record.id
    assert recovered.path.read_bytes() == payload
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "select count(*),sum(materialized) from knowledge_note"
        ).fetchone() == (1, 1)


def test_archive_crash_after_file_move_is_idempotently_recovered(tmp_path: Path) -> None:
    database, operational, files, project_id = _runtime(tmp_path)
    payload = b"# Archive recovery\n"
    manifest = _manifest(project_id, payload)
    service = KnowledgePlaneService(operational, files)
    materialized = service.materialize_note(
        realm_id=REALM_ID,
        project_id=project_id,
        manifest=manifest,
        payload=payload,
    )

    archived_ref = files.archive_note(manifest)
    assert not (files.home / manifest.portable_ref).exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute("select state from knowledge_note").fetchone() == ("active",)

    restarted = KnowledgePlaneService(
        SQLiteOperationalStore(database), KnowledgeFileStore(files.home)
    )
    archived = restarted.archive_note(record=materialized.record, manifest=manifest)
    replay = restarted.archive_note(record=archived, manifest=manifest)

    assert archived.state == "archived"
    assert replay.archived_ref == archived_ref
    assert (files.home / archived_ref).read_bytes() == payload
    with sqlite3.connect(database) as connection:
        assert connection.execute("select state,archived_ref from knowledge_note").fetchone() == (
            "archived",
            archived_ref,
        )


def test_artifact_manifest_first_failure_and_restart_replay_repairs_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, operational, files, _ = _runtime(tmp_path)
    payload = b"recoverable artifact"
    plan = ArtifactPutPlan.create(
        payload,
        media_type="application/octet-stream",
        classification=KnowledgeClassification.INTERNAL,
    )
    original_put = files.put_artifact
    failed = False

    def fail_once(candidate: ArtifactPutPlan, body: bytes) -> Path:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated CAS interruption")
        return original_put(candidate, body)

    monkeypatch.setattr(files, "put_artifact", fail_once)
    service = KnowledgePlaneService(operational, files)
    with pytest.raises(OSError, match="CAS interruption"):
        service.put_artifact(plan, payload)
    with sqlite3.connect(database) as connection:
        assert connection.execute("select count(*) from artifact_ref").fetchone() == (1,)
    assert not (files.home / plan.relative_path).exists()

    restarted = KnowledgePlaneService(
        SQLiteOperationalStore(database), KnowledgeFileStore(files.home)
    )
    assert restarted.put_artifact(plan, payload).digest == plan.digest
    assert restarted.put_artifact(plan, payload).digest == plan.digest
    assert (files.home / plan.relative_path).read_bytes() == payload


def test_pending_and_cross_realm_relations_are_rejected(tmp_path: Path) -> None:
    database, operational, _, first_project_id = _runtime(tmp_path)
    with operational.unit_of_work() as uow:
        second_project = uow.create_project(slug="second", display_name="Second")
        pending = uow.register_knowledge_note(
            realm_id=REALM_ID,
            project_id=first_project_id,
            owner_scope=f"project:{first_project_id}",
            portable_ref="projeler/akilli-kasa/notlar/user/pending.md",
            note_kind="note",
            authorship="user",
            classification="internal",
            content_digest=digest("pending"),
        )
        foreign = uow.register_knowledge_note(
            realm_id="018f0000-0000-7000-8000-000000000099",
            project_id=second_project.id,
            owner_scope=f"project:{second_project.id}",
            portable_ref="projeler/second/notlar/user/foreign.md",
            note_kind="note",
            authorship="user",
            classification="internal",
            content_digest=digest("foreign"),
        )
        with pytest.raises(ValidationFailed, match="active same-realm"):
            uow.relate_knowledge_notes(
                from_note_id=pending.id,
                to_note_id=foreign.id,
                relation_kind="related-to",
                source_digest=digest("pending-relation"),
                verified=True,
            )
        uow.confirm_knowledge_note(
            note_id=pending.id,
            expected_content_digest=pending.content_digest,
            evidence_digest=digest("pending-ready"),
        )
        uow.confirm_knowledge_note(
            note_id=foreign.id,
            expected_content_digest=foreign.content_digest,
            evidence_digest=digest("foreign-ready"),
        )
        with pytest.raises(ValidationFailed, match="active same-realm"):
            uow.relate_knowledge_notes(
                from_note_id=pending.id,
                to_note_id=foreign.id,
                relation_kind="related-to",
                source_digest=digest("cross-realm"),
                verified=True,
            )
        uow.commit()

    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="active same-realm"),
    ):
        connection.execute(
            "insert into knowledge_relation(id,from_note_id,to_note_id,relation_kind,"
            "source_digest,verified,created_at) values(?,?,?,?,?,1,?)",
            ("raw-cross-realm", pending.id, foreign.id, "related-to", digest("raw"), "now"),
        )
