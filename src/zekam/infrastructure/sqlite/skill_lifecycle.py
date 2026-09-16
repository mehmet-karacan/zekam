"""SQLite v2 personal-skill lifecycle over the existing learning authority."""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, cast

from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.domain.personal_skill import (
    PersonalSkillRevision,
    SkillEvaluationV2,
    SkillInvocationState,
    SkillOriginEvidence,
    SkillOriginKind,
    SkillScopeKind,
    assert_invocation_transition,
)
from zekam.infrastructure.local_file_security import private_regular
from zekam.infrastructure.sqlite.local_learning import (
    SCHEMA_DIGEST,
    SCHEMA_VERSION,
    _required_v2_indexes_present,
    _schema_digest,
)
from zekam.infrastructure.sqlite.operational_schema import status as operational_status

_WORD = re.compile(r"[a-z0-9çğıöşü]+", re.IGNORECASE)


def activation_effect_digest(
    revision_digest: str,
    evaluation_digest: str,
    review_digest: str,
    *,
    action: str,
    expected_previous_event_digest: str | None,
) -> str:
    """Bind one activation authorization to its exact CAS head and review."""

    return digest(
        {
            "operation": "skill.activate-v2",
            "revision_digest": revision_digest,
            "evaluation_digest": evaluation_digest,
            "review_digest": review_digest,
            "action": action,
            "expected_previous_event_digest": expected_previous_event_digest,
        }
    )


def outcome_verification_effect_digest(
    invocation_digest: str,
    *,
    status: str,
    grader_kind: str,
    verifier_ref: str,
    evidence_digest: str,
) -> str:
    """Bind verifier evidence to one exact invocation outcome decision."""

    return digest(
        {
            "operation": "skill.outcome.verify-v2",
            "invocation_digest": invocation_digest,
            "status": status,
            "grader_kind": grader_kind,
            "verifier_ref": verifier_ref,
            "evidence_digest": evidence_digest,
        }
    )


def evaluation_evidence_effect_digest(
    evaluation: SkillEvaluationV2, *, role: str
) -> str:
    """Bind evaluator/verifier terminal evidence to one exact evaluation."""

    if role not in {"evaluator", "verifier"}:
        raise ValidationFailed("Skill evaluation evidence role invalid")
    return digest(
        {
            "operation": f"skill.evaluation.{role}-v2",
            "revision_digest": evaluation.revision_digest,
            "plan_digest": evaluation.plan_digest,
            "state": evaluation.state,
            "trials": evaluation.trials,
            "actor_ref": getattr(evaluation, f"{role}_ref"),
            "execution_identity": getattr(
                evaluation, f"{role}_execution_identity"
            ),
            "evidence_digest": getattr(evaluation, f"{role}_evidence_digest"),
            "metrics": evaluation.metrics,
            "uncertainty": evaluation.uncertainty,
        }
    )


def review_evidence_effect_digest(
    revision_digest: str,
    evaluation_digest: str,
    *,
    reviewer_ref: str,
    reviewer_execution_identity: str,
    reviewer_evidence_digest: str,
    approved: bool,
    reason: str = "",
) -> str:
    """Bind independent review evidence to one exact evaluation decision."""

    return digest(
        {
            "operation": "skill.review.verify-v2",
            "revision_digest": revision_digest,
            "evaluation_digest": evaluation_digest,
            "reviewer_ref": reviewer_ref,
            "reviewer_execution_identity": reviewer_execution_identity,
            "reviewer_evidence_digest": reviewer_evidence_digest,
            "approved": approved,
            "reason": reason,
        }
    )


def _time(value: dt.datetime) -> str:
    if type(value) is not dt.datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed("Personal skill timezone-aware timestamp required")
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()


def _text(value: str, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise ValidationFailed(f"Personal skill {label} invalid")
    return value


def _body(value: Mapping[str, object]) -> tuple[str, str]:
    raw = canonical_json(value)
    if len(raw.encode("utf-8")) > 32_768:
        raise ValidationFailed("Personal skill canonical body too large")
    return raw, digest(value)


def _evaluation_from_body(body: Mapping[str, object]) -> SkillEvaluationV2:
    """Rebuild and validate the exact persisted evaluation used by a review."""

    metrics = body.get("metrics")
    uncertainty = body.get("uncertainty")
    if not isinstance(metrics, dict) or not isinstance(uncertainty, dict):
        raise PolicyViolation("Skill evaluation stored metrics malformed")
    try:
        return SkillEvaluationV2(
            revision_digest=str(body["revision_digest"]),
            plan_digest=str(body["plan_digest"]),
            state=str(body["state"]),
            trials=int(cast(Any, body["trials"])),
            evaluator_ref=str(body["evaluator_ref"]),
            verifier_ref=str(body["verifier_ref"]),
            evaluator_evidence_digest=str(body["evaluator_evidence_digest"]),
            verifier_evidence_digest=str(body["verifier_evidence_digest"]),
            evaluator_execution_identity=str(body["evaluator_execution_identity"]),
            verifier_execution_identity=str(body["verifier_execution_identity"]),
            metrics=cast(dict[str, dict[str, float | str | int | bool]], metrics),
            uncertainty=cast(dict[str, float | int | str], uncertainty),
        )
    except (KeyError, TypeError, ValueError, ValidationFailed) as exc:
        raise PolicyViolation("Skill evaluation stored body malformed") from exc


class SQLiteSkillLifecycle:
    """Provider-free skill revision, scope, evaluation and invocation ledger."""

    def __init__(self, learning_path: Path, operational_path: Path) -> None:
        if (
            not learning_path.is_absolute()
            or not operational_path.is_absolute()
            or learning_path == operational_path
            or learning_path.is_symlink()
            or operational_path.is_symlink()
        ):
            raise ValidationFailed("Personal skill exact distinct SQLite paths required")
        self.learning_path = learning_path
        self.operational_path = operational_path

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if not private_regular(self.learning_path):
            raise PolicyViolation("Personal skill learning store identity invalid")
        mode = "ro" if read_only else "rw"
        db = sqlite3.connect(
            f"{self.learning_path.resolve().as_uri()}?mode={mode}", uri=True, timeout=5.0
        )
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        if read_only:
            db.execute("pragma query_only=on")
        row = db.execute(
            "select version,schema_digest from learning_schema where singleton=1"
        ).fetchone()
        if (
            row is None
            or tuple(row) != (SCHEMA_VERSION, SCHEMA_DIGEST)
            or _schema_digest(db) != SCHEMA_DIGEST
            or not _required_v2_indexes_present(db)
        ):
            db.close()
            raise PolicyViolation("Personal skill learning schema v2 required")
        return db

    def _terminal_evidence(self, origin: SkillOriginEvidence) -> None:
        self._completed_terminal_evidence(origin.run_ref, origin.evidence_digest)

    def _completed_terminal_evidence(
        self,
        run_ref: str,
        evidence_digest: str,
        *,
        operation: str | None = None,
        effect_digest: str | None = None,
    ) -> str:
        if not private_regular(self.operational_path):
            raise PolicyViolation("Personal skill operational evidence identity invalid")
        observed = operational_status(self.operational_path)
        if not (observed.integrity_ok and observed.schema_ok):
            raise PolicyViolation("Personal skill current operational evidence required")
        with closing(
            sqlite3.connect(
                f"{self.operational_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
            )
        ) as db:
            rows = db.execute(
                "select j.id,j.state,r.status,c.operation,c.effect_digest "
                "from local_job j join local_effect_claim c "
                "on c.job_id=j.id join local_effect_receipt r on r.claim_id=c.id "
                "where (j.id=? or j.idempotency_key=?) "
                "and j.terminal_evidence_digest=? "
                "and r.evidence_digest=?",
                (run_ref, run_ref, evidence_digest, evidence_digest),
            ).fetchall()
        if (
            len(rows) != 1
            or tuple(rows[0])[1:3] != ("completed", "completed")
            or (operation is not None and str(rows[0][3]) != operation)
            or (effect_digest is not None and str(rows[0][4]) != effect_digest)
        ):
            raise PolicyViolation("Personal skill exact completed terminal receipt required")
        return str(rows[0][0])

    def _canonical_project_scope(self, reference: str) -> str:
        _text(reference, "project scope ref")
        if not private_regular(self.operational_path):
            raise PolicyViolation("Personal skill project authority identity invalid")
        with closing(
            sqlite3.connect(
                f"{self.operational_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
            )
        ) as db:
            row = db.execute(
                "select distinct p.slug from project p left join project_alias a "
                "on a.project_id=p.id where p.status='active' "
                "and (p.id=? or p.slug=? or a.alias=?)",
                (reference, reference, reference),
            ).fetchall()
        if len(row) != 1:
            raise PolicyViolation("Personal skill project scope is not authorized/registered")
        return str(row[0][0])

    def canonical_project_scope(self, reference: str) -> str:
        """Expose canonical registry resolution without treating it as caller ACL."""

        return self._canonical_project_scope(reference)

    def _canonical_scopes(
        self, allowed_scopes: tuple[tuple[SkillScopeKind, str], ...]
    ) -> tuple[tuple[SkillScopeKind, str], ...]:
        if not allowed_scopes or len(allowed_scopes) > 32:
            raise ValidationFailed("Personal skill allowed scopes required")
        canonical: list[tuple[SkillScopeKind, str]] = []
        for kind, reference in allowed_scopes:
            if type(kind) is not SkillScopeKind:
                raise ValidationFailed("Personal skill exact scope kind required")
            if kind is not SkillScopeKind.PROJECT:
                raise PolicyViolation("Personal skill realm/user ACL adapter not configured")
            value = (kind, self._canonical_project_scope(reference))
            if value not in canonical:
                canonical.append(value)
        return tuple(canonical)

    def validate_revision_scope(self, revision: PersonalSkillRevision) -> str:
        """Resolve proposal scope before any operational work is admitted."""

        if type(revision) is not PersonalSkillRevision:
            raise ValidationFailed("Exact personal skill revision required")
        revision.__post_init__()
        if revision.scope_kind is not SkillScopeKind.PROJECT:
            raise PolicyViolation("Personal skill realm/user proposal adapter not configured")
        canonical = self._canonical_project_scope(revision.scope_ref)
        if canonical != revision.scope_ref:
            raise PolicyViolation("Personal skill revision scope must use canonical project slug")
        return canonical

    def require_active_package(
        self,
        package_digest: str,
        *,
        allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
    ) -> dict[str, str]:
        """Require one current active revision before any client distribution."""

        parse_digest(package_digest)
        canonical_scopes = self._canonical_scopes(allowed_scopes)
        clauses = " or ".join("(r.scope_kind=? and r.scope_ref=?)" for _ in canonical_scopes)
        parameters = [
            value for kind, reference in canonical_scopes for value in (str(kind), reference)
        ]
        with closing(self._connect(read_only=True)) as db:
            rows = db.execute(
                "select r.revision_digest,r.skill_id,r.scope_kind,r.scope_ref,e.event_digest,"
                "e.body_json as activation_body_json "
                "from skill_revision_v2 r join skill_activation_event_v2 e "
                "on e.revision_digest=r.revision_digest where r.package_digest=? and ("
                + clauses
                + ") and e.action='active' and not exists("
                "select 1 from skill_activation_event_v2 n "
                "where n.previous_event_digest=e.event_digest)",
                (package_digest, *parameters),
            ).fetchall()
        rows = [row for row in rows if self._activation_event_effective(row)]
        if len(rows) != 1:
            raise PolicyViolation(
                "Skill client distribution requires one active authorized package revision"
            )
        row = rows[0]
        return {
            "revision_digest": str(row["revision_digest"]),
            "skill_id": str(row["skill_id"]),
            "scope_kind": str(row["scope_kind"]),
            "scope_ref": str(row["scope_ref"]),
            "activation_event_digest": str(row["event_digest"]),
        }

    def _activation_authorization(
        self, job_id: str, effect_digest: str
    ) -> tuple[str, str | None]:
        _text(job_id, "activation authorization job")
        parse_digest(effect_digest)
        if not private_regular(self.operational_path):
            raise PolicyViolation("Personal skill activation authority identity invalid")
        observed = operational_status(self.operational_path)
        if not (observed.integrity_ok and observed.schema_ok):
            raise PolicyViolation("Personal skill current activation authority required")
        with closing(
            sqlite3.connect(
                f"{self.operational_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
            )
        ) as db:
            rows = db.execute(
                "select c.id,j.state,r.status,r.evidence_digest,j.terminal_evidence_digest "
                "from local_job j "
                "join local_effect_claim c on c.job_id=j.id "
                "left join local_effect_receipt r on r.claim_id=c.id "
                "where j.id=? and c.operation='skill.activate-v2' and c.effect_digest=?",
                (job_id, effect_digest),
            ).fetchall()
        if len(rows) != 1 or not (
            (str(rows[0][1]) == "running" and rows[0][2] is None)
            or (
                str(rows[0][1]) == "completed"
                and str(rows[0][2]) == "completed"
                and rows[0][3] == rows[0][4]
            )
        ):
            raise PolicyViolation("Skill activation exact operational authorization missing")
        return str(rows[0][0]), None if rows[0][3] is None else str(rows[0][3])

    def _activation_event_effective(self, row: Mapping[str, object]) -> bool:
        """An append-only activation becomes effective only after terminal receipt."""

        try:
            body = json.loads(str(row["activation_body_json"]))
            if (
                "event_digest" in row
                and digest(body) != str(row["event_digest"])
            ):
                return False
            if (
                "revision_digest" in row
                and str(body["revision_digest"]) != str(row["revision_digest"])
            ):
                return False
            if "action" in row and str(body["action"]) != str(row["action"]):
                return False
            effect = activation_effect_digest(
                str(body["revision_digest"]),
                str(body["evaluation_digest"]),
                str(body["review_digest"]),
                action=str(body["action"]),
                expected_previous_event_digest=body["previous_event_digest"],
            )
            claim_id, receipt_evidence = self._activation_authorization(
                str(body["authorization_job_id"]), effect
            )
        except (
            KeyError,
            TypeError,
            json.JSONDecodeError,
            PolicyViolation,
            ValidationFailed,
        ):
            return False
        return receipt_evidence is not None and claim_id == body.get(
            "authorization_claim_id"
        )

    def propose_revision(
        self,
        revision: PersonalSkillRevision,
        origins: tuple[SkillOriginEvidence, ...],
        *,
        explicit_request_evidence_digest: str | None = None,
        now: dt.datetime,
    ) -> str:
        self.validate_revision_scope(revision)
        origin_digests = {item.evidence_digest for item in origins}
        if not origins or len(origins) > 16 or len(origin_digests) != len(origins):
            raise ValidationFailed("Skill revision needs bounded independent origins")
        if explicit_request_evidence_digest is not None:
            parse_digest(explicit_request_evidence_digest)
            requests = tuple(
                item
                for item in origins
                if item.kind is SkillOriginKind.USER_REQUEST
                and item.evidence_digest == explicit_request_evidence_digest
            )
            if len(requests) != 1:
                raise PolicyViolation("Explicit skill request needs one exact user_request origin")
        elif any(item.kind is SkillOriginKind.USER_REQUEST for item in origins):
            raise PolicyViolation("User request origin needs exact request evidence binding")
        if len(origins) < 2 and explicit_request_evidence_digest is None:
            raise PolicyViolation("Skill candidacy needs two observations or explicit request")
        for origin in origins:
            if type(origin) is not SkillOriginEvidence:
                raise ValidationFailed("Exact personal skill origin required")
            origin.__post_init__()
            if origin.kind in {
                SkillOriginKind.VERIFIED_SUCCESS,
                SkillOriginKind.USER_CORRECTION,
            }:
                self._terminal_evidence(origin)
        revision_body = revision.body() | {
            "origin_count": len(origins),
            "explicit_request_evidence_digest": explicit_request_evidence_digest,
        }
        raw, revision_digest = _body(revision_body)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            existing = db.execute(
                "select revision_digest,body_json from skill_revision_v2 "
                "where skill_id=? and version=? and scope_kind=? and scope_ref=?",
                (
                    revision.skill_id,
                    revision.version,
                    str(revision.scope_kind),
                    revision.scope_ref,
                ),
            ).fetchone()
            if existing is not None:
                try:
                    existing_body = json.loads(str(existing["body_json"]))
                except json.JSONDecodeError as exc:
                    raise PolicyViolation("Skill revision stored body malformed") from exc
                if isinstance(existing_body, dict):
                    existing_body.pop("created_at", None)
                if canonical_json(existing_body) != raw:
                    raise PolicyViolation("Skill revision identity replay drift")
                expected_origins = sorted(
                    canonical_json(origin.as_dict()) for origin in origins
                )
                stored_origins = sorted(
                    canonical_json(
                        {
                            "kind": str(item["origin_kind"]),
                            "evidence_digest": str(item["evidence_digest"]),
                            "work_ref": str(item["work_ref"]),
                            "run_ref": str(item["run_ref"]),
                            "artifact_revision_digest": item[
                                "artifact_revision_digest"
                            ],
                            "user_ref": item["user_ref"],
                            "grants_authority": False,
                        }
                    )
                    for item in db.execute(
                        "select origin_kind,evidence_digest,work_ref,run_ref,"
                        "artifact_revision_digest,user_ref from skill_origin_v2 "
                        "where revision_digest=?",
                        (str(existing["revision_digest"]),),
                    ).fetchall()
                )
                if stored_origins != expected_origins:
                    raise PolicyViolation("Skill revision origin replay drift")
                db.rollback()
                return str(existing["revision_digest"])
            db.execute(
                "insert into skill_revision_v2 values(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_digest,
                    revision.skill_id,
                    revision.name,
                    revision.version,
                    str(revision.scope_kind),
                    revision.scope_ref,
                    revision.package_digest,
                    revision.author_ref,
                    "candidate",
                    _time(now),
                    raw,
                ),
            )
            for origin in origins:
                if origin.kind is SkillOriginKind.FAILURE_LESSON:
                    lesson = db.execute(
                        "select 1 from lesson where lesson_digest=?", (origin.evidence_digest,)
                    ).fetchone()
                    if lesson is None:
                        raise PolicyViolation("Failure origin requires existing lesson")
                origin_body = {
                    "schema": "zekam-personal-skill-origin/v2",
                    "revision_digest": revision_digest,
                    **origin.as_dict(),
                    "observed_at": _time(now),
                }
                origin_raw, origin_digest = _body(origin_body)
                db.execute(
                    "insert into skill_origin_v2 values(?,?,?,?,?,?,?,?,?,?)",
                    (
                        origin_digest,
                        revision_digest,
                        str(origin.kind),
                        origin.evidence_digest,
                        origin.work_ref,
                        origin.run_ref,
                        origin.artifact_revision_digest,
                        origin.user_ref,
                        _time(now),
                        origin_raw,
                    ),
                )
            db.commit()
        return revision_digest

    def record_evaluation(self, evaluation: SkillEvaluationV2, *, now: dt.datetime) -> str:
        if type(evaluation) is not SkillEvaluationV2:
            raise ValidationFailed("Exact skill evaluation v2 required")
        evaluation.__post_init__()
        operational_jobs: list[str] = []
        for role in ("evaluator", "verifier"):
            execution_identity = str(
                getattr(evaluation, f"{role}_execution_identity")
            )
            evidence_digest = str(getattr(evaluation, f"{role}_evidence_digest"))
            operational_jobs.append(
                self._completed_terminal_evidence(
                    execution_identity,
                    evidence_digest,
                    operation=f"skill.evaluation.{role}-v2",
                    effect_digest=evaluation_evidence_effect_digest(
                        evaluation, role=role
                    ),
                )
            )
        if len(set(operational_jobs)) != 2:
            raise PolicyViolation("Skill evaluation independent operational jobs required")
        body = evaluation.body() | {"created_at": _time(now)}
        raw, value = _body(body)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            exists = db.execute(
                "select 1 from skill_revision_v2 where revision_digest=?",
                (evaluation.revision_digest,),
            ).fetchone()
            if exists is None:
                raise PolicyViolation("Skill evaluation needs persisted revision")
            replay = db.execute(
                "select evaluation_digest,body_json from skill_evaluation_v2 "
                "where revision_digest=? and plan_digest=?",
                (evaluation.revision_digest, evaluation.plan_digest),
            ).fetchall()
            if len(replay) > 1:
                raise PolicyViolation("Skill evaluation replay identity ambiguous")
            if replay:
                try:
                    stored = json.loads(str(replay[0]["body_json"]))
                except json.JSONDecodeError as exc:
                    raise PolicyViolation("Skill evaluation stored body malformed") from exc
                if isinstance(stored, dict):
                    stored.pop("created_at", None)
                if canonical_json(stored) != canonical_json(evaluation.body()):
                    raise PolicyViolation("Skill evaluation exact replay drift")
                db.rollback()
                return str(replay[0]["evaluation_digest"])
            db.execute(
                "insert into skill_evaluation_v2 values(?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    evaluation.revision_digest,
                    evaluation.plan_digest,
                    evaluation.state,
                    evaluation.trials,
                    evaluation.evaluator_ref,
                    evaluation.verifier_ref,
                    _time(now),
                    raw,
                ),
            )
            db.commit()
        return value

    def record_review(
        self,
        revision_digest: str,
        evaluation_digest: str,
        *,
        reviewer_ref: str,
        reviewer_evidence_digest: str,
        reviewer_execution_identity: str,
        approved: bool,
        reason: str,
        now: dt.datetime,
    ) -> str:
        parse_digest(revision_digest)
        parse_digest(evaluation_digest)
        _text(reviewer_ref, "reviewer")
        parse_digest(reviewer_evidence_digest)
        _text(reviewer_execution_identity, "reviewer execution identity")
        _text(reason, "review reason", 2048)
        if type(approved) is not bool:
            raise ValidationFailed("Skill review approval must be bool")
        reviewer_job_id = self._completed_terminal_evidence(
            reviewer_execution_identity,
            reviewer_evidence_digest,
            operation="skill.review.verify-v2",
            effect_digest=review_evidence_effect_digest(
                revision_digest,
                evaluation_digest,
                reviewer_ref=reviewer_ref,
                reviewer_execution_identity=reviewer_execution_identity,
                reviewer_evidence_digest=reviewer_evidence_digest,
                approved=approved,
                reason=reason,
            ),
        )
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            row = db.execute(
                "select r.author_ref,e.evaluator_ref,e.verifier_ref,e.body_json "
                "from skill_revision_v2 r "
                "join skill_evaluation_v2 e on e.revision_digest=r.revision_digest "
                "where r.revision_digest=? and e.evaluation_digest=?",
                (revision_digest, evaluation_digest),
            ).fetchone()
            if row is None or reviewer_ref in tuple(row)[:3]:
                raise PolicyViolation("Skill v2 review must be independent")
            try:
                evaluation_body = json.loads(str(row["body_json"]))
            except json.JSONDecodeError as exc:
                raise PolicyViolation("Skill evaluation stored body malformed") from exc
            if (
                reviewer_evidence_digest
                in {
                    evaluation_body.get("evaluator_evidence_digest"),
                    evaluation_body.get("verifier_evidence_digest"),
                }
                or reviewer_execution_identity
                in {
                    evaluation_body.get("evaluator_execution_identity"),
                    evaluation_body.get("verifier_execution_identity"),
                }
            ):
                raise PolicyViolation("Skill v2 review execution evidence must be independent")
            persisted_evaluation = _evaluation_from_body(evaluation_body)
            evaluator_job_id = self._completed_terminal_evidence(
                str(evaluation_body["evaluator_execution_identity"]),
                str(evaluation_body["evaluator_evidence_digest"]),
                operation="skill.evaluation.evaluator-v2",
                effect_digest=evaluation_evidence_effect_digest(
                    persisted_evaluation, role="evaluator"
                ),
            )
            verifier_job_id = self._completed_terminal_evidence(
                str(evaluation_body["verifier_execution_identity"]),
                str(evaluation_body["verifier_evidence_digest"]),
                operation="skill.evaluation.verifier-v2",
                effect_digest=evaluation_evidence_effect_digest(
                    persisted_evaluation, role="verifier"
                ),
            )
            if reviewer_job_id in {evaluator_job_id, verifier_job_id}:
                raise PolicyViolation("Skill v2 review operational job must be independent")
            body = {
                "schema": "zekam-personal-skill-review/v2",
                "revision_digest": revision_digest,
                "evaluation_digest": evaluation_digest,
                "reviewer_ref": reviewer_ref,
                "reviewer_evidence_digest": reviewer_evidence_digest,
                "reviewer_execution_identity": reviewer_execution_identity,
                "approved": approved,
                "reason": reason,
                "created_at": _time(now),
                "grants_authority": False,
            }
            raw, value = _body(body)
            replay = db.execute(
                "select review_digest,body_json from skill_review_v2 "
                "where revision_digest=? and evaluation_digest=? and reviewer_ref=?",
                (revision_digest, evaluation_digest, reviewer_ref),
            ).fetchall()
            if len(replay) > 1:
                raise PolicyViolation("Skill review replay identity ambiguous")
            if replay:
                try:
                    stored = json.loads(str(replay[0]["body_json"]))
                except json.JSONDecodeError as exc:
                    raise PolicyViolation("Skill review stored body malformed") from exc
                stored.pop("created_at", None)
                expected = dict(body)
                expected.pop("created_at", None)
                if canonical_json(stored) != canonical_json(expected):
                    raise PolicyViolation("Skill review exact replay drift")
                db.rollback()
                return str(replay[0]["review_digest"])
            db.execute(
                "insert into skill_review_v2 values(?,?,?,?,?,?,?,?)",
                (
                    value,
                    revision_digest,
                    evaluation_digest,
                    reviewer_ref,
                    int(approved),
                    reason,
                    _time(now),
                    raw,
                ),
            )
            db.commit()
        return value

    def append_activation(
        self,
        revision_digest: str,
        *,
        action: str,
        expected_previous_event_digest: str | None,
        authorization_job_id: str,
        now: dt.datetime,
    ) -> str:
        parse_digest(revision_digest)
        if expected_previous_event_digest is not None:
            parse_digest(expected_previous_event_digest)
        if action not in {"active", "deprecated", "revoked", "retired", "superseded"}:
            raise ValidationFailed("Skill activation action invalid")
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            revision = db.execute(
                "select skill_id,scope_kind,scope_ref from skill_revision_v2 "
                "where revision_digest=?",
                (revision_digest,),
            ).fetchone()
            if revision is None:
                raise PolicyViolation("Skill activation revision missing")
            replay_rows = db.execute(
                "select event_digest,body_json from skill_activation_event_v2 "
                "where revision_digest=?",
                (revision_digest,),
            ).fetchall()
            for replay in replay_rows:
                try:
                    replay_body = json.loads(str(replay["body_json"]))
                    replay_effect = activation_effect_digest(
                        revision_digest,
                        str(replay_body["evaluation_digest"]),
                        str(replay_body["review_digest"]),
                        action=str(replay_body["action"]),
                        expected_previous_event_digest=replay_body[
                            "previous_event_digest"
                        ],
                    )
                except (KeyError, TypeError, json.JSONDecodeError, ValidationFailed) as exc:
                    raise PolicyViolation("Skill activation replay evidence malformed") from exc
                if replay_body.get("authorization_job_id") != authorization_job_id:
                    continue
                if (
                    replay_body.get("action") != action
                    or replay_body.get("previous_event_digest")
                    != expected_previous_event_digest
                ):
                    raise ConcurrencyConflict("Skill activation authorization replay drift")
                self._activation_authorization(authorization_job_id, replay_effect)
                db.rollback()
                return str(replay["event_digest"])
            head = db.execute(
                "select e.event_digest,e.action,e.revision_digest,e.body_json "
                "from skill_activation_event_v2 e "
                "where e.skill_id=? and e.scope_kind=? and e.scope_ref=? and not exists("
                "select 1 from skill_activation_event_v2 n "
                "where n.previous_event_digest=e.event_digest)",
                tuple(revision),
            ).fetchone()
            observed_previous = None if head is None else str(head["event_digest"])
            if observed_previous != expected_previous_event_digest:
                raise ConcurrencyConflict("Skill activation CAS drift")
            if action == "active":
                evaluation = db.execute(
                    "select e.evaluation_digest,r.review_digest from skill_evaluation_v2 e "
                    "join skill_review_v2 r on r.evaluation_digest=e.evaluation_digest "
                    "where e.revision_digest=? and e.state='improved' and e.trials>=5 "
                    "and r.approved=1 order by r.created_at desc limit 1",
                    (revision_digest,),
                ).fetchone()
                if evaluation is None:
                    raise PolicyViolation("Skill activation needs passing v2 evaluation and review")
            else:
                if (
                    head is None
                    or str(head["action"]) != "active"
                    or str(head["revision_digest"]) != revision_digest
                ):
                    raise PolicyViolation("Skill lifecycle action needs current active revision")
                try:
                    head_body = json.loads(str(head["body_json"]))
                    evaluation_digest = str(head_body["evaluation_digest"])
                    review_digest = str(head_body["review_digest"])
                    parse_digest(evaluation_digest)
                    parse_digest(review_digest)
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                    ValidationFailed,
                ) as exc:
                    raise PolicyViolation("Skill active head evidence binding malformed") from exc
                reviewed = db.execute(
                    "select 1 from skill_evaluation_v2 e join skill_review_v2 r "
                    "on r.evaluation_digest=e.evaluation_digest "
                    "where e.evaluation_digest=? and r.review_digest=? "
                    "and e.revision_digest=? and r.approved=1",
                    (evaluation_digest, review_digest, revision_digest),
                ).fetchone()
                if reviewed is None:
                    raise PolicyViolation("Skill lifecycle active review binding missing")
                evaluation = {
                    "evaluation_digest": evaluation_digest,
                    "review_digest": review_digest,
                }
            effect = activation_effect_digest(
                revision_digest,
                str(evaluation["evaluation_digest"]),
                str(evaluation["review_digest"]),
                action=action,
                expected_previous_event_digest=observed_previous,
            )
            claim_id, receipt_evidence = self._activation_authorization(
                authorization_job_id, effect
            )
            body = {
                "schema": "zekam-personal-skill-activation-event/v2",
                "revision_digest": revision_digest,
                "skill_id": str(revision["skill_id"]),
                "scope": {"kind": str(revision["scope_kind"]), "ref": str(revision["scope_ref"])},
                "action": action,
                "previous_event_digest": observed_previous,
                "evaluation_digest": str(evaluation["evaluation_digest"]),
                "review_digest": str(evaluation["review_digest"]),
                "authorization_job_id": authorization_job_id,
                "authorization_claim_id": claim_id,
                "authorization_receipt_evidence_digest": receipt_evidence,
                "created_at": _time(now),
                "grants_authority": False,
            }
            raw, value = _body(body)
            db.execute(
                "insert into skill_activation_event_v2 values(?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    revision_digest,
                    str(revision["skill_id"]),
                    str(revision["scope_kind"]),
                    str(revision["scope_ref"]),
                    action,
                    observed_previous,
                    _time(now),
                    raw,
                ),
            )
            db.commit()
        return value

    def discover(
        self,
        query: str,
        *,
        allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
        maximum: int = 20,
        after: str | None = None,
    ) -> dict[str, object]:
        _text(query, "discovery query", 4096)
        if type(maximum) is not int or not 1 <= maximum <= 50:
            raise ValidationFailed("Skill discovery maximum must be 1..50")
        clauses: list[str] = []
        parameters: list[object] = []
        for kind, reference in self._canonical_scopes(allowed_scopes):
            clauses.append("(r.scope_kind=? and r.scope_ref=?)")
            parameters.extend((str(kind), reference))
        if after is not None:
            parse_digest(after)
        sql = (
            "select r.revision_digest,r.skill_id,r.name,r.version,r.scope_kind,r.scope_ref,"
            "r.package_digest,r.body_json,e.event_digest,"
            "e.body_json as activation_body_json from skill_revision_v2 r "
            "join skill_activation_event_v2 e on e.revision_digest=r.revision_digest "
            "where (" + " or ".join(clauses) + ") and e.action='active' and not exists("
            "select 1 from skill_activation_event_v2 n "
            "where n.previous_event_digest=e.event_digest) "
        )
        sql += "order by r.revision_digest limit 501"
        with closing(self._connect(read_only=True)) as db:
            rows = db.execute(sql, tuple(parameters)).fetchall()
        if len(rows) > 500:
            raise PolicyViolation("Skill discovery filtered scan bound exceeded")
        query_terms = {item.casefold() for item in _WORD.findall(query)}
        ranked: list[tuple[int, str, dict[str, object]]] = []
        for row in rows:
            if not self._activation_event_effective(row):
                continue
            try:
                body = json.loads(str(row["body_json"]))
            except json.JSONDecodeError as exc:
                raise PolicyViolation("Skill revision body malformed") from exc
            trigger_terms = {
                token.casefold()
                for item in body.get("trigger_terms", [])
                if isinstance(item, str)
                for token in _WORD.findall(item)
            }
            negative = {
                token.casefold()
                for item in body.get("non_trigger_terms", [])
                if isinstance(item, str)
                for token in _WORD.findall(item)
            }
            score = len(query_terms & trigger_terms) * 10 - len(query_terms & negative) * 20
            if score <= 0:
                continue
            item = {
                "revision_digest": str(row["revision_digest"]),
                "skill_id": str(row["skill_id"]),
                "name": str(row["name"]),
                "description": str(body.get("description")),
                "version": int(row["version"]),
                "scope": {"kind": str(row["scope_kind"]), "ref": str(row["scope_ref"])},
                "package_digest": str(row["package_digest"]),
                "activation_event_digest": str(row["event_digest"]),
                "selection_score": score,
                "selection_reason": "lexical-trigger-match",
                "metadata_bytes": len(str(row["name"]).encode("utf-8"))
                + len(str(body.get("description")).encode("utf-8")),
                "body_loaded": False,
            }
            ranked.append((-score, str(row["revision_digest"]), item))
        ranked.sort(key=lambda item: (item[0], item[1]))
        start = 0
        if after is not None:
            cursor_positions = [index for index, item in enumerate(ranked) if item[1] == after]
            if len(cursor_positions) != 1:
                raise PolicyViolation("Skill discovery pagination cursor is not visible")
            start = cursor_positions[0] + 1
        selected = [item[2] for item in ranked[start : start + maximum]]
        return {
            "schema": "zekam-skill-discovery/v2",
            "items": selected,
            "has_more": len(ranked) > start + maximum,
            "next_after": None if not selected else selected[-1]["revision_digest"],
            "abstained": not selected,
            "cost_proxy": "utf8-bytes-not-tokens",
            "grants_authority": False,
        }

    def catalog(
        self,
        *,
        allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
        maximum: int = 20,
        after: str | None = None,
    ) -> dict[str, object]:
        if type(maximum) is not int or not 1 <= maximum <= 50:
            raise ValidationFailed("Skill catalog maximum must be 1..50")
        clauses: list[str] = []
        parameters: list[object] = []
        for kind, reference in self._canonical_scopes(allowed_scopes):
            clauses.append("(r.scope_kind=? and r.scope_ref=?)")
            parameters.extend((str(kind), reference))
        if after is not None:
            parse_digest(after)
        sql = (
            "select r.revision_digest,r.skill_id,r.name,r.version,r.scope_kind,r.scope_ref,"
            "r.package_digest,r.body_json,e.event_digest,"
            "e.body_json as activation_body_json from skill_revision_v2 r "
            "join skill_activation_event_v2 e on e.revision_digest=r.revision_digest "
            "where (" + " or ".join(clauses) + ") and e.action='active' and not exists("
            "select 1 from skill_activation_event_v2 n "
            "where n.previous_event_digest=e.event_digest) "
        )
        if after is not None:
            sql += "and r.revision_digest>? "
            parameters.append(after)
        sql += "order by r.revision_digest limit 501"
        with closing(self._connect(read_only=True)) as db:
            rows = db.execute(sql, tuple(parameters)).fetchall()
        if len(rows) > 500:
            raise PolicyViolation("Skill catalog filtered scan bound exceeded")
        rows = [row for row in rows if self._activation_event_effective(row)]
        items: list[dict[str, object]] = []
        for row in rows[:maximum]:
            try:
                body = json.loads(str(row["body_json"]))
            except json.JSONDecodeError as exc:
                raise PolicyViolation("Skill revision body malformed") from exc
            items.append(
                {
                    "revision_digest": str(row["revision_digest"]),
                    "skill_id": str(row["skill_id"]),
                    "name": str(row["name"]),
                    "description": str(body.get("description")),
                    "version": int(row["version"]),
                    "scope": {"kind": str(row["scope_kind"]), "ref": str(row["scope_ref"])},
                    "package_digest": str(row["package_digest"]),
                    "activation_event_digest": str(row["event_digest"]),
                    "body_loaded": False,
                }
            )
        return {
            "schema": "zekam-skill-catalog/v2",
            "items": items,
            "has_more": len(rows) > maximum,
            "next_after": None if not items else items[-1]["revision_digest"],
            "grants_authority": False,
        }

    def inspect_revision(
        self,
        revision_digest: str,
        *,
        allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
    ) -> dict[str, object]:
        parse_digest(revision_digest)
        clauses: list[str] = []
        parameters: list[object] = [revision_digest]
        for kind, reference in self._canonical_scopes(allowed_scopes):
            clauses.append("(r.scope_kind=? and r.scope_ref=?)")
            parameters.extend((str(kind), reference))
        with closing(self._connect(read_only=True)) as db:
            revision = db.execute(
                "select r.body_json,e.body_json as activation_body_json,"
                "e.event_digest,e.revision_digest,e.action "
                "from skill_revision_v2 r "
                "join skill_activation_event_v2 e on e.revision_digest=r.revision_digest "
                "where r.revision_digest=? and ("
                + " or ".join(clauses)
                + ") and e.action='active' and not exists("
                "select 1 from skill_activation_event_v2 n "
                "where n.previous_event_digest=e.event_digest)",
                tuple(parameters),
            ).fetchone()
            if revision is None or not self._activation_event_effective(revision):
                raise PolicyViolation("Skill revision is not visible in allowed scope")
            origins = db.execute(
                "select origin_kind,evidence_digest,work_ref,run_ref,artifact_revision_digest,"
                "user_ref,observed_at from skill_origin_v2 where revision_digest=? "
                "order by origin_digest",
                (revision_digest,),
            ).fetchall()
        return {
            "schema": "zekam-skill-inspect/v2",
            "revision": json.loads(str(revision[0])),
            "origins": [dict(row) for row in origins],
            "package_body_loaded": False,
            "grants_authority": False,
        }

    def record_invocation_state(
        self,
        revision_digest: str,
        *,
        package_digest: str,
        work_ref: str,
        run_ref: str,
        agent_ref: str,
        client_id: str,
        harness_digest: str,
        state: SkillInvocationState,
        previous_invocation_digest: str | None,
        allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
        now: dt.datetime,
    ) -> str:
        for value in (revision_digest, package_digest, harness_digest):
            parse_digest(value)
        for label, value in (
            ("work ref", work_ref),
            ("run ref", run_ref),
            ("agent ref", agent_ref),
            ("client id", client_id),
        ):
            _text(value, label)
        previous_state: SkillInvocationState | None = None
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            canonical_scopes = self._canonical_scopes(allowed_scopes)
            clauses = " or ".join("(r.scope_kind=? and r.scope_ref=?)" for _ in canonical_scopes)
            scope_parameters = [
                value for kind, reference in canonical_scopes for value in (str(kind), reference)
            ]
            revision = db.execute(
                "select r.package_digest,e.body_json as activation_body_json,"
                "e.event_digest,e.revision_digest,e.action "
                "from skill_revision_v2 r "
                "join skill_activation_event_v2 e on e.revision_digest=r.revision_digest "
                "where r.revision_digest=? and ("
                + clauses
                + ") and e.action='active' and not exists("
                "select 1 from skill_activation_event_v2 n "
                "where n.previous_event_digest=e.event_digest)",
                (revision_digest, *scope_parameters),
            ).fetchone()
            if (
                revision is None
                or str(revision[0]) != package_digest
                or not self._activation_event_effective(revision)
            ):
                raise PolicyViolation("Skill invocation exact revision/package binding required")
            if previous_invocation_digest is not None:
                parse_digest(previous_invocation_digest)
                previous = db.execute(
                    "select state,revision_digest,package_digest,work_ref,run_ref,agent_ref,"
                    "client_id,harness_digest "
                    "from skill_invocation_v2 where invocation_digest=?",
                    (previous_invocation_digest,),
                ).fetchone()
                if previous is None or tuple(previous)[1:] != (
                    revision_digest,
                    package_digest,
                    work_ref,
                    run_ref,
                    agent_ref,
                    client_id,
                    harness_digest,
                ):
                    raise ConcurrencyConflict("Skill invocation predecessor drift")
                previous_state = SkillInvocationState(str(previous["state"]))
            assert_invocation_transition(previous_state, state)
            body = {
                "schema": "zekam-skill-invocation/v2",
                "revision_digest": revision_digest,
                "package_digest": package_digest,
                "work_ref": work_ref,
                "run_ref": run_ref,
                "agent_ref": agent_ref,
                "client_id": client_id,
                "harness_digest": harness_digest,
                "state": str(state),
                "previous_invocation_digest": previous_invocation_digest,
                "observed_at": _time(now),
                "grants_authority": False,
            }
            raw, value = _body(body)
            db.execute(
                "insert into skill_invocation_v2 values(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value,
                    revision_digest,
                    package_digest,
                    work_ref,
                    run_ref,
                    agent_ref,
                    client_id,
                    harness_digest,
                    str(state),
                    previous_invocation_digest,
                    _time(now),
                    raw,
                ),
            )
            db.commit()
        return value

    def record_outcome(
        self,
        invocation_digest: str,
        *,
        status: str,
        grader_kind: str,
        verifier_ref: str,
        evidence_digest: str,
        now: dt.datetime,
    ) -> str:
        parse_digest(invocation_digest)
        parse_digest(evidence_digest)
        _text(verifier_ref, "outcome verifier")
        if status not in {"verified-success", "verified-failure", "unverified"}:
            raise ValidationFailed("Skill outcome status invalid")
        graders = {"artifact", "process", "preference", "efficiency", "safety", "human"}
        if grader_kind not in graders:
            raise ValidationFailed("Skill outcome grader invalid")
        with closing(self._connect()) as db:
            row = db.execute(
                "select state,run_ref,agent_ref from skill_invocation_v2 "
                "where invocation_digest=?",
                (invocation_digest,),
            ).fetchone()
            if row is None or str(row[0]) not in {"completed", "unverified"}:
                raise PolicyViolation("Skill outcome requires completed invocation")
            if verifier_ref == str(row["agent_ref"]):
                raise PolicyViolation(
                    "Skill outcome verifier must be independent from invoking agent"
                )
            verification_effect = outcome_verification_effect_digest(
                invocation_digest,
                status=status,
                grader_kind=grader_kind,
                verifier_ref=verifier_ref,
                evidence_digest=evidence_digest,
            )
            self._completed_terminal_evidence(
                str(row["run_ref"]),
                evidence_digest,
                operation="skill.outcome.verify-v2",
                effect_digest=verification_effect,
            )
            body = {
                "schema": "zekam-skill-outcome/v2",
                "invocation_digest": invocation_digest,
                "status": status,
                "grader_kind": grader_kind,
                "verifier_ref": verifier_ref,
                "evidence_digest": evidence_digest,
                "observed_at": _time(now),
                "grants_authority": False,
            }
            raw, value = _body(body)
            db.execute(
                "insert into skill_outcome_v2 values(?,?,?,?,?,?,?,?)",
                (
                    value,
                    invocation_digest,
                    status,
                    grader_kind,
                    verifier_ref,
                    evidence_digest,
                    _time(now),
                    raw,
                ),
            )
            db.commit()
        return value

    def status(self) -> dict[str, object]:
        with closing(self._connect(read_only=True)) as db:
            counts = {
                table: int(db.execute(f"select count(*) from {table}").fetchone()[0])
                for table in (
                    "skill_revision_v2",
                    "skill_origin_v2",
                    "skill_evaluation_v2",
                    "skill_review_v2",
                    "skill_activation_event_v2",
                    "skill_invocation_v2",
                    "skill_outcome_v2",
                )
            }
            active_rows = db.execute(
                    "select e.body_json as activation_body_json,e.event_digest,"
                    "e.revision_digest,e.action "
                    "from skill_activation_event_v2 e where e.action='active' "
                    "and not exists(select 1 from skill_activation_event_v2 n "
                    "where n.previous_event_digest=e.event_digest)"
                ).fetchall()
            active = sum(
                1 for row in active_rows if self._activation_event_effective(row)
            )
            legacy_unbound = int(
                db.execute("select count(*) from skill_activation").fetchone()[0]
            )
        return {
            "schema": "zekam-personal-skill-status/v2",
            "counts": counts,
            "active_revisions": active,
            "legacy_unbound_activations": legacy_unbound,
            "skill_catalog": "ready",
            "skill_learning": "ready",
            "client_distribution": "not_configured",
            "verified_execution": "not_run",
            "native_acceptance": "not_run",
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        }


def classify_learning_candidate(kind: str, *, reusable_steps: bool, software_defect: bool) -> str:
    """Keep facts, preferences and code defects out of the skill catalog."""

    if software_defect:
        return "code-fix"
    if reusable_steps:
        return "skill-candidate"
    if kind == "preference":
        return "preference"
    return "knowledge"
