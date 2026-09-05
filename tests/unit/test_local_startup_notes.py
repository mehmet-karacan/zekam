"""Independent note-source integration over real Akilli Kasa content and disposable homes."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from typing import Any, cast
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity_bridge_close import _stage
from tests.unit.test_local_continuity_startup import NOW, ROOT, SOURCE_REF, _request
from tests.unit.test_local_continuity_startup import startup as startup

from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    KnowledgeNoteManifest,
    generated_note_bytes,
    note_content_digest,
)
from zekam.application.knowledge_plane_service import KnowledgePlaneService
from zekam.application.local_continuity_startup import LocalStartupService
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import AuthorityLevel
from zekam.domain.errors import LayoutError, PolicyViolation, ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver
from zekam.infrastructure.sqlite.local_startup_notes import SQLiteStartupNoteSource
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest


@pytest.fixture
def notes(startup: dict[str, Any]) -> dict[str, Any]:
    files = KnowledgeFileStore(startup["home"])
    source = SQLiteStartupNoteSource(startup["base"], files)
    binding = startup["binding"]
    project = ProjectContinuitySourceResolver(
        ROOT,
        project_id=binding.project_id,
        realm_id=binding.realm_id,
        source_snapshot_id=binding.source_snapshot_id,
        allowed_paths=(SOURCE_REF,),
    )
    composite = SQLiteStartupSourceResolver(startup["base"], project, note_sources=source)
    startup["base"].source_resolver = composite
    return startup | {
        "files": files,
        "notes": source,
        "sources": composite,
        "plane": KnowledgePlaneService(startup["operational"], files),
        "service": LocalStartupService(startup["lifecycle"], composite),
    }


def _note(
    notes: dict[str, Any],
    *,
    scope: str = "global-user",
    name: str = "health",
    kind: str = "note",
    state: str = "active",
    authorship: str = "user",
    payload: bytes | None = None,
    realm: str | None = None,
    materialize: bool = True,
) -> tuple[Any, KnowledgeNoteManifest, bytes]:
    binding = notes["binding"]
    owner_fields = {"project": "project_id", "work": "work_item_id", "run": "run_id"}
    owner = scope if scope == "global-user" else f"{scope}:{getattr(binding, owner_fields[scope])}"
    prefix = "global" if scope == "global-user" else "projeler/akilli-kasa"
    project_id = None if scope == "global-user" else binding.project_id
    project_slug = None if scope == "global-user" else "akilli-kasa"
    body = f"# Akilli Kasa {name}\n\nSource: {SOURCE_REF}\n\n{notes['text']}"
    if payload is None:
        if authorship == "generated":
            payload = generated_note_bytes(
                owner_scope=owner,
                note_kind=kind,
                classification=KnowledgeClassification.LOCAL_PRIVATE,
                source_refs=(SOURCE_REF,),
                source_digests=(digest(notes["text"]),),
                generated_at="2026-09-02T18:00:00Z",
                generator_version="wp08-note-test/v1",
                body=body,
                project_slug=project_slug,
            )
        else:
            payload = body.encode("utf-8")
    manifest = KnowledgeNoteManifest(
        owner,
        kind,
        authorship,
        KnowledgeClassification.LOCAL_PRIVATE,
        f"{prefix}/{authorship}/notes/{name}.md",
        note_content_digest(payload),
        project_slug,
        state,
    )
    if materialize:
        materialized = notes["plane"].materialize_note(
            realm_id=realm or binding.realm_id,
            project_id=project_id,
            manifest=manifest,
            payload=payload,
        )
        record = materialized.record
    else:
        with notes["operational"].unit_of_work() as uow:
            record = uow.register_knowledge_note(
                realm_id=realm or binding.realm_id,
                project_id=project_id,
                owner_scope=owner,
                portable_ref=manifest.portable_ref,
                note_kind=kind,
                authorship=authorship,
                classification=manifest.classification.value,
                content_digest=manifest.content_digest,
                state=state,
            )
            uow.commit()
    return record, manifest, payload


def _candidate(notes: dict[str, Any]) -> tuple[Any, str]:
    values = notes["notes"].candidates(notes["binding"], observed_at=NOW)
    assert len(values) == 1
    return cast(tuple[Any, str], values[0])


def _manifest(notes: dict[str, Any], value: str) -> dict[str, Any]:
    with sqlite3.connect(notes["path"]) as db:
        return cast(
            dict[str, Any],
            json.loads(
                db.execute(
                    "select body_json from context_manifest where manifest_digest=?",
                    (value,),
                ).fetchone()[0]
            ),
        )


def test_actual_global_project_work_notes_are_observed_evidence_not_learned_facts(
    notes: dict[str, Any],
) -> None:
    created = [
        _note(notes, scope=scope, name=scope) for scope in ("global-user", "project", "work")
    ]
    before = logical_database_digest(notes["path"])
    candidates = notes["notes"].candidates(notes["binding"], observed_at=NOW)
    assert len(candidates) == 3
    assert {item.scope_ref for item, _text in candidates} == {
        "global-user",
        f"project/{notes['binding'].project_id}",
        f"work/{notes['binding'].work_item_id}",
    }
    for candidate, text in candidates:
        assert candidate.authority is AuthorityLevel.OBSERVED
        assert candidate.required is False
        body = json.loads(text)
        assert body["evidence_only"] is True
        assert body["grants_authority"] is False
        assert body["learned_activation"] == "not-established"
        assert notes["text"] in body["content"]
        assert notes["notes"](notes["binding"], candidate.provenance_body) == text
    global_record = created[0][0]
    assert global_record.project_id is None and global_record.project_slug is None
    assert logical_database_digest(notes["path"]) == before


def test_global_note_hydration_has_truthful_scope_and_explicit_opt_in(
    notes: dict[str, Any],
) -> None:
    _note(notes)
    result = notes["service"].hydrate(_request(note_limit=1))
    body = _manifest(notes, result["manifest_digest"])["context"]
    assert body["ranking_request"]["additional_scope_refs"] == ["global-user"]
    selected = [p for p in body["selected_provenance"] if p["kind"] == "knowledge"]
    assert len(selected) == 1 and selected[0]["scope_ref"] == "global-user"
    assert selected[0]["authority"] == int(AuthorityLevel.OBSERVED)
    assert result["learned_state"] == "not-implemented"
    assert (
        notes["service"].hydrate(_request(note_limit=1))["manifest_digest"]
        == result["manifest_digest"]
    )


def test_note_limit_zero_never_opens_any_note_or_vault(
    notes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _note(notes)
    monkeypatch.setattr(
        notes["files"], "_read_optional", lambda *_a, **_k: pytest.fail("Note preload forbidden")
    )
    result = notes["service"].hydrate(_request(note_limit=0))
    body = _manifest(notes, result["manifest_digest"])["context"]
    assert "additional_scope_refs" not in body["ranking_request"]
    assert all(p["kind"] != "knowledge" for p in body["selected_provenance"])


def test_bounded_selection_does_not_read_unselected_trap_or_scan_vault(
    notes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    old, _, _ = _note(notes, name="older")
    newest, _, _ = _note(notes, name="newer")
    trap = notes["home"] / "global/user/notes/unselected-trap.md"
    trap.write_text(notes["text"], encoding="utf-8")
    read = notes["files"]._read_optional
    seen: list[str] = []

    def bounded(relative: str, *, max_bytes: int) -> bytes | None:
        assert relative == newest.portable_ref and relative != old.portable_ref
        seen.append(relative)
        return cast(bytes | None, read(relative, max_bytes=max_bytes))

    monkeypatch.setattr(notes["files"], "_read_optional", bounded)
    monkeypatch.setattr(
        notes["files"], "audit", lambda **_k: pytest.fail("Full-vault audit forbidden")
    )
    monkeypatch.setattr(os, "walk", lambda *_a, **_k: pytest.fail("Full-vault walk forbidden"))
    values = notes["notes"].candidates(notes["binding"], observed_at=NOW, limit=1)
    assert len(values) == 1 and seen == [newest.portable_ref]


@pytest.mark.parametrize("limit", [None, True, False, "1", -1, 0, 9])
def test_note_source_requires_exact_one_to_eight_limit(
    notes: dict[str, Any], limit: object
) -> None:
    with pytest.raises(ValidationFailed):
        notes["notes"].candidates(notes["binding"], observed_at=NOW, limit=limit)


@pytest.mark.parametrize("limit", [None, True, False, "1", -1, 9])
def test_startup_note_limit_rejects_wrong_type_and_bounds(limit: object) -> None:
    with pytest.raises(ValidationFailed):
        _request(note_limit=limit)


@pytest.mark.parametrize("value", [None, False, "2026-09-02", NOW.replace(tzinfo=None)])
def test_note_observation_time_rejects_wrong_type_and_naive_time(
    notes: dict[str, Any],
    value: object,
) -> None:
    with pytest.raises(ValidationFailed):
        notes["notes"].candidates(notes["binding"], observed_at=value)


def test_global_note_cannot_be_given_project_ownership_during_registration(
    notes: dict[str, Any],
) -> None:
    before = logical_database_digest(notes["path"])
    with (
        notes["operational"].unit_of_work() as uow,
        pytest.raises(ValidationFailed, match="Global"),
    ):
        uow.register_knowledge_note(
            realm_id=notes["binding"].realm_id,
            project_id=notes["binding"].project_id,
            owner_scope="global-user",
            portable_ref="global/user/notes/forged.md",
            note_kind="note",
            authorship="user",
            classification="local-private",
            content_digest=digest(notes["text"]),
        )
    assert logical_database_digest(notes["path"]) == before


def test_optional_note_does_not_displace_required_fragments_at_exact_budget(
    notes: dict[str, Any],
) -> None:
    _note(notes)
    required = notes["service"].hydrate(_request(note_limit=0, idempotency_key="required-only"))
    result = notes["service"].hydrate(
        _request(
            note_limit=1,
            token_budget=required["token_count"],
            idempotency_key="bounded-notes",
        )
    )
    context = _manifest(notes, result["manifest_digest"])["context"]
    assert all(p["kind"] != "knowledge" for p in context["selected_provenance"])
    assert context["compiler"]["omitted"]
    assert result["token_count"] == required["token_count"]


@pytest.mark.parametrize("mode", ["inbox", "archived", "pending", "foreign-realm"])
def test_noncurrent_or_foreign_notes_are_not_loaded_or_promoted(
    notes: dict[str, Any], mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, manifest, _ = _note(
        notes,
        state="inbox" if mode == "inbox" else "active",
        materialize=mode != "pending",
        realm=str(uuid4()) if mode == "foreign-realm" else None,
    )
    if mode == "archived":
        notes["plane"].archive_note(record=record, manifest=manifest)
    before = logical_database_digest(notes["path"])
    monkeypatch.setattr(
        notes["files"], "_read_optional", lambda *_a, **_k: pytest.fail("Excluded note read")
    )
    assert notes["notes"].candidates(notes["binding"], observed_at=NOW) == ()
    assert logical_database_digest(notes["path"]) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope_ref", "project/foreign"),
        ("revision", digest("wrong-revision")),
        ("digest", digest("wrong-content")),
        ("source_ref", "global/user/foreign.md"),
        ("canonical_revision_id", str(uuid4())),
        ("tokens", "1"),
        ("authority", 3),
        ("authority", True),
        ("kind", "system-policy"),
    ],
)
def test_note_caller_cannot_forge_source_identity_scope_or_authority(
    notes: dict[str, Any], field: str, value: Any
) -> None:
    _note(notes)
    candidate, _ = _candidate(notes)
    provenance = candidate.provenance_body | {field: value}
    with pytest.raises((PolicyViolation, ValidationFailed)):
        notes["notes"](notes["binding"], provenance)


@pytest.mark.parametrize(
    "field,value",
    [
        ("materialization_evidence_digest", digest("missing-receipt")),
        ("classification", "secret"),
        ("project_slug", "forged"),
    ],
)
def test_corrupt_note_manifest_or_materialization_evidence_fails_closed(
    notes: dict[str, Any], field: str, value: str
) -> None:
    scope = "project" if field == "project_slug" else "global-user"
    record, _, _ = _note(notes, scope=scope)
    with sqlite3.connect(notes["path"]) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"update knowledge_note set {field}=? where id=?", (value, record.id))
        db.execute("drop trigger knowledge_note_guard_update")
        if field == "project_slug":
            db.execute(
                "update knowledge_note set project_slug=?,portable_ref=? where id=?",
                (value, record.portable_ref.replace("akilli-kasa", value), record.id),
            )
        else:
            db.execute(f"update knowledge_note set {field}=? where id=?", (value, record.id))
    with pytest.raises((PolicyViolation, ValidationFailed)):
        notes["notes"].candidates(notes["binding"], observed_at=NOW)


def test_selected_note_secret_text_is_rejected_without_logging_or_hydration(
    notes: dict[str, Any],
) -> None:
    payload = (notes["text"] + "\nGITHUB_TOKEN=" + "ghp_" + "A" * 36).encode()
    _note(notes, payload=payload)
    with pytest.raises(PolicyViolation, match="secret"):
        notes["service"].hydrate(_request(note_limit=1))
    with sqlite3.connect(notes["path"]) as db:
        assert db.execute("select count(*) from hydration_receipt").fetchone()[0] == 0


def test_generated_note_is_evidence_only_and_stale_metadata_is_rejected(
    notes: dict[str, Any],
) -> None:
    record, manifest, payload = _note(notes, authorship="generated", kind="skill")
    candidate, text = _candidate(notes)
    assert json.loads(text)["learned_activation"] == "not-established"
    assert candidate.authority is AuthorityLevel.OBSERVED
    stale = payload.replace(b"freshness: current", b"freshness: stale")
    assert stale != payload
    path = notes["home"] / manifest.portable_ref
    path.write_bytes(stale)
    with sqlite3.connect(notes["path"]) as db:
        db.execute("drop trigger knowledge_note_guard_update")
        new_digest = note_content_digest(stale)
        evidence = digest(
            {
                "operation": "knowledge-note-materialized",
                "note_id": record.id,
                "portable_ref": manifest.portable_ref,
                "content_digest": new_digest,
            }
        )
        db.execute(
            "update knowledge_note set content_digest=?,materialization_evidence_digest=?"
            " where id=?",
            (new_digest, evidence, record.id),
        )
    with pytest.raises(PolicyViolation, match="provenance"):
        notes["notes"].candidates(notes["binding"], observed_at=NOW)


@pytest.mark.parametrize("mutation", ["changed", "missing", "symlink", "oversized"])
def test_selected_note_physical_drift_is_not_repaired_or_overwritten(
    notes: dict[str, Any], mutation: str
) -> None:
    _, manifest, original = _note(notes)
    candidate, _ = _candidate(notes)
    path = notes["home"] / manifest.portable_ref
    if mutation == "changed":
        path.write_bytes(original + b"\nChanged local note.")
    elif mutation == "missing":
        path.unlink()
    elif mutation == "symlink":
        path.unlink()
        path.symlink_to(ROOT / SOURCE_REF)
    else:
        path.write_bytes(original * (65536 // len(original) + 1))
    with pytest.raises((PolicyViolation, ValidationFailed, LayoutError)):
        notes["notes"](notes["binding"], candidate.provenance_body)
    if mutation == "missing":
        assert not path.exists()
    elif mutation == "symlink":
        assert path.is_symlink()
    else:
        assert path.read_bytes() != original


def test_note_race_after_snapshot_check_rejects_receipt_without_overwrite(
    notes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manifest, payload = _note(notes)
    original = notes["lifecycle"].hydrate
    path = notes["home"] / manifest.portable_ref

    def raced(*args: Any, **kwargs: Any) -> Any:
        path.write_bytes(payload + b"\nUser changed note.")
        return original(*args, **kwargs)

    monkeypatch.setattr(notes["lifecycle"], "hydrate", raced)
    with pytest.raises(PolicyViolation):
        notes["service"].hydrate(_request(note_limit=1))
    assert path.read_bytes() == payload + b"\nUser changed note."
    with sqlite3.connect(notes["path"]) as db:
        assert db.execute("select count(*) from hydration_receipt").fetchone()[0] == 0


def test_hydration_replay_after_note_change_does_not_use_stale_saved_text(
    notes: dict[str, Any],
) -> None:
    _, manifest, payload = _note(notes)
    notes["service"].hydrate(_request(note_limit=1))
    path = notes["home"] / manifest.portable_ref
    path.write_bytes(payload + b"\nNew user content.")
    before = logical_database_digest(notes["path"])
    with pytest.raises(PolicyViolation):
        notes["service"].hydrate(_request(note_limit=1))
    assert logical_database_digest(notes["path"]) == before
    assert path.read_bytes() == payload + b"\nNew user content."


def test_compaction_resume_revalidates_global_note_source_and_never_restores_authority(
    notes: dict[str, Any],
) -> None:
    _, manifest, payload = _note(notes)
    hydration = notes["service"].hydrate(_request(note_limit=1))
    _stage(notes, "PreCompact")
    notes["lifecycle"].drain()
    checkpoint = notes["lifecycle"].pre_compaction(
        context_digest=hydration["manifest_digest"],
        key="notes-compaction",
    )
    resumed = notes["base"].resume(notes["binding"], checkpoint)
    assert resumed["grants_authority"] is False
    assert resumed["reacquire_required"] is True
    context = resumed["context"]["context"]
    assert any(p["scope_ref"] == "global-user" for p in context["selected_provenance"])
    path = notes["home"] / manifest.portable_ref
    path.write_bytes(payload + b"\nChanged after compaction.")
    before = logical_database_digest(notes["path"])
    with pytest.raises(PolicyViolation):
        notes["base"].resume(notes["binding"], checkpoint)
    assert logical_database_digest(notes["path"]) == before
    assert path.read_bytes() == payload + b"\nChanged after compaction."


_CHILD = """
import json, socket, sys
from pathlib import Path
from tests.unit.test_local_continuity_startup import NOW, ROOT, SOURCE_REF, _request
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.application.local_continuity_startup import LocalStartupService
from zekam.domain.canonical import canonical_json, digest
from zekam.infrastructure.clients.local_continuity_decoder import validate_reviewed_control_entry
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver
from zekam.infrastructure.sqlite.local_startup_notes import SQLiteStartupNoteSource
def forbidden(*args, **kwargs):
    raise AssertionError('No network/provider calls during note startup')
socket.socket.connect = forbidden
socket.create_connection = forbidden
path, home, raw = sys.argv[1:]
binding = ContinuityBinding(**json.loads(raw))
base = SQLiteContinuityStore(Path(path))
notes = SQLiteStartupNoteSource(base, KnowledgeFileStore(Path(home)))
project = ProjectContinuitySourceResolver(ROOT, project_id=binding.project_id,
    realm_id=binding.realm_id, source_snapshot_id=binding.source_snapshot_id,
    allowed_paths=(SOURCE_REF,))
source = SQLiteStartupSourceResolver(base, project, note_sources=notes)
base.source_resolver = source
spool = ClientLifecycleSpool(Path(home), client_id=binding.client_id)
lifecycle = LocalLifecycleContinuity(base,spool,binding,
    source_probe=lambda: digest((ROOT/SOURCE_REF).read_text()),
    entry_validator=validate_reviewed_control_entry)
print(canonical_json(LocalStartupService(lifecycle,source).hydrate(_request(note_limit=1))))
"""


def test_fresh_process_reuses_exact_global_note_manifest(notes: dict[str, Any]) -> None:
    _note(notes)
    first = notes["service"].hydrate(_request(note_limit=1))
    before = logical_database_digest(notes["path"])
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(notes["path"]),
            str(notes["home"]),
            canonical_json(asdict(notes["binding"])),
        ],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout)["manifest_digest"] == first["manifest_digest"]
    assert logical_database_digest(notes["path"]) == before
