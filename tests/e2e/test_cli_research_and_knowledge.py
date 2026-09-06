from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from typer.testing import CliRunner

from zekam.application import research_runtime
from zekam.application.home import HomeLayout
from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    KnowledgeNoteManifest,
    generated_note_bytes,
    note_content_digest,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import LayoutError
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli import main as cli

pytestmark = pytest.mark.e2e
REALM_ID = str(uuid5(NAMESPACE_URL, "zekam://realm/yerel"))


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    layout = HomeLayout(tmp_path / ".zekam").ensure()
    layout.ensure_project("demo")
    database = layout.root / "state" / "operational.db"
    bootstrap(database)
    SQLiteLocalRuntimeStore(database)
    with SQLiteOperationalStore(database).unit_of_work() as uow:
        project = uow.create_project(slug="demo", display_name="Demo")
        uow.add_project_alias(project_id=project.id, alias="dm")
        uow.commit()
    monkeypatch.setattr(
        research_runtime,
        "project_rag_status",
        lambda *_: {
            "state": "ready",
            "generation_digest": digest("generation"),
            "source_revision": digest("revision"),
        },
    )
    return layout.root


def test_research_cli_dry_run_is_provider_free_and_apply_requires_digest_and_authorizations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    runner = CliRunner()
    dry = runner.invoke(
        cli.app,
        [
            "research",
            "run",
            "Demo servisi nerede?",
            "--project",
            "dm",
            "--home",
            str(home),
            "--json",
        ],
    )

    assert dry.exit_code == 0, dry.output
    plan = json.loads(dry.output)
    assert plan["dry_run"] is True
    assert plan["provider_calls_performed"] == 0
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    assert runtime.status().ready_jobs == 0

    denied = runner.invoke(
        cli.app,
        [
            "research",
            "run",
            "Demo servisi nerede?",
            "--project",
            "dm",
            "--home",
            str(home),
            "--run-digest",
            plan["run_digest"],
            "--uygula",
        ],
    )
    assert denied.exit_code == 77
    assert "authorization" in denied.output
    assert runtime.status().ready_jobs == 0


def test_knowledge_show_defaults_to_global_scope_and_requires_project_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    store = SQLiteOperationalStore(home / "state" / "operational.db")
    files = KnowledgeFileStore(home)
    with store.unit_of_work() as uow:
        project = uow.resolve_project("demo")
        uow.commit()
    payload = generated_note_bytes(
        owner_scope=f"project:{project.id}",
        project_slug="demo",
        note_kind="research",
        classification=KnowledgeClassification.INTERNAL,
        source_refs=("research-runs/test",),
        source_digests=(digest("source"),),
        generated_at=dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        generator_version="test/v1",
        body="# Project-only note",
    )
    manifest = KnowledgeNoteManifest(
        owner_scope=f"project:{project.id}",
        project_slug="demo",
        note_kind="research",
        authorship="generated",
        classification=KnowledgeClassification.INTERNAL,
        portable_ref="projeler/demo/arastirmalar/generated/test.md",
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

    runner = CliRunner()
    denied = runner.invoke(
        cli.app,
        ["knowledge", "show", note.id, "--home", str(home), "--json"],
    )
    allowed = runner.invoke(
        cli.app,
        [
            "knowledge",
            "show",
            note.id,
            "--project",
            "dm",
            "--home",
            str(home),
            "--json",
        ],
    )

    assert denied.exit_code == 77
    assert "owner scope" in denied.output
    assert allowed.exit_code == 0, allowed.output
    assert json.loads(allowed.output)["owner_scope"] == f"project:{project.id}"


def _invoke_json(runner: CliRunner, args: list[str]) -> dict[str, object]:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_user_markdown_revision_archive_restore_has_exact_claim_receipt_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    runner = CliRunner()
    first_body = tmp_path / "first.md"
    first_body.write_text("# Ilk\n\nGPU ODI bilgi notu.\n", encoding="utf-8")
    base = ["--project", "dm", "--home", str(home), "--json"]

    create_plan = _invoke_json(
        runner,
        ["knowledge", "create", str(first_body), "--title", "ODI Notu", *base],
    )
    assert create_plan["dry_run"] is True
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    assert runtime.status().ready_jobs == 0

    created = _invoke_json(
        runner,
        [
            "knowledge",
            "create",
            str(first_body),
            "--title",
            "ODI Notu",
            *base,
            "--plan-digest",
            str(create_plan["plan_digest"]),
            "--uygula",
        ],
    )
    assert created["state"] == "completed"
    first_note_id = str(created["result"]["note_id"])
    status = _invoke_json(
        runner,
        ["knowledge", "mutation-status", str(created["job_id"]), "--home", str(home), "--json"],
    )
    assert status["state"] == "completed"
    assert status["attempt_count"] == 1
    assert len(status["effects"]) == 1
    assert status["effects"][0]["receipt_id"] is not None

    replayed = _invoke_json(
        runner,
        [
            "knowledge",
            "create",
            str(first_body),
            "--title",
            "ODI Notu",
            *base,
            "--plan-digest",
            str(create_plan["plan_digest"]),
            "--uygula",
        ],
    )
    assert replayed["replayed"] is True
    assert replayed["attempt_count"] == 1

    second_body = tmp_path / "second.md"
    second_body.write_text("# Ikinci\n\nGPU ve SKY ODI revizyonu.\n", encoding="utf-8")
    update_plan = _invoke_json(
        runner,
        [
            "knowledge",
            "update",
            first_note_id,
            str(second_body),
            "--title",
            "ODI Notu",
            *base,
        ],
    )
    updated = _invoke_json(
        runner,
        [
            "knowledge",
            "update",
            first_note_id,
            str(second_body),
            "--title",
            "ODI Notu",
            *base,
            "--plan-digest",
            str(update_plan["plan_digest"]),
            "--uygula",
        ],
    )
    second_note_id = str(updated["result"]["note_id"])
    assert updated["result"]["predecessor_note_id"] == first_note_id
    assert updated["result"]["predecessor_state"] == "archived"

    archive_plan = _invoke_json(
        runner,
        ["knowledge", "archive", second_note_id, *base],
    )
    archived = _invoke_json(
        runner,
        [
            "knowledge",
            "archive",
            second_note_id,
            *base,
            "--plan-digest",
            str(archive_plan["plan_digest"]),
            "--uygula",
        ],
    )
    assert archived["result"]["state"] == "archived"

    restore_plan = _invoke_json(
        runner,
        ["knowledge", "restore", second_note_id, *base],
    )
    restored = _invoke_json(
        runner,
        [
            "knowledge",
            "restore",
            second_note_id,
            *base,
            "--plan-digest",
            str(restore_plan["plan_digest"]),
            "--uygula",
        ],
    )
    assert restored["result"]["restored_from_note_id"] == second_note_id
    assert restored["result"]["note_id"] not in {first_note_id, second_note_id}

    with sqlite3.connect(home / "state" / "operational.db") as connection:
        states = dict(connection.execute("select id,state from knowledge_note"))
        relation = connection.execute(
            "select from_note_id,to_note_id,relation_kind,verified from knowledge_relation"
        ).fetchone()
    assert states[first_note_id] == "archived"
    assert states[second_note_id] == "archived"
    assert states[str(restored["result"]["note_id"])] == "active"
    assert relation == (second_note_id, first_note_id, "supersedes", 1)


def test_user_markdown_rejects_secret_before_job_or_file_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    body = tmp_path / "secret.md"
    secret_fixture = "pass" + "word = 'real-" + "secret-value'\n"
    body.write_text(secret_fixture, encoding="utf-8")
    result = CliRunner().invoke(
        cli.app,
        [
            "knowledge",
            "create",
            str(body),
            "--title",
            "Secret",
            "--home",
            str(home),
            "--json",
        ],
    )

    assert result.exit_code == 77
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    assert runtime.status().ready_jobs == 0
    assert not any((home / "global" / "user").rglob("*.md"))


def test_user_markdown_rejects_original_symlink_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    target_root = tmp_path / "real"
    target_root.mkdir()
    target = target_root / "body.md"
    target.write_text("# Safe body\n", encoding="utf-8")
    linked_root = tmp_path / "linked"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked_root), str(target_root)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert created.returncode == 0, created.stdout + created.stderr
    else:
        linked_root.symlink_to(target_root, target_is_directory=True)
    linked = linked_root / "body.md"

    result = CliRunner().invoke(
        cli.app,
        ["knowledge", "create", str(linked), "--title", "Linked", "--home", str(home)],
    )

    assert result.exit_code == 77
    assert "link veya reparse" in result.output


def test_user_markdown_update_recovers_same_plan_after_archive_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    runner = CliRunner()
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# First\n", encoding="utf-8")
    second.write_text("# Second\n", encoding="utf-8")
    base = ["--project", "demo", "--home", str(home), "--json"]
    create_plan = _invoke_json(
        runner, ["knowledge", "create", str(first), "--title", "Recoverable", *base]
    )
    created = _invoke_json(
        runner,
        [
            "knowledge",
            "create",
            str(first),
            "--title",
            "Recoverable",
            *base,
            "--plan-digest",
            str(create_plan["plan_digest"]),
            "--uygula",
        ],
    )
    note_id = str(created["result"]["note_id"])
    update_plan = _invoke_json(
        runner,
        [
            "knowledge",
            "update",
            note_id,
            str(second),
            "--title",
            "Recoverable",
            *base,
        ],
    )
    original_archive = KnowledgeFileStore.archive_note
    calls = 0

    def fail_once(self: KnowledgeFileStore, manifest: KnowledgeNoteManifest) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LayoutError("injected archive fault")
        return original_archive(self, manifest)

    monkeypatch.setattr(KnowledgeFileStore, "archive_note", fail_once)
    failed = runner.invoke(
        cli.app,
        [
            "knowledge",
            "update",
            note_id,
            str(second),
            "--title",
            "Recoverable",
            *base,
            "--plan-digest",
            str(update_plan["plan_digest"]),
            "--uygula",
        ],
    )
    assert failed.exit_code != 0
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(str(update_plan["idempotency_key"]))
    assert snapshot is not None
    assert snapshot["state"] == "recovery-required"
    with SQLiteOperationalStore(home / "state" / "operational.db").unit_of_work() as uow:
        assert uow.get_knowledge_note(note_id).state == "active"
        uow.commit()

    recovered = _invoke_json(
        runner,
        [
            "knowledge",
            "update",
            note_id,
            str(second),
            "--title",
            "Recoverable",
            *base,
            "--plan-digest",
            str(update_plan["plan_digest"]),
            "--uygula",
        ],
    )
    assert recovered["state"] == "completed"
    assert recovered["recovered"] is True
    assert recovered["result"]["predecessor_state"] == "archived"


def test_user_markdown_update_recovers_same_cli_plan_after_completed_receipt_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    runner = CliRunner()
    first = tmp_path / "receipt-first.md"
    second = tmp_path / "receipt-second.md"
    first.write_text("# Before receipt fault\n", encoding="utf-8")
    second.write_text("# After receipt fault\n", encoding="utf-8")
    base = ["--project", "demo", "--home", str(home), "--json"]
    create_plan = _invoke_json(
        runner, ["knowledge", "create", str(first), "--title", "Receipt Fault", *base]
    )
    created = _invoke_json(
        runner,
        [
            "knowledge",
            "create",
            str(first),
            "--title",
            "Receipt Fault",
            *base,
            "--plan-digest",
            str(create_plan["plan_digest"]),
            "--uygula",
        ],
    )
    note_id = str(created["result"]["note_id"])
    update_plan = _invoke_json(
        runner,
        [
            "knowledge",
            "update",
            note_id,
            str(second),
            "--title",
            "Receipt Fault",
            *base,
        ],
    )
    original_receipt = SQLiteLocalRuntimeStore.record_receipt
    failed_completed = False

    def fail_completed_once(
        self: SQLiteLocalRuntimeStore,
        claim: Any,
        *,
        status: Any,
        evidence_digest: str,
        **kw: Any,
    ) -> Any:
        nonlocal failed_completed
        if status == "completed" and not failed_completed:
            failed_completed = True
            raise LayoutError("injected completed receipt fault")
        return original_receipt(self, claim, status=status, evidence_digest=evidence_digest, **kw)

    monkeypatch.setattr(SQLiteLocalRuntimeStore, "record_receipt", fail_completed_once)
    args = [
        "knowledge",
        "update",
        note_id,
        str(second),
        "--title",
        "Receipt Fault",
        *base,
        "--plan-digest",
        str(update_plan["plan_digest"]),
        "--uygula",
    ]
    failed = runner.invoke(cli.app, args)
    assert failed.exit_code != 0
    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db", existing_only=True)
    snapshot = runtime.job_snapshot(str(update_plan["idempotency_key"]))
    assert snapshot is not None
    assert snapshot["state"] == "recovery-required"
    with SQLiteOperationalStore(home / "state" / "operational.db").unit_of_work() as uow:
        assert uow.get_knowledge_note(note_id).state == "archived"
        uow.commit()

    recovered = _invoke_json(runner, args)
    assert recovered["state"] == "completed"
    assert recovered["recovered"] is True
