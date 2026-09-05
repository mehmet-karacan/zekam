from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from zekam.application import local_continuity_v4_compaction as app_compaction
from zekam.application import local_continuity_v4_ingress as app_ingress
from zekam.application import local_continuity_v4_internal as app_internal
from zekam.application import local_continuity_v4_recovery as app_recovery
from zekam.application.local_continuity import ContinuityBinding, ContinuityTail
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure import macos_precompaction_supervisor as supervisor
from zekam.infrastructure.sqlite import local_continuity_v4_compaction as compaction
from zekam.infrastructure.sqlite import local_continuity_v4_ingress as ingress
from zekam.infrastructure.sqlite import local_continuity_v4_internal as internal
from zekam.infrastructure.sqlite import local_continuity_v4_recovery as recovery

UUID1 = "00000000-0000-4000-8000-000000000001"
UUID2 = "00000000-0000-4000-8000-000000000002"
UPPER_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()
DIGEST = "sha256:" + "a" * 64


def _binding() -> ContinuityBinding:
    return ContinuityBinding(
        UUID1,
        "external-session",
        UUID2,
        "00000000-0000-4000-8000-000000000003",
        "codex",
        "local-device",
        "00000000-0000-4000-8000-000000000004",
        DIGEST,
        DIGEST,
        DIGEST,
    )


@pytest.mark.parametrize(
    ("call", "value"),
    (
        (lambda value: app_internal._text(value, "value"), 1),
        (lambda value: app_internal._text(value, "value"), " bad"),
        (lambda value: app_internal._text(value, "value"), "bad\n"),
        (lambda value: app_internal._key(value, "key"), "not allowed"),
        (lambda value: app_internal._digest(value, "digest"), 1),
        (lambda value: app_internal._uuid(value, "uuid"), UPPER_UUID),
        (lambda value: app_internal._runtime_time(value, "time"), 1),
        (lambda value: app_internal._runtime_time(value, "time"), "2026-09-04T12:00:00"),
        (
            lambda value: app_internal._issued_time(value, "time"),
            "2026-09-04T12:00:00.100000+00:00",
        ),
        (lambda value: app_internal._positive_int(value, "integer"), True),
    ),
)
def test_internal_application_helpers_reject_noncanonical_values(
    call: object, value: object
) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        call(value)  # type: ignore[operator]


def test_internal_tail_and_helper_positive_boundaries() -> None:
    tail = ContinuityTail(0, None)
    assert app_internal._tail(tail) is tail
    assert app_internal._text("x", "value", maximum=1) == "x"
    assert app_internal._key("canonical-key", "key") == "canonical-key"
    assert app_internal._uuid(UUID1, "uuid") == UUID1
    assert (
        app_internal._issued_time("2026-09-04T12:00:00+00:00", "time")
        == "2026-09-04T12:00:00+00:00"
    )
    assert app_internal._positive_int(2_147_483_647, "integer") == 2_147_483_647
    with pytest.raises(ValidationFailed, match="tail"):
        app_internal._tail(object())


@pytest.mark.parametrize(
    ("helper", "value"),
    (
        (internal._runtime_time, 1),
        (internal._runtime_time, "not-a-time"),
        (internal._runtime_time, "2026-09-04T12:00:00"),
        (internal._whole_second, "2026-09-04T12:00:00.1+00:00"),
        (internal._uuid, 1),
        (internal._uuid, UPPER_UUID),
        (internal._bounded_runtime_identity, 1),
        (internal._bounded_runtime_identity, ""),
        (internal._bounded_runtime_identity, " bad"),
        (internal._bounded_runtime_identity, "bad\n"),
        (internal._bounded_runtime_identity, "x" * 513),
    ),
)
def test_internal_sqlite_helpers_reject_durable_drift(helper: object, value: object) -> None:
    with pytest.raises(PolicyViolation):
        helper(value, "field")  # type: ignore[operator]


def _row(body_json: object, receipt_digest: object = DIGEST) -> sqlite3.Row:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("create table row_body(body_json, receipt_digest)")
    db.execute("insert into row_body values (?, ?)", (body_json, receipt_digest))
    row = db.execute("select * from row_body").fetchone()
    assert row is not None
    db.close()
    return cast(sqlite3.Row, row)


@pytest.mark.parametrize("value", (None, "{", "[]", '{"b":1,"a":2}'))
def test_internal_row_body_rejects_malformed_or_noncanonical_json(value: object) -> None:
    with pytest.raises(PolicyViolation):
        internal._row_body(_row(value))


def test_internal_row_body_enforces_receipt_digest() -> None:
    body = {"a": 1}
    encoded = canonical_json(body)
    assert internal._row_body(_row(encoded))["a"] == 1
    with pytest.raises(PolicyViolation, match="digest drift"):
        internal._row_body(_row(encoded), receipt_digest=True)
    assert internal._row_body(_row(encoded, digest(body)), receipt_digest=True) == body


def test_internal_event_receipt_requires_deterministic_detail() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "create table session_event(id text, session_id text, event_kind text, created_at text);"
        "create table session_event_detail(event_id text, session_id text, event_digest text);"
        "create table receipt(event_digest text, session_id text);"
        "insert into receipt values "
        "('sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','s');"
    )
    receipt = db.execute("select * from receipt").fetchone()
    assert receipt is not None
    with pytest.raises(PolicyViolation, match="deterministic event"):
        internal._event_for_receipt(db, receipt)
    db.close()


def test_internal_event_and_receipt_relations_reject_drift() -> None:
    binding = _binding()
    expected_event = internal._expected_event(
        kind="USER_TURN_COMMITTED",
        operation_key="turn-commit:user:one",
        occurred_at="2026-09-04T12:00:00+00:00",
        source_refs=["turn/" + UUID1],
        evidence_digests=[DIGEST],
    )
    detail = {
        "sequence": 1,
        "previous_digest": None,
        "event_digest": DIGEST,
        "event_kind": "USER_TURN_COMMITTED",
        "idempotency_key": "turn-commit:user:one",
        "event_created_at": "2026-09-04T12:00:00+00:00",
        "spool_digest": None,
    }
    with pytest.raises(PolicyViolation, match="event body"):
        internal._verify_event_body(
            cast(sqlite3.Row, detail),
            {},
            binding=binding,
            expected=expected_event,
        )
    producer_ref = digest("producer")
    receipt_body = {
        "binding_digest": binding.binding_digest,
        "created_at": "2026-09-04T12:00:00+00:00",
        "event_digest": DIGEST,
        "event_kind": "USER_TURN_COMMITTED",
        "producer_kind": "turn_commit_digest",
        "producer_ref": producer_ref,
        "session_id": binding.session_id,
    }
    receipt = {
        **dict.fromkeys(
            (
                "turn_commit_digest",
                "effect_claim_id",
                "effect_receipt_id",
                "native_event_receipt_digest",
                "close_request_digest",
                "close_receipt_digest",
                "hook_recovery_resolution_id",
                "local_recovery_resolution_id",
            )
        ),
        "turn_commit_digest": producer_ref,
        "binding_digest": "wrong",
        "session_id": binding.session_id,
        "created_at": "2026-09-04T12:00:00+00:00",
        "event_digest": DIGEST,
        "event_kind": "USER_TURN_COMMITTED",
        "attachment_revision_digest": DIGEST,
        "body_json": canonical_json(receipt_body),
        "receipt_digest": digest("wrong"),
    }
    with pytest.raises(PolicyViolation, match="receipt parity"):
        internal._verify_internal(
            receipt,  # type: ignore[arg-type]
            {
                "event_created_at": receipt["created_at"],
                "event_digest": DIGEST,
                "event_kind": "USER_TURN_COMMITTED",
                "previous_digest": None,
                "idempotency_key": "turn-commit:user:one",
            },  # type: ignore[arg-type]
            binding=binding,
            producer_kind="turn_commit_digest",
            producer_ref=producer_ref,
        )


def test_ingress_generation_verifier_rejects_missing_and_drift() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "create table continuity_hook_process_generation("
        "process_generation_digest text,native_pid integer,native_uid integer,"
        "native_start_token text,native_artifact_digest text,ancestry_policy_digest text)"
    )
    invocation = SimpleNamespace(
        process_generation_digest=DIGEST,
        native_pid=10,
        native_uid=501,
        native_start_token="start",
        native_artifact_digest=digest("native"),
        ancestry_policy_digest=digest("ancestry"),
    )
    with pytest.raises(PolicyViolation, match="generation tuple drift"):
        ingress.SQLiteCodexV4Ingress._verify_invocation_generation(db, invocation)  # type: ignore[arg-type]
    db.execute(
        "insert into continuity_hook_process_generation values (?,?,?,?,?,?)",
        (
            DIGEST,
            11,
            501,
            "start",
            digest("native"),
            digest("ancestry"),
        ),
    )
    with pytest.raises(PolicyViolation, match="generation tuple drift"):
        ingress.SQLiteCodexV4Ingress._verify_invocation_generation(db, invocation)  # type: ignore[arg-type]
    db.execute(
        "update continuity_hook_process_generation set native_pid=10 "
        "where process_generation_digest=?",
        (DIGEST,),
    )
    ingress.SQLiteCodexV4Ingress._verify_invocation_generation(db, invocation)  # type: ignore[arg-type]
    db.close()


def _managed_process(**changes: object) -> app_ingress.ManagedProcessSnapshot:
    values: dict[str, object] = {
        "attachment_id": UUID1,
        "captured_at": "2026-09-04T12:00:00+00:00",
        "native_pid": 10,
        "native_uid": 501,
        "native_start_token": "start-token",
        "native_artifact_digest": digest("native"),
        "client_contract_digest": digest("contract"),
        "hook_set_digest": digest("hooks"),
        "ancestry_policy_digest": digest("ancestry"),
        "reviewed_commands": (),
    }
    values.update(changes)
    result = object.__new__(app_ingress.ManagedProcessSnapshot)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    return result


@pytest.mark.parametrize(
    "changes",
    (
        {"attachment_id": 1},
        {"native_pid": True},
        {"native_uid": -1},
        {"reviewed_commands": []},
        {"reviewed_commands": ()},
    ),
)
def test_ingress_process_snapshot_rejects_malformed_fields(changes: dict[str, object]) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        _managed_process(**changes).__post_init__()


def _managed_invocation(**changes: object) -> app_ingress.ManagedInvocationSnapshot:
    values: dict[str, object] = {
        "delivery_id": DIGEST,
        "observed_at": "2026-09-04T12:00:00+00:00",
        "process_generation_digest": digest("generation"),
        "ancestry_policy_digest": digest("ancestry"),
        "native_pid": 10,
        "native_uid": 501,
        "native_start_token": "native-start",
        "native_artifact_digest": digest("native"),
        "hook_pid": 11,
        "hook_uid": 501,
        "hook_start_token": "hook-start",
        "shell_artifact_digest": digest("shell"),
        "python_launcher_artifact_digest": digest("launcher"),
        "python_runtime_artifact_digest": digest("runtime"),
        "launch_command_digest": digest("command"),
        "observation_digest": digest("observation"),
        "spool_digest": digest("spool"),
    }
    values.update(changes)
    result = object.__new__(app_ingress.ManagedInvocationSnapshot)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    return result


def test_ingress_invocation_rejects_integer_ancestry_and_artifact_drift() -> None:
    _managed_invocation().__post_init__()
    for changes in (
        {"native_pid": True},
        {"hook_uid": -1},
        {"hook_pid": 10},
        {"hook_uid": 502},
        {"shell_artifact_digest": digest("native")},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _managed_invocation(**changes).__post_init__()


@pytest.mark.parametrize("value", (None, b"x", "sha256:" + "A" * 64, "a" * 71))
def test_compaction_digest_rejects_wrong_shape(value: object) -> None:
    with pytest.raises(ValidationFailed, match="digest"):
        compaction._digest_text(value)  # type: ignore[arg-type]
    assert compaction._digest_text(DIGEST) == DIGEST


def _generation(monkeypatch: pytest.MonkeyPatch) -> supervisor._DarwinGenerationOwner:
    listener = supervisor._DarwinListenerObservation(
        "/private/tmp/zekam-precompact-coverage.sock", 7, 501, 0o600, 1, 2, 1, 1
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
    object.__setattr__(owner, "_digest", digest("coverage-generation"))
    seal = digest("coverage-generation-seal")
    object.__setattr__(owner, "_seal", seal)
    monkeypatch.setitem(supervisor._GENERATIONS, seal, owner)
    monkeypatch.setitem(supervisor._GENERATION_PARITY, seal, supervisor._generation_bytes(owner))
    return owner


def test_compaction_deadline_checks_clock_reserve_expiry_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _generation(monkeypatch)
    with pytest.raises(ValidationFailed, match="clock invalid"):
        app_compaction._issue_deadline(generation, lambda: True)
    now = [1_000_000_000]
    deadline = app_compaction._issue_deadline(generation, lambda: now[0])
    with pytest.raises(ValidationFailed, match="reserve"):
        deadline.remaining_seconds(reserve_ms=True)
    now[0] = deadline._deadline_ns
    with pytest.raises(TimeoutError, match="exhausted"):
        deadline.require_current()
    now[0] = -1
    with pytest.raises(PolicyViolation, match="clock drift"):
        deadline.remaining_ns()
    now[0] = 1_000_000_000
    with pytest.raises(PolicyViolation, match="production generation"):
        deadline.assert_generation(object())


def test_compaction_binding_resolver_rejects_non_absolute_and_symlink_paths(
    tmp_path: Path,
) -> None:
    event = SimpleNamespace(event_type="PreCompact")
    with pytest.raises(ValidationFailed, match="database path"):
        compaction.resolve_existing_precompaction_binding(Path("relative.db"), event, cwd=tmp_path)  # type: ignore[arg-type]
    database = tmp_path / "operational.db"
    database.write_bytes(b"")
    link = tmp_path / "link.db"
    link.symlink_to(database)
    with pytest.raises(ValidationFailed, match="database path"):
        compaction.resolve_existing_precompaction_binding(link, event, cwd=tmp_path)  # type: ignore[arg-type]


class _Snapshot:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid

    def __post_init__(self) -> None:
        if not self.valid:
            raise ValidationFailed("bad")


class _Port:
    def __init__(self, value: object = None, error: BaseException | None = None) -> None:
        self.value = value
        self.error = error

    def snapshot(self, _request: object) -> object:
        if self.error is not None:
            raise self.error
        return self.value

    def recheck(self, _snapshot: object) -> None:
        if self.error is not None:
            raise self.error


def test_recovery_authority_wrappers_fail_closed_and_preserve_valid_values() -> None:
    value = _Snapshot()
    assert recovery._safe_snapshot(_Port(value), object(), _Snapshot) is value
    for port in (_Port(object()), _Port(_Snapshot(valid=False)), _Port(error=OSError("lost"))):
        with pytest.raises(PolicyViolation, match="snapshot"):
            recovery._safe_snapshot(port, object(), _Snapshot)
    recovery._safe_recheck(_Port(value), value)
    with pytest.raises(PolicyViolation, match="recheck"):
        recovery._safe_recheck(_Port(error=OSError("lost")), value)


def test_recovery_revision_helpers_reject_missing_cardinality() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("create table continuity_hook_attachment(session_id text, attachment_id text)")
    with pytest.raises(PolicyViolation, match="exact hook attachment"):
        recovery._current_revision(db, _binding())
    db.execute(
        "create table continuity_hook_attachment_revision("
        "local_recovery_case_id text,state text,revision_number integer)"
    )
    with pytest.raises(PolicyViolation, match="revision cardinality"):
        recovery._entry_revision(db, UUID1)
    db.close()


def test_recovery_result_payloads_reject_wrong_status_operation_and_flags() -> None:
    with pytest.raises(ValidationFailed, match="entry result status"):
        app_recovery._UnknownEntryResultPayload("pending", UUID1, UUID2, DIGEST)
    with pytest.raises(ValidationFailed, match="resolution result status"):
        app_recovery._FailedRecoveryAttentionResultPayload("pending", UUID1, UUID2, UUID1)
    with pytest.raises(ValidationFailed, match="commit result operation"):
        app_recovery._RecoveryCommitOutcomePayload("other", UUID1, UUID2)
    body = {
        "schema": "zekam-v4-recovery-commit-outcome/v1",
        "status": "not-committed-or-unobservable",
        "operation": "resolve",
        "claim_id": UUID1,
        "recovery_case_id": UUID2,
        "safe_to_retry": True,
        "grants_authority": True,
        "approval_inherited": False,
        "production_activated": False,
    }
    with pytest.raises(ValidationFailed, match="authority flags"):
        app_recovery._validate_result_body(body)


def test_ingress_application_helpers_enforce_exact_wire_values() -> None:
    assert app_ingress._whole_second("2026-09-04T12:00:00+00:00") == ("2026-09-04T12:00:00+00:00")
    assert app_ingress._exact_digest(DIGEST) == DIGEST
    for value in (1, "2026-09-04T12:00:00.1+00:00", "2026-09-04T12:00:00"):
        with pytest.raises(ValidationFailed):
            app_ingress._whole_second(value)
    with pytest.raises(ValidationFailed, match="digest string"):
        app_ingress._exact_digest(1)
    with pytest.raises(ValidationFailed, match="fragment text"):
        app_ingress.startup_fragment_budget_units(1)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed, match="additional context"):
        app_ingress._session_start_success_stdout(1)  # type: ignore[arg-type]


def test_compaction_deadline_rejects_bad_clock_and_reserve() -> None:
    with pytest.raises(PolicyViolation):
        app_compaction.SealedPreCompactionDeadline()
    with pytest.raises(ValidationFailed, match="reserve"):
        value = object.__new__(app_compaction.SealedPreCompactionDeadline)
        app_compaction.SealedPreCompactionDeadline.remaining_seconds(value, reserve_ms=True)
