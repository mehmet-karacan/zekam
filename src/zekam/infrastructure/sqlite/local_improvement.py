"""Dormant Mac-local measured improvement ledger for WP-14."""

# ruff: noqa: E501 -- literal SQLite contracts remain directly reviewable.

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import statistics
import unicodedata
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast, final
from uuid import UUID

from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import (
    benchmark_effect_digest,
    benchmark_verifier_effect_digest,
)
from zekam.domain.optimization import (
    MeasurementEvidence,
    MetricAggregation,
    MetricDirection,
    MetricRole,
    MetricSpec,
    ProgressState,
    ValidatorAssetManifest,
    evaluate_progress,
)
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_tree,
)
from zekam.infrastructure.sqlite.local_learning import (
    SCHEMA_DIGEST as LEARNING_SCHEMA_DIGEST,
)
from zekam.infrastructure.sqlite.local_model_benchmark import (
    SCHEMA_DIGEST as BENCHMARK_SCHEMA_DIGEST,
)

MAX_BODY_BYTES = 1_048_576
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_SECRET = re.compile(r"(?i)(?:bearer\s+|password|credential|api[-_]?key|\bsk-[A-Za-z0-9]{8,})")
_BENCHMARK_METRICS = frozenset(
    f"{name}.{field}"
    for name in ("quality", "reliability", "latency_ms", "cost", "token_count")
    for field in ("mean", "median", "p95", "variance")
)
_MANDATORY_BENCHMARK_GROUPS = (
    "quality",
    "reliability",
    "latency_ms",
    "cost",
    "token_count",
    "correctness",
    "evidence_citation",
    "structured_format",
    "safety",
    "token_efficiency",
    "tool_correctness",
    "recovery",
    "human_correction",
)
_MANDATORY_MAXIMIZE = frozenset(
    {
        "quality",
        "reliability",
        "correctness",
        "evidence_citation",
        "structured_format",
        "safety",
        "token_efficiency",
        "tool_correctness",
        "recovery",
    }
)
_MANDATORY_MINIMIZE = frozenset({"latency_ms", "cost", "token_count", "human_correction"})
_AUTO_SAFE_RESOURCES = frozenset(
    {"local-cache", "local-index", "local-projection", "local-registry", "local-report"}
)
_REVIEW_RESOURCES = _AUTO_SAFE_RESOURCES | frozenset(
    {"prompt-candidate", "relation-proposal", "routing-candidate", "skill-draft"}
)
_HUMAN_RESOURCES = _REVIEW_RESOURCES | frozenset(
    {"external-effect", "retention", "root-instruction", "schema", "security-policy"}
)
_IDENTITY_VERSION = "lexical-set-v2"
_IDENTITY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "are",
        "be",
        "been",
        "being",
        "bir",
        "bu",
        "by",
        "da",
        "de",
        "ile",
        "is",
        "olan",
        "olarak",
        "the",
        "ve",
        "was",
        "were",
    }
)
_IDENTITY_EXACT_STEMS = {
    "prevented": "prevent",
    "preventing": "prevent",
    "prevents": "prevent",
}


class ImprovementChangeClass(StrEnum):
    AUTO_SAFE = "AUTO_SAFE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
    PROHIBITED_AUTONOMOUS = "PROHIBITED_AUTONOMOUS"


_CLASS_RESOURCES = {
    ImprovementChangeClass.AUTO_SAFE: _AUTO_SAFE_RESOURCES,
    ImprovementChangeClass.REVIEW_REQUIRED: _REVIEW_RESOURCES,
    ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED: _HUMAN_RESOURCES,
    ImprovementChangeClass.PROHIBITED_AUTONOMOUS: _HUMAN_RESOURCES
    | frozenset(
        {"approval-bypass", "force-push", "history-rewrite", "receipt-delete", "secret-export"}
    ),
}


_SCHEMA = r"""
pragma foreign_keys=on;
create table improvement_schema(singleton integer primary key check(singleton=1),version integer not null);
insert into improvement_schema values(1,3);
create table improvement_candidate(candidate_digest text primary key,candidate_id text unique not null,novelty_digest text unique not null,failure_card_digest text not null,baseline_aggregate_digest text not null,change_class text not null,proposer_ref text not null,created_at text not null,body_json text not null,check(change_class in('AUTO_SAFE','REVIEW_REQUIRED','HUMAN_APPROVAL_REQUIRED','PROHIBITED_AUTONOMOUS')),unique(failure_card_digest,novelty_digest)) strict;
create table validator_manifest(manifest_digest text primary key,candidate_digest text unique not null references improvement_candidate,builder_ref text not null,verifier_ref text not null,created_at text not null,body_json text not null,check(builder_ref<>verifier_ref)) strict;
create table attempt_claim(claim_digest text primary key,candidate_digest text not null references improvement_candidate,ordinal integer not null,reserved_provider_calls integer not null,reserved_tokens integer not null,reserved_cost_micros integer not null,started_at text not null,body_json text not null,unique(candidate_digest,ordinal),check(ordinal>0 and reserved_provider_calls>=0 and reserved_tokens>0 and reserved_cost_micros>0)) strict;
create table improvement_evaluation(evaluation_digest text primary key,claim_digest text unique not null references attempt_claim,candidate_digest text not null references improvement_candidate,after_aggregate_digest text not null,validator_manifest_digest text not null references validator_manifest,state text not null,evaluator_ref text not null,verifier_ref text not null,finished_at text not null,body_json text not null,check(state in('improved','target-reached','plateau','regressed','timeout','budget-exceeded','failed')),check(evaluator_ref<>verifier_ref)) strict;
create table operational_execution_claim(claim_digest text primary key,effect_digest text not null,candidate_digest text not null references improvement_candidate,evaluation_digest text not null references improvement_evaluation,operation text not null,policy_digest text not null,runner_ref text not null,claimed_at text not null,body_json text not null,check(operation in('shadow','canary','activation','rollback'))) strict;
create table operational_runner_receipt(runner_receipt_digest text primary key,claim_digest text unique not null references operational_execution_claim,status text not null,evidence_digest text not null,runner_ref text not null,finished_at text not null,body_json text not null,check(status in('completed','failed','ambiguous','recovery-required'))) strict;
create table operational_execution_receipt(receipt_digest text primary key,claim_digest text unique not null references operational_execution_claim,runner_receipt_digest text unique not null references operational_runner_receipt,candidate_digest text not null references improvement_candidate,evaluation_digest text not null references improvement_evaluation,operation text not null,status text not null,executor_ref text not null,verifier_ref text not null,verified_at text not null,body_json text not null,check(operation in('shadow','canary','activation','rollback')),check(status in('completed','failed','ambiguous','recovery-required')),check(executor_ref<>verifier_ref)) strict;
create table rollout_receipt(rollout_digest text primary key,candidate_digest text not null references improvement_candidate,evaluation_digest text not null references improvement_evaluation,stage text not null,status text not null,created_at text not null,body_json text not null,unique(candidate_digest,stage),check(stage in('shadow','canary')),check(status in('completed','failed'))) strict;
create table improvement_review(review_digest text primary key,candidate_digest text unique not null references improvement_candidate,evaluation_digest text unique not null references improvement_evaluation,reviewer_ref text not null,approved integer not null,created_at text not null,body_json text not null,check(approved in(0,1))) strict;
create table improvement_activation(activation_digest text primary key,candidate_digest text unique not null references improvement_candidate,evaluation_digest text not null references improvement_evaluation,review_digest text not null references improvement_review,shadow_digest text not null references rollout_receipt,canary_digest text not null references rollout_receipt,claim_digest text unique not null,effect_digest text unique not null,receipt_digest text unique not null,activated_at text not null,body_json text not null) strict;
create table rollback_receipt(rollback_digest text primary key,activation_digest text unique not null references improvement_activation,status text not null,created_at text not null,body_json text not null,check(status in('completed','failed'))) strict;
create table learning_feedback(feedback_digest text primary key,candidate_digest text unique not null references improvement_candidate,evaluation_digest text not null references improvement_evaluation,outcome_receipt_digest text not null,created_at text not null,body_json text not null) strict;
"""
for _table in (
    "improvement_candidate",
    "validator_manifest",
    "attempt_claim",
    "improvement_evaluation",
    "operational_execution_claim",
    "operational_runner_receipt",
    "operational_execution_receipt",
    "rollout_receipt",
    "improvement_review",
    "improvement_activation",
    "rollback_receipt",
    "learning_feedback",
):
    _SCHEMA += f"create trigger {_table}_no_update before update on {_table} begin select raise(abort,'append-only'); end;\n"
    _SCHEMA += f"create trigger {_table}_no_delete before delete on {_table} begin select raise(abort,'append-only'); end;\n"


def _text(value: object, label: str, *, maximum: int = 4096) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value.encode()) > maximum
        or _SECRET.search(value)
    ):
        raise ValidationFailed(f"Local improvement bounded {label} required")
    return value


def _safe(value: object, label: str) -> str:
    if type(value) is not str or not _SAFE.fullmatch(value) or _SECRET.search(value):
        raise ValidationFailed(f"Local improvement {label} invalid")
    return value


def _normalized_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    tokens: set[str] = set()
    for token in re.findall(r"[\w]+", normalized, flags=re.UNICODE):
        if token in _IDENTITY_STOPWORDS:
            continue
        stem = _IDENTITY_EXACT_STEMS.get(token, token)
        if stem == token and len(stem) > 4 and stem.endswith("s") and not stem.endswith("ss"):
            stem = stem[:-1]
        for suffix in (
            "mektedir",
            "maktadır",
            "maktadir",
            "mistir",
            "mustur",
            "iyor",
            "ıyor",
            "uyor",
            "ildi",
            "ıldı",
            "uldu",
        ):
            if len(stem) > len(suffix) + 3 and stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        if stem:
            tokens.add(stem)
    return f"{_IDENTITY_VERSION}:" + " ".join(sorted(tokens))


def _instant(value: dt.datetime) -> str:
    if type(value) is not dt.datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed("Local improvement timestamp must be timezone-aware")
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()


def _parse_time(value: object) -> dt.datetime:
    if type(value) is not str:
        raise PolicyViolation("Stored local improvement timestamp type drift")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise PolicyViolation("Stored local improvement timestamp drift") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PolicyViolation("Stored local improvement timestamp lacks timezone")
    return parsed.astimezone(dt.UTC)


def _body(value: Mapping[str, object]) -> tuple[str, str]:
    raw = canonical_json(value)
    if len(raw.encode()) > MAX_BODY_BYTES:
        raise ValidationFailed("Local improvement body exceeds bound")
    return raw, digest(value)


def _document(raw: object) -> dict[str, Any]:
    if type(raw) is not str or not 1 <= len(raw.encode()) <= MAX_BODY_BYTES:
        raise PolicyViolation("Stored local improvement body size/type drift")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise PolicyViolation("Stored local improvement body invalid") from exc
    if type(value) is not dict or canonical_json(value) != raw:
        raise PolicyViolation("Stored local improvement body not canonical")
    return value


def _schema_digest(db: sqlite3.Connection) -> str:
    rows = db.execute(
        "select type,name,sql from sqlite_master where type in ('table','trigger') "
        "and name not like 'sqlite_%' order by type,name"
    ).fetchall()
    return digest([{"type": str(row[0]), "name": str(row[1]), "sql": str(row[2])} for row in rows])


def _metric_aggregate(values: list[float]) -> dict[str, float]:
    if not values or any(type(value) is not float or not math.isfinite(value) for value in values):
        raise PolicyViolation("Improvement benchmark trial metric drift")
    ordered = sorted(values)
    rank = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "mean": statistics.fmean(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[rank],
        "variance": statistics.pvariance(ordered),
    }


with closing(sqlite3.connect(":memory:")) as _schema_db:
    _schema_db.executescript(_SCHEMA)
    SCHEMA_DIGEST = _schema_digest(_schema_db)


def _source(path: Path, expected: str) -> sqlite3.Connection:
    if not path.is_absolute() or path.is_symlink():
        raise ValidationFailed("Local improvement source path invalid")
    if not private_regular(path):
        raise PolicyViolation("Local improvement source identity invalid")
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("pragma query_only=on")
    db.execute("pragma foreign_keys=on")
    if _schema_digest(db) != expected:
        db.close()
        raise PolicyViolation("Local improvement source schema drift")
    return db


@dataclass(frozen=True, slots=True)
class ImprovementCandidate:
    candidate_id: UUID
    objective: str
    observed_problem: str
    failure_card_digest: str
    baseline_aggregate_digest: str
    hypothesis: str
    patch_digest: str
    change_class: ImprovementChangeClass
    allowed_resources: tuple[str, ...]
    metric_specs: tuple[MetricSpec, ...]
    regression_guards: tuple[str, ...]
    evaluation_plan_digest: str
    max_iterations: int
    max_provider_calls: int
    max_tokens: int
    max_cost_micros: int
    wall_clock_seconds: int
    rollback_plan: str
    proposer_ref: str
    source_revision: str
    created_at: dt.datetime

    def __post_init__(self) -> None:
        if type(self.candidate_id) is not UUID:
            raise ValidationFailed("Exact improvement candidate UUID required")
        for value in (
            self.failure_card_digest,
            self.baseline_aggregate_digest,
            self.patch_digest,
            self.evaluation_plan_digest,
        ):
            parse_digest(value)
        for value, label in (
            (self.objective, "objective"),
            (self.observed_problem, "observed problem"),
            (self.hypothesis, "hypothesis"),
            (self.rollback_plan, "rollback plan"),
        ):
            _text(value, label)
        _safe(self.proposer_ref, "proposer")
        _safe(self.source_revision, "source revision")
        if type(self.change_class) is not ImprovementChangeClass:
            raise ValidationFailed("Exact improvement change class required")
        if (
            type(self.allowed_resources) is not tuple
            or not self.allowed_resources
            or len(self.allowed_resources) > 32
            or tuple(sorted(set(self.allowed_resources))) != self.allowed_resources
            or type(self.regression_guards) is not tuple
            or not self.regression_guards
            or len(self.regression_guards) > 32
            or tuple(sorted(set(self.regression_guards))) != self.regression_guards
        ):
            raise ValidationFailed("Improvement resources and guards must be canonical")
        for value in (*self.allowed_resources, *self.regression_guards):
            _safe(value, "resource or guard")
        if not set(self.allowed_resources) <= _CLASS_RESOURCES[self.change_class]:
            raise PolicyViolation("Improvement resource is forbidden for its change class")
        if (
            type(self.metric_specs) is not tuple
            or not self.metric_specs
            or len(self.metric_specs) > 20
            or any(type(item) is not MetricSpec for item in self.metric_specs)
            or tuple(item.metric_id for item in self.metric_specs)
            != tuple(sorted({item.metric_id for item in self.metric_specs}))
            or any(item.metric_id not in _BENCHMARK_METRICS for item in self.metric_specs)
            or not any(item.role is MetricRole.PRIMARY for item in self.metric_specs)
        ):
            raise ValidationFailed("Improvement metric specs invalid")
        for item in self.metric_specs:
            item.__post_init__()
            if (
                type(item.direction) is not MetricDirection
                or type(item.role) is not MetricRole
                or type(item.aggregation) is not MetricAggregation
                or any(
                    value is not None and type(value) is not float
                    for value in (item.target_value, item.min_value, item.max_value)
                )
                or type(item.minimum_meaningful_delta) is not float
                or type(item.regression_tolerance) is not float
            ):
                raise ValidationFailed("Improvement metric spec exact types required")
        if any(
            type(value) is not int or isinstance(value, bool)
            for value in (
                self.max_iterations,
                self.max_provider_calls,
                self.max_tokens,
                self.max_cost_micros,
                self.wall_clock_seconds,
            )
        ) or not (
            1 <= self.max_iterations <= 20
            and 0 <= self.max_provider_calls <= 4096
            and 1 <= self.max_tokens <= 10_000_000
            and 1 <= self.max_cost_micros <= 1_000_000_000
            and 1 <= self.wall_clock_seconds <= 86_400
        ):
            raise ValidationFailed("Improvement budgets invalid")
        _instant(self.created_at)

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-local-improvement-candidate/v1",
            "candidate_id": str(self.candidate_id),
            "objective": self.objective,
            "observed_problem": self.observed_problem,
            "failure_card_digest": self.failure_card_digest,
            "baseline_aggregate_digest": self.baseline_aggregate_digest,
            "hypothesis": self.hypothesis,
            "patch_digest": self.patch_digest,
            "change_class": self.change_class.value,
            "allowed_resources": list(self.allowed_resources),
            "metric_specs": [item.as_dict() for item in self.metric_specs],
            "regression_guards": list(self.regression_guards),
            "evaluation_plan_digest": self.evaluation_plan_digest,
            "max_iterations": self.max_iterations,
            "max_provider_calls": self.max_provider_calls,
            "max_tokens": self.max_tokens,
            "max_cost_micros": self.max_cost_micros,
            "wall_clock_seconds": self.wall_clock_seconds,
            "rollback_plan": self.rollback_plan,
            "proposer_ref": self.proposer_ref,
            "source_revision": self.source_revision,
            "created_at": _instant(self.created_at),
        }

    @property
    def candidate_digest(self) -> str:
        return digest(self.body())

    @property
    def novelty_digest(self) -> str:
        """Bind one bounded attempt family to independently evidenced failure identity.

        Free-form objective, hypothesis, patch text, candidate UUID and lexical
        normalization are deliberately excluded.  Otherwise a proposer could
        mint a second attempt merely by paraphrasing the same hypothesis.  A
        materially new attempt therefore requires a new independently-evidenced
        failure-card revision.  Caller-supplied source revision, change class,
        resource set or benchmark identity cannot mint a second family.
        """
        return digest(
            {
                "schema": "zekam-improvement-prior-attempt-identity/v1",
                "failure_card_digest": self.failure_card_digest,
            }
        )


@dataclass(frozen=True, slots=True)
class AttemptReservation:
    candidate_digest: str
    reserved_provider_calls: int
    reserved_tokens: int
    reserved_cost_micros: int

    def __post_init__(self) -> None:
        parse_digest(self.candidate_digest)
        if (
            any(
                type(value) is not int or isinstance(value, bool) or value < 0
                for value in (
                    self.reserved_provider_calls,
                    self.reserved_tokens,
                    self.reserved_cost_micros,
                )
            )
            or self.reserved_tokens < 1
            or self.reserved_cost_micros < 1
        ):
            raise ValidationFailed("Improvement reservation invalid")


@dataclass(frozen=True, slots=True)
class EvaluationReceipt:
    evaluation_digest: str
    state: str
    progress_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.evaluation_digest)
        parse_digest(self.progress_digest)
        if self.state not in {
            "improved",
            "target-reached",
            "plateau",
            "regressed",
            "timeout",
            "budget-exceeded",
            "failed",
        }:
            raise ValidationFailed("Improvement evaluation state invalid")


@dataclass(frozen=True, slots=True)
class OperationalExecutionReceipt:
    """A terminal, independently attested observation of one bounded operation."""

    operation: str
    candidate_digest: str
    evaluation_digest: str | None
    policy_digest: str
    evidence_digest: str
    executor_ref: str
    verifier_ref: str
    status: str
    started_at: dt.datetime
    finished_at: dt.datetime
    external_effect_count: int = 0

    def __post_init__(self) -> None:
        if type(self.operation) is not str or self.operation not in {
            "shadow",
            "canary",
            "activation",
            "rollback",
        }:
            raise ValidationFailed("Improvement operational receipt operation invalid")
        parse_digest(self.candidate_digest)
        parse_digest(self.policy_digest)
        parse_digest(self.evidence_digest)
        if self.evaluation_digest is not None:
            parse_digest(self.evaluation_digest)
        _safe(self.executor_ref, "execution identity")
        _safe(self.verifier_ref, "execution verifier")
        if self.executor_ref == self.verifier_ref:
            raise PolicyViolation("Operational execution must have an independent verifier")
        if type(self.status) is not str or self.status not in {
            "completed",
            "failed",
            "ambiguous",
            "recovery-required",
        }:
            raise ValidationFailed("Improvement operational receipt status invalid")
        if (
            type(self.external_effect_count) is not int
            or isinstance(self.external_effect_count, bool)
            or not 0 <= self.external_effect_count <= 1
        ):
            raise ValidationFailed("Improvement operational effect count invalid")
        _instant(self.started_at)
        _instant(self.finished_at)
        if self.finished_at.astimezone(dt.UTC) <= self.started_at.astimezone(dt.UTC):
            raise ValidationFailed("Operational receipt must finish after its claim")
        if self.operation in {"shadow", "canary"} and self.external_effect_count != 0:
            raise PolicyViolation("Shadow/canary receipt cannot claim a production effect")
        if self.status != "completed" and self.external_effect_count != 0:
            raise PolicyViolation("Non-terminal-success receipt cannot claim an effect")

    @property
    def effect_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-improvement-operational-effect/v1",
                "operation": self.operation,
                "candidate_digest": self.candidate_digest,
                "evaluation_digest": self.evaluation_digest,
                "policy_digest": self.policy_digest,
            }
        )

    @property
    def claim_body(self) -> dict[str, object]:
        return {
            "schema": "zekam-improvement-operational-claim/v2",
            "effect_digest": self.effect_digest,
            "candidate_digest": self.candidate_digest,
            "evaluation_digest": self.evaluation_digest,
            "operation": self.operation,
            "policy_digest": self.policy_digest,
            "runner_ref": self.executor_ref,
            "claimed_at": _instant(self.started_at),
        }

    @property
    def claim_digest(self) -> str:
        return digest(self.claim_body)

    @property
    def runner_receipt_body(self) -> dict[str, object]:
        return {
            "schema": "zekam-improvement-runner-receipt/v1",
            "claim_digest": self.claim_digest,
            "effect_digest": self.effect_digest,
            "status": self.status,
            "evidence_digest": self.evidence_digest,
            "runner_ref": self.executor_ref,
            "external_effect_count": self.external_effect_count,
            "finished_at": _instant(self.finished_at),
        }

    @property
    def runner_receipt_digest(self) -> str:
        return digest(self.runner_receipt_body)

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-improvement-operational-receipt/v1",
            "operation": self.operation,
            "candidate_digest": self.candidate_digest,
            "evaluation_digest": self.evaluation_digest,
            "policy_digest": self.policy_digest,
            "claim_digest": self.claim_digest,
            "effect_digest": self.effect_digest,
            "evidence_digest": self.evidence_digest,
            "executor_ref": self.executor_ref,
            "verifier_ref": self.verifier_ref,
            "status": self.status,
            "external_effect_count": self.external_effect_count,
            "started_at": _instant(self.started_at),
            "finished_at": _instant(self.finished_at),
        }

    @property
    def receipt_digest(self) -> str:
        return digest(self.body())


@final
class SQLiteLocalImprovementStore:
    def __init__(self, path: Path, learning_path: Path, benchmark_path: Path) -> None:
        if any(
            not item.is_absolute() or item.is_symlink()
            for item in (path, learning_path, benchmark_path)
        ):
            raise ValidationFailed("Local improvement paths must be absolute non-symlinks")
        self.path = path
        self.learning_path = learning_path
        self.benchmark_path = benchmark_path

    def bootstrap(self) -> None:
        created = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            restrict_private_tree(self.path.parent)
        if not private_directory(self.path.parent):
            raise PolicyViolation("Local improvement private parent required")
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(_SCHEMA)
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        if not private_regular(self.path):
            raise PolicyViolation("Local improvement database identity invalid")
        db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        db.execute("pragma busy_timeout=5000")
        if (
            db.execute("select version from improvement_schema").fetchone()[0] != 3
            or _schema_digest(db) != SCHEMA_DIGEST
        ):
            db.close()
            raise PolicyViolation("Local improvement schema drift")
        return db

    def _failure_card(self, value: str) -> dict[str, Any]:
        parse_digest(value)
        with closing(_source(self.learning_path, LEARNING_SCHEMA_DIGEST)) as db:
            row = db.execute(
                "select body_json,signature_digest from failure_card where card_digest=?", (value,)
            ).fetchone()
            if row is None:
                raise PolicyViolation("Improvement requires stored WP-09 failure card")
            body = _document(row["body_json"])
            occurrences = db.execute(
                "select occurrence_digest,evidence_digest,body_json from failure_occurrence where signature_digest=? limit 4097",
                (row["signature_digest"],),
            ).fetchall()
        if len(occurrences) > 4096:
            raise PolicyViolation("Improvement failure evidence bound exceeded")
        valid_evidence: set[str] = set()
        for occurrence in occurrences:
            occurrence_body = _document(occurrence["body_json"])
            if (
                digest(occurrence_body) != occurrence["occurrence_digest"]
                or occurrence_body.get("signature_digest") != row["signature_digest"]
                or occurrence_body.get("evidence_digest") != occurrence["evidence_digest"]
            ):
                raise PolicyViolation("Improvement failure occurrence drift")
            valid_evidence.add(str(occurrence["evidence_digest"]))
        if digest(body) != value or len(valid_evidence) < 2:
            raise PolicyViolation("Improvement failure evidence drift")
        return body

    def _aggregate(self, value: str) -> tuple[dict[str, Any], str, dict[str, object]]:
        parse_digest(value)
        with closing(_source(self.benchmark_path, BENCHMARK_SCHEMA_DIGEST)) as db:
            row = db.execute(
                "select a.body_json,p.plan_digest,p.suite_digest,p.model_id,p.repetitions,p.body_json,p.plan_id "
                "from benchmark_aggregate a join benchmark_plan p on p.plan_id=a.plan_id "
                "where a.aggregate_digest=?",
                (value,),
            ).fetchone()
            binding_row = (
                None
                if row is None
                else db.execute(
                    "select body_json from contract where kind='plan-binding' and digest=?",
                    (row["plan_digest"],),
                ).fetchone()
            )
            trials = (
                ()
                if row is None
                else db.execute(
                    "select * from benchmark_trial where plan_id=? order by fixture_digest,repetition",
                    (row["plan_id"],),
                ).fetchall()
            )
            execution_rows: list[dict[str, str]] = []
            trial_documents: list[dict[str, Any]] = []
            execution_tokens = 0
            execution_cost_micros = 0
            for trial in trials:
                trial_body = _document(trial["body_json"])
                if (
                    digest(trial_body) != trial["trial_digest"]
                    or trial_body.get("fixture_digest") != trial["fixture_digest"]
                    or trial_body.get("repetition") != trial["repetition"]
                    or trial_body.get("status") != trial["status"]
                    or trial_body.get("evidence_digest") != trial["evidence_digest"]
                    or trial_body.get("response_digest") != trial["response_digest"]
                    or trial_body.get("status") != "passed"
                    or any(
                        trial_body.get(flag) is not True
                        for flag in (
                            "parse_ok",
                            "format_ok",
                            "evidence_ok",
                            "verifier_approved",
                        )
                    )
                ):
                    raise PolicyViolation("Improvement benchmark trial receipt drift")
                if (
                    type(trial_body.get("input_tokens")) is not int
                    or type(trial_body.get("output_tokens")) is not int
                    or trial_body["input_tokens"] < 0
                    or trial_body["output_tokens"] < 0
                    or type(trial_body.get("actual_cost")) is not float
                    or not math.isfinite(trial_body["actual_cost"])
                    or trial_body["actual_cost"] < 0.0
                ):
                    raise PolicyViolation("Improvement benchmark execution usage drift")
                execution_tokens += trial_body["input_tokens"] + trial_body["output_tokens"]
                execution_cost_micros += round(trial_body["actual_cost"] * 1_000_000)
                trial_documents.append(trial_body)
                claims: list[tuple[str, sqlite3.Row]] = []
                for phase, claim_id in (
                    ("tested", trial["tested_claim_id"]),
                    ("verifier", trial["verifier_claim_id"]),
                ):
                    if type(claim_id) is not str:
                        raise PolicyViolation("Improvement benchmark terminal receipt missing")
                    claim_row = db.execute(
                        "select c.*,r.receipt_digest,r.status as receipt_status,r.result_digest,"
                        "r.evidence_digest as receipt_evidence,r.body_json as receipt_body "
                        "from call_claim c join call_receipt r on r.claim_id=c.claim_id "
                        "where c.claim_id=?",
                        (claim_id,),
                    ).fetchone()
                    if claim_row is None:
                        raise PolicyViolation("Improvement benchmark terminal receipt missing")
                    claim_body = _document(claim_row["body_json"])
                    receipt_body = _document(claim_row["receipt_body"])
                    expected_effect = (
                        benchmark_effect_digest(
                            str(row["plan_digest"]),
                            str(trial["fixture_digest"]),
                            int(trial["repetition"]),
                        )
                        if phase == "tested"
                        else benchmark_verifier_effect_digest(
                            str(row["plan_digest"]),
                            str(trial["fixture_digest"]),
                            int(trial["repetition"]),
                            str(claim_row["model_id"]),
                            str(trial["response_digest"]),
                        )
                    )
                    expected_claim_body = {
                        "schema": "zekam-benchmark-call-claim/v1",
                        "claim_id": claim_id,
                        "plan_digest": str(row["plan_digest"]),
                        "phase": phase,
                        "fixture_digest": str(trial["fixture_digest"]),
                        "repetition": int(trial["repetition"]),
                        "model_id": str(claim_row["model_id"]),
                        "effect_digest": expected_effect,
                    }
                    expected_result = (
                        trial["response_digest"] if phase == "tested" else trial["evidence_digest"]
                    )
                    expected_receipt_body = {
                        "schema": "zekam-benchmark-call-receipt/v1",
                        "claim_id": claim_id,
                        "status": "completed",
                        "result_digest": expected_result,
                        "failure_category": None,
                        "evidence_digest": str(trial["evidence_digest"]),
                    }
                    if (
                        claim_row["plan_digest"] != row["plan_digest"]
                        or claim_row["phase"] != phase
                        or claim_row["fixture_digest"] != trial["fixture_digest"]
                        or claim_row["repetition"] != trial["repetition"]
                        or claim_body != expected_claim_body
                        or claim_row["effect_digest"] != expected_effect
                        or receipt_body != expected_receipt_body
                        or claim_row["receipt_status"] != "completed"
                        or digest(receipt_body) != claim_row["receipt_digest"]
                    ):
                        raise PolicyViolation("Improvement benchmark claim/receipt drift")
                    if claim_row["result_digest"] != expected_result:
                        raise PolicyViolation("Improvement benchmark receipt result drift")
                    claims.append((phase, claim_row))
                if claims[0][1]["model_id"] == claims[1][1]["model_id"]:
                    raise PolicyViolation("Improvement benchmark verifier independence drift")
                execution_rows.append(
                    {
                        "trial_digest": str(trial["trial_digest"]),
                        "tested_receipt_digest": str(claims[0][1]["receipt_digest"]),
                        "verifier_receipt_digest": str(claims[1][1]["receipt_digest"]),
                    }
                )
        if row is None or binding_row is None:
            raise PolicyViolation("Improvement requires stored WP-11 aggregate")
        body = _document(row["body_json"])
        plan = _document(row[5])
        binding = _document(binding_row[0])
        confidence = body.get("confidence_interval")
        confidence_95 = body.get("confidence_95")
        if (
            digest(body) != value
            or body.get("schema") != "zekam-benchmark-aggregate/v1"
            or body.get("approved") is not True
            or body.get("unsafe") is not False
            or body.get("tested_model_id") != row["model_id"]
            or type(body.get("trial_count")) is not int
            or body["trial_count"] < 5
            or type(body.get("pass_rate")) is not float
            or not 0.0 <= body["pass_rate"] <= 1.0
            or len(trials) < 5
            or len(trials) != body.get("trial_count")
            or any(
                type(body.get(group)) is not dict
                or frozenset(body[group]) != {"mean", "median", "p95", "variance"}
                or any(
                    type(item) is not float or not math.isfinite(item)
                    for item in body[group].values()
                )
                for group in _MANDATORY_BENCHMARK_GROUPS
            )
            or type(confidence) is not dict
            or frozenset(confidence) != {"low", "high"}
            or any(
                type(item) is not float or not math.isfinite(item) for item in confidence.values()
            )
            or not 0.0 <= confidence["low"] <= confidence["high"] <= 1.0
            or type(confidence_95) is not list
            or len(confidence_95) != 2
            or any(type(item) is not float or not math.isfinite(item) for item in confidence_95)
            or confidence_95 != [confidence["low"], confidence["high"]]
        ):
            raise PolicyViolation("Improvement aggregate canonical drift")
        token_totals = [
            float(item["input_tokens"] + item["output_tokens"]) for item in trial_documents
        ]
        token_floor = max(1.0, min(token_totals))
        recalculated = {
            "quality": _metric_aggregate([float(item["quality"]) for item in trial_documents]),
            "reliability": _metric_aggregate(
                [float(item["reliability"]) for item in trial_documents]
            ),
            "latency_ms": _metric_aggregate(
                [float(item["latency_ms"]) for item in trial_documents]
            ),
            "cost": _metric_aggregate([float(item["actual_cost"]) for item in trial_documents]),
            "token_count": _metric_aggregate(token_totals),
            "correctness": _metric_aggregate([float(item["quality"]) for item in trial_documents]),
            "evidence_citation": _metric_aggregate(
                [1.0 if item["evidence_ok"] else 0.0 for item in trial_documents]
            ),
            "structured_format": _metric_aggregate(
                [1.0 if item["parse_ok"] and item["format_ok"] else 0.0 for item in trial_documents]
            ),
            "safety": _metric_aggregate([1.0 for _item in trial_documents]),
            "token_efficiency": _metric_aggregate(
                [token_floor / max(1.0, value) for value in token_totals]
            ),
            "tool_correctness": _metric_aggregate(
                [float(item.get("tool_correctness", 0.0)) for item in trial_documents]
            ),
            "recovery": _metric_aggregate(
                [float(item.get("recovery", 0.0)) for item in trial_documents]
            ),
            "human_correction": _metric_aggregate(
                [float(item["human_corrections"]) for item in trial_documents]
            ),
        }
        if any(body[group] != recalculated[group] for group in _MANDATORY_BENCHMARK_GROUPS):
            raise PolicyViolation("Improvement aggregate/trial metric reconciliation drift")
        pass_rate = 1.0
        z = 1.959963984540054
        count = len(trial_documents)
        denominator = 1 + z * z / count
        centre = (pass_rate + z * z / (2 * count)) / denominator
        margin = (
            z * math.sqrt((pass_rate * (1 - pass_rate) + z * z / (4 * count)) / count) / denominator
        )
        calculated_confidence = [max(0.0, centre - margin), min(1.0, centre + margin)]
        if (
            body["pass_rate"] != pass_rate
            or body["confidence_95"] != calculated_confidence
            or body["confidence_interval"]
            != {"low": calculated_confidence[0], "high": calculated_confidence[1]}
        ):
            raise PolicyViolation("Improvement aggregate confidence reconciliation drift")
        plan_keys = {
            "schema",
            "model_id",
            "suite_digest",
            "inventory_digest",
            "policy_digest",
            "fixture_registry_digest",
            "repetitions",
            "remote_execution",
            "input_binding_digest",
        }
        binding_keys = {
            "schema",
            "plan_digest",
            "suite_digest",
            "task_digest",
            "fixture_digest",
            "prompt_digest",
            "hidden_key_digest",
            "grader_digest",
        }
        if (
            set(plan) != plan_keys
            or set(binding) != binding_keys
            or plan.get("schema") != "zekam-benchmark-plan/v1"
            or binding.get("schema") != "zekam-benchmark-plan-input-binding/v1"
            or plan.get("model_id") != row["model_id"]
            or plan.get("suite_digest") != row["suite_digest"]
            or plan.get("repetitions") != row["repetitions"]
            or plan.get("remote_execution") is not False
            or binding.get("plan_digest") != row["plan_digest"]
            or binding.get("suite_digest") != row["suite_digest"]
            or digest(binding) != plan.get("input_binding_digest")
            or any(
                type(plan.get(key)) is not str
                for key in (
                    "inventory_digest",
                    "policy_digest",
                    "fixture_registry_digest",
                    "input_binding_digest",
                )
            )
            or any(type(binding.get(key)) is not str for key in binding_keys - {"schema"})
        ):
            raise PolicyViolation("Improvement benchmark plan binding drift")
        contract = digest(
            {
                "suite_digest": binding["suite_digest"],
                "task_digest": binding["task_digest"],
                "fixture_digest": binding["fixture_digest"],
                "prompt_digest": binding["prompt_digest"],
                "hidden_key_digest": binding["hidden_key_digest"],
                "grader_digest": binding["grader_digest"],
                "policy_digest": plan["policy_digest"],
                "fixture_registry_digest": plan["fixture_registry_digest"],
                "repetitions": plan["repetitions"],
            }
        )
        execution_receipt_digest = digest(
            {
                "schema": "zekam-improvement-isolated-benchmark-receipt/v1",
                "aggregate_digest": value,
                "plan_digest": row["plan_digest"],
                "remote_execution": False,
                "trials": execution_rows,
            }
        )
        return (
            body,
            contract,
            {
                "receipt_digest": execution_receipt_digest,
                "provider_calls": len(execution_rows) * 2,
                "tokens": execution_tokens,
                "cost_micros": execution_cost_micros,
            },
        )

    @staticmethod
    def _mandatory_regressions(
        baseline: dict[str, Any], current: dict[str, Any]
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        for group in _MANDATORY_BENCHMARK_GROUPS:
            baseline_bucket = baseline[group]
            current_bucket = current[group]
            baseline_mean = baseline_bucket["mean"]
            current_mean = current_bucket["mean"]
            if group in _MANDATORY_MAXIMIZE and current_mean < baseline_mean:
                reasons.append(f"{group}.mean")
            if group in _MANDATORY_MINIMIZE and current_mean > baseline_mean:
                reasons.append(f"{group}.mean")
            if current_bucket["variance"] > baseline_bucket["variance"]:
                reasons.append(f"{group}.variance")
        if current["pass_rate"] < baseline["pass_rate"]:
            reasons.append("pass_rate")
        baseline_confidence = baseline["confidence_interval"]
        current_confidence = current["confidence_interval"]
        if current_confidence["low"] < max(0.5, baseline_confidence["low"]):
            reasons.append("confidence.lower-bound")
        if (
            current_confidence["high"] - current_confidence["low"]
            > baseline_confidence["high"] - baseline_confidence["low"]
        ):
            reasons.append("confidence.width")
        return tuple(sorted(set(reasons)))

    def propose(self, candidate: ImprovementCandidate) -> tuple[str, bool]:
        if type(candidate) is not ImprovementCandidate:
            raise ValidationFailed("Exact improvement candidate required")
        candidate.__post_init__()
        self._failure_card(candidate.failure_card_digest)
        _, baseline_contract, _ = self._aggregate(candidate.baseline_aggregate_digest)
        if baseline_contract != candidate.evaluation_plan_digest:
            raise PolicyViolation("Improvement candidate benchmark plan binding drift")
        raw, value = _body(candidate.body())
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select candidate_digest,body_json from improvement_candidate where candidate_id=? or novelty_digest=?",
                (str(candidate.candidate_id), candidate.novelty_digest),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Improvement candidate identity/novelty drift")
                db.rollback()
                return value, False
            db.execute(
                "insert into improvement_candidate values(?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    str(candidate.candidate_id),
                    candidate.novelty_digest,
                    candidate.failure_card_digest,
                    candidate.baseline_aggregate_digest,
                    candidate.change_class.value,
                    candidate.proposer_ref,
                    _instant(candidate.created_at),
                    raw,
                ),
            )
            db.commit()
        return value, True

    def freeze_validators(
        self, candidate: ImprovementCandidate, manifest: ValidatorAssetManifest
    ) -> str:
        if (
            type(candidate) is not ImprovementCandidate
            or type(manifest) is not ValidatorAssetManifest
        ):
            raise ValidationFailed("Exact candidate and validator manifest required")
        candidate.__post_init__()
        manifest.__post_init__()
        if (
            manifest.objective_id != candidate.candidate_id
            or manifest.validator_spec_digest != candidate.evaluation_plan_digest
            or manifest.source_revision != candidate.source_revision
            or str(manifest.builder_assignment_id) == candidate.proposer_ref
            or str(manifest.verifier_assignment_id) == candidate.proposer_ref
            or manifest.created_at <= candidate.created_at
        ):
            raise PolicyViolation("Validator freeze candidate/source/identity drift")
        raw, value = _body(manifest.as_dict())
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            row = db.execute(
                "select body_json from improvement_candidate where candidate_digest=?",
                (candidate.candidate_digest,),
            ).fetchone()
            if row is None or row[0] != canonical_json(candidate.body()):
                raise PolicyViolation("Validator freeze requires exact stored candidate")
            existing = db.execute(
                "select manifest_digest,body_json from validator_manifest where candidate_digest=?",
                (candidate.candidate_digest,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Validator manifest replay drift")
                return value
            db.execute(
                "insert into validator_manifest values(?,?,?,?,?,?)",
                (
                    value,
                    candidate.candidate_digest,
                    str(manifest.builder_assignment_id),
                    str(manifest.verifier_assignment_id),
                    _instant(manifest.created_at),
                    raw,
                ),
            )
            db.commit()
        return value

    def claim_attempt(self, reservation: AttemptReservation, *, now: dt.datetime) -> str:
        if type(reservation) is not AttemptReservation:
            raise ValidationFailed("Exact improvement reservation required")
        reservation.__post_init__()
        timestamp = _instant(now)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            row = db.execute(
                "select body_json,created_at from improvement_candidate where candidate_digest=?",
                (reservation.candidate_digest,),
            ).fetchone()
            manifest = db.execute(
                "select manifest_digest,created_at from validator_manifest where candidate_digest=?",
                (reservation.candidate_digest,),
            ).fetchone()
            if row is None or manifest is None:
                raise PolicyViolation("Attempt requires candidate and frozen validators")
            candidate = _document(row["body_json"])
            terminal = db.execute(
                "select state from improvement_evaluation where candidate_digest=? and state in('plateau','regressed','timeout','budget-exceeded','failed') limit 1",
                (reservation.candidate_digest,),
            ).fetchone()
            usage = db.execute(
                "select count(*),coalesce(sum(reserved_provider_calls),0),coalesce(sum(reserved_tokens),0),coalesce(sum(reserved_cost_micros),0) from attempt_claim where candidate_digest=?",
                (reservation.candidate_digest,),
            ).fetchone()
            if terminal is not None:
                raise PolicyViolation("Stopped improvement cannot claim another attempt")
            replay = db.execute(
                "select claim_digest,body_json from attempt_claim where candidate_digest=? order by ordinal desc limit 1",
                (reservation.candidate_digest,),
            ).fetchone()
            if replay is not None:
                replay_body = _document(replay["body_json"])
                if (
                    replay_body.get("started_at") == timestamp
                    and replay_body.get("reserved_provider_calls")
                    == reservation.reserved_provider_calls
                    and replay_body.get("reserved_tokens") == reservation.reserved_tokens
                    and replay_body.get("reserved_cost_micros") == reservation.reserved_cost_micros
                ):
                    db.rollback()
                    return str(replay["claim_digest"])
            deadline = _parse_time(row["created_at"]) + dt.timedelta(
                seconds=int(candidate["wall_clock_seconds"])
            )
            if (
                now.astimezone(dt.UTC) >= deadline
                or timestamp <= manifest["created_at"]
                or usage[0] >= candidate["max_iterations"]
                or usage[1] + reservation.reserved_provider_calls > candidate["max_provider_calls"]
                or usage[2] + reservation.reserved_tokens > candidate["max_tokens"]
                or usage[3] + reservation.reserved_cost_micros > candidate["max_cost_micros"]
            ):
                raise PolicyViolation("Improvement stop or budget condition reached")
            ordinal = int(usage[0]) + 1
            body = {
                "schema": "zekam-local-improvement-attempt-claim/v1",
                "candidate_digest": reservation.candidate_digest,
                "ordinal": ordinal,
                "validator_manifest_digest": manifest[0],
                "reserved_provider_calls": reservation.reserved_provider_calls,
                "reserved_tokens": reservation.reserved_tokens,
                "reserved_cost_micros": reservation.reserved_cost_micros,
                "started_at": timestamp,
            }
            raw, value = _body(body)
            db.execute(
                "insert into attempt_claim values(?,?,?,?,?,?,?,?)",
                (
                    value,
                    reservation.candidate_digest,
                    ordinal,
                    reservation.reserved_provider_calls,
                    reservation.reserved_tokens,
                    reservation.reserved_cost_micros,
                    timestamp,
                    raw,
                ),
            )
            db.commit()
        return value

    @staticmethod
    def _values(aggregate: dict[str, Any], specs: tuple[MetricSpec, ...]) -> dict[str, float]:
        result: dict[str, float] = {}
        for spec in specs:
            group, field = spec.metric_id.split(".", 1)
            bucket = aggregate.get(group)
            value = None if type(bucket) is not dict else bucket.get(field)
            if type(value) is not float or not math.isfinite(value):
                raise PolicyViolation("Benchmark metric vector type/nonfinite drift")
            result[spec.metric_id] = value
        return result

    def complete_evaluation(
        self,
        candidate: ImprovementCandidate,
        claim_digest: str,
        after_aggregate_digest: str,
        *,
        artifact_before_digest: str,
        artifact_after_digest: str,
        actual_provider_calls: int,
        actual_tokens: int,
        actual_cost_micros: int,
        finished_at: dt.datetime,
        failed: bool = False,
    ) -> EvaluationReceipt:
        if type(candidate) is not ImprovementCandidate or type(failed) is not bool:
            raise ValidationFailed("Exact improvement evaluation input required")
        candidate.__post_init__()
        finished_timestamp = _instant(finished_at)
        for value in (
            claim_digest,
            after_aggregate_digest,
            artifact_before_digest,
            artifact_after_digest,
        ):
            parse_digest(value)
        if any(
            type(value) is not int or isinstance(value, bool) or value < 0
            for value in (actual_provider_calls, actual_tokens, actual_cost_micros)
        ):
            raise ValidationFailed("Improvement actual usage invalid")
        baseline, baseline_contract, baseline_execution = self._aggregate(
            candidate.baseline_aggregate_digest
        )
        current, current_contract, current_execution = self._aggregate(after_aggregate_digest)
        if (
            baseline_contract != candidate.evaluation_plan_digest
            or current_contract != candidate.evaluation_plan_digest
            or baseline.get("tested_model_id") != current.get("tested_model_id")
        ):
            raise PolicyViolation("Improvement baseline/candidate benchmark plan drift")
        if (
            actual_provider_calls != current_execution["provider_calls"]
            or actual_tokens != current_execution["tokens"]
            or actual_cost_micros != current_execution["cost_micros"]
        ):
            raise PolicyViolation("Improvement usage must match immutable benchmark receipts")
        baseline_values = self._values(baseline, candidate.metric_specs)
        current_values = self._values(current, candidate.metric_specs)
        mandatory_regressions = self._mandatory_regressions(baseline, current)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            claim = db.execute(
                "select * from attempt_claim where claim_digest=? and candidate_digest=?",
                (claim_digest, candidate.candidate_digest),
            ).fetchone()
            manifest = db.execute(
                "select * from validator_manifest where candidate_digest=?",
                (candidate.candidate_digest,),
            ).fetchone()
            stored = db.execute(
                "select body_json from improvement_candidate where candidate_digest=?",
                (candidate.candidate_digest,),
            ).fetchone()
            if (
                claim is None
                or manifest is None
                or stored is None
                or stored[0] != canonical_json(candidate.body())
            ):
                raise PolicyViolation("Evaluation exact candidate/claim/validator binding required")
            if (
                finished_timestamp <= claim["started_at"]
                or finished_timestamp <= manifest["created_at"]
            ):
                raise PolicyViolation("Evaluation must follow claim and validator freeze")
            verifier = str(manifest["verifier_ref"])
            evaluator = str(manifest["builder_ref"])
            baseline_evidence = tuple(
                MeasurementEvidence(
                    spec.metric_id,
                    baseline_values[spec.metric_id],
                    f"benchmark:{candidate.baseline_aggregate_digest}",
                    candidate.baseline_aggregate_digest,
                    candidate.source_revision,
                    candidate.created_at,
                    evaluator,
                    verifier,
                )
                for spec in candidate.metric_specs
            )
            current_evidence = tuple(
                MeasurementEvidence(
                    spec.metric_id,
                    current_values[spec.metric_id],
                    f"benchmark:{after_aggregate_digest}",
                    after_aggregate_digest,
                    candidate.source_revision,
                    finished_at,
                    evaluator,
                    verifier,
                )
                for spec in candidate.metric_specs
            )
            progress = evaluate_progress(
                candidate.metric_specs,
                baseline_evidence,
                baseline_evidence,
                current_evidence,
                cost_micros=actual_cost_micros,
            )
            deadline = candidate.created_at + dt.timedelta(seconds=candidate.wall_clock_seconds)
            if failed:
                state = "failed"
            elif finished_at.astimezone(dt.UTC) > deadline:
                state = "timeout"
            elif (
                actual_provider_calls > claim["reserved_provider_calls"]
                or actual_tokens > claim["reserved_tokens"]
                or actual_cost_micros > claim["reserved_cost_micros"]
            ):
                state = "budget-exceeded"
            elif artifact_before_digest == artifact_after_digest:
                state = "plateau"
            elif mandatory_regressions or any(item.regressed for item in progress.metric_results):
                state = "regressed"
            elif progress.progress_state is ProgressState.TARGET_REACHED:
                state = "target-reached"
            elif progress.progress_state is ProgressState.IMPROVED:
                state = "improved"
            else:
                state = "plateau"
            body = {
                "schema": "zekam-local-improvement-evaluation/v1",
                "candidate_digest": candidate.candidate_digest,
                "claim_digest": claim_digest,
                "after_aggregate_digest": after_aggregate_digest,
                "validator_manifest_digest": manifest["manifest_digest"],
                "artifact_before_digest": artifact_before_digest,
                "artifact_after_digest": artifact_after_digest,
                "baseline_values": baseline_values,
                "current_values": current_values,
                "progress": progress.as_dict(),
                "mandatory_regressions": list(mandatory_regressions),
                "actual_provider_calls": actual_provider_calls,
                "actual_tokens": actual_tokens,
                "actual_cost_micros": actual_cost_micros,
                "state": state,
                "evaluator_ref": evaluator,
                "verifier_ref": verifier,
                "finished_at": finished_timestamp,
                "baseline_execution_receipt_digest": baseline_execution["receipt_digest"],
                "isolated_execution_receipt_digest": current_execution["receipt_digest"],
            }
            raw, value = _body(body)
            existing = db.execute(
                "select evaluation_digest,state,body_json from improvement_evaluation where claim_digest=?",
                (claim_digest,),
            ).fetchone()
            if existing is not None:
                if (existing["evaluation_digest"], existing["body_json"]) != (value, raw):
                    raise ConcurrencyConflict("Improvement evaluation replay drift")
                db.rollback()
                return EvaluationReceipt(value, str(existing["state"]), progress.progress_digest)
            db.execute(
                "insert into improvement_evaluation values(?,?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    claim_digest,
                    candidate.candidate_digest,
                    after_aggregate_digest,
                    manifest["manifest_digest"],
                    state,
                    evaluator,
                    verifier,
                    finished_timestamp,
                    raw,
                ),
            )
            db.commit()
        return EvaluationReceipt(value, state, progress.progress_digest)

    def _assert_operation_ready(
        self, db: sqlite3.Connection, receipt: OperationalExecutionReceipt
    ) -> None:
        binding = db.execute(
            "select e.state,e.finished_at,m.builder_ref,m.verifier_ref "
            "from improvement_evaluation e join validator_manifest m "
            "on m.manifest_digest=e.validator_manifest_digest "
            "where e.evaluation_digest=? and e.candidate_digest=?",
            (receipt.evaluation_digest, receipt.candidate_digest),
        ).fetchone()
        if (
            receipt.evaluation_digest is None
            or binding is None
            or binding["state"] not in {"improved", "target-reached"}
            or receipt.executor_ref != binding["builder_ref"]
            or receipt.verifier_ref != binding["verifier_ref"]
            or _instant(receipt.started_at) <= binding["finished_at"]
        ):
            raise PolicyViolation("Operational claim identity/order binding invalid")
        expected_policy: str | None = None
        predecessor_time = str(binding["finished_at"])
        if receipt.operation == "shadow":
            expected_policy = receipt.evaluation_digest
        elif receipt.operation == "canary":
            predecessor = db.execute(
                "select rollout_digest,created_at from rollout_receipt "
                "where candidate_digest=? and stage='shadow' and status='completed'",
                (receipt.candidate_digest,),
            ).fetchone()
            if predecessor is not None:
                expected_policy = str(predecessor["rollout_digest"])
                predecessor_time = str(predecessor["created_at"])
        elif receipt.operation == "activation":
            candidate = db.execute(
                "select change_class from improvement_candidate where candidate_digest=?",
                (receipt.candidate_digest,),
            ).fetchone()
            review = db.execute(
                "select review_digest,created_at from improvement_review "
                "where candidate_digest=? and evaluation_digest=? and approved=1",
                (receipt.candidate_digest, receipt.evaluation_digest),
            ).fetchone()
            rollout_count = db.execute(
                "select count(*) from rollout_receipt where candidate_digest=? "
                "and status='completed' and stage in('shadow','canary')",
                (receipt.candidate_digest,),
            ).fetchone()[0]
            if candidate is not None and candidate[0] != ImprovementChangeClass.AUTO_SAFE.value:
                raise PolicyViolation("Only reviewed AUTO_SAFE improvement may auto-activate")
            if (
                candidate is not None
                and candidate[0] == ImprovementChangeClass.AUTO_SAFE.value
                and review is not None
                and rollout_count == 2
            ):
                expected_policy = str(review["review_digest"])
                predecessor_time = str(review["created_at"])
        else:
            activation = db.execute(
                "select activation_digest,activated_at from improvement_activation "
                "where candidate_digest=? and evaluation_digest=?",
                (receipt.candidate_digest, receipt.evaluation_digest),
            ).fetchone()
            if activation is not None:
                expected_policy = str(activation["activation_digest"])
                predecessor_time = str(activation["activated_at"])
        if (
            expected_policy is None
            or receipt.policy_digest != expected_policy
            or _instant(receipt.started_at) <= predecessor_time
        ):
            raise PolicyViolation("Operational claim predecessor/policy binding invalid")

    def claim_operation(self, receipt: OperationalExecutionReceipt, *, now: dt.datetime) -> str:
        if type(receipt) is not OperationalExecutionReceipt:
            raise ValidationFailed("Exact operational claim contract required")
        receipt.__post_init__()
        if _instant(receipt.started_at) != _instant(now):
            raise PolicyViolation("Operational claim must be durable before effect")
        raw, value = _body(receipt.claim_body)
        if value != receipt.claim_digest:
            raise PolicyViolation("Operational claim digest drift")
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select claim_digest,body_json from operational_execution_claim where claim_digest=?",
                (value,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Operational claim replay drift")
                db.rollback()
                return value
            pending = db.execute(
                "select 1 from operational_execution_claim c left join operational_runner_receipt r "
                "on r.claim_digest=c.claim_digest where c.candidate_digest=? and c.operation=? "
                "and r.claim_digest is null limit 1",
                (receipt.candidate_digest, receipt.operation),
            ).fetchone()
            if pending is not None:
                raise ConcurrencyConflict("Operational effect already has a pending claim")
            self._assert_operation_ready(db, receipt)
            db.execute(
                "insert into operational_execution_claim values(?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    receipt.effect_digest,
                    receipt.candidate_digest,
                    receipt.evaluation_digest,
                    receipt.operation,
                    receipt.policy_digest,
                    receipt.executor_ref,
                    _instant(receipt.started_at),
                    raw,
                ),
            )
            db.commit()
        return value

    def record_runner_receipt(
        self, receipt: OperationalExecutionReceipt, *, now: dt.datetime
    ) -> str:
        if type(receipt) is not OperationalExecutionReceipt:
            raise ValidationFailed("Exact runner receipt contract required")
        receipt.__post_init__()
        if _instant(receipt.finished_at) != _instant(now):
            raise PolicyViolation("Runner receipt terminal time drift")
        raw, value = _body(receipt.runner_receipt_body)
        if value != receipt.runner_receipt_digest:
            raise PolicyViolation("Runner receipt digest drift")
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            claim = db.execute(
                "select body_json,runner_ref,claimed_at from operational_execution_claim "
                "where claim_digest=?",
                (receipt.claim_digest,),
            ).fetchone()
            if (
                claim is None
                or claim["body_json"] != canonical_json(receipt.claim_body)
                or claim["runner_ref"] != receipt.executor_ref
                or _instant(receipt.finished_at) <= claim["claimed_at"]
            ):
                raise PolicyViolation("Runner receipt requires prior exact durable claim")
            existing = db.execute(
                "select runner_receipt_digest,body_json from operational_runner_receipt "
                "where claim_digest=?",
                (receipt.claim_digest,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Runner receipt replay drift")
                db.rollback()
                return value
            db.execute(
                "insert into operational_runner_receipt values(?,?,?,?,?,?,?)",
                (
                    value,
                    receipt.claim_digest,
                    receipt.status,
                    receipt.evidence_digest,
                    receipt.executor_ref,
                    _instant(receipt.finished_at),
                    raw,
                ),
            )
            db.commit()
        return value

    def verify_operation(
        self,
        receipt: OperationalExecutionReceipt,
        *,
        approved: bool,
        now: dt.datetime,
    ) -> str:
        if type(receipt) is not OperationalExecutionReceipt or type(approved) is not bool:
            raise ValidationFailed("Exact independent operation verification required")
        receipt.__post_init__()
        verified_at = _instant(now)
        if verified_at < _instant(receipt.finished_at):
            raise PolicyViolation("Independent verification must follow runner receipt")
        terminal_status = receipt.status if approved else "failed"
        body = {
            "schema": "zekam-improvement-verified-operational-receipt/v1",
            "claim_digest": receipt.claim_digest,
            "runner_receipt_digest": receipt.runner_receipt_digest,
            "candidate_digest": receipt.candidate_digest,
            "evaluation_digest": receipt.evaluation_digest,
            "operation": receipt.operation,
            "status": terminal_status,
            "executor_ref": receipt.executor_ref,
            "verifier_ref": receipt.verifier_ref,
            "verification_approved": approved,
            "verified_at": verified_at,
        }
        raw, value = _body(body)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            source = db.execute(
                "select r.body_json,c.candidate_digest,c.evaluation_digest,c.operation,c.runner_ref,"
                "m.verifier_ref from operational_runner_receipt r "
                "join operational_execution_claim c on c.claim_digest=r.claim_digest "
                "join improvement_evaluation e on e.evaluation_digest=c.evaluation_digest "
                "join validator_manifest m on m.manifest_digest=e.validator_manifest_digest "
                "where r.runner_receipt_digest=? and r.claim_digest=?",
                (receipt.runner_receipt_digest, receipt.claim_digest),
            ).fetchone()
            if (
                source is None
                or source["body_json"] != canonical_json(receipt.runner_receipt_body)
                or source["candidate_digest"] != receipt.candidate_digest
                or source["evaluation_digest"] != receipt.evaluation_digest
                or source["operation"] != receipt.operation
                or source["runner_ref"] != receipt.executor_ref
                or source["verifier_ref"] != receipt.verifier_ref
            ):
                raise PolicyViolation("Independent verifier lacks exact runner receipt binding")
            existing = db.execute(
                "select receipt_digest,body_json from operational_execution_receipt "
                "where claim_digest=?",
                (receipt.claim_digest,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Operational verification replay drift")
                db.rollback()
                return value
            db.execute(
                "insert into operational_execution_receipt values(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    receipt.claim_digest,
                    receipt.runner_receipt_digest,
                    receipt.candidate_digest,
                    receipt.evaluation_digest,
                    receipt.operation,
                    terminal_status,
                    receipt.executor_ref,
                    receipt.verifier_ref,
                    verified_at,
                    raw,
                ),
            )
            db.commit()
        return value

    def _require_verified_operational_receipt(
        self, receipt: OperationalExecutionReceipt
    ) -> sqlite3.Row:
        with closing(self._connect()) as db:
            row = db.execute(
                "select * from operational_execution_receipt where claim_digest=? "
                "and runner_receipt_digest=?",
                (receipt.claim_digest, receipt.runner_receipt_digest),
            ).fetchone()
        if row is None or digest(_document(row["body_json"])) != row["receipt_digest"]:
            raise PolicyViolation("Caller receipt lacks durable runner/verifier settlement")
        return cast(sqlite3.Row, row)

    def record_rollout(
        self,
        candidate_digest: str,
        evaluation_digest: str,
        stage: str,
        *,
        receipt: OperationalExecutionReceipt | None = None,
        success: object | None = None,
        now: dt.datetime,
    ) -> str:
        for value in (candidate_digest, evaluation_digest):
            parse_digest(value)
        if type(stage) is not str or stage not in {"shadow", "canary"}:
            raise ValidationFailed("Improvement rollout input invalid")
        if success is not None or type(receipt) is not OperationalExecutionReceipt:
            raise ValidationFailed(
                "Improvement rollout input requires verified operational receipt"
            )
        receipt.__post_init__()
        verified = self._require_verified_operational_receipt(receipt)
        if (
            receipt.operation != stage
            or receipt.candidate_digest != candidate_digest
            or receipt.evaluation_digest != evaluation_digest
            or verified["operation"] != stage
            or verified["status"] != receipt.status
            or verified["verified_at"] != _instant(now)
        ):
            raise PolicyViolation("Improvement rollout receipt binding invalid")
        with closing(self._connect()) as db:
            evaluation = db.execute(
                "select state,finished_at from improvement_evaluation where evaluation_digest=? and candidate_digest=?",
                (evaluation_digest, candidate_digest),
            ).fetchone()
            shadow = db.execute(
                "select status,created_at from rollout_receipt where candidate_digest=? and stage='shadow'",
                (candidate_digest,),
            ).fetchone()
        if (
            evaluation is None
            or evaluation["state"] not in {"improved", "target-reached"}
            or _instant(receipt.started_at) <= evaluation["finished_at"]
            or (
                stage == "canary"
                and (
                    shadow is None
                    or shadow["status"] != "completed"
                    or _instant(receipt.started_at) <= shadow["created_at"]
                )
            )
        ):
            raise PolicyViolation("Improvement rollout order/evidence invalid")
        operational_receipt_digest = str(verified["receipt_digest"])
        if receipt.status != "completed":
            return operational_receipt_digest
        timestamp = _instant(now)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            evaluation = db.execute(
                "select state,finished_at from improvement_evaluation where evaluation_digest=? and candidate_digest=?",
                (evaluation_digest, candidate_digest),
            ).fetchone()
            shadow = db.execute(
                "select status,created_at from rollout_receipt where candidate_digest=? and stage='shadow'",
                (candidate_digest,),
            ).fetchone()
            if (
                evaluation is None
                or evaluation["state"] not in {"improved", "target-reached"}
                or timestamp <= evaluation["finished_at"]
                or (
                    stage == "canary"
                    and (
                        shadow is None
                        or shadow["status"] != "completed"
                        or timestamp <= shadow["created_at"]
                    )
                )
            ):
                raise PolicyViolation("Improvement rollout order/evidence invalid")
            body = {
                "schema": "zekam-local-improvement-rollout/v1",
                "candidate_digest": candidate_digest,
                "evaluation_digest": evaluation_digest,
                "stage": stage,
                "status": "completed",
                "created_at": timestamp,
                "operational_receipt_digest": operational_receipt_digest,
                "production_mutation_performed": False,
            }
            raw, value = _body(body)
            existing = db.execute(
                "select rollout_digest,body_json from rollout_receipt where candidate_digest=? and stage=?",
                (candidate_digest, stage),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Improvement rollout replay drift")
                db.rollback()
                return value
            db.execute(
                "insert into rollout_receipt values(?,?,?,?,?,?,?)",
                (value, candidate_digest, evaluation_digest, stage, body["status"], timestamp, raw),
            )
            db.commit()
        return value

    def review(
        self,
        candidate_digest: str,
        evaluation_digest: str,
        reviewer_ref: str,
        *,
        approved: bool,
        now: dt.datetime,
    ) -> str:
        for value in (candidate_digest, evaluation_digest):
            parse_digest(value)
        _safe(reviewer_ref, "reviewer")
        if type(approved) is not bool:
            raise ValidationFailed("Improvement approval must be bool")
        timestamp = _instant(now)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            evaluation = db.execute(
                "select e.state,e.evaluator_ref,e.verifier_ref,e.finished_at,c.proposer_ref "
                "from improvement_evaluation e join improvement_candidate c on c.candidate_digest=e.candidate_digest "
                "where e.evaluation_digest=? and e.candidate_digest=?",
                (evaluation_digest, candidate_digest),
            ).fetchone()
            canary = db.execute(
                "select status,created_at from rollout_receipt where candidate_digest=? and stage='canary'",
                (candidate_digest,),
            ).fetchone()
            if (
                evaluation is None
                or evaluation["state"] not in {"improved", "target-reached"}
                or canary is None
                or canary["status"] != "completed"
                or reviewer_ref
                in {
                    evaluation["evaluator_ref"],
                    evaluation["verifier_ref"],
                    evaluation["proposer_ref"],
                }
                or timestamp <= max(evaluation["finished_at"], canary["created_at"])
            ):
                raise PolicyViolation("Improvement review must be independent and after canary")
            body = {
                "schema": "zekam-local-improvement-review/v1",
                "candidate_digest": candidate_digest,
                "evaluation_digest": evaluation_digest,
                "reviewer_ref": reviewer_ref,
                "approved": approved,
                "created_at": timestamp,
            }
            raw, value = _body(body)
            existing = db.execute(
                "select review_digest,body_json from improvement_review where candidate_digest=?",
                (candidate_digest,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Improvement review replay drift")
                return value
            db.execute(
                "insert into improvement_review values(?,?,?,?,?,?,?)",
                (
                    value,
                    candidate_digest,
                    evaluation_digest,
                    reviewer_ref,
                    int(approved),
                    timestamp,
                    raw,
                ),
            )
            db.commit()
        return value

    def activate_auto(
        self,
        candidate_digest: str,
        evaluation_digest: str,
        review_digest: str,
        *,
        receipt: OperationalExecutionReceipt | None = None,
        now: dt.datetime,
    ) -> str:
        for value in (candidate_digest, evaluation_digest, review_digest):
            parse_digest(value)
        if type(receipt) is not OperationalExecutionReceipt:
            raise ValidationFailed("Improvement activation requires verified operational receipt")
        receipt.__post_init__()
        verified = self._require_verified_operational_receipt(receipt)
        if (
            receipt.operation != "activation"
            or receipt.candidate_digest != candidate_digest
            or receipt.evaluation_digest != evaluation_digest
            or receipt.external_effect_count != 0
            or verified["operation"] != "activation"
            or verified["status"] != receipt.status
            or verified["verified_at"] != _instant(now)
        ):
            raise PolicyViolation("Improvement activation receipt binding invalid")
        with closing(self._connect()) as db:
            eligibility_candidate = db.execute(
                "select change_class from improvement_candidate where candidate_digest=?",
                (candidate_digest,),
            ).fetchone()
            eligibility_evaluation = db.execute(
                "select state from improvement_evaluation where evaluation_digest=? and candidate_digest=?",
                (evaluation_digest, candidate_digest),
            ).fetchone()
            eligibility_review = db.execute(
                "select approved,created_at from improvement_review where review_digest=? and candidate_digest=? and evaluation_digest=?",
                (review_digest, candidate_digest, evaluation_digest),
            ).fetchone()
            eligibility_rows = db.execute(
                "select stage,status,created_at from rollout_receipt where candidate_digest=? order by created_at,stage",
                (candidate_digest,),
            ).fetchall()
        if (
            eligibility_candidate is None
            or eligibility_candidate[0] != ImprovementChangeClass.AUTO_SAFE.value
            or eligibility_evaluation is None
            or eligibility_evaluation[0] not in {"improved", "target-reached"}
            or eligibility_review is None
            or eligibility_review[0] != 1
            or [(row["stage"], row["status"]) for row in eligibility_rows]
            != [("shadow", "completed"), ("canary", "completed")]
            or _instant(receipt.started_at)
            <= max(eligibility_review["created_at"], eligibility_rows[-1]["created_at"])
        ):
            raise PolicyViolation("Only reviewed AUTO_SAFE improvement may auto-activate")
        operational_receipt_digest = str(verified["receipt_digest"])
        if receipt.status != "completed":
            raise PolicyViolation("Improvement activation requires recovered completed receipt")
        timestamp = _instant(now)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            candidate = db.execute(
                "select change_class from improvement_candidate where candidate_digest=?",
                (candidate_digest,),
            ).fetchone()
            evaluation = db.execute(
                "select state from improvement_evaluation where evaluation_digest=? and candidate_digest=?",
                (evaluation_digest, candidate_digest),
            ).fetchone()
            review = db.execute(
                "select approved,created_at from improvement_review where review_digest=? and candidate_digest=? and evaluation_digest=?",
                (review_digest, candidate_digest, evaluation_digest),
            ).fetchone()
            rows = db.execute(
                "select stage,status,rollout_digest,created_at from rollout_receipt where candidate_digest=? order by created_at,stage",
                (candidate_digest,),
            ).fetchall()
            if (
                candidate is None
                or candidate[0] != ImprovementChangeClass.AUTO_SAFE.value
                or evaluation is None
                or evaluation[0] not in {"improved", "target-reached"}
                or review is None
                or review[0] != 1
                or [(row["stage"], row["status"]) for row in rows]
                != [("shadow", "completed"), ("canary", "completed")]
                or _instant(receipt.started_at) <= max(review["created_at"], rows[-1]["created_at"])
            ):
                raise PolicyViolation("Only reviewed AUTO_SAFE improvement may auto-activate")
            body = {
                "schema": "zekam-local-improvement-activation/v1",
                "candidate_digest": candidate_digest,
                "evaluation_digest": evaluation_digest,
                "review_digest": review_digest,
                "shadow_digest": rows[0]["rollout_digest"],
                "canary_digest": rows[1]["rollout_digest"],
                "claim_digest": receipt.claim_digest,
                "effect_digest": receipt.effect_digest,
                "receipt_digest": operational_receipt_digest,
                "activated_at": timestamp,
                "registry_only": True,
                "production_mutation_performed": False,
            }
            raw, value = _body(body)
            existing = db.execute(
                "select activation_digest,body_json from improvement_activation where candidate_digest=?",
                (candidate_digest,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Improvement activation replay drift")
                db.rollback()
                return value
            db.execute(
                "insert into improvement_activation values(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    candidate_digest,
                    evaluation_digest,
                    review_digest,
                    rows[0]["rollout_digest"],
                    rows[1]["rollout_digest"],
                    receipt.claim_digest,
                    receipt.effect_digest,
                    operational_receipt_digest,
                    timestamp,
                    raw,
                ),
            )
            db.commit()
        return value

    def rollback(
        self,
        activation_digest: str,
        *,
        receipt: OperationalExecutionReceipt | None = None,
        success: object | None = None,
        now: dt.datetime,
    ) -> str:
        parse_digest(activation_digest)
        if success is not None and type(success) is not bool:
            raise ValidationFailed("Improvement rollback status must be bool")
        timestamp = _instant(now)
        with closing(self._connect()) as db:
            activation = db.execute(
                "select candidate_digest,evaluation_digest,activated_at from improvement_activation where activation_digest=?",
                (activation_digest,),
            ).fetchone()
        if activation is None:
            raise PolicyViolation("Rollback requires prior exact activation")
        if success is not None or type(receipt) is not OperationalExecutionReceipt:
            raise ValidationFailed("Improvement rollback requires verified operational receipt")
        receipt.__post_init__()
        verified = self._require_verified_operational_receipt(receipt)
        if (
            receipt.operation != "rollback"
            or receipt.candidate_digest != activation["candidate_digest"]
            or receipt.evaluation_digest != activation["evaluation_digest"]
            or receipt.external_effect_count != 0
            or _instant(receipt.started_at) <= activation["activated_at"]
            or verified["operation"] != "rollback"
            or verified["status"] != receipt.status
            or verified["verified_at"] != timestamp
        ):
            raise PolicyViolation("Improvement rollback receipt binding invalid")
        operational_receipt_digest = str(verified["receipt_digest"])
        if receipt.status != "completed":
            raise PolicyViolation("Improvement rollback requires recovered completed receipt")
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            row = db.execute(
                "select activated_at from improvement_activation where activation_digest=?",
                (activation_digest,),
            ).fetchone()
            if row is None or timestamp <= row[0]:
                raise PolicyViolation("Rollback requires prior exact activation")
            body = {
                "schema": "zekam-local-improvement-rollback/v1",
                "activation_digest": activation_digest,
                "status": "completed",
                "created_at": timestamp,
                "operational_receipt_digest": operational_receipt_digest,
                "production_mutation_performed": False,
            }
            raw, value = _body(body)
            existing = db.execute(
                "select rollback_digest,body_json from rollback_receipt where activation_digest=?",
                (activation_digest,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Improvement rollback replay drift")
                return value
            db.execute(
                "insert into rollback_receipt values(?,?,?,?,?)",
                (value, activation_digest, body["status"], timestamp, raw),
            )
            db.commit()
        return value

    def record_learning_feedback(
        self,
        candidate_digest: str,
        evaluation_digest: str,
        outcome_receipt_digest: str,
        *,
        now: dt.datetime,
    ) -> str:
        for value in (candidate_digest, evaluation_digest, outcome_receipt_digest):
            parse_digest(value)
        timestamp = _instant(now)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            candidate = db.execute(
                "select failure_card_digest,baseline_aggregate_digest from improvement_candidate where candidate_digest=?",
                (candidate_digest,),
            ).fetchone()
            evaluation = db.execute(
                "select after_aggregate_digest from improvement_evaluation where evaluation_digest=? and candidate_digest=?",
                (evaluation_digest, candidate_digest),
            ).fetchone()
            outcome = db.execute(
                "select activated_at from improvement_activation where receipt_digest=? and candidate_digest=? union all select r.created_at from rollback_receipt r join improvement_activation a on a.activation_digest=r.activation_digest where r.rollback_digest=? and a.candidate_digest=?",
                (
                    outcome_receipt_digest,
                    candidate_digest,
                    outcome_receipt_digest,
                    candidate_digest,
                ),
            ).fetchall()
            if (
                candidate is None
                or evaluation is None
                or len(outcome) != 1
                or timestamp <= outcome[0][0]
            ):
                raise PolicyViolation("Learning feedback requires exact durable outcome receipt")
            body = {
                "schema": "zekam-local-improvement-learning-feedback/v1",
                "candidate_digest": candidate_digest,
                "evaluation_digest": evaluation_digest,
                "failure_card_digest": candidate["failure_card_digest"],
                "baseline_aggregate_digest": candidate["baseline_aggregate_digest"],
                "after_aggregate_digest": evaluation["after_aggregate_digest"],
                "outcome_receipt_digest": outcome_receipt_digest,
                "created_at": timestamp,
                "grants_authority": False,
            }
            raw, value = _body(body)
            existing = db.execute(
                "select feedback_digest,body_json from learning_feedback where candidate_digest=?",
                (candidate_digest,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Improvement feedback replay drift")
                return value
            db.execute(
                "insert into learning_feedback values(?,?,?,?,?,?)",
                (
                    value,
                    candidate_digest,
                    evaluation_digest,
                    outcome_receipt_digest,
                    timestamp,
                    raw,
                ),
            )
            db.commit()
        return value

    def audit(self) -> dict[str, int]:
        tables = (
            ("improvement_candidate", "candidate_digest"),
            ("validator_manifest", "manifest_digest"),
            ("attempt_claim", "claim_digest"),
            ("improvement_evaluation", "evaluation_digest"),
            ("operational_execution_claim", "claim_digest"),
            ("operational_runner_receipt", "runner_receipt_digest"),
            ("operational_execution_receipt", "receipt_digest"),
            ("rollout_receipt", "rollout_digest"),
            ("improvement_review", "review_digest"),
            ("improvement_activation", "activation_digest"),
            ("rollback_receipt", "rollback_digest"),
            ("learning_feedback", "feedback_digest"),
        )
        counts: dict[str, int] = {}
        with closing(self._connect()) as db:
            db.execute("pragma query_only=on")
            db.execute("begin")
            if (
                db.execute("pragma integrity_check").fetchone()[0] != "ok"
                or db.execute("pragma foreign_key_check").fetchone() is not None
            ):
                raise PolicyViolation("Local improvement SQLite integrity drift")
            for table, identity in tables:
                rows = db.execute(f"select {identity},body_json from {table} limit 4097").fetchall()
                if len(rows) > 4096:
                    raise PolicyViolation("Local improvement row bound exceeded")
                for row in rows:
                    body = _document(row["body_json"])
                    if digest(body) != row[identity]:
                        raise PolicyViolation("Local improvement canonical digest drift")
                counts[table] = len(rows)
            db.rollback()
        return counts
