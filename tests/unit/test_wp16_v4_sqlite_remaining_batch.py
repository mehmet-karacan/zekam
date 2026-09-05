from __future__ import annotations

import datetime as dt
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid5

import pytest
from tests.integration import test_local_continuity_v4_internal as b1_fixture
from tests.integration import test_local_continuity_v4_pre_compaction as compact_fixture
from tests.integration import test_local_continuity_v4_session_start as ingress_fixture

from zekam.application import local_continuity_v4_compaction as compaction_contract
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.local_continuity import ContinuityBinding, ContinuityTail
from zekam.application.local_continuity_v4_ingress import (
    ManagedInvocationSnapshot,
)
from zekam.application.local_continuity_v4_internal import (
    DirectEffectOutcomeRequest,
    EffectClaimRequest,
    FrozenDirectEffectOutcomeSnapshot,
    FrozenEffectClaimSnapshot,
    FrozenTurnCommitSnapshot,
    TurnCommitRequest,
)
from zekam.application.local_continuity_v4_recovery import B2_EVENT_NS
from zekam.application.local_continuity_v4_writer import CanonicalManifestProvenance
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.infrastructure.clients.codex_macos_0151_lifecycle import CodexMacOS0151Event
from zekam.infrastructure.sqlite import local_continuity_v4_compaction as compact
from zekam.infrastructure.sqlite import local_continuity_v4_ingress as ingress
from zekam.infrastructure.sqlite import local_continuity_v4_internal as internal
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.local_continuity_source_authority import (
    local_source_authority_path,
)
from zekam.infrastructure.sqlite.local_continuity_v4_writer import (
    SQLiteDormantV4CloseWriter,
)


def _prepare_ingress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ContinuityBinding, Any, Any, ClientLifecycleSpool, CodexMacOS0151Event, Any]:
    root = tmp_path / "source"
    root.mkdir(parents=True)
    source_ref = "fixture.py"
    (root / source_ref).write_text("bounded source\n", encoding="utf-8")
    monkeypatch.setattr(ingress_fixture, "ROOT", root)
    monkeypatch.setattr(ingress_fixture, "SOURCE_REF", source_ref)
    seam = cast(Any, ingress_fixture._test_only_sealed_owner_seams)
    seam.__wrapped__(monkeypatch)
    return cast(
        tuple[
            Path,
            ContinuityBinding,
            Any,
            Any,
            ClientLifecycleSpool,
            CodexMacOS0151Event,
            Any,
        ],
        ingress_fixture._prepared(tmp_path),
    )


def _prepare_internal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ContinuityBinding, Any, Any, Any, Any]:
    root = tmp_path / "source"
    root.mkdir()
    source_ref = "fixture.py"
    source = root / source_ref
    source.write_text("bounded source\n", encoding="utf-8")
    monkeypatch.setattr(ingress_fixture, "ROOT", root)
    monkeypatch.setattr(ingress_fixture, "SOURCE_REF", source_ref)
    monkeypatch.setattr(b1_fixture, "SOURCE", source)
    monkeypatch.setattr(b1_fixture, "_load_slice_a", lambda: ingress_fixture)
    return cast(
        tuple[Path, ContinuityBinding, Any, Any, Any, Any],
        b1_fixture._prepared(tmp_path, monkeypatch),
    )


def _tail(path: Path, binding: ContinuityBinding) -> ContinuityTail:
    return b1_fixture._tail(path, binding)


def _running_job(path: Path, binding: ContinuityBinding, monkeypatch: pytest.MonkeyPatch) -> str:
    return b1_fixture._running_job(path, binding, monkeypatch)


def test_ingress_constructor_and_schema_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = ingress_fixture._binding()
    manager = ingress_fixture._Manager(binding)
    context = ingress_fixture._ContextPort(binding)
    spool = ClientLifecycleSpool(tmp_path / "spool", client_id="codex")
    monkeypatch.setattr(ingress, "_trusted_process_owner", lambda value: value is manager)
    from zekam.infrastructure import local_continuity_v4_composition as composition

    monkeypatch.setattr(composition, "_trusted_context_owner", lambda value: value is context)
    with pytest.raises(ValidationFailed, match="absolute"):
        ingress.SQLiteCodexV4Ingress(
            cast(Path, "relative.db"),
            binding,
            process_manager=manager,
            context_port=context,
            spool=spool,
        )
    with pytest.raises(ValidationFailed, match="binding"):
        ingress.SQLiteCodexV4Ingress(
            tmp_path / "missing.db",
            cast(ContinuityBinding, object()),
            process_manager=manager,
            context_port=context,
            spool=spool,
        )
    with pytest.raises(ValidationFailed, match="context"):
        ingress.SQLiteCodexV4Ingress(
            tmp_path / "missing.db",
            binding,
            process_manager=manager,
            context_port=cast(Any, object()),
            spool=spool,
        )
    monkeypatch.setattr(composition, "_trusted_context_owner", lambda _value: True)
    with pytest.raises(ValidationFailed, match="spool"):
        ingress.SQLiteCodexV4Ingress(
            tmp_path / "missing.db",
            binding,
            process_manager=manager,
            context_port=context,
            spool=cast(ClientLifecycleSpool, object()),
        )
    store = ingress.SQLiteCodexV4Ingress(
        tmp_path / "missing.db",
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    )
    with pytest.raises(ConfigurationError, match="integrity"):
        store._schema()


def test_ingress_real_attach_start_and_replay_use_exact_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _binding, _manager, _context, _spool, event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    attachment = store.attach_process()
    first = store.session_start(event)
    replay = store.session_start(event)
    assert first.replay is False and replay.replay is True
    assert first.stdout == replay.stdout
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_hook_attachment").fetchone()[0] == 1
        assert db.execute("select count(*) from continuity_native_event_receipt").fetchone()[0] == 1
        assert (
            db.execute("select attachment_digest from continuity_hook_attachment").fetchone()[0]
            == attachment
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-attachment",
        "missing-generation",
        "missing-ancestry",
        "missing-native",
        "missing-detail",
        "missing-hydration",
        "missing-source",
        "event-body-drift",
        "native-body-drift",
        "ancestry-body-drift",
    ),
)
def test_ingress_replay_rejects_partial_or_drifting_durable_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    path, _binding, _manager, _context, _spool, event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    store.attach_process()
    store.session_start(event)
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=off")
        triggers = db.execute("select name from sqlite_master where type='trigger'").fetchall()
        for trigger in triggers:
            db.execute(f'drop trigger "{trigger[0]}"')
        if mutation == "missing-attachment":
            db.execute("delete from continuity_hook_attachment")
        elif mutation == "missing-generation":
            db.execute("delete from continuity_hook_process_generation")
        elif mutation == "missing-ancestry":
            db.execute("delete from continuity_hook_invocation_ancestry_receipt")
        elif mutation == "missing-native":
            db.execute("delete from continuity_native_event_receipt")
        elif mutation == "missing-detail":
            db.execute("delete from session_event_detail")
        elif mutation == "missing-hydration":
            db.execute("delete from hydration_receipt")
        elif mutation == "missing-source":
            db.execute("delete from source_snapshot")
        elif mutation == "event-body-drift":
            db.execute("update session_event_detail set body_json='{}'")
        elif mutation == "native-body-drift":
            db.execute("update continuity_native_event_receipt set body_json='{}'")
        else:
            db.execute("update continuity_hook_invocation_ancestry_receipt set body_json='{}'")
    monkeypatch.setattr(store, "_schema", lambda: None)
    with pytest.raises((PolicyViolation, ConfigurationError, sqlite3.Error)):
        store.session_start(event)


def test_ingress_wrong_live_snapshots_and_event_selectors_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, binding, manager, _context, _spool, event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(manager, "capture_process", lambda _binding: object())
        with pytest.raises(ValidationFailed, match="process snapshot"):
            store.attach_process()
    _path, binding, manager, _context, _spool, event, store = _prepare_ingress(
        tmp_path / "second", monkeypatch
    )
    process = manager.process
    attachment_id = "018f0000-0000-7000-8000-000000000999"
    wrong = ingress_fixture._replace_test_snapshot(
        process,
        attachment_id=attachment_id,
        reviewed_commands=tuple(
            replace(command, attachment_id=attachment_id) for command in process.reviewed_commands
        ),
    )
    monkeypatch.setattr(manager, "capture_process", lambda _binding: wrong)
    with pytest.raises(PolicyViolation, match="attachment identity"):
        store.attach_process()
    monkeypatch.setattr(manager, "capture_process", lambda _binding: process)
    store.attach_process()
    bad = CodexMacOS0151Event(
        binding.external_session_id,
        "PreCompact",
        None,
        "turn-1",
        "manual",
        None,
        digest("wire"),
    )
    with pytest.raises(PolicyViolation, match="SessionStart"):
        store.session_start(bad)
    with pytest.raises(ValidationFailed, match="parsed"):
        store.session_start(cast(CodexMacOS0151Event, object()))
    monkeypatch.setattr(manager, "capture_invocation", lambda *_args, **_kwargs: object())
    with pytest.raises(ValidationFailed, match="invocation"):
        store.session_start(event)


@pytest.mark.parametrize("failure", ("process", "context", "head", "generation", "invocation"))
def test_ingress_fresh_session_start_rejects_each_precommit_authority_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    path, _binding, manager, context, _spool, event, store = _prepare_ingress(tmp_path, monkeypatch)
    store.attach_process()
    expected: type[Exception]
    if failure == "process":
        monkeypatch.setattr(manager, "capture_process", lambda _binding: object())
        expected = ValidationFailed
    elif failure == "context":
        monkeypatch.setattr(context, "build", lambda *_args, **_kwargs: object())
        expected = ValidationFailed
    elif failure == "head":
        with sqlite3.connect(path) as db:
            triggers = db.execute("select name from sqlite_master where type='trigger'").fetchall()
            for trigger in triggers:
                db.execute(f'drop trigger "{trigger[0]}"')
            db.execute(
                "update continuity_hook_attachment_revision set state='pre-compact-committed'"
            )
        monkeypatch.setattr(store, "_schema", lambda: None)
        expected = PolicyViolation
    elif failure == "generation":
        with sqlite3.connect(path) as db:
            triggers = db.execute("select name from sqlite_master where type='trigger'").fetchall()
            for trigger in triggers:
                db.execute(f'drop trigger "{trigger[0]}"')
            db.execute("delete from continuity_hook_process_generation")
        monkeypatch.setattr(store, "_schema", lambda: None)
        expected = PolicyViolation
    else:
        original = manager.capture_invocation

        def drifted(*args: object, **kwargs: object) -> ManagedInvocationSnapshot:
            value = original(*args, **kwargs)
            return cast(
                ManagedInvocationSnapshot,
                ingress_fixture._replace_test_snapshot(value, spool_digest=digest("wrong-spool")),
            )

        monkeypatch.setattr(manager, "capture_invocation", drifted)
        expected = PolicyViolation
    with pytest.raises(expected):
        store.session_start(event)


def test_ingress_replay_requires_live_process_and_exact_historical_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _binding, manager, context, _spool, event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    store.attach_process()
    store.session_start(event)
    with monkeypatch.context() as scoped:
        scoped.setattr(manager, "capture_process", lambda _binding: object())
        with pytest.raises(ValidationFailed, match="process snapshot"):
            store.session_start(event)
    with monkeypatch.context() as scoped:
        scoped.setattr(context, "build", lambda *_args, **_kwargs: object())
        with pytest.raises(ValidationFailed, match="historical context"):
            store.session_start(event)


def _captured_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Any, Any, CodexMacOS0151Event, Any, ManagedInvocationSnapshot, Any, str, str]:
    path, binding, manager, context, spool, event, store = _prepare_ingress(tmp_path, monkeypatch)
    store.attach_process()
    captured: list[ManagedInvocationSnapshot] = []
    original = manager.capture_invocation

    def capture(*args: object, **kwargs: object) -> ManagedInvocationSnapshot:
        value = cast(ManagedInvocationSnapshot, original(*args, **kwargs))
        captured.append(value)
        return value

    monkeypatch.setattr(manager, "capture_invocation", capture)
    store.session_start(event)
    monkeypatch.setattr(manager, "capture_invocation", original)
    entry = spool.read_session_entries(client_id="codex", session_id=binding.external_session_id)[0]
    event_id = ingress._event_uuid(binding, entry.entry_digest)
    key = ingress._operation_key(event_id)
    return path, manager, context, event, entry, captured[0], store, event_id, key


@pytest.mark.parametrize(
    "drift",
    (
        "attachment",
        "process",
        "context-type",
        "context-drift",
        "manifest",
        "hydration",
        "source",
        "detail",
        "native",
        "ancestry",
        "event-body",
        "native-body",
        "ancestry-body",
    ),
)
def test_ingress_direct_replay_rejects_every_partial_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    path, manager, context, event, entry, invocation, store, event_id, key = _captured_replay(
        tmp_path, monkeypatch
    )
    if drift == "process":
        monkeypatch.setattr(manager, "capture_process", lambda _binding: object())
    elif drift == "context-type":
        monkeypatch.setattr(context, "build", lambda *_args, **_kwargs: object())
    elif drift == "context-drift":
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            attachment = db.execute(
                "select attachment_id from continuity_hook_attachment"
            ).fetchone()
            assert attachment is not None
            current = store._current_revision(db, str(attachment[0]))
            verified = store._verify_hydrated_graph(db, event=event, entry=entry, revision=current)
        context.environment_label = "drifted-environment"
        context.expected_environment_evidence_digest = digest(context.environment_label)
        monkeypatch.setattr(store, "_verify_hydrated_graph", lambda *_args, **_kwargs: verified)
    else:
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            attachment = db.execute(
                "select attachment_id from continuity_hook_attachment"
            ).fetchone()
            assert attachment is not None
            current = store._current_revision(db, str(attachment[0]))
            verified = store._verify_hydrated_graph(db, event=event, entry=entry, revision=current)
            _drop_guards(db)
            if drift == "attachment":
                db.execute("delete from continuity_hook_attachment")
            elif drift == "manifest":
                db.execute("delete from context_manifest")
            elif drift == "hydration":
                db.execute("delete from hydration_receipt")
            elif drift == "source":
                db.execute("delete from source_snapshot")
            elif drift == "detail":
                db.execute("delete from session_event_detail")
            elif drift == "native":
                db.execute("delete from continuity_native_event_receipt")
            elif drift == "ancestry":
                db.execute("delete from continuity_hook_invocation_ancestry_receipt")
            elif drift == "event-body":
                db.execute("update session_event_detail set body_json='{}'")
            elif drift == "native-body":
                db.execute("update continuity_native_event_receipt set body_json='{}'")
            else:
                db.execute("update continuity_hook_invocation_ancestry_receipt set body_json='{}'")
        monkeypatch.setattr(store, "_schema", lambda: None)
        if drift != "attachment":
            monkeypatch.setattr(store, "_verify_hydrated_graph", lambda *_args, **_kwargs: verified)
    with pytest.raises((PolicyViolation, ValidationFailed, ConfigurationError, sqlite3.Error)):
        store._replay(event, entry, invocation, event_id, key)


def test_compaction_binding_resolution_and_rollover_are_restart_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, manager, _context, _spool, _start, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    store.attach_process()
    store.session_start(_start)
    with sqlite3.connect(path) as db:
        triggers = db.execute(
            "select name from sqlite_master where type='trigger' and tbl_name='source_binding'"
        ).fetchall()
        for trigger in triggers:
            db.execute(f'drop trigger "{trigger[0]}"')
        db.execute("update source_binding set source_kind='git'")
    monkeypatch.setattr(operational_schema, "_validate_connection", lambda _db: 4)
    event = compact_fixture._precompact_event(binding)
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        resolved = compact._resolve_existing_binding(cast(Any, db), event)
    assert resolved.head_state == "hydrated"
    assert (
        compact.rollover_existing_precompaction_process(
            path, event, tmp_path / "source", resolved, manager
        )
        == resolved
    )
    process = ingress_fixture._replace_test_snapshot(
        manager.process,
        captured_at="2026-09-03T12:00:01+00:00",
        native_pid=303,
        native_start_token="native-restart",
    )
    manager.process = process

    def resolve_after(
        selected_path: Path, selected_event: CodexMacOS0151Event, *, cwd: Path
    ) -> Any:
        assert selected_path == path and cwd == tmp_path / "source"
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as check:
            check.row_factory = sqlite3.Row
            return compact._resolve_existing_binding(cast(Any, check), selected_event)

    monkeypatch.setattr(compact, "resolve_existing_precompaction_binding", resolve_after)
    rolled = compact.rollover_existing_precompaction_process(
        path, event, tmp_path / "source", resolved, manager
    )
    assert (
        rolled.head_state == "hydrated"
        and rolled.head_revision_digest != resolved.head_revision_digest
    )
    with sqlite3.connect(path) as db:
        assert (
            db.execute("select max(generation) from continuity_hook_process_generation").fetchone()[
                0
            ]
            == 2
        )


def test_compaction_resolver_and_rollover_reject_wrong_public_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, manager, _context, _spool, start, store = _prepare_ingress(tmp_path, monkeypatch)
    store.attach_process()
    store.session_start(start)
    with sqlite3.connect(path) as db:
        triggers = db.execute(
            "select name from sqlite_master where type='trigger' and tbl_name='source_binding'"
        ).fetchall()
        for trigger in triggers:
            db.execute(f'drop trigger "{trigger[0]}"')
        db.execute("update source_binding set source_kind='git'")
    monkeypatch.setattr(operational_schema, "_validate_connection", lambda _db: 4)
    event = compact_fixture._precompact_event(binding)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(ValidationFailed, match="raw selector"):
            compact._resolve_existing_binding(cast(Any, db), start)
        resolved = compact._resolve_existing_binding(cast(Any, db), event)
    with pytest.raises(ValidationFailed, match="rollover anchor"):
        compact.rollover_existing_precompaction_process(
            path, event, tmp_path, cast(Any, object()), manager
        )
    with monkeypatch.context() as scoped:
        scoped.setattr(manager, "capture_process", lambda _binding: object())
        with pytest.raises(ValidationFailed, match="process snapshot"):
            compact.rollover_existing_precompaction_process(
                path, event, tmp_path, resolved, manager
            )
    wrong_attachment = ingress_fixture._replace_test_snapshot(
        manager.process,
        attachment_id="018f0000-0000-7000-8000-000000000998",
        reviewed_commands=tuple(
            replace(
                command,
                attachment_id="018f0000-0000-7000-8000-000000000998",
            )
            for command in manager.process.reviewed_commands
        ),
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(manager, "capture_process", lambda _binding: wrong_attachment)
        with pytest.raises(PolicyViolation, match="attachment mismatch"):
            compact.rollover_existing_precompaction_process(
                path, event, tmp_path, resolved, manager
            )


def _captured_precompact_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ContinuityBinding, Any, Any, Any, Any, Any, tuple[Any, ...]]:
    path, binding, manager, spool, writer, source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    event = compact_fixture._precompact_event(binding)
    plans: list[Any] = []
    invocations: list[Any] = []
    original_apply = compact._OwnedImmediateTransaction.apply_precompaction_graph
    original_capture = manager.capture_precompaction_invocation

    def apply(owner: Any, plan: Any) -> None:
        plans.append(plan)
        original_apply(owner, plan)

    def capture(*args: object, **kwargs: object) -> Any:
        value = original_capture(*args, **kwargs)
        invocations.append(value)
        return value

    monkeypatch.setattr(compact._OwnedImmediateTransaction, "apply_precompaction_graph", apply)
    monkeypatch.setattr(manager, "capture_precompaction_invocation", capture)
    assert writer.pre_compaction(event).status == "checkpoint-ready"
    monkeypatch.setattr(
        compact._OwnedImmediateTransaction, "apply_precompaction_graph", original_apply
    )
    monkeypatch.setattr(manager, "capture_precompaction_invocation", original_capture)
    entries = spool.read_session_entries(client_id="codex", session_id=binding.external_session_id)
    return path, binding, writer, source, plans[0], invocations[0], event, entries


@pytest.mark.parametrize(
    "drift",
    (
        "planned-row",
        "event-detail",
        "revision-row",
        "session-closed",
        "spool-prefix",
        "current-revision",
        "checkpoint",
        "rw-decision",
    ),
)
def test_compaction_reopened_verifier_rejects_each_durable_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    path, binding, writer, _source, plan, invocation, _event, entries = _captured_precompact_plan(
        tmp_path, monkeypatch
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        if drift == "planned-row":
            db.execute(
                "update continuity_internal_event_receipt set body_json='{}' "
                "where receipt_digest=?",
                (plan.checkpoint_receipt_digest,),
            )
        elif drift == "event-detail":
            db.execute(
                "update session_event_detail set body_json='{}' where event_digest=?",
                (plan.checkpoint_event_digest,),
            )
        elif drift == "revision-row":
            db.execute(
                "update continuity_hook_attachment_revision set body_json='{}' "
                "where revision_digest=?",
                (plan.revision_digest,),
            )
        elif drift == "session-closed":
            db.execute("update session set status='closing' where id=?", (binding.session_id,))
        elif drift == "spool-prefix":
            db.execute(
                "update session_event_detail set spool_digest=? where spool_digest is not null",
                (digest("wrong-spool"),),
            )
        elif drift == "current-revision":
            db.execute(
                "update continuity_hook_attachment_revision set state='hydrated' "
                "where revision_digest=?",
                (plan.revision_digest,),
            )
        elif drift == "checkpoint":
            db.execute("delete from continuity_checkpoint")
    monkeypatch.setattr(writer, "_schema", lambda _db: None)
    deadline = compaction_contract._issue_deadline(writer.generation, lambda: 1_000_000_000)
    with closing(writer._connect(deadline, read_only=drift != "rw-decision")) as verify:
        verify.execute("begin")
        with pytest.raises((PolicyViolation, ConfigurationError)):
            writer._verify_plan(
                cast(Any, verify),
                plan,
                entries,
                invocation,
                deadline,
                issue_decision=True,
            )


def test_compaction_exact_plan_rows_rejects_missing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _binding, writer, _source, plan, _invocation, _event, _entries = (
        _captured_precompact_plan(tmp_path, monkeypatch)
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        db.execute(
            "delete from continuity_native_event_receipt where receipt_digest=?",
            (plan.native_receipt_digest,),
        )
        with pytest.raises(PolicyViolation, match="planned row"):
            writer._exact_plan_rows(cast(Any, db), plan)


def test_compaction_real_success_replay_restart_and_selected_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, manager, spool, writer, source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    event = compact_fixture._precompact_event(binding)
    first = writer.pre_compaction(event)
    replay = writer.pre_compaction(event)
    restarted = compact.SQLiteDormantV4PreCompactionWriter(
        path, binding, spool=spool, generation=writer.generation
    )
    cast(Any, restarted).process_manager = manager
    cast(Any, restarted).source = source
    again = restarted.pre_compaction(event)
    assert (first.status, replay.replay, again.replay) == ("checkpoint-ready", True, True)
    assert first.ack_decision_digest == replay.ack_decision_digest == again.ack_decision_digest


def test_compaction_constructors_transaction_states_and_durability_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = compact_fixture._binding()
    spool = ClientLifecycleSpool(tmp_path / "spool", client_id="codex")
    with pytest.raises(ValidationFailed, match="absolute"):
        compact.SQLiteDormantV4PreCompactionWriter(
            cast(Path, "relative.db"), binding, spool=spool, generation=cast(Any, object())
        )
    db = sqlite3.connect(":memory:")
    owner = compact._OwnedImmediateTransaction(db)
    with pytest.raises(PolicyViolation, match="not planned"):
        owner.applying()
    owner.planned(digest("plan"))
    with pytest.raises(PolicyViolation, match="state"):
        owner.planned(digest("other"))
    owner.applying()
    with pytest.raises(PolicyViolation, match="operation"):
        owner.apply_precompaction_graph(cast(Any, SimpleNamespace(ack_decision_digest=digest("x"))))
    with pytest.raises(PolicyViolation, match="verification"):
        owner.verified(digest("x"))
    owner.abort()
    db.close()
    with pytest.raises(ValidationFailed, match="bound"):
        compact._key("x" * 600, "suffix")


@pytest.mark.parametrize("case", ("type", "owner", "links", "mode"))
def test_compaction_source_authority_file_identity_rejects_each_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    path = tmp_path / "authority"
    path.write_bytes(b"authority")
    path.chmod(0o600)
    if case == "type":
        path.unlink()
        path.mkdir()
    elif case == "links":
        os.link(path, tmp_path / "other")
    elif case == "mode":
        path.chmod(0o644)
    elif case == "owner":
        original = Path.lstat

        def wrong_owner(self: Path) -> os.stat_result:
            value = original(self)
            values = list(value)
            values[4] = value.st_uid + 1
            return os.stat_result(values)

        monkeypatch.setattr(Path, "lstat", wrong_owner)
    with pytest.raises(PolicyViolation, match="file identity"):
        compact._GenerationSource._file_ok(path)


def test_compaction_durability_and_unheld_source_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    source = compact._GenerationSource(writer.generation)
    with pytest.raises(PolicyViolation, match="not held"):
        source._unchanged(binding)
    db = sqlite3.connect(":memory:")
    db.execute("pragma foreign_keys=on")
    durability = compact._GenerationDurability(writer.generation)
    with pytest.raises(PolicyViolation, match="state invalid"):
        durability.verify(
            cast(Any, db),
            cast(Any, SimpleNamespace(assert_generation=lambda _: None)),
            read_only=cast(bool, 1),
        )
    db.close()


def test_internal_real_turn_claim_outcome_and_restart_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, turn, claim, outcome = _prepare_internal(tmp_path, monkeypatch)
    turn_request = TurnCommitRequest(
        binding,
        "user",
        "turn/018f0000-0000-7000-8000-000000000701",
        _tail(path, binding),
    )
    first = producer.commit_turn(turn_request)
    second_request = TurnCommitRequest(
        binding,
        "assistant",
        "turn/018f0000-0000-7000-8000-000000000702",
        _tail(path, binding),
    )
    assistant = producer.commit_turn(second_request)
    job_id = _running_job(path, binding, monkeypatch)
    claim_request = EffectClaimRequest(binding, job_id, _tail(path, binding))
    claimed = producer.claim_effect(claim_request)
    outcome_request = DirectEffectOutcomeRequest(
        binding, claimed.producer_ref, _tail(path, binding)
    )
    completed = producer.record_direct_outcome(outcome_request)
    assert [first.event_kind, assistant.event_kind, claimed.event_kind, completed.event_kind] == [
        "USER_TURN_COMMITTED",
        "ASSISTANT_TURN_COMMITTED",
        "TOOL_EFFECT_CLAIMED",
        "TOOL_EFFECT_COMPLETED",
    ]
    reopened = internal.SQLiteDormantV4InternalProducer(
        path,
        binding,
        turn_issuer=turn,
        claim_issuer=claim,
        outcome_issuer=outcome,
    )
    assert reopened.commit_turn(turn_request).replay
    assert reopened.claim_effect(claim_request).replay
    assert reopened.record_direct_outcome(outcome_request).replay


def _drop_guards(db: sqlite3.Connection) -> None:
    db.execute("pragma foreign_keys=off")
    for row in db.execute("select name from sqlite_master where type='trigger'").fetchall():
        db.execute(f'drop trigger "{row[0]}"')


def _completed_internal_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ContinuityBinding, Any, str, str]:
    path, binding, producer, _turn, _claim, _outcome = _prepare_internal(tmp_path, monkeypatch)
    producer.commit_turn(
        TurnCommitRequest(
            binding,
            "user",
            "turn/018f0000-0000-7000-8000-000000000710",
            _tail(path, binding),
        )
    )
    job_id = _running_job(path, binding, monkeypatch)
    claimed = producer.claim_effect(EffectClaimRequest(binding, job_id, _tail(path, binding)))
    producer.record_direct_outcome(
        DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
    )
    return path, binding, producer, job_id, claimed.producer_ref


@pytest.mark.parametrize(
    "drift",
    (
        "turn-cardinality",
        "turn-missing",
        "claim-binding-missing",
        "claim-event-missing",
        "claim-id",
        "claim-idempotency",
        "claim-operation",
        "terminal-event-missing",
        "receipt-id",
        "receipt-before-claim",
        "running-lease-missing",
        "terminal-retained-lease",
    ),
)
def test_internal_verifier_rejects_each_corrupt_durable_relation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    path, binding, _producer, job_id, _claim_id = _completed_internal_graph(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        if drift == "turn-cardinality":
            db.execute(
                "delete from continuity_internal_event_receipt where turn_commit_digest is not null"
            )
        elif drift == "turn-missing":
            row = db.execute("select * from continuity_turn_commit_receipt").fetchone()
            assert row is not None
            values = list(row)
            values[0] = digest("replacement-turn")
            db.execute("delete from continuity_turn_commit_receipt")
            db.execute(
                "insert into continuity_turn_commit_receipt values(?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        elif drift == "claim-binding-missing":
            db.execute("delete from continuity_effect_binding")
        elif drift == "claim-event-missing":
            db.execute(
                "delete from continuity_internal_event_receipt where effect_claim_id is not null"
            )
        elif drift == "claim-id":
            wrong = "018f0000-0000-7000-8000-000000000799"
            db.execute("update local_effect_claim set id=?", (wrong,))
            db.execute("update continuity_effect_binding set claim_id=?", (wrong,))
            db.execute(
                "update continuity_internal_event_receipt set effect_claim_id=? "
                "where effect_claim_id is not null",
                (wrong,),
            )
        elif drift == "claim-idempotency":
            db.execute("update local_effect_claim set idempotency_key='wrong-key'")
        elif drift == "claim-operation":
            db.execute("update local_effect_claim set operation='wrong-operation'")
        elif drift == "terminal-event-missing":
            db.execute(
                "delete from continuity_internal_event_receipt where effect_receipt_id is not null"
            )
        elif drift == "receipt-id":
            wrong = "018f0000-0000-7000-8000-000000000797"
            db.execute("update local_effect_receipt set id=?", (wrong,))
            db.execute(
                "update continuity_internal_event_receipt set effect_receipt_id=? "
                "where effect_receipt_id is not null",
                (wrong,),
            )
        elif drift == "receipt-before-claim":
            db.execute("update local_effect_receipt set created_at='2026-09-03T00:00:00+00:00'")
        elif drift == "running-lease-missing":
            db.execute("delete from local_resource_lock where job_id=?", (job_id,))
            db.execute("delete from local_lease where job_id=?", (job_id,))
        else:
            db.execute(
                "update local_job set state='completed',terminal_evidence_digest=? where id=?",
                (digest("terminal"), job_id),
            )
        with pytest.raises(PolicyViolation):
            internal.verify_b1_internal_producers(db, binding)


def test_internal_claim_without_receipt_requires_exact_running_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepare_internal(tmp_path, monkeypatch)
    job_id = _running_job(path, binding, monkeypatch)
    producer.claim_effect(EffectClaimRequest(binding, job_id, _tail(path, binding)))
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        db.execute("delete from local_resource_lock where job_id=?", (job_id,))
        db.execute("delete from local_lease where job_id=?", (job_id,))
        with pytest.raises(PolicyViolation, match="without running lease"):
            internal.verify_b1_internal_producers(db, binding)


@pytest.mark.parametrize(
    ("operation", "snapshot_type"),
    (
        ("turn", FrozenTurnCommitSnapshot),
        ("claim", FrozenEffectClaimSnapshot),
        ("outcome", FrozenDirectEffectOutcomeSnapshot),
    ),
)
def test_internal_wrong_snapshot_type_rolls_back_without_partial_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    snapshot_type: type[object],
) -> None:
    path, binding, producer, turn, claim, outcome = _prepare_internal(tmp_path, monkeypatch)
    call: Callable[[], object]
    if operation == "turn":
        monkeypatch.setattr(turn, "snapshot", lambda _request: object())
        call = lambda: producer.commit_turn(  # noqa: E731
            TurnCommitRequest(
                binding,
                "user",
                "turn/018f0000-0000-7000-8000-000000000703",
                _tail(path, binding),
            )
        )
    else:
        job_id = _running_job(path, binding, monkeypatch)
        if operation == "claim":
            monkeypatch.setattr(claim, "snapshot", lambda _request: object())
            call = lambda: producer.claim_effect(  # noqa: E731
                EffectClaimRequest(binding, job_id, _tail(path, binding))
            )
        else:
            claimed = producer.claim_effect(
                EffectClaimRequest(binding, job_id, _tail(path, binding))
            )
            monkeypatch.setattr(outcome, "snapshot", lambda _request: object())
            call = lambda: producer.record_direct_outcome(  # noqa: E731
                DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
            )
    assert snapshot_type in {
        FrozenTurnCommitSnapshot,
        FrozenEffectClaimSnapshot,
        FrozenDirectEffectOutcomeSnapshot,
    }
    with pytest.raises(ValidationFailed, match="snapshot"):
        call()


@pytest.mark.parametrize(
    ("table", "column", "value", "message"),
    (
        ("continuity_turn_commit_receipt", "item_ref", "bad", "turn selector"),
        ("local_effect_claim", "operation", "wrong", "claim operation"),
        ("continuity_effect_binding", "binding_digest", "sha256:" + "f" * 64, "claim binding"),
        (
            "local_effect_receipt",
            "id",
            "018f0000-0000-7000-8000-000000000799",
            "terminal event",
        ),
    ),
)
def test_internal_semantic_verifier_rejects_durable_parity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    column: str,
    value: str,
    message: str,
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepare_internal(tmp_path, monkeypatch)
    producer.commit_turn(
        TurnCommitRequest(
            binding,
            "user",
            "turn/018f0000-0000-7000-8000-000000000704",
            _tail(path, binding),
        )
    )
    job_id = _running_job(path, binding, monkeypatch)
    claimed = producer.claim_effect(EffectClaimRequest(binding, job_id, _tail(path, binding)))
    producer.record_direct_outcome(
        DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
    )
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=off")
        triggers = db.execute(
            "select name from sqlite_master where type='trigger' and tbl_name=?", (table,)
        ).fetchall()
        for trigger in triggers:
            db.execute(f'drop trigger "{trigger[0]}"')
        db.execute(f"update {table} set {column}=?", (value,))
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match=message):
            internal.verify_b1_internal_producers(db, binding)


def test_internal_constructor_request_and_missing_graph_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, turn, claim, outcome = _prepare_internal(tmp_path, monkeypatch)
    with pytest.raises(ValidationFailed, match="absolute"):
        internal.SQLiteDormantV4InternalProducer(
            cast(Path, "relative.db"),
            binding,
            turn_issuer=turn,
            claim_issuer=claim,
            outcome_issuer=outcome,
        )
    with pytest.raises(ValidationFailed, match="binding"):
        internal.SQLiteDormantV4InternalProducer(
            path,
            cast(ContinuityBinding, object()),
            turn_issuer=turn,
            claim_issuer=claim,
            outcome_issuer=outcome,
        )
    with pytest.raises(ValidationFailed, match="request"):
        producer.commit_turn(cast(TurnCommitRequest, object()))
    job_id = _running_job(path, binding, monkeypatch)
    missing = EffectClaimRequest(
        binding,
        job_id,
        _tail(path, binding),
    )
    snapshot = claim.snapshot(missing)
    monkeypatch.setattr(claim, "snapshot", lambda _request: snapshot)
    with sqlite3.connect(path) as db:
        triggers = db.execute("select name from sqlite_master where type='trigger'").fetchall()
        for trigger in triggers:
            db.execute(f'drop trigger "{trigger[0]}"')
        db.execute("pragma foreign_keys=off")
        db.execute("delete from local_resource_lock where job_id=?", (job_id,))
        db.execute("delete from local_lease where job_id=?", (job_id,))
        db.execute("delete from local_outbox_delivery")
        db.execute("delete from local_outbox where job_id=?", (job_id,))
        db.execute("delete from local_job where id=?", (job_id,))
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match="missing"):
            producer._verify_fresh_claim_snapshot(db, missing, snapshot)


def test_internal_commit_unknown_zero_and_complete_are_distinguished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepare_internal(tmp_path, monkeypatch)
    request = TurnCommitRequest(
        binding,
        "user",
        "turn/018f0000-0000-7000-8000-000000000705",
        _tail(path, binding),
    )

    def commit_then_raise(db: sqlite3.Connection) -> None:
        db.commit()
        raise OSError("unknown")

    monkeypatch.setattr(producer, "_commit", commit_then_raise)
    assert producer.commit_turn(request).replay
    with pytest.raises(ConcurrencyConflict, match="not-committed"):
        producer._classify("turn-commit:user:missing", digest("missing"))


def test_internal_low_level_canonical_boundaries() -> None:
    with pytest.raises(PolicyViolation, match="canonical UTC"):
        internal._whole_second("2026-09-04T12:00:00.1+00:00", "value")
    with pytest.raises(PolicyViolation, match="UUID"):
        internal._uuid("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", "value")
    with pytest.raises(PolicyViolation, match="secret"):
        internal._bounded_runtime_identity("AKIA" + "A" * 16, "value")


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchone(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return self.rows

    def __iter__(self) -> Any:
        return iter(self.rows)


class _QueueDb:
    def __init__(self, *rows: list[Any]) -> None:
        self.rows = list(rows)
        self.row_factory = sqlite3.Row

    def execute(self, _sql: str, _args: object = ()) -> _Rows:
        return _Rows(self.rows.pop(0))


def test_compaction_low_level_database_and_transaction_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    with pytest.raises(ValidationFailed, match="binding/spool"):
        compact.SQLiteDormantV4PreCompactionWriter(
            tmp_path / "operational.db",
            cast(ContinuityBinding, object()),
            spool=cast(ClientLifecycleSpool, object()),
            generation=writer.generation,
        )
    writer.path = tmp_path / "missing.db"
    deadline = compaction_contract._issue_deadline(writer.generation, lambda: 1_000_000_000)
    with pytest.raises(ConfigurationError, match="regular database"):
        writer._connect(deadline)
    monkeypatch.setattr(operational_schema, "_validate_connection", lambda _db: 3)
    with pytest.raises(ConfigurationError, match="explicit V4"):
        writer._schema(cast(Any, object()))

    db = sqlite3.connect(":memory:")
    owner = compact._OwnedImmediateTransaction(db)
    owner.planned(digest("plan"))
    owner.verified(digest("plan"))
    with pytest.raises(ValidationFailed, match="deadline"):
        owner.commit_verified(digest("plan"), cast(Any, object()))
    owner.abort()
    owner = compact._OwnedImmediateTransaction(db)
    owner.planned(digest("plan"))
    owner.verified(digest("plan"))
    with pytest.raises(PolicyViolation, match="unverified"):
        owner.commit_verified(digest("wrong"), deadline)
    db.close()


def test_compaction_durability_rejects_foreign_key_and_query_only_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    deadline = compaction_contract._issue_deadline(writer.generation, lambda: 1_000_000_000)
    durability = compact._GenerationDurability(writer.generation)
    db = sqlite3.connect(":memory:")
    with pytest.raises(ConfigurationError, match="foreign keys"):
        durability.verify(cast(Any, db), deadline, read_only=False)
    db.execute("pragma foreign_keys=on")
    with pytest.raises(ConfigurationError, match="query-only"):
        durability.verify(cast(Any, db), deadline, read_only=True)
    db.close()


def test_compaction_source_snapshot_and_provenance_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    deadline = compaction_contract._issue_deadline(writer.generation, lambda: 1_000_000_000)
    source = compact._GenerationSource(writer.generation, tmp_path / "wrong.db")
    with pytest.raises(ConfigurationError, match="layout"):
        source.snapshot(binding, deadline)
    with pytest.raises(PolicyViolation, match="snapshot drift"):
        source.assert_current(binding, cast(Any, object()), deadline)


def test_compaction_selected_census_enforces_cardinality_and_byte_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    deadline = compaction_contract._issue_deadline(writer.generation, lambda: 1_000_000_000)

    class CardinalityDb:
        def execute(self, sql: str, _args: object = ()) -> _Rows:
            return _Rows([(5000,)]) if sql.startswith("select count") else _Rows([])

    monkeypatch.setattr(
        SQLiteDormantV4CloseWriter,
        "_attachment",
        lambda *_args: {"attachment_id": "attachment"},
    )
    with pytest.raises(PolicyViolation, match="cardinality"):
        writer._selected_census(cast(Any, CardinalityDb()), binding, digest("delivery"), deadline)

    class ByteDb:
        def execute(self, sql: str, _args: object = ()) -> _Rows:
            if sql.startswith("select count"):
                return _Rows([(1,)])
            return _Rows([("x" * 1_048_577,)])

    with pytest.raises(PolicyViolation, match="byte bound"):
        writer._selected_census(cast(Any, ByteDb()), binding, digest("delivery"), deadline)


def test_compaction_manifest_missing_source_is_classified_as_source_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    deadline = compaction_contract._issue_deadline(writer.generation, lambda: 1_000_000_000)
    missing = cast(Any, _QueueDb([], [], []))
    with pytest.raises(compact._CompactionGateError) as failure:
        writer._manifest(
            missing,
            cast(
                Any,
                {
                    "active_manifest_digest": digest("manifest"),
                    "active_hydration_receipt_digest": digest("hydration"),
                },
            ),
            cast(
                Any,
                SimpleNamespace(
                    source_snapshot_id="x", revision_ref="a", snapshot_digest=digest("x")
                ),
            ),
            deadline,
        )
    assert failure.value.category.value == "SOURCE_DRIFT"


def test_compaction_public_decision_pair_never_allows_missing_or_extra_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    ready = cast(
        Any,
        SimpleNamespace(status="checkpoint-ready", ack_decision_digest=digest("decision")),
    )
    monkeypatch.setattr(writer, "pre_compaction", lambda _event: ready)
    with pytest.raises(PolicyViolation, match="decision unavailable"):
        writer.pre_compaction_with_decision(cast(Any, object()))
    rejected = cast(Any, SimpleNamespace(status="rejected"))
    monkeypatch.setattr(writer, "pre_compaction", lambda _event: rejected)
    writer._last_decision = cast(Any, SimpleNamespace())
    with pytest.raises(PolicyViolation, match="failure carried"):
        writer.pre_compaction_with_decision(cast(Any, object()))


def test_ingress_binding_and_attachment_cardinality_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _manager, _context, _spool, _event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        db.execute("delete from continuity_session_binding")
        with pytest.raises(PolicyViolation, match="existing binding"):
            store._assert_binding(db)
    path2, binding2, manager2, _context, _spool, _event, store2 = _prepare_ingress(
        tmp_path / "second", monkeypatch
    )
    store2.attach_process()
    with sqlite3.connect(path2) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        db.execute("delete from continuity_hook_attachment")
        with pytest.raises(PolicyViolation, match="attachment row"):
            store2._verify_attachment(db, manager2.process)
    path3, _binding3, manager3, _context, _spool, _event, store3 = _prepare_ingress(
        tmp_path / "third", monkeypatch
    )
    store3.attach_process()
    with sqlite3.connect(path3) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        db.execute("delete from continuity_managed_process_receipt")
        with pytest.raises(PolicyViolation, match="attach graph"):
            store3._verify_attachment(db, manager3.process)
    assert binding.session_id != binding2.session_id or path != path2


def test_ingress_spool_replay_payload_and_exact_field_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, binding, _manager, _context, _spool, event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    occurred = dt.datetime.fromisoformat("2026-09-03T12:00:01+00:00")
    with store._stage_current_invocation(event, occurred_at=occurred) as (entry, created):
        assert created
    drift_event = replace(event, source="resume")
    with (
        pytest.raises(PolicyViolation, match="replay payload"),
        store._stage_current_invocation(drift_event, occurred_at=occurred),
    ):
        pass
    for changed in (
        {"sequence": 2},
        {"client_id": "other"},
        {"session_id": "other-session"},
        {"internal_event_type": "PRE_COMPACTION"},
    ):
        with pytest.raises((PolicyViolation, ValidationFailed)):
            ingress.SQLiteCodexV4Ingress._verify_spool(event, replace(entry, **changed))
    assert entry.session_id == binding.external_session_id


def test_ingress_attach_replay_detects_command_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _binding, manager, _context, _spool, _event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    store.attach_process()
    command = manager.process.reviewed_commands[0]
    manager.process = ingress_fixture._replace_test_snapshot(
        manager.process,
        reviewed_commands=(
            replace(command, argv_recipe_digest=digest("drift")),
            *manager.process.reviewed_commands[1:],
        ),
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation, match="command replay"):
            store._verify_attachment(db, manager.process)


def test_internal_constructor_issuer_binding_and_tail_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, turn, claim, outcome = _prepare_internal(tmp_path, monkeypatch)
    with pytest.raises(ValidationFailed, match="issuer"):
        internal.SQLiteDormantV4InternalProducer(
            path,
            binding,
            turn_issuer=cast(Any, object()),
            claim_issuer=claim,
            outcome_issuer=outcome,
        )
    other = ingress_fixture._binding()
    with pytest.raises(PolicyViolation, match="scope drift"):
        producer.commit_turn(
            TurnCommitRequest(
                other,
                "user",
                "turn/018f0000-0000-7000-8000-000000000720",
                _tail(path, binding),
            )
        )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        db.execute("delete from session_event_detail")
        db.execute("delete from session_event")
        with pytest.raises(PolicyViolation, match="SessionStart"):
            producer._tail(db)
    assert turn is producer.turn_issuer


def test_internal_replay_partial_duplicate_and_identity_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _binding, producer, _turn, _claim, _outcome = _prepare_internal(tmp_path, monkeypatch)
    empty = cast(Any, _QueueDb([]))
    monkeypatch.setattr(producer, "_partial", lambda *_args: 1)
    with pytest.raises(ConcurrencyConflict, match="partial"):
        producer._replay_result(empty, "operation", digest("producer"))
    monkeypatch.setattr(internal, "verify_b1_internal_producers", lambda *_args: ())
    duplicate = cast(Any, _QueueDb([{}, {}]))
    with pytest.raises(ConcurrencyConflict, match="duplicate"):
        producer._replay_result(duplicate, "operation", digest("producer"))
    receipt = {
        "turn_commit_digest": digest("other"),
        "effect_claim_id": None,
        "effect_receipt_id": None,
        "event_kind": "USER_TURN_COMMITTED",
        "event_digest": digest("event"),
    }
    mismatch = cast(Any, _QueueDb([receipt]))
    with pytest.raises(ConcurrencyConflict, match="identity drift"):
        producer._replay_result(mismatch, "operation", digest("producer"))


def test_internal_turn_authority_and_generation_predecessor_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, turn, _claim, _outcome = _prepare_internal(tmp_path, monkeypatch)
    request = TurnCommitRequest(
        binding,
        "user",
        "turn/018f0000-0000-7000-8000-000000000721",
        _tail(path, binding),
    )
    snapshot = turn.snapshot(request)
    wrong = b1_fixture._issue(
        FrozenTurnCommitSnapshot,
        **{
            name: (
                "turn/018f0000-0000-7000-8000-000000000722"
                if name == "item_ref"
                else getattr(snapshot, name)
            )
            for name in FrozenTurnCommitSnapshot.__dataclass_fields__
        },
    )
    monkeypatch.setattr(turn, "snapshot", lambda _request: wrong)
    with pytest.raises(PolicyViolation, match="selector drift"):
        producer.commit_turn(request)


def test_internal_direct_outcome_missing_claim_and_running_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, producer, _turn, _claim, outcome = _prepare_internal(tmp_path, monkeypatch)
    job_id = _running_job(path, binding, monkeypatch)
    claimed = producer.claim_effect(EffectClaimRequest(binding, job_id, _tail(path, binding)))
    request = DirectEffectOutcomeRequest(binding, claimed.producer_ref, _tail(path, binding))
    snapshot = outcome.snapshot(request)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        db.execute("delete from continuity_effect_binding")
        with pytest.raises(PolicyViolation, match="claimed effect missing"):
            producer._verify_fresh_outcome_snapshot(db, request, snapshot)
    second = tmp_path / "second"
    second.mkdir()
    path2, binding2, producer2, _turn, _claim, outcome2 = _prepare_internal(second, monkeypatch)
    job2 = _running_job(path2, binding2, monkeypatch)
    claimed2 = producer2.claim_effect(EffectClaimRequest(binding2, job2, _tail(path2, binding2)))
    request2 = DirectEffectOutcomeRequest(binding2, claimed2.producer_ref, _tail(path2, binding2))
    snapshot2 = outcome2.snapshot(request2)
    with sqlite3.connect(path2) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        db.execute("delete from local_lease")
        with pytest.raises(PolicyViolation, match="running lease missing"):
            producer2._verify_fresh_outcome_snapshot(db, request2, snapshot2)


@pytest.mark.parametrize("drift", ("state", "predecessor", "generation"))
def test_ingress_hydrated_graph_head_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, _manager, _context, event, entry, _invocation, store, _event_id, _key = _captured_replay(
        tmp_path, monkeypatch
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        attachment = db.execute("select attachment_id from continuity_hook_attachment").fetchone()
        assert attachment is not None
        _drop_guards(db)
        revision = store._current_revision(db, str(attachment[0]))
        changed: Any = revision
        if drift == "state":
            changed = dict(revision)
            changed["state"] = "attached"
        elif drift == "predecessor":
            db.execute(
                "delete from continuity_hook_attachment_revision where revision_digest=?",
                (revision["previous_revision_digest"],),
            )
        else:
            db.execute(
                "delete from continuity_hook_process_generation where process_generation_digest=?",
                (revision["process_generation_digest"],),
            )
        with pytest.raises(PolicyViolation):
            store._verify_hydrated_graph(db, event=event, entry=entry, revision=changed)


@pytest.mark.parametrize("drift", ("attachment", "state", "scope"))
def test_ingress_process_recovery_rejects_missing_or_drifting_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, _manager, _context, event, entry, invocation, store, _event_id, _key = _captured_replay(
        tmp_path, monkeypatch
    )
    with sqlite3.connect(path) as db:
        _drop_guards(db)
        if drift == "attachment":
            db.execute("delete from continuity_hook_attachment")
    monkeypatch.setattr(store, "_schema", lambda: None)
    if drift == "state":
        monkeypatch.setattr(
            store,
            "_current_revision",
            lambda *_args: {"state": "pre-compact-committed"},
        )
    if drift == "scope":
        invocation = ingress_fixture._replace_test_snapshot(
            invocation, spool_digest=digest("wrong-spool")
        )
    with pytest.raises(PolicyViolation):
        store._recover_process_drift(event, entry, invocation)


@pytest.mark.parametrize("operation", ("turn", "claim", "outcome"))
@pytest.mark.parametrize("committed", (False, True))
def test_internal_integrity_error_before_and_after_commit_is_exactly_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    committed: bool,
) -> None:
    path, binding, producer, _turn, _claim, _outcome = _prepare_internal(tmp_path, monkeypatch)
    job_id = _running_job(path, binding, monkeypatch)
    call: Callable[[], Any]
    if operation == "turn":
        call = lambda: producer.commit_turn(  # noqa: E731
            TurnCommitRequest(
                binding,
                "user",
                "turn/018f0000-0000-7000-8000-000000000731",
                _tail(path, binding),
            )
        )
    elif operation == "claim":
        claim_request = EffectClaimRequest(binding, job_id, _tail(path, binding))
        call = lambda: producer.claim_effect(claim_request)  # noqa: E731
    else:
        claimed = producer.claim_effect(EffectClaimRequest(binding, job_id, _tail(path, binding)))
        outcome_request = DirectEffectOutcomeRequest(
            binding, claimed.producer_ref, _tail(path, binding)
        )
        call = lambda: producer.record_direct_outcome(outcome_request)  # noqa: E731

    def fail(db: sqlite3.Connection) -> None:
        if committed:
            db.commit()
        raise sqlite3.IntegrityError("injected")

    monkeypatch.setattr(producer, "_commit", fail)
    if committed:
        assert call().replay is True
    else:
        with pytest.raises(ConcurrencyConflict):
            call()


@pytest.mark.parametrize("drift", ("binding", "revision", "head"))
def test_internal_preflight_rejects_inactive_binding_revision_or_event_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, _binding, producer, _turn, _claim, _outcome = _prepare_internal(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        if drift == "binding":
            db.execute("delete from continuity_session_binding")
            with pytest.raises(PolicyViolation, match="active binding"):
                producer._attachment_revision(db)
        elif drift == "revision":
            monkeypatch.setattr(
                SQLiteDormantV4CloseWriter,
                "_current_revision",
                lambda *_args: {"state": "attached"},
            )
            with pytest.raises(PolicyViolation, match="hydrated attachment"):
                producer._attachment_revision(db)
        else:
            db.execute("delete from session_event_detail")
            db.execute("delete from session_event")
            with pytest.raises(PolicyViolation, match="SessionStart"):
                producer._tail(db)


@pytest.mark.parametrize("drift", ("turn-body", "terminal-progression", "recovery-case"))
def test_internal_full_verifier_rejects_remaining_body_and_progression_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, binding, _producer, job_id, claim_id = _completed_internal_graph(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        if drift == "turn-body":
            db.execute("update continuity_turn_commit_receipt set body_json='{}'")
        elif drift == "terminal-progression":
            db.execute(
                "update local_job set updated_at='2026-09-04T12:00:59.000000+00:00' where id=?",
                (job_id,),
            )
        else:
            columns = [row[1] for row in db.execute("pragma table_info(local_recovery_case)")]
            values: dict[str, object] = {
                "id": "018f0000-0000-7000-8000-000000000799",
                "job_id": job_id,
                "effect_claim_id": claim_id,
                "case_kind": "effect-unknown",
                "state": "open",
                "evidence_digest": digest("case"),
                "body_json": canonical_json({"case": "bounded"}),
                "created_at": "2026-09-04T12:00:03.000000+00:00",
            }
            db.execute(
                "insert into local_recovery_case("
                + ",".join(columns)
                + ") values("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(values.get(name) for name in columns),
            )
        with pytest.raises((PolicyViolation, sqlite3.Error)):
            internal.verify_b1_internal_producers(db, binding)


def test_internal_b2_crash_cardinality_identity_and_revision_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, binding, _producer, _turn, _claim, _outcome = _prepare_internal(tmp_path, monkeypatch)
    resolution = {
        "id": "018f0000-0000-7000-8000-000000000741",
        "recovery_case_id": "018f0000-0000-7000-8000-000000000742",
        "created_at": "2026-09-04T12:00:00+00:00",
        "evidence_digest": digest("resolution"),
    }
    revision = {"revision_digest": digest("recovery-revision")}
    restored = {"crash_recovered_receipt_digest": digest("restored")}
    with pytest.raises(PolicyViolation, match="cardinality"):
        internal._verify_b2_crash_event(
            cast(Any, _QueueDb([])),
            binding,
            resolution=cast(Any, resolution),
            recovery_revision=cast(Any, revision),
            restored=cast(Any, restored),
        )
    event_digest = digest("crash-event")
    receipt = {
        "event_digest": event_digest,
        "session_id": binding.session_id,
        "receipt_digest": digest("receipt"),
        "attachment_revision_digest": digest("wrong-revision"),
    }
    with pytest.raises(PolicyViolation, match="identity"):
        internal._verify_b2_crash_event(
            cast(Any, _QueueDb([receipt], [])),
            binding,
            resolution=cast(Any, resolution),
            recovery_revision=cast(Any, revision),
            restored=cast(Any, restored),
        )
    detail = {"event_id": str(uuid5(B2_EVENT_NS, f"event|{event_digest}"))}
    with pytest.raises(PolicyViolation, match="revision/receipt"):
        internal._verify_b2_crash_event(
            cast(Any, _QueueDb([receipt], [detail])),
            binding,
            resolution=cast(Any, resolution),
            recovery_revision=cast(Any, revision),
            restored=cast(Any, restored),
        )


def test_internal_verifier_detects_carried_selection_and_second_census_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _producer, _turn, _claim, _outcome = _prepare_internal(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        monkeypatch.setattr(internal, "_carried_b2_claim", lambda *_args: "claim-a")
        with pytest.raises(PolicyViolation, match="conflicts"):
            internal.verify_b1_b2_internal_producers(db, binding, selected_b2_claim_id="claim-b")
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        monkeypatch.setattr(internal, "_carried_b2_claim", lambda *_args: None)
        monkeypatch.setattr(internal, "_verify_turns", lambda *_args: None)
        monkeypatch.setattr(internal, "_verify_effects", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(SQLiteDormantV4CloseWriter, "_events", lambda *_args: ())
        observed = iter((("before",), ("after",)))
        monkeypatch.setattr(internal, "_producer_rows", lambda *_args: next(observed))
        with pytest.raises(ConcurrencyConflict, match="changed"):
            internal.verify_b1_b2_internal_producers(db, binding)


@pytest.mark.parametrize("case", ("type", "outside", "missing", "encoding", "digest", "resolver"))
def test_compaction_source_fragment_fail_closed_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    _path, binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    source = compact._GenerationSource(writer.generation)
    monkeypatch.setattr(compact._GenerationSource, "assert_current", lambda *_args: None)
    payload: bytes | None = b"bounded\n"
    allowed: tuple[str, ...] = ("fixture.py",)
    if case == "missing":
        payload = None
    elif case == "encoding":
        payload = b"\xff"
    elif case == "outside":
        allowed = ()
    source._source = cast(
        Any,
        SimpleNamespace(
            recipe=SimpleNamespace(allowed_paths=allowed),
            _read=lambda *_args: payload,
        ),
    )
    source._resolver = object()
    body: dict[str, object] = {
        "source_ref": "fixture.py",
        "revision": "rev",
        "digest": digest("bounded\n"),
    }
    if case == "digest":
        body["digest"] = digest("wrong")
    if case == "resolver":
        body["kind"] = "generated-note"
    provenance = CanonicalManifestProvenance("candidate", canonical_json(body), digest(body))
    candidate: Any = object() if case == "type" else provenance
    with pytest.raises((PolicyViolation, ValidationFailed)):
        source.resolve_fragment(
            binding, cast(Any, SimpleNamespace(revision_ref="rev")), candidate, cast(Any, object())
        )


def test_compaction_source_close_rolls_back_and_propagates_descriptor_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    db = sqlite3.connect(":memory:")
    db.execute("begin")
    source = compact._GenerationSource(writer.generation)
    cast(Any, source)._db = db
    source.close()
    assert source._db is None
    failed = cast(Any, compact._GenerationSource(writer.generation))
    failed._side_fd = -1
    with pytest.raises(OSError):
        failed.close()
    assert failed._side_fd is None


@pytest.mark.parametrize("drift", ("event-detail", "revision-row"))
def test_compaction_exact_plan_rows_reaches_late_graph_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, _binding, writer, _source, plan, _invocation, _event, _entries = (
        _captured_precompact_plan(tmp_path, monkeypatch)
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        if drift == "event-detail":
            db.execute(
                "update session_event_detail set body_json='{}' where event_digest=?",
                (plan.precompact_event_digest,),
            )
        else:
            db.execute(
                "update continuity_hook_attachment_revision set body_json='{}' "
                "where revision_digest=?",
                (plan.revision_digest,),
            )
        with pytest.raises(PolicyViolation):
            writer._exact_plan_rows(cast(Any, db), plan)


@pytest.mark.parametrize("drift", ("spool", "head", "checkpoint"))
def test_compaction_plan_late_reopen_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, binding, writer, _source, plan, invocation, _event, entries = _captured_precompact_plan(
        tmp_path, monkeypatch
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = SQLiteDormantV4CloseWriter._events(db, binding)
        if drift == "checkpoint":
            _drop_guards(db)
            db.execute("delete from continuity_checkpoint")
    monkeypatch.setattr(writer, "_schema", lambda _db: None)
    monkeypatch.setattr(
        SQLiteDormantV4CloseWriter,
        "_events",
        lambda *_args: (
            [dict(row) for row in rows[:-1]] + [{**dict(rows[-1]), "spool_digest": digest("other")}]
            if drift == "spool"
            else rows
        ),
    )
    monkeypatch.setattr(SQLiteDormantV4CloseWriter, "_no_pending", lambda *_args: None)
    if drift == "head":
        monkeypatch.setattr(
            writer,
            "_current",
            lambda *_args: {"revision_digest": digest("other"), "state": "hydrated"},
        )
    deadline = compaction_contract._issue_deadline(writer.generation, lambda: 1_000_000_000)
    with closing(writer._connect(deadline, read_only=True)) as db:
        db.execute("begin")
        with pytest.raises(PolicyViolation):
            writer._verify_plan(
                cast(Any, db), plan, entries, invocation, deadline, issue_decision=True
            )


def test_compaction_result_decision_pair_and_source_cleanup_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        writer,
        "_run",
        lambda *_args: compaction_contract.rejected(
            compaction_contract.PreCompactionFailure.VALIDATION
        ),
    )
    cast(Any, writer).source = SimpleNamespace(close=None)
    assert (
        writer.pre_compaction(compact_fixture._precompact_event(writer.binding)).status
        == "rejected"
    )

    def failure_with_decision(*_args: object) -> Any:
        writer._last_decision = cast(Any, SimpleNamespace(decision_digest=digest("decision")))
        return compaction_contract.rejected(compaction_contract.PreCompactionFailure.VALIDATION)

    monkeypatch.setattr(writer, "_run", failure_with_decision)
    with pytest.raises(PolicyViolation, match="failure carried"):
        writer.pre_compaction_with_decision(compact_fixture._precompact_event(writer.binding))


@pytest.mark.parametrize("drift", ("commands", "revision"))
def test_ingress_attach_rejects_late_readback_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    _path, _binding, _manager, _context, _spool, _event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    if drift == "commands":
        monkeypatch.setattr(ingress, "verify_reviewed_hook_commands", lambda *_args: ())
    else:
        monkeypatch.setattr(
            store, "_current_revision", lambda *_args: {"revision_digest": digest("wrong")}
        )
    with pytest.raises(PolicyViolation):
        store.attach_process()


def _write_start_with_injected_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, binding, _manager, context, _spool, event, store = _prepare_ingress(tmp_path, monkeypatch)
    store.attach_process()
    original = store._write_session_start

    def injected(
        selected_event: CodexMacOS0151Event, entry: Any, invocation: ManagedInvocationSnapshot
    ) -> Any:
        if drift == "attachment":
            with sqlite3.connect(path) as db:
                _drop_guards(db)
                db.execute("delete from continuity_hook_attachment")
            monkeypatch.setattr(store, "_schema", lambda: None)
        elif drift == "state":
            monkeypatch.setattr(store, "_current_revision", lambda *_args: {"state": "closing"})
        elif drift == "generation":
            invocation = ingress_fixture._replace_test_snapshot(
                invocation, process_generation_digest=digest("other-generation")
            )
        elif drift == "command":
            invocation = ingress_fixture._replace_test_snapshot(
                invocation, launch_command_digest=digest("other-command")
            )
        else:
            frozen = context.build(
                binding,
                hydration_key=ingress._operation_key(
                    ingress._event_uuid(binding, entry.entry_digest)
                ),
                observed_at="2026-09-04T12:00:59+00:00",
            )
            monkeypatch.setattr(context, "build", lambda *_args, **_kwargs: frozen)
        return original(selected_event, entry, invocation)

    monkeypatch.setattr(store, "_write_session_start", injected)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        store.session_start(event)


@pytest.mark.parametrize("drift", ("attachment", "state", "generation", "command", "time"))
def test_ingress_fresh_write_rejects_each_late_authority_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    _write_start_with_injected_drift(tmp_path, monkeypatch, drift)


def test_ingress_spool_parity_uses_event_not_only_entry_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, binding, _manager, _context, _spool, event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    store.attach_process()
    with store._stage_current_invocation(
        event,
        occurred_at=__import__("datetime").datetime.fromisoformat("2026-09-04T12:00:01+00:00"),
    ) as (entry, _created):
        foreign = replace(event, external_session_id=binding.external_session_id + "-other")
        with pytest.raises(PolicyViolation, match="parity"):
            store._verify_spool(foreign, entry)


def test_ingress_process_drift_recovery_is_restart_replayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _binding, manager, _context, _spool, event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    store.attach_process()
    manager.fail_invocation = True
    first = store.session_start(event)
    second = store.session_start(event)
    assert first.recovery_required and second.recovery_required and second.replay


@pytest.mark.parametrize(
    "drift", ("resolved", "session", "predecessor", "generation", "invocation", "collision")
)
def test_compaction_run_rejects_late_runtime_authority_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, binding, manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    event = compact_fixture._precompact_event(binding)
    if drift == "resolved":
        writer.resolved = cast(Any, object())
        monkeypatch.setattr(compact, "_resolve_existing_binding", lambda *_args: None)
    elif drift == "session":
        monkeypatch.setattr(
            SQLiteDormantV4CloseWriter,
            "_binding",
            lambda *_args: {"status": "closing"},
        )
    elif drift == "predecessor":
        monkeypatch.setattr(writer, "_current", lambda *_args: {"state": "attached"})
    elif drift == "generation":
        with sqlite3.connect(path) as db:
            _drop_guards(db)
            db.execute("delete from continuity_hook_process_generation")
        monkeypatch.setattr(writer, "_schema", lambda _db: None)
    elif drift == "invocation":
        original = manager.capture_precompaction_invocation

        def changed(*args: object, **kwargs: object) -> Any:
            return ingress_fixture._replace_test_snapshot(
                original(*args, **kwargs), spool_digest=digest("wrong-spool")
            )

        monkeypatch.setattr(manager, "capture_precompaction_invocation", changed)
    else:
        monkeypatch.setattr(writer, "_collisions", lambda *_args: (1, 0, 0, 0, 0, 0, 0))
    result = writer.pre_compaction(event)
    assert result.status != "checkpoint-ready"


def test_compaction_replay_collision_and_missing_final_decision_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    event = compact_fixture._precompact_event(binding)
    assert writer.pre_compaction(event).status == "checkpoint-ready"
    monkeypatch.setattr(writer, "_collisions", lambda *_args: (0, 0, 0, 0, 0, 0, 0))
    assert writer.pre_compaction(event).status != "checkpoint-ready"

    second = tmp_path / "second"
    second.mkdir()
    _path2, binding2, _manager2, _spool2, writer2, _source2 = compact_fixture._prepared_behavioral(
        second, monkeypatch
    )
    original = writer2._verify_plan

    def no_decision(*args: object, **kwargs: object) -> Any:
        value = cast(Any, original)(*args, **kwargs)
        return None if kwargs.get("issue_decision") else value

    monkeypatch.setattr(writer2, "_verify_plan", no_decision)
    assert (
        writer2.pre_compaction(compact_fixture._precompact_event(binding2)).status
        != "checkpoint-ready"
    )


def test_compaction_predecessor_lookup_and_resolved_factory_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, _binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    with pytest.raises(PolicyViolation, match="predecessor"):
        writer._current_by_digest(cast(Any, _QueueDb([])), digest("missing"))
    with pytest.raises(ValidationFailed, match="resolved authority"):
        compact.resolved_precompaction_writer(
            tmp_path / "operational.db",
            cast(Any, object()),
            spool=writer.spool,
            generation=writer.generation,
        )


def _captured_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Any, Any, Any, Any, Any]:
    path, binding, manager, _context, spool, event, store = _prepare_ingress(tmp_path, monkeypatch)
    store.attach_process()
    captured: list[Any] = []
    original = manager.capture_invocation

    def capture(*args: object, **kwargs: object) -> Any:
        value = original(*args, **kwargs)
        captured.append(value)
        return value

    monkeypatch.setattr(manager, "capture_invocation", capture)
    manager.fail_invocation = True
    assert store.session_start(event).recovery_required
    entry = spool.read_session_entries(client_id="codex", session_id=binding.external_session_id)[0]
    return path, binding, store, event, entry, captured[0]


@pytest.mark.parametrize("drift", ("state", "case", "kind", "partial"))
def test_ingress_recovery_head_rejects_each_invalid_replay_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, _binding, store, event, entry, invocation = _captured_recovery(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        attachment = db.execute("select attachment_id from continuity_hook_attachment").fetchone()
        assert attachment is not None
        current: Any = store._current_revision(db, str(attachment[0]))
        if drift == "state":
            current = {**dict(current), "state": "attached"}
        elif drift == "case":
            _drop_guards(db)
            db.execute("delete from continuity_hook_recovery_case")
        elif drift == "kind":
            _drop_guards(db)
            db.execute("update continuity_hook_recovery_case set case_kind='transaction-unknown'")
        else:
            monkeypatch.setattr(store, "_same_operation_census", lambda *_args, **_kwargs: 1)
        with pytest.raises(PolicyViolation):
            store._verify_recovery_head(
                db, event=event, entry=entry, invocation=invocation, current=current
            )


@pytest.mark.parametrize("committed", (False, True))
def test_ingress_recovery_commit_exception_classifies_without_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    _path, _binding, manager, _context, _spool, event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    store.attach_process()
    manager.fail_invocation = True

    def fail(db: sqlite3.Connection) -> None:
        if committed:
            db.commit()
        raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(store, "_commit", fail)
    result = store.session_start(event)
    assert result.recovery_required


def _git_hydrated_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ContinuityBinding, Any, Any, CodexMacOS0151Event, Any]:
    path, binding, manager, _context, _spool, start, store = _prepare_ingress(tmp_path, monkeypatch)
    store.attach_process()
    store.session_start(start)
    with sqlite3.connect(path) as db:
        for (name,) in db.execute(
            "select name from sqlite_master where type='trigger' and tbl_name='source_binding'"
        ):
            db.execute(f'drop trigger "{name}"')
        db.execute("update source_binding set source_kind='git'")
    monkeypatch.setattr(operational_schema, "_validate_connection", lambda _db: 4)
    event = compact_fixture._precompact_event(binding)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        resolved = compact._resolve_existing_binding(cast(Any, db), event)
    return path, binding, manager, store, event, resolved


@pytest.mark.parametrize("drift", ("attachment", "active"))
def test_compaction_existing_binding_detects_head_and_active_evidence_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, _binding, _manager, _store, event, _resolved = _git_hydrated_binding(
        tmp_path, monkeypatch
    )
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        current = SQLiteDormantV4CloseWriter._current_revision(
            db,
            str(db.execute("select attachment_id from continuity_hook_attachment").fetchone()[0]),
        )
        if drift == "attachment":
            monkeypatch.setattr(
                SQLiteDormantV4CloseWriter,
                "_attachment",
                lambda *_args: {"attachment_id": "different"},
            )
        else:
            altered = {**dict(current), "active_manifest_digest": None}
            monkeypatch.setattr(
                SQLiteDormantV4CloseWriter,
                "_current_revision",
                lambda *_args: altered,
            )
        with pytest.raises(PolicyViolation):
            compact._resolve_existing_binding(cast(Any, db), event)


def test_compaction_public_resolution_validates_path_schema_source_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _binding, _manager, _store, event, resolved = _git_hydrated_binding(tmp_path, monkeypatch)
    with pytest.raises(ValidationFailed, match="database path"):
        compact.resolve_existing_precompaction_binding(
            cast(Path, "relative.db"), event, cwd=tmp_path
        )
    monkeypatch.setattr(operational_schema, "_validate_connection", lambda _db: 3)
    with pytest.raises(ConfigurationError, match="V4"):
        compact.resolve_existing_precompaction_binding(path, event, cwd=tmp_path)
    monkeypatch.setattr(operational_schema, "_validate_connection", lambda _db: 4)
    monkeypatch.setattr(compact, "_resolve_existing_binding", lambda *_args: resolved)
    with sqlite3.connect(path) as db:
        _drop_guards(db)
        db.execute("delete from source_snapshot")
    with pytest.raises(PolicyViolation, match="source binding"):
        compact.resolve_existing_precompaction_binding(path, event, cwd=tmp_path)


@pytest.mark.parametrize("drift", ("schema", "current", "missing", "chain", "state"))
def test_compaction_rollover_rejects_each_generation_authority_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    path, _binding, manager, _store, event, resolved = _git_hydrated_binding(tmp_path, monkeypatch)
    process = ingress_fixture._replace_test_snapshot(
        manager.process,
        captured_at="2026-09-04T12:01:00+00:00",
        native_pid=909,
        native_start_token="rollover-process",
    )
    manager.process = process
    if drift == "schema":
        monkeypatch.setattr(operational_schema, "_validate_connection", lambda _db: 3)
    else:
        monkeypatch.setattr(operational_schema, "_validate_connection", lambda _db: 4)
    selected = resolved
    if drift == "current":
        monkeypatch.setattr(compact, "_resolve_existing_binding", lambda *_args: None)
    elif drift == "state":
        resolution_body = {
            "schema": "zekam-precompact-existing-binding-resolution/v1",
            "binding_digest": resolved.binding.binding_digest,
            "attachment_id": resolved.attachment_id,
            "head_revision_digest": resolved.head_revision_digest,
            "head_state": "pre-compact-committed",
            "active_manifest_digest": resolved.active_manifest_digest,
            "active_hydration_receipt_digest": resolved.active_hydration_receipt_digest,
        }
        selected = replace(
            resolved,
            head_state="pre-compact-committed",
            resolution_digest=digest(resolution_body),
        )
        monkeypatch.setattr(compact, "_resolve_existing_binding", lambda *_args: selected)
    else:
        monkeypatch.setattr(compact, "_resolve_existing_binding", lambda *_args: resolved)
    if drift in {"missing", "chain"}:
        with sqlite3.connect(path) as db:
            _drop_guards(db)
            if drift == "missing":
                db.execute("delete from continuity_hook_process_generation")
            else:
                db.execute(
                    "update continuity_hook_process_generation set generation=2,"
                    "previous_process_generation_digest=?",
                    (digest("missing-predecessor"),),
                )
    with pytest.raises((PolicyViolation, ConcurrencyConflict)):
        compact.rollover_existing_precompaction_process(
            path, event, tmp_path / "source", selected, manager
        )


def test_compaction_source_held_identity_and_open_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    source = compact._GenerationSource(writer.generation)
    source._db = cast(Any, object())
    source._side_fd = 1
    source._operational_fd = 2
    source._candidate = cast(Any, SimpleNamespace(device_id="wrong", project_id="wrong"))
    source._record = source._source = source._captured = source._snapshot = cast(Any, object())
    source._side_identity = source._operational_identity = cast(Any, object())
    source._operational_parent = "parent"
    monkeypatch.setattr(compact._GenerationSource, "_file_ok", lambda *_args: None)
    monkeypatch.setattr(compact, "_source_authority_held_identity", lambda *_args: object())
    with pytest.raises(PolicyViolation, match="identity drift"):
        source._unchanged(binding)

    missing = compact._GenerationSource(writer.generation)
    missing._db = cast(Any, _QueueDb([]))
    missing._side_fd = 1
    missing._operational_fd = 2
    missing._candidate = cast(
        Any,
        SimpleNamespace(
            device_id=binding.device_id,
            project_id=binding.project_id,
            revision_digest=digest("candidate"),
        ),
    )
    missing._record = missing._source = missing._captured = missing._snapshot = cast(Any, object())
    missing._side_identity = missing._operational_identity = cast(Any, "identity")
    missing._operational_parent = "parent"
    monkeypatch.setattr(compact, "_source_authority_held_identity", lambda *_args: "identity")
    monkeypatch.setattr(compact, "_source_authority_identity", lambda *_args, **_kwargs: "identity")
    monkeypatch.setattr(compact, "_source_authority_parent_chain", lambda *_args: "parent")
    with pytest.raises(PolicyViolation, match="revision drift"):
        missing._unchanged(binding)

    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    operational = state / "operational.db"
    operational.write_bytes(b"db")
    side = local_source_authority_path(home)
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_bytes(b"side")
    opened = compact._GenerationSource(writer.generation, operational)
    monkeypatch.setattr(
        compact, "_source_authority_identity", lambda *_args, **_kwargs: cast(Any, "identity")
    )
    monkeypatch.setattr(compact, "_source_authority_parent_chain", lambda *_args: "parent")
    monkeypatch.setattr(
        compact, "_source_authority_held_identity", lambda *_args: cast(Any, "other")
    )
    with pytest.raises(PolicyViolation, match="open identity"):
        opened.snapshot(binding, compaction_contract._issue_deadline(writer.generation, lambda: 0))


def test_ingress_binding_postcommit_and_replay_context_type_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _binding, _manager, _context, _spool, _event, store = _prepare_ingress(
        tmp_path, monkeypatch
    )
    store.attach_process()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        _drop_guards(db)
        db.execute("update session set status='closing'")
        with pytest.raises(PolicyViolation, match="binding/session"):
            store._assert_binding(db)

    path2, _manager2, context2, event2, entry2, invocation2, store2, event_id2, key2 = (
        _captured_replay(tmp_path / "replay", monkeypatch)
    )
    with sqlite3.connect(path2) as db:
        db.row_factory = sqlite3.Row
        attachment = db.execute("select attachment_id from continuity_hook_attachment").fetchone()
        assert attachment is not None
        revision = store2._current_revision(db, str(attachment[0]))
        verified = store2._verify_hydrated_graph(db, event=event2, entry=entry2, revision=revision)
    monkeypatch.setattr(store2, "_verify_hydrated_graph", lambda *_args, **_kwargs: verified)
    monkeypatch.setattr(context2, "build", lambda *_args, **_kwargs: object())
    with pytest.raises(ValidationFailed, match="replay exact context"):
        store2._replay(event2, entry2, invocation2, event_id2, key2)


def test_internal_remaining_timestamp_terminal_and_turn_predecessor_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(PolicyViolation, match="whole-second"):
        internal._whole_second("2026-09-04T12:00:00.100000+00:00", "value")

    path, _binding, _producer, job_id, claim_id = _completed_internal_graph(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        job = dict(db.execute("select * from local_job where id=?", (job_id,)).fetchone())
        claim = db.execute("select * from local_effect_claim where id=?", (claim_id,)).fetchone()
        receipt = db.execute(
            "select * from local_effect_receipt where claim_id=?", (claim_id,)
        ).fetchone()
        assert claim is not None and receipt is not None
        job["state"] = "completed"
        job["terminal_evidence_digest"] = receipt["evidence_digest"]
        job["updated_at"] = "2026-09-04T12:00:59.000000+00:00"
        enqueue = dict(
            db.execute("select * from local_outbox where job_id=?", (job_id,)).fetchone()
        )
        enqueue.update(
            {
                "id": "018f0000-0000-7000-8000-000000000753",
                "idempotency_key": f"job:{job_id}:terminal",
                "event_kind": "job.wrong",
                "payload_json": canonical_json({"job_id": job_id, "state": "completed"}),
                "payload_digest": digest({"job_id": job_id, "state": "completed"}),
                "created_at": job["updated_at"],
            }
        )
        db.execute(
            "insert into local_outbox("
            + ",".join(enqueue)
            + ") values("
            + ",".join("?" for _ in enqueue)
            + ")",
            tuple(enqueue.values()),
        )
        with pytest.raises(PolicyViolation, match="progression"):
            internal._terminal_outbox(
                db,
                cast(Any, job),
                claim,
                receipt,
                trusted_now=SQLiteDormantV4CloseWriter._trusted_now(),
            )

    second = tmp_path / "second-turn"
    second.mkdir()
    _path, binding2, producer2, turn2, _claim2, _outcome2 = _prepare_internal(second, monkeypatch)
    producer2.commit_turn(
        TurnCommitRequest(
            binding2, "user", "turn/018f0000-0000-7000-8000-000000000751", _tail(_path, binding2)
        )
    )
    request = TurnCommitRequest(
        binding2, "assistant", "turn/018f0000-0000-7000-8000-000000000752", _tail(_path, binding2)
    )
    snapshot = turn2.snapshot(request)
    wrong = b1_fixture._issue(
        FrozenTurnCommitSnapshot,
        **{
            name: (
                digest("wrong-generation")
                if name == "previous_store_generation_commitment_digest"
                else getattr(snapshot, name)
            )
            for name in FrozenTurnCommitSnapshot.__dataclass_fields__
        },
    )
    monkeypatch.setattr(turn2, "snapshot", lambda _request: wrong)
    with pytest.raises(PolicyViolation, match="generation predecessor"):
        producer2.commit_turn(request)


def test_compaction_close_and_public_decision_success_and_failure_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, binding, _manager, _spool, writer, _source = compact_fixture._prepared_behavioral(
        tmp_path, monkeypatch
    )
    source = compact._GenerationSource(writer.generation)
    db = sqlite3.connect(":memory:")
    db.execute("create table value(id integer)")
    db.execute("insert into value values(1)")
    assert db.in_transaction
    source._db = db
    source.close()

    calls: list[bool] = []
    cast(Any, writer).source = SimpleNamespace(close=lambda: calls.append(True))
    monkeypatch.setattr(
        writer,
        "_run",
        lambda *_args: compaction_contract.rejected(
            compaction_contract.PreCompactionFailure.VALIDATION
        ),
    )
    result, decision = writer.pre_compaction_with_decision(
        compact_fixture._precompact_event(binding)
    )
    assert result.status == "rejected" and decision is None and calls == [True]

    third = tmp_path / "success"
    third.mkdir()
    _path3, binding3, _manager3, _spool3, writer3, _source3 = compact_fixture._prepared_behavioral(
        third, monkeypatch
    )
    result3, decision3 = writer3.pre_compaction_with_decision(
        compact_fixture._precompact_event(binding3)
    )
    assert result3.status == "checkpoint-ready" and decision3 is not None
