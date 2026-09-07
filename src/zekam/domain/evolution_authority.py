"""Narrow parent grants and exact one-shot authority for evolution runs.

This module is deliberately effect-free.  A standing grant is admission input,
never an executable authorization.  Every admitted run receives a child
``Authorization`` already used by the security domain.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.improvement_policy import ImprovementChangeClass
from zekam.domain.security import (
    NEVER_OUTBOUND,
    Authorization,
    AuthorizationScope,
    DataClassification,
)

_TOKEN: Final = re.compile(r"^[a-z0-9][a-z0-9._:/+-]{0,255}$")
_DIGEST_FIELDS: Final = (
    "task_scope_digest",
    "policy_digest",
    "verifier_digest",
    "validator_digest",
    "source_lineage_digest",
    "protected_manifest_digest",
    "dependency_manifest_digest",
)
PROTECTED_WRITE_RESOURCES: Final = frozenset(
    {
        "approval",
        "approval-policy",
        "authorization",
        "control-plane",
        "evaluator-holdout",
        "receipt",
        "retention-policy",
        "root-instruction",
        "rollback-executor",
        "scheduler",
        "schema",
        "secret",
        "security-policy",
    }
)
PROTECTED_SOURCE_FRAGMENTS: Final = frozenset(
    {
        "/active_task_contract.py",
        "/authorization",
        "/evolution_authority.py",
        "/local_runtime",
        "/operational_migration.py",
        "/operational_schema.py",
        "/rollback",
        "/scheduler",
        "/secret",
        "/security.py",
    }
)


@dataclass(frozen=True, slots=True)
class EvolutionHandlerContract:
    operation: str
    version: str
    readable_resources: frozenset[str]
    writable_resources: frozenset[str]
    idempotency: str = "exact-effect-digest"

    @property
    def input_schema(self) -> dict[str, Any]:
        required = (
            "schema",
            "operation",
            "handler_version",
            "handler_contract_digest",
            "input_digest",
            "candidate_digest",
            "fixture_digest",
            "budget_digest",
            "readable_resources",
            "writable_resources",
        )
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(required),
            "digestFields": [
                "handler_contract_digest",
                "input_digest",
                "candidate_digest",
                "fixture_digest",
                "budget_digest",
            ],
        }

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "evidence_digest", "usage", "resource_digests"],
            "status": ["completed", "failed", "unknown"],
        }

    @property
    def contract_digest(self) -> str:
        return digest(
            {
                "operation": self.operation,
                "version": self.version,
                "input_schema": self.input_schema,
                "output_schema": self.output_schema,
                "readable_resources": sorted(self.readable_resources),
                "writable_resources": sorted(self.writable_resources),
                "idempotency": self.idempotency,
            }
        )

    def validate_input(self, body: dict[str, Any]) -> None:
        if set(body) != set(self.input_schema["required"]):
            raise ValidationFailed("Evolution handler input schema mismatch")
        for field in self.input_schema["digestFields"]:
            parse_digest(body[field])
        if (
            body["operation"] != self.operation
            or body["handler_version"] != self.version
            or body["handler_contract_digest"] != self.contract_digest
            or body["schema"] != f"zekam-{self.operation}-effect/v1"
            or body["readable_resources"] != sorted(body["readable_resources"])
            or body["writable_resources"] != sorted(body["writable_resources"])
        ):
            raise ValidationFailed("Evolution handler exact input contract mismatch")

    def validate_output(
        self,
        *,
        status: str,
        evidence_digest: str,
        usage: EvolutionBudget,
        resource_digests: tuple[str, ...],
    ) -> None:
        if status not in self.output_schema["status"]:
            raise ValidationFailed("Evolution handler output status mismatch")
        parse_digest(evidence_digest)
        if tuple(sorted(set(resource_digests))) != resource_digests:
            raise ValidationFailed("Evolution output resource digests noncanonical")
        for value in resource_digests:
            parse_digest(value)
        usage.body()


def _handler(
    operation: str, reads: tuple[str, ...], writes: tuple[str, ...]
) -> EvolutionHandlerContract:
    return EvolutionHandlerContract(
        operation,
        f"{operation}/v1",
        frozenset(reads),
        frozenset(writes),
    )


EVOLUTION_HANDLERS: Final = {
    item.operation: item
    for item in (
        _handler("continuity.capture", ("client-event",), ("local-capture",)),
        _handler("continuity.summarize", ("local-capture",), ("local-checkpoint",)),
        _handler(
            "knowledge.compile",
            ("local-capture", "local-checkpoint"),
            ("local-index", "local-report"),
        ),
        _handler("knowledge.reconcile", ("local-index", "local-report"), ("local-projection",)),
        _handler("learning.reflect", ("failure-card", "local-report"), ("lesson",)),
        _handler("skill.propose", ("lesson",), ("skill-draft",)),
        _handler("skill.evaluate", ("evaluation-fixture", "skill-draft"), ("skill-evaluation",)),
        _handler("improvement.plan", ("failure-card", "lesson"), ("improvement-candidate",)),
        _handler(
            "improvement.evaluate",
            ("evaluation-fixture", "improvement-candidate"),
            ("improvement-evaluation",),
        ),
        _handler(
            "improvement.shadow",
            ("improvement-candidate", "improvement-evaluation"),
            ("rollout-shadow",),
        ),
        _handler(
            "improvement.canary", ("improvement-candidate", "rollout-shadow"), ("rollout-canary",)
        ),
        _handler("improvement.activate", ("rollout-canary",), ("rollout-pointer",)),
        _handler("improvement.rollback", ("rollout-pointer",), ("rollout-pointer",)),
        _handler(
            "maintenance.reconcile",
            ("local-registry",),
            ("local-cache", "local-index", "local-projection", "local-registry", "local-report"),
        ),
        _handler("sources.refresh", ("public-source",), ("source-candidate",)),
        _handler("report.daily", ("local-checkpoint", "local-report"), ("local-report",)),
    )
}
EVOLUTION_HANDLER_VERSIONS: Final = {
    operation: contract.version for operation, contract in EVOLUTION_HANDLERS.items()
}
KNOWN_LOGICAL_RESOURCES: Final = frozenset(
    PROTECTED_WRITE_RESOURCES
    | frozenset(
        resource
        for contract in EVOLUTION_HANDLERS.values()
        for resource in contract.readable_resources | contract.writable_resources
    )
)


def evolution_effect_digest(
    *,
    operation: str,
    handler_version: str,
    input_digest: str,
    candidate_digest: str,
    fixture_digest: str,
    budget_digest: str,
    readable_resources: tuple[str, ...],
    writable_resources: tuple[str, ...],
) -> str:
    return digest(
        evolution_effect_body(
            operation=operation,
            handler_version=handler_version,
            input_digest=input_digest,
            candidate_digest=candidate_digest,
            fixture_digest=fixture_digest,
            budget_digest=budget_digest,
            readable_resources=readable_resources,
            writable_resources=writable_resources,
        )
    )


def evolution_effect_body(
    *,
    operation: str,
    handler_version: str,
    input_digest: str,
    candidate_digest: str,
    fixture_digest: str,
    budget_digest: str,
    readable_resources: tuple[str, ...],
    writable_resources: tuple[str, ...],
) -> dict[str, Any]:
    contract = EVOLUTION_HANDLERS.get(operation)
    if contract is None:
        raise ValidationFailed("Unknown evolution operation")
    return {
        "schema": f"zekam-{operation}-effect/v1",
        "operation": operation,
        "handler_version": handler_version,
        "handler_contract_digest": contract.contract_digest,
        "input_digest": input_digest,
        "candidate_digest": candidate_digest,
        "fixture_digest": fixture_digest,
        "budget_digest": budget_digest,
        "readable_resources": list(readable_resources),
        "writable_resources": list(writable_resources),
    }


class StandingGrantState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"


class StandingGrantKind(StrEnum):
    EVOLUTION = "evolution"
    CODE_MAINTENANCE = "code-maintenance"


class ExecutionBoundary(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


@dataclass(frozen=True, slots=True)
class EvolutionBudget:
    """Hard limits reserved before any child effect may start."""

    provider_calls: int
    tokens: int
    duration_seconds: int
    cost_micros: int
    disk_bytes: int
    concurrency: int

    def __post_init__(self) -> None:
        values = self.body()
        for name, value in values.items():
            if type(value) is not int or value < 0:
                raise ValidationFailed(f"Evolution budget {name} negatif olmayan integer olmali")
        if self.duration_seconds == 0 or self.disk_bytes == 0 or self.concurrency == 0:
            raise ValidationFailed("Evolution sure, disk ve concurrency limitleri pozitif olmali")

    def covers(self, requested: EvolutionBudget) -> bool:
        return all(self.body()[key] >= value for key, value in requested.body().items())

    def body(self) -> dict[str, int]:
        return {
            "provider_calls": self.provider_calls,
            "tokens": self.tokens,
            "duration_seconds": self.duration_seconds,
            "cost_micros": self.cost_micros,
            "disk_bytes": self.disk_bytes,
            "concurrency": self.concurrency,
        }


def _exact_tokens(values: tuple[str, ...], label: str, *, allow_empty: bool = False) -> None:
    if not values and not allow_empty:
        raise ValidationFailed(f"Standing grant {label} bos olamaz")
    if tuple(sorted(set(values))) != values:
        raise ValidationFailed(f"Standing grant {label} sirali ve benzersiz olmali")
    if any(not _TOKEN.fullmatch(value) or "*" in value for value in values):
        raise ValidationFailed(f"Standing grant {label} exact canonical tokenlar olmali")


@dataclass(frozen=True, slots=True)
class StandingGrant:
    """Immutable, narrow parent envelope. It cannot execute an effect itself."""

    grant_id: UUID
    revision: int
    kind: StandingGrantKind
    owner_id: UUID
    actor_id: UUID
    device_id: str
    realm_id: UUID
    project_ids: tuple[UUID, ...]
    logical_sources: tuple[str, ...]
    operations: tuple[str, ...]
    handler_versions: tuple[str, ...]
    change_classes: tuple[ImprovementChangeClass, ...]
    readable_resources: tuple[str, ...]
    writable_resources: tuple[str, ...]
    model_refs: tuple[str, ...]
    provider_refs: tuple[str, ...]
    data_classifications: tuple[DataClassification, ...]
    network_scopes: tuple[str, ...]
    execution_boundary: ExecutionBoundary
    task_scope_digest: str
    policy_digest: str
    verifier_digest: str
    validator_digest: str
    source_lineage_digest: str
    protected_manifest_digest: str
    dependency_manifest_digest: str
    budget: EvolutionBudget
    valid_from: dt.datetime
    expires_at: dt.datetime
    review_after: dt.datetime
    rollback_required: bool
    notify_on_failure: bool
    state: StandingGrantState = StandingGrantState.ACTIVE

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValidationFailed("Standing grant revision pozitif olmali")
        _exact_tokens((self.device_id,), "device_id")
        canonical_projects = tuple(sorted(set(self.project_ids), key=str))
        if not self.project_ids or canonical_projects != self.project_ids:
            raise ValidationFailed("Standing grant project scope sirali ve benzersiz olmali")
        for field in (
            "logical_sources",
            "operations",
            "handler_versions",
            "readable_resources",
            "writable_resources",
            "model_refs",
            "provider_refs",
        ):
            _exact_tokens(getattr(self, field), field)
        if any(operation not in EVOLUTION_HANDLER_VERSIONS for operation in self.operations):
            raise ValidationFailed("Standing grant unknown evolution operation iceriyor")
        expected_versions = tuple(
            sorted(EVOLUTION_HANDLER_VERSIONS[operation] for operation in self.operations)
        )
        if self.handler_versions != expected_versions:
            raise ValidationFailed("Standing grant operation/handler version binding drift")
        contracts = tuple(EVOLUTION_HANDLERS[operation] for operation in self.operations)
        allowed_reads = frozenset(
            resource for contract in contracts for resource in contract.readable_resources
        )
        allowed_writes = frozenset(
            resource for contract in contracts for resource in contract.writable_resources
        )
        if not set(self.readable_resources).issubset(allowed_reads):
            raise PolicyViolation("Standing grant read resource typed handler contract disinda")
        non_source_writes = {
            resource
            for resource in self.writable_resources
            if not resource.startswith("source-file:")
        }
        if not non_source_writes.issubset(allowed_writes):
            raise PolicyViolation("Standing grant write resource typed handler contract disinda")
        _exact_tokens(self.network_scopes, "network_scopes", allow_empty=True)
        canonical_classes = tuple(sorted(set(self.change_classes), key=lambda value: value.value))
        if canonical_classes != self.change_classes:
            raise ValidationFailed("Standing grant change classes sirali ve benzersiz olmali")
        autonomous = {
            ImprovementChangeClass.AUTO_SAFE,
            ImprovementChangeClass.REVIEW_REQUIRED,
        }
        if not self.change_classes or not set(self.change_classes).issubset(autonomous):
            raise PolicyViolation("Standing grant human veya prohibited change class tasiyamaz")
        canonical_data = tuple(
            sorted(set(self.data_classifications), key=lambda value: value.value)
        )
        if canonical_data != self.data_classifications:
            raise ValidationFailed("Standing grant data classifications sirali ve benzersiz olmali")
        if not self.data_classifications:
            raise ValidationFailed("Standing grant data classification scope bos olamaz")
        if PROTECTED_WRITE_RESOURCES.intersection(self.writable_resources):
            raise PolicyViolation("Standing grant protected control-plane kaynagina yazamaz")
        source_writes = tuple(
            resource for resource in self.writable_resources if resource.startswith("source-file:")
        )
        if any(
            fragment in resource.removeprefix("source-file:")
            for resource in source_writes
            for fragment in PROTECTED_SOURCE_FRAGMENTS
        ):
            raise PolicyViolation("Code-maintenance grant protected source dependency yazamaz")
        if self.kind is StandingGrantKind.EVOLUTION and source_writes:
            raise PolicyViolation("Evolution grant kaynak koduna yazamaz")
        if self.kind is StandingGrantKind.CODE_MAINTENANCE and (
            self.operations != ("maintenance.reconcile",)
            or not source_writes
            or len(source_writes) != len(self.writable_resources)
        ):
            raise PolicyViolation("Code-maintenance grant exact source files ister")
        if self.execution_boundary is ExecutionBoundary.LOCAL and self.network_scopes:
            raise PolicyViolation("Local grant external network scope tasiyamaz")
        if self.execution_boundary is ExecutionBoundary.REMOTE and (
            not self.network_scopes or set(self.data_classifications).intersection(NEVER_OUTBOUND)
        ):
            raise PolicyViolation("Remote grant network scope ve outbound-safe data ister")
        for field in _DIGEST_FIELDS:
            parse_digest(getattr(self, field))
        if self.expires_at <= self.valid_from or not (
            self.valid_from <= self.review_after <= self.expires_at
        ):
            raise ValidationFailed("Standing grant validity/review araligi gecersiz")
        if type(self.rollback_required) is not bool or type(self.notify_on_failure) is not bool:
            raise ValidationFailed("Standing grant rollout bayraklari boolean olmali")

    def body(self) -> dict[str, Any]:
        return {
            "grant_id": str(self.grant_id),
            "revision": self.revision,
            "kind": self.kind.value,
            "owner_id": str(self.owner_id),
            "actor_id": str(self.actor_id),
            "device_id": self.device_id,
            "realm_id": str(self.realm_id),
            "project_ids": [str(value) for value in self.project_ids],
            "logical_sources": list(self.logical_sources),
            "operations": list(self.operations),
            "handler_versions": list(self.handler_versions),
            "handler_contract_digests": [
                EVOLUTION_HANDLERS[operation].contract_digest for operation in self.operations
            ],
            "change_classes": [value.value for value in self.change_classes],
            "readable_resources": list(self.readable_resources),
            "writable_resources": list(self.writable_resources),
            "model_refs": list(self.model_refs),
            "provider_refs": list(self.provider_refs),
            "data_classifications": [value.value for value in self.data_classifications],
            "network_scopes": list(self.network_scopes),
            "execution_boundary": self.execution_boundary.value,
            **{field: getattr(self, field) for field in _DIGEST_FIELDS},
            "budget": self.budget.body(),
            "valid_from": self.valid_from,
            "expires_at": self.expires_at,
            "review_after": self.review_after,
            "rollback_required": self.rollback_required,
            "notify_on_failure": self.notify_on_failure,
            "state": self.state.value,
        }

    @property
    def grant_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class EvolutionRunPlan:
    """Exact immutable request from which one child authorization is derived."""

    parent_grant_digest: str
    project_id: UUID
    logical_source: str
    operation: str
    handler_version: str
    change_class: ImprovementChangeClass
    readable_resources: tuple[str, ...]
    writable_resources: tuple[str, ...]
    model_ref: str
    provider_ref: str
    data_classifications: tuple[DataClassification, ...]
    network_scope: str | None
    execution_boundary: ExecutionBoundary
    task_scope_digest: str
    policy_digest: str
    verifier_digest: str
    validator_digest: str
    source_lineage_digest: str
    protected_manifest_digest: str
    dependency_manifest_digest: str
    input_digest: str
    candidate_digest: str
    fixture_digest: str
    budget_digest: str
    effect_digest: str
    budget: EvolutionBudget
    executor_ref: str
    verifier_ref: str
    review_receipt_digest: str | None
    requested_at: dt.datetime
    deadline: dt.datetime

    def __post_init__(self) -> None:
        for label, value in (
            ("logical_source", self.logical_source),
            ("operation", self.operation),
            ("handler_version", self.handler_version),
            ("model_ref", self.model_ref),
            ("provider_ref", self.provider_ref),
            ("executor_ref", self.executor_ref),
            ("verifier_ref", self.verifier_ref),
        ):
            _exact_tokens((value,), f"run {label}")
        if self.network_scope is not None:
            _exact_tokens((self.network_scope,), "run network_scope")
        if self.execution_boundary is ExecutionBoundary.LOCAL and self.network_scope is not None:
            raise PolicyViolation("Local run external network scope tasiyamaz")
        if self.execution_boundary is ExecutionBoundary.REMOTE and (
            self.network_scope is None
            or set(self.data_classifications).intersection(NEVER_OUTBOUND)
        ):
            raise PolicyViolation("Remote run network scope ve outbound-safe data ister")
        for value in (
            self.parent_grant_digest,
            self.task_scope_digest,
            self.policy_digest,
            self.verifier_digest,
            self.validator_digest,
            self.source_lineage_digest,
            self.protected_manifest_digest,
            self.input_digest,
            self.candidate_digest,
            self.fixture_digest,
            self.budget_digest,
            self.effect_digest,
        ):
            parse_digest(value)
        if self.review_receipt_digest is not None:
            parse_digest(self.review_receipt_digest)
        if self.budget_digest != digest(self.budget.body()):
            raise ValidationFailed("Run plan budget digest drift")
        if self.deadline <= self.requested_at:
            raise ValidationFailed("Run plan deadline gecersiz")
        if self.executor_ref == self.verifier_ref:
            raise PolicyViolation("Executor ve verifier bagimsiz olmali")
        _exact_tokens(self.readable_resources, "run readable_resources")
        _exact_tokens(self.writable_resources, "run writable_resources")
        contract = EVOLUTION_HANDLERS.get(self.operation)
        if contract is None or self.handler_version != contract.version:
            raise PolicyViolation("Run operation/handler version binding drift")
        if not set(self.readable_resources).issubset(contract.readable_resources):
            raise PolicyViolation("Run read resource typed handler contract disinda")
        source_writes = {
            resource for resource in self.writable_resources if resource.startswith("source-file:")
        }
        non_source_writes = set(self.writable_resources) - source_writes
        if not non_source_writes.issubset(contract.writable_resources):
            raise PolicyViolation("Run write resource typed handler contract disinda")
        expected_effect = evolution_effect_digest(
            operation=self.operation,
            handler_version=self.handler_version,
            input_digest=self.input_digest,
            candidate_digest=self.candidate_digest,
            fixture_digest=self.fixture_digest,
            budget_digest=self.budget_digest,
            readable_resources=self.readable_resources,
            writable_resources=self.writable_resources,
        )
        if self.effect_digest != expected_effect:
            raise PolicyViolation("Run effect digest typed payload binding drift")
        contract.validate_input(self.effect_body())

    def body(self) -> dict[str, Any]:
        return {
            "parent_grant_digest": self.parent_grant_digest,
            "project_id": str(self.project_id),
            "logical_source": self.logical_source,
            "operation": self.operation,
            "handler_version": self.handler_version,
            "change_class": self.change_class.value,
            "readable_resources": list(self.readable_resources),
            "writable_resources": list(self.writable_resources),
            "model_ref": self.model_ref,
            "provider_ref": self.provider_ref,
            "data_classifications": [value.value for value in self.data_classifications],
            "network_scope": self.network_scope,
            "execution_boundary": self.execution_boundary.value,
            **{field: getattr(self, field) for field in _DIGEST_FIELDS},
            "input_digest": self.input_digest,
            "candidate_digest": self.candidate_digest,
            "fixture_digest": self.fixture_digest,
            "budget_digest": self.budget_digest,
            "effect_digest": self.effect_digest,
            "budget": self.budget.body(),
            "executor_ref": self.executor_ref,
            "verifier_ref": self.verifier_ref,
            "review_receipt_digest": self.review_receipt_digest,
            "requested_at": self.requested_at,
            "deadline": self.deadline,
        }

    def effect_body(self) -> dict[str, Any]:
        return evolution_effect_body(
            operation=self.operation,
            handler_version=self.handler_version,
            input_digest=self.input_digest,
            candidate_digest=self.candidate_digest,
            fixture_digest=self.fixture_digest,
            budget_digest=self.budget_digest,
            readable_resources=self.readable_resources,
            writable_resources=self.writable_resources,
        )

    @property
    def plan_digest(self) -> str:
        return digest(self.body())


def derive_child_authorization(
    grant: StandingGrant,
    plan: EvolutionRunPlan,
    *,
    now: dt.datetime,
) -> Authorization:
    """Fail closed and issue an existing-domain one-shot authorization."""

    if grant.state is not StandingGrantState.ACTIVE:
        raise PolicyViolation("Standing grant active degil")
    if now < grant.valid_from or now >= grant.expires_at or now >= grant.review_after:
        raise PolicyViolation("Standing grant validity/review kapisi kapali")
    if plan.parent_grant_digest != grant.grant_digest:
        raise PolicyViolation("Run plan parent grant drift")
    exact_pairs = (
        (plan.task_scope_digest, grant.task_scope_digest, "task scope"),
        (plan.policy_digest, grant.policy_digest, "policy"),
        (plan.verifier_digest, grant.verifier_digest, "verifier"),
        (plan.validator_digest, grant.validator_digest, "validator"),
        (plan.source_lineage_digest, grant.source_lineage_digest, "source lineage"),
        (plan.protected_manifest_digest, grant.protected_manifest_digest, "protected manifest"),
        (
            plan.dependency_manifest_digest,
            grant.dependency_manifest_digest,
            "dependency manifest",
        ),
    )
    for actual, expected, label in exact_pairs:
        if actual != expected:
            raise PolicyViolation(f"Run plan {label} drift")
    checks = (
        (plan.project_id in grant.project_ids, "project"),
        (plan.logical_source in grant.logical_sources, "logical source"),
        (plan.operation in grant.operations, "operation"),
        (plan.handler_version in grant.handler_versions, "handler version"),
        (plan.change_class in grant.change_classes, "change class"),
        (set(plan.readable_resources).issubset(grant.readable_resources), "read resource"),
        (set(plan.writable_resources).issubset(grant.writable_resources), "write resource"),
        (plan.model_ref in grant.model_refs, "model"),
        (plan.provider_ref in grant.provider_refs, "provider"),
        (set(plan.data_classifications).issubset(grant.data_classifications), "data class"),
        (plan.execution_boundary is grant.execution_boundary, "execution boundary"),
        (
            (plan.network_scope is None and not grant.network_scopes)
            or (plan.network_scope is not None and plan.network_scope in grant.network_scopes),
            "network scope",
        ),
        (grant.budget.covers(plan.budget), "budget"),
        (plan.deadline <= grant.expires_at, "deadline"),
    )
    for accepted, label in checks:
        if not accepted:
            raise PolicyViolation(f"Run plan {label} standing grant disinda")
    if PROTECTED_WRITE_RESOURCES.intersection(plan.writable_resources):
        raise PolicyViolation("Run plan protected control-plane kaynagina yazamaz")
    if plan.change_class in {
        ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED,
        ImprovementChangeClass.PROHIBITED_AUTONOMOUS,
    }:
        raise PolicyViolation("Run plan autonomous admission icin yasak change class")
    if plan.change_class is ImprovementChangeClass.REVIEW_REQUIRED and (
        plan.review_receipt_digest is None or plan.verifier_ref == plan.executor_ref
    ):
        raise PolicyViolation("Review-required run bagimsiz review receipt ister")
    lifetime = min(plan.deadline, grant.expires_at, grant.review_after) - now
    if lifetime <= dt.timedelta(0):
        raise PolicyViolation("Child authorization lifetime kapali")
    resources = tuple(sorted(set(plan.readable_resources + plan.writable_resources)))
    scope = AuthorizationScope(
        allowed_resources=resources,
        allowed_read_resources=plan.readable_resources,
        allowed_write_resources=plan.writable_resources,
        allowed_effects=(plan.operation,),
        provider_refs=(plan.provider_ref,),
        data_classifications=plan.data_classifications,
    )
    return Authorization.issue(
        realm_id=grant.realm_id,
        actor_id=grant.actor_id,
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=scope,
        risk=plan.change_class.value,
        lifetime=lifetime,
        now=now,
    )
