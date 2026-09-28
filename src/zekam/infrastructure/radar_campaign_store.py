"""SQLite repository and additive schema for bounded engineering radar campaigns.

The campaign store lives in a separate SQLite file under ``ZEKAM_HOME/state``
so that adding radar tables never changes the canonical operational schema
fingerprint.  It tracks its own schema migrations in ``radar_schema_migrations``.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConfigurationError, NotFound, PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7
from zekam.domain.radar_candidate import (
    RadarCandidateSelection,
    RadarGapCard,
    RadarPatternCard,
)

RADAR_SCHEMA_VERSION = 4

_RADAR_DDL: str = r"""
create table if not exists radar_schema_migrations (
    version integer primary key,
    name text not null,
    migration_digest text not null,
    applied_at text not null
) strict;

create table if not exists radar_campaign (
    id text primary key,
    project_id text not null,
    campaign_id text not null unique,
    stage text not null check(stage in ('discover','analyse')),
    state text not null check(state in (
        'ready','running','completed','failed','blocked','recovery-required'
    )),
    plan_digest text not null,
    plan_json text not null,
    idempotency_key text not null unique,
    effect_claim_id text,
    invocation_attempts integer not null default 0 check(invocation_attempts >= 0),
    planned_invocations integer not null default 0 check(planned_invocations >= 0),
    started_invocations integer not null default 0 check(started_invocations >= 0),
    completed_invocations integer not null default 0 check(completed_invocations >= 0),
    observed_provider_requests integer not null default 0 check(observed_provider_requests >= 0),
    created_at text not null,
    updated_at text not null
) strict;

create table if not exists radar_stage (
    id text primary key,
    campaign_id text not null references radar_campaign(id),
    stage text not null check(stage in ('discover','analyse')),
    state text not null check(state in (
        'ready','running','completed','failed','blocked','recovery-required'
    )),
    started_at text,
    finished_at text,
    evidence_digest text,
    next_safe_action text not null
) strict;

create table if not exists radar_inventory (
    id text primary key,
    campaign_id text not null references radar_campaign(id),
    owner text not null,
    inventory_digest text not null unique,
    inventory_json text not null,
    state text not null check(state in ('complete','partial')),
    observed_at text not null
) strict;

create table if not exists radar_pinned_commit (
    id text primary key,
    campaign_id text not null references radar_campaign(id),
    repository_id integer not null,
    owner text not null,
    name text not null,
    branch text not null,
    commit_sha text not null,
    pinned_at text not null
) strict;

create table if not exists radar_source_manifest (
    id text primary key,
    campaign_id text not null references radar_campaign(id),
    pin_id text not null references radar_pinned_commit(id),
    path text not null,
    blob_sha text,
    raw_content_digest text,
    complete integer not null check(complete in (0,1)),
    omission_reason text,
    fetched_at text not null
) strict;

create table if not exists radar_sub_research_run (
    id text primary key,
    campaign_id text not null references radar_campaign(id),
    question_digest text not null,
    question_json text not null,
    state text not null check(state in ('pending','running','completed','failed','blocked')),
    result_json text,
    result_digest text,
    started_at text,
    completed_at text
) strict;

create table if not exists radar_budget_reservation (
    id text primary key,
    campaign_id text not null references radar_campaign(id),
    category text not null check(category in ('requests','bytes','calls','tokens')),
    reserved integer not null check(reserved >= 0),
    consumed integer not null default 0 check(consumed >= 0),
    created_at text not null
) strict;

create table if not exists radar_coverage_gap (
    id text primary key,
    campaign_id text not null references radar_campaign(id),
    gap_key text not null,
    reason text not null,
    created_at text not null,
    resolved_at text
) strict;

create table if not exists radar_checkpoint (
    id text primary key,
    campaign_id text not null references radar_campaign(id),
    sequence integer not null check(sequence > 0),
    checkpoint_digest text not null,
    checkpoint_json text not null,
    created_at text not null
) strict;

create table if not exists radar_pattern_card (
    id text primary key,
    campaign_id text not null references radar_campaign(id),
    card_id text not null,
    card_digest text not null,
    kind text not null check(kind in ('pattern')),
    problem text not null,
    source_json text not null,
    observed_behavior text not null,
    inference_or_assumption text not null,
    test_evidence_level text not null,
    license_reuse_constraint text not null,
    cost_dependency_limit text not null,
    not_applicable_conditions text not null,
    created_at text not null,
    unique(campaign_id, card_id)
) strict;

create table if not exists radar_gap_card (
    id text primary key,
    campaign_id text not null references radar_campaign(id),
    card_id text not null,
    card_digest text not null,
    kind text not null check(kind in ('gap')),
    current_baseline text not null,
    source_binding text not null,
    production_call_path text not null,
    test_measurement_evidence text not null,
    covered_areas text not null,
    uncovered_areas text not null,
    result text not null check(result in (
        'present','partial','gap-demonstrated','different-fit',
        'not-applicable','unknown'
    )),
    created_at text not null,
    unique(campaign_id, card_id)
) strict;

create table if not exists radar_candidate_decision (
    id text primary key,
    campaign_id text not null references radar_campaign(id),
    selection_id text not null,
    selection_digest text not null,
    card_id text not null,
    decision text not null check(decision in (
        'present','partial','gap-demonstrated','different-fit','not-applicable',
        'unknown','already-satisfied','duplicate','evidence-insufficient',
        'deferred-dependency','rejected-risk'
    )),
    problem text not null,
    local_evidence text not null,
    upstream_evidence text not null,
    smallest_actionable_solution text not null,
    affected_logical_resources_json text not null,
    expected_benefit text not null,
    risk text not null,
    maintenance_burden text not null,
    dependencies text not null,
    acceptance_test text not null,
    rollback text not null,
    existing_decision text,
    existing_campaign_id text,
    created_at text not null,
    unique(campaign_id, selection_id)
) strict;

create index if not exists radar_campaign_project_idx on radar_campaign(project_id, state);
create index if not exists radar_stage_campaign_idx on radar_stage(campaign_id, stage);
create index if not exists radar_inventory_campaign_idx on radar_inventory(campaign_id, owner);
create index if not exists radar_pinned_commit_campaign_idx
    on radar_pinned_commit(campaign_id, repository_id);
create index if not exists radar_source_manifest_pin_idx on radar_source_manifest(pin_id);
create index if not exists radar_sub_run_campaign_idx on radar_sub_research_run(campaign_id, state);
create index if not exists radar_budget_campaign_idx
    on radar_budget_reservation(campaign_id, category);
create index if not exists radar_gap_campaign_idx on radar_coverage_gap(campaign_id, resolved_at);
create index if not exists radar_checkpoint_campaign_idx on radar_checkpoint(campaign_id, sequence);
create index if not exists radar_pattern_card_campaign_idx
    on radar_pattern_card(campaign_id, card_id);
create index if not exists radar_gap_card_campaign_idx
    on radar_gap_card(campaign_id, card_id);
create index if not exists radar_decision_campaign_idx
    on radar_candidate_decision(campaign_id, card_id);
create index if not exists radar_decision_selection_digest_idx
    on radar_candidate_decision(selection_digest, created_at desc);
"""

_MIGRATION_DIGEST = digest(_RADAR_DDL)


@dataclass(frozen=True, slots=True)
class CampaignRow:
    id: str
    project_id: str
    campaign_id: str
    stage: str
    state: str
    plan_digest: str
    plan_json: str
    idempotency_key: str
    effect_claim_id: str | None
    invocation_attempts: int
    planned_invocations: int
    started_invocations: int
    completed_invocations: int
    observed_provider_requests: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class InventoryRow:
    id: str
    campaign_id: str
    owner: str
    inventory_digest: str
    inventory_json: str
    state: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class BudgetReservationRow:
    id: str
    campaign_id: str
    category: str
    reserved: int
    consumed: int
    created_at: str


@dataclass(frozen=True, slots=True)
class SubResearchRunRow:
    id: str
    campaign_id: str
    question_digest: str
    question_json: str
    state: str
    result_json: str | None
    result_digest: str | None
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class CoverageGapRow:
    id: str
    campaign_id: str
    gap_key: str
    reason: str
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True, slots=True)
class CheckpointRow:
    id: str
    campaign_id: str
    sequence: int
    checkpoint_digest: str
    checkpoint_json: str
    created_at: str


@dataclass(frozen=True, slots=True)
class RadarPatternCardRow:
    id: str
    campaign_id: str
    card_id: str
    card_digest: str
    kind: str
    problem: str
    source_json: str
    observed_behavior: str
    inference_or_assumption: str
    test_evidence_level: str
    license_reuse_constraint: str
    cost_dependency_limit: str
    not_applicable_conditions: str
    created_at: str


@dataclass(frozen=True, slots=True)
class RadarGapCardRow:
    id: str
    campaign_id: str
    card_id: str
    card_digest: str
    kind: str
    current_baseline: str
    source_binding: str
    production_call_path: str
    test_measurement_evidence: str
    covered_areas: str
    uncovered_areas: str
    result: str
    created_at: str


@dataclass(frozen=True, slots=True)
class RadarCandidateDecisionRow:
    id: str
    campaign_id: str
    selection_id: str
    selection_digest: str
    card_id: str
    decision: str
    problem: str
    local_evidence: str
    upstream_evidence: str
    smallest_actionable_solution: str
    affected_logical_resources_json: str
    expected_benefit: str
    risk: str
    maintenance_burden: str
    dependencies: str
    acceptance_test: str
    rollback: str
    existing_decision: str | None
    existing_campaign_id: str | None
    created_at: str


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _validate_digest_value(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{label} bos olamaz")
    parse_digest(value)
    return value


def _canonical_json(payload: dict[str, Any]) -> str:
    return canonical_json(payload)


def _json_from_text(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationFailed("Radar JSON payload bozuk") from exc
    if not isinstance(parsed, dict):
        raise ValidationFailed("Radar JSON payload object olmali")
    return parsed


class RadarSchemaPort:
    """Idempotent additive schema manager for radar tables."""

    @staticmethod
    def ensure_schema(path: Path) -> None:
        if not path.is_absolute():
            raise ConfigurationError("Radar store path absolute olmali")
        connection = sqlite3.connect(str(path), timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("pragma foreign_keys = on")
            connection.execute("pragma busy_timeout = 5000")
            connection.execute("begin immediate")
            try:
                connection.executescript(_RADAR_DDL)
                existing = connection.execute(
                    "select version from radar_schema_migrations order by version desc limit 1"
                ).fetchone()
                current = 0 if existing is None else int(existing["version"])
                if current < RADAR_SCHEMA_VERSION:
                    if current < 3:
                        existing_columns = {
                            str(row["name"])
                            for row in connection.execute(
                                "pragma table_info(radar_campaign)"
                            ).fetchall()
                        }
                        additions = [
                            "invocation_attempts",
                            "planned_invocations",
                            "started_invocations",
                            "completed_invocations",
                            "observed_provider_requests",
                        ]
                        for column in additions:
                            if column not in existing_columns:
                                connection.execute(
                                    f"alter table radar_campaign add column {column}"
                                    " integer not null default 0"
                                )
                    if current < 4:
                        decision_columns = {
                            str(row["name"])
                            for row in connection.execute(
                                "pragma table_info(radar_candidate_decision)"
                            ).fetchall()
                        }
                        for column in ("existing_decision", "existing_campaign_id"):
                            if column not in decision_columns:
                                connection.execute(
                                    f"alter table radar_candidate_decision add column {column} text"
                                )
                    connection.execute(
                        "insert into radar_schema_migrations"
                        " (version, name, migration_digest, applied_at)"
                        " values (?, ?, ?, ?)",
                        (
                            RADAR_SCHEMA_VERSION,
                            "radar-campaign-duplicate-provenance-v4",
                            _MIGRATION_DIGEST,
                            _now(),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    @staticmethod
    def status(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"exists": False, "version": None}
        connection = sqlite3.connect(str(path), timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "select version from radar_schema_migrations order by version desc limit 1"
            ).fetchone()
            return {"exists": True, "version": None if row is None else int(row["version"])}
        finally:
            connection.close()


class RadarCampaignRepository:
    """Repository port for radar campaign persistence.

    The repository opens short-lived transactions.  It is thread-safe via a local
    lock for in-memory sequencing but relies on SQLite for concurrency control.
    """

    def __init__(self, path: Path) -> None:
        RadarSchemaPort.ensure_schema(path)
        self._path = path
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys = on")
        connection.execute("pragma busy_timeout = 5000")
        return connection

    def create_campaign(
        self,
        *,
        project_id: str,
        campaign_id: str,
        stage: str,
        state: str,
        plan_digest: str,
        plan: dict[str, Any],
        idempotency_key: str,
    ) -> CampaignRow:
        _validate_digest_value(plan_digest, "Plan digest")
        row_id = str(new_uuid7())
        now = _now()
        plan_json = _canonical_json(plan)
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                existing = connection.execute(
                    "select id from radar_campaign where campaign_id = ?", (campaign_id,)
                ).fetchone()
                if existing is not None:
                    row_id = str(existing["id"])
                    connection.execute(
                        "update radar_campaign set stage = ?, state = ?, plan_digest = ?,"
                        " plan_json = ?, idempotency_key = ?, effect_claim_id = null,"
                        " updated_at = ? where campaign_id = ?",
                        (stage, state, plan_digest, plan_json, idempotency_key, now, campaign_id),
                    )
                else:
                    connection.execute(
                        "insert into radar_campaign"
                        "(id, project_id, campaign_id, stage, state, plan_digest, plan_json,"
                        " idempotency_key, effect_claim_id, created_at, updated_at)"
                        " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row_id,
                            project_id,
                            campaign_id,
                            stage,
                            state,
                            plan_digest,
                            plan_json,
                            idempotency_key,
                            None,
                            now,
                            now,
                        ),
                    )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ValidationFailed("Campaign idempotency key duplicate") from exc
            except Exception:
                connection.rollback()
                raise
        return CampaignRow(
            id=row_id,
            project_id=project_id,
            campaign_id=campaign_id,
            stage=stage,
            state=state,
            plan_digest=plan_digest,
            plan_json=plan_json,
            idempotency_key=idempotency_key,
            effect_claim_id=None,
            invocation_attempts=0,
            planned_invocations=0,
            started_invocations=0,
            completed_invocations=0,
            observed_provider_requests=0,
            created_at=now,
            updated_at=now,
        )

    def get_campaign(self, campaign_id: str) -> CampaignRow:
        with self._connect() as connection:
            row = connection.execute(
                "select * from radar_campaign where campaign_id = ?", (campaign_id,)
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "select * from radar_campaign where id = ?", (campaign_id,)
                ).fetchone()
        if row is None:
            raise NotFound("Campaign bulunamadi")
        return self._row_to_campaign(row)

    def get_campaign_by_idempotency_key(self, idempotency_key: str) -> CampaignRow | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from radar_campaign where idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_campaign(row)

    def update_campaign_state(
        self,
        campaign_id: str,
        state: str,
        *,
        effect_claim_id: str | None = None,
    ) -> CampaignRow:
        if state not in {
            "ready",
            "running",
            "completed",
            "failed",
            "blocked",
            "recovery-required",
        }:
            raise ValidationFailed("Campaign state gecersiz")
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                if effect_claim_id is not None:
                    connection.execute(
                        "update radar_campaign set state = ?, effect_claim_id = ?, updated_at = ?"
                        " where campaign_id = ?",
                        (state, effect_claim_id, now, campaign_id),
                    )
                else:
                    connection.execute(
                        "update radar_campaign set state = ?, updated_at = ? where campaign_id = ?",
                        (state, now, campaign_id),
                    )
                if connection.total_changes == 0:
                    raise NotFound("Campaign bulunamadi")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_campaign(campaign_id)

    def record_stage(
        self,
        campaign_id: str,
        stage: str,
        state: str,
        *,
        evidence_digest: str | None = None,
        next_safe_action: str = "",
    ) -> None:
        if state not in {
            "ready",
            "running",
            "completed",
            "failed",
            "blocked",
            "recovery-required",
        }:
            raise ValidationFailed("Stage state gecersiz")
        stage_id = str(new_uuid7())
        now = _now()
        terminal_states = {"completed", "failed", "blocked", "recovery-required"}
        finished_at = now if state in terminal_states else None
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "insert into radar_stage(id, campaign_id, stage, state, started_at,"
                    " finished_at, evidence_digest, next_safe_action)"
                    " values (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stage_id,
                        campaign_id,
                        stage,
                        state,
                        now,
                        finished_at,
                        evidence_digest,
                        next_safe_action,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def save_inventory(
        self,
        campaign_id: str,
        owner: str,
        inventory: dict[str, Any],
        state: str,
    ) -> InventoryRow:
        inventory_digest = digest(inventory)
        inventory_json = _canonical_json(inventory)
        inventory_id = str(new_uuid7())
        observed_at = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                existing = connection.execute(
                    "select id from radar_inventory where inventory_digest = ?",
                    (inventory_digest,),
                ).fetchone()
                if existing is not None:
                    row = connection.execute(
                        "select * from radar_inventory where id = ?", (existing["id"],)
                    ).fetchone()
                    connection.commit()
                    return self._row_to_inventory(row)
                connection.execute(
                    "insert into radar_inventory(id, campaign_id, owner, inventory_digest,"
                    " inventory_json, state, observed_at) values (?, ?, ?, ?, ?, ?, ?)",
                    (
                        inventory_id,
                        campaign_id,
                        owner,
                        inventory_digest,
                        inventory_json,
                        state,
                        observed_at,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return InventoryRow(
            id=inventory_id,
            campaign_id=campaign_id,
            owner=owner,
            inventory_digest=inventory_digest,
            inventory_json=inventory_json,
            state=state,
            observed_at=observed_at,
        )

    def get_inventory_by_digest(self, inventory_digest: str) -> InventoryRow | None:
        _validate_digest_value(inventory_digest, "Inventory digest")
        with self._connect() as connection:
            row = connection.execute(
                "select * from radar_inventory where inventory_digest = ?", (inventory_digest,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_inventory(row)

    def list_inventories(self, campaign_id: str) -> tuple[InventoryRow, ...]:
        campaign = self.get_campaign(campaign_id)
        with self._connect() as connection:
            rows = connection.execute(
                "select * from radar_inventory where campaign_id = ? order by observed_at",
                (campaign.id,),
            ).fetchall()
        return tuple(self._row_to_inventory(row) for row in rows)

    def save_pinned_commits(
        self, campaign_id: str, pins: tuple[dict[str, Any], ...]
    ) -> tuple[str, ...]:
        now = _now()
        ids: list[str] = []
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                for pin in pins:
                    pin_id = str(new_uuid7())
                    ids.append(pin_id)
                    connection.execute(
                        "insert into radar_pinned_commit(id, campaign_id, repository_id, owner,"
                        " name, branch, commit_sha, pinned_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            pin_id,
                            campaign_id,
                            int(pin["repository_id"]),
                            str(pin["owner"]),
                            str(pin["name"]),
                            str(pin["branch"]),
                            str(pin["commit_sha"]),
                            now,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return tuple(ids)

    def list_pinned_commits(self, campaign_id: str) -> tuple[dict[str, Any], ...]:
        campaign = self.get_campaign(campaign_id)
        with self._connect() as connection:
            rows = connection.execute(
                "select * from radar_pinned_commit where campaign_id = ? order by pinned_at",
                (campaign.id,),
            ).fetchall()
        return tuple(
            {
                "id": str(row["id"]),
                "repository_id": int(row["repository_id"]),
                "owner": str(row["owner"]),
                "name": str(row["name"]),
                "branch": str(row["branch"]),
                "commit_sha": str(row["commit_sha"]),
                "pinned_at": str(row["pinned_at"]),
            }
            for row in rows
        )

    def save_source_manifest(
        self,
        campaign_id: str,
        pin_id: str,
        entries: tuple[dict[str, Any], ...],
    ) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                for entry in entries:
                    manifest_id = str(new_uuid7())
                    connection.execute(
                        "insert into radar_source_manifest(id, campaign_id, pin_id, path,"
                        " blob_sha, raw_content_digest, complete, omission_reason, fetched_at)"
                        " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            manifest_id,
                            campaign_id,
                            pin_id,
                            str(entry["path"]),
                            entry.get("blob_sha"),
                            entry.get("raw_content_digest"),
                            1 if bool(entry.get("complete", True)) else 0,
                            entry.get("omission_reason"),
                            now,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def list_source_manifest(self, campaign_id: str) -> tuple[dict[str, Any], ...]:
        campaign = self.get_campaign(campaign_id)
        with self._connect() as connection:
            rows = connection.execute(
                "select * from radar_source_manifest where campaign_id = ? order by fetched_at",
                (campaign.id,),
            ).fetchall()
        return tuple(
            {
                "id": str(row["id"]),
                "pin_id": str(row["pin_id"]),
                "path": str(row["path"]),
                "blob_sha": row["blob_sha"],
                "raw_content_digest": row["raw_content_digest"],
                "complete": bool(row["complete"]),
                "omission_reason": row["omission_reason"],
                "fetched_at": str(row["fetched_at"]),
            }
            for row in rows
        )

    def reserve_budget(
        self,
        campaign_id: str,
        category: str,
        amount: int,
        limit: int,
    ) -> BudgetReservationRow:
        if category not in {"requests", "bytes", "calls", "tokens"}:
            raise ValidationFailed("Budget category gecersiz")
        if not isinstance(amount, int) or amount <= 0:
            raise ValidationFailed("Budget amount pozitif integer olmali")
        if not isinstance(limit, int) or limit <= 0:
            raise ValidationFailed("Budget limit pozitif integer olmali")
        reservation_id = str(new_uuid7())
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                row = connection.execute(
                    "select coalesce(sum(reserved), 0) as total from radar_budget_reservation"
                    " where campaign_id = ? and category = ?",
                    (campaign_id, category),
                ).fetchone()
                total = int(row["total"]) if row is not None else 0
                if total + amount > limit:
                    raise PolicyViolation(
                        f"Budget {category} limit asildi: {total}+{amount} > {limit}"
                    )
                connection.execute(
                    "insert into radar_budget_reservation(id, campaign_id, category, reserved,"
                    " consumed, created_at) values (?, ?, ?, ?, 0, ?)",
                    (reservation_id, campaign_id, category, amount, now),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return BudgetReservationRow(
            id=reservation_id,
            campaign_id=campaign_id,
            category=category,
            reserved=amount,
            consumed=0,
            created_at=now,
        )

    def consume_budget(self, campaign_id: str, category: str, amount: int) -> BudgetReservationRow:
        if category not in {"requests", "bytes", "calls", "tokens"}:
            raise ValidationFailed("Budget category gecersiz")
        if not isinstance(amount, int) or amount < 0:
            raise ValidationFailed("Budget consume amount negatif olamaz")
        # Consume from oldest reservation first.
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                rows = connection.execute(
                    "select * from radar_budget_reservation where campaign_id = ? and category = ?"
                    " and consumed < reserved order by created_at",
                    (campaign_id, category),
                ).fetchall()
                remaining = amount
                for row in rows:
                    if remaining <= 0:
                        break
                    available = int(row["reserved"]) - int(row["consumed"])
                    take = min(available, remaining)
                    connection.execute(
                        "update radar_budget_reservation set consumed = consumed + ? where id = ?",
                        (take, str(row["id"])),
                    )
                    remaining -= take
                if remaining > 0:
                    raise PolicyViolation(
                        f"Budget {category} rezervasyonu tuketildi; {remaining} fazla"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        row = self._latest_budget_row(campaign_id, category)
        if row is None:
            raise ConfigurationError("Budget consume sonrasi kayip")
        return row

    def get_budget(self, campaign_id: str) -> dict[str, dict[str, int]]:
        campaign = self.get_campaign(campaign_id)
        with self._connect() as connection:
            rows = connection.execute(
                "select category, coalesce(sum(reserved), 0) as reserved,"
                " coalesce(sum(consumed), 0) as consumed from radar_budget_reservation"
                " where campaign_id = ? group by category",
                (campaign.id,),
            ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result[str(row["category"])] = {
                "reserved": int(row["reserved"]),
                "consumed": int(row["consumed"]),
            }
        return result

    def _latest_budget_row(self, campaign_id: str, category: str) -> BudgetReservationRow | None:
        with self._connect() as connection:
            row = connection.execute(
                "select * from radar_budget_reservation where campaign_id = ? and category = ?"
                " order by created_at desc limit 1",
                (campaign_id, category),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_budget(row)

    def save_sub_research_run(
        self,
        campaign_id: str,
        question: dict[str, Any],
        state: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> SubResearchRunRow:
        if state not in {"pending", "running", "completed", "failed", "blocked"}:
            raise ValidationFailed("Sub-research state gecersiz")
        question_digest = digest(question)
        question_json = _canonical_json(question)
        run_id = str(new_uuid7())
        now = _now()
        result_json = None if result is None else _canonical_json(result)
        result_digest = None if result is None else digest(result)
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "insert into radar_sub_research_run(id, campaign_id, question_digest,"
                    " question_json, state, result_json, result_digest, started_at, completed_at)"
                    " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        campaign_id,
                        question_digest,
                        question_json,
                        state,
                        result_json,
                        result_digest,
                        now,
                        now if state in {"completed", "failed", "blocked"} else None,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return SubResearchRunRow(
            id=run_id,
            campaign_id=campaign_id,
            question_digest=question_digest,
            question_json=question_json,
            state=state,
            result_json=result_json,
            result_digest=result_digest,
            started_at=now,
            completed_at=now if state in {"completed", "failed", "blocked"} else None,
        )

    def update_sub_research_run(
        self,
        run_id: str,
        state: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> SubResearchRunRow:
        if state not in {"pending", "running", "completed", "failed", "blocked"}:
            raise ValidationFailed("Sub-research state gecersiz")
        result_json = None if result is None else _canonical_json(result)
        result_digest = None if result is None else digest(result)
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "update radar_sub_research_run set state = ?, result_json = ?,"
                    " result_digest = ?, completed_at = ? where id = ?",
                    (
                        state,
                        result_json,
                        result_digest,
                        now if state in {"completed", "failed", "blocked"} else None,
                        run_id,
                    ),
                )
                if connection.total_changes == 0:
                    raise NotFound("Sub-research run bulunamadi")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_sub_research_run(run_id)

    def get_sub_research_run(self, run_id: str) -> SubResearchRunRow:
        with self._connect() as connection:
            row = connection.execute(
                "select * from radar_sub_research_run where id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise NotFound("Sub-research run bulunamadi")
        return self._row_to_sub_run(row)

    def list_sub_research_runs(self, campaign_id: str) -> tuple[SubResearchRunRow, ...]:
        campaign = self.get_campaign(campaign_id)
        with self._connect() as connection:
            rows = connection.execute(
                "select * from radar_sub_research_run where campaign_id = ? order by started_at",
                (campaign.id,),
            ).fetchall()
        return tuple(self._row_to_sub_run(row) for row in rows)

    def record_planned_invocations(self, campaign_row_id: str, count: int) -> CampaignRow:
        if not isinstance(count, int) or count < 0:
            raise ValidationFailed("planned_invocations negatif olamaz")
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "update radar_campaign set planned_invocations = ?, updated_at = ?"
                    " where id = ?",
                    (count, now, campaign_row_id),
                )
                if connection.total_changes == 0:
                    raise NotFound("Campaign bulunamadi")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_campaign(campaign_row_id)

    def record_invocation_attempt(self, campaign_row_id: str, limit: int) -> CampaignRow:
        if not isinstance(limit, int) or limit <= 0:
            raise ValidationFailed("invocation_attempts limiti pozitif integer olmali")
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                row = connection.execute(
                    "select invocation_attempts from radar_campaign where id = ?",
                    (campaign_row_id,),
                ).fetchone()
                if row is None:
                    raise NotFound("Campaign bulunamadi")
                current = int(row["invocation_attempts"])
                if current >= limit:
                    raise PolicyViolation(f"invocation_attempts limit asildi: {current} >= {limit}")
                connection.execute(
                    "update radar_campaign set invocation_attempts = invocation_attempts + 1,"
                    " updated_at = ? where id = ?",
                    (_now(), campaign_row_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_campaign(campaign_row_id)

    def record_invocation_started(self, campaign_row_id: str) -> CampaignRow:
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "update radar_campaign set started_invocations = started_invocations + 1,"
                    " updated_at = ? where id = ?",
                    (now, campaign_row_id),
                )
                if connection.total_changes == 0:
                    raise NotFound("Campaign bulunamadi")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_campaign(campaign_row_id)

    def record_invocation_completed(
        self, campaign_row_id: str, *, observed_provider_requests: int = 0
    ) -> CampaignRow:
        if observed_provider_requests < 0:
            raise ValidationFailed("observed_provider_requests negatif olamaz")
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "update radar_campaign set completed_invocations = completed_invocations + 1,"
                    " observed_provider_requests = observed_provider_requests + ?,"
                    " updated_at = ? where id = ?",
                    (observed_provider_requests, now, campaign_row_id),
                )
                if connection.total_changes == 0:
                    raise NotFound("Campaign bulunamadi")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_campaign(campaign_row_id)

    def record_coverage_gap(self, campaign_id: str, gap_key: str, reason: str) -> CoverageGapRow:
        if not gap_key or not reason:
            raise ValidationFailed("Coverage gap key/reason bos olamaz")
        gap_id = str(new_uuid7())
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "insert into radar_coverage_gap(id, campaign_id, gap_key, reason,"
                    " created_at, resolved_at) values (?, ?, ?, ?, ?, ?)",
                    (gap_id, campaign_id, gap_key, reason, now, None),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return CoverageGapRow(
            id=gap_id,
            campaign_id=campaign_id,
            gap_key=gap_key,
            reason=reason,
            created_at=now,
            resolved_at=None,
        )

    def resolve_coverage_gap(self, campaign_id: str, gap_key: str) -> None:
        campaign = self.get_campaign(campaign_id)
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "update radar_coverage_gap set resolved_at = ? where campaign_id = ?"
                    " and gap_key = ? and resolved_at is null",
                    (now, campaign.id, gap_key),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def list_coverage_gaps(
        self, campaign_id: str, *, unresolved_only: bool = False
    ) -> tuple[CoverageGapRow, ...]:
        campaign = self.get_campaign(campaign_id)
        query = "select * from radar_coverage_gap where campaign_id = ?"
        params: list[Any] = [campaign.id]
        if unresolved_only:
            query += " and resolved_at is null"
        query += " order by created_at"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._row_to_gap(row) for row in rows)

    def save_checkpoint(
        self, campaign_id: str, sequence: int, payload: dict[str, Any]
    ) -> CheckpointRow:
        checkpoint_digest = digest(payload)
        checkpoint_json = _canonical_json(payload)
        checkpoint_id = str(new_uuid7())
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "insert into radar_checkpoint(id, campaign_id, sequence, checkpoint_digest,"
                    " checkpoint_json, created_at) values (?, ?, ?, ?, ?, ?)",
                    (checkpoint_id, campaign_id, sequence, checkpoint_digest, checkpoint_json, now),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return CheckpointRow(
            id=checkpoint_id,
            campaign_id=campaign_id,
            sequence=sequence,
            checkpoint_digest=checkpoint_digest,
            checkpoint_json=checkpoint_json,
            created_at=now,
        )

    def latest_checkpoint(self, campaign_id: str) -> CheckpointRow | None:
        campaign = self.get_campaign(campaign_id)
        with self._connect() as connection:
            row = connection.execute(
                "select * from radar_checkpoint"
                " where campaign_id = ? order by sequence desc limit 1",
                (campaign.id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_checkpoint(row)

    def campaign_status_document(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.get_campaign(campaign_id)
        runs = self.list_sub_research_runs(campaign_id)
        gaps = self.list_coverage_gaps(campaign_id)
        budget = self.get_budget(campaign_id)
        checkpoint = self.latest_checkpoint(campaign_id)
        completed = [r.question_digest for r in runs if r.state == "completed"]
        pending = [r.question_digest for r in runs if r.state in {"pending", "running"}]
        latest_stage = self._latest_stage(campaign_id)
        next_safe_action = latest_stage["next_safe_action"] if latest_stage is not None else "plan"
        return {
            "schema": "zekam-radar-campaign-status/v1",
            "campaign_id": campaign_id,
            "stage": campaign.stage,
            "state": campaign.state,
            "completed_work": completed,
            "pending_work": pending,
            "coverage_gaps": [{"gap_key": g.gap_key, "reason": g.reason} for g in gaps],
            "consumed_budget": {k: v.get("consumed", 0) for k, v in budget.items()},
            "reserved_budget": {k: v.get("reserved", 0) for k, v in budget.items()},
            "planned_invocations": campaign.planned_invocations,
            "started_invocations": campaign.started_invocations,
            "completed_invocations": campaign.completed_invocations,
            "observed_provider_requests": campaign.observed_provider_requests,
            "next_safe_action": next_safe_action,
            "latest_checkpoint": None if checkpoint is None else checkpoint.checkpoint_digest,
            "read_only": True,
            "grants_authority": False,
        }

    def _latest_stage(self, campaign_id: str) -> dict[str, Any] | None:
        campaign = self.get_campaign(campaign_id)
        with self._connect() as connection:
            row = connection.execute(
                "select * from radar_stage where campaign_id = ? order by started_at desc limit 1",
                (campaign.id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "stage": str(row["stage"]),
            "state": str(row["state"]),
            "next_safe_action": str(row["next_safe_action"]),
        }

    def campaign_report_document(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.get_campaign(campaign_id)
        inventories = self.list_inventories(campaign_id)
        pins = self.list_pinned_commits(campaign_id)
        manifest = self.list_source_manifest(campaign_id)
        runs = self.list_sub_research_runs(campaign_id)
        gaps = self.list_coverage_gaps(campaign_id)
        budget = self.get_budget(campaign_id)
        inventory_summary = {
            "count": len(inventories),
            "owners": sorted({i.owner for i in inventories}),
            "states": [i.state for i in inventories],
        }
        return {
            "schema": "zekam-radar-campaign-report/v1",
            "campaign_id": campaign_id,
            "stage": campaign.stage,
            "status": campaign.state,
            "inventory_summary": inventory_summary,
            "selected_repos": [
                {"owner": p["owner"], "name": p["name"], "commit_sha": p["commit_sha"]}
                for p in pins
            ],
            "source_manifest_summary": {
                "entries": len(manifest),
                "complete": sum(1 for m in manifest if m["complete"]),
                "incomplete": sum(1 for m in manifest if not m["complete"]),
            },
            "sub_research_runs": [
                {
                    "question_digest": r.question_digest,
                    "state": r.state,
                    "result_digest": r.result_digest,
                }
                for r in runs
            ],
            "coverage_gaps": [{"gap_key": g.gap_key, "reason": g.reason} for g in gaps],
            "budget": budget,
            "read_only": True,
            "grants_authority": False,
        }

    @staticmethod
    def _row_to_campaign(row: sqlite3.Row) -> CampaignRow:
        return CampaignRow(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            campaign_id=str(row["campaign_id"]),
            stage=str(row["stage"]),
            state=str(row["state"]),
            plan_digest=str(row["plan_digest"]),
            plan_json=str(row["plan_json"]),
            idempotency_key=str(row["idempotency_key"]),
            effect_claim_id=row["effect_claim_id"],
            invocation_attempts=int(row["invocation_attempts"]),
            planned_invocations=int(row["planned_invocations"]),
            started_invocations=int(row["started_invocations"]),
            completed_invocations=int(row["completed_invocations"]),
            observed_provider_requests=int(row["observed_provider_requests"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_inventory(row: sqlite3.Row) -> InventoryRow:
        return InventoryRow(
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            owner=str(row["owner"]),
            inventory_digest=str(row["inventory_digest"]),
            inventory_json=str(row["inventory_json"]),
            state=str(row["state"]),
            observed_at=str(row["observed_at"]),
        )

    @staticmethod
    def _row_to_budget(row: sqlite3.Row) -> BudgetReservationRow:
        return BudgetReservationRow(
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            category=str(row["category"]),
            reserved=int(row["reserved"]),
            consumed=int(row["consumed"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_to_sub_run(row: sqlite3.Row) -> SubResearchRunRow:
        return SubResearchRunRow(
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            question_digest=str(row["question_digest"]),
            question_json=str(row["question_json"]),
            state=str(row["state"]),
            result_json=row["result_json"],
            result_digest=row["result_digest"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _row_to_gap(row: sqlite3.Row) -> CoverageGapRow:
        return CoverageGapRow(
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            gap_key=str(row["gap_key"]),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
            resolved_at=row["resolved_at"],
        )

    @staticmethod
    def _row_to_checkpoint(row: sqlite3.Row) -> CheckpointRow:
        return CheckpointRow(
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            sequence=int(row["sequence"]),
            checkpoint_digest=str(row["checkpoint_digest"]),
            checkpoint_json=str(row["checkpoint_json"]),
            created_at=str(row["created_at"]),
        )

    def save_pattern_card(self, campaign_id: str, card: RadarPatternCard) -> RadarPatternCardRow:
        campaign = self.get_campaign(campaign_id)
        row_id = str(new_uuid7())
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "insert into radar_pattern_card"
                    " (id, campaign_id, card_id, card_digest, kind, problem, source_json,"
                    " observed_behavior, inference_or_assumption, test_evidence_level,"
                    " license_reuse_constraint, cost_dependency_limit, not_applicable_conditions,"
                    " created_at)"
                    " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        campaign.id,
                        card.card_id,
                        card.card_digest,
                        str(card.kind),
                        card.problem,
                        _canonical_json(card.source.as_dict()),
                        card.observed_behavior,
                        card.inference_or_assumption,
                        str(card.test_evidence_level),
                        card.license_reuse_constraint,
                        card.cost_dependency_limit,
                        card.not_applicable_conditions,
                        now,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return RadarPatternCardRow(
            id=row_id,
            campaign_id=campaign.id,
            card_id=card.card_id,
            card_digest=card.card_digest,
            kind=str(card.kind),
            problem=card.problem,
            source_json=_canonical_json(card.source.as_dict()),
            observed_behavior=card.observed_behavior,
            inference_or_assumption=card.inference_or_assumption,
            test_evidence_level=str(card.test_evidence_level),
            license_reuse_constraint=card.license_reuse_constraint,
            cost_dependency_limit=card.cost_dependency_limit,
            not_applicable_conditions=card.not_applicable_conditions,
            created_at=now,
        )

    def list_pattern_cards(self, campaign_id: str) -> tuple[RadarPatternCardRow, ...]:
        campaign = self.get_campaign(campaign_id)
        with self._connect() as connection:
            rows = connection.execute(
                "select * from radar_pattern_card where campaign_id = ? order by created_at",
                (campaign.id,),
            ).fetchall()
        return tuple(self._row_to_pattern_card(row) for row in rows)

    def save_gap_card(self, campaign_id: str, card: RadarGapCard) -> RadarGapCardRow:
        campaign = self.get_campaign(campaign_id)
        row_id = str(new_uuid7())
        now = _now()
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "insert into radar_gap_card"
                    " (id, campaign_id, card_id, card_digest, kind, current_baseline,"
                    " source_binding, production_call_path, test_measurement_evidence,"
                    " covered_areas, uncovered_areas, result, created_at)"
                    " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        campaign.id,
                        card.card_id,
                        card.card_digest,
                        str(card.kind),
                        card.current_baseline,
                        card.source_binding,
                        card.production_call_path,
                        card.test_measurement_evidence,
                        card.covered_areas,
                        card.uncovered_areas,
                        str(card.result),
                        now,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return RadarGapCardRow(
            id=row_id,
            campaign_id=campaign.id,
            card_id=card.card_id,
            card_digest=card.card_digest,
            kind=str(card.kind),
            current_baseline=card.current_baseline,
            source_binding=card.source_binding,
            production_call_path=card.production_call_path,
            test_measurement_evidence=card.test_measurement_evidence,
            covered_areas=card.covered_areas,
            uncovered_areas=card.uncovered_areas,
            result=str(card.result),
            created_at=now,
        )

    def list_gap_cards(self, campaign_id: str) -> tuple[RadarGapCardRow, ...]:
        campaign = self.get_campaign(campaign_id)
        with self._connect() as connection:
            rows = connection.execute(
                "select * from radar_gap_card where campaign_id = ? order by created_at",
                (campaign.id,),
            ).fetchall()
        return tuple(self._row_to_gap_card(row) for row in rows)

    def save_candidate_decision(
        self, campaign_id: str, selection: RadarCandidateSelection
    ) -> RadarCandidateDecisionRow:
        campaign = self.get_campaign(campaign_id)
        row_id = str(new_uuid7())
        now = _now()
        resources_json = _canonical_json(
            {"affected_logical_resources": list(selection.affected_logical_resources)}
        )
        with self._connect() as connection:
            connection.execute("begin immediate")
            try:
                connection.execute(
                    "insert into radar_candidate_decision"
                    " (id, campaign_id, selection_id, selection_digest, card_id, decision,"
                    " problem, local_evidence, upstream_evidence, smallest_actionable_solution,"
                    " affected_logical_resources_json, expected_benefit, risk,"
                    " maintenance_burden, dependencies, acceptance_test, rollback,"
                    " existing_decision, existing_campaign_id, created_at)"
                    " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        campaign.id,
                        selection.selection_id,
                        selection.selection_digest,
                        selection.card_id,
                        str(selection.decision),
                        selection.problem,
                        selection.local_evidence,
                        selection.upstream_evidence,
                        selection.smallest_actionable_solution,
                        resources_json,
                        selection.expected_benefit,
                        selection.risk,
                        selection.maintenance_burden,
                        selection.dependencies,
                        selection.acceptance_test,
                        selection.rollback,
                        selection.existing_decision,
                        selection.existing_campaign_id,
                        now,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return RadarCandidateDecisionRow(
            id=row_id,
            campaign_id=campaign.id,
            selection_id=selection.selection_id,
            selection_digest=selection.selection_digest,
            card_id=selection.card_id,
            decision=str(selection.decision),
            problem=selection.problem,
            local_evidence=selection.local_evidence,
            upstream_evidence=selection.upstream_evidence,
            smallest_actionable_solution=selection.smallest_actionable_solution,
            affected_logical_resources_json=resources_json,
            expected_benefit=selection.expected_benefit,
            risk=selection.risk,
            maintenance_burden=selection.maintenance_burden,
            dependencies=selection.dependencies,
            acceptance_test=selection.acceptance_test,
            rollback=selection.rollback,
            existing_decision=selection.existing_decision,
            existing_campaign_id=selection.existing_campaign_id,
            created_at=now,
        )

    def list_candidate_decisions(self, campaign_id: str) -> tuple[RadarCandidateDecisionRow, ...]:
        campaign = self.get_campaign(campaign_id)
        with self._connect() as connection:
            rows = connection.execute(
                "select * from radar_candidate_decision where campaign_id = ? order by created_at",
                (campaign.id,),
            ).fetchall()
        return tuple(self._row_to_decision(row) for row in rows)

    def find_duplicate_selection_digest(
        self, selection_digest: str
    ) -> RadarCandidateDecisionRow | None:
        """Return the most recent candidate decision with the same selection digest.

        Looks across all campaigns so a candidate that already appeared in a previous
        campaign is linked to its prior provenance instead of creating a duplicate task.
        """

        _validate_digest_value(selection_digest, "Selection digest")
        with self._connect() as connection:
            row = connection.execute(
                "select * from radar_candidate_decision where selection_digest = ?"
                " order by created_at desc limit 1",
                (selection_digest,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_decision(row)

    def candidates_document(self, campaign_id: str) -> dict[str, Any]:
        """Read-only candidate view for CLI/report surfaces."""

        campaign = self.get_campaign(campaign_id)
        patterns = self.list_pattern_cards(campaign_id)
        gaps = self.list_gap_cards(campaign_id)
        decisions = self.list_candidate_decisions(campaign_id)
        return {
            "schema": "zekam-radar-campaign-candidates/v1",
            "campaign_id": campaign_id,
            "campaign_state": campaign.state,
            "pattern_cards": [
                {
                    "card_id": p.card_id,
                    "card_digest": p.card_digest,
                    "problem": p.problem,
                    "test_evidence_level": p.test_evidence_level,
                }
                for p in patterns
            ],
            "gap_cards": [
                {
                    "card_id": g.card_id,
                    "card_digest": g.card_digest,
                    "result": g.result,
                    "production_call_path": g.production_call_path,
                }
                for g in gaps
            ],
            "decisions": [
                {
                    "selection_id": d.selection_id,
                    "card_id": d.card_id,
                    "decision": d.decision,
                    "risk": d.risk,
                }
                for d in decisions
            ],
            "read_only": True,
            "grants_authority": False,
        }

    @staticmethod
    def _row_to_pattern_card(row: sqlite3.Row) -> RadarPatternCardRow:
        return RadarPatternCardRow(
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            card_id=str(row["card_id"]),
            card_digest=str(row["card_digest"]),
            kind=str(row["kind"]),
            problem=str(row["problem"]),
            source_json=str(row["source_json"]),
            observed_behavior=str(row["observed_behavior"]),
            inference_or_assumption=str(row["inference_or_assumption"]),
            test_evidence_level=str(row["test_evidence_level"]),
            license_reuse_constraint=str(row["license_reuse_constraint"]),
            cost_dependency_limit=str(row["cost_dependency_limit"]),
            not_applicable_conditions=str(row["not_applicable_conditions"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_to_gap_card(row: sqlite3.Row) -> RadarGapCardRow:
        return RadarGapCardRow(
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            card_id=str(row["card_id"]),
            card_digest=str(row["card_digest"]),
            kind=str(row["kind"]),
            current_baseline=str(row["current_baseline"]),
            source_binding=str(row["source_binding"]),
            production_call_path=str(row["production_call_path"]),
            test_measurement_evidence=str(row["test_measurement_evidence"]),
            covered_areas=str(row["covered_areas"]),
            uncovered_areas=str(row["uncovered_areas"]),
            result=str(row["result"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> RadarCandidateDecisionRow:
        return RadarCandidateDecisionRow(
            id=str(row["id"]),
            campaign_id=str(row["campaign_id"]),
            selection_id=str(row["selection_id"]),
            selection_digest=str(row["selection_digest"]),
            card_id=str(row["card_id"]),
            decision=str(row["decision"]),
            problem=str(row["problem"]),
            local_evidence=str(row["local_evidence"]),
            upstream_evidence=str(row["upstream_evidence"]),
            smallest_actionable_solution=str(row["smallest_actionable_solution"]),
            affected_logical_resources_json=str(row["affected_logical_resources_json"]),
            expected_benefit=str(row["expected_benefit"]),
            risk=str(row["risk"]),
            maintenance_burden=str(row["maintenance_burden"]),
            dependencies=str(row["dependencies"]),
            acceptance_test=str(row["acceptance_test"]),
            rollback=str(row["rollback"]),
            existing_decision=row["existing_decision"],
            existing_campaign_id=row["existing_campaign_id"],
            created_at=str(row["created_at"]),
        )
