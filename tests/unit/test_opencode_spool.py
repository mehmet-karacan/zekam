from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.opencode_spool import (
    apply_legacy_candidate_cleanup,
    inspect_spool,
    plan_legacy_candidate_cleanup,
    plugin_spool_root,
)
from zekam.domain.errors import PolicyViolation

NOW = dt.datetime(2026, 8, 26, 10, 0, tzinfo=dt.UTC)


def _candidate(home: Path, *, pid: int = 999_999, age_seconds: int = 600) -> Path:
    token = str(uuid4())
    path = plugin_spool_root(home) / f".drain.candidate.{token}"
    path.mkdir(parents=True)
    (path / "owner.json").write_text(
        json.dumps({"pid": pid, "ownerToken": token}),
        encoding="utf-8",
    )
    modified = (NOW - dt.timedelta(seconds=age_seconds)).timestamp()
    os.utime(path, (modified, modified))
    return path


def test_exact_stale_candidates_are_quarantined_without_raw_delete(tmp_path: Path) -> None:
    first = _candidate(tmp_path)
    second = _candidate(tmp_path)
    plan = plan_legacy_candidate_cleanup(tmp_path, now=NOW)

    receipt = apply_legacy_candidate_cleanup(
        tmp_path,
        expected_plan_digest=plan.plan_digest,
        now=NOW,
    )

    assert len(plan.candidates) == 2
    assert receipt.moved == 2
    assert not first.exists() and not second.exists()
    quarantine = plugin_spool_root(tmp_path) / "quarantine"
    assert len(tuple(quarantine.glob("legacy-drain-candidate.*"))) == 2
    receipt_path = next(quarantine.glob("cleanup-receipt-*.json"))
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert document["raw_delete"] is False
    assert document["reversible"] is True
    status = inspect_spool(tmp_path, now=NOW)
    assert status.legacy_candidates == 0
    assert status.quarantine == 3


def test_cleanup_rejects_plan_digest_drift(tmp_path: Path) -> None:
    _candidate(tmp_path)
    plan = plan_legacy_candidate_cleanup(tmp_path, now=NOW)
    _candidate(tmp_path)

    with pytest.raises(PolicyViolation, match="plan digest drift"):
        apply_legacy_candidate_cleanup(
            tmp_path,
            expected_plan_digest=plan.plan_digest,
            now=NOW,
        )


def test_live_or_malformed_candidate_is_never_moved(tmp_path: Path) -> None:
    live = _candidate(tmp_path, pid=os.getpid())
    malformed = plugin_spool_root(tmp_path) / f".drain.candidate.{uuid4()}"
    malformed.mkdir()
    (malformed / "owner.json").write_text("{}", encoding="utf-8")

    plan = plan_legacy_candidate_cleanup(tmp_path, now=NOW)

    assert plan.candidates == ()
    assert plan.invalid_count == 2
    with pytest.raises(PolicyViolation, match="exact typed candidate"):
        apply_legacy_candidate_cleanup(
            tmp_path,
            expected_plan_digest=plan.plan_digest,
            now=NOW,
        )
    assert live.exists() and malformed.exists()


def test_unrecognized_spool_entry_blocks_cleanup(tmp_path: Path) -> None:
    root = plugin_spool_root(tmp_path)
    root.mkdir(parents=True)
    (root / "unexpected.bin").write_bytes(b"x")
    plan = plan_legacy_candidate_cleanup(tmp_path, now=NOW)

    assert plan.unrecognized_count == 1
    with pytest.raises(PolicyViolation, match="exact typed candidate"):
        apply_legacy_candidate_cleanup(
            tmp_path,
            expected_plan_digest=plan.plan_digest,
            now=NOW,
        )


def test_status_contains_counts_and_digests_but_not_candidate_names(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    document = inspect_spool(tmp_path, now=NOW).as_dict()

    assert document["legacy_candidates"] == 1
    assert document["eligible_legacy_candidates"] == 1
    assert document["cleanup_plan_digest"].startswith("sha256:")
    assert candidate.name not in json.dumps(document)


def test_queued_delivery_makes_spool_unhealthy(tmp_path: Path) -> None:
    root = plugin_spool_root(tmp_path)
    root.mkdir(parents=True)
    (root / "delivery.json").write_text("{}", encoding="utf-8")

    status = inspect_spool(tmp_path, now=NOW)

    assert status.queued == 1
    assert not status.healthy
