"""SQLite benchmark ledger and immutable local artifact store.

The provider-free in-process acceptance path is portable across supported
desktop platforms. Process adapters retain their separate OS sandbox policy.
"""

# ruff: noqa: E501 -- literal SQLite DDL remains directly reviewable.

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, final
from uuid import UUID, uuid5

from zekam.domain.canonical import (
    canonical_json,
    digest,
    digest_of_bytes,
    parse_digest,
)
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import (
    REQUIRED_SCORE_DIMENSIONS,
    BenchmarkAggregate,
    BenchmarkFixture,
    BenchmarkPlan,
    BenchmarkSuite,
    BenchmarkTaskFamily,
    FixtureRegistry,
    TrialResult,
    TrialStatus,
    VerifierIdentity,
    VerifierVerdict,
    benchmark_effect_digest,
    benchmark_verifier_effect_digest,
)
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_tree,
)

MAX_ARTIFACT_BYTES = 1_048_576
MAX_CALLS = 4096
SCHEMA_DIGEST = "sha256:5834c83e5c0f4acc0ea62076e311f7684a27fe51c0e68a944c0d5884c9fd6a19"
_NS = UUID("3f742d08-22d3-4e19-a37c-39e47824c403")
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET = re.compile(
    r"(?i)(?:bearer\s+|password|credential|api[-_]?key|\b(?:sk|pk)-[A-Za-z0-9]{8,}|://)"
)

_SCHEMA = r"""
pragma foreign_keys=on;
create table local_benchmark_schema(singleton integer primary key check(singleton=1),version integer not null);
insert into local_benchmark_schema values(1,1);
create table contract(kind text not null,digest text not null,body_json text not null,primary key(kind,digest)) strict;
create table benchmark_plan(plan_id text primary key,plan_digest text unique not null,suite_digest text not null,model_id text not null,repetitions integer not null,body_json text not null) strict;
create table call_claim(claim_id text primary key,plan_digest text not null,phase text not null,fixture_digest text not null,repetition integer not null,model_id text not null,effect_digest text not null,body_json text not null,unique(plan_digest,phase,fixture_digest,repetition)) strict;
create table call_receipt(receipt_digest text primary key,claim_id text unique not null references call_claim,status text not null,result_digest text,failure_category text,evidence_digest text not null,body_json text not null,check(status in('completed','failed'))) strict;
create table artifact(artifact_digest text not null,kind text not null,size integer not null,body_json text not null,primary key(artifact_digest,kind),check(kind in('raw','normalized','fixed','verifier'))) strict;
create table artifact_pair(response_digest text primary key,raw_digest text not null,normalized_digest text not null,body_json text not null,check(response_digest=normalized_digest),check(raw_digest<>normalized_digest)) strict;
create trigger artifact_pair_artifacts before insert on artifact_pair begin select case when not exists(select 1 from artifact where artifact_digest=new.raw_digest and kind='raw') or not exists(select 1 from artifact where artifact_digest=new.normalized_digest and kind='normalized') then raise(abort,'artifact-pair-evidence') end; end;
create table benchmark_trial(trial_digest text primary key,plan_id text not null references benchmark_plan,tested_claim_id text not null references call_claim,verifier_claim_id text references call_claim,fixture_digest text not null,repetition integer not null,status text not null,evidence_digest text not null,response_digest text not null,body_json text not null,unique(plan_id,fixture_digest,repetition)) strict;
create table benchmark_failure(failure_digest text primary key,plan_id text not null references benchmark_plan,claim_id text not null references call_claim,fixture_digest text not null,repetition integer not null,phase text not null,category text not null,body_json text not null,unique(plan_id,fixture_digest,repetition,phase)) strict;
create table benchmark_aggregate(aggregate_digest text primary key,plan_id text unique not null references benchmark_plan,body_json text not null) strict;
"""
for _table in (
    "contract",
    "benchmark_plan",
    "call_claim",
    "call_receipt",
    "artifact",
    "artifact_pair",
    "benchmark_trial",
    "benchmark_failure",
    "benchmark_aggregate",
):
    _SCHEMA += f"create trigger {_table}_no_update before update on {_table} begin select raise(abort,'append-only'); end;\n"
    _SCHEMA += f"create trigger {_table}_no_delete before delete on {_table} begin select raise(abort,'append-only'); end;\n"


def _schema_digest(db: sqlite3.Connection) -> str:
    rows = db.execute(
        "select type,name,sql from sqlite_master where type in ('table','trigger') "
        "and name not like 'sqlite_%' order by type,name"
    ).fetchall()
    return digest([{"type": str(row[0]), "name": str(row[1]), "sql": str(row[2])} for row in rows])


def _safe(value: object, label: str) -> str:
    if type(value) is not str or not _SAFE.fullmatch(value) or _SECRET.search(value):
        raise ValidationFailed(f"Local benchmark {label} invalid or sensitive")
    return value


def _instant(value: dt.datetime) -> str:
    if type(value) is not dt.datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed("Local benchmark timestamp timezone-aware olmali")
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()


def _canonical_document(raw: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise PolicyViolation("Stored local benchmark JSON invalid") from exc
    if type(value) is not dict or canonical_json(value) != raw:
        raise PolicyViolation("Stored local benchmark JSON is not canonical")
    return value


@dataclass(frozen=True, slots=True)
class LocalBenchmarkTask:
    task_id: str
    version: int
    fixture_digest: str
    prompt_digest: str
    hidden_key_digest: str
    grader_digest: str
    repetitions: int
    timeout_seconds: int
    task_family: BenchmarkTaskFamily = BenchmarkTaskFamily.CODE_REPAIR
    scoring_dimensions: tuple[str, ...] = ("correctness",)

    def __post_init__(self) -> None:
        _safe(self.task_id, "task id")
        if type(self.version) is not int or self.version < 1:
            raise ValidationFailed("Local benchmark task version invalid")
        for value in (
            self.fixture_digest,
            self.prompt_digest,
            self.hidden_key_digest,
            self.grader_digest,
        ):
            parse_digest(value)
        if type(self.repetitions) is not int or self.repetitions < 5 or self.repetitions > 100:
            raise ValidationFailed("Local benchmark task repetitions invalid")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 600:
            raise ValidationFailed("Local benchmark task timeout invalid")
        if type(self.task_family) is not BenchmarkTaskFamily:
            raise ValidationFailed("Local benchmark task family exact enum olmali")
        if (
            type(self.scoring_dimensions) is not tuple
            or not self.scoring_dimensions
            or len(set(self.scoring_dimensions)) != len(self.scoring_dimensions)
            or any(item not in REQUIRED_SCORE_DIMENSIONS for item in self.scoring_dimensions)
        ):
            raise ValidationFailed("Local benchmark task scoring dimensions invalid")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-benchmark-task/v1",
            "task_id": self.task_id,
            "version": self.version,
            "fixture_digest": self.fixture_digest,
            "prompt_digest": self.prompt_digest,
            "hidden_key_digest": self.hidden_key_digest,
            "grader_digest": self.grader_digest,
            "repetitions": self.repetitions,
            "timeout_seconds": self.timeout_seconds,
            "task_family": self.task_family.value,
            "scoring_dimensions": list(self.scoring_dimensions),
            "hidden_key_material_present": False,
        }

    @property
    def task_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class LocalGraderContract:
    grader_id: str
    version: int
    implementation_digest: str
    dimensions: tuple[str, ...]

    def __post_init__(self) -> None:
        _safe(self.grader_id, "grader id")
        if type(self.version) is not int or self.version < 1:
            raise ValidationFailed("Local grader version invalid")
        parse_digest(self.implementation_digest)
        if (
            type(self.dimensions) is not tuple
            or not self.dimensions
            or len(self.dimensions) > 16
            or len(set(self.dimensions)) != len(self.dimensions)
        ):
            raise ValidationFailed("Local grader dimensions invalid")
        for item in self.dimensions:
            _safe(item, "grader dimension")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-benchmark-grader/v1",
            "grader_id": self.grader_id,
            "version": self.version,
            "implementation_digest": self.implementation_digest,
            "dimensions": list(self.dimensions),
            "independent_result_required": True,
        }

    @property
    def grader_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class DryRunReport:
    plan_digest: str
    trial_count: int
    call_count: int
    max_calls: int

    def __post_init__(self) -> None:
        parse_digest(self.plan_digest)
        if any(
            type(value) is not int or value < 0 for value in (self.trial_count, self.call_count)
        ):
            raise ValidationFailed("Dry-run counters invalid")
        if (
            type(self.max_calls) is not int
            or not 0 <= self.call_count <= self.max_calls <= MAX_CALLS
        ):
            raise PolicyViolation("Dry-run call budget exceeded")

    @property
    def report_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-benchmark-dry-run/v1",
                "plan_digest": self.plan_digest,
                "trial_count": self.trial_count,
                "call_count": self.call_count,
                "max_calls": self.max_calls,
                "provider_calls_performed": 0,
            }
        )


def dry_run(plan: BenchmarkPlan, suite: BenchmarkSuite, *, max_calls: int) -> DryRunReport:
    if type(plan) is not BenchmarkPlan or type(suite) is not BenchmarkSuite:
        raise ValidationFailed("Exact benchmark plan and suite required")
    if plan.suite_digest != suite.suite_digest:
        raise PolicyViolation("Dry-run plan/suite drift")
    trials = len(suite.fixture_digests) * plan.repetitions
    return DryRunReport(plan.plan_digest, trials, trials * 2, max_calls)


@dataclass(frozen=True, slots=True)
class BlindPacket:
    packet_digest: str
    aliases: tuple[tuple[str, str], ...]
    mapping_digest: str


def blind_pair(
    plan_digest: str,
    left_model_id: str,
    left_response_digest: str,
    right_model_id: str,
    right_response_digest: str,
) -> BlindPacket:
    parse_digest(plan_digest)
    parse_digest(left_response_digest)
    parse_digest(right_response_digest)
    _safe(left_model_id, "blind model id")
    _safe(right_model_id, "blind model id")
    if left_model_id == right_model_id:
        raise PolicyViolation("Blind evaluation requires distinct model identities")
    rows = sorted(
        ((left_model_id, left_response_digest), (right_model_id, right_response_digest)),
        key=lambda row: digest({"plan": plan_digest, "model": row[0]}),
    )
    aliases = (("A", rows[0][1]), ("B", rows[1][1]))
    packet = {"schema": "zekam-benchmark-blind-packet/v1", "responses": dict(aliases)}
    mapping = {alias: model for alias, (model, _) in zip(("A", "B"), rows, strict=True)}
    return BlindPacket(digest(packet), aliases, digest(mapping))


def parse_score(value: object) -> float:
    if type(value) is not float or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValidationFailed("Benchmark score must be a finite float in 0..1")
    return value


@final
class SQLiteLocalBenchmarkLab:
    def __init__(self, path: Path, artifact_root: Path) -> None:
        if not path.is_absolute() or path.is_symlink() or not artifact_root.is_absolute():
            raise ValidationFailed("Local benchmark paths must be absolute and non-symlink")
        self.path = path
        self.artifact_root = artifact_root

    def bootstrap(self) -> None:
        for parent in (self.path.parent, self.artifact_root):
            created = not parent.exists()
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if created:
                restrict_private_tree(parent)
            if not private_directory(parent):
                raise PolicyViolation("Local benchmark private directory required")
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(_SCHEMA)
        self.path.chmod(0o600)

    def prepare_local_security(self) -> None:
        """Narrow an existing benchmark tree to the canonical private ACL."""

        root = self.path.parent
        if not root.is_dir() or root.is_symlink():
            raise PolicyViolation("Local benchmark security root invalid")
        if (
            private_directory(root)
            and private_regular(self.path)
            and (not self.artifact_root.exists() or private_directory(self.artifact_root))
        ):
            return
        try:
            restrict_private_tree(root)
        except OSError as exc:
            raise PolicyViolation("Local benchmark private ACL preparation failed") from exc
        if not private_directory(root) or (self.path.exists() and not private_regular(self.path)):
            raise PolicyViolation("Local benchmark private ACL verification failed")

    def _connect(self) -> sqlite3.Connection:
        if not private_regular(self.path):
            raise PolicyViolation("Local benchmark database identity invalid")
        db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        db.execute("pragma busy_timeout=5000")
        if (
            db.execute("select version from local_benchmark_schema").fetchone()[0] != 1
            or _schema_digest(db) != SCHEMA_DIGEST
        ):
            db.close()
            raise PolicyViolation("Local benchmark schema drift")
        return db

    @staticmethod
    def _put_contract(
        db: sqlite3.Connection, kind: str, value: str, body: dict[str, object]
    ) -> None:
        raw = canonical_json(body)
        existing = db.execute(
            "select body_json from contract where kind=? and digest=?", (kind, value)
        ).fetchone()
        if existing is not None:
            if existing[0] != raw:
                raise ConcurrencyConflict("Local benchmark contract replay drift")
            return
        db.execute("insert into contract values(?,?,?)", (kind, value, raw))

    def register_contracts(
        self,
        task: LocalBenchmarkTask,
        grader: LocalGraderContract,
        *,
        plan: BenchmarkPlan,
        suite: BenchmarkSuite,
    ) -> None:
        if task.grader_digest != grader.grader_digest:
            raise PolicyViolation("Task/grader digest drift")
        if task.scoring_dimensions != grader.dimensions:
            raise PolicyViolation("Task-specific grader dimensions drift")
        if (
            plan.suite_digest != suite.suite_digest
            or suite.fixture_digests != (task.fixture_digest,)
            or plan.repetitions != task.repetitions
        ):
            raise PolicyViolation("Task is not the exact plan fixture/repetition binding")
        binding: dict[str, object] = {
            "schema": "zekam-benchmark-plan-input-binding/v1",
            "plan_digest": plan.plan_digest,
            "suite_digest": suite.suite_digest,
            "task_digest": task.task_digest,
            "fixture_digest": task.fixture_digest,
            "prompt_digest": task.prompt_digest,
            "hidden_key_digest": task.hidden_key_digest,
            "grader_digest": grader.grader_digest,
        }
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            self._put_contract(db, "task", task.task_digest, task.body())
            self._put_contract(db, "grader", grader.grader_digest, grader.body())
            self._put_contract(db, "plan-binding", plan.plan_digest, binding)
            db.commit()

    def ensure_plan(
        self, *, registry: FixtureRegistry, suite: BenchmarkSuite, plan: BenchmarkPlan
    ) -> tuple[UUID, bool]:
        if (
            type(registry) is not FixtureRegistry
            or type(suite) is not BenchmarkSuite
            or type(plan) is not BenchmarkPlan
        ):
            raise ValidationFailed("Exact benchmark contracts required")
        if plan.remote_execution:
            raise PolicyViolation("Local benchmark store rejects remote execution")
        if (
            plan.suite_digest != suite.suite_digest
            or plan.fixture_registry_digest != registry.registry_digest
        ):
            raise PolicyViolation("Local benchmark plan provenance drift")
        registry_body = {
            "schema": "zekam-benchmark-fixture-registry/v1",
            "version": registry.schema_version,
            "fixtures": [row.as_dict() for row in registry.fixtures],
        }
        suite_body = {"schema": "zekam-benchmark-suite/v1", **suite.as_dict()}
        with closing(self._connect()) as db:
            binding_row = db.execute(
                "select body_json from contract where kind='plan-binding' and digest=?",
                (plan.plan_digest,),
            ).fetchone()
        if binding_row is None:
            raise PolicyViolation("Local benchmark exact plan input binding missing")
        binding_body = _canonical_document(str(binding_row[0]))
        expected_binding_keys = {
            "schema",
            "plan_digest",
            "suite_digest",
            "task_digest",
            "fixture_digest",
            "prompt_digest",
            "hidden_key_digest",
            "grader_digest",
        }
        if set(binding_body) != expected_binding_keys or any(
            type(binding_body[key]) is not str for key in expected_binding_keys
        ):
            raise PolicyViolation("Local benchmark plan input binding drift")
        for key in (
            "plan_digest",
            "suite_digest",
            "task_digest",
            "fixture_digest",
            "prompt_digest",
            "hidden_key_digest",
            "grader_digest",
        ):
            parse_digest(str(binding_body[key]))
        with closing(self._connect()) as db:
            task_row = db.execute(
                "select body_json from contract where kind='task' and digest=?",
                (binding_body["task_digest"],),
            ).fetchone()
            grader_row = db.execute(
                "select body_json from contract where kind='grader' and digest=?",
                (binding_body["grader_digest"],),
            ).fetchone()
        task_body = None if task_row is None else _canonical_document(str(task_row[0]))
        grader_body = None if grader_row is None else _canonical_document(str(grader_row[0]))
        if (
            binding_body.get("schema") != "zekam-benchmark-plan-input-binding/v1"
            or binding_body.get("plan_digest") != plan.plan_digest
            or binding_body.get("suite_digest") != suite.suite_digest
            or suite.fixture_digests != (binding_body.get("fixture_digest"),)
            or task_body is None
            or grader_body is None
            or set(task_body)
            != {
                "schema",
                "task_id",
                "version",
                "fixture_digest",
                "prompt_digest",
                "hidden_key_digest",
                "grader_digest",
                "repetitions",
                "timeout_seconds",
                "task_family",
                "scoring_dimensions",
                "hidden_key_material_present",
            }
            or set(grader_body)
            != {
                "schema",
                "grader_id",
                "version",
                "implementation_digest",
                "dimensions",
                "independent_result_required",
            }
            or task_body.get("schema") != "zekam-benchmark-task/v1"
            or task_body.get("hidden_key_material_present") is not False
            or grader_body.get("schema") != "zekam-benchmark-grader/v1"
            or grader_body.get("independent_result_required") is not True
            or digest(task_body) != binding_body.get("task_digest")
            or digest(grader_body) != binding_body.get("grader_digest")
            or task_body.get("fixture_digest") != binding_body.get("fixture_digest")
            or task_body.get("prompt_digest") != binding_body.get("prompt_digest")
            or task_body.get("hidden_key_digest") != binding_body.get("hidden_key_digest")
            or task_body.get("grader_digest") != binding_body.get("grader_digest")
            or task_body.get("repetitions") != plan.repetitions
            or task_body.get("scoring_dimensions") != grader_body.get("dimensions")
        ):
            raise PolicyViolation("Local benchmark plan input binding drift")
        plan_body = {
            "schema": "zekam-benchmark-plan/v1",
            "model_id": plan.model_id,
            "suite_digest": plan.suite_digest,
            "inventory_digest": plan.inventory_digest,
            "policy_digest": plan.policy_digest,
            "fixture_registry_digest": plan.fixture_registry_digest,
            "repetitions": plan.repetitions,
            "remote_execution": plan.remote_execution,
            "input_binding_digest": digest(binding_body),
        }
        plan_id = uuid5(_NS, plan.plan_digest)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            self._put_contract(db, "fixture-registry", registry.registry_digest, registry_body)
            self._put_contract(db, "suite", suite.suite_digest, suite_body)
            existing = db.execute(
                "select plan_id,body_json from benchmark_plan where plan_digest=?",
                (plan.plan_digest,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (str(plan_id), canonical_json(plan_body)):
                    raise ConcurrencyConflict("Local benchmark plan replay drift")
                db.rollback()
                return plan_id, False
            db.execute(
                "insert into benchmark_plan values(?,?,?,?,?,?)",
                (
                    str(plan_id),
                    plan.plan_digest,
                    plan.suite_digest,
                    plan.model_id,
                    plan.repetitions,
                    canonical_json(plan_body),
                ),
            )
            db.commit()
        return plan_id, True

    def list_trials(self, plan_id: UUID) -> tuple[TrialResult, ...]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "select body_json from benchmark_trial where plan_id=? order by fixture_digest,repetition",
                (str(plan_id),),
            ).fetchall()
        return tuple(self._trial(_canonical_document(str(row[0]))) for row in rows)

    @staticmethod
    def _trial(body: dict[str, Any]) -> TrialResult:
        return TrialResult(
            fixture_digest=body["fixture_digest"],
            repetition=body["repetition"],
            status=TrialStatus(body["status"]),
            parse_ok=body["parse_ok"],
            format_ok=body["format_ok"],
            evidence_ok=body["evidence_ok"],
            verifier_approved=body["verifier_approved"],
            quality=body["quality"],
            reliability=body["reliability"],
            latency_ms=body["latency_ms"],
            input_tokens=body["input_tokens"],
            output_tokens=body["output_tokens"],
            retry_count=body["retry_count"],
            human_corrections=body["human_corrections"],
            estimated_cost=body["estimated_cost"],
            actual_cost=body["actual_cost"],
            response_digest=body["response_digest"],
            evidence_digest=body["evidence_digest"],
            failure_category=body["failure_category"],
            tool_correctness=body.get("tool_correctness", 0.0),
            recovery=body.get("recovery", 0.0),
        )

    def _claim(
        self,
        *,
        plan: BenchmarkPlan,
        fixture: BenchmarkFixture,
        repetition: int,
        phase: str,
        model_id: str,
        effect_digest: str,
    ) -> UUID:
        if type(repetition) is not int or not 1 <= repetition <= plan.repetitions:
            raise ValidationFailed("Local benchmark repetition outside plan")
        claim_id = uuid5(_NS, f"{effect_digest}:{phase}")
        body = {
            "schema": "zekam-benchmark-call-claim/v1",
            "claim_id": str(claim_id),
            "plan_digest": plan.plan_digest,
            "phase": phase,
            "fixture_digest": fixture.fixture_digest,
            "repetition": repetition,
            "model_id": model_id,
            "effect_digest": effect_digest,
        }
        raw = canonical_json(body)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            prepared = db.execute(
                "select 1 from benchmark_plan where plan_digest=? and model_id=?",
                (plan.plan_digest, plan.model_id),
            ).fetchone()
            if prepared is None:
                raise PolicyViolation("Local benchmark plan must be prepared before claim")
            existing = db.execute(
                "select claim_id,body_json from call_claim where plan_digest=? and phase=? and fixture_digest=? and repetition=?",
                (plan.plan_digest, phase, fixture.fixture_digest, repetition),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (str(claim_id), raw):
                    raise ConcurrencyConflict("Local benchmark claim drift")
                raise PolicyViolation("Existing local benchmark call cannot be reissued")
            db.execute(
                "insert into call_claim values(?,?,?,?,?,?,?,?)",
                (
                    str(claim_id),
                    plan.plan_digest,
                    phase,
                    fixture.fixture_digest,
                    repetition,
                    model_id,
                    effect_digest,
                    raw,
                ),
            )
            db.commit()
        return claim_id

    def claim_tested(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, repetition: int
    ) -> UUID:
        return self._claim(
            plan=plan,
            fixture=fixture,
            repetition=repetition,
            phase="tested",
            model_id=plan.model_id,
            effect_digest=benchmark_effect_digest(
                plan.plan_digest, fixture.fixture_digest, repetition
            ),
        )

    def claim_verifier(
        self,
        *,
        plan: BenchmarkPlan,
        fixture: BenchmarkFixture,
        result: TrialResult,
        verifier: VerifierIdentity,
    ) -> UUID:
        return self._claim(
            plan=plan,
            fixture=fixture,
            repetition=result.repetition,
            phase="verifier",
            model_id=verifier.model_id,
            effect_digest=benchmark_verifier_effect_digest(
                plan.plan_digest,
                fixture.fixture_digest,
                result.repetition,
                verifier.model_id,
                result.response_digest,
            ),
        )

    def _receipt(
        self,
        claim_id: UUID,
        *,
        status: str,
        result_digest: str | None,
        evidence_digest: str,
        failure_category: str | None,
    ) -> None:
        body = {
            "schema": "zekam-benchmark-call-receipt/v1",
            "claim_id": str(claim_id),
            "status": status,
            "result_digest": result_digest,
            "failure_category": failure_category,
            "evidence_digest": evidence_digest,
        }
        raw, value = canonical_json(body), digest(body)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select body_json from call_receipt where claim_id=?", (str(claim_id),)
            ).fetchone()
            if existing is not None:
                if existing[0] != raw:
                    raise ConcurrencyConflict("Local benchmark receipt drift")
                db.rollback()
                return
            db.execute(
                "insert into call_receipt values(?,?,?,?,?,?,?)",
                (
                    value,
                    str(claim_id),
                    status,
                    result_digest,
                    failure_category,
                    evidence_digest,
                    raw,
                ),
            )
            db.commit()

    def complete_tested(self, *, claim_id: UUID, result: TrialResult) -> None:
        with closing(self._connect()) as db:
            claim = db.execute(
                "select plan_digest,phase,fixture_digest,repetition,effect_digest "
                "from call_claim where claim_id=?",
                (str(claim_id),),
            ).fetchone()
        if (
            claim is None
            or claim["phase"] != "tested"
            or claim["fixture_digest"] != result.fixture_digest
            or claim["repetition"] != result.repetition
            or claim["effect_digest"]
            != benchmark_effect_digest(
                str(claim["plan_digest"]), result.fixture_digest, result.repetition
            )
        ):
            raise PolicyViolation("Tested receipt does not match exact claim")
        self._receipt(
            claim_id,
            status="completed",
            result_digest=result.response_digest,
            evidence_digest=result.evidence_digest,
            failure_category=None,
        )

    def complete_verifier(self, *, claim_id: UUID, verdict: VerifierVerdict) -> None:
        with closing(self._connect()) as db:
            claim = db.execute(
                "select plan_digest,phase,fixture_digest,repetition,model_id,effect_digest "
                "from call_claim where claim_id=?",
                (str(claim_id),),
            ).fetchone()
        if (
            claim is None
            or claim["phase"] != "verifier"
            or claim["model_id"] != verdict.verifier_model_id
            or claim["effect_digest"]
            != benchmark_verifier_effect_digest(
                str(claim["plan_digest"]),
                str(claim["fixture_digest"]),
                int(claim["repetition"]),
                verdict.verifier_model_id,
                verdict.tested_response_digest,
            )
        ):
            raise PolicyViolation("Verifier receipt does not match exact claim")
        self._receipt(
            claim_id,
            status="completed",
            result_digest=verdict.evidence_digest,
            evidence_digest=verdict.evidence_digest,
            failure_category=None,
        )

    def retain_failure(
        self,
        *,
        plan_id: UUID,
        claim_id: UUID,
        fixture_digest: str,
        repetition: int,
        phase: str,
        category: str,
        result: TrialResult | None = None,
    ) -> str:
        parse_digest(fixture_digest)
        _safe(category, "failure category")
        if phase not in {"tested", "verifier"}:
            raise ValidationFailed("Local benchmark failure phase invalid")
        if type(repetition) is not int or repetition < 1:
            raise ValidationFailed("Local benchmark failure repetition invalid")
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            exact = db.execute(
                "select 1 from benchmark_plan p join call_claim c "
                "on c.plan_digest=p.plan_digest where p.plan_id=? and c.claim_id=? "
                "and c.fixture_digest=? and c.repetition=? and c.phase=?",
                (str(plan_id), str(claim_id), fixture_digest, repetition, phase),
            ).fetchone()
            if exact is None:
                raise PolicyViolation("Local benchmark failure does not match exact claim")
            receipt = db.execute(
                "select receipt_digest,status,body_json from call_receipt where claim_id=?",
                (str(claim_id),),
            ).fetchone()
            evidence = digest({"claim_id": str(claim_id), "category": category})
            if receipt is None:
                receipt_body = {
                    "schema": "zekam-benchmark-call-receipt/v1",
                    "claim_id": str(claim_id),
                    "status": "failed",
                    "result_digest": None,
                    "failure_category": category,
                    "evidence_digest": evidence,
                }
                receipt_raw, receipt_digest = canonical_json(receipt_body), digest(receipt_body)
                db.execute(
                    "insert into call_receipt values(?,?,?,?,?,?,?)",
                    (
                        receipt_digest,
                        str(claim_id),
                        "failed",
                        None,
                        category,
                        evidence,
                        receipt_raw,
                    ),
                )
                settled_receipt_digest = receipt_digest
                settled_receipt_status = "failed"
            else:
                stored_receipt = _canonical_document(str(receipt[2]))
                if (
                    stored_receipt.get("status") not in {"completed", "failed"}
                    or digest(stored_receipt) != receipt[0]
                    or stored_receipt.get("claim_id") != str(claim_id)
                    or (
                        stored_receipt.get("status") == "failed"
                        and (
                            stored_receipt.get("failure_category") != category
                            or stored_receipt.get("evidence_digest") != evidence
                        )
                    )
                ):
                    raise PolicyViolation("Local benchmark terminal receipt invalid")
                settled_receipt_digest = str(receipt[0])
                settled_receipt_status = str(receipt[1])
            body = {
                "schema": "zekam-benchmark-failure/v1",
                "plan_id": str(plan_id),
                "claim_id": str(claim_id),
                "fixture_digest": fixture_digest,
                "repetition": repetition,
                "phase": phase,
                "category": category,
                "settled_receipt_digest": settled_receipt_digest,
                "settled_receipt_status": settled_receipt_status,
            }
            raw, value = canonical_json(body), digest(body)
            existing = db.execute(
                "select body_json from benchmark_failure where plan_id=? and fixture_digest=? and repetition=? and phase=?",
                (str(plan_id), fixture_digest, repetition, phase),
            ).fetchone()
            if existing is not None and existing[0] != raw:
                raise ConcurrencyConflict("Local benchmark failure drift")
            if existing is None:
                db.execute(
                    "insert into benchmark_failure values(?,?,?,?,?,?,?,?)",
                    (
                        value,
                        str(plan_id),
                        str(claim_id),
                        fixture_digest,
                        repetition,
                        phase,
                        category,
                        raw,
                    ),
                )
            tested_claim_id: str
            verifier_claim_id: str | None
            if phase == "tested":
                tested_claim_id, verifier_claim_id = str(claim_id), None
            else:
                tested = db.execute(
                    "select claim_id from call_claim c join benchmark_plan p "
                    "on p.plan_digest=c.plan_digest where p.plan_id=? "
                    "and c.fixture_digest=? and c.repetition=? and c.phase='tested'",
                    (str(plan_id), fixture_digest, repetition),
                ).fetchone()
                if tested is None:
                    raise PolicyViolation("Verifier failure lacks tested claim")
                tested_claim_id, verifier_claim_id = str(tested[0]), str(claim_id)
            failure_result = TrialResult(
                fixture_digest=fixture_digest,
                repetition=repetition,
                status=TrialStatus.TIMEOUT if category == "timeout" else TrialStatus.FAILED,
                parse_ok=False,
                format_ok=False,
                evidence_ok=False,
                verifier_approved=False,
                quality=0.0,
                reliability=0.0,
                latency_ms=0,
                input_tokens=0,
                output_tokens=0,
                retry_count=0,
                human_corrections=0,
                estimated_cost=0.0,
                actual_cost=None,
                response_digest=evidence if result is None else result.response_digest,
                evidence_digest=digest(
                    {
                        "failure": value,
                        "tested_evidence": None if result is None else result.evidence_digest,
                    }
                ),
                failure_category=category,
            )
            trial_body = self._trial_body(failure_result)
            trial_raw, trial_digest = canonical_json(trial_body), digest(trial_body)
            trial = db.execute(
                "select body_json from benchmark_trial where plan_id=? "
                "and fixture_digest=? and repetition=?",
                (str(plan_id), fixture_digest, repetition),
            ).fetchone()
            if trial is not None and trial[0] != trial_raw:
                raise ConcurrencyConflict("Local benchmark retained trial drift")
            if trial is None:
                db.execute(
                    "insert into benchmark_trial values(?,?,?,?,?,?,?,?,?,?)",
                    (
                        trial_digest,
                        str(plan_id),
                        tested_claim_id,
                        verifier_claim_id,
                        fixture_digest,
                        repetition,
                        failure_result.status.value,
                        failure_result.evidence_digest,
                        failure_result.response_digest,
                        trial_raw,
                    ),
                )
            db.commit()
        return value

    def store_artifact(self, kind: str, payload: bytes) -> str:
        if (
            kind not in {"raw", "normalized", "fixed", "verifier"}
            or type(payload) is not bytes
            or not 0 < len(payload) <= MAX_ARTIFACT_BYTES
        ):
            raise ValidationFailed("Local benchmark artifact invalid")
        text = payload.decode("utf-8", errors="strict")
        if _SECRET.search(text):
            raise PolicyViolation("Local benchmark artifact contains sensitive material")
        value = digest_of_bytes(payload)
        target = self.artifact_root / value.removeprefix("sha256:")
        if target.exists():
            if not private_regular(target) or target.read_bytes() != payload:
                raise ConcurrencyConflict("Local benchmark artifact overwrite drift")
        else:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                view = memoryview(payload)
                while view:
                    view = view[os.write(descriptor, view) :]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if os.name != "nt":
                directory = os.open(
                    self.artifact_root,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        body = {
            "schema": "zekam-benchmark-artifact/v1",
            "artifact_digest": value,
            "kind": kind,
            "size": len(payload),
        }
        raw = canonical_json(body)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select body_json from artifact where artifact_digest=? and kind=?", (value, kind)
            ).fetchone()
            if existing is not None and existing[0] != raw:
                raise ConcurrencyConflict("Local benchmark artifact metadata drift")
            if existing is None:
                db.execute("insert into artifact values(?,?,?,?)", (value, kind, len(payload), raw))
            db.commit()
        return value

    def bind_artifacts(
        self, *, response_digest: str, raw_digest: str, normalized_digest: str
    ) -> str:
        for value in (response_digest, raw_digest, normalized_digest):
            parse_digest(value)
        if response_digest != normalized_digest or raw_digest == normalized_digest:
            raise PolicyViolation("Local benchmark raw/normalized artifact relation invalid")
        body = {
            "schema": "zekam-benchmark-artifact-pair/v1",
            "response_digest": response_digest,
            "raw_digest": raw_digest,
            "normalized_digest": normalized_digest,
        }
        raw, value = canonical_json(body), digest(body)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select raw_digest,normalized_digest,body_json from artifact_pair "
                "where response_digest=?",
                (response_digest,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (raw_digest, normalized_digest, raw):
                    raise ConcurrencyConflict("Local benchmark artifact pair replay drift")
                db.rollback()
                return value
            db.execute(
                "insert into artifact_pair values(?,?,?,?)",
                (response_digest, raw_digest, normalized_digest, raw),
            )
            db.commit()
        return value

    def artifact_pair(self, response_digest: str) -> tuple[str, str]:
        parse_digest(response_digest)
        with closing(self._connect()) as db:
            row = db.execute(
                "select raw_digest,normalized_digest,body_json from artifact_pair "
                "where response_digest=?",
                (response_digest,),
            ).fetchone()
        if row is None:
            raise PolicyViolation("Local benchmark artifact pair missing")
        body = _canonical_document(str(row[2]))
        if (
            body
            != {
                "schema": "zekam-benchmark-artifact-pair/v1",
                "response_digest": response_digest,
                "raw_digest": str(row[0]),
                "normalized_digest": str(row[1]),
            }
            or row[1] != response_digest
            or row[0] == row[1]
        ):
            raise PolicyViolation("Local benchmark artifact pair drift")
        return str(row[0]), str(row[1])

    def trial_receipt_matches(
        self,
        *,
        plan_id: UUID,
        tested_claim_id: UUID,
        verifier_claim_id: UUID,
        verdict: VerifierVerdict,
        result: TrialResult,
    ) -> bool:
        with closing(self._connect()) as db:
            tested = db.execute(
                "select c.plan_digest,c.phase,c.fixture_digest,c.repetition,c.model_id,"
                "c.effect_digest,r.status,r.result_digest from call_claim c "
                "join call_receipt r on r.claim_id=c.claim_id where c.claim_id=?",
                (str(tested_claim_id),),
            ).fetchone()
            verifier = db.execute(
                "select c.plan_digest,c.phase,c.fixture_digest,c.repetition,c.model_id,"
                "c.effect_digest,r.status,r.result_digest from call_claim c "
                "join call_receipt r on r.claim_id=c.claim_id where c.claim_id=?",
                (str(verifier_claim_id),),
            ).fetchone()
            plan = db.execute(
                "select model_id,plan_digest from benchmark_plan where plan_id=?",
                (str(plan_id),),
            ).fetchone()
        if not plan or not tested or not verifier:
            return False
        plan_digest = str(plan["plan_digest"])
        return bool(
            plan["model_id"] == verdict.tested_model_id
            and tested["plan_digest"] == verifier["plan_digest"] == plan_digest
            and tested["phase"] == "tested"
            and verifier["phase"] == "verifier"
            and tested["fixture_digest"] == verifier["fixture_digest"] == result.fixture_digest
            and tested["repetition"] == verifier["repetition"] == result.repetition
            and tested["model_id"] == verdict.tested_model_id
            and verifier["model_id"] == verdict.verifier_model_id
            and tested["effect_digest"]
            == benchmark_effect_digest(plan_digest, result.fixture_digest, result.repetition)
            and verifier["effect_digest"]
            == benchmark_verifier_effect_digest(
                plan_digest,
                result.fixture_digest,
                result.repetition,
                verdict.verifier_model_id,
                result.response_digest,
            )
            and (tested["status"], tested["result_digest"]) == ("completed", result.response_digest)
            and (verifier["status"], verifier["result_digest"])
            == ("completed", verdict.evidence_digest)
            and verdict.tested_response_digest == result.response_digest
            and verdict.approved == result.verifier_approved
        )

    @staticmethod
    def _trial_body(result: TrialResult) -> dict[str, object]:
        return {
            "schema": "zekam-benchmark-trial/v1",
            "fixture_digest": result.fixture_digest,
            "repetition": result.repetition,
            "status": result.status.value,
            "parse_ok": result.parse_ok,
            "format_ok": result.format_ok,
            "evidence_ok": result.evidence_ok,
            "verifier_approved": result.verifier_approved,
            "quality": result.quality,
            "reliability": result.reliability,
            "latency_ms": result.latency_ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "retry_count": result.retry_count,
            "human_corrections": result.human_corrections,
            "estimated_cost": result.estimated_cost,
            "actual_cost": result.actual_cost,
            "response_digest": result.response_digest,
            "evidence_digest": result.evidence_digest,
            "failure_category": result.failure_category,
            "tool_correctness": result.tool_correctness,
            "recovery": result.recovery,
        }

    def record_trial(
        self,
        *,
        plan_id: UUID,
        tested_claim_id: UUID,
        verifier_claim_id: UUID,
        verdict: VerifierVerdict,
        result: TrialResult,
        observed_at: dt.datetime | None = None,
    ) -> tuple[UUID, bool]:
        del observed_at
        if not self.trial_receipt_matches(
            plan_id=plan_id,
            tested_claim_id=tested_claim_id,
            verifier_claim_id=verifier_claim_id,
            verdict=verdict,
            result=result,
        ):
            raise PolicyViolation("Local benchmark trial requires exact receipts")
        self.artifact_pair(result.response_digest)
        body = self._trial_body(result)
        raw, value = canonical_json(body), digest(body)
        record_id = uuid5(_NS, f"{plan_id}:{result.fixture_digest}:{result.repetition}")
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select trial_digest,body_json from benchmark_trial where plan_id=? and fixture_digest=? and repetition=?",
                (str(plan_id), result.fixture_digest, result.repetition),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Local benchmark trial replay drift")
                db.rollback()
                return record_id, False
            db.execute(
                "insert into benchmark_trial values(?,?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    str(plan_id),
                    str(tested_claim_id),
                    str(verifier_claim_id),
                    result.fixture_digest,
                    result.repetition,
                    result.status.value,
                    result.evidence_digest,
                    result.response_digest,
                    raw,
                ),
            )
            db.commit()
        return record_id, True

    def store_aggregate(self, *, plan_id: UUID, aggregate: BenchmarkAggregate) -> UUID:
        trials = self.list_trials(plan_id)
        passed = sum(row.valid for row in trials)
        count = len(trials)
        if count < 5:
            raise PolicyViolation("Local benchmark aggregate requires at least five trials")
        ratio = passed / count
        z = 1.959963984540054
        denominator = 1 + z * z / count
        centre = (ratio + z * z / (2 * count)) / denominator
        margin = z * math.sqrt((ratio * (1 - ratio) + z * z / (4 * count)) / count) / denominator
        body = {
            "schema": "zekam-benchmark-aggregate/v1",
            **aggregate.as_dict(),
            "trial_count": count,
            "pass_rate": ratio,
            "confidence_95": [max(0.0, centre - margin), min(1.0, centre + margin)],
        }
        raw, value = canonical_json(body), digest(body)
        record_id = uuid5(_NS, f"aggregate:{plan_id}")
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select aggregate_digest,body_json from benchmark_aggregate where plan_id=?",
                (str(plan_id),),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Local benchmark aggregate drift")
                db.rollback()
                return record_id
            db.execute("insert into benchmark_aggregate values(?,?,?)", (value, str(plan_id), raw))
            db.commit()
        return record_id

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as db:
            return {
                table: int(db.execute(f"select count(*) from {table}").fetchone()[0])
                for table in (
                    "benchmark_plan",
                    "call_claim",
                    "call_receipt",
                    "benchmark_trial",
                    "benchmark_failure",
                    "benchmark_aggregate",
                    "artifact",
                    "artifact_pair",
                )
            }

    def campaign_snapshot(self, plan_digest: str) -> dict[str, object] | None:
        """Return bounded read-only status/report evidence for one exact plan."""

        parse_digest(plan_digest)
        with closing(self._connect()) as db:
            plan = db.execute(
                "select plan_id,suite_digest,model_id,repetitions,body_json "
                "from benchmark_plan where plan_digest=?",
                (plan_digest,),
            ).fetchone()
            if plan is None:
                return None
            suite_row = db.execute(
                "select body_json from contract where kind='suite' and digest=?",
                (str(plan["suite_digest"]),),
            ).fetchone()
            if suite_row is None:
                raise PolicyViolation("Stored benchmark suite contract missing")
            suite = _canonical_document(str(suite_row[0]))
            fixture_digests = suite.get("fixture_digests")
            if not isinstance(fixture_digests, list) or not fixture_digests:
                raise PolicyViolation("Stored benchmark suite fixture set invalid")
            plan_id = str(plan["plan_id"])
            trial_count = int(
                db.execute(
                    "select count(*) from benchmark_trial where plan_id=?", (plan_id,)
                ).fetchone()[0]
            )
            failure_count = int(
                db.execute(
                    "select count(*) from benchmark_failure where plan_id=?", (plan_id,)
                ).fetchone()[0]
            )
            claim_count = int(
                db.execute(
                    "select count(*) from call_claim where plan_digest=?", (plan_digest,)
                ).fetchone()[0]
            )
            receipt_count = int(
                db.execute(
                    "select count(*) from call_receipt r join call_claim c "
                    "on c.claim_id=r.claim_id where c.plan_digest=?",
                    (plan_digest,),
                ).fetchone()[0]
            )
            aggregate_row = db.execute(
                "select aggregate_digest,body_json from benchmark_aggregate where plan_id=?",
                (plan_id,),
            ).fetchone()
        expected_trials = int(plan["repetitions"]) * len(fixture_digests)
        if aggregate_row is not None:
            state = "completed"
        elif failure_count:
            state = "failed"
        elif trial_count:
            state = "running"
        else:
            state = "planned"
        aggregate = None if aggregate_row is None else _canonical_document(str(aggregate_row[1]))
        return {
            "schema": "zekam-native-benchmark-campaign-status/v1",
            "plan_id": plan_id,
            "plan_digest": plan_digest,
            "suite_digest": str(plan["suite_digest"]),
            "model_id": str(plan["model_id"]),
            "state": state,
            "repetitions": int(plan["repetitions"]),
            "expected_trials": expected_trials,
            "trial_count": trial_count,
            "failure_count": failure_count,
            "expected_calls": expected_trials * 2,
            "claim_count": claim_count,
            "receipt_count": receipt_count,
            "aggregate_digest": None if aggregate_row is None else str(aggregate_row[0]),
            "aggregate": aggregate,
            "provider_calls": 0,
            "read_only": True,
            "grants_authority": False,
        }
