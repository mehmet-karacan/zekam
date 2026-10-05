"""Native start/checkpoint/close/resume: gercek git, SQLite, CAS ve lifecycle ledger (A24).

Bir host (ornegin Claude Code) checkpoint yazar, baska bir host (ornegin Codex) resume eder:
ayni dogrulanmis Work/proje/source revision anlasilir; hayali session, OpenCode kimligi, review
veya yetki uretilmez. Model/provider/ag cagrisi yoktur.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.composition import build_context
from zekam.application.opencode_lifecycle import lifecycle_root, recent_events
from zekam.application.workspace_resume import build_resume_packet
from zekam.domain.canonical import digest
from zekam.domain.unit_test_engineering import UnitTestBudget, UnitTestRequest
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli.main import app

runner = CliRunner()
pytestmark = pytest.mark.integration


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass
class World:
    home: Path
    repo: Path
    database: Path
    project_id: str
    work_id: str
    work_revision: int
    revision: str
    evidence_object: str
    request_digest: str


@pytest.fixture
def world(tmp_path: Path) -> World:
    repo = tmp_path / "proje"
    repo.mkdir()
    (repo / "A.java").write_text("class A {}\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ilk")
    home = tmp_path / "home"
    context = build_context(home=home)
    database = context.settings.database.sqlite_path(context.home)
    assert schema.bootstrap_v6(database).schema_ok
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="fixture", display_name="Fixture")
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Native continuity",
            state="active",
            payload_digest=digest("work"),
        )
        request = UnitTestRequest.with_defaults(
            project_id=project.id,
            source_binding_id="binding-1",
            source_revision=_git(repo, "rev-parse", "HEAD"),
            source_files=["A.java"],
            percent="50",
            budget=UnitTestBudget(1, 60, 120),
        )
        uow.unit_test_ledger().register_request(request, now=dt.datetime.now(dt.UTC))
        uow.commit()
    objects = LocalContentAddressedStore(
        (context.home / context.settings.object_store_relative).resolve()
    ).ensure()
    info = objects.put(b'{"kanit":"gercek"}', media_type="application/json")
    return World(
        context.home,
        repo,
        database,
        project.id,
        work.id,
        work.revision,
        _git(repo, "rev-parse", "HEAD"),
        info.digest,
        request.request_digest,
    )


def _invoke(args: list[str], *, ok: bool = True):
    result = runner.invoke(app, ["continuity", "native", *args])
    if ok:
        assert result.exit_code == 0, result.output
    return result


def _json(result) -> dict:
    return json.loads(result.stdout)


def _error(result) -> dict:
    text = result.output.strip()
    return json.loads(text[text.index("{") :])


def _start(world: World, client: str = "claude-code", *extra: str) -> dict:
    return _json(
        _invoke(
            [
                "start",
                "--client",
                client,
                "--project",
                "fixture",
                "--work-item",
                world.work_id,
                "--home",
                str(world.home),
                *extra,
            ]
        )
    )


def _checkpoint_args(world: World, session_id: str, client: str = "claude-code") -> list[str]:
    return [
        "--session-id",
        session_id,
        "--client",
        client,
        "--project",
        "fixture",
        "--work-item",
        world.work_id,
        "--source-root",
        str(world.repo),
        "--home",
        str(world.home),
    ]


def _resume(world: World, *extra: str) -> dict:
    return _json(
        _invoke(
            ["resume", "--source-root", str(world.repo), "--home", str(world.home), *extra],
        )
    )


def test_checkpoint_on_one_host_resumes_on_another_with_same_verified_state(world: World) -> None:
    started = _start(world, "claude-code", "--client-version", "2.1.0")
    session_id = started["session_id"]
    assert started["session_origin"] == "zekam-generated"
    assert started["client"]["native_session_id"] is None  # uydurma native kimlik yok
    assert started["client"]["version"] == "2.1.0"
    assert "opencode" not in session_id
    checkpoint = _json(
        _invoke(
            [
                "checkpoint",
                *_checkpoint_args(world, session_id),
                "--evidence-ref",
                f"object:{world.evidence_object}",
                "--evidence-ref",
                f"unit-test:{world.request_digest}",
                "--completed",
                "Hedef sinifin olcumu alindi",
                "--pending",
                "Dal testleri eksik",
                "--next-safe-action",
                "CalculatorBranchTest ekle ve yeniden olc",
                "--client-version",
                "2.1.0",
            ]
        )
    )
    assert checkpoint["work_transition_verified"] is False
    assert checkpoint["grants_authority"] is False
    assert checkpoint["source"]["revision"] == world.revision
    assert {item["kind"] for item in checkpoint["evidence"]} == {"object", "unit-test"}

    # Baska host: yalniz paylasilan home + source root ile resume.
    resumed = _resume(world)
    assert resumed["status"] == "ready", resumed
    assert resumed["read_only"] is True and resumed["approval_inherited"] is False
    saved = resumed["checkpoint"]
    assert saved["client"]["name"] == "claude-code" and saved["client"]["version"] == "2.1.0"
    assert saved["recorded_source_revision"] == world.revision
    assert saved["recorded_binding"]["work"]["work_item_id"] == world.work_id
    assert saved["self_reported"]["provenance"] == "client-self-reported"
    assert saved["self_reported"]["verified"] is False
    assert resumed["verification"]["source"]["state"] == "identical"
    assert resumed["verification"]["binding"]["state"] == "valid"
    assert {item["state"] for item in resumed["verification"]["evidence"]} == {"unchanged"}
    assert resumed["next_safe_action"] == "CalculatorBranchTest ekle ve yeniden olc"
    assert resumed["next_safe_action_source"] == "client-self-reported"

    # Mevcut ortak resume paketi ayni checkpoint'i gorur (ikinci store yok).
    packet = build_resume_packet(world.home)
    latest = packet["latest_semantic_checkpoint"]
    assert latest["agent"] == "claude-code"
    assert latest["completed"] == "Hedef sinifin olcumu alindi"
    assert latest["next_safe_action"] == "CalculatorBranchTest ekle ve yeniden olc"
    assert packet["next_safe_action"] == "CalculatorBranchTest ekle ve yeniden olc"
    # Tek store: yeni bir session/checkpoint dizini olusmadi.
    created = sorted(path.name for path in lifecycle_root(world.home).iterdir())
    assert all(name.endswith(".json") or name.startswith(".") for name in created)


def test_client_reported_native_session_id_is_kept_as_metadata(world: World) -> None:
    started = _start(world, "codex", "--native-session-id", "019f-abc", "--client-version", "0.9")
    assert started["session_origin"] == "client-reported"
    assert started["session_id"] == "native.codex.019f-abc"
    assert started["client"]["native_session_id"] == "019f-abc"
    result = _invoke(
        [
            "checkpoint",
            *_checkpoint_args(world, started["session_id"], "codex"),
            "--native-session-id",
            "019f-abc",
            "--pending",
            "devam",
        ]
    )
    assert _json(result)["client"]["provenance"] == "client-reported"
    mismatch = _invoke(
        [
            "checkpoint",
            *_checkpoint_args(world, started["session_id"], "codex"),
            "--native-session-id",
            "baska",
            "--pending",
            "devam",
        ],
        ok=False,
    )
    assert mismatch.exit_code == 70
    assert _error(mismatch)["reason"] == "native-session-id-mismatch"


def test_resume_without_checkpoint_invents_nothing(world: World) -> None:
    resumed = _resume(world)
    assert resumed["status"] == "no-checkpoint"
    assert resumed["checkpoint"] is None
    assert "uydurmayin" in resumed["next_safe_action"]
    assert recent_events(world.home, limit=10) == ()


def test_source_revision_drift_is_reported_not_trusted(world: World) -> None:
    session_id = _start(world)["session_id"]
    _invoke(["checkpoint", *_checkpoint_args(world, session_id), "--pending", "bekleyen"])
    (world.repo / "A.java").write_text("class A { int x; }\n", encoding="utf-8")
    changed = _resume(world)
    assert changed["status"] == "changed-since-checkpoint"
    assert changed["verification"]["source"]["state"] == "worktree-changed"
    _git(world.repo, "commit", "-aq", "-m", "ikinci")
    drift = _resume(world)
    assert drift["status"] == "source-drift"
    assert drift["verification"]["source"]["recorded_revision"] == world.revision
    assert drift["verification"]["source"]["current_revision"] != world.revision
    assert "dogrulanmamistir" in drift["next_safe_action"]
    assert drift["next_safe_action_source"] == "zekam-verification"


def test_unverifiable_evidence_is_rejected_before_any_event(world: World) -> None:
    session_id = _start(world)["session_id"]
    for ref, reason in (
        (f"object:sha256:{'0' * 64}", "evidence-object-missing-or-corrupt"),
        (f"unit-test:sha256:{'1' * 64}", "evidence-unit-test-request-unknown"),
        ("receipt:sha256:" + "2" * 64, "evidence-ref-kind-unsupported"),
        ("object:abc", "evidence-ref-digest-invalid"),
    ):
        result = _invoke(
            [
                "checkpoint",
                *_checkpoint_args(world, session_id),
                "--evidence-ref",
                ref,
                "--completed",
                "tamamlandi",
            ],
            ok=False,
        )
        assert result.exit_code == 70
        assert _error(result)["reason"] == reason
    kinds = [e["event_type"] for e in recent_events(world.home, limit=20)]
    assert kinds == ["session.created"]  # reddedilen checkpoint iz birakmadi


def test_evidence_removed_after_checkpoint_blocks_resume(world: World) -> None:
    session_id = _start(world)["session_id"]
    _invoke(
        [
            "checkpoint",
            *_checkpoint_args(world, session_id),
            "--evidence-ref",
            f"object:{world.evidence_object}",
            "--completed",
            "kanitli is",
        ]
    )
    hex_digest = world.evidence_object.removeprefix("sha256:")
    context = build_context(home=world.home)
    root = (context.home / context.settings.object_store_relative).resolve()
    removed = [p for p in root.rglob(f"{hex_digest}*") if p.is_file()]
    assert removed
    for path in removed:
        path.unlink()
    resumed = _resume(world)
    assert resumed["status"] == "evidence-missing"
    assert "iddiasini kullanmayin" in resumed["next_safe_action"]


def test_tampered_checkpoint_object_is_integrity_error(world: World) -> None:
    session_id = _start(world)["session_id"]
    result = _json(
        _invoke(["checkpoint", *_checkpoint_args(world, session_id), "--pending", "bekleyen"])
    )
    hex_digest = result["checkpoint_digest"].removeprefix("sha256:")
    context = build_context(home=world.home)
    root = (context.home / context.settings.object_store_relative).resolve()
    (target,) = [p for p in root.rglob(f"{hex_digest}.bin") if p.is_file()]
    target.write_bytes(b'{"schema":"zekam-native-checkpoint/v1","oynandi":true}')
    resumed = _resume(world)
    assert resumed["status"] == "integrity-error"
    assert resumed["checkpoint"] is None


def test_closed_or_cancelled_work_and_close_semantics(world: World) -> None:
    session_id = _start(world)["session_id"]
    closed = _json(
        _invoke(
            [
                "close",
                *_checkpoint_args(world, session_id),
                "--completed",
                "kismen bitti",
                "--pending",
                "geri kalan",
            ]
        )
    )
    assert closed["session_closed"] is True and closed["work_transition_verified"] is False
    again = _invoke(
        ["checkpoint", *_checkpoint_args(world, session_id), "--pending", "x"], ok=False
    )
    assert _error(again)["reason"] == "session-already-closed"
    resumed = _resume(world)
    assert resumed["status"] == "ready" and resumed["checkpoint"]["kind"] == "close"
    # Work baska yerde iptal edildi: checkpoint beyani artik gecerli devam sebebi degil.
    store = SQLiteOperationalStore(world.database)
    with store.unit_of_work() as uow:
        uow.transition_work(
            work_item_id=world.work_id,
            expected_revision=world.work_revision,
            to_state="cancelled",
            payload_digest=digest("p"),
            event_digest=digest("e"),
        )
        uow.commit()
    assert _resume(world)["status"] == "work-closed"


def test_summaries_reject_secret_like_paths_and_multiline_without_echo(world: World) -> None:
    session_id = _start(world)["session_id"]
    secret = "api_key=" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    cases = {
        secret: "completed-secret-like",
        "dosya C:\\Users\\biri\\x.txt degisti": "completed-absolute-path",
        "satir1\nsatir2": "completed-bound-exceeded",
        "x" * 501: "completed-bound-exceeded",
    }
    for text, reason in cases.items():
        result = _invoke(
            ["checkpoint", *_checkpoint_args(world, session_id), "--completed", text], ok=False
        )
        assert result.exit_code == 70
        assert _error(result)["reason"] == reason
        assert secret not in result.output
    empty = _invoke(["checkpoint", *_checkpoint_args(world, session_id)], ok=False)
    assert _error(empty)["reason"] == "checkpoint-summary-required"


def test_binding_and_source_guards(world: World, tmp_path: Path) -> None:
    session_id = _start(world)["session_id"]

    def failing(args: list[str]) -> str:
        result = _invoke(["checkpoint", *args], ok=False)
        assert result.exit_code == 70
        return _error(result)["reason"]

    base = _checkpoint_args(world, session_id)
    wrong_project = [*base]
    wrong_project[wrong_project.index("--project") + 1] = "yok-proje"
    assert failing([*wrong_project, "--pending", "x"]) == "project-or-work-not-found"
    other_work = [*base]
    other_work[other_work.index("--work-item") + 1] = "01a10959-24e8-7f06-8e51-d4ca73b9f21b"
    assert failing([*other_work, "--pending", "x"]) == "project-or-work-not-found"
    subdir = world.repo / "alt"
    subdir.mkdir()
    sub = [*base]
    sub[sub.index("--source-root") + 1] = str(subdir)
    assert failing([*sub, "--pending", "x"]) == "source-root-not-exact-git-toplevel"
    plain = tmp_path / "git-degil"
    plain.mkdir()
    nogit = [*base]
    nogit[nogit.index("--source-root") + 1] = str(plain)
    assert failing([*nogit, "--pending", "x"]) in {
        "source-git-unavailable",
        "source-root-not-exact-git-toplevel",
    }
    relative = [*base]
    relative[relative.index("--source-root") + 1] = "proje"
    assert failing([*relative, "--pending", "x"]) == "source-root-absolute-required"
    unstarted = [*base]
    unstarted[unstarted.index("--session-id") + 1] = "native.claude-code.hic-baslamadi"
    assert failing([*unstarted, "--pending", "x"]) == "session-not-started"
    foreign = [*base]
    foreign[foreign.index("--session-id") + 1] = "opencode-session-1"
    assert failing([*foreign, "--pending", "x"]) == "session-not-native"
    other_client = _checkpoint_args(world, session_id, "codex")
    assert failing([*other_client, "--pending", "x"]) == "session-client-mismatch"
    invalid_client = _checkpoint_args(world, session_id, "Claude Code!")
    assert failing([*invalid_client, "--pending", "x"]) == "client-name-invalid"


def test_checkpoint_is_idempotent_and_chain_is_valid(world: World) -> None:
    session_id = _start(world)["session_id"]
    args = ["checkpoint", *_checkpoint_args(world, session_id), "--pending", "ayni"]
    first = _json(_invoke(args))
    second = _json(_invoke(args))
    assert first["checkpoint_digest"] == second["checkpoint_digest"]
    assert first["event_digest"] == second["event_digest"]  # delivery_id replay: tek olay
    events = [e for e in recent_events(world.home, limit=20) if e["session_id"] == session_id]
    assert [e["event_type"] for e in sorted(events, key=lambda e: e["sequence"])] == [
        "session.created",
        "session.checkpoint",
    ]


def test_help_documents_native_commands() -> None:
    result = runner.invoke(app, ["continuity", "native", "--help"])
    assert result.exit_code == 0
    for name in ("start", "checkpoint", "close", "resume"):
        assert name in result.output
    detail = runner.invoke(app, ["continuity", "native", "checkpoint", "--help"])
    for option in ("--session-id", "--source-root", "--evidence-ref", "--next-safe-action"):
        assert option in detail.output
