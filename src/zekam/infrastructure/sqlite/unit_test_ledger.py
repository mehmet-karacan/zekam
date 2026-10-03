"""Unit-test mühendisligi icin operational SQLite ledger (additive migration + repository).

Mevcut operational SQLite motoruna eklenen yalniz-ekleme (append-only) tablolar.
Evolution authority ile ayni sinir: DDL, ``operational`` migration zincirine kabul
edilecek surumlu bir paket olarak burada tanimlidir; repository cagiranin actigi
baglanti/transaction uzerinde calisir, ayri veritabani, PostgreSQL veya JSON state
dosyasi kullanmaz.

Migration/rollback politikasi:

- ``MIGRATION_NAME``/``migration_digest()`` degismez kimliktir; DDL degisirse yeni
  migration adi gerekir.
- Migration yalniz yeni tablolar/indeksler/trigger'lar ekler; mevcut satir degistirilmez.
- Rollback = migration oncesi alinan operational online backup'in geri yuklenmesi
  (``operational_backup``); tablo drop veya in-place downgrade yoktur.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from zekam.application.unit_test_ledger import AttemptReceiptRecord, LedgerWrite
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.domain.unit_test_engineering import (
    RESUMABLE_REASONS,
    AttemptState,
    CoverageMetric,
    CoverageObservation,
    CoveragePolicy,
    CoverageState,
    EvaluationVerdict,
    RatioThreshold,
    UnitTestAttempt,
    UnitTestBudget,
    UnitTestRequest,
    UnitTestStopReason,
    UnitTestTerminal,
    evaluate_scope,
)

MIGRATION_NAME = "operational-unit-test-engineering-v6"

UNIT_TEST_LEDGER_DDL: str = r"""
create table unit_test_request (
    request_digest text primary key check(length(request_digest)=71),
    project_id text not null references project(id),
    source_binding_id text not null,
    metric text not null check(metric in ('line','branch')),
    policy text not null check(policy in ('per-file','aggregate')),
    max_attempts integer not null check(max_attempts>0),
    body_json text not null,
    created_at text not null
) strict;
create table unit_test_attempt (
    attempt_id text primary key,
    request_digest text not null references unit_test_request(request_digest),
    ordinal integer not null check(ordinal>0),
    parent_attempt_id text references unit_test_attempt(attempt_id),
    plan_digest text not null check(length(plan_digest)=71),
    candidate_digest text not null check(length(candidate_digest)=71),
    effect_digest text not null check(length(effect_digest)=71),
    idempotency_key text not null unique,
    body_json text not null,
    created_at text not null,
    unique(request_digest, ordinal),
    check((ordinal=1 and parent_attempt_id is null)
          or (ordinal>1 and parent_attempt_id is not null))
) strict;
create table unit_test_attempt_receipt (
    attempt_id text primary key references unit_test_attempt(attempt_id),
    status text not null check(status in ('completed','failed','interrupted','unknown')),
    evidence_digest text not null check(length(evidence_digest)=71),
    created_at text not null
) strict;
create table unit_test_observation (
    attempt_id text not null references unit_test_attempt_receipt(attempt_id),
    source_file text not null,
    metric text not null check(metric in ('line','branch')),
    state text not null check(state in
        ('measured','not-applicable','not-measured','missing-report','mapping-error')),
    covered integer,
    missed integer,
    total integer,
    reason text not null,
    primary key(attempt_id, source_file, metric),
    check(
        (state='measured' and typeof(covered)='integer' and typeof(missed)='integer'
         and typeof(total)='integer' and covered>=0 and missed>=0 and total>0
         and covered+missed=total)
        or
        (state<>'measured' and covered is null and missed is null and total is null
         and length(reason)>0)
    )
) strict;
create table unit_test_terminal (
    request_digest text not null references unit_test_request(request_digest),
    terminal_seq integer not null check(terminal_seq>0),
    terminal_status text not null check(terminal_status in
        ('passed','blocked','budget-exhausted','manual-review')),
    stop_reason text not null check(stop_reason in
        ('target-reached','already-at-target','budget-exhausted','stagnation-review',
         'environment-missing','technology-unsupported','spec-ambiguous',
         'measurement-incomplete','production-defect','refactor-approval-required',
         'user-paused','user-cancelled','recovery-required')),
    receipt_digest text,
    evidence_digest text,
    final integer not null check(final in (0,1)),
    created_at text not null,
    primary key(request_digest, terminal_seq),
    check(stop_reason not in ('target-reached','already-at-target') or receipt_digest is not null),
    check((final=0) = (stop_reason in ('user-paused','recovery-required')))
) strict;
create unique index unit_test_terminal_final_idx
    on unit_test_terminal(request_digest) where final=1;
create index unit_test_attempt_request_idx on unit_test_attempt(request_digest, ordinal);
create trigger unit_test_request_no_update before update on unit_test_request begin
    select raise(abort,'unit_test_request append-only');
end;
create trigger unit_test_request_no_delete before delete on unit_test_request begin
    select raise(abort,'unit_test_request append-only');
end;
create trigger unit_test_attempt_no_update before update on unit_test_attempt begin
    select raise(abort,'unit_test_attempt append-only');
end;
create trigger unit_test_attempt_no_delete before delete on unit_test_attempt begin
    select raise(abort,'unit_test_attempt append-only');
end;
create trigger unit_test_attempt_receipt_no_update before update on unit_test_attempt_receipt begin
    select raise(abort,'unit_test_attempt_receipt append-only');
end;
create trigger unit_test_attempt_receipt_no_delete before delete on unit_test_attempt_receipt begin
    select raise(abort,'unit_test_attempt_receipt append-only');
end;
create trigger unit_test_observation_no_update before update on unit_test_observation begin
    select raise(abort,'unit_test_observation append-only');
end;
create trigger unit_test_observation_no_delete before delete on unit_test_observation begin
    select raise(abort,'unit_test_observation append-only');
end;
create trigger unit_test_terminal_no_update before update on unit_test_terminal begin
    select raise(abort,'unit_test_terminal append-only');
end;
create trigger unit_test_terminal_no_delete before delete on unit_test_terminal begin
    select raise(abort,'unit_test_terminal append-only');
end;
create trigger unit_test_attempt_after_final before insert on unit_test_attempt
when exists(select 1 from unit_test_terminal t
            where t.request_digest=new.request_digest and t.final=1)
begin
    select raise(abort,'unit_test_attempt after final terminal forbidden');
end;
"""


def migration_digest() -> str:
    """Migration baglama parmak izi (DDL degisirse degisir)."""

    return digest({"name": MIGRATION_NAME, "ddl": UNIT_TEST_LEDGER_DDL})


def apply_unit_test_ledger_ddl(connection: sqlite3.Connection) -> None:
    """DDL'i cagiranin transaction'i icinde uygular (``executescript`` commit etmez)."""

    if not connection.in_transaction:
        raise ConfigurationError("Unit-test ledger DDL acik migration transaction'i ister")
    pending = ""
    for line in UNIT_TEST_LEDGER_DDL.splitlines(keepends=True):
        pending += line
        if not sqlite3.complete_statement(pending):
            continue
        statement = pending.strip()
        pending = ""
        if statement:
            connection.execute(statement)
    if pending.strip():
        raise ConfigurationError("Unit-test ledger DDL tamamlanmamis statement")


def _iso(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValidationFailed("Ledger zamani timezone'lu olmali")
    return now.astimezone(UTC).isoformat()


def _request_from_row(body_json: str) -> UnitTestRequest:
    body = json.loads(body_json)
    threshold = body["threshold"]
    budget = body["budget"]
    return UnitTestRequest(
        project_id=body["project_id"],
        source_binding_id=body["source_binding_id"],
        source_revision=body["source_revision"],
        source_files=tuple(body["source_files"]),
        metric=CoverageMetric(body["metric"]),
        threshold=RatioThreshold(threshold[0], threshold[1]),
        policy=CoveragePolicy(body["policy"]),
        budget=UnitTestBudget(budget[0], budget[1], budget[2]),
        allowed_test_paths=tuple(body["allowed_test_paths"]),
        forbidden_paths=tuple(body["forbidden_paths"]),
        regression_scope=tuple(body["regression_scope"]),
        defaults_applied=tuple(body["defaults_applied"]),
        work_item_id=body.get("work_item_id"),
        plan_id=body.get("plan_id"),
        run_id=body.get("run_id"),
        source_snapshot_id=body.get("source_snapshot_id"),
        graph_generation_digest=body.get("graph_generation_digest"),
    )


def _attempt_from_row(row: sqlite3.Row | tuple[str, ...]) -> UnitTestAttempt:
    body = json.loads(str(row[0]))
    return UnitTestAttempt(
        attempt_id=body["attempt_id"],
        request_digest=body["request_digest"],
        ordinal=body["ordinal"],
        parent_attempt_id=body["parent_attempt_id"],
        plan_digest=body["plan_digest"],
        candidate_digest=body["candidate_digest"],
        idempotency_key=str(row[1]),
        changed_files=tuple((str(p), str(d)) for p, d in body["changed_files"]),
    )


class SQLiteUnitTestLedger:
    """Operational baglanti uzerinde claim-before-effect / terminal-receipt ledger'i.

    Cagiran bagli bir transaction acmissa (ornegin operational unit-of-work) yazimlar
    savepoint ile ona katilir ve commit/rollback cagirana aittir. Acik transaction
    yoksa her cagri kendi ``begin immediate`` transaction'ini acip commit eder.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection
        if self._db.execute("pragma foreign_keys").fetchone()[0] != 1:
            raise ConfigurationError("Unit-test ledger foreign key enforcement ister")

    @contextmanager
    def _tx(self) -> Iterator[None]:
        if self._db.in_transaction:
            self._db.execute("savepoint unit_test_ledger")
            try:
                yield
            except BaseException:
                self._db.execute("rollback to savepoint unit_test_ledger")
                self._db.execute("release savepoint unit_test_ledger")
                raise
            self._db.execute("release savepoint unit_test_ledger")
            return
        self._db.execute("begin immediate")
        try:
            yield
        except BaseException:
            self._db.rollback()
            raise
        self._db.commit()

    def _request(self, request_digest: str) -> UnitTestRequest:
        row = self._db.execute(
            "select body_json from unit_test_request where request_digest=?", (request_digest,)
        ).fetchone()
        if row is None:
            raise PolicyViolation("Unit-test request kayitli degil")
        return _request_from_row(str(row[0]))

    def register_request(self, request: UnitTestRequest, *, now: datetime) -> LedgerWrite:
        key = request.request_digest
        body = canonical_json(request.to_payload())
        with self._tx():
            existing = self._db.execute(
                "select body_json from unit_test_request where request_digest=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing[0] != body:
                    raise ConcurrencyConflict("Unit-test request replay payload drift")
                return LedgerWrite(key, True)
            try:
                self._db.execute(
                    "insert into unit_test_request(request_digest,project_id,source_binding_id,"
                    "metric,policy,max_attempts,body_json,created_at) values(?,?,?,?,?,?,?,?)",
                    (
                        key,
                        request.project_id,
                        request.source_binding_id,
                        request.metric.value,
                        request.policy.value,
                        request.budget.max_attempts,
                        body,
                        _iso(now),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PolicyViolation("Unit-test request proje baglanti kurali reddetti") from exc
        return LedgerWrite(key, False)

    def claim_attempt(self, attempt: UnitTestAttempt, *, now: datetime) -> LedgerWrite:
        with self._tx():
            replay = self._db.execute(
                "select attempt_id, effect_digest from unit_test_attempt where idempotency_key=?",
                (attempt.idempotency_key,),
            ).fetchone()
            if replay is not None:
                if replay[0] != attempt.attempt_id or replay[1] != attempt.effect_digest:
                    raise ConcurrencyConflict("Attempt claim replay payload drift")
                return LedgerWrite(attempt.attempt_id, True)
            request = self._request(attempt.request_digest)
            final = self._db.execute(
                "select stop_reason from unit_test_terminal where request_digest=? and final=1",
                (attempt.request_digest,),
            ).fetchone()
            if final is not None:
                raise PolicyViolation("Final terminal sonrasi yeni attempt baslayamaz")
            last = self._db.execute(
                "select attempt_id, ordinal from unit_test_attempt where request_digest=?"
                " order by ordinal desc limit 1",
                (attempt.request_digest,),
            ).fetchone()
            expected_ordinal = 1 if last is None else int(last[1]) + 1
            if attempt.ordinal != expected_ordinal:
                raise ConcurrencyConflict("Attempt ordinal ardisik olmali")
            if (last[0] if last is not None else None) != attempt.parent_attempt_id:
                raise ConcurrencyConflict("Attempt parent son attempt olmali")
            if attempt.ordinal > request.budget.max_attempts:
                raise PolicyViolation("Attempt butcesi asildi")
            if self.unreceipted_attempts(attempt.request_digest):
                raise PolicyViolation("Receipt'siz onceki attempt cozulmeden yenisi baslayamaz")
            self._db.execute(
                "insert into unit_test_attempt(attempt_id,request_digest,ordinal,parent_attempt_id,"
                "plan_digest,candidate_digest,effect_digest,idempotency_key,body_json,created_at)"
                " values(?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt.attempt_id,
                    attempt.request_digest,
                    attempt.ordinal,
                    attempt.parent_attempt_id,
                    attempt.plan_digest,
                    attempt.candidate_digest,
                    attempt.effect_digest,
                    attempt.idempotency_key,
                    canonical_json(attempt.to_payload()),
                    _iso(now),
                ),
            )
        return LedgerWrite(attempt.attempt_id, False)

    def record_attempt_receipt(
        self,
        *,
        attempt_id: str,
        status: AttemptState,
        evidence_digest: str,
        observations: tuple[CoverageObservation, ...],
        now: datetime,
    ) -> LedgerWrite:
        if status is AttemptState.CLAIMED:
            raise ValidationFailed("Receipt durumu claimed olamaz")
        if status is AttemptState.COMPLETED and not observations:
            raise PolicyViolation("Completed receipt olcum gozlemi ister")
        if status is not AttemptState.COMPLETED and observations:
            raise PolicyViolation("Yalniz completed receipt gozlem tasir")
        with self._tx():
            row = self._db.execute(
                "select request_digest from unit_test_attempt where attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise PolicyViolation("Receipt icin claim yok (claim-before-effect)")
            existing = self._db.execute(
                "select status, evidence_digest from unit_test_attempt_receipt where attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != status.value or existing[1] != evidence_digest:
                    raise ConcurrencyConflict("Attempt receipt replay payload drift")
                return LedgerWrite(attempt_id, True)
            request = self._request(str(row[0]))
            wanted = set(request.source_files)
            seen: set[tuple[str, CoverageMetric]] = set()
            for item in observations:
                pair = (item.source_file, item.metric)
                if item.source_file not in wanted or pair in seen:
                    raise PolicyViolation("Gozlem scope disi veya tekrar")
                seen.add(pair)
            self._db.execute(
                "insert into unit_test_attempt_receipt"
                "(attempt_id,status,evidence_digest,created_at) values(?,?,?,?)",
                (attempt_id, status.value, evidence_digest, _iso(now)),
            )
            for item in observations:
                self._db.execute(
                    "insert into unit_test_observation(attempt_id,source_file,metric,state,"
                    "covered,missed,total,reason) values(?,?,?,?,?,?,?,?)",
                    (
                        attempt_id,
                        item.source_file,
                        item.metric.value,
                        item.state.value,
                        item.covered,
                        item.missed,
                        item.total,
                        item.reason,
                    ),
                )
        return LedgerWrite(attempt_id, False)

    def unreceipted_attempts(self, request_digest: str) -> tuple[str, ...]:
        rows = self._db.execute(
            "select a.attempt_id from unit_test_attempt a"
            " left join unit_test_attempt_receipt r on r.attempt_id=a.attempt_id"
            " where a.request_digest=? and r.attempt_id is null order by a.ordinal",
            (request_digest,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def list_attempts(self, request_digest: str) -> tuple[UnitTestAttempt, ...]:
        rows = self._db.execute(
            "select body_json, idempotency_key from unit_test_attempt where request_digest=?"
            " order by ordinal",
            (request_digest,),
        ).fetchall()
        return tuple(_attempt_from_row(row) for row in rows)

    def get_attempt_receipt(self, attempt_id: str) -> AttemptReceiptRecord | None:
        row = self._db.execute(
            "select status, evidence_digest from unit_test_attempt_receipt where attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        return AttemptReceiptRecord(attempt_id, AttemptState(row[0]), str(row[1]))

    def list_observations(self, attempt_id: str) -> tuple[CoverageObservation, ...]:
        rows = self._db.execute(
            "select source_file,metric,state,covered,missed,total,reason from unit_test_observation"
            " where attempt_id=? order by source_file, metric",
            (attempt_id,),
        ).fetchall()
        return tuple(
            CoverageObservation(
                str(r[0]), CoverageMetric(r[1]), CoverageState(r[2]), r[3], r[4], r[5], str(r[6])
            )
            for r in rows
        )

    def record_terminal(self, terminal: UnitTestTerminal, *, now: datetime) -> LedgerWrite:
        with self._tx():
            request = self._request(terminal.request_digest)
            rows = self._db.execute(
                "select terminal_seq, stop_reason, receipt_digest, evidence_digest, final"
                " from unit_test_terminal where request_digest=? order by terminal_seq",
                (terminal.request_digest,),
            ).fetchall()
            if rows:
                last = rows[-1]
                if (
                    last[1] == terminal.stop_reason.value
                    and last[2] == terminal.receipt_digest
                    and last[3] == terminal.evidence_digest
                ):
                    return LedgerWrite(f"{terminal.request_digest}#{last[0]}", True)
                if int(last[4]) == 1:
                    raise PolicyViolation("Final terminal sonrasi baska terminal yazilamaz")
            if terminal.stop_reason in {
                UnitTestStopReason.TARGET_REACHED,
                UnitTestStopReason.ALREADY_AT_TARGET,
            }:
                self._assert_success_evidence(request, terminal)
            seq = (int(rows[-1][0]) + 1) if rows else 1
            self._db.execute(
                "insert into unit_test_terminal(request_digest,terminal_seq,terminal_status,"
                "stop_reason,receipt_digest,evidence_digest,final,created_at)"
                " values(?,?,?,?,?,?,?,?)",
                (
                    terminal.request_digest,
                    seq,
                    terminal.status.value,
                    terminal.stop_reason.value,
                    terminal.receipt_digest,
                    terminal.evidence_digest,
                    1 if terminal.stop_reason not in RESUMABLE_REASONS else 0,
                    _iso(now),
                ),
            )
        return LedgerWrite(f"{terminal.request_digest}#{seq}", False)

    def _assert_success_evidence(
        self, request: UnitTestRequest, terminal: UnitTestTerminal
    ) -> None:
        """Basari yalniz saklanmis tamsayi sayaclarin yeniden degerlendirmesinden gelir."""

        if self.unreceipted_attempts(terminal.request_digest):
            raise PolicyViolation("Receipt'siz attempt varken basari terminali yazilamaz")
        attempts = self.list_attempts(terminal.request_digest)
        if not attempts:
            raise PolicyViolation("Olcum attempt'i olmadan basari terminali yazilamaz")
        latest = attempts[-1]
        receipt = self.get_attempt_receipt(latest.attempt_id)
        if (
            receipt is None
            or receipt.status is not AttemptState.COMPLETED
            or receipt.evidence_digest != terminal.receipt_digest
        ):
            raise PolicyViolation("Terminal receipt son completed attempt kanitina bagli degil")
        if terminal.stop_reason is UnitTestStopReason.ALREADY_AT_TARGET and len(attempts) != 1:
            raise PolicyViolation("already-at-target yalniz baslangic olcumunden gelir")
        result = evaluate_scope(request, self.list_observations(latest.attempt_id))
        if result.verdict is not EvaluationVerdict.MET:
            raise PolicyViolation(
                "Hedef tam hassasiyetle karsilanmadi; basari terminali reddedildi"
            )

    def list_terminals(self, request_digest: str) -> tuple[UnitTestTerminal, ...]:
        rows = self._db.execute(
            "select stop_reason, receipt_digest, evidence_digest from unit_test_terminal"
            " where request_digest=? order by terminal_seq",
            (request_digest,),
        ).fetchall()
        return tuple(
            UnitTestTerminal(request_digest, UnitTestStopReason(r[0]), r[1], r[2]) for r in rows
        )

    # -- salt okunur okuma modeli (status/report/resume) --------------------------------

    def get_request(self, request_digest: str) -> UnitTestRequest | None:
        row = self._db.execute(
            "select body_json from unit_test_request where request_digest=?", (request_digest,)
        ).fetchone()
        return None if row is None else _request_from_row(str(row[0]))

    def list_requests(
        self, *, limit: int = 20, project_id: str | None = None
    ) -> tuple[tuple[str, UnitTestRequest, str], ...]:
        """En yeni once (request_digest, request, created_at); sinirli."""

        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValidationFailed("Liste siniri 1..200 olmali")
        if project_id is None:
            rows = self._db.execute(
                "select request_digest, body_json, created_at from unit_test_request"
                " order by created_at desc, request_digest desc limit ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._db.execute(
                "select request_digest, body_json, created_at from unit_test_request"
                " where project_id=? order by created_at desc, request_digest desc limit ?",
                (project_id, limit),
            ).fetchall()
        return tuple((str(r[0]), _request_from_row(str(r[1])), str(r[2])) for r in rows)
