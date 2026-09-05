# mypy: disable-error-code="arg-type,attr-defined,misc"
from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.integration import test_local_continuity_source_authority as authority_fixture
from tests.integration import test_macos_precompaction_supervisor as supervisor_fixture
from tests.unit import test_wp16_continuity_spool_exact_missing_coverage as spool_exact_fixture
from tests.unit import test_wp16_macos_remaining_coverage_wp05 as mac_fixture
from tests.unit import test_wp16_spool_remaining_coverage as spool_contract_fixture

from zekam.application import client_lifecycle_spool as lifecycle_spool
from zekam.application import local_continuity_v4_compaction as compaction
from zekam.application import local_continuity_v4_ingress as ingress
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure import macos_precompaction_supervisor as supervisor
from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource
from zekam.infrastructure.sqlite import local_continuity_source_authority as authority_store

pytestmark = pytest.mark.unit


def _generation() -> Any:
    generation = object.__new__(supervisor._DarwinGenerationOwner)
    adapter = object.__new__(supervisor._DarwinAuthorityAdapter)
    object.__setattr__(adapter, "_expected", mac_fixture._job())
    object.__setattr__(generation, "_adapter", adapter)
    object.__setattr__(generation, "_artifacts", object())
    object.__setattr__(generation, "_digest", digest("generation"))
    object.__setattr__(generation, "_job", mac_fixture._job())
    object.__setattr__(generation, "_seal", "final-exact-generation")
    supervisor._GENERATIONS[generation._seal] = generation
    supervisor._GENERATION_PARITY[generation._seal] = supervisor._generation_bytes(generation)
    return generation


def _result(**changes: object) -> Any:
    values: dict[str, object] = {
        "_seal": "result",
        "_stdout": b"",
        "status": "rejected",
        "failure_category": "STORAGE_UNAVAILABLE",
        "checkpoint_digest": None,
        "checkpoint_requested_event_digest": None,
        "pre_compaction_event_digest": None,
        "native_receipt_digest": None,
        "attachment_revision_digest": None,
        "ack_decision_digest": None,
        "replay": False,
        "durable_reopen_verified": False,
        "native_ack_observed": False,
        "grants_authority": False,
    }
    values.update(changes)
    value = object.__new__(compaction.PreCompactionResult)
    for name, item in values.items():
        object.__setattr__(value, name, item)
    return value


@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        ("wrong-type", "exact durable result"),
        ("decision-type", "decision body unavailable"),
        ("decision-generation", "decision body unavailable"),
        ("decision-result", "decision body unavailable"),
        ("decision-body", "result/decision mismatch"),
    ),
)
def test_supervisor_response_exact_result_and_decision_guards(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected: str
) -> None:
    monkeypatch.setattr(supervisor._DarwinGenerationOwner, "_recheck", lambda *_a: None)
    generation = _generation()
    request = mac_fixture._request()
    if mode == "wrong-type":
        result: object = object()
        decision = None
    else:
        result = _result(
            status="checkpoint-ready",
            failure_category=None,
            checkpoint_digest=digest("checkpoint"),
            checkpoint_requested_event_digest=digest("requested"),
            pre_compaction_event_digest=digest("precompact"),
            native_receipt_digest=digest("native"),
            attachment_revision_digest=digest("attachment"),
            ack_decision_digest=digest("decision"),
            durable_reopen_verified=True,
        )
        monkeypatch.setattr(compaction.PreCompactionResult, "__post_init__", lambda _self: None)
        if mode == "decision-type":
            decision = object()
        else:
            decision = object.__new__(compaction.VerifiedAckDecision)
            body = {
                "checkpoint_digest": result.checkpoint_digest,
                "checkpoint_requested_event_digest": result.checkpoint_requested_event_digest,
                "pre_compaction_event_digest": result.pre_compaction_event_digest,
                "native_receipt_digest": result.native_receipt_digest,
                "pre_compact_committed_revision_digest": result.attachment_revision_digest,
            }
            for name, item in {
                "generation_digest": (
                    digest("wrong")
                    if mode == "decision-generation"
                    else generation.generation_digest
                ),
                "decision_digest": (
                    digest("wrong") if mode == "decision-result" else result.ack_decision_digest
                ),
                "body_json": canonical_json(
                    body
                    | ({"checkpoint_digest": digest("wrong")} if mode == "decision-body" else {})
                ),
            }.items():
                object.__setattr__(decision, name, item)
            monkeypatch.setattr(compaction.VerifiedAckDecision, "__post_init__", lambda _self: None)
    with pytest.raises(PolicyViolation, match=expected):
        supervisor._response_body(generation, request, result, decision)


@pytest.mark.parametrize("category", ("STORAGE_UNAVAILABLE", "UNRECOGNIZED"))
def test_supervisor_response_failure_category_mapping(
    monkeypatch: pytest.MonkeyPatch, category: str
) -> None:
    monkeypatch.setattr(supervisor._DarwinGenerationOwner, "_recheck", lambda *_a: None)
    generation = _generation()
    result = _result(failure_category=category)
    monkeypatch.setattr(compaction.PreCompactionResult, "__post_init__", lambda _self: None)
    body = supervisor._response_body(generation, mac_fixture._request(), result, object())
    assert body["classification"] == (
        category if category == "STORAGE_UNAVAILABLE" else "RECOVERY_REQUIRED"
    )
    assert body["decision_body"] is None


def _session_result(**changes: object) -> Any:
    values: dict[str, object] = {
        "manifest_digest": digest("manifest"),
        "hydration_receipt_digest": digest("hydration"),
        "attachment_revision_digest": digest("attachment"),
        "stdout": b"ok",
        "replay": False,
    }
    values.update(changes)
    value = object.__new__(ingress.SessionStartIngressResult)
    for name, item in values.items():
        object.__setattr__(value, name, item)
    return value


def test_supervisor_session_response_complete_and_each_missing_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor._DarwinGenerationOwner, "_recheck", lambda *_a: None)
    generation = _generation()
    request = mac_fixture._request(session=True)
    monkeypatch.setattr(ingress.SessionStartIngressResult, "__post_init__", lambda _self: None)
    complete = supervisor._session_response_body(generation, request, _session_result())
    assert complete["classification"] == "hydrated"
    for field in (
        "manifest_digest",
        "hydration_receipt_digest",
        "attachment_revision_digest",
    ):
        with pytest.raises(PolicyViolation, match="complete evidence"):
            supervisor._session_response_body(generation, request, _session_result(**{field: None}))


def test_supervisor_resolved_and_session_composition_scope_and_artifact_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = _generation()
    raw = mac_fixture._request(raw=True)
    home = tmp_path / "home"
    database = home / "state" / "operational.db"
    with pytest.raises(PolicyViolation, match="server composition"):
        supervisor._resolved_precompaction(
            generation,
            raw | {"schema": "wrong"},
            (1, 1, "s", digest("a"), digest("b")),
            database,
            home,
        )
    from zekam.infrastructure.sqlite import local_continuity_v4_compaction as sqlite_compaction

    monkeypatch.setattr(
        sqlite_compaction, "resolve_existing_precompaction_binding", lambda *_a, **_k: object()
    )
    with pytest.raises(PolicyViolation, match="artifact pins unavailable"):
        supervisor._resolved_precompaction(
            generation, raw, (1, 1, "s", digest("a"), digest("b")), database, home
        )
    session = mac_fixture._request(session=True)
    with pytest.raises(PolicyViolation, match="canary scope"):
        supervisor._allocate_and_hydrate_session(
            generation,
            session | {"schema": "wrong"},
            (1, 1, "s", digest("a"), digest("b")),
            database,
            home,
            home / "plan.json",
        )


def test_supervisor_acquire_capability_true_still_refuses_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor, "DARWIN_LAUNCHD_CAPABILITY_OBSERVED", True)
    monkeypatch.setattr(supervisor, "PRODUCTION_GENERATION_ISSUED", True)
    monkeypatch.setattr(
        supervisor._DarwinAuthorityAdapter,
        "_launch_activate_socket",
        lambda *_a, **_k: (3,),
    )
    with pytest.raises(PolicyViolation, match="remains unissued"):
        supervisor._DarwinAuthorityAdapter.acquire()


def test_supervisor_observe_current_detects_live_generation_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = object.__new__(supervisor._DarwinAuthorityAdapter)
    expected = mac_fixture._job()
    object.__setattr__(adapter, "_expected", expected)
    monkeypatch.setattr(supervisor, "_listener_observation_from_fd", lambda *_a: expected.listener)
    monkeypatch.setattr(
        supervisor,
        "_process_row",
        lambda *_a, **_k: (1, expected.service_uid, "changed", Path("/bin/x")),
    )
    monkeypatch.setattr(
        supervisor, "_raw_file_digest", lambda _path: expected.service_artifact_digest
    )
    with pytest.raises(PolicyViolation, match="generation drift"):
        adapter.observe_current()


def test_supervisor_listener_family_name_and_descriptor_flag_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = SimpleNamespace(
        st_mode=stat.S_IFSOCK | 0o600,
        st_uid=os.geteuid(),
        st_dev=1,
        st_ino=2,
        st_nlink=1,
    )
    directory = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
    monkeypatch.setattr(
        supervisor.os,
        "lstat",
        lambda path: metadata if os.fsdecode(path).endswith("test.sock") else directory,
    )
    monkeypatch.setattr(supervisor.os, "fstat", lambda _fd: metadata)

    class FakeSocket:
        family = socket.AF_INET
        path_ok = False

        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def getsockopt(self, *_args: object) -> int:
            return socket.SOCK_STREAM

        def getsockname(self) -> str:
            return "/tmp/test.sock" if self.path_ok else "/tmp/wrong"

    monkeypatch.setattr(supervisor.socket, "socket", FakeSocket)
    with pytest.raises(PolicyViolation, match="family mismatch"):
        supervisor._listener_observation_from_fd("/tmp/test.sock", 3, os.geteuid())
    FakeSocket.family = socket.AF_UNIX
    with pytest.raises(PolicyViolation, match="pathname/descriptor mismatch"):
        supervisor._listener_observation_from_fd("/tmp/test.sock", 3, os.geteuid())
    FakeSocket.path_ok = True
    monkeypatch.setattr(supervisor.fcntl, "fcntl", lambda *_args: 0)
    with pytest.raises(PolicyViolation, match="descriptor flags unavailable"):
        supervisor._listener_observation_from_fd("/tmp/test.sock", 3, os.geteuid())


def test_supervisor_peer_observation_accepts_two_exact_digests() -> None:
    observed = supervisor._DarwinPeerObservation(1, 0, "start", digest("audit"), digest("artifact"))
    assert observed.audit_token_digest != observed.artifact_digest


@pytest.mark.parametrize("session", (False, True))
def test_supervisor_environment_dispatch_fails_closed_and_releases_all_pins(
    monkeypatch: pytest.MonkeyPatch,
    session: bool,
) -> None:
    nonce = ("8" if session else "7") * 64
    with (
        TemporaryDirectory(prefix="zkpc-final-", dir="/private/tmp") as directory,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener,
    ):
        root = Path(directory)
        root.chmod(0o700)
        activation = supervisor_fixture._canary(root, listener, nonce, monkeypatch)
        generation_seal = activation._generation._seal
        monkeypatch.setenv(
            "ZEKAM_PRECOMPACT_CANARY_DATABASE", str(root / "state" / "operational.db")
        )
        monkeypatch.setenv("ZEKAM_PRECOMPACT_CANARY_HOME", str(root))
        monkeypatch.delenv("ZEKAM_PRECOMPACT_CANARY_SESSION_PLAN", raising=False)
        failures: list[BaseException] = []

        def serve() -> None:
            try:
                supervisor.serve_canary_once(activation)
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=serve)
        thread.start()
        request = (
            dict(mac_fixture._request(session=True))
            if session
            else supervisor_fixture._live_request(nonce)
        )
        if session:
            _parent, uid, start, _executable = supervisor_fixture.lifecycle._process_row(
                os.getpid(), timeout=1.0
            )
            created = supervisor_fixture.time.monotonic_ns()
            request.update(
                attempt_nonce=nonce,
                client_pid=os.getpid(),
                client_uid=uid,
                client_start_token=start,
                created_monotonic_ns=created,
                deadline_monotonic_ns=created + supervisor_fixture.client.TOTAL_DEADLINE_NS,
            )
        with pytest.raises((ConnectionError, OSError, PolicyViolation)):
            supervisor_fixture.client.canary_exchange(
                root / "canary.sock",
                request,
                deadline_ns=supervisor_fixture.time.monotonic_ns()
                + supervisor_fixture.client.TOTAL_DEADLINE_NS,
            )
        thread.join(3)
        assert not thread.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], PolicyViolation)
        assert generation_seal not in supervisor._GENERATIONS


@pytest.fixture
def source_authority(tmp_path: Path) -> dict[str, Any]:
    factory = cast(Callable[[Path], dict[str, Any]], authority_fixture.authority.__wrapped__)
    return factory(tmp_path)


def _authority_execute(
    store: authority_store.SQLiteLocalSourceAuthority,
    context: dict[str, Any],
    *,
    previous: str | None = None,
    rebind: bool = False,
) -> Any:
    return authority_fixture._execute(
        store,
        context,
        previous_revision_digest=previous,
        rebind=rebind,
    )


def _authority_candidate(
    store: authority_store.SQLiteLocalSourceAuthority, revision_digest: str
) -> Any:
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "select r.*,cast(r.body_json as blob) as body_blob,h.previous_generation,"
            "h.previous_revision_digest as head_previous from local_source_binding_revision r "
            "join local_source_binding_head h on h.device_id=r.device_id "
            "and h.source_binding_id=r.source_binding_id and h.generation=r.generation "
            "and h.revision_digest=r.revision_digest where r.revision_digest=?",
            (revision_digest,),
        ).fetchone()
        assert row is not None
        return authority_store._validated_candidate(row)


@pytest.mark.parametrize(
    ("case", "match"),
    (
        ("record", "typed bind request"),
        ("source", "concrete source"),
        ("command", "command mismatch"),
        ("device-type", "bounded device"),
        ("device-empty", "bounded device"),
        ("root-type", "absolute source root"),
        ("root-parent", "absolute source root"),
        ("mode", "predecessor mode mismatch"),
    ),
)
def test_source_authority_rejects_typed_scope_and_capability_confusion(
    source_authority: dict[str, Any], case: str, match: str
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    source: object = BoundedContinuitySource(source_authority["root"], source_authority["recipe"])
    record: object = source_authority["record"]
    device: object = "macbook"
    root: object = source_authority["root"]
    previous: str | None = None
    rebind: object = False
    command = ("continuity", "source-bind")
    if case == "record":
        record = object()
    elif case == "source":
        source = object()
    elif case == "command":
        command = ("continuity", "source-rebind")
    elif case == "device-type":
        device = None
    elif case == "device-empty":
        device = ""
    elif case == "root-type":
        root = str(source_authority["root"])
    elif case == "root-parent":
        root = source_authority["root"] / ".." / "source"
    else:
        previous = digest("predecessor")
    capability = object.__new__(authority_fixture._GateASourceCapability)
    with authority_fixture._GATE_A_LOCK:
        authority_fixture._GATE_A_STATES[capability] = (command, "INPUTS_VALID")
    with pytest.raises((PolicyViolation, authority_fixture.ValidationFailed), match=match):
        store.execute(
            capability=capability,
            record=record,
            source=source,
            device_id=device,
            root=root,
            previous_revision_digest=previous,
            rebind=rebind,
        )


class _CandidateRow:
    def __init__(self, row: sqlite3.Row, body: bytes) -> None:
        self._values = tuple(row)
        self._mapping = {key: row[key] for key in row.keys()}  # noqa: SIM118
        self._mapping["body_blob"] = body

    def __getitem__(self, key: object) -> object:
        if isinstance(key, str):
            return self._mapping[key]
        assert isinstance(key, (int, slice))
        return self._values[key]

    def __iter__(self) -> Any:
        return iter(self._values)


@pytest.mark.parametrize("corruption", ("nested-type", "extra-field"))
def test_source_authority_revision_body_corruption_is_rejected(
    source_authority: dict[str, Any], corruption: str
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    result = _authority_execute(store, source_authority)
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "select r.*,cast(r.body_json as blob) as body_blob,h.previous_generation,"
            "h.previous_revision_digest as head_previous from local_source_binding_revision r "
            "join local_source_binding_head h on h.device_id=r.device_id "
            "and h.source_binding_id=r.source_binding_id and h.generation=r.generation "
            "and h.revision_digest=r.revision_digest where r.revision_digest=?",
            (result.revision_digest,),
        ).fetchone()
        assert row is not None
        body = json.loads(bytes(row["body_blob"]))
        if corruption == "nested-type":
            body["operational_identity"] = []
        else:
            body["unexpected"] = True
        forged = _CandidateRow(row, json.dumps(body, separators=(",", ":")).encode())
    with pytest.raises(PolicyViolation, match="revision body drift"):
        authority_store._validated_candidate(forged)


def test_source_authority_classify_missing_and_forged_candidate_returns_none(
    source_authority: dict[str, Any],
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    result = _authority_execute(store, source_authority)
    candidate = _authority_candidate(store, result.revision_digest)
    absent = replace(candidate, device_id="other-device")
    assert store._classify(absent) is None
    forged = object.__new__(type(candidate))
    for field in candidate.__dataclass_fields__:
        object.__setattr__(forged, field, getattr(candidate, field))
    object.__setattr__(forged, "device_id", "forged-device")
    assert store._classify(forged) is None


@pytest.mark.parametrize("mismatch_at", (1, 2, 3))
def test_source_authority_capture_fences_reject_each_late_source_change(
    source_authority: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    mismatch_at: int,
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    original = BoundedContinuitySource.capture
    count = 0

    def capture(source: BoundedContinuitySource) -> object:
        nonlocal count
        count += 1
        if count == mismatch_at:
            return object()
        return original(source)

    monkeypatch.setattr(BoundedContinuitySource, "capture", capture)
    expected = {
        1: "first capture mismatch",
        2: "second capture mismatch",
        3: "precommit capture mismatch",
    }[mismatch_at]
    with pytest.raises(PolicyViolation, match=expected):
        _authority_execute(store, source_authority)
    if mismatch_at < 3:
        assert (
            not store.path.exists()
            or authority_fixture._rows(store.path)["local_source_binding_revision"] == 0
        )


def test_source_authority_postcommit_capture_drift_preserves_durable_revision(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    original = BoundedContinuitySource.capture
    count = 0

    def capture(source: BoundedContinuitySource) -> object:
        nonlocal count
        count += 1
        return object() if count == 4 else original(source)

    monkeypatch.setattr(BoundedContinuitySource, "capture", capture)
    with pytest.raises(PolicyViolation, match="durable verification failed"):
        _authority_execute(store, source_authority)
    assert authority_fixture._rows(store.path)["local_source_binding_revision"] == 1


def test_source_authority_commit_unknown_recovers_only_exact_durable_candidate(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    original_sync = store._sync
    calls = 0

    def uncertain_sync() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("post-commit uncertainty")
        original_sync()

    monkeypatch.setattr(store, "_sync", uncertain_sync)
    result = _authority_execute(store, source_authority)
    assert result.generation == 1
    assert calls == 3


def test_source_authority_replay_classification_failure_is_terminal(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    first = _authority_execute(store, source_authority)
    monkeypatch.setattr(store, "_classify", lambda _candidate: None)
    with pytest.raises(PolicyViolation, match="replay verification failed"):
        _authority_execute(store, source_authority)
    assert authority_fixture._rows(store.path)["local_source_binding_revision"] == 1
    assert first.generation == 1


def test_source_authority_operational_fd_identity_failure_closes_descriptor(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    monkeypatch.setattr(authority_store, "_held_identity", lambda _fd: object())
    with pytest.raises(PolicyViolation, match="Operational identity changed"):
        store._operational_snapshot(source_authority["record"], fenced=False)


def test_source_authority_operational_schema_and_snapshot_parity_failures(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    monkeypatch.setattr(authority_store, "_validate_connection", lambda _db: 3)
    with pytest.raises(PolicyViolation, match="requires dormant V4"):
        store._operational_snapshot(source_authority["record"], fenced=False)
    monkeypatch.setattr(authority_store, "_validate_connection", lambda _db: 4)
    wrong = replace(
        source_authority["record"], source_snapshot_id=str(source_authority["recipe"].project_id)
    )
    with pytest.raises(PolicyViolation, match="snapshot mismatch"):
        store._operational_snapshot(wrong, fenced=False)


def test_source_authority_side_identity_changes_after_preflight(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    original = authority_store._held_identity
    calls = 0

    def changed(fd: int) -> object:
        nonlocal calls
        calls += 1
        return object() if calls == 2 else original(fd)

    monkeypatch.setattr(authority_store, "_held_identity", changed)
    with pytest.raises(PolicyViolation, match="file changed"):
        _authority_execute(store, source_authority)


class _OneRow:
    def __init__(self, value: object) -> None:
        self.value = value

    def fetchone(self) -> tuple[object]:
        return (self.value,)


@pytest.mark.parametrize(
    ("marker", "value", "match"),
    (
        ("SIDE_PAGE_PRE", 2049, "capacity exceeded"),
        ("SIDE_PAGE_PROJECTED", 2048, "projected capacity exceeded"),
        ("SIDE_PAGE_POST", 2049, "capacity exceeded"),
    ),
)
def test_source_authority_capacity_guards_roll_back_without_partial_revision(
    source_authority: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
    value: int,
    match: str,
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    original = authority_store._GuardedSQLite.execute

    def execute(db: Any, operation: str, sql: str, parameters: object = ()) -> Any:
        if operation == marker:
            return _OneRow(value)
        return original(db, operation, sql, parameters)

    monkeypatch.setattr(authority_store._GuardedSQLite, "execute", execute)
    with pytest.raises(PolicyViolation, match=match):
        _authority_execute(store, source_authority)
    assert authority_fixture._rows(store.path)["local_source_binding_revision"] == 0


def test_source_authority_locked_baseline_and_fenced_snapshot_drift_are_rejected(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    original = authority_store._source_authority_baseline
    calls = 0

    def baseline(db: Any, state: object = None) -> object:
        nonlocal calls
        calls += 1
        value = original(db, state) if state is not None else original(db)
        return value if calls == 1 else ("drift",)

    monkeypatch.setattr(authority_store, "_source_authority_baseline", baseline)
    with pytest.raises(PolicyViolation, match="B0 drift"):
        _authority_execute(store, source_authority)

    monkeypatch.undo()
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    original_snapshot = store._operational_snapshot
    snapshot_calls = 0

    def snapshot(record: Any, *, fenced: bool) -> Any:
        nonlocal snapshot_calls
        snapshot_calls += 1
        descriptor, db, found = original_snapshot(record, fenced=fenced)
        return descriptor, db, found if snapshot_calls == 1 else (*found, "drift")

    monkeypatch.setattr(store, "_operational_snapshot", snapshot)
    with pytest.raises(PolicyViolation, match="snapshot changed"):
        _authority_execute(store, source_authority)


def test_source_authority_precommit_portable_evidence_drift_rolls_back(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    monkeypatch.setattr(authority_store, "read_portable_source_plan", lambda *_a: object())
    with pytest.raises(PolicyViolation, match="precommit evidence drift"):
        _authority_execute(store, source_authority)
    assert authority_fixture._rows(store.path)["local_source_binding_revision"] == 0


def test_source_authority_replay_evidence_drift_is_not_acknowledged(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    _authority_execute(store, source_authority)
    monkeypatch.setattr(authority_store, "read_portable_source_plan", lambda *_a: object())
    with pytest.raises(PolicyViolation, match="replay evidence drift"):
        _authority_execute(store, source_authority)
    assert authority_fixture._rows(store.path)["local_source_binding_revision"] == 1


def test_source_authority_unclassifiable_commit_unknown_requires_attention(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    original_sync = store._sync
    calls = 0

    def uncertain_sync() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("post-commit uncertainty")
        original_sync()

    monkeypatch.setattr(store, "_sync", uncertain_sync)
    monkeypatch.setattr(store, "_classify", lambda _candidate: None)
    with pytest.raises(PolicyViolation, match="requires attention"):
        _authority_execute(store, source_authority)
    assert authority_fixture._rows(store.path)["local_source_binding_revision"] == 1


def test_source_authority_recovery_rejects_changed_portable_evidence(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    original_sync = store._sync
    original_read = authority_store.read_portable_source_plan
    sync_calls = 0
    read_calls = 0

    def uncertain_sync() -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            raise OSError("post-commit uncertainty")
        original_sync()

    def changed_read(*args: object) -> object:
        nonlocal read_calls
        read_calls += 1
        return object() if read_calls == 2 else original_read(*args)

    monkeypatch.setattr(store, "_sync", uncertain_sync)
    monkeypatch.setattr(authority_store, "read_portable_source_plan", changed_read)
    with pytest.raises(PolicyViolation, match="recovered evidence drift"):
        _authority_execute(store, source_authority)
    assert authority_fixture._rows(store.path)["local_source_binding_revision"] == 1


def test_spool_pending_repairs_existing_queue_checkpoint_before_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool, entry = spool_exact_fixture._spool(tmp_path)
    called = 0
    original = spool._advance_drain_cursor

    def advance() -> None:
        nonlocal called
        called += 1
        original()

    monkeypatch.setattr(spool, "_advance_drain_cursor", advance)
    assert spool.pending() == (entry,)
    assert called == 1


def test_spool_predecessor_chain_and_terminal_state_are_both_required(tmp_path: Path) -> None:
    spool, first = spool_exact_fixture._spool(tmp_path)
    second = spool.stage(
        spool_exact_fixture._observation("PreCompact"),
        delivery_id=digest("final-exact-second"),
        occurred_at=spool_exact_fixture.NOW,
    )
    spool._entry_path(first.entry_digest).unlink()
    with pytest.raises(PolicyViolation, match="chain binding mismatch"):
        spool.record_predecessor_manual_review(second.entry_digest)

    spool2, _first2 = spool_exact_fixture._spool(tmp_path / "state")
    second2 = spool2.stage(
        spool_exact_fixture._observation("PreCompact"),
        delivery_id=digest("final-exact-state-second"),
        occurred_at=spool_exact_fixture.NOW,
    )
    with pytest.raises(PolicyViolation, match="terminal manual-review"):
        spool2.record_predecessor_manual_review(second2.entry_digest)


def test_spool_cursor_document_requires_attempt_state_and_source_entry(tmp_path: Path) -> None:
    spool, entry = spool_exact_fixture._spool(tmp_path)
    with pytest.raises(PolicyViolation, match="exact attempt-state"):
        spool._cursor_record_document(
            queue_sequence=1,
            previous_entry_digest=None,
            previous_cursor_digest=None,
            previous_acknowledged_count=0,
            previous_manual_review_count=0,
        )
    spool._entry_path(entry.entry_digest).unlink()
    with pytest.raises(PolicyViolation, match="source entry"):
        spool._cursor_record_document(
            queue_sequence=1,
            previous_entry_digest=None,
            previous_cursor_digest=None,
            previous_acknowledged_count=0,
            previous_manual_review_count=0,
        )


def test_spool_predecessor_terminal_validators_reject_wrong_outcome_and_zero_failure(
    tmp_path: Path,
) -> None:
    _spool, entry, artifacts = spool_contract_fixture._artifact_set(tmp_path)
    predecessor_entry = digest("final-predecessor-entry")
    predecessor_state = digest("final-predecessor-state")
    evidence = digest(
        {
            "schema": "zekam-client-lifecycle-predecessor-block/v1",
            "entry_digest": entry.entry_digest,
            "predecessor_entry_digest": predecessor_entry,
            "predecessor_attempt_state_digest": predecessor_state,
            "grants_authority": False,
        }
    )
    attempt = dict(artifacts["attempt"])
    attempt.update(
        outcome="failed",
        evidence_digest=evidence,
        attempt_number=2,
        failure_count=2,
        disposition="manual-review",
        terminal_reason="predecessor-manual-review",
        predecessor_entry_digest=predecessor_entry,
        predecessor_attempt_state_digest=predecessor_state,
    )
    attempt["retry_key"] = digest(
        {
            "entry_digest": entry.entry_digest,
            "outcome": "failed",
            "evidence_digest": evidence,
            "terminal_reason": "predecessor-manual-review",
        }
    )
    spool_contract_fixture._redigest(attempt, "attempt_digest")
    with pytest.raises(PolicyViolation, match="manual-review sonucu"):
        lifecycle_spool._validate_attempt(attempt, entry_digest=entry.entry_digest)

    state = dict(artifacts["attempt_state"])
    state.update(
        attempt_count=1,
        failure_count=0,
        disposition="manual-review",
        terminal_reason="predecessor-manual-review",
        predecessor_entry_digest=predecessor_entry,
        predecessor_attempt_state_digest=predecessor_state,
    )
    spool_contract_fixture._redigest(state, "state_digest")
    with pytest.raises(PolicyViolation, match="predecessor attempt state"):
        lifecycle_spool._validate_attempt_state(state, entry_digest=entry.entry_digest)


def test_spool_parent_fd_identity_size_and_atomic_collision_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    real_lstat = lifecycle_spool.os.lstat
    regular = real_lstat(target)
    bad_anchor = SimpleNamespace(st_mode=stat.S_IFREG)
    monkeypatch.setattr(lifecycle_spool.os, "lstat", lambda path: bad_anchor)
    with pytest.raises(PolicyViolation, match="anchor"):
        lifecycle_spool._assert_safe_parent_chain(target)
    monkeypatch.setattr(lifecycle_spool.os, "lstat", real_lstat)
    monkeypatch.setattr(lifecycle_spool, "_safe_directory_exists", lambda _path: False)
    with pytest.raises(PolicyViolation, match="dizini olusturulamadi"):
        lifecycle_spool._ensure_safe_directory(tmp_path / "new")
    monkeypatch.undo()

    monkeypatch.setattr(
        lifecycle_spool.os,
        "fstat",
        lambda _fd: SimpleNamespace(st_mode=stat.S_IFDIR, st_size=0, st_ino=1, st_dev=1),
    )
    with pytest.raises(PolicyViolation, match="opened target"):
        lifecycle_spool._assert_fd_matches_path(1, target, max_bytes=100)
    monkeypatch.setattr(lifecycle_spool.os, "fstat", lambda _fd: regular)
    monkeypatch.setattr(
        lifecycle_spool.os,
        "lstat",
        lambda _path: SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_size=regular.st_size,
            st_ino=regular.st_ino + 1,
            st_dev=regular.st_dev,
        ),
    )
    with pytest.raises(PolicyViolation, match="identity drift"):
        lifecycle_spool._assert_fd_matches_path(1, target, max_bytes=100)


def test_spool_read_limit_concurrent_collision_and_lock_size_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "read.json"
    target.write_bytes(b"ab")
    monkeypatch.setattr(lifecycle_spool, "_safe_regular_file_exists", lambda *_a, **_k: True)
    chunks = iter((b"ab", b""))
    monkeypatch.setattr(lifecycle_spool.os, "open", lambda *_a, **_k: 41)
    monkeypatch.setattr(lifecycle_spool.os, "close", lambda _fd: None)
    monkeypatch.setattr(lifecycle_spool, "_assert_fd_matches_path", lambda *_a, **_k: object())
    monkeypatch.setattr(lifecycle_spool.os, "read", lambda *_a, **_k: next(chunks))
    monkeypatch.setattr(lifecycle_spool, "MAX_SPOOL_DOCUMENT_BYTES", 1)
    with pytest.raises(PolicyViolation, match="boyut"):
        lifecycle_spool._read_bounded_bytes(target)
    monkeypatch.undo()

    collision = tmp_path / "collision.json"
    collision.write_text('{"other":true}\n', encoding="utf-8")
    lock = tmp_path / "bad.lock"
    lock.write_bytes(b"xx")
    original_safe = lifecycle_spool._safe_regular_file_exists
    checks = 0

    def raced(path: Path, **kwargs: object) -> bool:
        nonlocal checks
        if path == collision:
            checks += 1
            return checks > 1
        if path == lock:
            return True
        return original_safe(path, **kwargs)

    monkeypatch.setattr(lifecycle_spool, "_safe_regular_file_exists", raced)
    monkeypatch.setattr(
        lifecycle_spool,
        "_link_immutable_no_follow",
        lambda *_a: (_ for _ in ()).throw(FileExistsError()),
    )
    with pytest.raises(lifecycle_spool.ConcurrencyConflict, match="concurrent immutable"):
        lifecycle_spool._write_immutable_json(collision, {"expected": True})

    monkeypatch.setattr(lifecycle_spool, "_assert_fd_matches_path", lambda *_a, **_k: object())
    with (
        pytest.raises(PolicyViolation, match="lock boyutu"),
        lifecycle_spool._exclusive_lock(lock),
    ):
        pass


def test_spool_lock_failure_honors_deadline_on_each_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    class Deadline:
        calls = 0

        def remaining_seconds(self) -> float:
            self.calls += 1
            return 0.0

    deadline = Deadline()
    monkeypatch.setattr(lifecycle_spool, "LOCK_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(lifecycle_spool.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(fcntl, "flock", lambda *_a: (_ for _ in ()).throw(OSError("busy")))
    with (
        pytest.raises(lifecycle_spool.ConcurrencyConflict),
        lifecycle_spool._exclusive_lock(tmp_path / "deadline.lock", deadline=deadline),
    ):
        pass
    assert deadline.calls == 2


def test_spool_receipt_runtime_binding_drift_and_precompact_binding_absence(
    tmp_path: Path,
) -> None:
    spool, entry = spool_exact_fixture._spool(tmp_path)
    event = lifecycle_spool.canonical_lifecycle_event(
        entry,
        client_instance_id=spool.client_instance_id(),
        previous_canonical_event_digest=None,
    )
    common = {
        "event_id": spool_contract_fixture.UUID(spool_contract_fixture.UUIDS[0]),
        "local_event_digest": event["event_digest"],
        "canonical_digest": digest("canonical"),
    }
    first = SimpleNamespace(
        **common,
        compaction_outbox_id=spool_contract_fixture.UUID(spool_contract_fixture.UUIDS[1]),
        compaction_payload_digest=digest("runtime"),
    )
    lookup = SimpleNamespace(
        **common,
        compaction_outbox_id=None,
        compaction_payload_digest=None,
    )
    with pytest.raises(PolicyViolation, match="runtime binding lookup drift"):
        lifecycle_spool.CanonicalLifecycleReceipt.verified(entry, event, first, lookup)

    precompact = replace(entry, internal_event_type="pre_compaction")
    receipt = lifecycle_spool.CanonicalLifecycleReceipt(
        entry.entry_digest,
        digest("event"),
        spool_contract_fixture.UUID(spool_contract_fixture.UUIDS[0]),
        digest("ack"),
        digest("lookup"),
        None,
        None,
        {},
        False,
    )
    with pytest.raises(PolicyViolation, match="runtime binding eksik"):
        receipt.assert_binding(precompact)


def test_spool_empty_ack_attempt_census_and_missing_predecessor_receipt(
    tmp_path: Path,
) -> None:
    spool, first = spool_exact_fixture._spool(tmp_path)
    assert spool._verified_ack_entry_digests([first]) == frozenset()
    assert spool._verified_attempt_count([first]) == 0
    second = spool.stage(
        spool_exact_fixture._observation("PreCompact"),
        delivery_id=digest("missing-predecessor"),
        occurred_at=spool_exact_fixture.NOW,
    )
    with pytest.raises(PolicyViolation, match="predecessor receipt"):
        spool.previous_canonical_event_digest(second)


def test_spool_status_rejects_delivery_parity_after_valid_delivery_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool, entry = spool_exact_fixture._spool(tmp_path)
    original_read = lifecycle_spool._read_json

    def changed(path: Path) -> Any:
        document = original_read(path)
        if path == spool._delivery_path(entry.delivery_id):
            return dict(document, queue_sequence=2)
        return document

    monkeypatch.setattr(lifecycle_spool, "_read_json", changed)
    monkeypatch.setattr(lifecycle_spool, "_validate_delivery_ref", lambda *_a, **_k: None)
    with pytest.raises(PolicyViolation, match="status delivery parity"):
        spool.status()


def test_bootstrap_projection_refuses_job_without_work_identity() -> None:
    from zekam.application import client_runtime_bootstrap as runtime_bootstrap

    service = object.__new__(runtime_bootstrap.ClaimedLifecycleBootstrapService)
    job = SimpleNamespace(work_item_id=None)
    facts = (1, "active", digest("record"), "git:revision", None, digest("migration"), None)
    with pytest.raises(PolicyViolation, match="projection Work identity"):
        service._store_projection(
            job=job,
            facts=facts,
            source_digest=digest("source"),
            now=spool_exact_fixture.NOW,
        )


def _replace_authority_meta(
    path: Path, *, schema_digest: str | None = None, local_instance_id: str | None = None
) -> None:
    with sqlite3.connect(path) as db:
        trigger = db.execute(
            "select sql from sqlite_schema where name='local_source_authority_meta_no_update'"
        ).fetchone()
        assert trigger is not None
        db.execute("drop trigger local_source_authority_meta_no_update")
        if schema_digest is not None:
            db.execute("update local_source_authority_meta set schema_digest=?", (schema_digest,))
        if local_instance_id is not None:
            db.execute(
                "update local_source_authority_meta set local_instance_id=?", (local_instance_id,)
            )
        db.execute(str(trigger[0]))


@pytest.mark.parametrize("corruption", ("schema", "uuid-version"))
def test_source_authority_validate_rejects_metadata_and_uuid_semantic_drift(
    source_authority: dict[str, Any], corruption: str
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    store._bootstrap(authority_store._source_authority_now())
    if corruption == "schema":
        _replace_authority_meta(store.path, schema_digest=digest("wrong-schema"))
        expected = "metadata drift"
    else:
        _replace_authority_meta(
            store.path, local_instance_id="00000000-0000-1000-8000-000000000001"
        )
        expected = "metadata drift"
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        with pytest.raises(PolicyViolation, match=expected):
            authority_store._validate(db)


class _DatabaseIdentityDrift:
    def __init__(self, database: sqlite3.Connection) -> None:
        self.database = database

    def execute(self, sql: str, parameters: object = ()) -> Any:
        if sql == "pragma database_list":
            return SimpleNamespace(fetchall=lambda: [])
        return self.database.execute(sql, parameters)


def test_source_authority_validate_rejects_missing_physical_database_identity(
    source_authority: dict[str, Any],
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    store._bootstrap(authority_store._source_authority_now())
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        with pytest.raises(PolicyViolation, match="database identity drift"):
            authority_store._validate(_DatabaseIdentityDrift(db))


def test_source_authority_bootstrap_rejects_nonempty_residue(
    source_authority: dict[str, Any],
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    residue = store.path.parent / ".source-authority.sqlite3.bootstrap-residue"
    residue.write_bytes(b"partial")
    residue.chmod(0o600)
    with pytest.raises(PolicyViolation, match="bootstrap residue"):
        store._bootstrap(authority_store._source_authority_now())
    assert not store.path.exists()


def test_source_authority_bootstrap_wraps_short_write_and_leaves_no_sidecar(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    monkeypatch.setattr(
        authority_store.os, "write", lambda *_a: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(PolicyViolation, match="bootstrap failed"):
        store._bootstrap(authority_store._source_authority_now())
    assert not store.path.exists()


def test_spool_empty_pending_and_older_orphan_attempt_replay_are_stable(tmp_path: Path) -> None:
    empty = lifecycle_spool.ClientLifecycleSpool(tmp_path / "empty", client_id="codex")
    assert empty.pending() == ()

    spool, entry = spool_exact_fixture._spool(tmp_path / "orphan")
    first = spool.record_attempt(
        entry.entry_digest,
        outcome="failed",
        evidence_digest=digest("older-attempt"),
        attempted_at=spool_exact_fixture.NOW,
    )
    spool.record_attempt(
        entry.entry_digest,
        outcome="failed",
        evidence_digest=digest("newer-attempt"),
        attempted_at=spool_exact_fixture.NOW,
    )
    assert (
        spool.record_attempt(
            entry.entry_digest,
            outcome="failed",
            evidence_digest=digest("older-attempt"),
            attempted_at=spool_exact_fixture.NOW,
        )
        == first
    )


def test_spool_terminal_child_refuses_late_predecessor_dependency_result(tmp_path: Path) -> None:
    spool, first = spool_exact_fixture._spool(tmp_path)
    for index in range(lifecycle_spool.MAX_REPLAY_FAILURES):
        spool.record_attempt(
            first.entry_digest,
            outcome="failed",
            evidence_digest=digest(f"parent-terminal-{index}"),
            attempted_at=spool_exact_fixture.NOW,
        )
    second = spool.stage(
        spool_exact_fixture._observation("PreCompact"),
        delivery_id=digest("terminal-child"),
        occurred_at=spool_exact_fixture.NOW,
    )
    spool.record_attempt(
        second.entry_digest,
        outcome="completed",
        evidence_digest=digest("completed-child"),
        attempted_at=spool_exact_fixture.NOW,
    )
    with pytest.raises(PolicyViolation, match="terminal attempt-state"):
        spool.record_predecessor_manual_review(second.entry_digest)


def test_spool_replay_terminalizes_child_of_manual_review_without_delivery(
    tmp_path: Path,
) -> None:
    spool, first = spool_exact_fixture._spool(tmp_path)
    for index in range(lifecycle_spool.MAX_REPLAY_FAILURES):
        spool.record_attempt(
            first.entry_digest,
            outcome="failed",
            evidence_digest=digest(f"blocked-parent-{index}"),
            attempted_at=spool_exact_fixture.NOW,
        )
    second = spool.stage(
        spool_exact_fixture._observation("PreCompact"),
        delivery_id=digest("blocked-child"),
        occurred_at=spool_exact_fixture.NOW,
    )
    result = lifecycle_spool.replay_pending(
        spool,
        deliver=lambda _entry: (_ for _ in ()).throw(AssertionError("must not deliver")),
        attempted_at=spool_exact_fixture.NOW,
    )
    assert len(result) == 1
    assert result[0].entry_digest == second.entry_digest
    assert result[0].outcome == "recovery-required"


def test_spool_postgres_drain_refuses_client_instance_substitution(tmp_path: Path) -> None:
    spool, _entry = spool_exact_fixture._spool(tmp_path)
    spool.client_instance_id()
    with pytest.raises(PolicyViolation, match="client instance binding mismatch"):
        lifecycle_spool.drain_to_postgres(
            spool,
            client_instance_id="codex-00000000-0000-4000-8000-000000000000",
            continuity_admission=object(),
            attempted_at=spool_exact_fixture.NOW,
        )


def test_supervisor_resolved_precompaction_uses_exact_pins_and_server_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zekam.infrastructure.clients import codex_macos_0151_lifecycle as lifecycle
    from zekam.infrastructure.sqlite import local_continuity_v4_compaction as sqlite_compaction

    nonce = "9" * 64
    with (
        TemporaryDirectory(prefix="zkpc-pins-", dir="/private/tmp") as directory,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener,
    ):
        root = Path(directory)
        root.chmod(0o700)
        activation = supervisor_fixture._canary(root, listener, nonce, monkeypatch)
        generation = activation._generation
        resolved = object()
        manager = object()

        class Writer:
            process_manager: object | None = None

            def pre_compaction_with_decision(self, _event: object) -> tuple[str, str]:
                return "result", "decision"

        writer = Writer()
        monkeypatch.setattr(
            sqlite_compaction, "resolve_existing_precompaction_binding", lambda *_a, **_k: resolved
        )
        monkeypatch.setattr(
            lifecycle, "_issue_peer_bound_process_manager", lambda *_a, **_k: manager
        )
        monkeypatch.setattr(
            sqlite_compaction,
            "rollover_existing_precompaction_process",
            lambda *_a, **_k: resolved,
        )
        monkeypatch.setattr(
            sqlite_compaction, "resolved_precompaction_writer", lambda *_a, **_k: writer
        )
        home = root / "home"
        request = mac_fixture._request(raw=True)
        result = supervisor._resolved_precompaction(
            generation,
            request,
            (1, 1, "start", digest("artifact"), digest("audit")),
            home / "state" / "operational.db",
            home,
        )
        assert result == ("result", "decision")
        assert writer.process_manager is manager
        generation._artifacts.close()


def test_supervisor_timeout_cleanup_accepts_absent_connection_and_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "a" * 64
    with (
        TemporaryDirectory(prefix="zkpc-empty-", dir="/private/tmp") as directory,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener,
    ):
        root = Path(directory)
        root.chmod(0o700)
        activation = supervisor_fixture._canary(root, listener, nonce, monkeypatch)
        generation = activation._generation
        generation._artifacts.close()
        object.__setattr__(generation, "_artifacts", None)
        supervisor._GENERATION_PARITY[generation._seal] = supervisor._generation_bytes(generation)
        with pytest.raises(TimeoutError):
            supervisor.serve_canary_once(activation, timeout_seconds=0.05)
        assert generation._seal not in supervisor._GENERATIONS


def test_source_authority_bootstrap_removes_exact_empty_residue(
    source_authority: dict[str, Any],
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    residue = store.path.parent / ".source-authority.sqlite3.bootstrap-empty"
    residue.touch(mode=0o600)
    store._bootstrap(authority_store._source_authority_now())
    assert store.path.is_file()
    assert not residue.exists()


def test_source_authority_bootstrap_does_not_relabel_programming_failure(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    monkeypatch.setattr(
        authority_store.os,
        "write",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("programming-failure")),
    )
    with pytest.raises(RuntimeError, match="programming-failure"):
        store._bootstrap(authority_store._source_authority_now())
    assert not store.path.exists()


def test_source_authority_second_fresh_bind_detects_existing_head(
    source_authority: dict[str, Any],
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    _authority_execute(store, source_authority)
    original_mode = source_authority["root"].stat().st_mode & 0o777
    source_authority["root"].chmod(0o700 if original_mode != 0o700 else 0o755)
    with pytest.raises(PolicyViolation, match="already exists"):
        _authority_execute(store, source_authority)
    assert authority_fixture._rows(store.path)["local_source_binding_revision"] == 1


def test_source_authority_concurrent_latest_head_disappearance_rolls_back(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    original = authority_store._GuardedSQLite.execute

    def execute(db: Any, operation: str, sql: str, parameters: object = ()) -> Any:
        if operation == "SIDE_LATEST":
            return SimpleNamespace(fetchone=lambda: (1, digest("impossible-head")))
        return original(db, operation, sql, parameters)

    monkeypatch.setattr(authority_store._GuardedSQLite, "execute", execute)
    with pytest.raises(PolicyViolation, match="concurrent head drift"):
        _authority_execute(store, source_authority)
    assert authority_fixture._rows(store.path)["local_source_binding_revision"] == 0


def test_source_authority_commit_identity_drift_is_terminal_but_durable(
    source_authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    committed = False
    original_commit = authority_store._GuardedSQLite.commit
    original_held = authority_store._held_identity

    def commit(db: Any) -> None:
        nonlocal committed
        original_commit(db)
        committed = True

    def held(fd: int) -> object:
        return object() if committed else original_held(fd)

    monkeypatch.setattr(authority_store._GuardedSQLite, "commit", commit)
    monkeypatch.setattr(authority_store, "_held_identity", held)
    with pytest.raises(PolicyViolation, match="commit identity drift"):
        _authority_execute(store, source_authority)
    assert authority_fixture._rows(store.path)["local_source_binding_revision"] == 1


def test_source_authority_classify_rejects_same_digest_forged_values(
    source_authority: dict[str, Any],
) -> None:
    store = authority_store.SQLiteLocalSourceAuthority(
        source_authority["home"], source_authority["path"]
    )
    result = _authority_execute(store, source_authority)
    candidate = _authority_candidate(store, result.revision_digest)

    class Forged:
        revision_digest = result.revision_digest
        device_id = "forged-device"

        def body(self) -> dict[str, object]:
            return cast(dict[str, object], candidate.body())

        def __getattr__(self, name: str) -> object:
            return getattr(candidate, name)

    forged = Forged()
    assert store._classify(forged) is None
