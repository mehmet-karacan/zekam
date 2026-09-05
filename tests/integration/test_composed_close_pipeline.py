"""Existing startup composition must remain usable by its own frozen close workflow."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity_bridge_close import _stage
from tests.unit.test_local_continuity_environment import environment as environment
from tests.unit.test_local_continuity_startup import ROOT, SOURCE_REF, _request, _stage_start
from tests.unit.test_local_startup_composition import composition as composition

from zekam.application.knowledge_plane_service import KnowledgePlaneService
from zekam.application.local_continuity_close import CloseSummary, LocalCloseService
from zekam.application.local_runtime import RUNTIME_OUTBOX_KINDS
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import ConfigurationError, PolicyViolation
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.local_startup_composition import compose_local_startup
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_close import SQLiteCloseStore
from zekam.infrastructure.sqlite.local_continuity_control import SQLiteContinuityControlStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.integration


def test_composed_startup_can_reopen_its_durable_frozen_close_request(
    composition: dict[str, Any],
) -> None:
    value = composition
    _stage_start(value, drain=False)
    assert value["composed"].drain() == 1
    started = value["composed"].hydrate(_request())
    _stage(value, "Stop")
    assert value["composed"].drain() == 2

    def current(binding: Any) -> None:
        assert binding == value["binding"]
        value["sources"].preflight(binding)

    store = SQLiteCloseStore(
        value["base"],
        SQLiteLocalRuntimeStore(value["path"]),
        KnowledgeFileStore(value["home"]),
        source_probe=current,
    )
    summary = CloseSummary(
        ("Inspected the actual Akilli Kasa health source.",),
        (),
        (),
        ("Installed-client lifecycle remains unproven.",),
        "Verify the next approved lifecycle gate.",
        ((SOURCE_REF, digest(value["text"])),),
        ((f"context/{started['manifest_digest'][7:]}", started["manifest_digest"]),),
    )
    frozen = value["lifecycle"].pre_close(
        store, summary, context_digest=started["manifest_digest"], key="composed-pre-close"
    )
    assert store.load(value["binding"], frozen.request_digest) == frozen


OWNER: dict[str, Any] = {
    "owner_id": "composed-close-local-test",
    "owner_pid": os.getpid(),
    "owner_token": "independent-composed-close-incarnation",
}


def _attach_close(value: dict[str, Any]) -> dict[str, Any]:
    composed = compose_local_startup(value["gate"], value["binding"], value["source"])

    def current(binding: Any) -> None:
        if binding != value["binding"]:
            raise PolicyViolation("Composed close exact source owner required")
        composed.sources.preflight(binding)

    runtime = SQLiteLocalRuntimeStore(value["path"])
    files = KnowledgeFileStore(value["home"])
    assert isinstance(composed.lifecycle.store, SQLiteContinuityStore)
    store = SQLiteCloseStore(composed.lifecycle.store, runtime, files, source_probe=current)
    service = LocalCloseService(
        store,
        runtime,
        KnowledgePlaneService(SQLiteOperationalStore(value["path"]), files),
        source_probe=current,
        verify_projection=store.verify_projection,
    )
    return value | {
        "composed": composed,
        "base": composed.lifecycle.store,
        "sources": composed.sources,
        "lifecycle": composed.lifecycle,
        "spool": composed.lifecycle.spool,
        "runtime": runtime,
        "files": files,
        "store": store,
        "service": service,
        "probe": current,
    }


@pytest.fixture
def before_freeze(composition: dict[str, Any]) -> dict[str, Any]:
    value = _attach_close(composition)
    _stage_start(value, drain=False)
    assert value["composed"].drain() == 1
    started = value["composed"].hydrate(_request())
    manifest = started["manifest_digest"]
    _stage(value, "PreCompact")
    assert value["composed"].drain() == 2
    checkpoint = value["lifecycle"].pre_compaction(
        context_digest=manifest, key="composed-before-compact"
    )
    assert value["base"].resume(value["binding"], checkpoint)["reacquire_required"] is True
    _stage(value, "Stop")
    assert value["composed"].drain() == 3
    summary = CloseSummary(
        ("Inspected the actual bounded Akilli Kasa health source.",),
        (),
        (),
        ("Native installed-client lifecycle is still unproven.",),
        "Verify the next approved lifecycle gate.",
        ((SOURCE_REF, digest(value["text"])),),
        ((f"context/{manifest[7:]}", manifest),),
    )
    return value | {"manifest": manifest, "summary": summary, "compact_checkpoint": checkpoint}


def _freeze(value: dict[str, Any], summary: CloseSummary | None = None) -> Any:
    return value["lifecycle"].pre_close(
        value["store"],
        value["summary"] if summary is None else summary,
        context_digest=value["manifest"],
        key="composed-frozen-close",
    )


@pytest.fixture
def frozen(before_freeze: dict[str, Any]) -> dict[str, Any]:
    return before_freeze | {"request": _freeze(before_freeze)}


def _not_closed(value: dict[str, Any], *, expected_state: str = "closing") -> None:
    with sqlite3.connect(value["path"]) as db:
        assert db.execute("select count(*) from close_receipt").fetchone()[0] == 0
        assert (
            db.execute(
                "select status from session where id=?", (value["binding"].session_id,)
            ).fetchone()[0]
            == expected_state
        )


def _local_bookkeeping_delivery(value: dict[str, Any]) -> int:
    """Deliver only exact local job observations into a real fsynced test sink.

    This is not external publication evidence. Generated candidate publication is
    verified separately by the dedicated compile effect/delivery/finalizer path.
    """
    request = value["request"]
    sink = value["home"].parent / "test-only-local-bookkeeping"
    sink.mkdir(exist_ok=True)
    with sqlite3.connect(value["path"]) as db:
        rows = db.execute(
            "select o.id,o.event_kind,j.payload_json from local_outbox o"
            " join local_job j on j.id=o.job_id"
            " where json_extract(j.payload_json,'$.request_digest')=? order by o.id",
            (request.request_digest,),
        ).fetchall()
    delivered = 0
    for outbox_id, kind, job_json in rows:
        if kind not in RUNTIME_OUTBOX_KINDS:
            continue
        job = json.loads(job_json)
        assert job["session_id"] == value["binding"].session_id
        assert job["binding_digest"] == value["binding"].binding_digest
        claim = value["runtime"].claim_outbox(
            supported_kinds=(kind,), outbox_id=outbox_id, lease_seconds=30, **OWNER
        )
        if claim is None:
            continue
        assert claim.event.id == outbox_id and claim.event.event_kind == kind
        assert claim.event.payload["job_id"] == claim.event.job_id
        assert digest(claim.event.payload) == claim.event.payload_digest
        body = {
            "schema": "test-only-local-runtime-observation/v1",
            "scope": "local-observation-delivery-only-not-external-publication",
            "outbox_id": outbox_id,
            "job_id": claim.event.job_id,
            "event_kind": kind,
            "payload": claim.event.payload,
            "payload_digest": claim.event.payload_digest,
        }
        payload = canonical_json(body).encode()
        path = sink / f"{outbox_id}.json"
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            descriptor = os.open(sink, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        assert path.read_bytes() == payload
        value["runtime"].record_outbox_receipt(
            claim, status="delivered", evidence_digest=digest_of_bytes(path.read_bytes())
        )
        with sqlite3.connect(value["path"]) as db:
            receipt = db.execute(
                "select evidence_digest,status from local_outbox_receipt where outbox_id=?",
                (outbox_id,),
            ).fetchone()
        assert receipt == (digest_of_bytes(payload), "delivered")
        delivered += 1
    return delivered


def _compile_and_deliver(value: dict[str, Any]) -> None:
    request, binding = value["request"], value["binding"]
    assert value["runtime"].claim_next(lease_seconds=30, **OWNER) is None
    value["service"].compile_once(binding, request.request_digest, **OWNER)
    value["service"].deliver_once(binding, request.request_digest, **OWNER)
    for projection in request.projections(binding):
        assert (value["home"] / projection.manifest.portable_ref).read_bytes() == projection.payload


def test_composed_full_pipeline_replay_and_postclose_controls(frozen: dict[str, Any]) -> None:
    value = _attach_close(frozen)
    request, binding = value["request"], value["binding"]
    assert value["store"].load(binding, request.request_digest) == request
    assert _freeze(value) == request
    # Frozen evidence is not permission to hydrate a new live context.
    with pytest.raises(PolicyViolation, match="open session"):
        value["composed"].hydrate(_request(idempotency_key="forbidden-after-freeze"))
    with pytest.raises(PolicyViolation):
        value["service"].finalize(binding, request.request_digest)
    _compile_and_deliver(value)
    with pytest.raises(PolicyViolation, match="pending"):
        value["service"].finalize(binding, request.request_digest)
    assert _local_bookkeeping_delivery(value) == 2
    assert _local_bookkeeping_delivery(value) == 0
    receipt = value["service"].finalize(binding, request.request_digest)
    assert value["service"].finalize(binding, request.request_digest) == receipt
    assert value["store"].load(binding, request.request_digest).state == "complete"
    with sqlite3.connect(value["path"]) as db:
        assert db.execute("select count(*) from close_receipt").fetchone()[0] == 1
        assert (
            db.execute(
                "select state,authorship,materialized from knowledge_note order by id"
            ).fetchall()
            == [("inbox", "generated", 1)] * 2
        )
    value["lifecycle"].controls = SQLiteContinuityControlStore(value["base"], value["spool"])
    _stage(value, "SessionEnd")
    value["composed"].drain()
    report = value["lifecycle"].doctor()
    assert report["control_event_count"] == 1 and report["rejected_count"] == 0
    _stage(value, "PreCompact")
    with pytest.raises(PolicyViolation):
        value["composed"].drain()
    report = value["lifecycle"].doctor()
    assert report["control_event_count"] == 2 and report["rejected_count"] == 1
    assert report["state"] == "attention-required"
    assert value["service"].finalize(binding, request.request_digest) == receipt


def test_frozen_request_load_survives_real_process_restart(frozen: dict[str, Any]) -> None:
    script = """
import json,socket,sys
from dataclasses import asdict
from pathlib import Path
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_source_plan import ContinuitySourceRecipe
from zekam.infrastructure.local_continuity_environment import LocalContinuityEnvironment
from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource
from zekam.infrastructure.local_startup_composition import compose_local_startup
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_continuity_close import SQLiteCloseStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
def forbidden(*args,**kwargs): raise AssertionError('No provider/network')
socket.socket.connect=forbidden;socket.create_connection=forbidden
d=json.load(sys.stdin)
gate=LocalContinuityEnvironment(**{k:Path(v) for k,v in d['gate'].items()})
binding=ContinuityBinding(**d['binding'])
d['recipe']['allowed_paths']=tuple(d['recipe']['allowed_paths'])
source=BoundedContinuitySource(Path(d['root']),ContinuitySourceRecipe(**d['recipe']))
composed=compose_local_startup(gate,binding,source)
def current(b):
 assert b==binding
 composed.sources.preflight(b)
store=SQLiteCloseStore(composed.lifecycle.store,SQLiteLocalRuntimeStore(gate.operational_path),
 KnowledgeFileStore(gate.home),source_probe=current)
print(json.dumps(asdict(store.load(binding,d['request']))))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(
            {
                "gate": {k: str(v) for k, v in asdict(frozen["gate"]).items()},
                "binding": asdict(frozen["binding"]),
                "recipe": asdict(frozen["recipe"]),
                "root": str(ROOT),
                "request": frozen["request"].request_digest,
            }
        ),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(completed.stdout) == asdict(frozen["request"])
    _not_closed(frozen)


def test_initial_freeze_still_requires_live_source_verification(
    before_freeze: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        before_freeze["sources"].project_sources,
        "read_fragment",
        lambda *_args, **_kwargs: "not the approved source bytes",
    )
    with pytest.raises(PolicyViolation):
        _freeze(before_freeze)
    _not_closed(before_freeze, expected_state="open")
    with sqlite3.connect(before_freeze["path"]) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone()[0] == 0
        assert db.execute("select count(*) from local_job").fetchone()[0] == 0


@pytest.mark.parametrize(
    "table",
    [
        "context_manifest",
        "hydration_receipt",
        "continuity_checkpoint",
        "session_event",
        "continuity_close_request",
        "local_job",
        "local_outbox",
    ],
)
def test_frozen_evidence_tampering_cannot_load_or_finalize(
    frozen: dict[str, Any], table: str
) -> None:
    changes = {
        "context_manifest": "update context_manifest set body_json='{}'",
        "hydration_receipt": "update hydration_receipt set receipt_digest=?",
        "continuity_checkpoint": (
            "update continuity_checkpoint set covered_sequence=covered_sequence+1"
        ),
        "session_event": "update session_event set event_kind='POST_COMPACTION'",
        "continuity_close_request": "update continuity_close_request set input_json='{}'",
        "local_job": "update local_job set payload_json='{}'",
        "local_outbox": "update local_outbox set payload_json='{}'",
    }
    with sqlite3.connect(frozen["path"]) as db:
        triggers = db.execute(
            "select name,sql from sqlite_master where type='trigger' and tbl_name=?", (table,)
        ).fetchall()
        for name, _sql in triggers:
            db.execute('drop trigger "' + name.replace('"', '""') + '"')
        db.execute(
            changes[table], (digest("tampered-receipt"),) if table == "hydration_receipt" else ()
        )
        # Restore the exact schema: test the corrupted evidence, not a missing-trigger alarm.
        for _name, sql in triggers:
            db.execute(sql)
    with pytest.raises((PolicyViolation, ConfigurationError)):
        frozen["store"].load(frozen["binding"], frozen["request"].request_digest)
    with pytest.raises((PolicyViolation, ConfigurationError)):
        frozen["service"].finalize(frozen["binding"], frozen["request"].request_digest)
    _not_closed(frozen)


def test_frozen_load_rechecks_current_source_inside_its_writer_transaction(
    frozen: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = frozen["store"].source_probe
    locked = 0

    def probe(binding: Any) -> None:
        nonlocal locked
        try:
            with sqlite3.connect(frozen["path"], timeout=0) as db:
                db.execute("begin immediate")
                db.rollback()
        except sqlite3.OperationalError:
            locked += 1
            raise PolicyViolation("Injected source drift inside close writer") from None
        original(binding)

    monkeypatch.setattr(frozen["store"], "source_probe", probe)
    before = logical_database_digest(frozen["path"])
    with pytest.raises(PolicyViolation, match="inside close writer"):
        frozen["store"].load(frozen["binding"], frozen["request"].request_digest)
    assert locked == 1
    assert logical_database_digest(frozen["path"]) == before


@pytest.mark.parametrize("kind", ["source", "configuration"])
def test_frozen_request_does_not_bypass_current_source_or_configuration(
    frozen: dict[str, Any], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "source":
        monkeypatch.setattr(
            frozen["source"], "capture", lambda: replace(frozen["plan"], revision_ref="a" * 40)
        )
    else:
        config = frozen["home"] / "config.yaml"
        config.write_text(config.read_text() + "\nruntime:\n  log_level: DEBUG\n")
    with pytest.raises((PolicyViolation, ConfigurationError)):
        frozen["store"].load(frozen["binding"], frozen["request"].request_digest)
    _not_closed(frozen)


def test_frozen_replay_rejects_owner_and_summary_drift(frozen: dict[str, Any]) -> None:
    request = frozen["request"]
    with pytest.raises(PolicyViolation):
        frozen["store"].load(
            replace(frozen["binding"], session_id=str(uuid4())), request.request_digest
        )
    with pytest.raises(PolicyViolation):
        _freeze(frozen, replace(frozen["summary"], next_safe_step="Changed frozen instruction"))
    assert frozen["store"].load(frozen["binding"], request.request_digest) == request
    _not_closed(frozen)


def test_unknown_bookkeeping_kind_stays_pending_and_blocks_close(frozen: dict[str, Any]) -> None:
    request = frozen["request"]
    with frozen["base"]._transaction() as db:
        frozen["runtime"]._emit_outbox(
            db,
            job_id=request.job_id,
            event_kind="unreviewed.observation",
            payload={"job_id": request.job_id},
            idempotency_key="unknown-close-observation",
            created_at=request.input_body["created_at"],
        )
    _compile_and_deliver(frozen)
    assert _local_bookkeeping_delivery(frozen) == 2
    with pytest.raises(PolicyViolation, match="pending"):
        frozen["service"].finalize(frozen["binding"], request.request_digest)
    with sqlite3.connect(frozen["path"]) as db:
        assert (
            db.execute(
                "select d.state from local_outbox o"
                " join local_outbox_delivery d on d.outbox_id=o.id"
                " where o.event_kind='unreviewed.observation'"
            ).fetchone()[0]
            == "pending"
        )
    _not_closed(frozen)


@pytest.mark.parametrize("variant", ["missing", "user-changed"])
def test_missing_or_user_changed_projection_never_gets_final_close(
    frozen: dict[str, Any], variant: str
) -> None:
    _compile_and_deliver(frozen)
    _local_bookkeeping_delivery(frozen)
    projection = frozen["request"].projections(frozen["binding"])[0]
    path = frozen["home"] / projection.manifest.portable_ref
    altered = b"User altered this disposable candidate. Preserve it.\n"
    saved = path.with_name(path.name + ".saved")
    if variant == "missing":
        path.rename(saved)
    else:
        path.write_bytes(altered)
    with pytest.raises(PolicyViolation):
        frozen["service"].finalize(frozen["binding"], frozen["request"].request_digest)
    if variant == "missing":
        assert not path.exists() and saved.read_bytes() == projection.payload
    else:
        assert path.read_bytes() == altered
    _not_closed(frozen)


def test_legacy_source_digest_probe_is_read_only_under_existing_writer(
    composition: dict[str, Any],
) -> None:
    before = logical_database_digest(composition["path"])
    with composition["base"]._transaction():
        assert (
            composition["base"].source_content_digest(composition["binding"])
            == composition["plan"].content_digest
        )
        with pytest.raises(PolicyViolation):
            composition["base"].source_content_digest(
                replace(composition["binding"], client_id="claude-code")
            )
    assert logical_database_digest(composition["path"]) == before


def test_partial_candidate_publish_requires_explicit_repair_after_reopen(
    frozen: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    request, binding = frozen["request"], frozen["binding"]
    original = frozen["files"].create_note
    calls = 0

    def publish(manifest: Any, payload: bytes) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Injected second candidate publication interruption")
        result = original(manifest, payload)
        assert isinstance(result, Path)
        return result

    monkeypatch.setattr(frozen["files"], "create_note", publish)
    with pytest.raises(OSError, match="publication interruption"):
        frozen["service"].compile_once(binding, request.request_digest, **OWNER)
    with sqlite3.connect(frozen["path"]) as db:
        original_receipt = db.execute(
            "select r.id,r.status,r.evidence_digest from local_effect_receipt r"
            " join local_effect_claim c on c.id=r.claim_id where c.job_id=?",
            (request.job_id,),
        ).fetchone()
    assert original_receipt[1] == "unknown"
    reopened = _attach_close(frozen)
    assert reopened["store"].load(binding, request.request_digest).state == "recovery-required"
    assert (
        reopened["service"].compile_once(binding, request.request_digest, **OWNER).state
        == "recovery-required"
    )
    _not_closed(reopened)
    first = request.projections(binding)[0]
    assert (reopened["home"] / first.manifest.portable_ref).read_bytes() == first.payload
    reopened["service"].repair_generated_candidates(
        binding, request.request_digest, repair_key="explicit-composed-repair", **OWNER
    )
    reopened["service"].deliver_once(binding, request.request_digest, **OWNER)
    assert _local_bookkeeping_delivery(reopened) == 5
    receipt = reopened["service"].finalize(binding, request.request_digest)
    assert reopened["service"].finalize(binding, request.request_digest) == receipt
    with sqlite3.connect(frozen["path"]) as db:
        assert (
            db.execute(
                "select id,status,evidence_digest from local_effect_receipt where id=?",
                (original_receipt[0],),
            ).fetchone()
            == original_receipt
        )
        assert db.execute("select count(*) from local_recovery_resolution").fetchone()[0] == 1
