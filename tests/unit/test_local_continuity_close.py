"""Close pipeline using one read-only real Akilli Kasa source and disposable homes."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast

import pytest
from tests.unit.test_local_continuity import ROOT, SOURCE_REF, _checkpoint, _resolver
from tests.unit.test_local_continuity import continuity as continuity

from zekam.application.home import HomeLayout
from zekam.application.knowledge_plane_service import KnowledgePlaneService
from zekam.application.local_continuity import ContinuityEvent
from zekam.application.local_continuity_close import CloseSummary, LocalCloseService
from zekam.application.local_runtime_service import (
    LocalDeliveryResult,
    LocalEffectResult,
    LocalRuntimeService,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_close import SQLiteCloseStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

OWNER: dict[str, Any] = {"owner_id": "close-worker", "owner_pid": 42, "owner_token": "incarnation"}


@pytest.fixture
def close(continuity: Any, tmp_path: Path) -> dict[str, Any]:
    base, binding, context = continuity
    checkpoint = _checkpoint(base, binding, context)
    manifest = base.resume(binding, checkpoint)["checkpoint"]["context_digest"]
    text = (ROOT / SOURCE_REF).read_text()

    def probe(value: Any) -> None:
        if value != binding or digest((ROOT / SOURCE_REF).read_text()) != digest(text):
            raise PolicyViolation("Real bounded Akilli Kasa source drift")

    home = tmp_path / "home"
    HomeLayout(home).ensure().ensure_project("akilli-kasa")
    files = KnowledgeFileStore(home)
    runtime = SQLiteLocalRuntimeStore(base.path)
    store = SQLiteCloseStore(base, runtime, files, source_probe=probe)
    knowledge = KnowledgePlaneService(SQLiteOperationalStore(base.path), files)
    service = LocalCloseService(
        store, runtime, knowledge, source_probe=probe, verify_projection=store.verify_projection
    )
    summary = CloseSummary(
        ("Inspected the Akilli Kasa health endpoint.",),
        (),
        (),
        ("Continue bounded acceptance checks.",),
        "Run the next acceptance gate.",
        ((SOURCE_REF, digest(text)),),
        ((f"checkpoint/{checkpoint[7:]}", checkpoint),),
    )
    return {
        "base": base,
        "binding": binding,
        "context": context,
        "checkpoint": checkpoint,
        "manifest": manifest,
        "files": files,
        "runtime": runtime,
        "store": store,
        "service": service,
        "summary": summary,
        "probe": probe,
        "knowledge": knowledge,
    }


def _freeze(close: dict[str, Any]) -> Any:
    return close["store"].freeze(
        close["binding"],
        close["summary"],
        checkpoint_digest=close["checkpoint"],
        manifest_digest=close["manifest"],
        expected_tail=close["base"].tail(close["binding"]),
    )


def _drain_runtime(close: dict[str, Any]) -> None:
    service = LocalRuntimeService(
        close["runtime"],
        effect_executor=lambda _: LocalEffectResult("failed", digest("unused")),
        outbox_publisher=lambda claim: LocalDeliveryResult("delivered", digest(claim.event.id)),
    )
    while service.publish_outbox_once(**OWNER):
        pass


def test_complete_close_is_durable_candidate_only_and_replay_exact(close: dict[str, Any]) -> None:
    request = _freeze(close)
    assert _freeze(close) == request
    assert (
        close["service"].deliver_once(close["binding"], request.request_digest, **OWNER).state
        == "pending"
    )
    assert close["runtime"].status().claimed_outbox == 0
    with pytest.raises(PolicyViolation):
        close["store"].finalize(close["binding"], request.request_digest)
    assert close["runtime"].claim_next(**OWNER, lease_seconds=30) is None
    close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    expected = request.projections(close["binding"])
    with sqlite3.connect(close["base"].path) as db:
        assert (
            db.execute(
                "select state,authorship,materialized from knowledge_note order by id"
            ).fetchall()
            == [("inbox", "generated", 1)] * 2
        )
    close["service"].deliver_once(close["binding"], request.request_digest, **OWNER)
    with pytest.raises(PolicyViolation, match="pending"):
        close["service"].finalize(close["binding"], request.request_digest)
    _drain_runtime(close)
    receipt = close["service"].finalize(close["binding"], request.request_digest)
    reopened = SQLiteCloseStore(
        SQLiteContinuityStore(close["base"].path, source_resolver=_resolver(close["binding"])),
        SQLiteLocalRuntimeStore(close["base"].path),
        close["files"],
        source_probe=close["probe"],
    )
    assert reopened.finalize(close["binding"], request.request_digest) == receipt
    assert reopened.load(close["binding"], request.request_digest).state == "complete"
    assert (
        reopened.load(close["binding"], request.request_digest).projections(close["binding"])
        == expected
    )
    for item in expected:
        assert (close["files"].home / item.manifest.portable_ref).read_bytes() == item.payload


@pytest.mark.parametrize(
    "field,value",
    [
        ("performed", None),
        ("performed", []),
        ("performed", ()),
        ("performed", (1,)),
        ("performed", ("",)),
        ("performed", ("same", "same")),
        ("next_safe_step", None),
        ("next_safe_step", "x" * 2049),
        ("next_safe_step", "line1\nline2"),
        ("sources", ()),
        ("sources", (("../../bad", "bad"),)),
        ("evidence", None),
    ],
)
def test_summary_boundary_rejects_before_freeze(
    close: dict[str, Any], field: str, value: Any
) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        replace(close["summary"], **{field: value})
    assert close["runtime"].status().ready_jobs == 0


def test_freeze_rejects_payload_drift_wrong_source_and_owner(close: dict[str, Any]) -> None:
    wrong_source = replace(close["summary"], sources=((SOURCE_REF, digest("wrong")),))
    with pytest.raises(PolicyViolation, match="provenance"):
        close["store"].freeze(
            close["binding"],
            wrong_source,
            checkpoint_digest=close["checkpoint"],
            manifest_digest=close["manifest"],
            expected_tail=close["base"].tail(close["binding"]),
        )
    request = _freeze(close)
    close["summary"] = replace(close["summary"], next_safe_step="Different frozen input.")
    with pytest.raises(PolicyViolation, match="drift"):
        _freeze(close)
    with pytest.raises(PolicyViolation):
        close["store"].load(replace(close["binding"], client_id="other"), request.request_digest)
    with pytest.raises(PolicyViolation, match="frozen"):
        close["base"].append_event(
            close["binding"],
            ContinuityEvent("USER_TURN_COMMITTED", "after-close", "2026-09-02T18:00:00+00:00"),
            expected_tail=close["base"].tail(close["binding"]),
        )


def test_freeze_outbox_backpressure_rolls_back_entire_close(
    close: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(close["runtime"], "max_pending_outbox", 1)
    with pytest.raises(PolicyViolation, match="backpressure"):
        _freeze(close)
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select status from session").fetchone()[0] == "open"
        assert db.execute("select count(*) from local_job").fetchone()[0] == 0
        assert db.execute("select count(*) from continuity_close_request").fetchone()[0] == 0


def test_partial_publish_and_restart_never_reexecute_unknown_effect(
    close: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _freeze(close)
    original = close["files"].create_note
    calls = 0

    def fail_second(manifest: Any, payload: bytes) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted second candidate publication")
        return cast(Path, original(manifest, payload))

    monkeypatch.setattr(close["files"], "create_note", fail_second)
    with pytest.raises(OSError, match="interrupted"):
        close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    state = close["store"].load(close["binding"], request.request_digest)
    assert state.state == "recovery-required"
    first = request.projections(close["binding"])[0]
    assert (close["files"].home / first.manifest.portable_ref).read_bytes() == first.payload
    assert (
        close["service"].compile_once(close["binding"], request.request_digest, **OWNER).state
        == "recovery-required"
    )
    assert calls == 2
    with pytest.raises(PolicyViolation):
        close["service"].finalize(close["binding"], request.request_digest)


def test_user_file_collision_is_preserved_and_close_not_complete(close: dict[str, Any]) -> None:
    request = _freeze(close)
    destination = (
        close["files"].home / request.projections(close["binding"])[0].manifest.portable_ref
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"User-owned material; preserve exactly.\n")
    with pytest.raises(PolicyViolation, match="overwrite"):
        close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    assert destination.read_bytes() == b"User-owned material; preserve exactly.\n"
    assert (
        close["store"].load(close["binding"], request.request_digest).state == "recovery-required"
    )


def test_compiler_does_not_repeat_and_finalizer_rechecks_deleted_output(
    close: dict[str, Any],
) -> None:
    request = _freeze(close)
    close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    close["service"].deliver_once(close["binding"], request.request_digest, **OWNER)
    _drain_runtime(close)
    destination = (
        close["files"].home / request.projections(close["binding"])[0].manifest.portable_ref
    )
    destination.write_bytes(b"changed disposable test projection")
    with pytest.raises(PolicyViolation, match="projection"):
        close["store"].finalize(close["binding"], request.request_digest)
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from local_effect_claim").fetchone()[0] == 1
        assert db.execute("select count(*) from close_receipt").fetchone()[0] == 0


def test_concurrent_freeze_publishes_one_request_and_job(close: dict[str, Any]) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        requests = list(executor.map(lambda _: _freeze(close), range(2)))
    assert requests[0] == requests[1]
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from local_job").fetchone()[0] == 1
        assert db.execute("select count(*) from continuity_close_request").fetchone()[0] == 1


@pytest.mark.parametrize("selector", [None, 0, 1, "true", [], {}])
def test_completed_job_filter_requires_real_bool(close: dict[str, Any], selector: Any) -> None:
    request = _freeze(close)
    with pytest.raises(ValidationFailed):
        close["runtime"].claim_outbox(
            supported_kinds=("continuity.compile",),
            outbox_id=request.outbox_id,
            require_completed_job=selector,
            **OWNER,
            lease_seconds=30,
        )
    assert close["runtime"].status().claimed_outbox == 0


def test_partial_publish_explicit_repair_preserves_unknown_and_closes(
    close: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _freeze(close)
    original = close["files"].create_note
    calls = 0

    def interrupted(manifest: Any, payload: bytes) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("partial test publication")
        return cast(Path, original(manifest, payload))

    monkeypatch.setattr(close["files"], "create_note", interrupted)
    with pytest.raises(OSError):
        close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    with sqlite3.connect(close["base"].path) as db:
        original_receipt = db.execute(
            "select r.* from local_effect_receipt r"
            " join local_effect_claim c on c.id=r.claim_id where c.job_id=?",
            (request.job_id,),
        ).fetchone()
    monkeypatch.setattr(close["files"], "create_note", original)
    repaired = close["service"].repair_generated_candidates(
        close["binding"], request.request_digest, repair_key="reviewed-repair-1", **OWNER
    )
    assert repaired.state == "pending"
    assert (
        close["service"].repair_generated_candidates(
            close["binding"], request.request_digest, repair_key="reviewed-repair-1", **OWNER
        )
        == repaired
    )
    with sqlite3.connect(close["base"].path) as db:
        assert (
            db.execute(
                "select r.* from local_effect_receipt r"
                " join local_effect_claim c on c.id=r.claim_id where c.job_id=?",
                (request.job_id,),
            ).fetchone()
            == original_receipt
        )
        assert original_receipt[2] == "unknown"
        assert db.execute("select outcome from local_recovery_resolution").fetchall() == [
            ("completed",)
        ]
        assert db.execute("select count(*) from knowledge_note").fetchone()[0] == 2
    close["service"].deliver_once(close["binding"], request.request_digest, **OWNER)
    _drain_runtime(close)
    assert close["service"].finalize(close["binding"], request.request_digest).startswith("sha256:")


def test_repair_cannot_overwrite_user_collision_or_duplicate_unknown_attempt(
    close: dict[str, Any],
) -> None:
    request = _freeze(close)
    item = request.projections(close["binding"])[0]
    target = close["files"].home / item.manifest.portable_ref
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"Protected test user content")
    with pytest.raises(PolicyViolation):
        close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    with pytest.raises(PolicyViolation):
        close["service"].repair_generated_candidates(
            close["binding"], request.request_digest, repair_key="repair-conflict", **OWNER
        )
    assert target.read_bytes() == b"Protected test user content"
    with pytest.raises(PolicyViolation, match="not duplicated"):
        close["service"].repair_generated_candidates(
            close["binding"], request.request_digest, repair_key="different-attempt", **OWNER
        )
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from local_recovery_resolution").fetchone()[0] == 0
        assert db.execute("select count(*) from close_receipt").fetchone()[0] == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX process death evidence; Windows live deferred")
def test_killed_compiler_after_file_write_restarts_into_explicit_repair(
    close: dict[str, Any],
) -> None:
    request = _freeze(close)
    program = """
import json, os, signal, sys
from pathlib import Path
from tests.unit.test_local_continuity import ROOT, SOURCE_REF, _resolver
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_close import LocalCloseService
from zekam.application.knowledge_plane_service import KnowledgePlaneService
from zekam.domain.canonical import digest
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_close import SQLiteCloseStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
database, home, binding_json, request_digest, source_digest = sys.argv[1:]
binding = ContinuityBinding(**json.loads(binding_json))
def probe(current):
    assert current == binding
    assert digest((ROOT / SOURCE_REF).read_text()) == source_digest
runtime = SQLiteLocalRuntimeStore(Path(database))
files = KnowledgeFileStore(Path(home))
base = SQLiteContinuityStore(Path(database), source_resolver=_resolver(binding))
store = SQLiteCloseStore(base, runtime, files, source_probe=probe)
original = files.create_note
def killed(manifest, payload):
    original(manifest, payload)
    os.kill(os.getpid(), signal.SIGKILL)
files.create_note = killed
knowledge = KnowledgePlaneService(SQLiteOperationalStore(Path(database)), files)
service = LocalCloseService(store, runtime, knowledge, source_probe=probe,
    verify_projection=store.verify_projection)
service.compile_once(binding, request_digest, owner_id='crash-child', owner_pid=os.getpid(),
    owner_token='crash-incarnation', lease_seconds=30)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(close["base"].path),
            str(close["files"].home),
            canonical_json(asdict(close["binding"])),
            request.request_digest,
            digest((ROOT / SOURCE_REF).read_text()),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr
    sweep = close["runtime"].recover_orphans(lambda _: None)
    assert sweep.recovery_required == 1
    assert (
        close["store"].load(close["binding"], request.request_digest).state == "recovery-required"
    )
    close["service"].repair_generated_candidates(
        close["binding"], request.request_digest, repair_key="repair-after-crash", **OWNER
    )
    close["service"].deliver_once(close["binding"], request.request_digest, **OWNER)
    _drain_runtime(close)
    assert close["service"].finalize(close["binding"], request.request_digest).startswith("sha256:")


def test_receipted_compile_restart_finishes_without_republishing(
    close: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _freeze(close)
    original_finish = close["runtime"].finish

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise OSError("process stopped after durable effect receipt")

    monkeypatch.setattr(close["runtime"], "finish", interrupted)
    with pytest.raises(OSError):
        close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    monkeypatch.setattr(close["runtime"], "finish", original_finish)
    sweep = close["runtime"].recover_orphans(lambda _: None)
    assert sweep.finalized == 1
    close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    close["service"].deliver_once(close["binding"], request.request_digest, **OWNER)
    _drain_runtime(close)
    close["service"].finalize(close["binding"], request.request_digest)
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute("select count(*) from knowledge_note").fetchone()[0] == 2
        assert db.execute("select count(*) from local_effect_claim").fetchone()[0] == 1


def test_exact_outbox_selection_wrong_id_does_not_claim(close: dict[str, Any]) -> None:
    request = _freeze(close)
    close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    assert (
        close["runtime"].claim_outbox(
            supported_kinds=("continuity.compile",),
            outbox_id="missing-exact-id",
            require_completed_job=True,
            lease_seconds=30,
            **OWNER,
        )
        is None
    )
    assert close["runtime"].status().claimed_outbox == 0


def test_source_probe_failure_blocks_work_before_any_claim(
    close: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _freeze(close)

    def stale(_: Any) -> None:
        raise PolicyViolation("Source changed")

    monkeypatch.setattr(close["store"], "source_probe", stale)
    with pytest.raises(PolicyViolation, match="Source changed"):
        close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    assert close["runtime"].status().ready_jobs == 1
    assert close["runtime"].status().running_jobs == 0


def test_unknown_delivery_explicit_reconcile_keeps_original_receipt(close: dict[str, Any]) -> None:
    request = _freeze(close)
    close["service"].compile_once(close["binding"], request.request_digest, **OWNER)
    claim = close["runtime"].claim_outbox(
        supported_kinds=("continuity.compile",),
        outbox_id=request.outbox_id,
        require_completed_job=True,
        lease_seconds=30,
        **OWNER,
    )
    assert claim is not None
    close["runtime"].record_outbox_receipt(
        claim, status="unknown", evidence_digest=digest("lost-ack")
    )
    assert (
        close["service"].deliver_once(close["binding"], request.request_digest, **OWNER).state
        == "recovery-required"
    )
    close["service"].reconcile_delivery(close["binding"], request.request_digest)
    _drain_runtime(close)
    close["service"].finalize(close["binding"], request.request_digest)
    with sqlite3.connect(close["base"].path) as db:
        assert db.execute(
            "select status,evidence_digest from local_outbox_receipt where outbox_id=?",
            (request.outbox_id,),
        ).fetchone() == ("unknown", digest("lost-ack"))
        assert (
            db.execute("select outcome from local_recovery_resolution").fetchone()[0] == "delivered"
        )
