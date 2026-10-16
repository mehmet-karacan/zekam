"""Control history integrity; all stores are isolated SQLite fixtures."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from tests.integration.test_local_improvement import _store
from zekam.application.evolution_runtime import _read_evolution_control, effective_evolution_state
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.evolution_control import read_control_chain


def _database(path=":memory:"):
    db = sqlite3.connect(path)
    db.execute(
        "create table evolution_control_event(event_digest text,ordinal integer,state text,"
        "previous_event_digest text,reason text,occurred_at text,body_json text)"
    )
    return db


def _event(ordinal=1, state="paused", previous=None):
    return {
        "schema": "zekam-evolution-control-event/v1",
        "ordinal": ordinal,
        "state": state,
        "previous_event_digest": previous,
        "reason": "owner-control",
        "enable_plan_digest": None,
        "admission_evidence": None,
        "bootstrap_settlement": None,
        "occurred_at": "2026-10-16T12:00:00+00:00",
        "grants_authority": False,
    }


def _insert(db, body, *, raw=None, header=None, state=None):
    value = header or digest(body)
    db.execute(
        "insert into evolution_control_event values(?,?,?,?,?,?,?)",
        (
            value,
            body["ordinal"],
            state or body["state"],
            body["previous_event_digest"],
            body["reason"],
            body["occurred_at"],
            canonical_json(body) if raw is None else raw,
        ),
    )
    db.commit()
    return value


def test_complete_chain_returns_verified_head_and_preserves_historical_optional_fields():
    with _database() as db:
        first = _event()
        first.pop("bootstrap_settlement")  # Pre-bootstrap v1 body remains readable.
        head = _insert(db, first)
        final = _event(2, "disabled", head)
        last = _insert(db, final)
        result = read_control_chain(db)
    assert result["state"] == "disabled" and result["ordinal"] == 2
    assert result["event_digest"] == last


@pytest.mark.parametrize(
    "mutation",
    ["header", "column", "ordinal", "previous", "authority", "timezone", "duplicate", "nonfinite"],
)
def test_self_consistency_and_column_chain_corruption_are_rejected(mutation):
    with _database() as db:
        body = _event()
        raw = header = state = None
        if mutation == "header":
            header = digest("wrong-header")
        elif mutation == "column":
            state = "observing"
        elif mutation == "ordinal":
            body["ordinal"] = 2
        elif mutation == "previous":
            body["previous_event_digest"] = digest("missing-parent")
        elif mutation == "authority":
            body["grants_authority"] = True
        elif mutation == "timezone":
            body["occurred_at"] = "2026-10-16T12:00:00"
        elif mutation == "duplicate":
            raw = canonical_json(body)[:-1] + ',"state":"observing"}'
        else:
            raw = canonical_json(body)[:-1] + ',"extra":NaN}'
        _insert(db, body, raw=raw, header=header, state=state)
        with pytest.raises(PolicyViolation):
            read_control_chain(db)


def test_valid_last_row_cannot_hide_corrupted_ancestor():
    with _database() as db:
        first = _event()
        head = _insert(db, first, raw=canonical_json(first | {"reason": "changed"}))
        _insert(db, _event(2, "disabled", head))
        with pytest.raises(PolicyViolation, match="row/hash/chain drift"):
            read_control_chain(db)


@pytest.mark.parametrize("initial", ["paused", "disabled"])
def test_self_consistent_resume_without_required_authority_is_rejected(initial):
    with _database() as db:
        parent = _insert(db, _event(state=initial))
        _insert(db, _event(2, "observing", parent))
        with pytest.raises(PolicyViolation, match="admission|enable plan"):
            read_control_chain(db)


def test_event_and_cumulative_byte_budget_never_return_partial_head():
    with _database() as db:
        parent = _insert(db, _event())
        _insert(db, _event(2, "disabled", parent))
        with pytest.raises(PolicyViolation, match="read budget"):
            read_control_chain(db, max_events=1)
        with pytest.raises(PolicyViolation, match="byte budget"):
            read_control_chain(db, max_history_bytes=1)


def test_store_writer_and_read_only_runtime_share_same_integrity_gate(tmp_path):
    store, _card, _aggregates = _store(tmp_path)
    first = store.set_evolution_control_state(
        "paused", reason="owner-pause", now=dt.datetime.now(dt.UTC)
    )
    forged = _event(2, "observing", first["event_digest"])
    with sqlite3.connect(store.path) as db:
        _insert(db, forged)
    with pytest.raises(PolicyViolation, match="resume admission missing"):
        store.evolution_control_status()
    with pytest.raises(PolicyViolation, match="resume admission missing"):
        store.set_evolution_control_state(
            "disabled", reason="owner-disable", now=dt.datetime.now(dt.UTC)
        )
    report = _read_evolution_control(store.path)
    assert report == {"state": "degraded", "reason": "control-chain-invalid"}
    with sqlite3.connect(store.path) as db:
        assert db.execute("select count(*) from evolution_control_event").fetchone()[0] == 2


def test_invalid_admission_digest_type_is_a_bounded_policy_error():
    with _database() as db:
        body = _event()
        body["enable_plan_digest"] = {"not": "digest-text"}
        _insert(db, body)
        with pytest.raises(PolicyViolation, match="digest must be text"):
            read_control_chain(db)


@pytest.mark.parametrize(
    "control,expected",
    [
        ("degraded", "degraded"),
        ("setup-required", "setup-required"),
        ("future-state", "degraded"),
        (None, "degraded"),
    ],
)
def test_invalid_control_cannot_fall_back_to_running_or_observing(control, expected):
    plan = {
        "state": "observing",
        "runtime": {"running_jobs": 1},
        "supervisor": {"state": "matching"},
        "blockers": (),
        "setup_gaps": (),
    }
    report = {"ledger_state": "observing", "control": {"state": control}}
    assert effective_evolution_state(plan, report) == expected
