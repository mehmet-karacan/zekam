"""Current-source branch repairs for bounded source and recovery transactions."""

from __future__ import annotations

import ctypes
import errno
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.unit import test_local_runtime_recovery_tx as recovery

from zekam.application.app_server import AppServerConnection, InMemoryNotificationStore
from zekam.application.ignore_rules import IgnoreMatcher
from zekam.application.local_continuity_source_authority import PortableSourcePlanRecord
from zekam.application.local_continuity_source_plan import (
    CapturedSourceFile,
    ContinuitySourcePlan,
    ContinuitySourceRecipe,
)
from zekam.domain.app_server_protocol import ProtocolFault, schema_bundle_digest
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.infrastructure import local_continuity_source_plan as source_module
from zekam.infrastructure.local_continuity_source_plan import (
    BoundedContinuitySource,
    _bounded_git_process,
    _GuardedSQLite,
    _open_owned_directory,
    _portable_plan_parent,
    _read_plan_at,
    _rename_exclusive,
    _source_authority_birthtime,
    _source_authority_cleanup,
    _SourceAuthorityDeadline,
    publish_portable_source_plan,
    read_portable_source_plan,
)
from zekam.infrastructure.sqlite.local_runtime_recovery_tx import (
    EffectRecoveryCaseSpec,
    EffectRecoveryResolutionSpec,
    LockRow,
    RecoveryReconcileSpec,
    RecoveryTransitionSpec,
    _canonical_payload,
    _prepare_outbox,
    insert_effect_recovery_case_tx,
    insert_effect_recovery_resolution_tx,
    reconcile_effect_recovery_job_tx,
    transition_running_job_to_recovery_tx,
)

PROJECT = "22222222-2222-4222-8222-222222222222"
BINDING = "33333333-3333-4333-8333-333333333333"
SNAPSHOT = "44444444-4444-4444-8444-444444444444"
REALM = "55555555-5555-4555-8555-555555555555"
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64


def _record() -> PortableSourcePlanRecord:
    recipe = ContinuitySourceRecipe(PROJECT, REALM, BINDING, ("AGENTS.md",), SHA_A, SHA_B)
    plan = ContinuitySourcePlan(
        recipe,
        "0" * 40,
        (CapturedSourceFile("AGENTS.md", SHA_C, 9),),
        ((".git/info/exclude", None),),
        SHA_A,
    )
    return PortableSourcePlanRecord(SNAPSHOT, plan)


def _portable_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    parent = home / "projeler" / PROJECT / "baglantilar"
    parent.mkdir(parents=True, mode=0o700)
    for path in (home, home / "projeler", home / "projeler" / PROJECT, parent):
        path.chmod(0o700)
    return home


def _git_source(tmp_path: Path) -> BoundedContinuitySource:
    root = tmp_path / "source"
    root.mkdir(mode=0o700)
    (root / "AGENTS.md").write_text("bounded\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Coverage"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "coverage@example.invalid"], cwd=root, check=True
    )
    subprocess.run(["git", "add", "AGENTS.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    return BoundedContinuitySource(root, _record().plan.recipe)


def _begin(tmp_path: Path) -> sqlite3.Connection:
    db = recovery._db(tmp_path)
    db.execute("begin immediate")
    return db


def _case_spec(route: str = "sweep-receiptless") -> EffectRecoveryCaseSpec:
    body: dict[str, object] = {
        "case_kind": "effect-unknown",
        "claim_id": recovery.CLAIM,
        "effect_digest": digest("effect"),
    }
    fence: int | None = None
    if route == "sweep-receiptless":
        body["recovered_fence"] = 1
        fence = 1
    return EffectRecoveryCaseSpec(
        route,
        recovery.CASE,
        recovery.JOB,
        recovery.CLAIM,
        None,
        digest("effect"),
        fence,
        digest(body),
        recovery.NOW,
    )


def test_recovery_case_missing_claim_unknown_and_replay_paths(tmp_path: Path) -> None:
    db = _begin(tmp_path)
    try:
        with pytest.raises(ConcurrencyConflict, match="claim scope"):
            insert_effect_recovery_case_tx(
                db, _case_spec()._replace(job_id="018f0000-0000-7000-8000-000000000898")
            )
        db.rollback()
    finally:
        db.close()

    db = _begin(tmp_path / "unknown")
    try:
        receipt_evidence = digest("receipt")
        db.execute(
            "insert into local_effect_receipt values(?,?,?,?,?)",
            (
                "018f0000-0000-7000-8000-000000000899",
                recovery.CLAIM,
                "unknown",
                receipt_evidence,
                recovery.NOW,
            ),
        )
        expected = digest(
            {
                "case_kind": "effect-unknown",
                "claim_id": recovery.CLAIM,
                "receipt_evidence": receipt_evidence,
            }
        )
        spec = EffectRecoveryCaseSpec(
            "unknown-receipt",
            recovery.CASE,
            recovery.JOB,
            recovery.CLAIM,
            receipt_evidence,
            None,
            None,
            expected,
            recovery.NOW,
        )
        first = insert_effect_recovery_case_tx(db, spec)
        second = insert_effect_recovery_case_tx(db, spec)
        assert first.inserted is True and second.inserted is False
        with pytest.raises(ValidationFailed, match="Unknown receipt"):
            insert_effect_recovery_case_tx(db, spec._replace(effect_digest=digest("effect")))
        db.rollback()
    finally:
        db.close()


def test_recovery_receiptless_and_transition_prevalidation_edges(tmp_path: Path) -> None:
    db = _begin(tmp_path)
    try:
        db.execute(
            "insert into local_effect_receipt values(?,?,?,?,?)",
            (
                "018f0000-0000-7000-8000-000000000899",
                recovery.CLAIM,
                "completed",
                digest("receipt"),
                recovery.NOW,
            ),
        )
        with pytest.raises(ConcurrencyConflict, match="found receipt"):
            insert_effect_recovery_case_tx(db, _case_spec("finish-receiptless"))
        db.rollback()
    finally:
        db.close()

    db = _begin(tmp_path / "finish")
    try:
        with pytest.raises(ValidationFailed, match="fence must be absent"):
            insert_effect_recovery_case_tx(
                db, _case_spec("finish-receiptless")._replace(recovered_fence=1)
            )
        with pytest.raises(ValidationFailed, match="transition spec"):
            transition_running_job_to_recovery_tx(db, cast(Any, object()))
        spec = recovery._case_and_transition_spec(db)
        with pytest.raises(ValidationFailed, match="lock row"):
            transition_running_job_to_recovery_tx(
                db, spec._replace(expected_locks=cast(Any, (object(),)))
            )
        with pytest.raises(ValidationFailed, match="case evidence tuple"):
            transition_running_job_to_recovery_tx(
                db, spec._replace(ordered_case_evidence_digests=cast(Any, []))
            )
        with pytest.raises(PolicyViolation, match="terminal evidence"):
            transition_running_job_to_recovery_tx(
                db, spec._replace(expected_terminal_evidence_digest=digest("wrong"))
            )
        db.rollback()
    finally:
        db.close()


def test_recovery_payload_case_and_running_graph_drift(tmp_path: Path) -> None:
    body = canonical_json({"job_id": recovery.OTHER_CASE})
    with pytest.raises(PolicyViolation, match="job payload"):
        _prepare_outbox(
            outbox_id=recovery.OUTBOX,
            job_id=recovery.JOB,
            key="recovery",
            event_kind="job.failed",
            payload_json=body,
            payload_digest=digest({"job_id": recovery.OTHER_CASE}),
            created_at=recovery.NOW,
        )
    with pytest.raises(ValidationFailed, match="malformed"):
        _canonical_payload("{", digest({}))
    with pytest.raises(ValidationFailed, match="noncanonical"):
        _canonical_payload('{"b":1, "a":2}', digest({"a": 2, "b": 1}))

    db = _begin(tmp_path)
    try:
        spec = recovery._case_and_transition_spec(db)
        db.execute(
            "update local_job set state='failed',terminal_evidence_digest=?", (digest("terminal"),)
        )
        with pytest.raises(ConcurrencyConflict, match="running job/lease"):
            transition_running_job_to_recovery_tx(db, spec)
        db.rollback()
    finally:
        db.close()


def test_recovery_unknown_sweep_and_receipt_state_drift(tmp_path: Path) -> None:
    db = _begin(tmp_path)
    try:
        receipt_evidence = digest("receipt")
        db.execute(
            "insert into local_effect_receipt values(?,?,?,?,?)",
            (
                "018f0000-0000-7000-8000-000000000899",
                recovery.CLAIM,
                "unknown",
                receipt_evidence,
                recovery.NOW,
            ),
        )
        case_evidence = digest(
            {
                "case_kind": "effect-unknown",
                "claim_id": recovery.CLAIM,
                "receipt_evidence": receipt_evidence,
            }
        )
        insert_effect_recovery_case_tx(
            db,
            EffectRecoveryCaseSpec(
                "unknown-receipt",
                recovery.CASE,
                recovery.JOB,
                recovery.CLAIM,
                receipt_evidence,
                None,
                None,
                case_evidence,
                recovery.NOW,
            ),
        )
        swept = digest(
            {
                "case_kind": "effect-unknown",
                "claim_id": recovery.CLAIM,
                "effect_digest": digest("effect"),
                "recovered_fence": 1,
            }
        )
        payload = {"job_id": recovery.JOB, "state": "recovery-required", "fencing_token": 1}
        spec = RecoveryTransitionSpec(
            "sweep-recovery-required",
            recovery.JOB,
            recovery.LEASE,
            1,
            (LockRow("resource/a", recovery.JOB, recovery.LEASE, 1, recovery.NOW),),
            (swept,),
            digest([swept]),
            recovery.NOW,
            recovery.OUTBOX,
            8,
            digest(payload),
        )
        assert transition_running_job_to_recovery_tx(db, spec).new_state == "recovery-required"
        db.rollback()
    finally:
        db.close()

    db = _begin(tmp_path / "drift")
    try:
        spec = recovery._case_and_transition_spec(db)
        db.execute(
            "insert into local_effect_receipt values(?,?,?,?,?)",
            (
                "018f0000-0000-7000-8000-000000000899",
                recovery.CLAIM,
                "failed",
                digest("receipt"),
                recovery.NOW,
            ),
        )
        with pytest.raises(ConcurrencyConflict, match="receipt state"):
            transition_running_job_to_recovery_tx(db, spec)
        db.rollback()
    finally:
        db.close()


def test_resolution_and_reconcile_reject_exact_graph_drift(tmp_path: Path) -> None:
    db = _begin(tmp_path)
    try:
        with pytest.raises(ValidationFailed, match="resolution spec"):
            insert_effect_recovery_resolution_tx(db, cast(Any, object()))
        with pytest.raises(ConcurrencyConflict, match="open effect case"):
            insert_effect_recovery_resolution_tx(
                db,
                EffectRecoveryResolutionSpec(
                    recovery.RESOLUTION,
                    recovery.CASE,
                    "completed",
                    digest("resolution"),
                    recovery.NOW,
                ),
            )
        recovery._enter(db)
        resolution = EffectRecoveryResolutionSpec(
            recovery.RESOLUTION, recovery.CASE, "completed", digest("resolution"), recovery.NOW
        )
        insert_effect_recovery_resolution_tx(db, resolution)
        with pytest.raises(ConcurrencyConflict, match="open effect case"):
            insert_effect_recovery_resolution_tx(db, resolution)
        with pytest.raises(ValidationFailed, match="reconcile spec"):
            reconcile_effect_recovery_job_tx(db, cast(Any, object()))
        terminal = digest([(None, None, "completed", digest("resolution"))])
        base = RecoveryReconcileSpec(
            recovery.JOB,
            (recovery.CASE,),
            "completed",
            terminal,
            recovery.NOW,
            recovery.OTHER_OUTBOX,
            8,
            digest({"job_id": recovery.JOB, "state": "completed", "reconciled": True}),
        )
        with pytest.raises(ValidationFailed, match="case tuple"):
            reconcile_effect_recovery_job_tx(db, base._replace(expected_case_ids=()))
        with pytest.raises(ValidationFailed, match="noncanonical"):
            reconcile_effect_recovery_job_tx(
                db, base._replace(expected_case_ids=(recovery.CASE, recovery.CASE))
            )
        with pytest.raises(ConcurrencyConflict, match="case tuple drift"):
            reconcile_effect_recovery_job_tx(
                db, base._replace(expected_case_ids=(recovery.OTHER_CASE,))
            )
        with pytest.raises(PolicyViolation, match="terminal evidence drift"):
            reconcile_effect_recovery_job_tx(
                db, base._replace(expected_terminal_evidence_digest=digest("wrong"))
            )
        db.execute("update local_job set state='failed'")
        with pytest.raises(ConcurrencyConflict, match="job state drift"):
            reconcile_effect_recovery_job_tx(db, base)
        db.rollback()
    finally:
        db.close()


def test_source_low_level_identity_deadline_and_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = cast(os.stat_result, SimpleNamespace(st_birthtime=None))
    with pytest.raises(PolicyViolation, match="birth time"):
        _source_authority_birthtime(invalid)

    monkeypatch.setattr(source_module.__dict__["time"], "monotonic_ns", lambda: -1)
    with pytest.raises(PolicyViolation, match="deadline unavailable"):
        _SourceAuthorityDeadline()

    class Broken:
        def rollback(self) -> None:
            raise RuntimeError("rollback")

        def close(self) -> None:
            raise RuntimeError("close")

    with pytest.raises(RuntimeError, match="rollback"):
        _source_authority_cleanup(cast(Any, Broken()), None, None, None)


def test_guarded_sqlite_rejects_canary_escape_and_unexpected_error() -> None:
    db = sqlite3.connect(":memory:")
    guard = _GuardedSQLite(db)
    try:
        db.set_authorizer(None)
        with pytest.raises(PolicyViolation, match="canary failed"):
            guard.execute("SELECT", "select 1")
    finally:
        guard.close()

    class Unexpected:
        def set_authorizer(self, _callback: object) -> None:
            pass

        def execute(self, _sql: str, _parameters: tuple[object, ...] = ()) -> object:
            error = sqlite3.OperationalError("unexpected")
            error.sqlite_errorcode = sqlite3.SQLITE_ERROR
            raise error

    guard = _GuardedSQLite(cast(Any, Unexpected()))
    with pytest.raises(sqlite3.OperationalError, match="unexpected"):
        guard.execute("SELECT", "select 1")


def test_portable_directory_reader_and_rename_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "open"
    directory.mkdir()
    original_fstat = os.fstat

    def foreign(fd: int) -> os.stat_result:
        info = original_fstat(fd)
        values = list(info)
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", foreign)
    with pytest.raises(PolicyViolation, match="directory policy"):
        _open_owned_directory(None, directory)
    monkeypatch.setattr(os, "fstat", original_fstat)

    with pytest.raises(ValidationFailed, match="absolute home"):
        _portable_plan_parent(Path("relative"), PROJECT)
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(PolicyViolation, match="file policy"):
            _read_plan_at(parent, empty.name)
    finally:
        os.close(parent)

    monkeypatch.setattr(
        source_module.__dict__["ctypes"], "CDLL", lambda *_args, **_kwargs: object()
    )
    with pytest.raises(PolicyViolation, match="publication unavailable"):
        _rename_exclusive(1, "a", "b")


def test_rename_errno_and_portable_publish_failure_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Function:
        argtypes: object = None
        restype: object = None

        def __init__(self, number: int) -> None:
            self.number = number

        def __call__(self, *_args: object) -> int:
            ctypes.set_errno(self.number)
            return -1

    class Library:
        def __init__(self, number: int) -> None:
            self.renameatx_np = Function(number)

    monkeypatch.setattr(
        source_module.__dict__["ctypes"], "CDLL", lambda *_a, **_k: Library(errno.EEXIST)
    )
    with pytest.raises(FileExistsError):
        _rename_exclusive(1, "a", "b")
    monkeypatch.setattr(
        source_module.__dict__["ctypes"], "CDLL", lambda *_a, **_k: Library(errno.EIO)
    )
    with pytest.raises(OSError, match="publication failed"):
        _rename_exclusive(1, "a", "b")

    with pytest.raises(ValidationFailed, match="Typed portable"):
        publish_portable_source_plan(tmp_path, cast(Any, object()))
    home, record = _portable_home(tmp_path), _record()
    monkeypatch.setattr(source_module.__dict__["os"], "write", lambda *_args: 0)
    with pytest.raises(PolicyViolation, match="publication failed"):
        publish_portable_source_plan(home, record)


def test_portable_concurrent_conflict_and_read_address_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, record = _portable_home(tmp_path), _record()
    original = source_module._rename_exclusive

    def concurrent(parent: int, source: str, destination: str) -> None:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        try:
            os.write(fd, b"different")
        finally:
            os.close(fd)
        raise FileExistsError(destination)

    monkeypatch.setattr(source_module, "_rename_exclusive", concurrent)
    with pytest.raises(PolicyViolation, match="concurrent conflict"):
        publish_portable_source_plan(home, record)
    monkeypatch.setattr(source_module, "_rename_exclusive", original)
    target = (
        home
        / "projeler"
        / PROJECT
        / "baglantilar"
        / (record.plan.content_digest.removeprefix("sha256:") + ".json")
    )
    target.unlink()

    name = publish_portable_source_plan(home, record)
    assert name.endswith(".json")
    other_project = "66666666-6666-4666-8666-666666666666"
    other_parent = home / "projeler" / other_project / "baglantilar"
    other_parent.mkdir(parents=True, mode=0o700)
    (other_parent / name).write_bytes(target.read_bytes())
    with pytest.raises(PolicyViolation, match="address or project"):
        read_portable_source_plan(home, other_project, record.plan.content_digest)


def test_bounded_source_private_entry_and_capture_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValidationFailed, match="typed absolute root"):
        BoundedContinuitySource(Path("relative"), _record().plan.recipe)
    with pytest.raises(ValidationFailed, match="typed recipe"):
        BoundedContinuitySource(tmp_path.resolve(), cast(Any, object()))

    adapter = _git_source(tmp_path)
    original_fstat = os.fstat
    head_inode = (adapter.root / ".git" / "HEAD").stat().st_ino

    def linked(fd: int) -> os.stat_result:
        info = original_fstat(fd)
        if info.st_ino == head_inode:
            values = list(info)
            values[3] = 2
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", linked)
    with pytest.raises(PolicyViolation, match="HEAD policy"):
        BoundedContinuitySource(adapter.root, adapter.recipe)


def test_bounded_source_git_head_empty_capture_and_public_type_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _git_source(tmp_path)
    with pytest.raises(ValidationFailed, match="fixed Git inputs"):
        adapter._git(("rev-parse",), input_bytes=b"x" * 8193)
    monkeypatch.setattr(adapter, "_git", lambda *_args, **_kwargs: b"bad")
    with pytest.raises(PolicyViolation, match="canonical commit lines"):
        adapter._head()

    monkeypatch.setattr(adapter, "_head", lambda: "0" * 40)
    monkeypatch.setattr(adapter, "_ignore_capture", lambda: ((), IgnoreMatcher()))
    monkeypatch.setattr(adapter, "_git", lambda *_args, **_kwargs: b"")
    monkeypatch.setattr(adapter, "_read", lambda *_args, **_kwargs: b"")
    with pytest.raises(PolicyViolation, match="empty or missing"):
        adapter._capture_once()

    with pytest.raises(ValidationFailed, match="typed store"):
        adapter.apply(cast(Any, object()), _record().plan, expected_plan_digest=SHA_A)
    with pytest.raises(ValidationFailed, match="typed operational store"):
        adapter.assert_snapshot(cast(Any, object()), SNAPSHOT)
    with pytest.raises(ValidationFailed, match="typed continuity binding"):
        adapter.probe(cast(Any, object()), cast(Any, object()))


def test_bounded_git_process_writes_stdin_and_drains_both_pipes() -> None:
    code, stdout, stderr = _bounded_git_process(
        [
            os.environ.get("PYTHON", sys.executable),
            "-c",
            "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); "
            "sys.stderr.buffer.write(b'e')",
        ],
        {"PATH": os.defpath},
        b"payload",
    )
    assert (code, stdout, stderr) == (0, b"payload", b"e")


def _app_frame(method: str, params: dict[str, object], request_id: int = 1) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _app_initialize(*, capabilities: list[str]) -> dict[str, object]:
    return _app_frame(
        "initialize",
        {
            "client_id": "coverage",
            "client_version": "1.0.0",
            "protocol_version": "1.0",
            "schema_bundle_digest": schema_bundle_digest(),
            "capabilities": capabilities,
            "experimental_methods": [],
            "replay_cursor": None,
        },
    )


def _app_ready(capabilities: list[str]) -> AppServerConnection:
    connection = AppServerConnection(InMemoryNotificationStore())
    assert connection.handle(_app_initialize(capabilities=capabilities))
    connection.handle({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    return connection


@pytest.mark.parametrize(
    "changes",
    (
        {"ingress_limit": 0},
        {"outbound_limit": 0},
        {"replay_limit": 0},
        {"max_frame_bytes": 1},
    ),
)
def test_app_server_constructor_limits_and_empty_queue(changes: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        AppServerConnection(InMemoryNotificationStore(), **cast(Any, changes))
    connection = AppServerConnection(InMemoryNotificationStore())
    connection.process_next()
    assert connection.drain_outbound() == ()


def test_app_server_closed_and_handshake_notification_edges() -> None:
    closed = AppServerConnection(InMemoryNotificationStore())
    closed.close()
    response = closed.handle(_app_frame("server/status", {}))[0]
    assert response["error"]["data"]["category"] == "policy-denied"

    missing_id = AppServerConnection(InMemoryNotificationStore())
    missing_id.enqueue({"jsonrpc": "2.0", "method": "initialize", "params": {}})
    with pytest.raises(ProtocolFault):
        missing_id.process_next()

    initialized_with_id = AppServerConnection(InMemoryNotificationStore())
    response = initialized_with_id.handle(_app_frame("initialized", {}))[0]
    assert response["error"]["data"]["category"] == "invalid-request"

    premature = AppServerConnection(InMemoryNotificationStore())
    premature.enqueue({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    with pytest.raises(ProtocolFault):
        premature.process_next()

    ready = _app_ready([])
    ready.enqueue({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    with pytest.raises(ProtocolFault):
        ready.process_next()


def test_app_server_replay_read_and_parser_fail_closed_edges() -> None:
    no_caps = _app_ready([])
    replay = no_caps.handle(_app_frame("notifications/replay", {"after_sequence": 0, "limit": 1}))[
        0
    ]
    assert replay["error"]["data"]["category"] == "policy-denied"
    read = no_caps.handle(_app_frame("project/read", {"project_id": PROJECT}))[0]
    assert read["error"]["data"]["category"] == "policy-denied"

    replay_ready = _app_ready(["replay"])
    expired = replay_ready.handle(
        _app_frame("notifications/replay", {"after_sequence": 99, "limit": 1})
    )[0]
    assert expired["error"]["data"]["category"] == "cursor-expired"

    read_ready = _app_ready(["read-status"])
    for method, params in (
        ("session/read", {"session_id": "session"}),
        ("project/read", {"project_id": "bad"}),
        ("work/read", {"work_item_id": PROJECT}),
    ):
        result = read_ready.handle(_app_frame(method, cast(dict[str, object], params)))[0]
        assert result["error"]["data"]["category"] in {"invalid-request", "policy-denied"}

    connection = AppServerConnection(InMemoryNotificationStore())
    malformed: tuple[object, ...] = (
        [],
        {"jsonrpc": "1.0", "method": "ok", "params": {}},
        {"jsonrpc": "2.0", "method": "ok", "params": []},
        {"jsonrpc": "2.0", "id": True, "method": "ok", "params": {}},
    )
    for document in malformed:
        with pytest.raises(ProtocolFault):
            connection.enqueue(document)
