from __future__ import annotations

import datetime as dt
import inspect
import os
import socket
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pytest
from tests.integration import test_local_continuity_source_authority as gate_a
from tests.integration import test_local_continuity_v4_session_start as session_start

from zekam.application import local_continuity_v4_compaction as application
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_v4_ingress import ManagedInvocationSnapshot
from zekam.application.local_continuity_v4_writer import (
    CanonicalManifestProvenance,
    CurrentSourceSnapshot,
    ResolvedManifestFragment,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure import macos_precompaction_supervisor as supervisor
from zekam.infrastructure.clients import codex_macos_0151_lifecycle as lifecycle
from zekam.infrastructure.clients import codex_macos_0151_precompaction_client as ipc_client
from zekam.infrastructure.clients.codex_macos_0151_lifecycle import CodexMacOS0151Event
from zekam.infrastructure.local_continuity_source_plan import publish_portable_source_plan
from zekam.infrastructure.sqlite import local_continuity_v4_compaction as compact
from zekam.infrastructure.sqlite.local_continuity_source_authority import (
    SQLiteLocalSourceAuthority,
    local_source_authority_path,
)
from zekam.infrastructure.sqlite.local_continuity_v4_writer import SQLiteDormantV4CloseWriter

SESSION = "018f0000-0000-7000-8000-000000000201"
PROJECT = "018f0000-0000-7000-8000-000000000202"
REALM = "018f0000-0000-7000-8000-000000000203"
SNAPSHOT = "018f0000-0000-7000-8000-000000000204"


def _test_generation(monkeypatch: pytest.MonkeyPatch) -> supervisor._DarwinGenerationOwner:
    listener = supervisor._DarwinListenerObservation(
        "/private/tmp/zekam-precompact-test.sock",
        7,
        501,
        0o600,
        1,
        2,
        1,
        1,
    )
    job = supervisor._DarwinJobObservation(
        1,
        b"\0" * 16,
        supervisor.JOB_LABEL,
        supervisor.LISTENER_KEY,
        101,
        501,
        "service-start",
        digest("service-artifact"),
        digest("protocol"),
        listener,
    )
    adapter = object.__new__(supervisor._DarwinAuthorityAdapter)
    monkeypatch.setattr(supervisor._DarwinAuthorityAdapter, "observe_current", lambda _self: job)
    owner = object.__new__(supervisor._DarwinGenerationOwner)
    object.__setattr__(owner, "_adapter", adapter)
    object.__setattr__(owner, "_job", job)
    object.__setattr__(owner, "_digest", digest("test-darwin-generation"))
    seal = digest("test-darwin-generation-seal")
    object.__setattr__(owner, "_seal", seal)
    monkeypatch.setitem(supervisor._GENERATIONS, seal, owner)
    monkeypatch.setitem(supervisor._GENERATION_PARITY, seal, supervisor._generation_bytes(owner))
    assert owner.generation_digest == digest("test-darwin-generation")
    return owner


class _BoundedSource:
    def __init__(self, text: str, snapshot_digest: str) -> None:
        self.text = text
        self.snapshot_digest = snapshot_digest
        self.fail_current = False
        self.snapshot_id: str | None = None
        self.deadline_checks = 0

    def snapshot(self, binding: ContinuityBinding, deadline: Any) -> CurrentSourceSnapshot:
        deadline.require_current()
        self.deadline_checks += 1
        return CurrentSourceSnapshot(
            self.snapshot_id or binding.source_snapshot_id,
            "a" * 40,
            self.snapshot_digest,
        )

    def assert_current(
        self,
        _binding: ContinuityBinding,
        _snapshot: CurrentSourceSnapshot,
        deadline: Any,
    ) -> None:
        deadline.require_current()
        self.deadline_checks += 1
        if self.fail_current:
            raise PolicyViolation("injected source drift")

    def resolve_fragment(
        self,
        _binding: ContinuityBinding,
        _snapshot: CurrentSourceSnapshot,
        provenance: Any,
        deadline: Any,
    ) -> ResolvedManifestFragment:
        deadline.require_current()
        self.deadline_checks += 1
        return ResolvedManifestFragment(provenance.candidate_id, self.text)


def _precompact_event(binding: ContinuityBinding) -> CodexMacOS0151Event:
    return CodexMacOS0151Event(
        binding.external_session_id,
        "PreCompact",
        None,
        "turn-1",
        "manual",
        None,
        digest("precompact-wire"),
    )


def _prepared_behavioral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    Path,
    ContinuityBinding,
    Any,
    ClientLifecycleSpool,
    compact.SQLiteDormantV4PreCompactionWriter,
    _BoundedSource,
]:
    source_root = tmp_path / "source-root"
    source_root.mkdir(parents=True)
    source_ref = "fixture.py"
    text = "bounded local source for direct PreCompact writer verification\n"
    (source_root / source_ref).write_text(text, encoding="utf-8")
    monkeypatch.setattr(session_start, "ROOT", source_root)
    monkeypatch.setattr(session_start, "SOURCE_REF", source_ref)
    cast(Any, session_start._test_only_sealed_owner_seams).__wrapped__(monkeypatch)
    snapshot_digest = digest(
        {
            "schema": "zekam-current-source-snapshot/v1",
            "source_snapshot_id": session_start.SNAPSHOT,
            "revision_ref": "a" * 40,
            "tree_digest": digest("tree"),
            "content_digest": digest(text),
            "config_digest": digest("config"),
        }
    )

    def build_context(
        context_port: Any,
        binding: ContinuityBinding,
        *,
        hydration_key: str,
        observed_at: str,
    ) -> Any:
        return session_start._issue_test_context(
            binding=binding,
            context=session_start._context(binding),
            source_snapshot=CurrentSourceSnapshot(
                session_start.SNAPSHOT, "a" * 40, snapshot_digest
            ),
            environment_evidence_digest=digest(context_port.environment_label),
            hydration_key=hydration_key,
            observed_at=observed_at,
        )

    monkeypatch.setattr(session_start._ContextPort, "build", build_context)

    def capture_precompaction_invocation(
        manager: Any,
        binding: ContinuityBinding,
        observation: dict[str, Any],
        spool_digest: str,
        observed_at: str,
        expected_process_generation_digest: str,
        _expected_generation_created_at: str,
        _expected_managed_receipt_digest: str,
        expected_launch_command: Any,
        expected_ancestry_policy_digest: str,
        deadline: Any,
    ) -> Any:
        deadline.require_current()
        delivery_id = digest(
            {
                "schema": "zekam-codex-0151-delivery/v1",
                "session_id": binding.external_session_id,
                "external_event_type": "PreCompact",
                "turn_id": observation["turn_id"],
                "trigger": observation["trigger"],
                "wire_digest": observation["wire_digest"],
            }
        )
        return session_start._issue_test_snapshot(
            ManagedInvocationSnapshot,
            delivery_id=delivery_id,
            observed_at=observed_at,
            process_generation_digest=expected_process_generation_digest,
            ancestry_policy_digest=expected_ancestry_policy_digest,
            native_pid=101,
            native_uid=501,
            native_start_token="native-start",
            native_artifact_digest=digest("native"),
            hook_pid=manager.hook_pid,
            hook_uid=501,
            hook_start_token=manager.hook_start,
            shell_artifact_digest=digest("shell"),
            python_launcher_artifact_digest=digest("launcher"),
            python_runtime_artifact_digest=digest("runtime"),
            launch_command_digest=expected_launch_command.command_digest,
            observation_digest=digest(observation),
            spool_digest=spool_digest,
        )

    def assert_invocation_bounded(manager: Any, snapshot: Any, deadline: Any) -> None:
        deadline.require_current()
        manager.assert_invocation(snapshot)

    monkeypatch.setattr(
        session_start._Manager,
        "capture_precompaction_invocation",
        capture_precompaction_invocation,
        raising=False,
    )
    monkeypatch.setattr(
        session_start._Manager,
        "assert_invocation_bounded",
        assert_invocation_bounded,
        raising=False,
    )
    path, binding, manager, _context, spool, event, ingress = session_start._prepared(tmp_path)
    ingress.attach_process()
    assert ingress.session_start(event).replay is False
    generation = _test_generation(monkeypatch)
    writer = compact.SQLiteDormantV4PreCompactionWriter(
        path, binding, spool=spool, generation=generation
    )
    source = _BoundedSource(text, snapshot_digest)
    cast(Any, writer).process_manager = manager
    cast(Any, writer).source = source
    return path, binding, manager, spool, writer, source


def _binding() -> ContinuityBinding:
    return ContinuityBinding(
        SESSION,
        "codex-external",
        PROJECT,
        REALM,
        "codex",
        "macbook",
        SNAPSHOT,
        digest("task"),
        digest("plan"),
        digest("policy"),
    )


def test_writer_rejects_unissued_generation_before_database_or_spool_change(tmp_path: Path) -> None:
    database = tmp_path / "operational.db"
    database.write_bytes(b"unchanged")
    spool_root = tmp_path / "spool"
    spool = ClientLifecycleSpool(spool_root, client_id="codex")
    before = database.read_bytes()
    with pytest.raises(PolicyViolation):
        compact.SQLiteDormantV4PreCompactionWriter(
            database,
            _binding(),
            spool=spool,
            generation=object(),  # type: ignore[arg-type]
        )
    assert database.read_bytes() == before
    assert not spool_root.exists()


def test_concrete_generation_owned_ports_reject_lookalikes_without_io() -> None:
    for constructor in (compact._GenerationSource, compact._GenerationDurability):
        with pytest.raises(PolicyViolation):
            constructor(object())  # type: ignore[arg-type]


def test_writer_constructor_has_no_caller_selected_authority_ports() -> None:
    parameters = inspect.signature(compact.SQLiteDormantV4PreCompactionWriter).parameters
    assert tuple(parameters) == ("path", "binding", "spool", "generation")
    assert "process_manager" not in parameters
    assert "source" not in parameters
    assert "durability" not in parameters


def test_transaction_facade_exposes_only_fixed_operation_and_rolls_back_baseexception() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("create table evidence(value text)")
    owner = compact._OwnedImmediateTransaction(db)
    owner.planned(digest("plan"))
    owner.applying()
    db.execute("insert into evidence values('partial')")
    try:
        raise KeyboardInterrupt
    except BaseException:
        owner.abort()
    assert owner.state == "rolled-back"
    assert db.execute("select count(*) from evidence").fetchone()[0] == 0
    for name in ("db", "commit", "rollback", "savepoint", "attach", "pragma", "execute"):
        with pytest.raises((AttributeError, PolicyViolation)):
            getattr(owner, name)
    db.close()


def test_writer_outer_transaction_scope_rolls_back_every_baseexception() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._run)
    assert "except BaseException:" in source
    assert "owner.abort()" in source
    assert "finally:" in source and "db.close()" in source
    assert "apply_precompaction_graph" in source


def test_resource_census_is_selector_bound_and_not_full_database_walk() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._selected_census)
    assert "binding" in source and "delivery_id" in source and "deadline" in source
    assert "select *" not in source
    assert "pragma table_info" not in source
    assert "limit 4097" in source and "2_097_152" in source
    for table in ("schema_migration", "project_knowledge_realm", "local_recovery_resolution"):
        assert table not in source


def test_mutation_sql_exists_only_inside_fixed_transaction_owner() -> None:
    owner = inspect.getsource(compact._OwnedImmediateTransaction)
    writer = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter)
    assert "insert into continuity_native_event_receipt" in owner
    assert "def _insert(" not in writer
    assert "sqlite3.Connection" not in owner


def test_generation_rechecked_at_accept_first_mutation_precommit_read_only_response() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._run)
    for stage in ("accept", "first-mutation", "precommit", "read-only-verification", "response"):
        assert f'._recheck("{stage}")' in source


def test_catch_commit_unknown_is_read_only_and_never_repairs() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._commit_unknown)
    assert "read_only=True" in source
    assert "_verify_plan" in source
    assert "recovery_required" in source
    assert "insert " not in source.lower() and "update " not in source.lower()


def test_dormant_service_and_writer_share_exact_generation_type() -> None:
    annotation = (
        inspect.signature(compact.SQLiteDormantV4PreCompactionWriter)
        .parameters["generation"]
        .annotation
    )
    assert annotation == "_DarwinGenerationOwner"
    assert supervisor.PRODUCTION_GENERATION_ISSUED is False


def test_legacy_default_v3_and_b2_activation_are_outside_slice() -> None:
    from zekam.infrastructure.sqlite import operational_schema

    assert operational_schema.SCHEMA_VERSION == 3
    assert supervisor.DARWIN_LAUNCHD_CAPABILITY_OBSERVED is False
    assert supervisor.NATIVE_HOOK_ACTIVATED is False
    assert supervisor.NATIVE_ACK_OBSERVED is False


@pytest.mark.parametrize(
    "relation",
    (
        "CHECKPOINT_REQUESTED",
        "PRE_COMPACTION",
        "checkpoint-requested-event",
        "pre-compaction-event",
        "checkpoint-requested",
        "pre-compact-revision",
        "native-fork-shell-exec-launcher-exec-runtime/v1",
        "success_stdout_digest",
        "full_spool_tuple_digest",
        "hydrated_predecessor_revision_digest",
        "active_hydration_receipt_digest",
        "process_generation_digest",
    ),
)
def test_plan_retains_every_checkpoint_graph_relation(relation: str) -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._plan)
    if relation == "native-fork-shell-exec-launcher-exec-runtime/v1":
        assert relation == compact._TOPOLOGY
        assert "_TOPOLOGY" in source
    else:
        assert relation in source


@pytest.mark.parametrize(
    "selector",
    (
        "spool_sequence",
        "ancestry_receipt_digest",
        "event_digest",
        "operation_key",
        "idempotency_key",
        "revision_number",
        "process_generation_digest",
    ),
)
def test_selector_collision_preflight_covers_all_unique_selectors(selector: str) -> None:
    assert selector in inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._collisions)


def test_replay_requires_complete_seven_part_graph() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._run)
    assert "collisions != (1, 1, 1, 2, 2, 1, 1)" in source
    assert "partial replay graph" in source
    assert "elif any(collisions)" in source
    assert "deterministic identity collision" in source


def test_source_manifest_and_process_are_rechecked_before_and_after_apply() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._run)
    assert source.count("self._manifest") >= 2
    assert "self.source.snapshot" in source
    assert "self.process_manager.assert_invocation_bounded" in inspect.getsource(
        compact.SQLiteDormantV4PreCompactionWriter._verify_plan
    )
    assert "self.source.assert_current" in inspect.getsource(
        compact.SQLiteDormantV4PreCompactionWriter._verify_plan
    )


def test_pending_job_outbox_and_unpersisted_spool_are_hard_gates() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._run)
    assert "SQLiteDormantV4CloseWriter._no_pending" in source
    pending = inspect.getsource(SQLiteDormantV4CloseWriter._no_pending)
    assert "local_outbox" in pending and "local_effect_receipt" in pending
    assert "PreCompactionFailure.PENDING_WORK" in source
    assert "PreCompactionFailure.UNPERSISTED_DELTA" in source
    assert "persisted != tuple(item.entry_digest for item in entries[:-1])" in source


def test_spool_lock_encloses_source_and_transaction_lifetime() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._run)
    stage = source.index("with self.spool.stage_frozen")
    source_read = source.index("self.source.snapshot", stage)
    begin = source.index("_OwnedImmediateTransaction", source_read)
    commit = source.index("commit_verified", begin)
    assert stage < source_read < begin < commit


def test_commit_unknown_distinguishes_baseline_exact_post_and_third_state() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._commit_unknown)
    assert "observed == baseline" in source
    assert "observed != expected_post" in source
    assert "issue_decision=True" in source
    assert "replay=True" in source
    assert source.count("RECOVERY_REQUIRED") >= 4


def test_reopened_success_is_read_only_and_after_transaction_close() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._run)
    close = source.index("finally:\n                db.close()")
    reopen = source.index("read_only=True", close)
    decision = source.index("issue_decision=True", reopen)
    response = source.index('_recheck("response")', decision)
    assert close < reopen < decision < response


@pytest.mark.parametrize(
    ("exception_name", "category"),
    (
        ("TimeoutError", "DEADLINE"),
        ("LiveProcessVerificationError", "PROCESS_DRIFT"),
        ("ConcurrencyConflict", "UNPERSISTED_DELTA"),
        ("_CompactionGateError", "category"),
        ("ConfigurationError, sqlite3.Error, OSError", "STORAGE_UNAVAILABLE"),
        ("PolicyViolation", "RECOVERY_REQUIRED"),
        ("ValidationFailed", "VALIDATION"),
    ),
)
def test_public_writer_failure_mapping_is_fixed(exception_name: str, category: str) -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter.pre_compaction)
    assert exception_name in source and category in source
    assert "str(" not in source


def test_process_control_baseexceptions_are_not_mapped_to_hook_output() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter.pre_compaction)
    assert "except BaseException" not in source
    assert "SystemExit" not in source and "KeyboardInterrupt" not in source


def test_database_connect_is_existing_only_and_readonly_reopen() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._connect)
    assert 'mode = "ro" if read_only else "rw"' in source
    assert "?mode={mode}" in source
    assert "query_only=on" in source
    assert "foreign_keys=on" in source
    assert "busy_timeout" in source
    assert "bootstrap" not in source and "create table" not in source


def test_census_has_deadline_before_each_materialization_and_aggregate_cap() -> None:
    source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter._selected_census)
    assert "deadline.require_current" in source
    assert "count(*)" in source
    assert "cardinality exceeded" in source
    assert "byte bound exceeded" in source
    assert "2_097_152" in source


def test_generation_is_bound_into_plan_decision_deadline_and_result() -> None:
    from zekam.application import local_continuity_v4_compaction as application

    for function in (
        application._issue_deadline,
        application._issue_plan,
        application._issue_ack_decision,
        application._checkpoint_ready,
    ):
        assert "generation" in inspect.signature(function).parameters
    assert "generation_digest" in application.PreparedPreCompactionPlan.__dataclass_fields__
    assert "generation_digest" in application.VerifiedAckDecision.__dataclass_fields__


def test_production_source_composition_is_read_only_gate_a_authority() -> None:
    source = inspect.getsource(compact._GenerationSource)
    assert "read_portable_source_plan" in source
    assert "_validate_source_authority" in source
    assert "BoundedContinuitySource" in source
    assert "insert " not in source.lower() and "update " not in source.lower()
    assert "return CurrentSourceSnapshot" not in source
    assert "lambda" not in source and "Protocol" not in source


def test_gate_a_authority_is_held_read_only_and_resolves_exact_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_definition: Any = gate_a.authority
    fixture_factory = cast(Callable[[Path], dict[str, Any]], fixture_definition.__wrapped__)
    fixture = fixture_factory(tmp_path)
    publish_portable_source_plan(fixture["home"], fixture["record"])
    result = gate_a._execute(
        SQLiteLocalSourceAuthority(fixture["home"], fixture["path"]),
        fixture,
        previous_revision_digest=None,
        rebind=False,
    )
    binding = ContinuityBinding(
        SESSION,
        "codex-external",
        fixture["recipe"].project_id,
        fixture["recipe"].realm_id,
        "codex",
        "macbook",
        fixture["snapshot"].id,
        fixture["recipe"].task_digest,
        digest("plan"),
        fixture["recipe"].policy_digest,
    )
    generation = _test_generation(monkeypatch)
    deadline = application._issue_deadline(generation, __import__("time").monotonic_ns)
    source = compact._GenerationSource(generation, fixture["path"])
    before = fixture["path"].read_bytes()
    snapshot = source.snapshot(binding, deadline)
    text = (fixture["root"] / gate_a.SOURCE_REF).read_text(encoding="utf-8")
    body = {
        "source_ref": gate_a.SOURCE_REF,
        "revision": fixture["plan"].revision_ref,
        "digest": digest(text),
    }
    provenance = CanonicalManifestProvenance("source-health", canonical_json(body), digest(body))
    assert source.resolve_fragment(binding, snapshot, provenance, deadline).text == text
    source.assert_current(binding, snapshot, deadline)
    source_path = fixture["root"] / gate_a.SOURCE_REF
    source_path.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises((PolicyViolation, ValidationFailed)):
        source.assert_current(binding, snapshot, deadline)
    source_path.write_text(text, encoding="utf-8")
    sidecar = local_source_authority_path(fixture["home"])
    replacement = sidecar.with_name("replacement.sqlite3")
    replacement.write_bytes(sidecar.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(sidecar)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        source.assert_current(binding, snapshot, deadline)
    source.close()
    assert result.generation == 1 and fixture["path"].read_bytes() == before


def test_artifact_set_hashes_once_and_rechecks_retained_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = tuple(tmp_path / f"artifact-{index}" for index in range(4))
    payloads = tuple(f"artifact-{index}\n".encode() for index in range(4))
    for path, payload in zip(paths, payloads, strict=True):
        path.write_bytes(payload)
    pins = tuple(
        (f"role-{index}", len(payload), __import__("hashlib").sha256(payload).hexdigest())
        for index, payload in enumerate(payloads)
    )
    monkeypatch.setattr(lifecycle, "_ARTIFACT_PINS", pins)
    held = lifecycle._PinnedArtifactSet(cast(Any, paths))
    expected = tuple(f"sha256:{item[2]}" for item in pins)
    assert held.recheck() == expected
    replacement = tmp_path / "replacement"
    replacement.write_bytes(payloads[0])
    replacement.replace(paths[0])
    with pytest.raises(lifecycle.LiveProcessVerificationError):
        held.recheck()
    held.close()


def test_c2_and_c3_census_drift_never_acknowledge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for drift_call, expected_rows in ((3, 0), (4, 1)):
        case = tmp_path / f"case-{drift_call}"
        case.mkdir()
        path, binding, _manager, _spool, writer, _source = _prepared_behavioral(case, monkeypatch)
        original = compact.SQLiteDormantV4PreCompactionWriter._selected_census
        calls = 0

        def census(
            *args: Any,
            _original: Any = original,
            _drift_call: int = drift_call,
            **kwargs: Any,
        ) -> str:
            nonlocal calls
            calls += 1
            value = _original(*args, **kwargs)
            return digest(f"census-drift-{_drift_call}") if calls == _drift_call else value

        with monkeypatch.context() as scoped:
            scoped.setattr(
                compact.SQLiteDormantV4PreCompactionWriter,
                "_selected_census",
                staticmethod(census),
            )
            result = writer.pre_compaction(_precompact_event(binding))
        assert result.status != "checkpoint-ready"
        with sqlite3.connect(path) as db:
            assert (
                db.execute("select count(*) from continuity_checkpoint").fetchone()[0]
                == expected_rows
            )


def test_writer_has_no_arbitrary_sql_or_raw_connection_parameter() -> None:
    owner_signature = inspect.signature(
        compact._OwnedImmediateTransaction.apply_precompaction_graph
    )
    assert tuple(owner_signature.parameters) == ("self", "plan")
    assert all(
        parameter.annotation not in {sqlite3.Connection, "sqlite3.Connection"}
        for parameter in owner_signature.parameters.values()
    )
    writer_source = inspect.getsource(compact.SQLiteDormantV4PreCompactionWriter)
    assert "def _insert" not in writer_source
    assert "execute(self" not in writer_source


def test_direct_writer_success_replay_and_fresh_restart_persist_exact_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, manager, spool, writer, source = _prepared_behavioral(tmp_path, monkeypatch)
    event = _precompact_event(binding)
    first = writer.pre_compaction(event)
    replay = writer.pre_compaction(event)
    restarted = compact.SQLiteDormantV4PreCompactionWriter(
        path, binding, spool=spool, generation=writer.generation
    )
    cast(Any, restarted).process_manager = manager
    cast(Any, restarted).source = source
    third = restarted.pre_compaction(event)
    assert (first.status, first.replay) == ("checkpoint-ready", False)
    assert (replay.status, replay.replay) == ("checkpoint-ready", True)
    assert (third.status, third.replay) == ("checkpoint-ready", True)
    assert first.stdout == replay.stdout == third.stdout
    assert first.ack_decision_digest == replay.ack_decision_digest == third.ack_decision_digest
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 1
        assert db.execute("select count(*) from session_event").fetchone()[0] == 3
        assert (
            db.execute(
                "select state from continuity_hook_attachment_revision "
                "order by revision_number desc limit 1"
            ).fetchone()[0]
            == "pre-compact-committed"
        )
    assert source.deadline_checks >= 9


def test_seeded_pending_job_and_source_drift_leave_no_precompact_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _manager, _spool, writer, _source = _prepared_behavioral(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into local_job values(?,?,?,'ready',0,1,?,null,0,null,?,?)",
            (
                "018f0000-0000-7000-8000-000000000299",
                "pending",
                canonical_json({"session_id": binding.session_id}),
                session_start.NOW,
                session_start.NOW,
                session_start.NOW,
            ),
        )
    pending = writer.pre_compaction(_precompact_event(binding))
    assert (pending.status, pending.failure_category) == ("recovery-required", "PENDING_WORK")
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 0
        assert db.execute("select count(*) from session_event").fetchone()[0] == 1

    drift_root = tmp_path / "drift"
    drift_path, drift_binding, _manager2, _spool2, drift_writer, drift_source = (
        _prepared_behavioral(drift_root, monkeypatch)
    )
    drift_source.fail_current = True
    drift = drift_writer.pre_compaction(_precompact_event(drift_binding))
    assert (drift.status, drift.failure_category) == ("recovery-required", "SOURCE_DRIFT")
    assert drift.native_ack_observed is False
    with sqlite3.connect(drift_path) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 0
        assert db.execute("select count(*) from session_event").fetchone()[0] == 1


def test_commit_unknown_complete_baseline_and_apply_failure_are_behavioral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _manager, _spool, writer, _source = _prepared_behavioral(tmp_path, monkeypatch)
    original_commit = compact._OwnedImmediateTransaction.commit_verified

    def committed_then_unknown(owner: Any, value: str, deadline: Any) -> None:
        original_commit(owner, value, deadline)
        raise sqlite3.OperationalError("commit outcome unavailable")

    monkeypatch.setattr(
        compact._OwnedImmediateTransaction, "commit_verified", committed_then_unknown
    )
    recovered = writer.pre_compaction(_precompact_event(binding))
    assert (recovered.status, recovered.replay) == ("checkpoint-ready", True)
    assert recovered.durable_reopen_verified is True
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 1

    other = tmp_path / "baseline"
    path2, binding2, _manager2, _spool2, writer2, _source2 = _prepared_behavioral(
        other, monkeypatch
    )

    def unknown_before_commit(_owner: Any, _value: str, _deadline: Any) -> None:
        raise sqlite3.OperationalError("no commit")

    monkeypatch.setattr(
        compact._OwnedImmediateTransaction, "commit_verified", unknown_before_commit
    )
    baseline = writer2.pre_compaction(_precompact_event(binding2))
    assert (baseline.status, baseline.failure_category) == (
        "recovery-required",
        "RECOVERY_REQUIRED",
    )
    with sqlite3.connect(path2) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 0

    third = tmp_path / "apply"
    path3, binding3, _manager3, _spool3, writer3, _source3 = _prepared_behavioral(
        third, monkeypatch
    )
    original_apply = compact._OwnedImmediateTransaction.apply_precompaction_graph

    def apply_then_fail(owner: Any, plan: Any) -> None:
        original_apply(owner, plan)
        raise RuntimeError("after apply")

    monkeypatch.setattr(
        compact._OwnedImmediateTransaction, "apply_precompaction_graph", apply_then_fail
    )
    failed = writer3.pre_compaction(_precompact_event(binding3))
    assert (failed.status, failed.failure_category) == (
        "recovery-required",
        "RECOVERY_REQUIRED",
    )
    with sqlite3.connect(path3) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 0
        assert db.execute("select count(*) from session_event").fetchone()[0] == 1


def test_seeded_selector_collision_and_unpersisted_suffix_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _manager, spool, writer, _source = _prepared_behavioral(tmp_path, monkeypatch)
    event = _precompact_event(binding)
    delivery = digest(
        {
            "schema": "zekam-codex-0151-delivery/v1",
            "session_id": binding.external_session_id,
            "external_event_type": "PreCompact",
            "turn_id": event.turn_id,
            "trigger": event.trigger,
            "wire_digest": event.wire_digest,
        }
    )
    spool.stage(
        event.observation_body(),
        delivery_id=delivery,
        occurred_at=dt.datetime.fromisoformat("2026-09-03T12:00:01+00:00"),
    )
    with sqlite3.connect(path) as db:
        covered = db.execute(
            "select event_digest from session_event_detail where session_id=? and sequence=1",
            (binding.session_id,),
        ).fetchone()[0]
        db.execute(
            "insert into continuity_checkpoint values(?,?,?,?,?,?,?,?,?,?)",
            (
                digest("collision"),
                binding.session_id,
                f"precompact:{delivery}:checkpoint",
                1,
                covered,
                binding.source_snapshot_id,
                digest("context"),
                None,
                canonical_json({"collision": True}),
                session_start.NOW,
            ),
        )
    collision = writer.pre_compaction(event)
    assert collision.status != "checkpoint-ready"
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from session_event").fetchone()[0] == 1

    suffix_root = tmp_path / "suffix"
    suffix_path, suffix_binding, _manager2, suffix_spool, suffix_writer, _source2 = (
        _prepared_behavioral(suffix_root, monkeypatch)
    )
    extra = CodexMacOS0151Event(
        suffix_binding.external_session_id,
        "PostCompact",
        None,
        "turn-1",
        "manual",
        None,
        digest("postcompact-wire"),
    )
    suffix_spool.stage(
        extra.observation_body(),
        delivery_id=digest("unpersisted-postcompact"),
        occurred_at=dt.datetime.fromisoformat("2026-09-03T12:00:02+00:00"),
    )
    unpersisted = suffix_writer.pre_compaction(_precompact_event(suffix_binding))
    assert (unpersisted.status, unpersisted.failure_category) == (
        "recovery-required",
        "UNPERSISTED_DELTA",
    )
    with sqlite3.connect(suffix_path) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 0


def test_process_and_source_identity_drift_are_direct_closed_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_path, process_binding, manager, _spool, writer, _source = _prepared_behavioral(
        tmp_path / "process", monkeypatch
    )
    manager.fail_invocation = True
    process = writer.pre_compaction(_precompact_event(process_binding))
    assert (process.status, process.failure_category) == ("rejected", "PROCESS_DRIFT")
    with sqlite3.connect(process_path) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 0

    source_path, source_binding, _manager, _spool2, source_writer, source = _prepared_behavioral(
        tmp_path / "source-identity", monkeypatch
    )
    source.snapshot_id = "018f0000-0000-7000-8000-000000000299"
    result = source_writer.pre_compaction(_precompact_event(source_binding))
    assert (result.status, result.failure_category) == (
        "recovery-required",
        "SOURCE_DRIFT",
    )
    with sqlite3.connect(source_path) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 0


def test_commit_unknown_unexpected_selected_census_never_acknowledges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _manager, _spool, writer, _source = _prepared_behavioral(tmp_path, monkeypatch)
    original_commit = compact._OwnedImmediateTransaction.commit_verified
    original_census = compact.SQLiteDormantV4PreCompactionWriter._selected_census
    census_calls = 0

    def committed_then_unknown(owner: Any, value: str, deadline: Any) -> None:
        original_commit(owner, value, deadline)
        raise sqlite3.OperationalError("commit outcome unavailable")

    def unexpected_census(*args: Any, **kwargs: Any) -> str:
        nonlocal census_calls
        census_calls += 1
        observed = original_census(*args, **kwargs)
        return digest("unexpected-selected-census") if census_calls >= 4 else observed

    monkeypatch.setattr(
        compact._OwnedImmediateTransaction, "commit_verified", committed_then_unknown
    )
    monkeypatch.setattr(
        compact.SQLiteDormantV4PreCompactionWriter,
        "_selected_census",
        staticmethod(unexpected_census),
    )
    result = writer.pre_compaction(_precompact_event(binding))
    assert (result.status, result.failure_category) == (
        "recovery-required",
        "RECOVERY_REQUIRED",
    )
    assert result.native_ack_observed is False and census_calls >= 4
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 1


def test_deadline_reserve_and_wrong_event_are_direct_closed_behaviors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zekam.application import local_continuity_v4_compaction as application

    _path, binding, _manager, spool, writer, _source = _prepared_behavioral(tmp_path, monkeypatch)
    values = iter((1_000_000_000, 8_996_000_000, 9_000_000_000))
    deadline = application._issue_deadline(writer.generation, lambda: next(values))
    assert deadline.remaining_seconds(reserve_ms=1) == pytest.approx(0.003)
    with pytest.raises(TimeoutError):
        deadline.require_current(reserve_ms=1)
    with (
        pytest.raises(ValidationFailed),
        spool.stage_frozen(
            _precompact_event(binding).observation_body(),
            delivery_id=digest("wrong-deadline"),
            occurred_at=dt.datetime.fromisoformat("2026-09-03T12:00:02+00:00"),
            deadline=object(),
        ),
    ):
        pass
    wrong = writer.pre_compaction(session_start._event())
    null = writer.pre_compaction(None)  # type: ignore[arg-type]
    assert wrong.status != "checkpoint-ready" and null.status == "rejected"
    assert wrong.native_ack_observed is null.native_ack_observed is False


def test_canary_success_requires_real_writer_c3_and_exact_sealed_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory(prefix="zkpc-c3-", dir="/private/tmp") as directory:
        root = Path(directory)
        root.chmod(0o700)
        real_observe = supervisor._DarwinAuthorityAdapter.observe_current
        path, binding, _manager, _spool, writer, _source = _prepared_behavioral(root, monkeypatch)
        monkeypatch.setattr(supervisor._DarwinAuthorityAdapter, "observe_current", real_observe)
        nonce = "f" * 64
        socket_path = root / "canary.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            listener.listen(1)
            socket_path.chmod(0o600)
            monkeypatch.setattr(
                supervisor._DarwinAuthorityAdapter,
                "_launch_activate_socket",
                staticmethod(lambda _key=supervisor.LISTENER_KEY: (os.dup(listener.fileno()),)),
            )
            activation = supervisor._issue_canary_activation(
                nonce,
                f"io.zekam.precompaction-canary.{nonce}",
                str(socket_path),
            )
            generation = activation._generation
            cast(Any, writer).generation = generation
            cast(Any, writer).durability = compact._GenerationDurability(generation)
            decisions: list[application.VerifiedAckDecision] = []
            original_verify = compact.SQLiteDormantV4PreCompactionWriter._verify_plan

            def capture_decision(*args: Any, **kwargs: Any) -> Any:
                value = original_verify(*args, **kwargs)
                if value is not None:
                    decisions.append(value)
                return value

            monkeypatch.setattr(
                compact.SQLiteDormantV4PreCompactionWriter, "_verify_plan", capture_decision
            )
            event = _precompact_event(binding)
            observation = event.observation_body()
            delivery = digest(
                {
                    "schema": "zekam-codex-0151-delivery/v1",
                    "session_id": binding.external_session_id,
                    "external_event_type": "PreCompact",
                    "turn_id": event.turn_id,
                    "trigger": event.trigger,
                    "wire_digest": event.wire_digest,
                }
            )
            _parent, uid, start, _executable = lifecycle._process_row(os.getpid(), timeout=1.0)
            created = time.monotonic_ns()
            request: dict[str, object] = {
                "attempt_nonce": nonce,
                "binding_digest": binding.binding_digest,
                "client_pid": os.getpid(),
                "client_start_token": start,
                "client_uid": uid,
                "created_monotonic_ns": created,
                "deadline_monotonic_ns": created + ipc_client.TOTAL_DEADLINE_NS,
                "delivery_id": delivery,
                "event_observation": observation,
                "event_wire_digest": event.wire_digest,
                "external_session_id": binding.external_session_id,
                "protocol_digest": ipc_client.PROTOCOL_DIGEST,
                "request_key": "",
                "schema": "zekam-precompact-local-request/v1",
                "trigger": event.trigger,
                "turn_id": event.turn_id,
            }
            request["request_key"] = digest(
                {
                    "schema": "zekam-precompact-local-request-key/v1",
                    "binding_digest": request["binding_digest"],
                    "delivery_id": request["delivery_id"],
                    "event_wire_digest": request["event_wire_digest"],
                    "external_session_id": request["external_session_id"],
                    "trigger": request["trigger"],
                    "turn_id": request["turn_id"],
                }
            )
            service_results: list[int] = []

            def handle(_request: dict[str, object]) -> tuple[object, object]:
                result = writer.pre_compaction(event)
                assert result.status == "checkpoint-ready" and decisions
                return result, decisions[-1]

            service = threading.Thread(
                target=lambda: service_results.append(
                    supervisor.serve_canary_once(activation, handle)
                )
            )
            service.start()
            response = ipc_client.canary_exchange(
                socket_path,
                request,
                deadline_ns=time.monotonic_ns() + ipc_client.TOTAL_DEADLINE_NS,
            )
            service.join(8)
            assert not service.is_alive() and service_results == [os.EX_OK]
            assert response["classification"] == "checkpoint-ready"
            assert response["decision_digest"] == decisions[-1].decision_digest
            with sqlite3.connect(path) as db:
                assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 1


def test_raw_selector_resolves_exact_existing_hydrated_binding_and_rechecks_in_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, manager, spool, _writer, source = _prepared_behavioral(tmp_path, monkeypatch)
    with sqlite3.connect(path) as db:
        db.execute("update source_binding set source_kind='git' where id='source'")
    event = _precompact_event(binding)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        resolved = compact._resolve_existing_binding(db, event)
    assert resolved.binding == binding
    assert resolved.head_state == "hydrated"
    generation = _test_generation(monkeypatch)
    writer = compact.resolved_precompaction_writer(
        path, resolved, spool=spool, generation=generation
    )
    cast(Any, writer).process_manager = manager
    cast(Any, writer).source = source
    assert writer.pre_compaction(event).status == "checkpoint-ready"
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        replay = compact._resolve_existing_binding(db, event)
    assert replay.head_state == "pre-compact-committed"
    with pytest.raises(PolicyViolation), sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        compact._resolve_existing_binding(
            db,
            CodexMacOS0151Event(
                "missing-session",
                "PreCompact",
                None,
                "turn-1",
                "manual",
                None,
                digest("missing-wire"),
            ),
        )

    drift_root = tmp_path / "drift"
    drift_root.mkdir()
    drift_path, drift_binding, drift_manager, drift_spool, _unused, drift_source = (
        _prepared_behavioral(drift_root, monkeypatch)
    )
    with sqlite3.connect(drift_path) as db:
        db.execute("update source_binding set source_kind='git' where id='source'")
        db.row_factory = sqlite3.Row
        drift_resolution = compact._resolve_existing_binding(db, _precompact_event(drift_binding))
        db.execute("update source_binding set active=0 where id='source'")
    drift_writer = compact.resolved_precompaction_writer(
        drift_path,
        drift_resolution,
        spool=drift_spool,
        generation=_test_generation(monkeypatch),
    )
    cast(Any, drift_writer).process_manager = drift_manager
    cast(Any, drift_writer).source = drift_source
    assert (
        drift_writer.pre_compaction(_precompact_event(drift_binding)).status != "checkpoint-ready"
    )
    with sqlite3.connect(drift_path) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 0


def test_raw_canary_request_contains_no_binding_or_delivery_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lifecycle,
        "_process_row",
        lambda _pid, timeout=1.0: (101, os.geteuid(), "hook-start", Path(sys.executable)),
    )
    request = ipc_client._raw_canary_request(
        {
            "session_id": "native-session",
            "transcript_path": None,
            "cwd": "/private/tmp/source",
            "hook_event_name": "PreCompact",
            "turn_id": "turn-7",
            "trigger": "auto",
        },
        "ab" * 32,
    )
    assert request["schema"] == "zekam-precompact-local-raw-request/v1"
    assert "binding_digest" not in request
    assert "delivery_id" not in request
    assert (
        ipc_client.decode_frame(ipc_client.encode_frame(request, response=False), response=False)
        == request
    )
