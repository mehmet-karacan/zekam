"""Exact, provider-free bootstrap plan for operational v5 and a narrow local grant."""

from __future__ import annotations

import datetime as dt
import getpass
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.application.client_lifecycle_spool import (
    _read_bounded_bytes,
    _safe_regular_file_exists,
    _write_immutable_json,
)
from zekam.application.composition import ApplicationContext
from zekam.application.evolution_runtime import (
    CurrentEvolutionResumeVerifier,
    build_evolution_plan,
    build_resume_admission,
    build_windows_supervisor_plan,
)
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.evolution_authority import (
    EVOLUTION_HANDLERS,
    EvolutionBudget,
    ExecutionBoundary,
    StandingGrant,
    StandingGrantKind,
    StandingGrantState,
)
from zekam.domain.improvement_policy import ImprovementChangeClass
from zekam.domain.security import DataClassification
from zekam.infrastructure.local_file_security import (
    private_regular,
    restrict_private_file,
    restrict_private_tree,
)
from zekam.infrastructure.sqlite.evolution_authority import (
    SQLiteEvolutionApprovalAuthority,
    SQLiteEvolutionAuthorityLedger,
)
from zekam.infrastructure.sqlite.local_improvement import SQLiteLocalImprovementStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest
from zekam.infrastructure.sqlite.operational_migration import (
    MigrationSpoolTarget,
    migrate_v3_to_v5,
)
from zekam.infrastructure.sqlite.operational_schema import status as operational_status
from zekam.infrastructure.windows_task_scheduler import WindowsTaskPlan, install_windows_task

LOCAL_GRANT_OPERATIONS = (
    "continuity.capture",
    "continuity.summarize",
    "improvement.evaluate",
    "improvement.plan",
    "improvement.shadow",
    "knowledge.compile",
    "knowledge.reconcile",
    "learning.reflect",
    "maintenance.reconcile",
    "report.daily",
    "skill.evaluate",
    "skill.propose",
)


@dataclass(frozen=True, slots=True)
class EvolutionBootstrapPlan:
    body: dict[str, Any]
    grant: StandingGrant

    @property
    def plan_digest(self) -> str:
        return digest(self.body)

    def as_dict(self) -> dict[str, Any]:
        value = json.loads(canonical_json(self.body | {"plan_digest": self.plan_digest}))
        if not isinstance(value, dict):
            raise ConfigurationError("Evolution bootstrap plan canonical object olmali")
        return value


def _operational_inventory(database: Path) -> tuple[tuple[uuid.UUID, str], ...]:
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute("select id,slug from project order by id").fetchall()
    if not rows:
        raise ConfigurationError("Evolution bootstrap en az bir canonical project ister")
    try:
        return tuple((uuid.UUID(str(row[0])), str(row[1])) for row in rows)
    except ValueError as exc:
        raise ConfigurationError("Evolution bootstrap project identity drift") from exc


def _realm_scope(
    database: Path, projects: tuple[tuple[uuid.UUID, str], ...]
) -> tuple[uuid.UUID, tuple[uuid.UUID, ...]]:
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "select project_id,realm_id from project_knowledge_realm order by project_id"
        ).fetchall()
    realms = {uuid.UUID(str(row[1])) for row in rows}
    if len(realms) != 1:
        raise ConfigurationError("Evolution bootstrap exact tek knowledge realm ister")
    bound = {uuid.UUID(str(row[0])) for row in rows}
    missing = tuple(sorted((item[0] for item in projects if item[0] not in bound), key=str))
    return next(iter(realms)), missing


def _device_identity() -> tuple[str, uuid.UUID, uuid.UUID]:
    principal = getpass.getuser().strip().lower()
    computer = os.environ.get("COMPUTERNAME", "windows").strip().lower()
    identity = digest({"principal": principal, "computer": computer})[7:23]
    device_id = f"windows-{identity}"
    owner_id = uuid.uuid5(uuid.NAMESPACE_URL, f"zekam-owner:{principal}")
    actor_id = uuid.uuid5(uuid.NAMESPACE_URL, f"zekam-actor:{principal}:{computer}")
    return device_id, owner_id, actor_id


def _next_grant_revision(database: Path, grant_id: uuid.UUID, schema_version: int) -> int:
    if schema_version == 3:
        return 1
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "select max(revision) from evolution_standing_grant where grant_id=?",
            (str(grant_id),),
        ).fetchone()
    return 1 if row is None or row[0] is None else int(row[0]) + 1


def _control_precondition(context: ApplicationContext) -> dict[str, object]:
    store = SQLiteLocalImprovementStore(
        context.home / "state" / "improvement.db",
        context.home / "state" / "learning.db",
        context.home / "benchmarklar" / "benchmark.db",
    )
    current = store.evolution_control_status()
    return {
        "state": current["state"],
        "ordinal": current["ordinal"],
        "event_digest": current["event_digest"],
    }


def build_evolution_bootstrap_plan(
    context: ApplicationContext, *, now: dt.datetime | None = None
) -> EvolutionBootstrapPlan:
    """Build a stable-within-the-hour plan; never migrates, approves or installs."""

    moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).replace(
        minute=0, second=0, microsecond=0
    )
    database = context.settings.database.sqlite_path(context.home)
    current = operational_status(database)
    if not current.exists or not current.integrity_ok or not current.schema_ok:
        raise ConfigurationError("Evolution bootstrap current operational authority ister")
    if current.schema_version not in {3, 5}:
        raise ConfigurationError("Evolution bootstrap operational schema v3 veya v5 ister")
    evolution = build_evolution_plan(context)
    bindings = evolution.get("admission_bindings")
    supervisor = evolution.get("supervisor")
    if not isinstance(bindings, dict) or len(bindings) != 7 or not isinstance(supervisor, dict):
        raise ConfigurationError("Evolution bootstrap current manifest bindings ister")
    projects = _operational_inventory(database)
    realm_id, missing_realm_projects = _realm_scope(database, projects)
    device_id, owner_id, actor_id = _device_identity()
    contracts = tuple(EVOLUTION_HANDLERS[operation] for operation in LOCAL_GRANT_OPERATIONS)
    reads = tuple(sorted({item for contract in contracts for item in contract.readable_resources}))
    writes = tuple(sorted({item for contract in contracts for item in contract.writable_resources}))
    grant_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        "zekam-local-evolution:" + str(bindings["task_scope_digest"]) + ":" + device_id,
    )
    grant = StandingGrant(
        grant_id=grant_id,
        revision=_next_grant_revision(database, grant_id, current.schema_version),
        kind=StandingGrantKind.EVOLUTION,
        owner_id=owner_id,
        actor_id=actor_id,
        device_id=device_id,
        realm_id=realm_id,
        project_ids=tuple(sorted((item[0] for item in projects), key=str)),
        logical_sources=tuple(sorted(item[1] for item in projects)),
        operations=LOCAL_GRANT_OPERATIONS,
        handler_versions=tuple(sorted(contract.version for contract in contracts)),
        change_classes=(ImprovementChangeClass.AUTO_SAFE,),
        readable_resources=reads,
        writable_resources=writes,
        model_refs=("none",),
        provider_refs=("local-deterministic",),
        data_classifications=(DataClassification.LOCAL_ONLY,),
        network_scopes=(),
        execution_boundary=ExecutionBoundary.LOCAL,
        task_scope_digest=str(bindings["task_scope_digest"]),
        policy_digest=str(bindings["policy_digest"]),
        verifier_digest=str(bindings["verifier_digest"]),
        validator_digest=str(bindings["validator_digest"]),
        source_lineage_digest=str(bindings["source_lineage_digest"]),
        protected_manifest_digest=str(bindings["protected_manifest_digest"]),
        dependency_manifest_digest=str(bindings["dependency_manifest_digest"]),
        budget=EvolutionBudget(0, 0, 86_400, 0, 104_857_600, 1),
        valid_from=moment,
        expires_at=moment + dt.timedelta(days=30),
        review_after=moment + dt.timedelta(days=7),
        rollback_required=True,
        notify_on_failure=True,
    )
    source_digest = logical_database_digest(database)
    backup = context.home / "backups" / f"operational-v3-before-v5-{source_digest[7:19]}.db"
    if current.schema_version == 3 and (backup.exists() or backup.is_symlink()):
        raise PolicyViolation("Evolution bootstrap exact backup target already exists")
    approval_body = {
        "schema": "zekam-evolution-owner-approval/v1",
        "grant_digest": grant.grant_digest,
        "owner_id": str(owner_id),
        "realm_id": str(grant.realm_id),
        "device_id": device_id,
        "approver_id": str(owner_id),
        "issued_at": moment,
        "expires_at": moment + dt.timedelta(days=1),
    }
    body: dict[str, Any] = {
        "schema": "zekam-evolution-bootstrap-plan/v1",
        "operational": {
            "source_version": current.schema_version,
            "target_version": 5,
            "source_logical_digest": source_digest,
            "database_ref": "ZEKAM_HOME/state/operational.db",
            "backup_ref": f"ZEKAM_HOME/backups/{backup.name}",
            "migration_required": current.schema_version == 3,
            "realm_id": str(realm_id),
            "realm_bindings_to_add": [str(value) for value in missing_realm_projects],
        },
        "supervisor_plan_digest": supervisor.get("plan_digest"),
        "grant": grant.body(),
        "grant_digest": grant.grant_digest,
        "approval_receipt": approval_body,
        "approval_receipt_digest": digest(approval_body),
        "bootstrap_claim": {
            "operation": "evolution.bootstrap/v1",
            "owner_id": str(owner_id),
            "actor_id": str(actor_id),
            "device_id": device_id,
            "effects": [
                "operational.migrate-v3-v5",
                "project-realm.bind-missing",
                "evolution-grant.register",
                "windows-supervisor.install",
                "evolution-control.resume",
            ],
            "terminal_required": True,
            "grants_authority": False,
        },
        "control_precondition": _control_precondition(context),
        "provider_calls": 0,
        "network_scope": "none",
        "apply": False,
        "authorization_required": True,
        "grants_authority": False,
    }
    return EvolutionBootstrapPlan(body=body, grant=grant)


def _timestamp(value: object, label: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigurationError(f"Evolution bootstrap {label} timestamp invalid") from exc
    else:
        raise ConfigurationError(f"Evolution bootstrap {label} timestamp invalid")
    if parsed.tzinfo is None:
        raise ConfigurationError(f"Evolution bootstrap {label} timezone ister")
    return parsed.astimezone(dt.UTC)


def _grant_from_body(body: object) -> StandingGrant:
    if not isinstance(body, dict):
        raise ConfigurationError("Evolution bootstrap persisted grant object ister")
    try:
        budget = body["budget"]
        if not isinstance(budget, dict):
            raise TypeError
        grant = StandingGrant(
            grant_id=uuid.UUID(str(body["grant_id"])),
            revision=int(body["revision"]),
            kind=StandingGrantKind(str(body["kind"])),
            owner_id=uuid.UUID(str(body["owner_id"])),
            actor_id=uuid.UUID(str(body["actor_id"])),
            device_id=str(body["device_id"]),
            realm_id=uuid.UUID(str(body["realm_id"])),
            project_ids=tuple(uuid.UUID(str(value)) for value in body["project_ids"]),
            logical_sources=tuple(map(str, body["logical_sources"])),
            operations=tuple(map(str, body["operations"])),
            handler_versions=tuple(map(str, body["handler_versions"])),
            change_classes=tuple(
                ImprovementChangeClass(str(value)) for value in body["change_classes"]
            ),
            readable_resources=tuple(map(str, body["readable_resources"])),
            writable_resources=tuple(map(str, body["writable_resources"])),
            model_refs=tuple(map(str, body["model_refs"])),
            provider_refs=tuple(map(str, body["provider_refs"])),
            data_classifications=tuple(
                DataClassification(str(value)) for value in body["data_classifications"]
            ),
            network_scopes=tuple(map(str, body["network_scopes"])),
            execution_boundary=ExecutionBoundary(str(body["execution_boundary"])),
            task_scope_digest=str(body["task_scope_digest"]),
            policy_digest=str(body["policy_digest"]),
            verifier_digest=str(body["verifier_digest"]),
            validator_digest=str(body["validator_digest"]),
            source_lineage_digest=str(body["source_lineage_digest"]),
            protected_manifest_digest=str(body["protected_manifest_digest"]),
            dependency_manifest_digest=str(body["dependency_manifest_digest"]),
            budget=EvolutionBudget(
                int(budget["provider_calls"]),
                int(budget["tokens"]),
                int(budget["duration_seconds"]),
                int(budget["cost_micros"]),
                int(budget["disk_bytes"]),
                int(budget["concurrency"]),
            ),
            valid_from=_timestamp(body["valid_from"], "valid_from"),
            expires_at=_timestamp(body["expires_at"], "expires_at"),
            review_after=_timestamp(body["review_after"], "review_after"),
            rollback_required=body["rollback_required"],
            notify_on_failure=body["notify_on_failure"],
            state=StandingGrantState(str(body["state"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError("Evolution bootstrap persisted grant invalid") from exc
    expected = body.get("handler_contract_digests")
    actual = grant.body().get("handler_contract_digests")
    if expected != actual:
        raise ConfigurationError("Evolution bootstrap handler contract drift")
    return grant


def _intent_path(context: ApplicationContext, plan_digest: str) -> Path:
    try:
        parse_digest(plan_digest)
    except (AttributeError, TypeError, ValidationFailed) as exc:
        raise PolicyViolation("Evolution bootstrap plan digest invalid") from exc
    return context.home / "runtime" / "evolution-bootstrap" / f"{plan_digest[7:]}.json"


def _persist_intent(context: ApplicationContext, plan: EvolutionBootstrapPlan) -> None:
    path = _intent_path(context, plan.plan_digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    restrict_private_tree(path.parent)
    _write_immutable_json(path, plan.as_dict())
    restrict_private_file(path)
    if not private_regular(path):
        raise PolicyViolation("Evolution bootstrap immutable intent ACL readback invalid")


def _load_intent(
    context: ApplicationContext, authorized_plan_digest: str
) -> EvolutionBootstrapPlan | None:
    path = _intent_path(context, authorized_plan_digest)
    if not _safe_regular_file_exists(path):
        return None
    if not private_regular(path):
        raise PolicyViolation("Evolution bootstrap intent ACL/identity invalid")
    try:
        document = json.loads(_read_bounded_bytes(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("Evolution bootstrap intent okunamadi") from exc
    if not isinstance(document, dict) or document.get("plan_digest") != authorized_plan_digest:
        raise PolicyViolation("Evolution bootstrap intent digest binding invalid")
    body = {key: value for key, value in document.items() if key != "plan_digest"}
    if digest(body) != authorized_plan_digest:
        raise PolicyViolation("Evolution bootstrap intent content digest drift")
    grant = _grant_from_body(body.get("grant"))
    if grant.grant_digest != body.get("grant_digest"):
        raise PolicyViolation("Evolution bootstrap intent grant digest drift")
    return EvolutionBootstrapPlan(body=body, grant=grant)


def _verify_bootstrap_authority(
    context: ApplicationContext,
    plan: EvolutionBootstrapPlan,
    *,
    now: dt.datetime,
) -> WindowsTaskPlan:
    approval = plan.body.get("approval_receipt")
    operational = plan.body.get("operational")
    if not isinstance(approval, dict) or not isinstance(operational, dict):
        raise PolicyViolation("Evolution bootstrap authority body invalid")
    issued = _timestamp(approval.get("issued_at"), "approval-issued")
    approval_expires = _timestamp(approval.get("expires_at"), "approval-expires")
    if not (issued <= now < approval_expires):
        raise PolicyViolation("Evolution bootstrap owner approval current degil")
    if not (plan.grant.valid_from <= now < plan.grant.review_after <= plan.grant.expires_at):
        raise PolicyViolation("Evolution bootstrap grant validity/review current degil")
    device_id, owner_id, actor_id = _device_identity()
    if (
        plan.grant.device_id != device_id
        or plan.grant.owner_id != owner_id
        or plan.grant.actor_id != actor_id
        or tuple(map(str, (plan.grant.owner_id, plan.grant.realm_id)))
        != (str(approval.get("owner_id")), str(approval.get("realm_id")))
        or device_id != approval.get("device_id")
        or str(owner_id) != approval.get("approver_id")
        or plan.grant.grant_digest != approval.get("grant_digest")
        or digest(approval) != plan.body.get("approval_receipt_digest")
    ):
        raise PolicyViolation("Evolution bootstrap owner/device/realm approval drift")
    database = context.settings.database.sqlite_path(context.home)
    projects = _operational_inventory(database)
    if (
        tuple(sorted((item[0] for item in projects), key=str)) != plan.grant.project_ids
        or tuple(sorted(item[1] for item in projects)) != plan.grant.logical_sources
    ):
        raise PolicyViolation("Evolution bootstrap current project scope drift")
    realm_id, missing = _realm_scope(database, projects)
    planned_missing = operational.get("realm_bindings_to_add")
    if (
        realm_id != plan.grant.realm_id
        or str(realm_id) != operational.get("realm_id")
        or not isinstance(planned_missing, list)
        or not set(map(str, missing)).issubset(set(map(str, planned_missing)))
    ):
        raise PolicyViolation("Evolution bootstrap current realm scope drift")
    current = build_evolution_plan(context).get("admission_bindings")
    expected = {
        name: getattr(plan.grant, name)
        for name in (
            "task_scope_digest",
            "policy_digest",
            "verifier_digest",
            "validator_digest",
            "source_lineage_digest",
            "protected_manifest_digest",
            "dependency_manifest_digest",
        )
    }
    if current != expected:
        raise PolicyViolation("Evolution bootstrap current seven-binding drift")
    scheduler_plan = build_windows_supervisor_plan(context)
    if scheduler_plan.plan_digest != plan.body.get("supervisor_plan_digest"):
        raise PolicyViolation("Evolution bootstrap supervisor plan drift")
    return scheduler_plan


def _assert_current_bootstrap_claim(
    store: SQLiteLocalImprovementStore,
    plan_digest: str,
    claim_event_digest: str,
) -> None:
    current = store.evolution_control_status()
    if not (
        current.get("state") == "paused"
        and current.get("reason") == "bootstrap-claim"
        and current.get("enable_plan_digest") == plan_digest
        and current.get("event_digest") == claim_event_digest
    ):
        raise PolicyViolation("Evolution bootstrap exact claim no longer current")


class ProductionEvolutionMigrationAdmission:
    """Fence supervised ticks and prove operational workers quiescent for v5 migration."""

    def __init__(
        self,
        context: ApplicationContext,
        *,
        plan_digest: str,
        claim_event_digest: str,
    ) -> None:
        self._context = context
        self._plan_digest = plan_digest
        self._claim_event_digest = claim_event_digest
        database = context.settings.database.sqlite_path(context.home)
        self._runtime = SQLiteLocalRuntimeStore(database, existing_only=True)
        self._control = SQLiteLocalImprovementStore(
            context.home / "state" / "improvement.db",
            context.home / "state" / "learning.db",
            context.home / "benchmarklar" / "benchmark.db",
        )

    def trusted_home(self) -> Path:
        return self._context.home

    def stop_new_admission(self) -> None:
        current = self._control.evolution_control_status()
        if current["state"] == "observing":
            self._control.set_evolution_control_state(
                "paused",
                reason="operational-v5-migration",
                now=dt.datetime.now(dt.UTC),
            )

    def drain_and_reap(self) -> None:
        self._runtime.recover_expired()
        self._runtime.recover_outbox()

    def assert_no_admitted_authority(self) -> None:
        _assert_current_bootstrap_claim(
            self._control, self._plan_digest, self._claim_event_digest
        )
        control = self._control.evolution_control_status()
        runtime = self._runtime.status()
        if control["state"] not in {"paused", "disabled"}:
            raise PolicyViolation("Operational v5 migration tick admission acik")
        if any(
            (
                runtime.running_jobs,
                runtime.recovery_jobs,
                runtime.claimed_outbox,
                runtime.recovery_outbox,
                runtime.open_recovery_cases,
            )
        ):
            raise PolicyViolation("Operational v5 migration runtime quiescent degil")

    def release_admission(self) -> None:
        # Deliberately remain paused until v5, grant and supervisor exact readback all pass.
        return

    def mark_recovery_required(self) -> None:
        current = self._control.evolution_control_status()
        if current["state"] != "disabled":
            self._control.set_evolution_control_state(
                "disabled",
                reason="operational-v5-recovery",
                now=dt.datetime.now(dt.UTC),
            )


def _spool_targets(database: Path, home: Path) -> tuple[MigrationSpoolTarget, ...]:
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "select session_id,client_id,external_session_id "
            "from continuity_session_binding order by session_id"
        ).fetchall()
    return tuple(
        MigrationSpoolTarget(home, str(row[1]), str(row[0]), str(row[2])) for row in rows
    )


def apply_evolution_bootstrap(
    context: ApplicationContext, *, authorized_plan_digest: str
) -> dict[str, Any]:
    """Apply only the freshly re-read exact bootstrap plan and return terminal readback."""

    plan = _load_intent(context, authorized_plan_digest)
    if plan is None:
        plan = build_evolution_bootstrap_plan(context)
        if authorized_plan_digest != plan.plan_digest:
            raise PolicyViolation("Evolution bootstrap exact plan authorization ister")
        _persist_intent(context, plan)
    control_store = SQLiteLocalImprovementStore(
        context.home / "state" / "improvement.db",
        context.home / "state" / "learning.db",
        context.home / "benchmarklar" / "benchmark.db",
    )
    control_before = control_store.evolution_control_status()
    if (
        control_before.get("state") == "observing"
        and control_before.get("reason") == "bootstrap-complete"
        and control_before.get("enable_plan_digest") == plan.plan_digest
    ):
        persisted_settlement = control_before.get("bootstrap_settlement")
        if not isinstance(persisted_settlement, dict):
            raise PolicyViolation("Evolution bootstrap terminal settlement missing")
        settlement_body = dict(persisted_settlement)
        settlement_digest = settlement_body.pop("receipt_digest", None)
        if not isinstance(settlement_digest, str) or digest(settlement_body) != settlement_digest:
            raise PolicyViolation("Evolution bootstrap terminal settlement drift")
        return dict(persisted_settlement) | {
            "control": control_before | {"changed": False},
        }
    exact_outstanding_claim = (
        control_before.get("state") == "paused"
        and control_before.get("reason") == "bootstrap-claim"
        and control_before.get("enable_plan_digest") == plan.plan_digest
    )
    current_precondition = {
        "state": control_before.get("state"),
        "ordinal": control_before.get("ordinal"),
        "event_digest": control_before.get("event_digest"),
    }
    if not exact_outstanding_claim and current_precondition != plan.body.get(
        "control_precondition"
    ):
        raise PolicyViolation("Evolution bootstrap later owner control decision blocks intent")
    moment = dt.datetime.now(dt.UTC)
    scheduler_plan = _verify_bootstrap_authority(context, plan, now=moment)
    if not exact_outstanding_claim:
        claim = control_store.set_evolution_control_state(
            "paused",
            reason="bootstrap-claim",
            now=moment,
            enable_plan_digest=plan.plan_digest,
            expected_previous_event_digest=(
                None
                if control_before.get("event_digest") is None
                else str(control_before["event_digest"])
            ),
        )
        claim_event_digest = str(claim["event_digest"])
    else:
        claim_event_digest = str(control_before["event_digest"])
    _assert_current_bootstrap_claim(control_store, plan.plan_digest, claim_event_digest)
    database = context.settings.database.sqlite_path(context.home)
    operational = plan.body["operational"]
    if not isinstance(operational, dict):
        raise PolicyViolation("Evolution bootstrap operational intent invalid")
    migration_receipt: dict[str, Any] = {"state": "already-v5"}
    if operational["migration_required"]:
        source_digest = str(operational["source_logical_digest"])
        backup = context.home / "backups" / (
            f"operational-v3-before-v5-{source_digest[7:19]}.db"
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        restrict_private_tree(backup.parent)
        current = operational_status(database)
        if current.schema_version == 3:
            if logical_database_digest(database) != source_digest:
                raise PolicyViolation("Evolution bootstrap pre-migration source drift")
            receipt = migrate_v3_to_v5(
                database,
                backup,
                migration_lock=context.home / "runtime" / "operational-v5-migration.lock",
                admission=ProductionEvolutionMigrationAdmission(
                    context,
                    plan_digest=plan.plan_digest,
                    claim_event_digest=claim_event_digest,
                ),
                spool_targets=_spool_targets(database, context.home),
            )
            migration_receipt = {
                "state": "migrated",
                "schema_version": receipt.status.schema_version,
                "schema_ok": receipt.status.schema_ok,
                "backup_ref": f"ZEKAM_HOME/backups/{backup.name}",
                "source_v3_logical_digest": receipt.source_v3_logical_digest,
                "source_v3_original_digest": receipt.source_v3_original_digest,
            }
        elif current.schema_version == 5:
            if not backup.is_file() or logical_database_digest(backup) != source_digest:
                raise PolicyViolation("Evolution bootstrap migrated backup readback drift")
            migration_receipt = {
                "state": "already-migrated",
                "schema_version": 5,
                "schema_ok": current.schema_ok,
                "backup_ref": f"ZEKAM_HOME/backups/{backup.name}",
                "source_v3_logical_digest": source_digest,
            }
        else:
            raise PolicyViolation("Evolution bootstrap migration recovery-required")
    _assert_current_bootstrap_claim(control_store, plan.plan_digest, claim_event_digest)
    approval = plan.body["approval_receipt"]
    if not isinstance(approval, dict):
        raise PolicyViolation("Evolution bootstrap approval intent invalid")
    approval_digest = str(plan.body["approval_receipt_digest"])
    _assert_current_bootstrap_claim(control_store, plan.plan_digest, claim_event_digest)
    with sqlite3.connect(database) as connection:
        connection.execute("pragma foreign_keys=on")
        realm_id = str(operational["realm_id"])
        additions = operational["realm_bindings_to_add"]
        if not isinstance(additions, list):
            raise PolicyViolation("Evolution bootstrap realm binding intent invalid")
        for project_id in additions:
            existing_realm = connection.execute(
                "select realm_id from project_knowledge_realm where project_id=?",
                (str(project_id),),
            ).fetchone()
            if existing_realm is None:
                project_exists = connection.execute(
                    "select 1 from project where id=?", (str(project_id),)
                ).fetchone()
                if project_exists is None:
                    raise PolicyViolation("Evolution bootstrap realm project missing")
                connection.execute(
                    "insert into project_knowledge_realm values(?,?,?)",
                    (
                        str(project_id),
                        realm_id,
                        _timestamp(approval["issued_at"], "approval-issued").isoformat(),
                    ),
                )
            elif str(existing_realm[0]) != realm_id:
                raise PolicyViolation("Evolution bootstrap realm binding replay drift")
        scoped_rows = connection.execute(
            "select project_id,realm_id from project_knowledge_realm where project_id in "
            f"({','.join('?' for _ in plan.grant.project_ids)})",
            tuple(map(str, plan.grant.project_ids)),
        ).fetchall()
        expected_scope = {(str(value), realm_id) for value in plan.grant.project_ids}
        if {(str(row[0]), str(row[1])) for row in scoped_rows} != expected_scope:
            raise PolicyViolation("Evolution bootstrap grant/realm scope drift")
        existing = connection.execute(
            "select grant_digest,owner_id,realm_id,device_id,approver_id,issued_at,expires_at "
            "from evolution_grant_approval where receipt_digest=?",
            (approval_digest,),
        ).fetchone()
        approval_values = (
            str(approval["grant_digest"]),
            str(approval["owner_id"]),
            str(approval["realm_id"]),
            str(approval["device_id"]),
            str(approval["approver_id"]),
            _timestamp(approval["issued_at"], "approval-issued").isoformat(),
            _timestamp(approval["expires_at"], "approval-expires").isoformat(),
        )
        if existing is None:
            connection.execute(
                "insert into evolution_grant_approval values(?,?,?,?,?,?,?,?)",
                (approval_digest, *approval_values),
            )
        elif tuple(map(str, existing)) != approval_values:
            raise PolicyViolation("Evolution bootstrap approval receipt replay drift")
        connection.commit()
        ledger = SQLiteEvolutionAuthorityLedger(
            connection, SQLiteEvolutionApprovalAuthority(connection)
        )
        registered = connection.execute(
            "select 1 from evolution_standing_grant where grant_digest=?",
            (plan.grant.grant_digest,),
        ).fetchone()
        if registered is None:
            ledger.register_grant(
                plan.grant,
                approval_receipt_digest=approval_digest,
                now=dt.datetime.now(dt.UTC),
            )
    _assert_current_bootstrap_claim(control_store, plan.plan_digest, claim_event_digest)
    supervisor_receipt = install_windows_task(
        scheduler_plan,
        authorized_plan_digest=scheduler_plan.plan_digest,
    )
    _assert_current_bootstrap_claim(control_store, plan.plan_digest, claim_event_digest)
    evidence = build_resume_admission(context)
    if evidence is None:
        raise PolicyViolation("Evolution bootstrap post-install admission readback basarisiz")
    settlement_body = {
        "schema": "zekam-evolution-bootstrap-receipt/v1",
        "plan_digest": plan.plan_digest,
        "migration": migration_receipt,
        "grant_digest": plan.grant.grant_digest,
        "supervisor": supervisor_receipt,
        "admission_evidence_digest": evidence["evidence_digest"],
        "provider_calls": 0,
        "network_scope": "none",
    }
    settlement = settlement_body | {"receipt_digest": digest(settlement_body)}
    control = control_store.set_evolution_control_state(
        "observing",
        reason="bootstrap-complete",
        now=dt.datetime.now(dt.UTC),
        enable_plan_digest=plan.plan_digest,
        admission_evidence=evidence,
        admission_verifier=CurrentEvolutionResumeVerifier(context),
        bootstrap_settlement=settlement,
        expected_previous_event_digest=claim_event_digest,
    )
    return settlement | {"control": control}
