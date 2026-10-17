"""Native candidate creation requires local terminal evidence, never implicit activation."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.memory_service import memory_observation_evidence
from zekam.domain.memory import MemoryCandidate, MemoryClass, MemoryKey, MemoryScope
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.local_learning import SQLiteLocalLearning
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.integration


def _home(tmp_path: Path) -> tuple[Path, SQLiteLocalLearning]:
    home = tmp_path / "home"
    initialized = CliRunner().invoke(app, ["init", "--home", str(home)])
    assert initialized.exit_code == 0, initialized.output
    return home, SQLiteLocalLearning(
        (home / "state" / "learning.db").resolve(),
        operational_path=(home / "state" / "operational.db").resolve(),
    )


def _apply(runner: CliRunner, args: list[str]) -> dict:
    planned = runner.invoke(app, args)
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    applied = runner.invoke(app, args + ["--uygula", "--plan-digest", plan["plan_digest"]])
    assert applied.exit_code == 0, applied.output
    return json.loads(applied.output)


def test_fresh_home_proposal_replay_and_reviewed_correction(tmp_path: Path) -> None:
    home, store = _home(tmp_path)
    runner = CliRunner()
    text = "Yanitlar kisa ve Turkce olsun."
    args = ["memory", "propose", text, "--home", str(home)]
    planned = runner.invoke(app, args)
    assert planned.exit_code == 0, planned.output
    assert text not in planned.output
    assert store.list_candidates() == ()
    plan = json.loads(planned.output)
    refused = runner.invoke(app, args + ["--uygula", "--plan-digest", "sha256:" + "0" * 64])
    assert refused.exit_code != 0
    assert store.list_candidates() == ()
    result = _apply(runner, args)
    assert result["state"] == "candidate"
    assert result["operational_terminal_evidence_digest"] == result["receipt_digest"]
    assert store.active_records() == ()
    assert store.inspect_candidate(result["candidate_digest"])["source_kind"] == "observation-summary"
    replay = _apply(runner, args)
    assert replay == result
    assert len(store.list_candidates()) == 1
    with sqlite3.connect(store.operational_path) as db:
        assert db.execute("select count(*) from close_receipt").fetchone()[0] == 0
        jobs = db.execute("select payload_json from local_job").fetchall()
        assert len(jobs) == 2
        assert all(text not in row[0] for row in jobs)
    promoted = _apply(runner, ["memory", "promote", result["candidate_id"], "--home", str(home)])
    old = store.list_records()[0]["revision_digest"]
    replacement = _apply(runner, ["memory", "propose", "Yanitlar ayrintili ve Turkce olsun.", "--home", str(home)])
    corrected = _apply(runner, ["memory", "promote", replacement["candidate_id"], "--supersedes", old, "--home", str(home)])
    assert corrected["revision_digest"] != promoted["revision_digest"]
    assert len(store.active_records()) == 1
    assert store.list_records()[0]["revision"] == 2


def test_forged_or_missing_observation_never_becomes_candidate(tmp_path: Path) -> None:
    _home_path, store = _home(tmp_path)
    candidate = MemoryCandidate(
        "forged", MemoryKey(MemoryScope.GLOBAL_USER, "varsayilan"),
        MemoryClass.PREFERENCE, "Kisa yanit", "cli-user", dt.datetime.now(dt.UTC),
    )
    candidate = replace(candidate, evidence=(memory_observation_evidence(candidate),))
    with pytest.raises(PolicyViolation, match="terminal runtime receipt"):
        store.propose_memory(candidate, source_kind="observation-summary")
    assert store.list_candidates() == ()


@pytest.mark.parametrize("extra,content", [
    (["--project", "unregistered"], "Kisa yanit"),
    (["--class", "failure"], "Kisa yanit"),
    ([], "password=not-a-real-secret"),
    ([], " " * 5),
    ([], "x" * 4001),
])
def test_invalid_proposal_is_rejected_before_runtime_effect(tmp_path: Path, extra: list[str], content: str) -> None:
    home, store = _home(tmp_path)
    result = CliRunner().invoke(app, ["memory", "propose", content, "--home", str(home), *extra])
    assert result.exit_code != 0
    assert store.list_candidates() == ()
    with sqlite3.connect(store.operational_path) as db:
        assert db.execute("select count(*) from local_job").fetchone()[0] == 0
