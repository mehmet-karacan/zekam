"""Independent existing-session composition and exact local CLI admission gates."""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity_bridge_close import _stage
from tests.unit.test_local_continuity_environment import environment as environment
from tests.unit.test_local_continuity_startup import ROOT, SOURCE_REF, _request, _stage_start
from tests.unit.test_local_startup_composition import composition as composition
from typer.testing import CliRunner

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.home import HomeLayout
from zekam.application.local_continuity_close import CloseSummary
from zekam.application.mutation_admission import (
    DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY,
    MutationAdmissionExemption,
    assert_local_effect_admission,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed, ZekamError
from zekam.infrastructure.local_continuity_composition import (
    LocalContinuityArguments,
    LocalContinuityRuntime,
)
from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource
from zekam.infrastructure.sqlite import local_runtime as runtime_module
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest
from zekam.infrastructure.sqlite.operational_schema import status as operational_schema_status
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("leaf", ["drain", "hydrate", "checkpoint", "freeze", "close-tick"])
def test_only_exact_continuity_mutating_leaves_have_local_admission(leaf: str) -> None:
    admission = assert_local_effect_admission(("continuity", "local", leaf))
    assert admission.command_path == ("continuity", "local", leaf)
    assert admission.mutating is True
    assert admission.exemption is MutationAdmissionExemption.LOCAL_EFFECT
    assert admission.requires_full_continuity is False
    assert admission.requires_existing_hydration is False
    assert admission.grants_authority is False


@pytest.mark.parametrize(
    "path",
    [
        ("continuity", "local", "doctor"),
        ("continuity", "local", "resume"),
        ("continuity", "local", "unknown-writer"),
        ("continuity", "local"),
        ("continuity", "drain"),
        ("continuity", "local", "drain", "unknown"),
        ("future", "local", "drain"),
    ],
)
def test_reader_unknown_or_neighbor_path_cannot_claim_local_mutation_exemption(
    path: tuple[str, ...],
) -> None:
    with pytest.raises(PolicyViolation, match="exact reviewed admission"):
        assert_local_effect_admission(path)


def test_unknown_continuity_apply_stays_full_continuity_fail_closed() -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("continuity", "local", "unknown-writer"), {"apply": True}
    )
    assert admission.mutating is True
    assert admission.requires_full_continuity is True
    assert admission.requires_existing_hydration is True
    assert admission.exemption is None
    assert admission.grants_authority is False


def _arguments(value: dict[str, Any]) -> LocalContinuityArguments:
    return LocalContinuityArguments(value["home"], value["binding"].session_id, ROOT, (SOURCE_REF,))


@pytest.fixture
def runtime(composition: dict[str, Any]) -> dict[str, Any]:
    # Explicit test setup admits runtime configuration before the production factory.
    SQLiteLocalRuntimeStore(composition["path"])
    arguments = _arguments(composition)
    return composition | {"arguments": arguments, "command": LocalContinuityRuntime(arguments)}


def _files(home: Path) -> dict[str, tuple[int, bytes | None]]:
    return {
        str(path.relative_to(home)): (
            stat.S_IMODE(path.lstat().st_mode),
            path.read_bytes() if path.is_file() else None,
        )
        for path in (home, *home.rglob("*"))
        if not path.name.startswith("operational.db")
    }


def _start(value: dict[str, Any]) -> str:
    _stage_start(value, drain=False)
    drained = value["command"].drain()
    assert drained["persisted_spool_count"] == 1
    assert drained["scope"] == "current-source-and-spool"
    hydrated = value["command"].hydrate(_request())
    assert hydrated["provider_called"] is hydrated["grants_authority"] is False
    assert hydrated["installed_client_lifecycle_proven"] is False
    return str(hydrated["manifest_digest"])


def _freeze(value: dict[str, Any]) -> tuple[str, str]:
    context = _start(value)
    _stage(value, "PreCompact")
    assert value["command"].drain()["persisted_spool_count"] == 2
    checkpoint = value["command"].checkpoint(context, "integration-explicit-checkpoint")
    resumed = value["command"].resume(checkpoint["checkpoint_digest"])
    assert resumed["reacquire_required"] is True
    assert resumed["approval_inherited"] is resumed["grants_authority"] is False
    _stage(value, "Stop")
    assert value["command"].drain()["persisted_spool_count"] == 3
    summary = CloseSummary(
        ("Inspected the actual bounded Akilli Kasa health source.",),
        (),
        (),
        ("Native lifecycle is a separate unproven gate.",),
        "Verify the next approved gate.",
        ((SOURCE_REF, digest(value["text"])),),
        ((f"context/{context[7:]}", context),),
    )
    frozen = value["command"].freeze(summary, context, "integration-explicit-freeze")
    assert frozen["native_ack"] is frozen["grants_authority"] is False
    return context, str(frozen["request_digest"])


def _tick(value: dict[str, Any], request: str, phase: str, **kwargs: Any) -> dict[str, Any]:
    return dict(
        value["command"].close_tick(
            request,
            phase,
            "composition-independent",
            os.getpid(),
            "composition-process",
            **kwargs,
        )
    )


def _complete(value: dict[str, Any], request: str) -> str:
    _tick(value, request, "compile")
    _tick(value, request, "deliver")
    for _ in range(3):
        result = CliRunner().invoke(
            app,
            ["local-runtime", "outbox-once", "--home", str(value["home"])],
        )
        assert result.exit_code == 0, result.output
    final = _tick(value, request, "finalize")
    assert final["state"] == "complete"
    return str(final["receipt_digest"])


def test_factory_and_open_doctor_are_readonly_existing_state(
    runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = logical_database_digest(runtime["path"])
    files = _files(runtime["home"])
    source = (ROOT / SOURCE_REF).read_bytes()
    statements: list[str] = []
    original = sqlite3.connect

    def observe(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return cast(sqlite3.Connection, connection)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Read-only composition doctor attempted mutation")

    with monkeypatch.context() as guard:
        guard.setattr(sqlite3, "connect", observe)
        guard.setattr(SQLiteContinuityStore, "_transaction", forbidden)
        guard.setattr(HomeLayout, "ensure", forbidden)
        guard.setattr(BoundedContinuitySource, "apply", forbidden)
        guard.setattr(SQLiteContinuityStore, "bind_session", forbidden)
        guard.setattr(ClientLifecycleSpool, "stage", forbidden)
        guard.setattr(Path, "mkdir", forbidden)
        guard.setattr(Path, "chmod", forbidden)
        command = LocalContinuityRuntime(runtime["arguments"])
        report = command.doctor()
    assert report["read_only"] is True
    assert report["grants_authority"] is report["native_ack"] is False
    assert report["current_source_verified"] is True
    assert "missing-required-hook-events" in report["issues"]
    assert not any("begin immediate" in sql.lower() for sql in statements)
    assert logical_database_digest(runtime["path"]) == before
    assert _files(runtime["home"]) == files
    assert (ROOT / SOURCE_REF).read_bytes() == source


def test_actual_lifecycle_survives_new_runtime_and_finishes_through_production_delivery(
    runtime: dict[str, Any],
) -> None:
    source = (ROOT / SOURCE_REF).read_bytes()
    _, request = _freeze(runtime)
    runtime["command"] = LocalContinuityRuntime(runtime["arguments"])
    before = runtime["command"].doctor()
    assert before["projection_state"] == "missing-or-drifted"
    receipt = _complete(runtime, request)
    reopened = LocalContinuityRuntime(runtime["arguments"])
    report = reopened.doctor()
    assert report["state"] == "healthy"
    assert report["session_state"] == "closed"
    assert report["projection_state"] == "exact"
    assert report["close_receipt_digest"] == receipt
    assert report["installed_client_lifecycle_proven"] is report["native_ack"] is False
    assert _tick(runtime, request, "finalize")["receipt_digest"] == receipt
    assert (ROOT / SOURCE_REF).read_bytes() == source


def test_missing_runtime_config_is_not_created_by_close_factory(
    composition: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = LocalContinuityRuntime(_arguments(composition))
    before = logical_database_digest(composition["path"])
    with sqlite3.connect(composition["path"]) as db:
        assert db.execute("select count(*) from local_runtime_config").fetchone()[0] == 0

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("Missing config was passed into an implicitly initializing runtime store")

    monkeypatch.setattr(SQLiteLocalRuntimeStore, "__init__", forbidden)
    with pytest.raises(PolicyViolation, match="existing admitted runtime config"):
        command.close_tick(digest("nonexistent"), "compile", "owner", os.getpid(), "token")
    assert logical_database_digest(composition["path"]) == before


@pytest.mark.parametrize("field", ["session_id", "source_root", "source_paths"])
def test_foreign_source_or_missing_session_never_creates_authority(
    runtime: dict[str, Any],
    field: str,
) -> None:
    changes: dict[str, Any] = {
        "session_id": str(uuid4()),
        "source_root": Path(__file__).resolve().parents[2],
        "source_paths": ("src/akilli_kasa/api/__init__.py",),
    }
    before = logical_database_digest(runtime["path"])
    files = _files(runtime["home"])
    with pytest.raises(ZekamError):
        command = LocalContinuityRuntime(replace(runtime["arguments"], **{field: changes[field]}))
        command.hydrate(_request())
    assert logical_database_digest(runtime["path"]) == before
    assert _files(runtime["home"]) == files


def test_missing_optional_index_is_not_created_and_cannot_hydrate(runtime: dict[str, Any]) -> None:
    index = runtime["home"].parent / "missing-index.sqlite"
    command = LocalContinuityRuntime(replace(runtime["arguments"], index_path=index))
    before = logical_database_digest(runtime["path"])
    with pytest.raises(ZekamError):
        command.hydrate(_request())
    assert not index.exists()
    assert logical_database_digest(runtime["path"]) == before


@pytest.mark.parametrize(
    "phase,repair", [("unknown", None), ("compile", "unexpected"), ("repair", None)]
)
def test_invalid_phase_never_enters_current_composition(
    runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    repair: str | None,
) -> None:
    before = logical_database_digest(runtime["path"])

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("Invalid close phase entered live composition")

    monkeypatch.setattr(LocalContinuityRuntime, "_close", forbidden)
    with pytest.raises(ValidationFailed):
        _tick(runtime, digest("invalid"), phase, repair_key=repair)
    assert logical_database_digest(runtime["path"]) == before


def test_historical_doctor_and_advisory_drain_survive_current_config_drift(
    runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, request = _freeze(runtime)
    receipt = _complete(runtime, request)
    _stage(runtime, "SessionEnd")
    config = runtime["home"] / "config.yaml"
    config.write_text(config.read_text() + "logging:\n  level: DEBUG\n")
    command = LocalContinuityRuntime(runtime["arguments"])
    before = logical_database_digest(runtime["path"])

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("Historical doctor called writer transaction")

    with monkeypatch.context() as guard:
        guard.setattr(SQLiteContinuityStore, "_transaction", forbidden)
        report = command.doctor()
    assert report["current_source_verified"] is False
    assert report["close_receipt_digest"] == receipt
    assert report["projection_state"] == "exact"
    assert "current-source-or-authority-stale" in report["issues"]
    assert logical_database_digest(runtime["path"]) == before
    drained = command.drain()
    assert drained["scope"] == "historical-control-only"
    assert drained["native_ack"] is drained["grants_authority"] is False
    with sqlite3.connect(runtime["path"]) as db:
        assert (
            db.execute(
                "select count(*) from session_event where session_id=?",
                (runtime["binding"].session_id,),
            ).fetchone()[0]
            == 3
        )
        assert db.execute("select count(*) from continuity_control_event").fetchone()[0] == 1
    persisted = logical_database_digest(runtime["path"])
    with pytest.raises(ZekamError):
        command.hydrate(_request())
    with pytest.raises(ZekamError):
        command.close_tick(request, "compile", "owner", os.getpid(), "token")
    assert logical_database_digest(runtime["path"]) == persisted


def test_doctor_projection_fifo_is_opened_nonblocking_before_type_rejection(
    runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, request = _freeze(runtime)
    _tick(runtime, request, "compile")
    target = next((runtime["home"] / "inbox" / "generated").rglob("*.md"))
    target.unlink()
    os.mkfifo(target, mode=0o600)
    original = os.open
    reached = False

    def bounded_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal reached
        if os.fsdecode(path) == target.name:
            reached = True
            assert flags & os.O_NONBLOCK, "FIFO projection could block doctor before fstat"
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", bounded_open)
    report = runtime["command"].doctor()
    assert report["projection_state"] == "missing-or-drifted"
    assert "generated-projection-missing-or-drifted" in report["issues"]
    assert reached


def test_frozen_source_drift_allows_only_historical_advisory(
    runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, request = _freeze(runtime)
    _stage(runtime, "SessionEnd")
    original = BoundedContinuitySource._read

    def changed_read(
        source: BoundedContinuitySource,
        relative: str,
        cap: int,
        *,
        optional: bool = False,
    ) -> bytes | None:
        actual = original(source, relative, cap, optional=optional)
        if relative == SOURCE_REF and actual is not None:
            return actual + b"\n# independent changed-capture simulation\n"
        return actual

    monkeypatch.setattr(BoundedContinuitySource, "_read", changed_read)
    command = LocalContinuityRuntime(runtime["arguments"])
    before = logical_database_digest(runtime["path"])
    report = command.doctor()
    assert report["read_only"] is True
    assert report["current_source_verified"] is False
    assert "current-source-or-authority-stale" in report["issues"]
    assert logical_database_digest(runtime["path"]) == before
    drained = command.drain()
    assert drained["scope"] == "historical-control-only"
    after_advisory = logical_database_digest(runtime["path"])
    with pytest.raises(ZekamError):
        command.close_tick(request, "compile", "owner", os.getpid(), "token")
    assert logical_database_digest(runtime["path"]) == after_advisory
    with sqlite3.connect(runtime["path"]) as db:
        assert db.execute("select count(*) from local_effect_claim").fetchone()[0] == 0
        assert db.execute("select count(*) from close_receipt").fetchone()[0] == 0
        assert db.execute("select count(*) from continuity_control_event").fetchone()[0] == 1


@pytest.mark.parametrize("field", ["home", "source_root", "index_path"])
@pytest.mark.parametrize("value", [False, 7, "relative-text"])
def test_wrong_path_types_are_typed_rejections(tmp_path: Path, field: str, value: Any) -> None:
    values: dict[str, Any] = {
        "home": tmp_path,
        "session_id": str(uuid4()),
        "source_root": ROOT,
        "source_paths": (SOURCE_REF,),
        "index_path": None,
    }
    values[field] = value
    with pytest.raises(ValidationFailed):
        LocalContinuityArguments(**values)


def test_config_changes_after_factory_before_hydration_create_no_receipt(
    runtime: dict[str, Any],
) -> None:
    _stage_start(runtime, drain=False)
    runtime["command"].drain()
    config = runtime["home"] / "config.yaml"
    config.write_text(config.read_text() + "logging:\n  level: DEBUG\n")
    before = logical_database_digest(runtime["path"])
    with pytest.raises(ZekamError):
        runtime["command"].hydrate(_request())
    assert logical_database_digest(runtime["path"]) == before
    with sqlite3.connect(runtime["path"]) as db:
        assert db.execute("select count(*) from hydration_receipt").fetchone()[0] == 0


def test_config_drift_inside_hydration_writer_rolls_back_without_receipt(
    runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stage_start(runtime, drain=False)
    runtime["command"].drain()
    config = runtime["home"] / "config.yaml"
    before = logical_database_digest(runtime["path"])
    original = SQLiteContinuityStore._no_pending
    injected = False

    def change_inside_writer(db: sqlite3.Connection, binding: Any) -> None:
        nonlocal injected
        original(db, binding)
        # The admitted SQLite writer explicitly enables foreign_keys; the source
        # resolver's separate mode=ro connection does not. This selects the real
        # hydrate transaction after all outer composition/source preflights.
        if not injected and db.execute("pragma foreign_keys").fetchone()[0] == 1:
            assert db.in_transaction
            config.write_text(config.read_text() + "logging:\n  level: DEBUG\n")
            injected = True

    monkeypatch.setattr(SQLiteContinuityStore, "_no_pending", staticmethod(change_inside_writer))
    with pytest.raises(ZekamError):
        runtime["command"].hydrate(_request())
    assert injected
    assert logical_database_digest(runtime["path"]) == before
    with sqlite3.connect(runtime["path"]) as db:
        assert db.execute("select count(*) from hydration_receipt").fetchone()[0] == 0


def test_existing_only_runtime_never_initializes_missing_config(
    composition: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = logical_database_digest(composition["path"])

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("Existing-only runtime attempted bootstrap")

    monkeypatch.setattr(runtime_module, "bootstrap", forbidden)
    with pytest.raises(ZekamError):
        SQLiteLocalRuntimeStore(composition["path"], existing_only=True)
    assert logical_database_digest(composition["path"]) == before
    with sqlite3.connect(composition["path"]) as db:
        assert db.execute("select count(*) from local_runtime_config").fetchone()[0] == 0


def test_config_removed_between_composition_gate_and_constructor_is_not_recreated(
    runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SQLiteLocalRuntimeStore.__init__
    after_external_removal: list[str] = []

    def remove_before_constructor(
        store: SQLiteLocalRuntimeStore, path: Path, **kwargs: Any
    ) -> None:
        assert kwargs.get("existing_only") is True
        with sqlite3.connect(path) as db:
            trigger = db.execute(
                "select sql from sqlite_master where name='local_runtime_config_no_delete'"
            ).fetchone()[0]
            db.execute("drop trigger local_runtime_config_no_delete")
            db.execute("delete from local_runtime_config")
            db.execute(trigger)
        # Restore the exact trigger: the failure must be missing config, not a
        # schema-fingerprint alarm from the deliberate external fault injection.
        after_external_removal.append(logical_database_digest(path))
        original(store, path, **kwargs)

    monkeypatch.setattr(SQLiteLocalRuntimeStore, "__init__", remove_before_constructor)
    with pytest.raises(ZekamError):
        _tick(runtime, digest("nonexistent-close"), "compile")
    assert len(after_external_removal) == 1
    assert logical_database_digest(runtime["path"]) == after_external_removal[0]
    with sqlite3.connect(runtime["path"]) as db:
        assert db.execute("select count(*) from local_runtime_config").fetchone()[0] == 0
        assert db.execute("select count(*) from local_effect_claim").fetchone()[0] == 0


def test_existing_only_database_removed_after_schema_check_is_not_recreated(
    runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = runtime["path"]
    retained = path.with_name("retained-operational.db")
    original = operational_schema_status
    moved = False

    def remove_after_check(candidate: Path) -> Any:
        nonlocal moved
        result = original(candidate)
        if candidate == path and not moved:
            path.rename(retained)
            moved = True
        return result

    monkeypatch.setattr(runtime_module, "status", remove_after_check)
    with pytest.raises((ZekamError, sqlite3.Error)):
        SQLiteLocalRuntimeStore(path, existing_only=True)
    assert moved
    assert not path.exists()
    assert retained.is_file()
