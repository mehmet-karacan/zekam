"""Explicit local SQLite source of truth for the WP-09 learning lifecycle.

The store is separate from the operational database.  It is never opened by
default composition and it never imports or migrates legacy PostgreSQL data.
"""

# ruff: noqa: E501 -- literal SQL and canonical query recipes remain reviewable on one line.

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import final

from zekam.application.memory_service import ReviewDecision
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.learning import MINIMUM_SKILL_TRIALS, FailureOccurrence, SkillEvaluation
from zekam.domain.memory import MemoryCandidate
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_tree,
)
from zekam.infrastructure.sqlite.operational_schema import status as operational_status

SCHEMA_VERSION = 1
SCHEMA_DIGEST = "sha256:4d5d9f79cf576a2385179198045d0bd91972c86d9ab996d57cd68eb181d0f038"
MAX_BODY_BYTES = 32_768
_SOURCE_KINDS = frozenset({"receipt", "test", "citation", "observation-summary"})

_SCHEMA = r"""
pragma foreign_keys=on;
create table learning_schema(
 singleton integer primary key check(singleton=1),version integer not null,
 schema_digest text not null
);
create table memory_candidate(
 candidate_digest text primary key,candidate_id text not null unique,scope_digest text not null,
 memory_class text not null,content_digest text not null,source_kind text not null,
 author_ref text not null,observed_at text not null,body_json text not null,
 unique(scope_digest,memory_class,content_digest),
 check(memory_class in ('working','episodic','semantic','procedural','preference','failure')),
 check(source_kind in ('receipt','test','citation','observation-summary')),
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table memory_review(
 review_digest text primary key,candidate_digest text not null references memory_candidate,
 reviewer_ref text not null,approved integer not null check(approved in (0,1)),
 reason text not null,created_at text not null,body_json text not null,
 unique(candidate_digest,reviewer_ref),
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table memory_revision(
 revision_digest text primary key,memory_id text not null,revision integer not null check(revision>0),
 candidate_digest text not null references memory_candidate,
 predecessor_revision_digest text references memory_revision,
 state text not null check(state in ('active','revoked','archived')),
 review_digest text not null references memory_review,created_at text not null,body_json text not null,
 unique(memory_id,revision),unique(candidate_digest),
 check((revision=1)=(predecessor_revision_digest is null)),
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table memory_relation(
 relation_digest text primary key,from_revision_digest text not null references memory_revision,
 to_revision_digest text not null references memory_revision,
 relation_kind text not null check(relation_kind in ('supersedes','supports','conflicts-with')),
 created_at text not null,body_json text not null,
 unique(from_revision_digest,to_revision_digest,relation_kind),
 check(from_revision_digest<>to_revision_digest),
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table memory_head(
 memory_id text primary key,revision_digest text not null unique references memory_revision,
 revision integer not null check(revision>0)
) strict;
create trigger memory_head_insert_guard before insert on memory_head
when not exists(select 1 from memory_revision r where r.revision_digest=new.revision_digest
 and r.memory_id=new.memory_id and r.revision=1 and r.predecessor_revision_digest is null)
begin select raise(abort,'memory head initial revision required'); end;
create trigger memory_head_update_guard before update on memory_head
when new.memory_id is not old.memory_id or new.revision<>old.revision+1
 or not exists(select 1 from memory_revision r where r.revision_digest=new.revision_digest
 and r.memory_id=old.memory_id and r.revision=new.revision
 and r.predecessor_revision_digest=old.revision_digest)
begin select raise(abort,'memory head contiguous supersession required'); end;
create trigger memory_head_no_delete before delete on memory_head
begin select raise(abort,'memory head cannot be deleted'); end;
create table failure_signature(
 signature_digest text primary key,signature_key text not null unique,category text not null,
 created_at text not null,body_json text not null,
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table failure_occurrence(
 occurrence_digest text primary key,signature_digest text not null references failure_signature,
 evidence_digest text not null,run_ref text not null,observed_at text not null,body_json text not null,
 unique(signature_digest,evidence_digest),
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table failure_card(
 card_digest text primary key,signature_digest text not null unique references failure_signature,
 author_ref text not null,reviewed_by text not null,created_at text not null,body_json text not null,
 check(author_ref<>reviewed_by),
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table lesson(
 lesson_digest text primary key,card_digest text not null references failure_card,
 author_ref text not null,created_at text not null,body_json text not null,
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table skill_manifest(
 manifest_digest text primary key,skill_id text not null,version integer not null check(version>0),
 lesson_digest text not null references lesson,author_ref text not null,state text not null,
 created_at text not null,body_json text not null,unique(skill_id,version),
 check(state='candidate'),
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table skill_evaluation(
 evaluation_digest text primary key,manifest_digest text not null references skill_manifest,
 evaluator_ref text not null,verifier_ref text not null,trials integer not null,
 successes integer not null,baseline real not null,created_at text not null,body_json text not null,
 unique(manifest_digest,evaluation_digest),check(evaluator_ref<>verifier_ref),
 check(trials>=5 and successes between 0 and trials and baseline between 0.0 and 1.0),
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table skill_review(
 review_digest text primary key,manifest_digest text not null references skill_manifest,
 evaluation_digest text not null references skill_evaluation,reviewer_ref text not null,
 approved integer not null check(approved in (0,1)),reason text not null,created_at text not null,
 body_json text not null,unique(manifest_digest,reviewer_ref),
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table skill_activation(
 activation_digest text primary key,manifest_digest text not null unique references skill_manifest,
 evaluation_digest text not null references skill_evaluation,
 review_digest text not null references skill_review,activated_at text not null,body_json text not null,
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table skill_usage(
 usage_digest text primary key,activation_digest text not null references skill_activation,
 run_ref text not null,used_at text not null,body_json text not null,
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table skill_outcome(
 outcome_digest text primary key,usage_digest text not null unique references skill_usage,
 status text not null check(status in ('verified-success','verified-failure')),
 verifier_ref text not null,observed_at text not null,body_json text not null,
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
create table hygiene_proposal(
 proposal_digest text primary key,subject_digest text not null,
 finding text not null check(finding in ('duplicate','conflict','stale','supersession','retention-review')),
 created_at text not null,body_json text not null,
 unique(subject_digest,finding),
 check(json_valid(body_json) and length(cast(body_json as blob)) between 2 and 32768)
) strict;
"""

for _table in (
    "memory_candidate",
    "memory_review",
    "memory_revision",
    "memory_relation",
    "failure_signature",
    "failure_occurrence",
    "failure_card",
    "lesson",
    "skill_manifest",
    "skill_evaluation",
    "skill_review",
    "skill_activation",
    "skill_usage",
    "skill_outcome",
    "hygiene_proposal",
):
    _SCHEMA += f"create trigger {_table}_no_update before update on {_table} begin select raise(abort,'append-only'); end;\n"
    _SCHEMA += f"create trigger {_table}_no_delete before delete on {_table} begin select raise(abort,'append-only'); end;\n"


def _time(value: dt.datetime) -> str:
    if type(value) is not dt.datetime or value.tzinfo is None:
        raise ValidationFailed("WP-09 timezone-aware datetime required")
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()


def _parse_time(value: object) -> dt.datetime:
    if type(value) is not str:
        raise PolicyViolation("WP-09 stored timestamp type drift")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise PolicyViolation("WP-09 stored timestamp malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PolicyViolation("WP-09 stored timestamp lacks timezone")
    return parsed.astimezone(dt.UTC)


def _text(value: object, label: str, *, maximum: int = 4096) -> str:
    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise ValidationFailed(f"WP-09 bounded {label} required")
    return value


def _body(value: Mapping[str, object]) -> tuple[str, str]:
    raw = canonical_json(value)
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValidationFailed("WP-09 canonical body exceeds bound")
    return raw, digest(value)


def _schema_digest(db: sqlite3.Connection) -> str:
    rows = db.execute(
        "select type,name,sql from sqlite_master "
        "where type in ('table','trigger') and name not like 'sqlite_%' "
        "order by type,name"
    ).fetchall()
    return digest([{"type": str(row[0]), "name": str(row[1]), "sql": str(row[2])} for row in rows])


@dataclass(frozen=True, slots=True)
class FailureCardDraft:
    symptom: str
    environment: str
    root_cause: str
    unsafe_workaround: str
    safe_remediation: str
    verification: str
    source_refs: tuple[str, ...]
    author_ref: str
    reviewed_by: str

    def __post_init__(self) -> None:
        for label in (
            "symptom",
            "environment",
            "root_cause",
            "unsafe_workaround",
            "safe_remediation",
            "verification",
            "author_ref",
            "reviewed_by",
        ):
            _text(getattr(self, label), label)
        if self.author_ref == self.reviewed_by:
            raise PolicyViolation("Failure card requires independent review")
        if type(self.source_refs) is not tuple or not 2 <= len(set(self.source_refs)) <= 16:
            raise ValidationFailed("Failure card requires bounded distinct source refs")
        for value in self.source_refs:
            _text(value, "source ref", maximum=512)


@dataclass(frozen=True, slots=True)
class SkillManifestDraft:
    skill_id: str
    version: int
    purpose: str
    triggers: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    required_tools: tuple[str, ...]
    steps: tuple[str, ...]
    checks: tuple[str, ...]
    risks: tuple[str, ...]
    permissions_ceiling: str
    source_evidence: tuple[str, ...]
    rollback: str
    deprecation: str
    author_ref: str

    def __post_init__(self) -> None:
        if type(self.version) is not int or not 1 <= self.version <= 10_000:
            raise ValidationFailed("Skill version invalid")
        for label in (
            "skill_id",
            "purpose",
            "permissions_ceiling",
            "rollback",
            "deprecation",
            "author_ref",
        ):
            _text(getattr(self, label), label)
        for label in (
            "triggers",
            "inputs",
            "outputs",
            "steps",
            "checks",
            "risks",
            "source_evidence",
        ):
            values = getattr(self, label)
            if (
                type(values) is not tuple
                or not 1 <= len(values) <= 32
                or len(set(values)) != len(values)
            ):
                raise ValidationFailed(f"Skill {label} must be bounded and distinct")
            for value in values:
                _text(value, label, maximum=512)
        if (
            type(self.required_tools) is not tuple
            or len(self.required_tools) > 16
            or len(set(self.required_tools)) != len(self.required_tools)
        ):
            raise ValidationFailed("Skill tool set invalid")
        for value in self.required_tools:
            _text(value, "required tool", maximum=128)
        if self.permissions_ceiling not in {
            "read-only",
            "workspace-write-no-network",
        }:
            raise ValidationFailed("Skill permission ceiling invalid")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-local-skill-manifest/v1",
            "skill_id": self.skill_id,
            "version": self.version,
            "purpose": self.purpose,
            "triggers": list(self.triggers),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "required_tools": list(self.required_tools),
            "steps": list(self.steps),
            "checks": list(self.checks),
            "risks": list(self.risks),
            "permissions_ceiling": self.permissions_ceiling,
            "source_evidence": list(self.source_evidence),
            "rollback": self.rollback,
            "deprecation": self.deprecation,
            "author_ref": self.author_ref,
            "grants_authority": False,
        }


@final
class SQLiteLocalLearning:
    """Append-only, explicitly constructed WP-09 local learning store."""

    def __init__(self, path: Path, *, operational_path: Path) -> None:
        if type(path) is not type(Path()) or not path.is_absolute() or path.is_symlink():
            raise ValidationFailed("WP-09 exact absolute SQLite path required")
        if (
            type(operational_path) is not type(Path())
            or not operational_path.is_absolute()
            or operational_path.is_symlink()
            or operational_path == path
        ):
            raise ValidationFailed("WP-09 distinct operational evidence path required")
        self.path = path
        self.operational_path = operational_path

    def _operational(self) -> sqlite3.Connection:
        if not private_regular(self.operational_path):
            raise PolicyViolation("WP-09 operational evidence identity required")
        observed = operational_status(self.operational_path)
        if (
            not observed.integrity_ok
            or not observed.schema_ok
            or observed.schema_version
            not in {
                3,
                4,
            }
        ):
            raise PolicyViolation("WP-09 current operational evidence schema required")
        db = sqlite3.connect(
            f"{self.operational_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
        )
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        db.execute("pragma query_only=on")
        return db

    def _close_evidence(self, candidate: MemoryCandidate) -> None:
        receipts = tuple(item.digest_value for item in candidate.evidence if item.kind == "receipt")
        if not receipts:
            raise PolicyViolation("Memory candidate requires WP-08 close receipt evidence")
        with closing(self._operational()) as db:
            for receipt in receipts:
                row = db.execute(
                    "select 1 from close_receipt r join session s on s.id=r.session_id "
                    "where r.receipt_digest=? and s.status='closed' "
                    "and s.close_receipt_digest=r.receipt_digest",
                    (receipt,),
                ).fetchone()
                if row is None:
                    raise PolicyViolation("Memory evidence is not a terminal WP-08 close receipt")

    def _failure_evidence(self, occurrence: FailureOccurrence) -> None:
        effect = digest(
            {
                "operation": "failure.observe",
                "job_id": occurrence.run_ref,
                "occurrence_key": occurrence.occurrence_key,
                "evidence_digest": occurrence.evidence_digest,
                "failure_category": occurrence.failure_category,
            }
        )
        with closing(self._operational()) as db:
            row = db.execute(
                "select c.claimed_at,r.created_at,j.updated_at from local_effect_receipt r "
                "join local_effect_claim c on c.id=r.claim_id "
                "join local_job j on j.id=c.job_id "
                "where j.id=? and r.evidence_digest=? and r.status='failed' "
                "and j.state='failed' and j.terminal_evidence_digest=r.evidence_digest "
                "and c.operation='failure.observe' and c.effect_digest=? "
                "and json_type(j.payload_json,'$.operation')='text' "
                "and json_extract(j.payload_json,'$.operation')='failure.observe' "
                "and json_extract(j.payload_json,'$.occurrence_key')=? "
                "and json_extract(j.payload_json,'$.evidence_digest')=? "
                "and json_extract(j.payload_json,'$.failure_category')=?",
                (
                    occurrence.run_ref,
                    occurrence.evidence_digest,
                    effect,
                    occurrence.occurrence_key,
                    occurrence.evidence_digest,
                    occurrence.failure_category,
                ),
            ).fetchone()
        if row is None or not (
            _parse_time(row["claimed_at"])
            <= _parse_time(row["created_at"])
            <= _parse_time(row["updated_at"])
        ):
            raise PolicyViolation("Failure occurrence requires exact terminal run receipt")

    def _activation_evidence(
        self,
        job_id: str,
        manifest_digest: str,
        evaluation_digest: str,
        review_digest: str,
    ) -> dict[str, str]:
        _text(job_id, "activation job")
        effect = digest(
            {
                "operation": "skill.activate",
                "manifest_digest": manifest_digest,
                "evaluation_digest": evaluation_digest,
                "review_digest": review_digest,
            }
        )
        with closing(self._operational()) as db:
            row = db.execute(
                "select c.id claim_id,r.id receipt_id,r.evidence_digest,"
                "j.created_at authorization_at,c.claimed_at,r.created_at receipt_at,"
                "j.updated_at terminal_at "
                "from local_job j join local_effect_claim c on c.job_id=j.id "
                "join local_effect_receipt r on r.claim_id=c.id "
                "where j.id=? and j.state='completed' and c.operation='skill.activate' "
                "and c.effect_digest=? and r.status='completed' "
                "and j.terminal_evidence_digest=r.evidence_digest "
                "and json_extract(j.payload_json,'$.authorization_review_digest')=?",
                (job_id, effect, review_digest),
            ).fetchone()
        if row is None:
            raise PolicyViolation(
                "Skill activation requires authorization, claim-before-effect and terminal receipt"
            )
        return {
            "job_id": job_id,
            "claim_id": str(row["claim_id"]),
            "receipt_id": str(row["receipt_id"]),
            "receipt_evidence_digest": str(row["evidence_digest"]),
            "authorization_at": str(row["authorization_at"]),
            "claimed_at": str(row["claimed_at"]),
            "receipt_at": str(row["receipt_at"]),
            "terminal_at": str(row["terminal_at"]),
        }

    def bootstrap(self) -> None:
        created = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            restrict_private_tree(self.path.parent)
        if not private_directory(self.path.parent):
            raise PolicyViolation("WP-09 private parent directory required")
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(_SCHEMA)
            db.execute(
                "insert into learning_schema values(1,?,?)",
                (SCHEMA_VERSION, SCHEMA_DIGEST),
            )
            db.commit()
        self.path.chmod(0o600)

    def _file_ok(self) -> None:
        if not private_regular(self.path):
            raise PolicyViolation("WP-09 private SQLite identity required")

    def _connect(self) -> sqlite3.Connection:
        self._file_ok()
        db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        db.execute("pragma busy_timeout=5000")
        row = db.execute(
            "select version,schema_digest from learning_schema where singleton=1"
        ).fetchone()
        if (
            row is None
            or row[0] != SCHEMA_VERSION
            or row[1] != SCHEMA_DIGEST
            or _schema_digest(db) != SCHEMA_DIGEST
        ):
            db.close()
            raise PolicyViolation("WP-09 local schema drift")
        return db

    def audit(self) -> dict[str, int]:
        """Bounded read-only canonical and relational integrity census."""
        digest_columns = {
            "memory_candidate": "candidate_digest",
            "memory_review": "review_digest",
            "memory_revision": "revision_digest",
            "memory_relation": "relation_digest",
            "failure_signature": "signature_digest",
            "failure_occurrence": "occurrence_digest",
            "failure_card": "card_digest",
            "lesson": "lesson_digest",
            "skill_manifest": "manifest_digest",
            "skill_evaluation": "evaluation_digest",
            "skill_review": "review_digest",
            "skill_activation": "activation_digest",
            "skill_usage": "usage_digest",
            "skill_outcome": "outcome_digest",
            "hygiene_proposal": "proposal_digest",
        }
        counts: dict[str, int] = {}
        with closing(self._connect()) as db:
            db.execute("pragma query_only=on")
            db.execute("begin")
            if db.execute("pragma integrity_check").fetchone()[0] != "ok":
                raise PolicyViolation("WP-09 SQLite integrity drift")
            if db.execute("pragma foreign_key_check").fetchone() is not None:
                raise PolicyViolation("WP-09 SQLite relation drift")
            for table, identity in digest_columns.items():
                rows = db.execute(f"select {identity},body_json from {table} limit 4097").fetchall()
                if len(rows) > 4096:
                    raise PolicyViolation("WP-09 audit row bound exceeded")
                for row in rows:
                    try:
                        document = json.loads(str(row["body_json"]))
                    except (TypeError, ValueError) as exc:
                        raise PolicyViolation("WP-09 stored JSON malformed") from exc
                    if (
                        type(document) is not dict
                        or canonical_json(document) != row["body_json"]
                        or digest(document) != row[identity]
                    ):
                        raise PolicyViolation("WP-09 stored canonical digest drift")
                counts[table] = len(rows)
            heads = db.execute(
                "select h.memory_id,h.revision_digest,h.revision,"
                "(select r.revision_digest from memory_revision r "
                "where r.memory_id=h.memory_id order by r.revision desc limit 1) latest "
                "from memory_head h limit 4097"
            ).fetchall()
            if len(heads) > 4096 or any(row[1] != row[3] for row in heads):
                raise PolicyViolation("WP-09 memory head drift")
            family_count = int(
                db.execute("select count(distinct memory_id) from memory_revision").fetchone()[0]
            )
            if family_count != len(heads):
                raise PolicyViolation("WP-09 memory family head cardinality drift")
            counts["memory_head"] = len(heads)
            db.rollback()
        return counts

    def propose_memory(self, candidate: MemoryCandidate, *, source_kind: str) -> str:
        if (
            type(candidate) is not MemoryCandidate
            or type(source_kind) is not str
            or source_kind not in _SOURCE_KINDS
        ):
            raise PolicyViolation("Raw or untyped input cannot become a memory candidate")
        candidate.__post_init__()
        if not candidate.evidence:
            raise PolicyViolation("Memory candidate requires durable evidence")
        self._close_evidence(candidate)
        body = {
            **candidate.as_dict(),
            "schema": "zekam-local-memory-candidate/v1",
            "source_kind": source_kind,
        }
        raw, value = _body(body)
        scope = digest(candidate.key.as_dict())
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select candidate_digest,body_json from memory_candidate where scope_digest=? and memory_class=? and content_digest=?",
                (scope, str(candidate.memory_class), digest(candidate.content)),
            ).fetchone()
            if existing is not None:
                if str(existing["body_json"]) != raw:
                    raise PolicyViolation("Memory duplicate payload drift")
                db.rollback()
                return str(existing["candidate_digest"])
            db.execute(
                "insert into memory_candidate values(?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    candidate.candidate_id,
                    scope,
                    str(candidate.memory_class),
                    digest(candidate.content),
                    source_kind,
                    candidate.author_ref,
                    _time(candidate.observed_at),
                    raw,
                ),
            )
            db.commit()
        return value

    def review_memory(
        self, candidate_digest: str, decision: ReviewDecision, *, now: dt.datetime
    ) -> str:
        parse_digest(candidate_digest)
        if type(decision) is not ReviewDecision:
            raise ValidationFailed("Memory exact review decision required")
        if type(decision.approved) is not bool:
            raise ValidationFailed("Memory review approval must be bool")
        decision.__post_init__()
        body = {
            "schema": "zekam-local-memory-review/v1",
            "candidate_digest": candidate_digest,
            **decision.as_dict(),
            "created_at": _time(now),
        }
        raw, value = _body(body)
        with closing(self._connect()) as db:
            row = db.execute(
                "select author_ref,observed_at from memory_candidate where candidate_digest=?",
                (candidate_digest,),
            ).fetchone()
            if row is None or row[0] == decision.reviewer_ref:
                raise PolicyViolation("Memory review must be independent and in scope")
            if _time(now) < str(row["observed_at"]):
                raise PolicyViolation("Memory review cannot predate its candidate")
            db.execute(
                "insert into memory_review values(?,?,?,?,?,?,?)",
                (
                    value,
                    candidate_digest,
                    decision.reviewer_ref,
                    int(decision.approved),
                    decision.reason,
                    _time(now),
                    raw,
                ),
            )
            db.commit()
        return value

    def activate_memory(
        self,
        candidate_digest: str,
        review_digest: str,
        *,
        now: dt.datetime,
        supersedes: str | None = None,
    ) -> str:
        parse_digest(candidate_digest)
        parse_digest(review_digest)
        timestamp = _time(now)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            candidate = db.execute(
                "select * from memory_candidate where candidate_digest=?", (candidate_digest,)
            ).fetchone()
            review = db.execute(
                "select * from memory_review where review_digest=? and candidate_digest=? and approved=1",
                (review_digest, candidate_digest),
            ).fetchone()
            if candidate is None or review is None:
                raise PolicyViolation("Memory activation requires approved exact review")
            if timestamp < str(review["created_at"]) or timestamp < str(candidate["observed_at"]):
                raise PolicyViolation("Memory activation cannot predate candidate or review")
            if (
                candidate["memory_class"] == "failure"
                and int(
                    db.execute(
                        "select json_extract(body_json,'$.observation_count') "
                        "from memory_candidate where candidate_digest=?",
                        (candidate_digest,),
                    ).fetchone()[0]
                )
                < 2
            ):
                raise PolicyViolation("Failure memory requires two independent observations")
            latest = db.execute(
                "select r.* from memory_revision r join memory_candidate c on c.candidate_digest=r.candidate_digest where c.scope_digest=? and c.memory_class=? order by r.revision desc limit 1",
                (candidate["scope_digest"], candidate["memory_class"]),
            ).fetchone()
            if latest is not None and (
                supersedes is None or latest["revision_digest"] != supersedes
            ):
                raise PolicyViolation(
                    "Memory conflict/stale activation requires exact supersession"
                )
            if latest is not None and str(candidate["observed_at"]) < str(latest["created_at"]):
                raise PolicyViolation("Stale memory candidate cannot supersede current revision")
            revision = 1 if latest is None else int(latest["revision"]) + 1
            memory_id = "memory:" + digest(
                {
                    "scope_digest": candidate["scope_digest"],
                    "memory_class": candidate["memory_class"],
                }
            ).removeprefix("sha256:")
            body = {
                "schema": "zekam-local-memory-revision/v1",
                "memory_id": memory_id,
                "revision": revision,
                "candidate_digest": candidate_digest,
                "predecessor_revision_digest": supersedes,
                "state": "active",
                "review_digest": review_digest,
                "created_at": timestamp,
            }
            raw, value = _body(body)
            db.execute(
                "insert into memory_revision values(?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    memory_id,
                    revision,
                    candidate_digest,
                    supersedes,
                    "active",
                    review_digest,
                    timestamp,
                    raw,
                ),
            )
            if latest is None:
                db.execute("insert into memory_head values(?,?,1)", (memory_id, value))
            else:
                db.execute(
                    "update memory_head set revision_digest=?,revision=? where memory_id=? "
                    "and revision_digest=? and revision=?",
                    (value, revision, memory_id, supersedes, revision - 1),
                )
                if db.execute("select changes()").fetchone()[0] != 1:
                    raise PolicyViolation("Memory current head changed concurrently")
            if supersedes is not None:
                relation = {
                    "schema": "zekam-local-memory-relation/v1",
                    "from_revision_digest": value,
                    "to_revision_digest": supersedes,
                    "relation_kind": "supersedes",
                    "created_at": timestamp,
                }
                relation_raw, relation_digest = _body(relation)
                db.execute(
                    "insert into memory_relation values(?,?,?,?,?,?)",
                    (relation_digest, value, supersedes, "supersedes", timestamp, relation_raw),
                )
            db.commit()
        return value

    def observe_failure(self, occurrence: FailureOccurrence) -> str:
        if type(occurrence) is not FailureOccurrence:
            raise ValidationFailed("Exact failure occurrence required")
        occurrence.__post_init__()
        self._failure_evidence(occurrence)
        signature_body = {
            "schema": "zekam-local-failure-signature/v1",
            "signature_key": occurrence.occurrence_key,
            "category": occurrence.failure_category,
        }
        signature_raw, signature = _body(signature_body)
        occurrence_body = {
            "schema": "zekam-local-failure-occurrence/v1",
            "signature_digest": signature,
            **occurrence.as_dict(),
            "observed_at": _time(occurrence.observed_at),
        }
        occurrence_raw, occurrence_digest = _body(occurrence_body)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            db.execute(
                "insert or ignore into failure_signature values(?,?,?,?,?)",
                (
                    signature,
                    occurrence.occurrence_key,
                    occurrence.failure_category,
                    _time(occurrence.observed_at),
                    signature_raw,
                ),
            )
            row = db.execute(
                "select category from failure_signature where signature_digest=?", (signature,)
            ).fetchone()
            if row is None or row[0] != occurrence.failure_category:
                raise PolicyViolation("Failure signature conflict")
            db.execute(
                "insert or ignore into failure_occurrence values(?,?,?,?,?,?)",
                (
                    occurrence_digest,
                    signature,
                    occurrence.evidence_digest,
                    occurrence.run_ref,
                    _time(occurrence.observed_at),
                    occurrence_raw,
                ),
            )
            db.commit()
        return signature

    def create_failure_card(
        self, signature: str, draft: FailureCardDraft, *, now: dt.datetime
    ) -> str:
        parse_digest(signature)
        if type(draft) is not FailureCardDraft:
            raise ValidationFailed("Exact failure card draft required")
        draft.__post_init__()
        with closing(self._connect()) as db:
            rows = db.execute(
                "select evidence_digest from failure_occurrence where signature_digest=?",
                (signature,),
            ).fetchall()
            if len({str(row[0]) for row in rows}) < 2:
                raise PolicyViolation("Failure card requires two independent observations")
            body = {
                "schema": "zekam-local-failure-card/v1",
                "signature_digest": signature,
                "symptom": draft.symptom,
                "environment": draft.environment,
                "root_cause": draft.root_cause,
                "unsafe_workaround": draft.unsafe_workaround,
                "safe_remediation": draft.safe_remediation,
                "verification": draft.verification,
                "source_refs": list(draft.source_refs),
                "author_ref": draft.author_ref,
                "reviewed_by": draft.reviewed_by,
                "created_at": _time(now),
            }
            raw, value = _body(body)
            db.execute(
                "insert into failure_card values(?,?,?,?,?,?)",
                (value, signature, draft.author_ref, draft.reviewed_by, _time(now), raw),
            )
            db.commit()
        return value

    def extract_lesson(
        self, card_digest: str, lesson: str, *, author_ref: str, now: dt.datetime
    ) -> str:
        parse_digest(card_digest)
        _text(lesson, "lesson")
        _text(author_ref, "author")
        body = {
            "schema": "zekam-local-lesson/v1",
            "card_digest": card_digest,
            "lesson": lesson,
            "author_ref": author_ref,
            "created_at": _time(now),
        }
        raw, value = _body(body)
        with closing(self._connect()) as db:
            if (
                db.execute(
                    "select 1 from failure_card where card_digest=?", (card_digest,)
                ).fetchone()
                is None
            ):
                raise PolicyViolation("Lesson requires reviewed failure card")
            db.execute(
                "insert into lesson values(?,?,?,?,?)",
                (value, card_digest, author_ref, _time(now), raw),
            )
            db.commit()
        return value

    def propose_skill(
        self, draft: SkillManifestDraft, lesson_digest: str, *, now: dt.datetime
    ) -> str:
        if type(draft) is not SkillManifestDraft:
            raise ValidationFailed("Exact skill manifest draft required")
        draft.__post_init__()
        parse_digest(lesson_digest)
        body = {**draft.body(), "lesson_digest": lesson_digest, "created_at": _time(now)}
        raw, value = _body(body)
        with closing(self._connect()) as db:
            if (
                db.execute(
                    "select 1 from lesson where lesson_digest=?", (lesson_digest,)
                ).fetchone()
                is None
            ):
                raise PolicyViolation("Skill requires durable lesson evidence")
            db.execute(
                "insert into skill_manifest values(?,?,?,?,?,?,?,?)",
                (
                    value,
                    draft.skill_id,
                    draft.version,
                    lesson_digest,
                    draft.author_ref,
                    "candidate",
                    _time(now),
                    raw,
                ),
            )
            db.commit()
        return value

    def evaluate_skill(
        self, manifest_digest: str, evaluation: SkillEvaluation, *, now: dt.datetime
    ) -> str:
        parse_digest(manifest_digest)
        if type(evaluation) is not SkillEvaluation:
            raise ValidationFailed("Exact skill evaluation required")
        if (
            type(evaluation.trials) is not int
            or type(evaluation.successes) is not int
            or type(evaluation.baseline_success_rate) is not float
        ):
            raise ValidationFailed("Skill evaluation numeric types invalid")
        evaluation.__post_init__()
        with closing(self._connect()) as db:
            manifest = db.execute(
                "select skill_id,created_at from skill_manifest where manifest_digest=?",
                (manifest_digest,),
            ).fetchone()
            if (
                manifest is None
                or manifest[0] != evaluation.skill_id
                or not evaluation.improves
                or _parse_time(manifest["created_at"]) >= _parse_time(_time(now))
            ):
                raise PolicyViolation("Skill evaluation must match and improve baseline")
            body = {
                "schema": "zekam-local-skill-evaluation/v1",
                "manifest_digest": manifest_digest,
                **evaluation.as_dict(),
                "created_at": _time(now),
            }
            raw, value = _body(body)
            db.execute(
                "insert into skill_evaluation values(?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    manifest_digest,
                    evaluation.evaluator_ref,
                    evaluation.verifier_ref,
                    evaluation.trials,
                    evaluation.successes,
                    evaluation.baseline_success_rate,
                    _time(now),
                    raw,
                ),
            )
            db.commit()
        return value

    def review_skill(
        self,
        manifest_digest: str,
        evaluation_digest: str,
        decision: ReviewDecision,
        *,
        now: dt.datetime,
    ) -> str:
        parse_digest(manifest_digest)
        parse_digest(evaluation_digest)
        if type(decision) is not ReviewDecision:
            raise ValidationFailed("Exact skill review required")
        if type(decision.approved) is not bool:
            raise ValidationFailed("Skill review approval must be bool")
        decision.__post_init__()
        with closing(self._connect()) as db:
            row = db.execute(
                "select m.author_ref,e.evaluator_ref,e.verifier_ref,m.created_at manifest_at,"
                "e.created_at evaluation_at from skill_manifest m join skill_evaluation e "
                "on e.manifest_digest=m.manifest_digest where m.manifest_digest=? "
                "and e.evaluation_digest=?",
                (manifest_digest, evaluation_digest),
            ).fetchone()
            if row is None or decision.reviewer_ref in tuple(row):
                raise PolicyViolation("Skill review must be independent")
            if not (
                _parse_time(row["manifest_at"])
                < _parse_time(row["evaluation_at"])
                < _parse_time(_time(now))
            ):
                raise PolicyViolation("Skill review chronology invalid")
            body = {
                "schema": "zekam-local-skill-review/v1",
                "manifest_digest": manifest_digest,
                "evaluation_digest": evaluation_digest,
                **decision.as_dict(),
                "created_at": _time(now),
            }
            raw, value = _body(body)
            db.execute(
                "insert into skill_review values(?,?,?,?,?,?,?,?)",
                (
                    value,
                    manifest_digest,
                    evaluation_digest,
                    decision.reviewer_ref,
                    int(decision.approved),
                    decision.reason,
                    _time(now),
                    raw,
                ),
            )
            db.commit()
        return value

    def activate_skill(
        self,
        manifest_digest: str,
        evaluation_digest: str,
        review_digest: str,
        *,
        activation_job_id: str,
        now: dt.datetime,
    ) -> str:
        for value in (manifest_digest, evaluation_digest, review_digest):
            parse_digest(value)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            row = db.execute(
                "select m.body_json,e.trials,e.successes,e.baseline,r.approved,"
                "m.created_at manifest_at,e.created_at evaluation_at,r.created_at review_at "
                "from skill_manifest m join skill_evaluation e "
                "on e.manifest_digest=m.manifest_digest join skill_review r "
                "on r.manifest_digest=m.manifest_digest "
                "and r.evaluation_digest=e.evaluation_digest where m.manifest_digest=? "
                "and e.evaluation_digest=? and r.review_digest=?",
                (manifest_digest, evaluation_digest, review_digest),
            ).fetchone()
            if (
                row is None
                or row[4] != 1
                or int(row[1]) < MINIMUM_SKILL_TRIALS
                or int(row[2]) / int(row[1]) <= float(row[3])
            ):
                raise PolicyViolation(
                    "Skill activation requires passing tests and independent approval"
                )
            runtime_evidence = self._activation_evidence(
                activation_job_id, manifest_digest, evaluation_digest, review_digest
            )
            chronology = (
                _parse_time(row["manifest_at"]),
                _parse_time(row["evaluation_at"]),
                _parse_time(row["review_at"]),
                _parse_time(runtime_evidence["authorization_at"]),
                _parse_time(runtime_evidence["claimed_at"]),
                _parse_time(runtime_evidence["receipt_at"]),
                _parse_time(runtime_evidence["terminal_at"]),
                _parse_time(_time(now)),
            )
            if any(left >= right for left, right in pairwise(chronology)):
                raise PolicyViolation("Skill activation causal chronology invalid")
            body = {
                "schema": "zekam-local-skill-activation/v1",
                "manifest_digest": manifest_digest,
                "evaluation_digest": evaluation_digest,
                "review_digest": review_digest,
                "runtime_evidence": runtime_evidence,
                "activated_at": _time(now),
            }
            raw, value = _body(body)
            existing = db.execute(
                "select activation_digest,body_json from skill_activation where manifest_digest=?",
                (manifest_digest,),
            ).fetchone()
            if existing is not None:
                if str(existing["body_json"]) != raw:
                    raise PolicyViolation("Skill activation replay drift")
                db.rollback()
                return str(existing["activation_digest"])
            db.execute(
                "insert into skill_activation values(?,?,?,?,?,?)",
                (value, manifest_digest, evaluation_digest, review_digest, _time(now), raw),
            )
            db.commit()
        return value

    def record_skill_outcome(
        self,
        activation_digest: str,
        *,
        run_ref: str,
        usage_digest: str,
        outcome: str,
        verifier_ref: str,
        now: dt.datetime,
    ) -> tuple[str, str]:
        parse_digest(activation_digest)
        parse_digest(usage_digest)
        _text(run_ref, "run ref")
        _text(verifier_ref, "verifier")
        if type(outcome) is not str or outcome not in {
            "verified-success",
            "verified-failure",
        }:
            raise ValidationFailed("Skill outcome must be verified")
        used = {
            "schema": "zekam-local-skill-usage/v1",
            "activation_digest": activation_digest,
            "run_ref": run_ref,
            "usage_evidence_digest": usage_digest,
            "used_at": _time(now),
        }
        used_raw, used_id = _body(used)
        result = {
            "schema": "zekam-local-skill-outcome/v1",
            "usage_digest": used_id,
            "status": outcome,
            "verifier_ref": verifier_ref,
            "observed_at": _time(now),
        }
        result_raw, result_id = _body(result)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            db.execute(
                "insert into skill_usage values(?,?,?,?,?)",
                (used_id, activation_digest, run_ref, _time(now), used_raw),
            )
            db.execute(
                "insert into skill_outcome values(?,?,?,?,?,?)",
                (result_id, used_id, outcome, verifier_ref, _time(now), result_raw),
            )
            db.commit()
        return used_id, result_id

    def effectiveness(self, activation_digest: str) -> dict[str, int]:
        parse_digest(activation_digest)
        with closing(self._connect()) as db:
            row = db.execute(
                "select count(u.usage_digest),count(o.outcome_digest),sum(case when o.status='verified-success' then 1 else 0 end) from skill_usage u left join skill_outcome o on o.usage_digest=u.usage_digest where u.activation_digest=?",
                (activation_digest,),
            ).fetchone()
        return {
            "usage_count": int(row[0]),
            "verified_outcome_count": int(row[1]),
            "verified_success_count": int(row[2] or 0),
        }

    def propose_hygiene(self, subject_digest: str, finding: str, *, now: dt.datetime) -> str:
        parse_digest(subject_digest)
        if type(finding) is not str or finding not in {
            "duplicate",
            "conflict",
            "stale",
            "supersession",
            "retention-review",
        }:
            raise ValidationFailed("Unknown hygiene proposal")
        body = {
            "schema": "zekam-local-hygiene-proposal/v1",
            "subject_digest": subject_digest,
            "finding": finding,
            "created_at": _time(now),
            "automatic_delete": False,
        }
        raw, value = _body(body)
        with closing(self._connect()) as db:
            db.execute(
                "insert into hygiene_proposal values(?,?,?,?,?)",
                (value, subject_digest, finding, _time(now), raw),
            )
            db.commit()
        return value
