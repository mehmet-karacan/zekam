"""Dormant evidence-bound Mac-local model routing ledger."""

# ruff: noqa: E501 -- compact literal DDL is intentionally reviewable.

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, final

from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.local_model_benchmark import (
    SCHEMA_DIGEST as BENCHMARK_SCHEMA_DIGEST,
)
from zekam.infrastructure.sqlite.local_model_registry import (
    SCHEMA_DIGEST as REGISTRY_SCHEMA_DIGEST,
)
from zekam.infrastructure.sqlite.operational_schema import SCHEMA_VERSION, status

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_WORKLOAD_MODALITY = {
    "code": "text",
    "analysis": "text",
    "chat": "text",
    "embedding": "embedding",
}
_CAPABILITIES = frozenset({"text", "code", "tools", "structured-output", "embedding"})
_CLASSIFICATIONS = frozenset({"public", "internal", "restricted"})

_SCHEMA = r"""
pragma foreign_keys=on;
create table routing_schema(singleton integer primary key check(singleton=1),version integer not null);
insert into routing_schema values(1,1);
create table candidate(candidate_digest text primary key,exact_id text not null,snapshot_digest text not null,revision_fingerprint text not null,health_digest text not null,aggregate_digest text not null,observed_at text not null,expires_at text not null,body_json text not null) strict;
create table policy_stage(stage_digest text primary key,revision integer not null,ordinal integer not null,stage text not null,evidence_digest text not null,actor_id text not null,body_json text not null,check(stage in('offline-replay','shadow','canary','independent-review','approval')),unique(revision,ordinal),unique(revision,stage)) strict;
create table policy_revision(policy_digest text primary key,revision integer unique not null,activation_job_id text unique not null,body_json text not null) strict;
create table route_decision(decision_digest text primary key,request_digest text unique not null,policy_digest text not null references policy_revision,primary_id text,fallback_id text,body_json text not null) strict;
create table route_effect(effect_digest text primary key,decision_digest text not null references route_decision,operation_key text unique not null,primary_id text not null,fallback_id text,body_json text not null) strict;
create table route_outcome(outcome_digest text primary key,effect_digest text not null references route_effect,exact_id text not null,status text not null,evidence_digest text not null,job_id text unique not null,claim_id text unique not null,receipt_id text not null,recovery_resolution_id text,observed_at text not null,body_json text not null,check(status in('succeeded','failed')),unique(effect_digest,exact_id)) strict;
"""
for _table in (
    "candidate",
    "policy_stage",
    "policy_revision",
    "route_decision",
    "route_effect",
    "route_outcome",
):
    _SCHEMA += f"create trigger {_table}_no_update before update on {_table} begin select raise(abort,'append-only'); end;\n"
    _SCHEMA += f"create trigger {_table}_no_delete before delete on {_table} begin select raise(abort,'append-only'); end;\n"


def _schema_digest(db: sqlite3.Connection) -> str:
    rows = db.execute(
        "select type,name,sql from sqlite_master where type in ('table','trigger') "
        "and name not like 'sqlite_%' order by type,name"
    ).fetchall()
    return digest([{"type": str(row[0]), "name": str(row[1]), "sql": str(row[2])} for row in rows])


def _text(value: object, label: str) -> str:
    if type(value) is not str or not _SAFE.fullmatch(value):
        raise ValidationFailed(f"Local routing {label} invalid")
    return value


def _instant(value: dt.datetime) -> str:
    if type(value) is not dt.datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed("Local routing timestamp must be timezone-aware")
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()


def _parse_instant(value: object) -> dt.datetime:
    if type(value) is not str:
        raise PolicyViolation("Stored local routing timestamp type drift")
    try:
        result = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise PolicyViolation("Stored local routing timestamp drift") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise PolicyViolation("Stored local routing timestamp lacks timezone")
    return result.astimezone(dt.UTC)


def _document(raw: object) -> dict[str, Any]:
    if type(raw) is not str or not 0 < len(raw.encode()) <= 1_048_576:
        raise PolicyViolation("Stored local routing JSON size/type drift")

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
        raise PolicyViolation("Stored local routing JSON invalid") from exc
    if type(value) is not dict or canonical_json(value) != raw:
        raise PolicyViolation("Stored local routing JSON not canonical")
    return value


def route_execution_effect_digest(route_effect_digest: str, exact_id: str) -> str:
    parse_digest(route_effect_digest)
    _text(exact_id, "execution model")
    return digest(
        {
            "schema": "zekam-local-route-execution-effect/v1",
            "route_effect_digest": route_effect_digest,
            "exact_id": exact_id,
        }
    )


def route_policy_stage_effect_digest(
    revision: int,
    candidate_digests: tuple[str, ...],
    stage: str,
    evidence_digest: str,
    actor_id: str,
) -> str:
    return digest(
        {
            "schema": "zekam-local-route-policy-stage-effect/v1",
            "revision": revision,
            "candidate_digests": list(candidate_digests),
            "stage": stage,
            "evidence_digest": evidence_digest,
            "actor_id": actor_id,
        }
    )


def _source(path: Path, expected_digest: str) -> sqlite3.Connection:
    if not path.is_absolute() or path.is_symlink():
        raise ValidationFailed("Local routing source path invalid")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise PolicyViolation("Local routing source identity invalid")
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("pragma query_only=on")
    db.execute("pragma foreign_keys=on")
    if _schema_digest(db) != expected_digest:
        db.close()
        raise PolicyViolation("Local routing source schema drift")
    return db


@dataclass(frozen=True, slots=True)
class LocalRouteBinding:
    exact_id: str
    snapshot_digest: str
    revision_fingerprint: str
    health_digest: str
    aggregate_digest: str
    family_id: str
    execution_identity: str
    workloads: tuple[str, ...]
    modalities: tuple[str, ...]
    data_classifications: tuple[str, ...]
    capabilities: tuple[str, ...]
    observed_at: dt.datetime
    expires_at: dt.datetime

    def __post_init__(self) -> None:
        _text(self.exact_id, "exact model id")
        for value in (
            self.snapshot_digest,
            self.revision_fingerprint,
            self.health_digest,
            self.aggregate_digest,
        ):
            parse_digest(value)
        _text(self.family_id, "model family")
        _text(self.execution_identity, "execution identity")
        if any(
            type(items) is not tuple
            for items in (
                self.workloads,
                self.modalities,
                self.data_classifications,
                self.capabilities,
            )
        ):
            raise ValidationFailed("Local routing scopes must be tuples")
        if (
            tuple(sorted(set(self.workloads))) != self.workloads
            or tuple(sorted(set(self.modalities))) != self.modalities
            or tuple(sorted(set(self.data_classifications))) != self.data_classifications
            or tuple(sorted(set(self.capabilities))) != self.capabilities
            or not self.workloads
            or not self.modalities
            or not self.data_classifications
            or not self.capabilities
            or any(item not in _WORKLOAD_MODALITY for item in self.workloads)
            or any(item not in frozenset(_WORKLOAD_MODALITY.values()) for item in self.modalities)
            or any(item not in _CLASSIFICATIONS for item in self.data_classifications)
            or any(item not in _CAPABILITIES for item in self.capabilities)
        ):
            raise ValidationFailed("Local routing scopes invalid")
        if any(_WORKLOAD_MODALITY[item] not in self.modalities for item in self.workloads):
            raise PolicyViolation("Local routing workload/modality mismatch")
        if not self.observed_at < self.expires_at <= self.observed_at + dt.timedelta(hours=24):
            raise ValidationFailed("Local routing candidate TTL invalid")
        _instant(self.observed_at)
        _instant(self.expires_at)

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-local-route-binding/v1",
            "exact_id": self.exact_id,
            "snapshot_digest": self.snapshot_digest,
            "revision_fingerprint": self.revision_fingerprint,
            "health_digest": self.health_digest,
            "aggregate_digest": self.aggregate_digest,
            "family_id": self.family_id,
            "execution_identity": self.execution_identity,
            "workloads": list(self.workloads),
            "modalities": list(self.modalities),
            "data_classifications": list(self.data_classifications),
            "capabilities": list(self.capabilities),
            "observed_at": _instant(self.observed_at),
            "expires_at": _instant(self.expires_at),
        }

    @property
    def candidate_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class LocalRouteRequest:
    workload: str
    modality: str
    data_classification: str
    device_id: str
    client_id: str
    allowed_provider_ids: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    policy_digest: str
    inventory_snapshot_digest: str
    evidence_epoch_digest: str
    project_id: str
    source_snapshot_id: str
    project_context_digest: str
    max_latency_ms: float
    max_cost: float
    local_only: bool = True
    independent_from_model_id: str | None = None

    def __post_init__(self) -> None:
        if (
            self.workload not in _WORKLOAD_MODALITY
            or self.modality != _WORKLOAD_MODALITY[self.workload]
        ):
            raise ValidationFailed("Local route workload/modality invalid")
        if self.data_classification not in _CLASSIFICATIONS:
            raise ValidationFailed("Local route data classification invalid")
        for value, label in (
            (self.device_id, "device"),
            (self.client_id, "client"),
            (self.project_id, "project"),
            (self.source_snapshot_id, "source snapshot"),
        ):
            _text(value, label)
        if (
            type(self.allowed_provider_ids) is not tuple
            or tuple(sorted(set(self.allowed_provider_ids))) != self.allowed_provider_ids
            or not self.allowed_provider_ids
        ):
            raise ValidationFailed("Local route allowed providers invalid")
        for value in self.allowed_provider_ids:
            _text(value, "allowed provider")
        if (
            self.client_id not in {"opencode", "codex"}
            or type(self.local_only) is not bool
            or not self.local_only
        ):
            raise PolicyViolation("This routing slice is Mac local-only")
        if tuple(sorted(set(self.required_capabilities))) != self.required_capabilities or any(
            item not in _CAPABILITIES for item in self.required_capabilities
        ):
            raise ValidationFailed("Local route capability requirements invalid")
        for value, label in (
            (self.policy_digest, "policy digest"),
            (self.inventory_snapshot_digest, "inventory snapshot digest"),
            (self.evidence_epoch_digest, "evidence epoch digest"),
            (self.project_context_digest, "project context digest"),
        ):
            if type(value) is not str:
                raise ValidationFailed(f"Local route {label} invalid")
            parse_digest(value)
        if (
            type(self.max_latency_ms) is not float
            or type(self.max_cost) is not float
            or not math.isfinite(self.max_latency_ms)
            or not math.isfinite(self.max_cost)
            or min(self.max_latency_ms, self.max_cost) < 0
        ):
            raise ValidationFailed("Local route budgets require finite floats")
        if self.independent_from_model_id is not None:
            _text(self.independent_from_model_id, "independence model")

    @property
    def request_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-local-route-request/v1",
                "workload": self.workload,
                "modality": self.modality,
                "data_classification": self.data_classification,
                "device_id": self.device_id,
                "client_id": self.client_id,
                "allowed_provider_ids": list(self.allowed_provider_ids),
                "required_capabilities": list(self.required_capabilities),
                "policy_digest": self.policy_digest,
                "inventory_snapshot_digest": self.inventory_snapshot_digest,
                "evidence_epoch_digest": self.evidence_epoch_digest,
                "project_id": self.project_id,
                "source_snapshot_id": self.source_snapshot_id,
                "project_context_digest": self.project_context_digest,
                "max_latency_ms": self.max_latency_ms,
                "max_cost": self.max_cost,
                "local_only": self.local_only,
                "independent_from_model_id": self.independent_from_model_id,
            }
        )


@dataclass(frozen=True, slots=True)
class RouteEffectClaim:
    effect_digest: str
    disposition: Literal["fresh", "replay", "terminal"]

    def __post_init__(self) -> None:
        parse_digest(self.effect_digest)
        if self.disposition not in {"fresh", "replay", "terminal"}:
            raise ValidationFailed("Local route effect claim disposition invalid")


@final
class SQLiteLocalEvidenceRouter:
    def __init__(
        self, path: Path, registry_path: Path, benchmark_path: Path, operational_path: Path
    ) -> None:
        if any(
            not item.is_absolute() or item.is_symlink()
            for item in (path, registry_path, benchmark_path, operational_path)
        ):
            raise ValidationFailed("Local routing paths must be absolute non-symlinks")
        self.path = path
        self.registry_path = registry_path
        self.benchmark_path = benchmark_path
        self.operational_path = operational_path

    def bootstrap(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = self.path.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PolicyViolation("Local routing private directory required")
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(_SCHEMA)
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        info = self.path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise PolicyViolation("Local routing database identity invalid")
        connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys=on")
        connection.execute("pragma busy_timeout=5000")
        if (
            connection.execute("select version from routing_schema").fetchone()[0] != 1
            or _schema_digest(connection) != LOCAL_ROUTING_SCHEMA_DIGEST
        ):
            connection.close()
            raise PolicyViolation("Local routing schema drift")
        return connection

    def _operational(self) -> sqlite3.Connection:
        current = status(self.operational_path)
        if not (
            current.exists
            and current.schema_version == SCHEMA_VERSION
            and current.integrity_ok
            and current.schema_ok
        ):
            raise PolicyViolation("Local routing operational evidence invalid")
        info = self.operational_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise PolicyViolation("Local routing operational identity invalid")
        db = sqlite3.connect(
            f"{self.operational_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5
        )
        db.row_factory = sqlite3.Row
        db.execute("pragma query_only=on")
        db.execute("pragma foreign_keys=on")
        return db

    def _project_context(self, project_id: str, source_snapshot_id: str) -> str:
        with closing(self._operational()) as db:
            active = db.execute(
                "select id from source_binding where project_id=? and active=1 order by id",
                (project_id,),
            ).fetchall()
            if len(active) != 1:
                raise PolicyViolation("Local route project source authority ambiguous")
            row = db.execute(
                "select p.id project_id,p.status,p.revision,sb.id source_binding_id,"
                "sb.portable_ref,sb.source_kind,ss.id source_snapshot_id,ss.revision_ref,"
                "ss.tree_digest,ss.content_digest,ss.config_digest,ss.captured_at "
                "from project p join source_binding sb on sb.project_id=p.id and sb.active=1 "
                "join source_snapshot ss on ss.source_binding_id=sb.id "
                "where p.id=? order by ss.captured_at desc,ss.id desc limit 1",
                (project_id,),
            ).fetchall()
        if len(row) != 1 or row[0]["source_snapshot_id"] != source_snapshot_id:
            raise PolicyViolation("Local route current project context drift")
        value = row[0]
        if value["status"] != "active":
            raise PolicyViolation("Local route project is not active")
        body = {
            "schema": "zekam-local-route-project-context/v1",
            "project_id": value["project_id"],
            "project_revision": value["revision"],
            "project_status": value["status"],
            "source_binding_id": value["source_binding_id"],
            "portable_ref": value["portable_ref"],
            "source_kind": value["source_kind"],
            "source_snapshot_id": value["source_snapshot_id"],
            "revision_ref": value["revision_ref"],
            "tree_digest": value["tree_digest"],
            "content_digest": value["content_digest"],
            "config_digest": value["config_digest"],
            "captured_at": value["captured_at"],
        }
        return digest(body)

    def current_project_context_digest(self, project_id: str, source_snapshot_id: str) -> str:
        _text(project_id, "project")
        _text(source_snapshot_id, "source snapshot")
        return self._project_context(project_id, source_snapshot_id)

    def current_evidence_epoch_digest(self, request: LocalRouteRequest) -> str:
        """Derive the route epoch from immutable, decision-relevant evidence."""
        if type(request) is not LocalRouteRequest:
            raise ValidationFailed("Exact local route request required")
        request.__post_init__()
        project_context_digest = self._project_context(
            request.project_id, request.source_snapshot_id
        )
        if project_context_digest != request.project_context_digest:
            raise PolicyViolation("Local route project context is stale")
        with closing(self._connect()) as db:
            policy = db.execute(
                "select policy_digest,body_json from policy_revision order by revision desc limit 1"
            ).fetchone()
            if policy is None or policy["policy_digest"] != request.policy_digest:
                raise PolicyViolation("Local route active policy missing or drifted")
            policy_body = _document(policy["body_json"])
            if digest(policy_body) != policy["policy_digest"]:
                raise PolicyViolation("Local route policy body drift")
            candidate_digests = policy_body.get("candidate_digests")
            if (
                type(candidate_digests) is not list
                or not candidate_digests
                or any(type(item) is not str for item in candidate_digests)
            ):
                raise PolicyViolation("Local route policy candidate identity drift")
            candidates = db.execute(
                f"select candidate_digest,exact_id,body_json from candidate "
                f"where candidate_digest in ({','.join('?' for _ in candidate_digests)}) "
                "order by candidate_digest",
                tuple(candidate_digests),
            ).fetchall()
            outcomes = db.execute(
                "select outcome_digest,exact_id,status,evidence_digest,body_json "
                "from route_outcome order by exact_id,outcome_digest"
            ).fetchall()
        if len(candidates) != len(candidate_digests):
            raise PolicyViolation("Local route policy candidate evidence incomplete")
        candidate_state: list[dict[str, str]] = []
        exact_ids: list[str] = []
        for row in candidates:
            body = _document(row["body_json"])
            if digest(body) != row["candidate_digest"] or body.get("exact_id") != row["exact_id"]:
                raise PolicyViolation("Local route candidate body drift")
            exact_id = str(row["exact_id"])
            exact_ids.append(exact_id)
            candidate_state.append(
                {"candidate_digest": str(row["candidate_digest"]), "exact_id": exact_id}
            )
        if len(set(exact_ids)) != len(exact_ids):
            raise PolicyViolation("Local route duplicate exact model identity")
        outcome_state: list[dict[str, str]] = []
        for row in outcomes:
            body = _document(row["body_json"])
            if (
                digest(body) != row["outcome_digest"]
                or body.get("exact_id") != row["exact_id"]
                or body.get("status") != row["status"]
                or body.get("evidence_digest") != row["evidence_digest"]
            ):
                raise PolicyViolation("Local route outcome body drift")
            outcome_state.append(
                {
                    "outcome_digest": str(row["outcome_digest"]),
                    "exact_id": str(row["exact_id"]),
                }
            )
        with closing(_source(self.registry_path, REGISTRY_SCHEMA_DIGEST)) as registry:
            snapshot = registry.execute(
                "select snapshot_digest,body_json from discovery_snapshot "
                "where device_id=? and client_id=? "
                "order by observed_at desc,snapshot_digest desc limit 1",
                (request.device_id, request.client_id),
            ).fetchone()
            snapshot_digest: str | None = None
            if snapshot is not None:
                snapshot_body = _document(snapshot["body_json"])
                if digest(snapshot_body) != snapshot["snapshot_digest"]:
                    raise PolicyViolation("Local route discovery snapshot body drift")
                snapshot_digest = str(snapshot["snapshot_digest"])
            registry_state: list[dict[str, object]] = []
            for exact_id, candidate in zip(exact_ids, candidates, strict=True):
                body = _document(candidate["body_json"])
                snapshot_id = body.get("snapshot_digest")
                revision = body.get("revision_fingerprint")
                if type(snapshot_id) is not str or type(revision) is not str:
                    raise PolicyViolation("Local route candidate source binding drift")
                event = registry.execute(
                    "select event_digest,body_json from reconcile_event "
                    "where snapshot_digest=? and exact_id=?",
                    (snapshot_id, exact_id),
                ).fetchone()
                event_digest: str | None = None
                if event is not None:
                    event_body = _document(event["body_json"])
                    if digest(event_body) != event["event_digest"]:
                        raise PolicyViolation("Local route reconcile event body drift")
                    event_digest = str(event["event_digest"])
                health_rows = registry.execute(
                    "select health_digest,status,observed_at,body_json "
                    "from health_observation where snapshot_digest=? and exact_id=? "
                    "and revision_fingerprint=? "
                    "order by observed_at desc,health_digest desc limit 3",
                    (snapshot_id, exact_id, revision),
                ).fetchall()
                health_state: list[dict[str, str]] = []
                for health in health_rows:
                    health_body = _document(health["body_json"])
                    if digest(health_body) != health["health_digest"]:
                        raise PolicyViolation("Local route health body digest drift")
                    health_state.append(
                        {
                            "health_digest": str(health["health_digest"]),
                            "status": str(health["status"]),
                            "observed_at": str(health["observed_at"]),
                        }
                    )
                profiles = registry.execute(
                    "select report_digest,body_json from local_profile_report "
                    "where snapshot_digest=? and json_extract(body_json,'$.schema')=? "
                    "and json_extract(body_json,'$.exact_id')=? order by report_digest",
                    (snapshot_id, "zekam-local-model-capability-profile/v1", exact_id),
                ).fetchall()
                profile_digests: list[str] = []
                for profile in profiles:
                    profile_body = _document(profile["body_json"])
                    if digest(profile_body) != profile["report_digest"]:
                        raise PolicyViolation("Local route capability profile body drift")
                    profile_digests.append(str(profile["report_digest"]))
                registry_state.append(
                    {
                        "exact_id": exact_id,
                        "event_digest": event_digest,
                        "health": health_state,
                        "profile_digests": profile_digests,
                    }
                )
        return digest(
            {
                "schema": "zekam-local-route-evidence-epoch/v1",
                "policy_digest": request.policy_digest,
                "project_context_digest": project_context_digest,
                "current_discovery_snapshot_digest": snapshot_digest,
                "candidates": candidate_state,
                "registry": registry_state,
                "outcomes": outcome_state,
            }
        )

    def _terminal_execution(
        self,
        job_id: str,
        *,
        operation: str,
        effect_digest: str,
        expected_payload: dict[str, object],
    ) -> dict[str, str]:
        _text(job_id, "operational job")
        parse_digest(effect_digest)
        with closing(self._operational()) as db:
            job = db.execute(
                "select state,payload_json,terminal_evidence_digest,created_at,updated_at "
                "from local_job where id=?",
                (job_id,),
            ).fetchone()
            rows = db.execute(
                "select c.id claim_id,c.operation,c.effect_digest,c.claimed_at,"
                "r.id receipt_id,r.status receipt_status,r.evidence_digest,r.created_at receipt_at "
                "from local_effect_claim c join local_effect_receipt r on r.claim_id=c.id "
                "where c.job_id=? order by c.id",
                (job_id,),
            ).fetchall()
            recovery = db.execute(
                "select c.id case_id,c.state case_state,x.id resolution_id,x.outcome,"
                "x.evidence_digest,x.created_at resolution_at "
                "from local_recovery_case c join local_recovery_resolution x "
                "on x.recovery_case_id=c.id where c.job_id=?",
                (job_id,),
            ).fetchall()
        if job is None or len(rows) != 1:
            raise PolicyViolation("Local route requires exact operational claim and receipt")
        payload = _document(job["payload_json"])
        if payload != expected_payload:
            raise PolicyViolation("Local route operational authorization payload drift")
        claim = rows[0]
        if claim["operation"] != operation or claim["effect_digest"] != effect_digest:
            raise PolicyViolation("Local route operational effect binding drift")
        status_value = str(claim["receipt_status"])
        evidence = str(claim["evidence_digest"])
        observed_at = str(claim["receipt_at"])
        resolution_id: str | None = None
        if status_value == "unknown":
            if len(recovery) != 1 or recovery[0]["case_state"] != "resolved":
                raise PolicyViolation("Local route unknown execution is unresolved")
            resolution = recovery[0]
            if resolution["outcome"] not in {"completed", "failed"}:
                raise PolicyViolation("Local route recovery outcome invalid")
            status_value = str(resolution["outcome"])
            evidence = str(resolution["evidence_digest"])
            observed_at = str(resolution["resolution_at"])
            resolution_id = str(resolution["resolution_id"])
        elif recovery:
            raise PolicyViolation("Local route direct execution has unexpected recovery evidence")
        if status_value not in {"completed", "failed"} or job["state"] != status_value:
            raise PolicyViolation("Local route operational terminal state drift")
        if job["terminal_evidence_digest"] != evidence:
            raise PolicyViolation("Local route terminal evidence drift")
        if not (
            _parse_instant(job["created_at"])
            <= _parse_instant(claim["claimed_at"])
            <= _parse_instant(claim["receipt_at"])
            <= _parse_instant(observed_at)
            <= _parse_instant(job["updated_at"])
        ):
            raise PolicyViolation("Local route operational chronology invalid")
        return {
            "job_id": job_id,
            "claim_id": str(claim["claim_id"]),
            "receipt_id": str(claim["receipt_id"]),
            "recovery_resolution_id": resolution_id or "",
            "status": "succeeded" if status_value == "completed" else "failed",
            "evidence_digest": evidence,
            "observed_at": observed_at,
        }

    def _evidence(
        self, binding: LocalRouteBinding, *, device_id: str, client_id: str, now: dt.datetime
    ) -> dict[str, Any]:
        with closing(_source(self.registry_path, REGISTRY_SCHEMA_DIGEST)) as registry:
            snapshot = registry.execute(
                "select snapshot_digest,listing_supported,observed_at,expires_at,body_json "
                "from discovery_snapshot "
                "where device_id=? and client_id=? order by observed_at desc limit 1",
                (device_id, client_id),
            ).fetchone()
            event = registry.execute(
                "select event_digest,disposition,new_fingerprint,body_json from reconcile_event where snapshot_digest=? and exact_id=?",
                (binding.snapshot_digest, binding.exact_id),
            ).fetchone()
            health = registry.execute(
                "select health_digest,status,revision_fingerprint,observed_at,body_json from health_observation where health_digest=?",
                (binding.health_digest,),
            ).fetchone()
            latest_health = registry.execute(
                "select health_digest,status,observed_at from health_observation "
                "where snapshot_digest=? and exact_id=? and revision_fingerprint=? "
                "order by observed_at desc,health_digest desc limit 3",
                (binding.snapshot_digest, binding.exact_id, binding.revision_fingerprint),
            ).fetchall()
            profiles = registry.execute(
                "select report_digest,body_json from local_profile_report "
                "where snapshot_digest=? and json_extract(body_json,'$.schema')=? "
                "and json_extract(body_json,'$.exact_id')=?",
                (
                    binding.snapshot_digest,
                    "zekam-local-model-capability-profile/v1",
                    binding.exact_id,
                ),
            ).fetchall()
        if (
            snapshot is None
            or snapshot["snapshot_digest"] != binding.snapshot_digest
            or not snapshot["listing_supported"]
            or digest(_document(snapshot["body_json"])) != snapshot["snapshot_digest"]
            or not (
                _parse_instant(snapshot["observed_at"])
                <= now
                < _parse_instant(snapshot["expires_at"])
            )
            or event is None
            or event["disposition"] in {"removed", "ambiguous"}
            or event["new_fingerprint"] != binding.revision_fingerprint
            or digest(_document(event["body_json"])) != event["event_digest"]
            or health is None
            or health["status"] != "passed"
            or health["revision_fingerprint"] != binding.revision_fingerprint
            or now - _parse_instant(health["observed_at"]) > dt.timedelta(hours=24)
            or not latest_health
            or latest_health[0]["health_digest"] != binding.health_digest
            or latest_health[0]["status"] != "passed"
            or (
                len(latest_health) == 3
                and latest_health[1]["status"] == latest_health[2]["status"] == "failed"
                and now - _parse_instant(latest_health[1]["observed_at"]) < dt.timedelta(minutes=5)
            )
            or len(profiles) != 1
        ):
            raise PolicyViolation("Local routing current availability/health evidence invalid")
        health_body = _document(health["body_json"])
        if digest(health_body) != binding.health_digest:
            raise PolicyViolation("Local routing health body digest drift")
        profile = _document(profiles[0]["body_json"])
        expected_profile = {
            "snapshot_digest": binding.snapshot_digest,
            "exact_id": binding.exact_id,
            "revision_fingerprint": binding.revision_fingerprint,
            "family_id": binding.family_id,
            "execution_identity": binding.execution_identity,
            "workloads": list(binding.workloads),
            "modalities": list(binding.modalities),
            "data_classifications": list(binding.data_classifications),
            "capabilities": list(binding.capabilities),
        }
        if (
            digest(profile) != profiles[0]["report_digest"]
            or any(profile.get(key) != value for key, value in expected_profile.items())
            or profile.get("grants_routing_authority") is not False
        ):
            raise PolicyViolation("Local routing stored capability profile drift")
        with closing(_source(self.benchmark_path, BENCHMARK_SCHEMA_DIGEST)) as benchmark:
            row = benchmark.execute(
                "select a.body_json,p.model_id,p.body_json,p.plan_id from benchmark_aggregate a "
                "join benchmark_plan p on p.plan_id=a.plan_id where a.aggregate_digest=?",
                (binding.aggregate_digest,),
            ).fetchone()
            trials = (
                ()
                if row is None
                else benchmark.execute(
                    "select body_json from benchmark_trial where plan_id=? order by fixture_digest,repetition",
                    (row[3],),
                ).fetchall()
            )
        if row is None:
            raise PolicyViolation("Local routing benchmark aggregate missing")
        aggregate = _document(row[0])
        plan = _document(row[2])
        confidence = aggregate.get("confidence_95")
        if (
            digest(aggregate) != binding.aggregate_digest
            or row[1] != binding.exact_id
            or plan.get("model_id") != binding.exact_id
            or not aggregate.get("approved")
            or aggregate.get("unsafe") is not False
            or type(aggregate.get("trial_count")) is not int
            or aggregate["trial_count"] < 5
            or type(confidence) is not list
            or len(confidence) != 2
            or any(type(item) is not float or not 0 <= item <= 1 for item in confidence)
        ):
            raise PolicyViolation("Local routing benchmark evidence invalid")
        trial_bodies = tuple(_document(item[0]) for item in trials)
        if len(trial_bodies) != aggregate["trial_count"]:
            raise PolicyViolation("Local routing benchmark trial census drift")
        aggregate["failure_rate"] = sum(item["status"] != "passed" for item in trial_bodies) / len(
            trial_bodies
        )
        aggregate["correction_rate"] = sum(
            int(item["human_corrections"]) for item in trial_bodies
        ) / len(trial_bodies)
        return aggregate

    def register_candidate(
        self, binding: LocalRouteBinding, *, device_id: str, client_id: str
    ) -> str:
        if type(binding) is not LocalRouteBinding:
            raise ValidationFailed("Exact local route binding required")
        binding.__post_init__()
        self._evidence(binding, device_id=device_id, client_id=client_id, now=binding.observed_at)
        raw = canonical_json(binding.body())
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            old = db.execute(
                "select body_json from candidate where candidate_digest=?",
                (binding.candidate_digest,),
            ).fetchone()
            if old is not None:
                if old[0] != raw:
                    raise ConcurrencyConflict("Local route candidate replay drift")
                db.rollback()
                return binding.candidate_digest
            db.execute(
                "insert into candidate values(?,?,?,?,?,?,?,?,?)",
                (
                    binding.candidate_digest,
                    binding.exact_id,
                    binding.snapshot_digest,
                    binding.revision_fingerprint,
                    binding.health_digest,
                    binding.aggregate_digest,
                    _instant(binding.observed_at),
                    _instant(binding.expires_at),
                    raw,
                ),
            )
            db.commit()
        return binding.candidate_digest

    def policy_activation_spec(
        self,
        revision: int,
        candidate_digests: tuple[str, ...],
        reviewer_model_id: str,
    ) -> tuple[str, str]:
        if type(revision) is not int or revision < 1:
            raise ValidationFailed("Local route policy revision invalid")
        _text(reviewer_model_id, "reviewer model")
        with closing(self._connect()) as db:
            candidates = db.execute(
                f"select candidate_digest,exact_id,body_json from candidate "
                f"where candidate_digest in ({','.join('?' for _ in candidate_digests)}) "
                "order by candidate_digest",
                candidate_digests,
            ).fetchall()
            stages = db.execute(
                "select stage_digest,stage,evidence_digest,actor_id from policy_stage "
                "where revision=? order by ordinal",
                (revision,),
            ).fetchall()
        if len(candidates) != len(candidate_digests):
            raise PolicyViolation("Local route policy candidate evidence incomplete")
        if len({str(row["exact_id"]) for row in candidates}) != len(candidates):
            raise PolicyViolation("Local route duplicate exact model identity")
        if len(stages) != 5:
            raise PolicyViolation("Local route policy candidate evidence incomplete")
        artifact = {
            "schema": "zekam-local-route-policy-candidate/v1",
            "revision": revision,
            "candidate_digests": list(candidate_digests),
            "candidate_bodies_digest": digest([_document(row["body_json"]) for row in candidates]),
            "stages": [
                {
                    "stage_digest": row["stage_digest"],
                    "stage": row["stage"],
                    "evidence_digest": row["evidence_digest"],
                    "actor_id": row["actor_id"],
                }
                for row in stages
            ],
            "reviewer_model_id": reviewer_model_id,
        }
        artifact_digest = digest(artifact)
        return artifact_digest, digest(
            {
                "schema": "zekam-local-route-policy-activation-effect/v1",
                "policy_candidate_digest": artifact_digest,
            }
        )

    def activate_policy(
        self,
        revision: int,
        candidate_digests: tuple[str, ...],
        *,
        offline_replay_digest: str,
        shadow_digest: str,
        canary_digest: str,
        review_digest: str,
        approval_digest: str,
        reviewer_model_id: str,
        activation_job_id: str,
    ) -> str:
        if (
            type(revision) is not int
            or revision < 1
            or tuple(sorted(set(candidate_digests))) != candidate_digests
            or not candidate_digests
        ):
            raise ValidationFailed("Local route policy revision/candidates invalid")
        for value in (
            *candidate_digests,
            offline_replay_digest,
            shadow_digest,
            canary_digest,
            review_digest,
            approval_digest,
        ):
            parse_digest(value)
        _text(reviewer_model_id, "reviewer model")
        _text(activation_job_id, "activation job")
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            latest = db.execute("select max(revision) from policy_revision").fetchone()[0]
            existing = db.execute(
                "select policy_digest,activation_job_id,body_json from policy_revision where revision=?",
                (revision,),
            ).fetchone()
            if existing is None and revision != (1 if latest is None else int(latest) + 1):
                raise PolicyViolation("Local route policy revision must be contiguous")
            candidates = db.execute(
                f"select candidate_digest,exact_id,body_json from candidate "
                f"where candidate_digest in ({','.join('?' for _ in candidate_digests)}) "
                "order by candidate_digest",
                candidate_digests,
            ).fetchall()
            if len(candidates) != len(candidate_digests) or any(
                digest(_document(row["body_json"])) != row["candidate_digest"] for row in candidates
            ):
                raise PolicyViolation("Local route policy requires registered candidates")
            if len({str(row["exact_id"]) for row in candidates}) != len(candidates):
                raise PolicyViolation("Local route duplicate exact model identity")
            stages = db.execute(
                "select stage_digest,stage,evidence_digest,actor_id,body_json from policy_stage "
                "where revision=? order by ordinal",
                (revision,),
            ).fetchall()
            expected_stages = (
                ("offline-replay", offline_replay_digest),
                ("shadow", shadow_digest),
                ("canary", canary_digest),
                ("independent-review", review_digest),
                ("approval", approval_digest),
            )
            if len(stages) != len(expected_stages) or any(
                row["stage"] != stage
                or row["evidence_digest"] != evidence
                or digest(_document(row["body_json"])) != row["stage_digest"]
                or _document(row["body_json"]).get("revision") != revision
                or _document(row["body_json"]).get("ordinal") != index
                or _document(row["body_json"]).get("candidate_digests") != list(candidate_digests)
                for index, (row, (stage, evidence)) in enumerate(
                    zip(stages, expected_stages, strict=True), 1
                )
            ):
                raise PolicyViolation("Local route policy lifecycle evidence incomplete")
            if stages[3]["actor_id"] != reviewer_model_id:
                raise PolicyViolation("Local route policy review/approval actors invalid")
            tested_models = {str(row["exact_id"]) for row in candidates}
            if reviewer_model_id in tested_models:
                raise PolicyViolation("Local route policy requires independent reviewer")
            artifact = {
                "schema": "zekam-local-route-policy-candidate/v1",
                "revision": revision,
                "candidate_digests": list(candidate_digests),
                "candidate_bodies_digest": digest(
                    [_document(row["body_json"]) for row in candidates]
                ),
                "stages": [
                    {
                        "stage_digest": row["stage_digest"],
                        "stage": row["stage"],
                        "evidence_digest": row["evidence_digest"],
                        "actor_id": row["actor_id"],
                    }
                    for row in stages
                ],
                "reviewer_model_id": reviewer_model_id,
            }
            artifact_digest = digest(artifact)
            activation_effect = digest(
                {
                    "schema": "zekam-local-route-policy-activation-effect/v1",
                    "policy_candidate_digest": artifact_digest,
                }
            )
            terminal = self._terminal_execution(
                activation_job_id,
                operation="model.route.activate",
                effect_digest=activation_effect,
                expected_payload={
                    "operation": "model.route.activate",
                    "policy_candidate_digest": artifact_digest,
                    "authorization_review_digest": review_digest,
                    "authorization_approval_digest": approval_digest,
                    "authorization_actor_id": stages[4]["actor_id"],
                },
            )
            if terminal["status"] != "succeeded":
                raise PolicyViolation("Local route policy activation receipt must complete")
            body = {
                **artifact,
                "schema": "zekam-local-route-policy/v2",
                "policy_candidate_digest": artifact_digest,
                "activation_job_id": activation_job_id,
                "activation_claim_id": terminal["claim_id"],
                "activation_receipt_id": terminal["receipt_id"],
                "activation_evidence_digest": terminal["evidence_digest"],
                "activation_mode": "reviewed-shadow-canary",
                "benchmark_can_activate": False,
            }
            value, raw = digest(body), canonical_json(body)
            if existing is not None:
                if tuple(existing) != (value, activation_job_id, raw):
                    raise ConcurrencyConflict("Local route policy revision replay drift")
                db.rollback()
                return value
            db.execute(
                "insert into policy_revision values(?,?,?,?)",
                (value, revision, activation_job_id, raw),
            )
            db.commit()
        return value

    def record_policy_stage(
        self,
        revision: int,
        candidate_digests: tuple[str, ...],
        stage: str,
        evidence_digest: str,
        actor_id: str,
        evidence_job_id: str | None = None,
    ) -> str:
        order = ("offline-replay", "shadow", "canary", "independent-review", "approval")
        if (
            type(revision) is not int
            or revision < 1
            or stage not in order
            or tuple(sorted(set(candidate_digests))) != candidate_digests
            or not candidate_digests
        ):
            raise ValidationFailed("Local route policy stage invalid")
        for value in (*candidate_digests, evidence_digest):
            parse_digest(value)
        _text(actor_id, "policy stage actor")
        if (stage == "independent-review") != (evidence_job_id is not None):
            raise PolicyViolation("Independent review requires exact operational evidence")
        ordinal = order.index(stage) + 1
        terminal: dict[str, str] | None = None
        if evidence_job_id is not None:
            stage_effect = route_policy_stage_effect_digest(
                revision, candidate_digests, stage, evidence_digest, actor_id
            )
            terminal = self._terminal_execution(
                evidence_job_id,
                operation="model.route.policy-stage",
                effect_digest=stage_effect,
                expected_payload={
                    "operation": "model.route.policy-stage",
                    "revision": revision,
                    "candidate_digests": list(candidate_digests),
                    "stage": stage,
                    "evidence_digest": evidence_digest,
                    "actor_id": actor_id,
                },
            )
            if terminal["status"] != "succeeded":
                raise PolicyViolation("Independent review operational receipt must complete")
        body = {
            "schema": "zekam-local-route-policy-stage/v1",
            "revision": revision,
            "ordinal": ordinal,
            "stage": stage,
            "candidate_digests": list(candidate_digests),
            "evidence_digest": evidence_digest,
            "actor_id": actor_id,
            "evidence_job_id": evidence_job_id,
            "evidence_claim_id": None if terminal is None else terminal["claim_id"],
            "evidence_receipt_id": None if terminal is None else terminal["receipt_id"],
        }
        value, raw = digest(body), canonical_json(body)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select stage_digest,body_json from policy_stage where revision=? and ordinal=?",
                (revision, ordinal),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise ConcurrencyConflict("Local route policy stage replay drift")
                db.rollback()
                return value
            if ordinal > 1:
                previous = db.execute(
                    "select body_json from policy_stage where revision=? and ordinal=?",
                    (revision, ordinal - 1),
                ).fetchone()
                if previous is None or _document(previous[0]).get("candidate_digests") != list(
                    candidate_digests
                ):
                    raise PolicyViolation("Local route policy stage predecessor missing")
            if db.execute(
                f"select count(*) from candidate where candidate_digest in ({','.join('?' for _ in candidate_digests)})",
                candidate_digests,
            ).fetchone()[0] != len(candidate_digests):
                raise PolicyViolation("Local route policy stage candidate missing")
            db.execute(
                "insert into policy_stage values(?,?,?,?,?,?,?)",
                (value, revision, ordinal, stage, evidence_digest, actor_id, raw),
            )
            db.commit()
        return value

    def decide(self, request: LocalRouteRequest, *, now: dt.datetime) -> dict[str, Any]:
        if type(request) is not LocalRouteRequest:
            raise ValidationFailed("Exact local route request required")
        request.__post_init__()
        _instant(now)
        evidence_epoch_digest = self.current_evidence_epoch_digest(request)
        if evidence_epoch_digest != request.evidence_epoch_digest:
            raise PolicyViolation("Local route evidence epoch is stale or caller-controlled")
        with closing(self._connect()) as db:
            policy = db.execute(
                "select policy_digest,body_json from policy_revision order by revision desc limit 1"
            ).fetchone()
            if policy is None or policy["policy_digest"] != request.policy_digest:
                raise PolicyViolation("Local route active policy missing or drifted")
            policy_body = _document(policy["body_json"])
            if digest(policy_body) != policy["policy_digest"]:
                raise PolicyViolation("Local route policy body drift")
            rows = db.execute(
                f"select body_json from candidate where candidate_digest in ({','.join('?' for _ in policy_body['candidate_digests'])}) order by exact_id",
                tuple(policy_body["candidate_digests"]),
            ).fetchall()
            outcome_rows = db.execute(
                "select outcome_digest,exact_id,status,evidence_digest,body_json from route_outcome "
                "order by exact_id,outcome_digest"
            ).fetchall()
        outcomes_by_id: dict[str, list[sqlite3.Row]] = {}
        for outcome in outcome_rows:
            body = _document(outcome["body_json"])
            if (
                digest(body) != outcome["outcome_digest"]
                or body.get("exact_id") != outcome["exact_id"]
                or body.get("status") != outcome["status"]
                or body.get("evidence_digest") != outcome["evidence_digest"]
            ):
                raise PolicyViolation("Local route outcome body drift")
            outcomes_by_id.setdefault(str(outcome["exact_id"]), []).append(outcome)
        candidates: list[dict[str, Any]] = []
        bindings_by_id: dict[str, LocalRouteBinding] = {}
        for row in rows:
            body = _document(row[0])
            binding = LocalRouteBinding(
                body["exact_id"],
                body["snapshot_digest"],
                body["revision_fingerprint"],
                body["health_digest"],
                body["aggregate_digest"],
                body["family_id"],
                body["execution_identity"],
                tuple(body["workloads"]),
                tuple(body["modalities"]),
                tuple(body["data_classifications"]),
                tuple(body["capabilities"]),
                _parse_instant(body["observed_at"]),
                _parse_instant(body["expires_at"]),
            )
            bindings_by_id[binding.exact_id] = binding
            reasons: list[str] = []
            aggregate: dict[str, Any] | None = None
            try:
                aggregate = self._evidence(
                    binding, device_id=request.device_id, client_id=request.client_id, now=now
                )
            except PolicyViolation:
                reasons.append("availability-health-or-revision-stale")
            provider, separator, _ = binding.exact_id.partition("/")
            if not separator or provider not in request.allowed_provider_ids:
                reasons.append("provider-mismatch")
            if request.inventory_snapshot_digest != binding.snapshot_digest:
                reasons.append("inventory-revision-drift")
            if (
                request.workload not in binding.workloads
                or request.modality not in binding.modalities
            ):
                reasons.append("workload-modality-mismatch")
            if request.data_classification not in binding.data_classifications:
                reasons.append("data-classification-mismatch")
            if not set(request.required_capabilities) <= set(binding.capabilities):
                reasons.append("capability-missing")
            if request.independent_from_model_id == binding.exact_id:
                reasons.append("independence-violation")
            if now < binding.observed_at or now >= binding.expires_at:
                reasons.append("benchmark-stale")
            score = 0.0
            outcomes = outcomes_by_id.get(binding.exact_id, [])
            observed_success = (
                0.5
                if not outcomes
                else sum(item["status"] == "succeeded" for item in outcomes) / len(outcomes)
            )
            if aggregate is not None:
                latency = float(aggregate["latency_ms"]["mean"])
                cost = float(aggregate["cost"]["mean"])
                if latency > request.max_latency_ms:
                    reasons.append("latency-budget")
                if cost > request.max_cost:
                    reasons.append("cost-budget")
                score = round(
                    0.20 * float(aggregate["quality"]["mean"])
                    + 0.16 * float(aggregate["reliability"]["mean"])
                    + 0.12 * float(aggregate["pass_rate"])
                    + 0.12 * float(aggregate["confidence_95"][0])
                    + 0.10 * max(0.0, 1.0 - float(aggregate["quality"]["variance"]))
                    + 0.08 * max(0.0, 1.0 - float(aggregate["reliability"]["variance"]))
                    + 0.06 * max(0.0, 1.0 - float(aggregate["failure_rate"]))
                    + 0.04 * max(0.0, 1.0 - float(aggregate["correction_rate"]))
                    + 0.04 * (1.0 if latency <= request.max_latency_ms else 0.0)
                    + 0.04 * (1.0 if cost <= request.max_cost else 0.0)
                    + 0.04 * observed_success,
                    12,
                )
            candidates.append(
                {
                    "exact_id": binding.exact_id,
                    "candidate_digest": binding.candidate_digest,
                    "score": score,
                    "reasons": sorted(set(reasons)),
                    "aggregate_digest": binding.aggregate_digest,
                    "health_digest": binding.health_digest,
                    "outcome_evidence": [item["evidence_digest"] for item in outcomes],
                }
            )
        eligible = sorted(
            (row for row in candidates if not row["reasons"]),
            key=lambda row: (-row["score"], row["exact_id"]),
        )
        primary = None if not eligible else eligible[0]["exact_id"]
        primary_candidate = None if primary is None else bindings_by_id[primary]
        fallback = next(
            (
                item["exact_id"]
                for item in eligible[1:]
                for candidate in (bindings_by_id[item["exact_id"]],)
                if primary_candidate is not None
                and candidate.family_id != primary_candidate.family_id
                and candidate.execution_identity != primary_candidate.execution_identity
                and candidate.exact_id.partition("/")[0]
                != primary_candidate.exact_id.partition("/")[0]
            ),
            None,
        )
        if primary is not None and primary == fallback:
            raise PolicyViolation("Local route primary/fallback exact model identity collision")
        if self.current_evidence_epoch_digest(request) != evidence_epoch_digest:
            raise PolicyViolation("Local route evidence epoch changed during decision")
        body = {
            "schema": "zekam-local-route-decision/v1",
            "request_digest": request.request_digest,
            "policy_digest": request.policy_digest,
            "primary_id": primary,
            "fallback_id": fallback,
            "candidates": candidates,
            "status": "selected" if primary else "pending",
            "authority_granted": False,
        }
        value, raw = digest(body), canonical_json(body)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            old = db.execute(
                "select decision_digest,body_json from route_decision where request_digest=?",
                (request.request_digest,),
            ).fetchone()
            if old is not None:
                if tuple(old) != (value, raw):
                    raise ConcurrencyConflict("Local route replay evidence drift")
                db.rollback()
                return body
            db.execute(
                "insert into route_decision values(?,?,?,?,?,?)",
                (value, request.request_digest, request.policy_digest, primary, fallback, raw),
            )
            db.commit()
        return body

    def claim_effect(self, decision_digest: str, operation_key: str) -> RouteEffectClaim:
        parse_digest(decision_digest)
        _text(operation_key, "operation key")
        with closing(self._connect()) as db:
            decision = db.execute(
                "select primary_id,fallback_id from route_decision where decision_digest=?",
                (decision_digest,),
            ).fetchone()
            if decision is None or decision["primary_id"] is None:
                raise PolicyViolation("Local route effect requires selected decision")
            body = {
                "schema": "zekam-local-route-effect/v1",
                "decision_digest": decision_digest,
                "operation_key": operation_key,
                "primary_id": decision["primary_id"],
                "fallback_id": decision["fallback_id"],
            }
            value, raw = digest(body), canonical_json(body)
            db.execute("begin immediate")
            old = db.execute(
                "select effect_digest,body_json from route_effect where operation_key=?",
                (operation_key,),
            ).fetchone()
            if old is not None:
                if tuple(old) != (value, raw):
                    raise ConcurrencyConflict("Local route effect replay drift")
                terminal = db.execute(
                    "select 1 from route_outcome where effect_digest=? limit 1", (value,)
                ).fetchone()
                db.rollback()
                return RouteEffectClaim(value, "terminal" if terminal is not None else "replay")
            db.execute(
                "insert into route_effect values(?,?,?,?,?,?)",
                (
                    value,
                    decision_digest,
                    operation_key,
                    decision["primary_id"],
                    decision["fallback_id"],
                    raw,
                ),
            )
            db.commit()
        return RouteEffectClaim(value, "fresh")

    def failover_target(self, effect_digest: str, failed_exact_id: str) -> str | None:
        parse_digest(effect_digest)
        _text(failed_exact_id, "failed model")
        with closing(self._connect()) as db:
            row = db.execute(
                "select primary_id,fallback_id from route_effect where effect_digest=?",
                (effect_digest,),
            ).fetchone()
            failed = db.execute(
                "select 1 from route_outcome where effect_digest=? and exact_id=? and status='failed'",
                (effect_digest, failed_exact_id),
            ).fetchone()
        if row is None or row["primary_id"] != failed_exact_id:
            raise PolicyViolation("Local route failover must follow exact primary effect")
        if failed is None:
            raise PolicyViolation("Local route failover requires failed primary receipt")
        return None if row["fallback_id"] is None else str(row["fallback_id"])

    def record_outcome(
        self,
        effect_digest: str,
        exact_id: str,
        *,
        execution_job_id: str,
    ) -> str:
        parse_digest(effect_digest)
        _text(exact_id, "outcome model")
        with closing(self._connect()) as db:
            effect = db.execute(
                "select decision_digest,primary_id,fallback_id from route_effect where effect_digest=?",
                (effect_digest,),
            ).fetchone()
            if effect is None or exact_id not in {effect["primary_id"], effect["fallback_id"]}:
                raise PolicyViolation("Local route outcome outside exact effect")
            if (
                exact_id == effect["fallback_id"]
                and db.execute(
                    "select 1 from route_outcome where effect_digest=? and exact_id=? and status='failed'",
                    (effect_digest, effect["primary_id"]),
                ).fetchone()
                is None
            ):
                raise PolicyViolation("Local route fallback requires failed primary receipt")
            execution_effect = route_execution_effect_digest(effect_digest, exact_id)
            terminal = self._terminal_execution(
                execution_job_id,
                operation="model.route.execute",
                effect_digest=execution_effect,
                expected_payload={
                    "operation": "model.route.execute",
                    "route_effect_digest": effect_digest,
                    "route_decision_digest": effect["decision_digest"],
                    "exact_id": exact_id,
                },
            )
            body = {
                "schema": "zekam-local-route-outcome/v2",
                "effect_digest": effect_digest,
                "exact_id": exact_id,
                "status": terminal["status"],
                "evidence_digest": terminal["evidence_digest"],
                "execution_job_id": terminal["job_id"],
                "execution_claim_id": terminal["claim_id"],
                "execution_receipt_id": terminal["receipt_id"],
                "recovery_resolution_id": terminal["recovery_resolution_id"] or None,
                "observed_at": terminal["observed_at"],
                "grants_policy_authority": False,
            }
            value, raw = digest(body), canonical_json(body)
            db.execute("begin immediate")
            old = db.execute(
                "select outcome_digest,body_json from route_outcome where effect_digest=? and exact_id=?",
                (effect_digest, exact_id),
            ).fetchone()
            if old is not None:
                if tuple(old) != (value, raw):
                    raise ConcurrencyConflict("Local route outcome replay drift")
                db.rollback()
                return value
            db.execute(
                "insert into route_outcome values(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    effect_digest,
                    exact_id,
                    body["status"],
                    body["evidence_digest"],
                    body["execution_job_id"],
                    body["execution_claim_id"],
                    body["execution_receipt_id"],
                    body["recovery_resolution_id"],
                    body["observed_at"],
                    raw,
                ),
            )
            db.commit()
        return value

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as db:
            return {
                table: int(db.execute(f"select count(*) from {table}").fetchone()[0])
                for table in (
                    "candidate",
                    "policy_stage",
                    "policy_revision",
                    "route_decision",
                    "route_effect",
                    "route_outcome",
                )
            }


with closing(sqlite3.connect(":memory:")) as _digest_db:
    _digest_db.executescript(_SCHEMA)
    LOCAL_ROUTING_SCHEMA_DIGEST = _schema_digest(_digest_db)
