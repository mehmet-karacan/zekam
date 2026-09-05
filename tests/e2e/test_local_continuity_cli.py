"""Bounded local CLI wiring; parser fixtures do not claim installed client lifecycle."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import typer
from tests.integration.test_local_continuity_source_authority import authority as authority
from tests.unit.test_local_continuity_environment import environment as environment
from tests.unit.test_local_continuity_startup import NOW, ROOT, SOURCE_REF, _stage_start
from tests.unit.test_local_startup_composition import composition as composition
from typer.testing import CliRunner

from zekam.application.config import PersistenceBackend
from zekam.application.local_continuity_close import (
    CANDIDATE_RECIPE_DIGEST,
    CloseCandidateBundle,
    CloseCandidateClaim,
    CloseSummary,
)
from zekam.application.local_continuity_startup import StartupRequest
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest
from zekam.interfaces.cli import continuity as cli

pytestmark = pytest.mark.e2e
INCARNATION = "test-process-incarnation"
SESSION = "00000000-0000-4000-8000-000000000001"


def _arguments(home: Path) -> list[str]:
    return [
        "local",
        "--home",
        str(home),
        "--session-id",
        SESSION,
        "--source-root",
        str(ROOT),
        "--source-file",
        SOURCE_REF,
    ]


@pytest.mark.parametrize(
    ("leaf", "previous", "rebind"),
    [
        ("source-bind", [], False),
        ("source-rebind", ["--previous-revision", digest("previous")], True),
    ],
)
def test_source_authority_leaves_dispatch_exact_typed_explicit_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leaf: str,
    previous: list[str],
    rebind: bool,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "_source_authority_execute", lambda **values: calls.append(values))
    project = "22222222-2222-4222-8222-222222222222"
    binding = "33333333-3333-4333-8333-333333333333"
    snapshot = "44444444-4444-4444-8444-444444444444"
    result = CliRunner().invoke(
        cli.app,
        [
            leaf,
            "--home",
            str(tmp_path),
            "--project-id",
            project,
            "--source-binding-id",
            binding,
            "--source-snapshot-id",
            snapshot,
            "--device-id",
            "macbook",
            "--source-root",
            str(ROOT),
            "--source-file",
            SOURCE_REF,
            "--onayliyorum",
            *previous,
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "home": tmp_path,
            "project_id": project,
            "source_binding_id": binding,
            "source_snapshot_id": snapshot,
            "device_id": "macbook",
            "source_root": ROOT,
            "source_files": (SOURCE_REF,),
            "previous_revision": digest("previous") if rebind else None,
            "rebind": rebind,
            "confirmed": True,
        }
    ]


def test_source_authority_requires_confirmation_after_exact_admission_before_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admissions: list[tuple[str, ...]] = []

    def reject(command: tuple[str, ...], *, confirmed: bool) -> object:
        admissions.append(command)
        assert confirmed is False
        raise ValidationFailed("confirmation")

    monkeypatch.setattr(cli, "_issue_gate_a_source_capability", reject)
    monkeypatch.setattr(
        cli,
        "build_context",
        lambda **_values: pytest.fail("context accessed before confirmation"),
    )
    with pytest.raises(typer.Exit):
        cli._source_authority_execute(
            home=tmp_path,
            project_id="22222222-2222-4222-8222-222222222222",
            source_binding_id="33333333-3333-4333-8333-333333333333",
            source_snapshot_id="44444444-4444-4444-8444-444444444444",
            device_id="macbook",
            source_root=ROOT,
            source_files=(SOURCE_REF,),
            previous_revision=None,
            rebind=False,
            confirmed=False,
        )
    assert admissions == [("continuity", "source-bind")]


def test_source_bind_and_rebind_use_existing_operational_snapshot(
    authority: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = SimpleNamespace(
        backend=PersistenceBackend.SQLITE,
        sqlite_path=lambda _home: authority["path"],
    )
    monkeypatch.setattr(
        cli,
        "build_context",
        lambda **_values: SimpleNamespace(
            home=authority["home"], settings=SimpleNamespace(database=database)
        ),
    )
    base = [
        "--home",
        str(authority["home"]),
        "--project-id",
        authority["recipe"].project_id,
        "--source-binding-id",
        authority["recipe"].source_binding_id,
        "--source-snapshot-id",
        authority["snapshot"].id,
        "--device-id",
        "macbook",
        "--source-root",
        str(authority["root"]),
        "--source-file",
        authority["recipe"].allowed_paths[0],
        "--onayliyorum",
    ]
    first = CliRunner().invoke(cli.app, ["source-bind", *base])
    assert first.exit_code == 0, first.output
    first_body = json.loads(first.stdout)
    assert first_body["generation"] == 1
    assert first_body["backup_restore_ready"] is False
    second = CliRunner().invoke(
        cli.app,
        [
            "source-rebind",
            *base,
            "--previous-revision",
            first_body["revision_digest"],
        ],
    )
    assert second.exit_code == 0, second.output
    second_body = json.loads(second.stdout)
    assert second_body["generation"] == 2
    assert second_body["revision_digest"] != first_body["revision_digest"]


@pytest.fixture
def dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    admission: list[tuple[str, ...]] = []
    identities: list[int] = []

    class Runtime:
        def __getattr__(self, name: str) -> Any:
            def method(*args: Any, **kwargs: Any) -> dict[str, Any]:
                calls.append((name, args, kwargs))
                return {"operation": name, "wiring_only": True}

            return method

    def runtime(ctx: Any) -> Any:
        assert ctx.obj.home == tmp_path
        assert ctx.obj.session_id == SESSION
        assert ctx.obj.source_root == ROOT
        assert ctx.obj.source_paths == (SOURCE_REF,)
        return Runtime()

    def token(pid: int) -> str:
        identities.append(pid)
        return INCARNATION

    monkeypatch.setattr(cli, "_runtime", runtime)
    monkeypatch.setattr(cli, "assert_local_effect_admission", admission.append)
    monkeypatch.setattr(cli, "process_incarnation_token", token)
    return {"home": tmp_path, "calls": calls, "admission": admission, "identities": identities}


@pytest.mark.parametrize(
    "command,method,mutating",
    [
        (["doctor"], "doctor", False),
        (["drain"], "drain", True),
        (["checkpoint", "--context-digest", digest("context"), "--key", "cp"], "checkpoint", True),
        (["resume", "--checkpoint-digest", digest("checkpoint")], "resume", False),
    ],
)
def test_each_leaf_dispatches_exact_method_without_process_identity(
    dispatch: dict[str, Any], command: list[str], method: str, mutating: bool
) -> None:
    result = CliRunner().invoke(cli.app, _arguments(dispatch["home"]) + command)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"operation": method, "wiring_only": True}
    assert len(dispatch["calls"]) == 1 and dispatch["calls"][0][0] == method
    assert dispatch["admission"] == ([("continuity", "local", method)] if mutating else [])
    assert dispatch["identities"] == []


def test_hydrate_passes_exact_typed_request(dispatch: dict[str, Any]) -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            *_arguments(dispatch["home"]),
            "hydrate",
            "--source-ref",
            SOURCE_REF,
            "--token-budget",
            "8192",
            "--key",
            "h1",
            "--observed-at",
            NOW.isoformat(),
            "--query",
            "health",
            "--note-limit",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    method, args, kwargs = dispatch["calls"][0]
    assert method == "hydrate" and kwargs == {}
    assert args == (StartupRequest((SOURCE_REF,), 8192, "h1", NOW, 2, "health"),)
    assert dispatch["admission"] == [("continuity", "local", "hydrate")]
    assert dispatch["identities"] == []


@pytest.mark.parametrize(
    "phase", ["compile", "deliver", "finalize", "repair", "reconcile-delivery"]
)
def test_close_tick_uses_only_current_real_process_identity(
    dispatch: dict[str, Any], phase: str
) -> None:
    arguments = [
        *_arguments(dispatch["home"]),
        "close-tick",
        "--request-digest",
        digest("request"),
        "--phase",
        phase,
        "--owner-id",
        "reviewed-local-worker",
    ]
    if phase == "repair":
        arguments += ["--repair-key", "repair-1"]
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 0, result.output
    assert dispatch["calls"] == [
        (
            "close_tick",
            (digest("request"), phase, "reviewed-local-worker", os.getpid(), INCARNATION),
            {"repair_key": "repair-1" if phase == "repair" else None},
        )
    ]
    assert dispatch["identities"] == [os.getpid()]
    assert dispatch["admission"] == [("continuity", "local", "close-tick")]


def _summary_body() -> dict[str, Any]:
    # Bounded content comes from the user's actual read-only project.
    return {
        "performed": [(ROOT / SOURCE_REF).read_text().splitlines()[0]],
        "decisions": [],
        "failures": [],
        "remaining": [],
        "next_safe_step": "Verify checkpoint",
        "sources": [[SOURCE_REF, digest((ROOT / SOURCE_REF).read_text())]],
        "evidence": [["checkpoint/fixture", digest("checkpoint")]],
    }


def _candidate_body(
    *,
    summary: dict[str, Any] | None = None,
    memory: tuple[str, ...] = ("Remember only the literal verified health evidence.",),
) -> dict[str, Any]:
    summary = _summary_body() if summary is None else summary

    def claim(text: str) -> CloseCandidateClaim:
        return CloseCandidateClaim(
            text,
            tuple(tuple(pair) for pair in summary["sources"]),
            tuple(tuple(pair) for pair in summary["evidence"]),
        )

    claims = tuple(claim(text) for text in memory)
    claims = tuple(sorted(claims, key=lambda item: item.candidate_id("memory")))
    result = CloseCandidateBundle(memory=claims).body()
    assert result["recipe_digest"] == CANDIDATE_RECIPE_DIGEST
    return result


def _freeze_v2_arguments(home: Path, summary: Path, candidates: Path) -> list[str]:
    return [
        *_arguments(home),
        "freeze-v2",
        "--summary",
        str(summary),
        "--candidates-file",
        str(candidates),
        "--context-digest",
        digest("context"),
        "--key",
        "c2",
    ]


def test_freeze_parses_only_explicit_summary_file(dispatch: dict[str, Any]) -> None:
    path = dispatch["home"] / "summary.json"
    body = _summary_body()
    path.write_text(json.dumps(body))
    result = CliRunner().invoke(
        cli.app,
        [
            *_arguments(dispatch["home"]),
            "freeze",
            "--summary",
            str(path),
            "--context-digest",
            digest("context"),
            "--key",
            "c1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert dispatch["calls"] == [
        ("freeze", (CloseSummary.from_body(body), digest("context"), "c1"), {})
    ]
    assert dispatch["identities"] == []


def test_freeze_v2_requires_separate_exact_candidate_file(dispatch: dict[str, Any]) -> None:
    summary_path = dispatch["home"] / "summary.json"
    candidate_path = dispatch["home"] / "candidates.json"
    summary_body = _summary_body()
    candidate_body = _candidate_body(summary=summary_body)
    summary_path.write_text(json.dumps(summary_body))
    candidate_path.write_text(json.dumps(candidate_body))
    result = CliRunner().invoke(
        cli.app,
        [
            *_arguments(dispatch["home"]),
            "freeze-v2",
            "--summary",
            str(summary_path),
            "--candidates-file",
            str(candidate_path),
            "--context-digest",
            digest("context"),
            "--key",
            "c2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert dispatch["calls"] == [
        (
            "freeze_v2",
            (
                CloseSummary.from_body(summary_body),
                CloseCandidateBundle.from_body(candidate_body),
                digest("context"),
                "c2",
            ),
            {},
        )
    ]
    assert dispatch["admission"] == [("continuity", "local", "freeze-v2")]
    assert dispatch["identities"] == []


def test_freeze_v2_same_file_identity_rejects_before_dispatch(dispatch: dict[str, Any]) -> None:
    path = dispatch["home"] / "same.json"
    path.write_text(json.dumps(_summary_body()))
    result = CliRunner().invoke(
        cli.app,
        [
            *_arguments(dispatch["home"]),
            "freeze-v2",
            "--summary",
            str(path),
            "--candidates-file",
            str(path),
            "--context-digest",
            digest("context"),
            "--key",
            "c2",
        ],
    )
    assert result.exit_code == 70 and dispatch["calls"] == []
    assert json.loads(result.stderr)["error"] == "policy-violation"


def test_unregistered_freeze_v2_leaf_denial_precedes_runtime_and_file_reads(
    dispatch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[str, ...]] = []

    def denied(path: tuple[str, ...]) -> None:
        observed.append(path)
        raise PolicyViolation("Exact v2 leaf is not admitted")

    def forbidden(_ctx: Any) -> Any:
        pytest.fail("Denied v2 leaf must not construct runtime")

    monkeypatch.setattr(cli, "assert_local_effect_admission", denied)
    monkeypatch.setattr(cli, "_runtime", forbidden)
    missing = dispatch["home"] / "must-not-be-read.json"
    result = CliRunner().invoke(
        cli.app,
        _freeze_v2_arguments(dispatch["home"], missing, missing),
    )
    assert result.exit_code == 70
    assert observed == [("continuity", "local", "freeze-v2")]
    assert dispatch["calls"] == []


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"null",
        b"[]",
        b"true",
        b"\xff",
        b"{",
        b"{}",
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b"[" * 16000 + b"]" * 16000,
        b"x" * 32769,
    ],
)
def test_bad_candidate_document_is_sanitized_before_dispatch(
    dispatch: dict[str, Any], payload: bytes
) -> None:
    summary = dispatch["home"] / "summary.json"
    candidates = dispatch["home"] / "private-candidates.json"
    summary.write_text(json.dumps(_summary_body()))
    candidates.write_bytes(payload)
    result = CliRunner().invoke(
        cli.app, _freeze_v2_arguments(dispatch["home"], summary, candidates)
    )
    assert result.exit_code == 70 and dispatch["calls"] == []
    assert "private-candidates" not in result.output and str(dispatch["home"]) not in result.output
    assert json.loads(result.stderr)["error"] in {"validation-failed", "io-error"}


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo", "symlink"])
def test_candidate_path_must_be_existing_nofollow_regular_file(
    dispatch: dict[str, Any], kind: str
) -> None:
    summary = dispatch["home"] / "summary.json"
    summary.write_text(json.dumps(_summary_body()))
    candidates = dispatch["home"] / "candidate-input"
    if kind == "directory":
        candidates.mkdir()
    elif kind == "fifo":
        os.mkfifo(candidates)
    elif kind == "symlink":
        target = dispatch["home"] / "candidate-target.json"
        target.write_text(json.dumps(_candidate_body()))
        candidates.symlink_to(target)
    result = CliRunner().invoke(
        cli.app, _freeze_v2_arguments(dispatch["home"], summary, candidates)
    )
    assert result.exit_code == 70 and dispatch["calls"] == []
    assert str(dispatch["home"]) not in result.output


def test_candidate_exact_schema_secret_order_and_refs_reject_before_dispatch(
    dispatch: dict[str, Any],
) -> None:
    summary_path = dispatch["home"] / "summary.json"
    candidates_path = dispatch["home"] / "candidates.json"
    summary_body = _summary_body()
    summary_path.write_text(json.dumps(summary_body))
    valid = _candidate_body(summary=summary_body)
    source = tuple(summary_body["sources"][0])
    evidence = tuple(summary_body["evidence"][0])
    first = CloseCandidateClaim("First exact literal.", (source,), (evidence,))
    second = CloseCandidateClaim("Second exact literal.", (source,), (evidence,))
    ordered = sorted((first, second), key=lambda item: item.candidate_id("memory"))
    secret_text = "api_" + "key" + "=" + '"' + "abc123456789" + '"'
    secret_claim = {
        "schema": "zekam-close-candidate-claim/v1",
        "text": secret_text,
        "source_refs": [source],
        "evidence_refs": [evidence],
    }
    ref_order_claim = {
        "schema": "zekam-close-candidate-claim/v1",
        "text": "Refs must already be canonical.",
        "source_refs": [
            ["z-ref", digest("z-ref")],
            ["a-ref", digest("a-ref")],
        ],
        "evidence_refs": [evidence],
    }
    invalid = (
        {**valid, "extra": False},
        {**valid, "recipe_digest": digest("unknown-recipe")},
        {**valid, "memory": None},
        {**valid, "memory": [secret_claim]},
        {**valid, "memory": [ordered[1].body(), ordered[0].body()]},
        {**valid, "memory": [ref_order_claim]},
    )
    for body in invalid:
        candidates_path.write_text(json.dumps(body))
        result = CliRunner().invoke(
            cli.app, _freeze_v2_arguments(dispatch["home"], summary_path, candidates_path)
        )
        assert result.exit_code == 70 and dispatch["calls"] == []
        assert json.loads(result.stderr)["error"] in {
            "validation-failed",
            "policy-violation",
        }


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"null",
        b"[]",
        b"true",
        b"\xff",
        b"{",
        b"{}",
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b"[" * 16000 + b"]" * 16000,
        b"x" * 32769,
    ],
)
def test_bad_summary_is_sanitized_and_never_dispatched(
    dispatch: dict[str, Any], payload: bytes
) -> None:
    path = dispatch["home"] / "private-summary.json"
    path.write_bytes(payload)
    result = CliRunner().invoke(
        cli.app,
        [
            *_arguments(dispatch["home"]),
            "freeze",
            "--summary",
            str(path),
            "--context-digest",
            digest("context"),
            "--key",
            "c1",
        ],
    )
    assert result.exit_code == 70
    assert dispatch["calls"] == []
    assert "private-summary" not in result.output and str(dispatch["home"]) not in result.output
    assert json.loads(result.stderr)["error"] in {"validation-failed", "io-error"}


def test_summary_symlink_is_not_opened(dispatch: dict[str, Any]) -> None:
    source = dispatch["home"] / "summary.json"
    source.write_text(json.dumps(_summary_body()))
    link = dispatch["home"] / "link.json"
    link.symlink_to(source)
    result = CliRunner().invoke(
        cli.app,
        [
            *_arguments(dispatch["home"]),
            "freeze",
            "--summary",
            str(link),
            "--context-digest",
            digest("context"),
            "--key",
            "c1",
        ],
    )
    assert result.exit_code == 70 and dispatch["calls"] == []
    assert source.read_text() == json.dumps(_summary_body())


@pytest.mark.parametrize("time", ["", "2026-09-02", "2026-09-02T12:00:00", "not-time", "x" * 65])
def test_hydrate_requires_explicit_timezone_without_echoing_input(
    dispatch: dict[str, Any], time: str
) -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            *_arguments(dispatch["home"]),
            "hydrate",
            "--source-ref",
            SOURCE_REF,
            "--token-budget",
            "8192",
            "--key",
            "h1",
            "--observed-at",
            time,
        ],
    )
    assert result.exit_code == 70 and dispatch["calls"] == []
    assert json.loads(result.stderr)["error"] == "validation-failed"


@pytest.mark.parametrize(
    "error",
    [PolicyViolation, ValidationFailed, OSError, TypeError, ValueError, sqlite3.DatabaseError],
)
def test_private_exception_text_never_reaches_cli(
    dispatch: dict[str, Any], monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def broken(_ctx: Any) -> Any:
        raise error("PRIVATE-CALLER-VALUE /private/secret")

    monkeypatch.setattr(cli, "_runtime", broken)
    result = CliRunner().invoke(cli.app, [*_arguments(dispatch["home"]), "doctor"])
    assert result.exit_code == 70 and dispatch["calls"] == []
    assert "PRIVATE" not in result.output and "/private" not in result.output
    assert dispatch["admission"] == []


def test_mutation_admission_rejection_precedes_runtime_construction(
    dispatch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(_path: tuple[str, ...]) -> Any:
        raise PolicyViolation("Unreviewed leaf")

    def forbidden(_ctx: Any) -> Any:
        pytest.fail("Admission rejection must precede runtime construction")

    monkeypatch.setattr(cli, "assert_local_effect_admission", denied)
    monkeypatch.setattr(cli, "_runtime", forbidden)
    result = CliRunner().invoke(cli.app, [*_arguments(dispatch["home"]), "drain"])
    assert result.exit_code == 70 and dispatch["calls"] == []


@pytest.mark.parametrize("phase,repair", [("unknown", None), ("repair", None), ("compile", "x")])
def test_invalid_close_phase_never_claims_or_reads_process_identity(
    dispatch: dict[str, Any], phase: str, repair: str | None
) -> None:
    arguments = [
        *_arguments(dispatch["home"]),
        "close-tick",
        "--request-digest",
        digest("request"),
        "--phase",
        phase,
    ]
    if repair is not None:
        arguments += ["--repair-key", repair]
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 70
    assert dispatch["calls"] == dispatch["identities"] == []


def test_missing_process_incarnation_cannot_claim(
    dispatch: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "process_incarnation_token", lambda _pid: None)
    result = CliRunner().invoke(
        cli.app,
        [
            *_arguments(dispatch["home"]),
            "close-tick",
            "--request-digest",
            digest("request"),
            "--phase",
            "compile",
        ],
    )
    assert result.exit_code == 70 and dispatch["calls"] == []


def _process(value: dict[str, Any], arguments: list[str]) -> subprocess.CompletedProcess[str]:
    base = _arguments(value["home"])
    base[base.index("--session-id") + 1] = value["binding"].session_id
    program = """
import socket
def forbidden(*args, **kwargs): raise AssertionError('No network/provider in local CLI')
socket.socket.connect = forbidden
socket.create_connection = forbidden
from zekam.interfaces.cli.main import run
run()
"""
    return subprocess.run(
        [sys.executable, "-c", program, "continuity", *base, *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )


def _root_process(value: dict[str, Any], arguments: list[str]) -> subprocess.CompletedProcess[str]:
    program = """
import socket
def forbidden(*args, **kwargs): raise AssertionError('No network/provider in local CLI')
socket.socket.connect = forbidden
socket.create_connection = forbidden
from zekam.interfaces.cli.main import run
run()
"""
    return subprocess.run(
        [sys.executable, "-c", program, *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )


def _stage_cli_event(value: dict[str, Any], event: str) -> None:
    document = {
        "session_id": value["binding"].external_session_id,
        "hook_event_name": event,
        "turn_id": str(uuid4()),
    }
    if event in {"PreCompact", "PostCompact"}:
        document["trigger"] = "manual"
    elif event == "Stop":
        document.update(stop_hook_active=False, permission_mode="default")
    parsed = parse_codex_hook_input(json.dumps(document))
    value["spool"].stage(
        parsed.observation_body(),
        delivery_id=parsed.delivery_id(occurrence_id=str(uuid4())),
        occurred_at=NOW,
    )


def test_real_process_existing_state_doctor_hydrate_and_replay(composition: dict[str, Any]) -> None:
    value = composition
    before = logical_database_digest(value["path"])
    result = _process(value, ["doctor"])
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["grants_authority"] is False
    assert logical_database_digest(value["path"]) == before
    _stage_start(value, drain=False)
    result = _process(value, ["drain"])
    assert result.returncode == 0, result.stdout + result.stderr
    arguments = [
        "hydrate",
        "--source-ref",
        SOURCE_REF,
        "--token-budget",
        "8192",
        "--key",
        "cli-hydrate",
        "--observed-at",
        dt.datetime.now(dt.UTC).isoformat(),
    ]
    first = _process(value, arguments)
    assert first.returncode == 0, first.stdout + first.stderr
    repeated = _process(value, arguments)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert (
        json.loads(first.stdout)["manifest_digest"]
        == json.loads(repeated.stdout)["manifest_digest"]
    )
    assert json.loads(first.stdout)["installed_client_lifecycle_proven"] is False
    manifest = json.loads(first.stdout)["manifest_digest"]
    checkpoint_args = ["checkpoint", "--context-digest", manifest, "--key", "cli-precompact"]
    before = logical_database_digest(value["path"])
    missing = _process(value, checkpoint_args)
    assert missing.returncode == 70
    assert logical_database_digest(value["path"]) == before

    turn_id = str(uuid4())

    def stage(event: str) -> None:
        parsed = parse_codex_hook_input(
            json.dumps(
                {
                    "session_id": value["binding"].external_session_id,
                    "hook_event_name": event,
                    "turn_id": turn_id,
                    "trigger": "manual",
                }
            )
        )
        value["spool"].stage(
            parsed.observation_body(),
            delivery_id=parsed.delivery_id(occurrence_id=str(uuid4())),
            occurred_at=NOW,
        )

    stage("PreCompact")
    unpersisted = _process(value, checkpoint_args)
    assert unpersisted.returncode == 70
    assert logical_database_digest(value["path"]) == before
    drained = _process(value, ["drain"])
    assert drained.returncode == 0, drained.stdout + drained.stderr
    checkpointed = _process(value, checkpoint_args)
    assert checkpointed.returncode == 0, checkpointed.stdout + checkpointed.stderr
    checkpoint = json.loads(checkpointed.stdout)
    assert checkpoint["native_ack"] is False
    stage("PostCompact")
    drained = _process(value, ["drain"])
    assert drained.returncode == 0, drained.stdout + drained.stderr
    resumed = _process(value, ["resume", "--checkpoint-digest", checkpoint["checkpoint_digest"]])
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    golden = json.loads(resumed.stdout)
    assert value["text"] in golden["context"]["context"]["fragments"].values()
    assert golden["uncovered_events"] == 1
    assert golden["reacquire_required"] is True
    assert (
        golden["approval_inherited"] is golden["grants_authority"] is golden["native_ack"] is False
    )


def test_structural_stop_fixture_explicit_v2_creates_six_inbox_files_and_replays(
    composition: dict[str, Any],
) -> None:
    # Stop is only a reviewed structural regression here. Installed Codex 0.151
    # exposes it per-turn, so this does not prove a native PRE_CLOSE boundary.
    value = composition
    SQLiteLocalRuntimeStore(value["path"])
    _stage_start(value, drain=False)
    drained = _process(value, ["drain"])
    assert drained.returncode == 0, drained.stdout + drained.stderr
    hydrated = _process(
        value,
        [
            "hydrate",
            "--source-ref",
            SOURCE_REF,
            "--token-budget",
            "8192",
            "--key",
            "cli-v2-hydrate",
            "--observed-at",
            dt.datetime.now(dt.UTC).isoformat(),
        ],
    )
    assert hydrated.returncode == 0, hydrated.stdout + hydrated.stderr
    manifest = json.loads(hydrated.stdout)["manifest_digest"]
    _stage_cli_event(value, "PreCompact")
    assert _process(value, ["drain"]).returncode == 0
    checkpoint = _process(
        value,
        ["checkpoint", "--context-digest", manifest, "--key", "cli-v2-precompact"],
    )
    assert checkpoint.returncode == 0, checkpoint.stdout + checkpoint.stderr
    _stage_cli_event(value, "Stop")
    assert _process(value, ["drain"]).returncode == 0

    summary_body = {
        "performed": ["Inspected the exact bounded health source."],
        "decisions": [],
        "failures": [],
        "remaining": ["Installed native lifecycle proof remains pending."],
        "next_safe_step": "Verify the reviewed close receipts.",
        "sources": [[SOURCE_REF, digest(value["text"])]],
        "evidence": [[f"context/{manifest[7:]}", manifest]],
    }
    candidate_body = _candidate_body(summary=summary_body)
    summary_path = value["home"] / "cli-v2-summary.json"
    candidate_path = value["home"] / "cli-v2-candidates.json"
    summary_path.write_text(json.dumps(summary_body))
    candidate_path.write_text(json.dumps(candidate_body))
    frozen = _process(
        value,
        [
            "freeze-v2",
            "--summary",
            str(summary_path),
            "--candidates-file",
            str(candidate_path),
            "--context-digest",
            manifest,
            "--key",
            "cli-v2-close",
        ],
    )
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr
    request = json.loads(frozen.stdout)
    assert request["candidate_recipe_digest"] == CANDIDATE_RECIPE_DIGEST
    assert request["operation"] == "freeze-v2" and request["state"] == "pending"
    request_digest = request["request_digest"]

    compiled = _process(
        value,
        ["close-tick", "--request-digest", request_digest, "--phase", "compile"],
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    delivered = _process(
        value,
        ["close-tick", "--request-digest", request_digest, "--phase", "deliver"],
    )
    assert delivered.returncode == 0, delivered.stdout + delivered.stderr
    for _ in range(8):
        bookkeeping = _root_process(
            value,
            ["local-runtime", "outbox-once", "--home", str(value["home"])],
        )
        assert bookkeeping.returncode == 0, bookkeeping.stdout + bookkeeping.stderr
        if json.loads(bookkeeping.stdout)["claimed_outbox_id"] is None:
            break
    else:  # pragma: no cover - bounded queue is expected to drain in at most two claims
        pytest.fail("Local bookkeeping outbox did not drain within the fixed bound")
    finalized = _process(
        value,
        ["close-tick", "--request-digest", request_digest, "--phase", "finalize"],
    )
    assert finalized.returncode == 0, finalized.stdout + finalized.stderr
    receipt = json.loads(finalized.stdout)["receipt_digest"]
    replay = _process(
        value,
        ["close-tick", "--request-digest", request_digest, "--phase", "finalize"],
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert json.loads(replay.stdout)["receipt_digest"] == receipt
    with sqlite3.connect(value["path"]) as db:
        rows = db.execute(
            "select portable_ref,state,authorship from knowledge_note order by portable_ref"
        ).fetchall()
        assert len(rows) == 6
        assert all(row[1:] == ("inbox", "generated") for row in rows)
        assert db.execute("select count(*) from close_receipt").fetchone()[0] == 1
    assert all((value["home"] / row[0]).is_file() for row in rows)


def test_real_process_missing_session_never_bootstraps_or_changes_rows(
    composition: dict[str, Any],
) -> None:
    value = composition
    before = logical_database_digest(value["path"])
    changed = dict(value)
    from dataclasses import replace

    changed["binding"] = replace(value["binding"], session_id=SESSION)
    result = _process(changed, ["doctor"])
    assert result.returncode == 70
    assert logical_database_digest(value["path"]) == before
    assert str(value["home"]) not in result.stdout + result.stderr
