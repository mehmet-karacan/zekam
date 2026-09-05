from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.integration import test_local_continuity_v4_atomic_close as close_fixture
from tests.unit import test_local_continuity as continuity_fixture

from zekam.application.local_continuity import ContinuityEvent, ContinuityTail
from zekam.application.local_continuity_v4_writer import (
    FinalizeClosedWriteRequest,
    FrozenProjectionSnapshot,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite import local_continuity as continuity_module
from zekam.infrastructure.sqlite import local_continuity_v4_writer as writer_module
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_v4_writer import SQLiteDormantV4CloseWriter


def _continuity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SQLiteContinuityStore, Any, Any]:
    root = tmp_path / "source"
    source_ref = "fixture.py"
    root.mkdir()
    (root / source_ref).write_text("bounded source\n", encoding="utf-8")
    monkeypatch.setattr(continuity_fixture, "ROOT", root)
    monkeypatch.setattr(continuity_fixture, "SOURCE_REF", source_ref)
    monkeypatch.setattr(
        cast(Any, continuity_fixture).subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="HEAD\n"),
    )
    fixture = cast(Any, continuity_fixture.continuity)
    return cast(tuple[SQLiteContinuityStore, Any, Any], fixture.__wrapped__(tmp_path))


def _event(key: str = "event", *, large: bool = False) -> ContinuityEvent:
    refs = tuple(f"source/{index}/{'x' * 490}" for index in range(32)) if large else ()
    return ContinuityEvent("SESSION_START", key, "2026-09-05T12:00:00+00:00", source_refs=refs)


def _drop_matching_triggers(db: sqlite3.Connection, fragment: str) -> None:
    for (name,) in db.execute(
        "select name from sqlite_master where type='trigger' and name like ?", (f"%{fragment}%",)
    ).fetchall():
        db.execute(f'drop trigger "{name}"')


def test_continuity_exact_input_and_missing_schema_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(Exception, match="admitted current local schema"):
        SQLiteContinuityStore(tmp_path / "absent.db")
    store, binding, context = _continuity(tmp_path, monkeypatch)
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(ValidationFailed, match="Typed continuity binding"):
            store._assert_binding(db, cast(Any, object()))
    with pytest.raises(ValidationFailed, match="Typed continuity binding"):
        store.bind_session(cast(Any, object()))
    with pytest.raises(ValidationFailed, match="Typed continuity event"):
        store.append_event(binding, cast(Any, object()), expected_tail=ContinuityTail(0, None))
    with pytest.raises(ValidationFailed, match="Typed continuity event"):
        store.append_event(binding, _event(), expected_tail=cast(Any, object()))
    with pytest.raises(ValidationFailed, match="Typed local context"):
        store.hydrate(binding, cast(Any, object()), idempotency_key="hydrate")
    with pytest.raises(ValidationFailed, match="Typed continuity tail"):
        store.checkpoint(
            binding,
            expected_tail=cast(Any, object()),
            context_digest=digest("context"),
            idempotency_key="checkpoint",
            spool_digests=(),
        )
    with pytest.raises(ValidationFailed, match="exact spool digest tuple"):
        store.checkpoint(
            binding,
            expected_tail=ContinuityTail(0, None),
            context_digest=digest("context"),
            idempotency_key="checkpoint",
            spool_digests=cast(Any, []),
        )
    with pytest.raises(PolicyViolation, match="effect claim missing"):
        store.bind_effect(binding, "missing-claim")
    assert context.fragments


@pytest.mark.parametrize(
    "mutation,message",
    (
        ("binding", "stored binding payload"),
        ("realm", "project/realm"),
        ("source", "source unavailable"),
        ("config", "task/policy"),
        ("run", "run owner"),
    ),
)
def test_continuity_binding_authority_drift_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    store, binding, _context = _continuity(tmp_path, monkeypatch)
    with sqlite3.connect(store.path) as db:
        if mutation == "binding":
            _drop_matching_triggers(db, "continuity_session_binding")
            db.execute(
                "update continuity_session_binding set external_session_id='drift' "
                "where session_id=?",
                (binding.session_id,),
            )
        elif mutation == "realm":
            _drop_matching_triggers(db, "project_knowledge_realm")
            db.execute(
                "delete from project_knowledge_realm where project_id=?", (binding.project_id,)
            )
        elif mutation == "source":
            _drop_matching_triggers(db, "source_binding")
            db.execute("update source_binding set active=0")
        elif mutation == "config":
            _drop_matching_triggers(db, "config_revision")
            db.execute("update config_revision set active=0")
        else:
            _drop_matching_triggers(db, "run")
            db.execute("update run set plan_digest=? where id=?", (digest("drift"), binding.run_id))
    with pytest.raises(PolicyViolation, match=message):
        store.tail(binding)


@pytest.mark.parametrize("mutation", ("event-list", "event-extra", "envelope-missing", "column"))
def test_continuity_event_graph_corruption_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    store, binding, _context = _continuity(tmp_path, monkeypatch)
    store.append_event(binding, _event(), expected_tail=ContinuityTail(0, None))
    with sqlite3.connect(store.path) as db:
        _drop_matching_triggers(db, "session_event_detail")
        row = db.execute("select body_json from session_event_detail").fetchone()
        assert row is not None
        body = json.loads(row[0])
        if mutation == "event-list":
            body["event"] = []
        elif mutation == "event-extra":
            body["event"]["extra"] = False
        elif mutation == "envelope-missing":
            del body["binding_digest"]
        else:
            db.execute("update session_event_detail set sequence=2")
            body = None
        if body is not None:
            db.execute("update session_event_detail set body_json=?", (canonical_json(body),))
    with pytest.raises(PolicyViolation):
        store.tail(binding)


def test_continuity_append_hydrate_checkpoint_and_resume_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, binding, context = _continuity(tmp_path, monkeypatch)
    with pytest.raises(ValidationFailed, match="byte bound"):
        store.append_event(binding, _event(large=True), expected_tail=ContinuityTail(0, None))

    no_resolver = SQLiteContinuityStore(store.path)
    with pytest.raises(PolicyViolation, match="trusted source resolver"):
        no_resolver.hydrate(binding, context, idempotency_key="no-resolver")
    wrong_resolver = SQLiteContinuityStore(store.path, source_resolver=lambda *_args: "wrong")
    with pytest.raises(PolicyViolation, match="source mismatch"):
        wrong_resolver.hydrate(binding, context, idempotency_key="wrong-resolver")

    with pytest.raises(ValidationFailed, match="constraint rejected"):
        store.hydrate(binding, context, idempotency_key="scoped", checkpoint_digest=digest("prior"))
    manifest = store.hydrate(binding, context, idempotency_key="hydrate")
    tail = store.append_event(binding, _event(), expected_tail=ContinuityTail(0, None))
    with pytest.raises(ConcurrencyConflict, match="event boundary"):
        store.checkpoint(
            binding,
            expected_tail=ContinuityTail(0, None),
            context_digest=manifest,
            idempotency_key="wrong-tail",
            spool_digests=(),
        )
    checkpoint = store.checkpoint(
        binding,
        expected_tail=tail,
        context_digest=manifest,
        idempotency_key="checkpoint",
        spool_digests=(),
    )
    with pytest.raises(PolicyViolation, match="absent or corrupted"):
        store.resume(binding, digest("absent"))
    with sqlite3.connect(store.path) as db:
        _drop_matching_triggers(db, "continuity_checkpoint")
        db.execute(
            "update continuity_checkpoint set checkpoint_digest=? where session_id=?",
            (digest("drift"), binding.session_id),
        )
    with pytest.raises(PolicyViolation):
        store.checkpoint(
            binding,
            expected_tail=tail,
            context_digest=manifest,
            idempotency_key="checkpoint",
            spool_digests=(),
        )
    assert checkpoint.startswith("sha256:")


def _v4_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "source"
    target = root / close_fixture.SOURCE_REF
    target.parent.mkdir(parents=True)
    target.write_text("bounded source\n", encoding="utf-8")
    real_path = Path

    def mapped_path(value: object = ".") -> Path:
        if str(value) == "/Users/mkaracan/Projeler/akilli-kasa":
            return root
        return real_path(cast(Any, value))

    monkeypatch.setattr(close_fixture, "Path", mapped_path)
    path = tmp_path / "v4.db"
    return path, close_fixture._seed(path)


def test_writer_binding_spool_revision_and_manifest_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, seed = _v4_seed(tmp_path, monkeypatch)
    writer = close_fixture._v4_writer(path, seed)
    binding = close_fixture._binding()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match="binding unavailable"):
            SQLiteDormantV4CloseWriter._binding(
                db, replace(binding, session_id="018f0000-0000-7000-8000-000000000099")
            )
        with pytest.raises(PolicyViolation, match="attachment revision missing"):
            SQLiteDormantV4CloseWriter._verified_revision(None)
        with pytest.raises(PolicyViolation, match="bounded durable context missing"):
            writer._manifest(
                db,
                binding,
                digest("missing"),
                SQLiteDormantV4CloseWriter._current_revision(db, close_fixture.ATTACHMENT_ID),
                writer._source_snapshot(binding),
            )
        rows = SQLiteDormantV4CloseWriter._events(db, binding)
        spool = cast(Any, writer.spool)
        snapshot = replace(
            spool.handle.snapshot,
            entry_digests=(*spool.handle.snapshot.entry_digests, digest("extra")),
        )
        with pytest.raises(PolicyViolation, match="control spool suffix"):
            writer._spool_gate(db, rows, snapshot, binding, allow_controls=True)


def test_writer_owner_binding_and_projection_snapshot_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, seed = _v4_seed(tmp_path, monkeypatch)
    binding = close_fixture._binding()
    writer = close_fixture._v4_writer(path, seed)
    close_fixture._unsafe_fixture_mutation(
        path,
        trigger_names=("continuity_session_binding_immutable_update",),
        statement="update continuity_session_binding set external_session_id='drift'",
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match="owner binding drift"):
            writer._binding(db, binding)


def test_continuity_no_run_binding_takes_portable_owner_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, seed = _v4_seed(tmp_path, monkeypatch)
    monkeypatch.setattr(continuity_module, "SCHEMA_VERSION", 4)
    binding = cast(Any, seed["binding"])
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into config_revision values(?,?,?,?,?,?)",
            (
                "018f0000-0000-7000-8000-000000000099",
                binding.policy_digest,
                binding.task_digest,
                "{}",
                1,
                close_fixture.NOW,
            ),
        )
    store = SQLiteContinuityStore(path, source_resolver=lambda *_args: "bounded source\n")
    assert store.tail(binding) == cast(Any, seed["tail"])


@pytest.mark.parametrize("mutation", ("closed", "revision"))
def test_continuity_hydration_rechecks_session_and_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    store, binding, context = _continuity(tmp_path, monkeypatch)
    with sqlite3.connect(store.path) as db:
        if mutation == "closed":
            _drop_matching_triggers(db, "session")
            db.execute("update session set status='closing' where id=?", (binding.session_id,))
        else:
            _drop_matching_triggers(db, "source_snapshot")
            db.execute(
                "update source_snapshot set revision_ref='drift' where id=?",
                (binding.source_snapshot_id,),
            )
    message = "open session" if mutation == "closed" else "source revision mismatch"
    with pytest.raises(PolicyViolation, match=message):
        store.hydrate(binding, context, idempotency_key="hydrate")


def test_continuity_manifest_collision_missing_and_resolver_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, binding, context = _continuity(tmp_path, monkeypatch)
    manifest = store.hydrate(binding, context, idempotency_key="hydrate")
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match="durable context missing"):
            store._verified_manifest(db, binding, digest("missing"))
        no_resolver = SQLiteContinuityStore(store.path)
        with pytest.raises(PolicyViolation, match="trusted source resolver"):
            no_resolver._verified_manifest(db, binding, manifest)
        wrong = SQLiteContinuityStore(store.path, source_resolver=lambda *_args: "wrong")
        with pytest.raises(PolicyViolation, match="source provenance mismatch"):
            wrong._verified_manifest(db, binding, manifest)
        _drop_matching_triggers(db, "hydration_receipt")
        db.execute("delete from hydration_receipt")
        _drop_matching_triggers(db, "context_manifest")
        db.execute("update context_manifest set body_json='{}'")
    with pytest.raises(PolicyViolation, match="Stored context payload drift"):
        store.hydrate(binding, context, idempotency_key="hydrate-again")


def test_continuity_checkpoint_closed_and_resume_coverage_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, binding, context = _continuity(tmp_path, monkeypatch)
    manifest = store.hydrate(binding, context, idempotency_key="hydrate")
    tail = store.append_event(binding, _event(), expected_tail=ContinuityTail(0, None))
    checkpoint = store.checkpoint(
        binding,
        expected_tail=tail,
        context_digest=manifest,
        idempotency_key="checkpoint",
        spool_digests=(),
    )
    with sqlite3.connect(store.path) as db:
        _drop_matching_triggers(db, "session_event_detail")
        _drop_matching_triggers(db, "session_event")
        db.execute("delete from session_event_detail")
        db.execute("delete from session_event")
    with pytest.raises(PolicyViolation, match="covered event mismatch"):
        store.resume(binding, checkpoint)
    with sqlite3.connect(store.path) as db:
        _drop_matching_triggers(db, "session")
        db.execute("update session set status='closing' where id=?", (binding.session_id,))
    with pytest.raises(PolicyViolation, match="open session"):
        store.checkpoint(
            binding,
            expected_tail=tail,
            context_digest=manifest,
            idempotency_key="closed",
            spool_digests=(),
        )


def _frozen_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, object], SQLiteDormantV4CloseWriter, Any, str]:
    path, seed = _v4_seed(tmp_path, monkeypatch)
    writer = close_fixture._v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(close_fixture._request(seed))
    with sqlite3.connect(path) as db:
        frozen_revision = str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "where state='frozen'"
            ).fetchone()[0]
        )
    return path, seed, writer, frozen, frozen_revision


def test_writer_foreign_key_and_control_suffix_success_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _seed, writer, frozen, _revision = _frozen_writer(tmp_path, monkeypatch)

    class _ForeignKeysOff:
        def execute(self, statement: str) -> Any:
            return SimpleNamespace(fetchone=lambda: (0,))

        def close(self) -> None:
            return None

    with monkeypatch.context() as scoped:
        scoped.setattr(
            cast(Any, writer_module).sqlite3,
            "connect",
            lambda *_args, **_kwargs: _ForeignKeysOff(),
        )
        with pytest.raises(Exception, match="foreign keys unavailable"):
            writer._connect()

    binding = close_fixture._binding()
    extra = digest("control-extra")
    spool = cast(Any, writer.spool)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_matching_triggers(db, "continuity_control")
        db.execute(
            "insert into continuity_control_event values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                digest("control"),
                binding.session_id,
                binding.binding_digest,
                frozen.request_digest,
                binding.client_id,
                binding.device_id,
                binding.external_session_id,
                extra,
                digest("observation"),
                "018f0000-0000-7000-8000-000000000099",
                2,
                spool.handle.snapshot.entry_digests[0],
                "PreCompact",
                "pre_compaction",
                "rejected-after-freeze",
                "{}",
                close_fixture.NOW,
            ),
        )
        rows = writer._events(db, binding)
        snapshot = replace(
            spool.handle.snapshot,
            entry_digests=(*spool.handle.snapshot.entry_digests, extra),
        )
        writer._spool_gate(db, rows, snapshot, binding, allow_controls=True)


def test_writer_finalize_missing_request_and_projection_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, seed = _v4_seed(tmp_path, monkeypatch)
    writer = close_fixture._v4_writer(path, seed)
    binding = close_fixture._binding()
    request = FinalizeClosedWriteRequest(
        binding,
        digest("absent-request"),
        str(seed["revision_digest"]),
        "finalize",
        close_fixture.NOW,
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match="frozen request unavailable"):
            writer._frozen_for_finalize(db, request, writer._source_snapshot(binding))

    path, _seed, writer, frozen, _revision = _frozen_writer(tmp_path / "projection", monkeypatch)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match="locked projection evidence drift"):
            writer._projection_gate(db, binding, frozen, FrozenProjectionSnapshot(()))
        with (
            writer.projections.frozen(frozen) as handle,
            pytest.raises(PolicyViolation, match="projection manifest incomplete"),
        ):
            writer._projection_gate(db, binding, frozen, handle.snapshot)


def test_writer_finalize_requires_delivered_outbox_and_exact_frozen_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _seed, writer, frozen, _frozen_revision = _frozen_writer(tmp_path, monkeypatch)
    close_fixture._materialize_and_complete(path, frozen, monkeypatch)
    with sqlite3.connect(path) as db:
        _drop_matching_triggers(db, "local_outbox_receipt")
        db.execute("delete from local_outbox_receipt where outbox_id=?", (frozen.outbox_id,))
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation):
            writer._terminal_work(db, close_fixture._binding(), frozen)

    path, _seed, writer, frozen, _frozen_revision = _frozen_writer(
        tmp_path / "wrong-revision", monkeypatch
    )
    close_fixture._materialize_and_complete(path, frozen, monkeypatch)
    wrong = FinalizeClosedWriteRequest(
        close_fixture._binding(),
        frozen.request_digest,
        digest("wrong-frozen"),
        "finalize",
        close_fixture.NOW,
    )
    with pytest.raises(PolicyViolation, match="frozen revision"):
        writer.finalize_with_session_closed(wrong)


def test_writer_manifest_second_read_and_provenance_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, seed = _v4_seed(tmp_path, monkeypatch)
    writer = close_fixture._v4_writer(path, seed)
    binding = close_fixture._binding()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        revision = db.execute(
            "select * from continuity_hook_attachment_revision where state='hydrated'"
        ).fetchone()
        assert revision is not None

        class _MissingSecondRead:
            def execute(self, statement: str, parameters: object = ()) -> Any:
                if "body_json from context_manifest" in statement:
                    return SimpleNamespace(fetchone=lambda: None)
                return db.execute(statement, parameters)  # type: ignore[arg-type]

        source = writer._source_snapshot(binding)
        with pytest.raises(PolicyViolation, match="changed during bounded read"):
            writer._manifest(
                cast(Any, _MissingSecondRead()),
                binding,
                str(seed["manifest_digest"]),
                revision,
                source,
            )

        actual_verify = cast(Any, writer_module).verify_persisted_context_manifest
        actual = actual_verify(
            binding=binding,
            manifest_digest=str(seed["manifest_digest"]),
            row_columns=dict(
                db.execute(
                    "select manifest_digest,session_id,checkpoint_digest,token_budget,"
                    "token_count,body_json from context_manifest"
                ).fetchone()
            ),
            body_json=str(db.execute("select body_json from context_manifest").fetchone()[0]),
            active_hydration_receipt=dict(db.execute("select * from hydration_receipt").fetchone()),
            db_source_revision="HEAD",
            port_source_revision="HEAD",
        )
        bad = SimpleNamespace(
            fragments=actual.fragments,
            selected=(SimpleNamespace(provenance=object()),),
        )
        with monkeypatch.context() as scoped:
            scoped.setattr(writer_module, "verify_persisted_context_manifest", lambda **_kw: bad)
            with pytest.raises(PolicyViolation, match="exact selected provenance"):
                writer._manifest(
                    db,
                    binding,
                    str(seed["manifest_digest"]),
                    revision,
                    source,
                )


@pytest.mark.parametrize(
    ("mode", "statement", "parameters", "message"),
    (
        (
            "lease-recovery",
            "update local_outbox set idempotency_key=replace(idempotency_key,':1:',':2:') "
            "where idempotency_key like '%:recovery:%'",
            (),
            "recovery outbox fence drift",
        ),
        (
            "direct-reconciled",
            "update local_outbox set event_kind='job.completed',payload_json=?,payload_digest=? "
            "where idempotency_key like '%:terminal'",
            ({"state": "completed"},),
            "illegal reconciliation history",
        ),
        (
            "direct",
            "update local_job set state='failed' where id=?",
            ("JOB",),
            "compile delivery preceded completed job",
        ),
    ),
)
def test_writer_runtime_terminal_graph_tamper_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    statement: str,
    parameters: tuple[object, ...],
    message: str,
) -> None:
    path, seed = _v4_seed(tmp_path, monkeypatch)
    writer = close_fixture._v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(close_fixture._request(seed))
    close_fixture._materialize_runtime_variant(
        path, frozen, monkeypatch, mode=mode, resolved_unknown_delivery=False
    )
    if mode == "direct-reconciled":
        body = {"job_id": frozen.job_id, "state": "completed"}
        parameters = (canonical_json(body), digest(body))
    elif mode == "direct":
        parameters = (frozen.job_id,)
    if mode == "direct":
        with sqlite3.connect(path) as db:
            db.execute(statement, parameters)
    else:
        close_fixture._unsafe_fixture_mutation(
            path,
            trigger_names=("local_outbox_no_update",),
            statement=statement,
            parameters=parameters,
        )
    with pytest.raises(PolicyViolation, match=message):
        writer.freeze_with_preclose(close_fixture._request(seed))


def test_writer_private_frozen_input_and_closed_event_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _seed, writer, frozen, revision = _frozen_writer(tmp_path, monkeypatch)
    request = FinalizeClosedWriteRequest(
        close_fixture._binding(), frozen.request_digest, revision, "finalize", close_fixture.NOW
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_matching_triggers(db, "continuity_close_request")
        db.execute(
            "update continuity_close_request set input_json='{}' where request_digest=?",
            (frozen.request_digest,),
        )
        with pytest.raises(PolicyViolation, match="frozen request integrity drift"):
            writer._frozen_for_finalize(db, request, writer._source_snapshot(request.binding))

    path, _seed, writer, frozen, revision = _frozen_writer(tmp_path / "closed", monkeypatch)
    close_fixture._materialize_and_complete(path, frozen, monkeypatch)
    request = FinalizeClosedWriteRequest(
        close_fixture._binding(), frozen.request_digest, revision, "finalize", close_fixture.NOW
    )
    receipt = writer.finalize_with_session_closed(request)
    wrong = replace(request, expected_frozen_revision_digest=digest("wrong-replay"))
    with pytest.raises(PolicyViolation, match="closed replay predecessor drift"):
        writer.finalize_with_session_closed(wrong)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        closed = db.execute(
            "select * from continuity_hook_attachment_revision where state='closed'"
        ).fetchone()
        assert closed is not None
        projections = json.loads(
            db.execute("select projections_json from close_receipt").fetchone()[0]
        )
        _drop_matching_triggers(db, "continuity_internal_event")
        db.execute(
            "delete from continuity_internal_event_receipt where event_digest=?",
            (closed["session_closed_event_digest"],),
        )
        with pytest.raises(PolicyViolation, match="terminal event graph drift"):
            writer._closed_graph(
                db,
                request.binding,
                frozen,
                closed,
                projections,
                frozen.delivery_evidence(request.binding),
            )
    assert receipt


def test_writer_transaction_exceptions_are_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, seed = _v4_seed(tmp_path, monkeypatch)
    writer = close_fixture._v4_writer(path, seed)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            writer,
            "_load_frozen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("busy")),
        )
        with pytest.raises(ConcurrencyConflict, match="SQLite writer unavailable"):
            writer.freeze_with_preclose(close_fixture._request(seed))
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone() == (0,)

    path, _seed, writer, frozen, revision = _frozen_writer(tmp_path / "final", monkeypatch)
    close_fixture._materialize_and_complete(path, frozen, monkeypatch)
    request = FinalizeClosedWriteRequest(
        close_fixture._binding(), frozen.request_digest, revision, "finalize", close_fixture.NOW
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(
            writer,
            "_terminal_work",
            lambda *_args: (_ for _ in ()).throw(sqlite3.OperationalError("busy")),
        )
        with pytest.raises(ConcurrencyConflict, match="SQLite writer unavailable"):
            writer.finalize_with_session_closed(request)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from close_receipt").fetchone() == (0,)


def test_writer_rejects_noncanonical_session_and_evidence_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, seed = _v4_seed(tmp_path, monkeypatch)

    @contextmanager
    def wrong_frozen(_value: object) -> Any:
        yield SimpleNamespace(snapshot=object(), recheck=lambda: None)

    writer = close_fixture._v4_writer(path, seed)
    writer.spool = SimpleNamespace(frozen=wrong_frozen)
    with pytest.raises(ValidationFailed, match="exact spool snapshot"):
        writer.freeze_with_preclose(close_fixture._request(seed))
