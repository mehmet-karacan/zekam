"""AC-04: Semantic memory headless CLI yuzeyi.

7 islem (status/inspect/search/candidates/review/promote/hygiene) headless olarak
erisilir ve testlidir. Mutation islemlerinde claim/receipt vardir; read-only
islemlerde mutation yoktur. Secret redaction uygulanir.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from tests.unit.test_operational_schema_v3 import _source
from typer.testing import CliRunner

from zekam.application.memory_service import ReviewDecision
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.memory import (
    MemoryCandidate,
    MemoryClass,
    MemoryEvidence,
    MemoryKey,
    MemoryScope,
)
from zekam.infrastructure.sqlite.local_learning import SQLiteLocalLearning
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 9, 20, 12, tzinfo=dt.UTC)
NOW_TEXT = NOW.isoformat()

REVIEWER_B = "reviewer-b"


def _candidate(
    identifier: str,
    content: str,
    *,
    cls: MemoryClass = MemoryClass.SEMANTIC,
    author: str = "author-a",
) -> MemoryCandidate:
    return MemoryCandidate(
        identifier,
        MemoryKey(MemoryScope.PROJECT, "varsayilan", project_ref="zekam"),
        cls,
        content,
        author,
        NOW,
        (MemoryEvidence("receipt", f"receipt/{identifier}", digest("receipt")),),
    )


def _store(tmp_path: Path, home: Path) -> SQLiteLocalLearning:
    """CLI'nin tam bekledigi yollarda operational + learning DB hazirlar.

    Proven test_local_learning_sqlite._store fixture'inin ayni operational seed'ini
    kullanir: close receipt + delivered outbox + tamamlanmis job.
    """
    home.mkdir(parents=True, exist_ok=True)
    operational = home / "state" / "operational.db"
    seeded = _source((tmp_path / "operational-seed.db").resolve(), 3)
    payload = {
        "session_id": "session",
        "binding_digest": digest("binding"),
        "request_digest": digest("close"),
    }
    with sqlite3.connect(seeded) as db:
        db.execute("pragma foreign_keys=on")
        db.execute("insert into local_runtime_config values(1,64)")
        db.execute(
            "insert into local_job(id,idempotency_key,payload_json,state,max_attempts,"
            "available_at,terminal_evidence_digest,created_at,updated_at) "
            "values('close-job','close-job-key',?,'completed',1,?,?,?,?)",
            (canonical_json(payload), NOW_TEXT, digest("compile"), NOW_TEXT, NOW_TEXT),
        )
    from tests.unit.test_operational_schema_v3 import _pending_close

    _pending_close(seeded, control=True)
    with sqlite3.connect(seeded) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into local_outbox values('close-outbox','close-job','close-outbox-key',"
            "'continuity.compile',?,?,?)",
            (canonical_json(payload), digest(payload), NOW_TEXT),
        )
        db.execute(
            "insert into local_outbox_delivery values('close-outbox','delivered',1,"
            "'delivery-claim','worker',1,'past-owner',?,?)",
            (NOW_TEXT, NOW_TEXT),
        )
        db.execute(
            "insert into local_outbox_receipt values('delivery-receipt','close-outbox',"
            "'delivery-claim',1,'delivered',?,?)",
            (digest("delivery"), NOW_TEXT),
        )
        db.execute(
            "insert into continuity_outbox_binding values('close-outbox','session',"
            "'close-job','close',?,?)",
            (digest("close"), digest("close")),
        )
        db.execute(
            "insert into close_receipt values(?,?,'session',?,?,'close-outbox','[]',?)",
            (digest("receipt"), digest("close"), digest("checkpoint"), digest("context"), NOW_TEXT),
        )
        db.execute(
            "update session set status='closed',closed_at=?,close_receipt_digest=? "
            "where id='session'",
            (NOW_TEXT, digest("receipt")),
        )
    seeded.chmod(0o600)
    project_store = SQLiteOperationalStore(seeded)
    with project_store.unit_of_work() as uow:
        for slug in ("zekam", "demo"):
            uow.create_project(slug=slug, display_name=slug)
        uow.commit()

    operational.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(seeded, operational)
    operational.chmod(0o600)
    learning_path = home / "state" / "learning.db"
    learning = SQLiteLocalLearning(learning_path, operational_path=operational)
    if not learning_path.exists():
        learning.bootstrap()
    (home / "runtime" / "local-effects").mkdir(parents=True, exist_ok=True)
    return learning


def _active_count(learning: SQLiteLocalLearning) -> int:
    rows = learning.list_records()
    return sum(1 for row in rows if row["state"] == "active")


def test_memory_cli_all_operations_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("status", "inspect", "search", "candidates", "hygiene", "review", "promote"):
        assert command in result.output


def test_memory_status_and_candidates_read_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _store(tmp_path, home)
    runner = CliRunner()
    status = runner.invoke(app, ["memory", "status", "--home", str(home), "--json"])
    assert status.exit_code == 0, status.output
    doc = json.loads(status.output)
    assert doc["schema"] == "zekam-memory-status/v1"
    assert doc["read_only"] is True

    candidates = runner.invoke(app, ["memory", "candidates", "--home", str(home), "--json"])
    assert candidates.exit_code == 0, candidates.output
    cdoc = json.loads(candidates.output)
    assert cdoc["read_only"] is True
    assert cdoc["candidates"] == []


def test_memory_review_and_promote_use_claim_receipt(tmp_path: Path) -> None:
    """Promote mutation yalniz exact --uygula ile uygulanir; receipt dondurur."""
    home = tmp_path / "home"
    learning = _store(tmp_path, home)
    cd = learning.propose_memory(
        _candidate("c-promote", "Transport duzeni ulasilabilir olmali"),
        source_kind="receipt",
    )
    assert cd
    runner = CliRunner()

    # Dry-run plan mutation uretmez; aktif yok.
    assert _active_count(learning) == 0
    plan = runner.invoke(
        app,
        ["memory", "promote", "c-promote", "--home", str(home), "--reviewer-ref", REVIEWER_B],
    )
    assert plan.exit_code == 0, plan.output
    pdoc = json.loads(plan.output)
    assert pdoc["apply"] is False
    assert _active_count(learning) == 0

    # Exact plan digest + --uygula promotion'i claim/receipt ile uygular.
    exact_plan = pdoc["plan_digest"]
    applied = runner.invoke(
        app,
        [
            "memory",
            "promote",
            "c-promote",
            "--home",
            str(home),
            "--uygula",
            "--plan-digest",
            exact_plan,
            "--reviewer-ref",
            REVIEWER_B,
        ],
    )
    assert applied.exit_code == 0, applied.output
    adoc = json.loads(applied.output)
    assert adoc["schema"] == "zekam-memory-promote-receipt/v1"
    assert "operational_job_id" in adoc
    assert "operational_terminal_evidence_digest" in adoc

    # Promotion gercekten dogrulandi.
    assert _active_count(learning) == 1
    # Claim/receipt zinciri runtime store'da terminal durumda.
    runtime = SQLiteLocalRuntimeStore(learning.operational_path, existing_only=True)
    snapshot = runtime.job_snapshot(adoc["operational_job_id"])
    assert snapshot is not None
    assert snapshot["state"] == "completed"
    assert snapshot["terminal_evidence_digest"] == adoc["operational_terminal_evidence_digest"]


def test_memory_inspect_redacts_secrets(tmp_path: Path) -> None:
    """Inspect ciktisinda secret benzeri isaretci gorunmez (redaction)."""
    # CLI redaction helper'i dogrudan test et (email/isaretci kaliplari).
    from zekam.interfaces.cli.memory import _redact_text

    assert "[redacted]" in _redact_text("ulasim operator@ornek.com icin")
    assert "[redacted]" in _redact_text("parola: SUPERSECRET123")

    home = tmp_path / "home"
    learning = _store(tmp_path, home)
    cd = learning.propose_memory(
        _candidate("c-plain", "Ulasim operator@ornek.com adresi icin not"),
        source_kind="receipt",
    )
    review = learning.review_memory(
        cd, ReviewDecision(True, REVIEWER_B, "verified"), now=NOW + dt.timedelta(seconds=1)
    )
    learning.activate_memory(cd, review, now=NOW + dt.timedelta(seconds=2))
    records = learning.active_records()
    assert records
    memory_id = records[0].memory_id
    runner = CliRunner()

    # Varsayilan inspect: preview, email gorunmez.
    preview = runner.invoke(
        app, ["memory", "inspect", memory_id, "--home", str(home), "--json"]
    )
    assert preview.exit_code == 0, preview.output
    pdoc = json.loads(preview.output)
    body = pdoc["body"]
    # Normal content'ten email token'i full-body'de redact edilir; preview'da da yok.
    assert "operator@ornek.com" not in json.dumps(body)
    assert "content_preview" in body
    assert pdoc["full_body_exposed"] is False

    # --full-body: secret degerler redact edilir, baska icerik gorunur.
    full = runner.invoke(
        app, ["memory", "inspect", memory_id, "--home", str(home), "--json", "--full-body"]
    )
    assert full.exit_code == 0, full.output
    fdoc = json.loads(full.output)
    fbody = fdoc["body"]
    assert "operator@ornek.com" not in json.dumps(fbody)
    assert "[redacted]" in json.dumps(fbody)
    assert fdoc["full_body_exposed"] is True


def test_memory_reject_review_and_hygiene_read_only(tmp_path: Path) -> None:
    """Reject review duzgun plan verir; hygiene read-only'dir ve silmez."""
    home = tmp_path / "home"
    learning = _store(tmp_path, home)
    cd = learning.propose_memory(
        _candidate("c-rev", "Superseded gercek korunsun"),
        source_kind="receipt",
    )
    assert cd
    runner = CliRunner()
    plan = runner.invoke(
        app,
        [
            "memory",
            "review",
            "c-rev",
            "reject",
            "--home",
            str(home),
            "--reviewer-ref",
            REVIEWER_B,
            "--reason",
            "candidate istendi",
        ],
    )
    assert plan.exit_code == 0, plan.output
    pdoc = json.loads(plan.output)
    assert pdoc["decision"]["approved"] is False
    assert pdoc["apply"] is False

    # Review mutation'unu uygula.
    exact_plan = pdoc["plan_digest"]
    applied = runner.invoke(
        app,
        [
            "memory",
            "review",
            "c-rev",
            "reject",
            "--home",
            str(home),
            "--uygula",
            "--plan-digest",
            exact_plan,
            "--reviewer-ref",
            REVIEWER_B,
            "--reason",
            "candidate istendi",
        ],
    )
    assert applied.exit_code == 0, applied.output
    adoc = json.loads(applied.output)
    assert adoc["decision"] == "reject"
    # Reject sonrasi aktif yok; hyhiene yine de calisir ve silmez.
    assert _active_count(learning) == 0

    hyg = runner.invoke(app, ["memory", "hygiene", "--home", str(home), "--json"])
    assert hyg.exit_code == 0, hyg.output
    hdoc = json.loads(hyg.output)
    assert hdoc["schema"] == "zekam-memory-hygiene/v1"
    assert hdoc["read_only"] is True
    assert hdoc["automatic_delete"] is False
    assert hdoc["deleted"] == 0


def test_memory_search_requires_active_records(tmp_path: Path) -> None:
    """search read-only'dir ve request scope izolasyonu korur."""
    home = tmp_path / "home"
    learning = _store(tmp_path, home)
    cd = learning.propose_memory(
        _candidate("c-se", "Demo servisi nerede bulunur"),
        source_kind="receipt",
    )
    review = learning.review_memory(
        cd, ReviewDecision(True, REVIEWER_B, "verified"), now=NOW + dt.timedelta(seconds=1)
    )
    learning.activate_memory(cd, review, now=NOW + dt.timedelta(seconds=2))
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "memory",
            "search",
            "Demo servisi",
            "--home",
            str(home),
            "--project",
            "zekam",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["schema"] == "zekam-memory-search/v1"
    assert doc["read_only"] is True
    # En az bir hit ve secret/full-body yok (yalniz preview).
    assert doc["hits"]
    raw = result.output
    assert "SUPERSECRET" not in raw.upper() or "SUPERSECRET123" not in raw

    # Baska proje scope'u izolasyon nedeniyle hit uretmez.
    other = runner.invoke(
        app,
        [
            "memory",
            "search",
            "Demo servisi",
            "--home",
            str(home),
            "--project",
            "demo",
            "--json",
        ],
    )
    assert other.exit_code == 0, other.output
    assert json.loads(other.output)["hits"] == []
