"""Real ODI XML/private files/SQLite/CAS; regression source, no provider calls."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.e2e.test_cli_odi11g_export import _home, _bundle, _admit_binding
from zekam.application import odi11g_binding_runtime as subject
from zekam.application.odi11g_export import build_odi11g_export_plan, bind_odi11g_export
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


@pytest.fixture
def odi_world(tmp_path):
    home, root = _home(tmp_path), _bundle(tmp_path)
    with SQLiteOperationalStore(home / "state/operational.db").unit_of_work() as uow:
        project = uow.resolve_project("gpu")
    plan = build_odi11g_export_plan(
        home=home, project_id=project.id, project_slug="gpu", export_root=root
    )
    binding = _admit_binding(home, plan.body)
    return SimpleNamespace(home=home, root=root, plan=plan, binding=binding)


def bind(w, **kwargs):
    return bind_odi11g_export(
        home=w.home,
        plan=w.plan,
        expected_plan_digest=w.plan.plan_digest,
        work_binding=kwargs.get("binding", w.binding),
    )


def test_actual_claim_receipt_has_no_absolute_path_and_completed_replay_does_not_write(
    odi_world, monkeypatch
):
    w = odi_world
    result = bind(w)
    assert (
        result["canonical_receipt_verified"] is True and result["current_binding_verified"] is True
    )
    assert result["source_snapshot_id"] == w.binding.source_snapshot_id
    assert "source_root" not in result and result["current_export_rechecked"] is False

    def forbidden(*_args, **_kwargs):
        pytest.fail("completed replay wrote private files or parsed source")

    monkeypatch.setattr(subject, "build_odi11g_export_plan", forbidden)
    monkeypatch.setattr(KnowledgeFileStore, "write_private_binding", forbidden)
    assert bind(w)["replayed"] is True


@pytest.mark.parametrize("change", ["missing", "wrong-plan", "wrong-snapshot"])
def test_missing_or_unadmitted_work_source_never_writes(odi_world, change):
    w = odi_world
    binding = (
        None
        if change == "missing"
        else replace(
            w.binding,
            **(
                {"plan_digest": digest("other-plan")}
                if change == "wrong-plan"
                else {"source_snapshot_id": None}
            ),
        )
    )
    with pytest.raises((PolicyViolation, ValidationFailed)):
        bind(w, binding=binding)
    assert not (w.home / w.plan.body["binding_ref"]).exists()


def test_legacy_private_mapping_is_preserved_without_receipt_adoption(odi_world):
    w = odi_world
    store = KnowledgeFileStore(w.home)
    old = b'{"schema":"legacy","source_root":"private-old-source"}\n'
    store.write_private_binding(w.plan.body["binding_ref"], old)
    result = bind(w)
    assert result["previous_binding_digest"] == digest_of_bytes(old)
    history = subject._history_ref(result["binding_ref"], digest_of_bytes(old))
    assert subject._read(store, history) == old
    assert result["canonical_receipt_verified"] is True


def test_superseded_binding_has_historical_proof_without_becoming_current(odi_world):
    w = odi_world
    first = bind(w)
    (w.root / "reports/smart-export-report.xml").write_text(
        '<ExportReport status="changed"/>', encoding="utf-8"
    )
    second_plan = build_odi11g_export_plan(
        home=w.home, project_id=w.binding.project_id, project_slug="gpu", export_root=w.root
    )
    second_binding = _admit_binding(w.home, second_plan.body)
    second = bind_odi11g_export(
        home=w.home,
        plan=second_plan,
        expected_plan_digest=second_plan.plan_digest,
        work_binding=second_binding,
    )
    assert second["current_binding_verified"] is True
    historical = subject.read_odi_binding_source(w.home, first["job_id"])
    assert (
        historical["canonical_receipt_verified"] is True
        and historical["current_binding_verified"] is False
    )
    assert historical["current_export_rechecked"] is False


def test_post_write_xml_drift_never_produces_completed_source_receipt(odi_world, monkeypatch):
    w = odi_world
    original = KnowledgeFileStore.write_private_binding

    def write_then_drift(store, relative, payload):
        result = original(store, relative, payload)
        if relative == w.plan.body["binding_ref"]:
            (w.root / "reports/smart-export-report.xml").write_text(
                '<ExportReport status="drift"/>', encoding="utf-8"
            )
        return result

    monkeypatch.setattr(KnowledgeFileStore, "write_private_binding", write_then_drift)
    with pytest.raises(PolicyViolation, match="source/plan drift"):
        bind(w)
    key = subject.canonical_job_key(
        w.binding.binding_digest, subject.OPERATION, subject._input(w.plan.plan_digest)
    )
    with pytest.raises(PolicyViolation, match="completed canonical receipt"):
        subject.read_odi_binding_source(w.home, key)
    before = (w.home / w.plan.body["binding_ref"]).read_bytes()
    with pytest.raises(PolicyViolation):
        bind(w)
    assert (w.home / w.plan.body["binding_ref"]).read_bytes() == before
