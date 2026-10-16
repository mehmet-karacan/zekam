"""Real local SQLite/file/receipt chain with synthetic remote observations; UNRUN."""

import copy
import json
from types import SimpleNamespace

import pytest

from zekam.application import jira_knowledge_import as subject
from zekam.application.home import HomeLayout
from zekam.application.markdown_knowledge import search_markdown_knowledge
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

STAMP = "2026-10-16T12:00:00Z"


def _setup(tmp_path, monkeypatch):
    home = HomeLayout(tmp_path / ".zekam").ensure().root
    HomeLayout(home).ensure_project("demo")
    database = home / "state/operational.db"
    bootstrap(database)
    store, files = SQLiteOperationalStore(database), KnowledgeFileStore(home)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="demo", display_name="Demo")
        uow.create_project(slug="other", display_name="Other")
        uow.commit()
    observation = {
        "schema": "zekam-jira-attachment-read/v1",
        "issue_key": "DEMO-42",
        "attachment": {"id": "123", "content_digest": digest("bytes"), "content_read": True},
        "normalized_digest": digest("normalized"),
        "parser_profile_digest": digest("parser"),
        "units": [
            {"order": 0, "text": "Example behavior specification", "locator": {"block_index": 0}}
        ],
        "read_only": True,
        "knowledge_imported": False,
        "grants_authority": False,
    }
    calls = []

    def read(**kwargs):
        calls.append(kwargs)
        return copy.deepcopy(observation)

    monkeypatch.setattr(subject, "read_jira_attachment", read)
    monkeypatch.setattr(
        subject,
        "resolve_jira_issue",
        lambda _: SimpleNamespace(project_ref="demo", mapping_digest=digest("mapping")),
    )
    plan = subject.build_jira_knowledge_plan(
        store,
        project_ref=project.id,
        issue_key="DEMO-42",
        attachment_id="123",
        metadata_digest=digest("metadata"),
        generated_at=STAMP,
    )
    return home, store, files, project, plan, calls, observation


def test_import_publishes_generated_project_note_with_terminal_receipt_and_replays_without_network(
    tmp_path, monkeypatch
):
    home, store, files, project, plan, calls, _ = _setup(tmp_path, monkeypatch)
    assert calls and not (home / plan.body["target_ref"]).exists()
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan.body), encoding="utf-8")
    body = subject.load_jira_knowledge_plan(path)
    result = subject.apply_jira_knowledge_plan(
        store, files, home, body, expected_plan_digest=plan.plan_digest
    )
    assert result["state"] == "completed" and result["receipt"]["status"] == "completed"
    assert len(calls) == 2
    with store.unit_of_work() as uow:
        note = uow.get_knowledge_note(body["target_ref"])
        notes = uow.list_knowledge_notes(project_id=project.id)
    assert note.authorship == "generated" and note.project_id == project.id
    assert note.owner_scope == f"project:{project.id}" and len(notes) == 1
    assert b"generated: true" in plan.payload
    with store.unit_of_work() as uow:
        found = search_markdown_knowledge(uow, files, "Example behavior", project_id=project.id)
    assert found["count"] == 1 and found["hits"][0]["note"]["authorship"] == "generated"
    monkeypatch.setattr(
        subject, "read_jira_attachment", lambda **kw: pytest.fail("replay network call")
    )
    replay = subject.apply_jira_knowledge_plan(
        store, files, home, body, expected_plan_digest=plan.plan_digest
    )
    assert replay["replayed"] and replay["job_id"] == result["job_id"]
    (home / body["target_ref"]).write_bytes(b"tampered")
    with pytest.raises(ZekamError):
        subject.apply_jira_knowledge_plan(
            store, files, home, body, expected_plan_digest=plan.plan_digest
        )


def test_changed_remote_source_fails_before_claim_and_file_effect(tmp_path, monkeypatch):
    home, store, files, _, plan, _, observation = _setup(tmp_path, monkeypatch)
    observation["normalized_digest"] = digest("changed")
    with pytest.raises(PolicyViolation, match="source/plan drift"):
        subject.apply_jira_knowledge_plan(
            store, files, home, plan.body, expected_plan_digest=plan.plan_digest
        )
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    assert runtime.job_snapshot(plan.body["idempotency_key"]) is None
    assert not (home / plan.body["target_ref"]).exists()


def test_pending_enqueue_before_first_lease_resumes_exact_plan(tmp_path, monkeypatch):
    home, store, files, _, plan, _, _ = _setup(tmp_path, monkeypatch)
    runtime = SQLiteLocalRuntimeStore(home / "state/operational.db", existing_only=True)
    job, created = runtime.enqueue(
        idempotency_key=plan.body["idempotency_key"],
        payload=plan.body | {"dry_run": False},
        max_attempts=1,
    )
    assert created
    result = subject.apply_jira_knowledge_plan(
        store, files, home, plan.body, expected_plan_digest=plan.plan_digest
    )
    assert result["state"] == "completed" and result["job_id"] == job.id
    assert len(runtime.job_snapshot(job.id)["effects"]) == 1


def test_cross_project_jira_mapping_rejected_before_remote_read(tmp_path, monkeypatch):
    _, store, _, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    count = len(calls)
    with pytest.raises(PolicyViolation, match="mapping"):
        subject.build_jira_knowledge_plan(
            store,
            project_ref="other",
            issue_key="DEMO-42",
            attachment_id="123",
            metadata_digest=digest("metadata"),
            generated_at=STAMP,
        )
    assert len(calls) == count


def test_unknown_receipt_recovers_materialized_note_and_replay_uses_resolution(
    tmp_path, monkeypatch
):
    from zekam.application import markdown_knowledge_mutation as mutations

    home, store, files, _, plan, _, _ = _setup(tmp_path, monkeypatch)
    perform = mutations._perform_mutation

    def crash_after_file(*args, **kwargs):
        result = perform(*args, **kwargs)
        if not kwargs["recovering"]:
            raise OSError("synthetic interruption after materialization")
        return result

    monkeypatch.setattr(mutations, "_perform_mutation", crash_after_file)
    with pytest.raises(OSError):
        subject.apply_jira_knowledge_plan(
            store, files, home, plan.body, expected_plan_digest=plan.plan_digest
        )
    recovered = subject.apply_jira_knowledge_plan(
        store, files, home, plan.body, expected_plan_digest=plan.plan_digest
    )
    assert recovered["state"] == "completed" and recovered["recovered"] is True
    monkeypatch.setattr(subject, "read_jira_attachment", lambda **kw: pytest.fail("replay network"))
    replay = subject.apply_jira_knowledge_plan(
        store, files, home, plan.body, expected_plan_digest=plan.plan_digest
    )
    assert replay["state"] == "completed" and replay["replayed"]
    assert replay["effects"][0]["receipt_status"] == "unknown"
    assert replay["effects"][0]["resolution_outcome"] == "completed"
