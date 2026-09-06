from __future__ import annotations

import datetime as dt
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from zekam.application.home import HomeLayout
from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    KnowledgeNoteManifest,
    generated_note_bytes,
    note_content_digest,
)
from zekam.application.markdown_knowledge import (
    list_markdown_knowledge,
    search_markdown_knowledge,
    show_markdown_knowledge,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.unit
REALM_ID = str(uuid5(NAMESPACE_URL, "zekam://realm/yerel"))


def _note(tmp_path: Path) -> tuple[SQLiteOperationalStore, KnowledgeFileStore, str, str]:
    layout = HomeLayout(tmp_path / ".zekam").ensure()
    layout.ensure_project("demo")
    home = layout.root
    bootstrap(home / "state" / "operational.db")
    store = SQLiteOperationalStore(home / "state" / "operational.db")
    files = KnowledgeFileStore(home)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="demo", display_name="Demo")
        uow.commit()
    payload = generated_note_bytes(
        owner_scope=f"project:{project.id}",
        project_slug="demo",
        note_kind="research",
        classification=KnowledgeClassification.INTERNAL,
        source_refs=("research-runs/run-1",),
        source_digests=(digest("source"),),
        generated_at=dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        generator_version="test/v1",
        body="# Sonuc\n\nMusteri servis siniflari burada.",
    )
    manifest = KnowledgeNoteManifest(
        owner_scope=f"project:{project.id}",
        project_slug="demo",
        note_kind="research",
        authorship="generated",
        classification=KnowledgeClassification.INTERNAL,
        portable_ref="projeler/demo/arastirmalar/generated/run-1.md",
        content_digest=note_content_digest(payload),
    )
    with store.unit_of_work() as uow:
        note = uow.register_knowledge_note(
            realm_id=REALM_ID,
            project_id=project.id,
            owner_scope=manifest.owner_scope,
            portable_ref=manifest.portable_ref,
            note_kind=manifest.note_kind,
            authorship=manifest.authorship,
            classification=manifest.classification.value,
            content_digest=manifest.content_digest,
        )
        files.create_note(manifest, payload)
        uow.confirm_knowledge_note(
            note_id=note.id,
            expected_content_digest=manifest.content_digest,
            evidence_digest=digest("materialized"),
        )
        uow.commit()
    return store, files, project.id, note.id


def test_list_show_and_search_are_scope_and_digest_verified(tmp_path: Path) -> None:
    store, files, project_id, note_id = _note(tmp_path)
    with store.unit_of_work() as uow:
        listed = list_markdown_knowledge(uow, project_id=project_id)
        shown = show_markdown_knowledge(uow, files, note_id)
        searched = search_markdown_knowledge(uow, files, "musteri servis", project_id=project_id)
        uow.commit()

    assert listed["count"] == 1
    assert shown["verified"] is True
    assert "Musteri servis" in shown["body"]
    assert searched["count"] == 1
    assert searched["hits"][0]["note"]["note_id"] == note_id


def test_show_rejects_file_content_drift(tmp_path: Path) -> None:
    store, files, _project_id, note_id = _note(tmp_path)
    path = files.home / "projeler/demo/arastirmalar/generated/run-1.md"
    path.write_text("drift", encoding="utf-8")

    with store.unit_of_work() as uow, pytest.raises(ConcurrencyConflict, match="digest drift"):
        show_markdown_knowledge(uow, files, note_id)


def test_show_rejects_note_outside_expected_scope(tmp_path: Path) -> None:
    store, files, project_id, note_id = _note(tmp_path)
    with store.unit_of_work() as uow:
        with pytest.raises(PolicyViolation, match="owner scope"):
            show_markdown_knowledge(
                uow,
                files,
                note_id,
                owner_scope="global-user",
            )
        with pytest.raises(PolicyViolation, match="project scope"):
            show_markdown_knowledge(
                uow,
                files,
                note_id,
                project_id="01900000-0000-7000-8000-000000000099",
            )
        shown = show_markdown_knowledge(
            uow,
            files,
            note_id,
            project_id=project_id,
            owner_scope=f"project:{project_id}",
        )

    assert shown["note_id"] == note_id
