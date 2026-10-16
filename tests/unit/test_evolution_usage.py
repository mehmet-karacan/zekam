"""Offline regression sources for unknown and bounded report measurements."""

from __future__ import annotations

import sqlite3

import pytest

from zekam.application.composition import build_context
from zekam.application.evolution_runtime import _operational_usage, build_evolution_report
from zekam.domain.canonical import canonical_json, digest


def _report(home):
    return build_evolution_report(
        build_context(home=home),
        plan={
            "state": "setup-required",
            "setup_gaps": ("native",),
            "blockers": (),
            "runtime": {"running_jobs": 0},
            "supervisor": {"state": "absent"},
            "permissions": {"budget_limits": {}},
        },
    )


def _evaluations(home, bodies, *, raw_override=None):
    state = home / "state"
    state.mkdir(parents=True)
    with sqlite3.connect(state / "improvement.db") as db:
        db.executescript(
            "create table improvement_candidate(candidate_digest text,change_class text,"
            "created_at text,failure_card_digest text,baseline_aggregate_digest text);"
            "create table improvement_evaluation(evaluation_digest text,candidate_digest text,"
            "after_aggregate_digest text,state text,finished_at text,body_json text);"
            "create table improvement_review(candidate_digest text,approved integer);"
        )
        candidate = digest("candidate")
        db.execute(
            "insert into improvement_candidate values(?,?,?,?,?)",
            (candidate, "AUTO_SAFE", "2026-10-16T00:00:00Z", digest("failure"), digest("before")),
        )
        for index, body in enumerate(bodies):
            value = body | {"sample": index}
            db.execute(
                "insert into improvement_evaluation values(?,?,?,?,?,?)",
                (
                    digest(value),
                    candidate,
                    digest("after"),
                    "improved",
                    "2026-10-16T00:00:00Z",
                    canonical_json(value) if raw_override is None else raw_override(value),
                ),
            )


def test_missing_stores_report_unknown_measurements_and_budget(tmp_path):
    report = _report(tmp_path / "home")
    usage = report["actual_usage"]
    assert usage["available"] is False
    assert usage["terminal_runs"] is None
    assert usage["tokens"] is None and report["budget"]["usage"]["tokens"] is None
    assert report["budget"]["available"] is False
    assert usage["evaluations"] == dict.fromkeys(("provider_calls", "tokens", "cost_micros"))
    assert usage["evaluations_coverage"]["complete"] is False


@pytest.mark.parametrize("bad", [None, True, "42", -1, 1.5, (1 << 63)])
def test_unmeasured_or_invalid_tokens_cannot_become_zero_or_coerced_number(tmp_path, bad):
    home = tmp_path / "home"
    _evaluations(
        home, [{"actual_provider_calls": 0, "actual_tokens": bad, "actual_cost_micros": 7}]
    )
    report = _report(home)
    assert report["evaluation_pairs"][0]["actual_usage"] == {
        "provider_calls": 0,
        "tokens": None,
        "cost_micros": 7,
    }
    assert report["actual_usage"]["evaluations"]["tokens"] is None
    coverage = report["actual_usage"]["evaluations_coverage"]
    assert coverage["complete"] is False
    assert coverage["unknown_measurements"]["tokens"] == 1
    assert coverage["sample_totals_are_ledger_totals"] is False


def test_bounded_sample_is_not_ledger_wide_usage(tmp_path):
    home = tmp_path / "home"
    _evaluations(
        home, [{"actual_provider_calls": 1, "actual_tokens": 42, "actual_cost_micros": 7}] * 101
    )
    usage = _report(home)["actual_usage"]
    assert usage["evaluations"] == dict.fromkeys(("provider_calls", "tokens", "cost_micros"))
    coverage = usage["evaluations_coverage"]
    assert coverage["total_records"] == 101 and coverage["included_records"] == 100
    assert coverage["omitted_or_invalid_records"] == 1
    assert coverage["observed_sample_totals"]["tokens"] == 4200
    assert coverage["complete"] is False


def test_known_empty_evaluation_table_is_measured_zero(tmp_path):
    home = tmp_path / "home"
    _evaluations(home, [])
    usage = _report(home)["actual_usage"]
    assert usage["evaluations"] == {"provider_calls": 0, "tokens": 0, "cost_micros": 0}
    assert usage["evaluations_coverage"]["complete"] is True


@pytest.mark.parametrize("kind", ["duplicate", "hash", "oversize", "nonfinite"])
def test_bad_evaluation_body_does_not_create_complete_zero_usage(tmp_path, kind):
    def corrupt(body):
        raw = canonical_json(body)
        if kind == "duplicate":
            return raw[:-1] + ',"actual_tokens":42}'
        if kind == "hash":
            return canonical_json(body | {"actual_tokens": 100})
        if kind == "nonfinite":
            return raw[:-1] + ',"extra":NaN}'
        return " " * 1_048_577

    home = tmp_path / "home"
    _evaluations(
        home,
        [{"actual_provider_calls": 1, "actual_tokens": 42, "actual_cost_micros": 7}],
        raw_override=corrupt,
    )
    report = _report(home)
    assert report["evaluation_pairs"] == []
    assert report["actual_usage"]["evaluations"]["tokens"] is None
    assert report["actual_usage"]["evaluations_coverage"]["omitted_or_invalid_records"] == 1


def _terminal_database(path):
    db = sqlite3.connect(path)
    db.execute(
        "create table evolution_child_terminal(reservation_id text,status text,"
        "provider_calls integer,tokens integer,duration_seconds integer,cost_micros integer,"
        "disk_bytes integer)"
    )
    return db


def test_empty_terminal_ledger_has_real_zero_recorded_usage(tmp_path):
    path = tmp_path / "operational.db"
    with _terminal_database(path):
        pass
    usage = _operational_usage(path)
    assert usage["available"] is True and usage["tokens"] == 0
    assert usage["scope"] == "recorded-child-terminals"


def test_unknown_effect_status_preserves_actual_recorded_consumption(tmp_path):
    path = tmp_path / "operational.db"
    with _terminal_database(path) as db:
        db.execute("insert into evolution_child_terminal values('r','unknown',1,42,3,7,9)")
    usage = _operational_usage(path)
    assert usage["available"] is True and usage["unknown_runs"] == 1
    assert usage["tokens"] == 42
    assert usage["scope"] == "recorded-child-terminals"


def test_terminal_census_budget_never_returns_partial_zero_usage(tmp_path):
    path = tmp_path / "operational.db"
    with _terminal_database(path) as db:
        db.executemany(
            "insert into evolution_child_terminal values(?,'completed',0,1,0,0,0)",
            ((str(index),) for index in range(10_001)),
        )
    usage = _operational_usage(path)
    assert usage["available"] is False and usage["tokens"] is None
    assert usage["reason"] == "terminal-read-budget-exceeded"


def test_bad_recorded_usage_does_not_crash_or_report_coerced_total(tmp_path):
    path = tmp_path / "operational.db"
    with _terminal_database(path) as db:
        db.execute("insert into evolution_child_terminal values('r','completed',1,'unknown',3,7,9)")
    usage = _operational_usage(path)
    assert usage["available"] is False and usage["tokens"] is None
    assert usage["reason"] == "terminal-usage-invalid"
