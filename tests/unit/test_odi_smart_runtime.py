"""Actual Smart XML/copy/SQLite/CAS fixtures; no live ODI/provider operations."""

import os
import threading
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from tests.e2e.test_cli_odi11g_export import _home, _smart_export, _admit_binding
from zekam.application import odi11g_smart_runtime as subject
from zekam.application.odi11g_smart_export import (
    build_smart_import_plan,
    import_smart_export,
    load_smart_binding,
    build_sanitized_odi_plan,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.local_file_security import private_regular
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


@pytest.fixture
def smart_world(tmp_path):
    home = _home(tmp_path)
    source = _smart_export(tmp_path / "source" / "SmartExport.xml")
    with SQLiteOperationalStore(home / "state/operational.db").unit_of_work() as uow:
        project = uow.resolve_project("gpu")
    plan = build_smart_import_plan(
        project_id=project.id,
        project_slug="gpu",
        source=source,
        library_root=tmp_path / "library",
        library_name="gpu",
    )
    binding = _admit_binding(home, plan.body)
    return SimpleNamespace(home=home, source=source, plan=plan, binding=binding)


def apply(w, binding=None):
    return import_smart_export(
        home=w.home,
        plan=w.plan,
        expected_plan_digest=w.plan.plan_digest,
        work_binding=w.binding if binding is None else binding,
    )


def test_actual_private_copy_receipt_and_index_consumer_do_not_embed_topology(smart_world):
    w = smart_world
    result = apply(w)
    assert (
        result["canonical_receipt_verified"] is True and result["current_binding_verified"] is True
    )
    assert private_regular(w.plan.destination)
    assert w.plan.destination.read_bytes() == w.source.read_bytes()
    loaded = load_smart_binding(w.home, "gpu")
    assert loaded["canonical_job_id"] == result["job_id"]
    assert loaded["source_file"] == str(w.plan.destination)
    index_plan = build_sanitized_odi_plan(
        project_id=UUID(w.binding.project_id), project_slug="gpu", source=w.plan.destination
    )
    assert b"never-embed-this" not in index_plan.manifest
    assert all("never-embed-this" not in chunk.text for chunk in index_plan.chunks)
    objects = subject.LocalContentAddressedStore(w.home / "artifacts/sha256")
    stored = objects.get(result["source_evidence_digest"])
    assert b"never-embed-this" not in stored and str(w.source).encode() not in stored
    assert str(w.plan.destination).encode() not in stored


def test_completed_replay_reads_receipt_without_recopy_or_original_source(smart_world, monkeypatch):
    w = smart_world
    first = apply(w)
    w.source.unlink()

    def forbidden(*_args, **_kwargs):
        pytest.fail("completed replay attempted source parse/copy")

    monkeypatch.setattr(subject, "_copy_private", forbidden)
    monkeypatch.setattr(subject, "build_smart_import_plan", forbidden)
    second = apply(w)
    assert (
        second["replayed"] is True
        and second["source_evidence_digest"] == first["source_evidence_digest"]
    )
    assert load_smart_binding(w.home, "gpu")["canonical_job_id"] == first["job_id"]


@pytest.mark.parametrize("change", ["unbound", "plan", "snapshot"])
def test_unadmitted_smart_copy_never_creates_library(smart_world, change):
    w = smart_world
    with pytest.raises((PolicyViolation, ValidationFailed)):
        import_smart_export(
            home=w.home,
            plan=w.plan,
            expected_plan_digest=w.plan.plan_digest,
            work_binding=None
            if change == "unbound"
            else replace(
                w.binding,
                **(
                    {"plan_digest": digest("other")}
                    if change == "plan"
                    else {"source_snapshot_id": None}
                ),
            ),
        )
    assert not w.plan.destination.exists()


def test_existing_different_destination_is_never_overwritten(smart_world):
    w = smart_world
    subject._mkdir_private(w.plan.destination.parent)
    w.plan.destination.write_bytes(b"other immutable user content")
    subject.restrict_private_file(w.plan.destination)
    with pytest.raises(PolicyViolation, match="destination ownership/digest"):
        apply(w)
    assert w.plan.destination.read_bytes() == b"other immutable user content"
    assert not (w.home / w.plan.body["binding_ref"]).exists()


def test_legacy_binding_has_no_index_authority_and_is_preserved_on_admitted_replace(smart_world):
    w = smart_world
    old = b'{"schema":"zekam-project-local-odi11g-smart-binding/v1","source_file":"legacy"}\n'
    store = KnowledgeFileStore(w.home)
    store.write_private_binding(w.plan.body["binding_ref"], old)
    with pytest.raises(PolicyViolation, match="legacy/unbound"):
        load_smart_binding(w.home, "gpu")
    apply(w)
    assert (
        subject._read(store, subject._history(w.plan.body["binding_ref"], digest_of_bytes(old)))
        == old
    )


def test_lost_receipt_blocks_index_even_when_private_copy_and_binding_exist(
    smart_world, monkeypatch
):
    w = smart_world
    original = subject.SQLiteLocalRuntimeStore.record_receipt

    def interrupted(store, claim, *, status, evidence_digest, **kwargs):
        if status == "completed":
            raise OSError("fixture receipt fault")
        return original(store, claim, status=status, evidence_digest=evidence_digest, **kwargs)

    monkeypatch.setattr(subject.SQLiteLocalRuntimeStore, "record_receipt", interrupted)
    with pytest.raises(OSError):
        apply(w)
    assert w.plan.destination.exists()
    with pytest.raises(PolicyViolation, match="completed canonical receipt"):
        load_smart_binding(w.home, "gpu")
    before = (w.home / w.plan.body["binding_ref"]).read_bytes()
    with pytest.raises(PolicyViolation, match="recovery required"):
        apply(w)
    assert (w.home / w.plan.body["binding_ref"]).read_bytes() == before


def test_imported_file_drift_is_not_hidden_by_completed_receipt(smart_world):
    w = smart_world
    apply(w)
    w.plan.destination.write_bytes(b"changed raw XML")
    with pytest.raises(PolicyViolation, match="imported source/private file drift"):
        load_smart_binding(w.home, "gpu")


def test_cancelled_copy_cannot_create_destination(smart_world):
    w = smart_world
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(PolicyViolation, match="cancelled before write"):
        subject._copy_private(w.plan, cancel)
    assert not w.plan.destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission fixture")
def test_existing_public_raw_file_is_not_adopted(smart_world):
    w = smart_world
    subject._mkdir_private(w.plan.destination.parent)
    w.plan.destination.write_bytes(w.source.read_bytes())
    w.plan.destination.chmod(0o644)
    with pytest.raises(PolicyViolation, match="destination ownership/digest"):
        apply(w)
