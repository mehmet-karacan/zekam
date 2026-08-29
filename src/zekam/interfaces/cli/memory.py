"""Memory Continuity read, control and shadow-first upgrade commands."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID

import typer
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.config import core_root
from zekam.application.control_plane_completion import (
    ControlPlaneCompletionRequest,
    ControlPlaneCompletionService,
)
from zekam.application.execution import ExecutionHost
from zekam.application.governance import DEFAULT_POLICY_NAME, GovernanceService
from zekam.application.memory_continuity import HydrationPreparation, MemoryContinuityService
from zekam.application.memory_control import (
    MemoryControlOperation,
    MemoryControlService,
)
from zekam.application.memory_observability import (
    MemoryDimensionStatus,
    MemoryObservabilityService,
)
from zekam.application.memory_policy import load_memory_policy
from zekam.application.memory_upgrade import (
    MemoryFeatureMode,
    MemoryUpgradeService,
    MemoryVerificationEvidence,
    UpgradeTarget,
)
from zekam.application.obsidian_projection import (
    ObsidianApplyPlan,
    ObsidianProjectionService,
    build_obsidian_projection,
)
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import Checkpoint
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed, ZekamError
from zekam.domain.markdown_projection import ObsidianProfile, ObsidianProjectionBundle
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, FailureCategory, Job, JobKind
from zekam.domain.session_continuity import (
    CloseStatus,
    ContextOmissionReference,
    ContextSelectionReference,
    DigestReference,
    FreshnessDimension,
    SessionCloseReceipt,
    TruthClass,
)
from zekam.domain.work import (
    AcceptanceCriterion,
    EffectKind,
    EvidenceRef,
    PlanStep,
    WorkState,
    WorkType,
)
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.control_plane_completion_repository import (
    PostgresControlPlaneCompletionRepository,
)
from zekam.infrastructure.postgres.markdown_projection_repository import (
    PostgresMarkdownProjectionRepository,
)
from zekam.infrastructure.postgres.memory_continuity_repository import (
    MemoryContinuityRepository,
)
from zekam.infrastructure.postgres.memory_control_repository import (
    PostgresMemoryControlRepository,
)
from zekam.infrastructure.postgres.memory_hook_installer import PostgresMemoryHookInstaller
from zekam.infrastructure.postgres.memory_observability_repository import (
    PostgresMemoryHealthReader,
)
from zekam.infrastructure.postgres.memory_upgrade_repository import (
    PostgresMemoryUpgradeRepository,
)
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository
from zekam.infrastructure.storage.obsidian_projection_store import (
    LocalObsidianProjectionStore,
)
from zekam.interfaces.cli.session import (
    EXIT_POLICY_VIOLATION,
    HOME_HELP,
    REALM_HELP,
    RealmSession,
    fail,
    fail_from,
)

app = typer.Typer(name="memory", help="Memory Continuity Plane", no_args_is_help=True)
console = Console()

_READ_EXIT = {
    MemoryDimensionStatus.PASSED: 0,
    MemoryDimensionStatus.DEGRADED: 1,
    MemoryDimensionStatus.UNAVAILABLE: 1,
    MemoryDimensionStatus.FAILED: 2,
}


@app.command("hook-upgrade-plan")
def hook_upgrade_plan(
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Plan the exact managed hook generation upgrade without mutation."""
    try:
        with RealmSession(home, realm) as context:
            plan = PostgresMemoryHookInstaller(context.connection, context.realm_id).plan_upgrade()
    except ZekamError as exc:
        raise _raise(exc) from exc
    _emit(plan.body() | {"apply": False})


@app.command("hook-upgrade-apply")
def hook_upgrade_apply(
    expected_plan_digest: Annotated[str, typer.Option("--plan-digest")],
    project_id: Annotated[UUID, typer.Option("--project-id")],
    work_item_id: Annotated[UUID, typer.Option("--work-item-id")],
    task_plan_id: Annotated[UUID, typer.Option("--task-plan-id")],
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    actor_id: Annotated[UUID | None, typer.Option("--actor-id")] = None,
    authorize: Annotated[bool, typer.Option("--yetkilendir")] = False,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Apply one exact hook upgrade through job, claim and terminal receipt gates."""
    if not apply:
        raise fail("Hook upgrade apply --uygula ister", 64)
    try:
        with RealmSession(home, realm) as context:
            installer = PostgresMemoryHookInstaller(context.connection, context.realm_id)
            plan = installer.plan_upgrade()
            if plan.plan_digest != expected_plan_digest:
                raise PolicyViolation("Memory hook upgrade plan digest drift")
            governance = GovernanceService(
                context.connection,
                context.realm,
                actor_id=actor_id,
            )
            if authorize:
                if actor_id is None or authorization_id is not None:
                    raise ValidationFailed(
                        "Hook upgrade --yetkilendir exact --actor-id ve bos authorization ister"
                    )
                target_work = WorkGraphService(
                    context.connection, context.realm, actor_id=actor_id
                ).items.get(work_item_id)
                if target_work.project_id != project_id:
                    raise PolicyViolation("Hook upgrade target Work/project binding drift")
                graph = WorkGraphService(context.connection, context.realm, actor_id=actor_id)
                prep_work = graph.create_item(
                    project_id=project_id,
                    type=WorkType.MAINTENANCE,
                    title="Memory hook generation governed upgrade",
                    summary=(
                        "Exact code bundle revision'ini unrelated hook'lari koruyarak aktive et"
                    ),
                    acceptance_criteria=(
                        AcceptanceCriterion("Effect claim effect'ten once kalici yazilir"),
                        AcceptanceCriterion("Terminal receipt yeni generation digestini baglar"),
                    ),
                )
                graph.set_intent(
                    prep_work.id,
                    goal=f"Target Work {work_item_id} icin exact hook generation upgrade",
                    non_goals=("provider-call", "unrelated-hook-rewrite", "silent-retry"),
                    outcomes=("new-hook-generation", "terminal-effect-receipt"),
                    constraints=("max-attempts-one", "claim-before-effect"),
                )
                prep_work = graph.transition(prep_work.id, WorkState.READY)
                prep_work = graph.transition(prep_work.id, WorkState.ACTIVE)
                policy = governance.policies.current(DEFAULT_POLICY_NAME)
                if policy is None:
                    raise PolicyViolation("Hook upgrade current policy ister")
                prep_plan = graph.create_plan(
                    prep_work.id,
                    source_revision=f"memory-hook-upgrade:{plan.plan_digest}",
                    policy_digest=policy.policy_digest,
                    steps=(
                        PlanStep(
                            step_id="memory-hook-upgrade",
                            title="Exact managed memory hook generation upgrade",
                            effect=EffectKind.DATABASE_WRITE,
                            logical_resources=(plan.resource,),
                            risk="high",
                        ),
                    ),
                )
                work_item_id = prep_work.id
                task_plan_id = prep_plan.id
                authorization_id = governance.issue_authorization(
                    request=plan.effect_request,
                    actor_id=actor_id,
                    plan=prep_plan,
                    lifetime=dt.timedelta(minutes=15),
                ).id
            if authorization_id is None:
                raise AuthorizationRequired(
                    "Hook upgrade --authorization-id veya --yetkilendir ister"
                )
            authorizations = AuthorizationRepository(context.connection, context.realm_id)
            authorization = authorizations.get(authorization_id)
            graph = WorkGraphService(context.connection, context.realm, actor_id=actor_id)
            bound_work = graph.items.get(work_item_id)
            bound_plan = graph.plans.current(work_item_id)
            rejection = authorization.rejection_reason(dt.datetime.now(dt.UTC))
            if (
                rejection is not None
                or bound_work.project_id != project_id
                or bound_plan is None
                or bound_plan.id != task_plan_id
                or bound_plan.project_id != project_id
                or bound_plan.source_revision != f"memory-hook-upgrade:{plan.plan_digest}"
                or len(bound_plan.steps) != 1
                or bound_plan.steps[0].step_id != "memory-hook-upgrade"
                or bound_plan.steps[0].logical_resources != (plan.resource,)
                or authorization.work_item_id != work_item_id
                or authorization.plan_id != task_plan_id
                or authorization.plan_digest != bound_plan.plan_digest
                or authorization.effect_digest != plan.effect_digest
                or not authorization.scope.covers_effect("database-write")
                or not authorization.scope.covers_resource(plan.resource)
            ):
                raise AuthorizationRequired(
                    f"Memory hook upgrade exact authorization yok: {rejection or 'scope-mismatch'}"
                )
            capability = f"memory.hook-upgrade.{plan.plan_digest[-16:]}"
            resources = parse_requests(write=(plan.resource,))
            host = ExecutionHost(
                context.connection, context.realm_id, worker_label="memory-hook-upgrade"
            )
            job, created = host.jobs.enqueue(
                Job.create(
                    realm_id=context.realm_id,
                    project_id=project_id,
                    kind=JobKind.MUTATION,
                    idempotency_key=f"memory-hook-upgrade:{plan.plan_digest}",
                    resources=resources,
                    required_capabilities=(capability,),
                    max_attempts=1,
                    work_item_id=work_item_id,
                    plan_id=task_plan_id,
                    step_id="memory-hook-upgrade",
                )
            )
            if not created:
                raise PolicyViolation("Memory hook upgrade runtime replay reddedildi")
            claimed = host.acquire_work(capabilities=(capability,))
            if claimed is None or claimed.job.id != job.id:
                raise PolicyViolation("Memory hook upgrade job claim edilemedi")
            claim = host.claim_effect(
                claimed,
                operation="memory-hook-upgrade",
                effect_digest=plan.effect_digest,
                authorization_digest=authorization.authorization_digest,
                authorization_id=authorization.id,
                idempotency_key=plan.plan_digest,
                resources=resources,
                adapter_digest=digest(
                    {"adapter": "memory-hook-installer/v2", "plan_digest": plan.plan_digest}
                ),
            )
            effect_started = False
            receipt_known = False
            terminalization_started = False
            try:
                governance.require_authorized(
                    plan.effect_request,
                    authorization=authorization,
                    consumed_by="cli:memory:hook-upgrade-apply",
                )
                effect_started = True
                receipt = installer.ensure(installed_at=dt.datetime.now(dt.UTC))
                result = {
                    "schema": "zekam-memory-hook-upgrade-receipt/v1",
                    "job_id": str(job.id),
                    "claim_id": str(claim.id),
                    "generation": receipt.generation,
                    "hook_set_digest": receipt.hook_set_digest,
                    "bundle_digest": receipt.bundle_digest,
                    "created": receipt.created,
                    "plan_digest": plan.plan_digest,
                    "grants_authority": False,
                }
                result_digest = digest(result)
                terminal = host.record_success(
                    claim,
                    result_digest=result_digest,
                    adapter_evidence_digest=digest(
                        {"plan_digest": plan.plan_digest, "result_digest": result_digest}
                    ),
                )
                receipt_known = True
                terminal_moment = dt.datetime.now(dt.UTC)
                checkpoint = Checkpoint(
                    checkpoint_id=f"memory-hook-upgrade-{job.id}",
                    project_id=str(project_id),
                    work_item_id=str(work_item_id),
                    plan_revision_id=str(task_plan_id),
                    source_revision=bound_plan.source_revision,
                    plan_steps=("memory-hook-upgrade",),
                    completed_steps=("memory-hook-upgrade",),
                    pending_steps=(),
                    step_results=(("memory-hook-upgrade", result_digest),),
                    context_manifest_digest=plan.plan_digest,
                    journal_head_digest=terminal.adapter_evidence_digest or result_digest,
                    next_safe_action="reprepare-lifecycle-runtime-template",
                    created_at=terminal_moment,
                )
                checkpoint_id = ContextContinuityRepository(
                    context.connection,
                    context.realm_id,
                    project_id,
                    work_item_id,
                ).store_checkpoint(
                    checkpoint,
                    task_plan_id=task_plan_id,
                    job_id=job.id,
                )
                if not host.finish(
                    claimed,
                    outcome=AttemptOutcome.SUCCEEDED,
                    result_digest=result_digest,
                    now=terminal_moment,
                ):
                    raise PolicyViolation("Memory hook upgrade terminal attempt kapanmadi")
                terminalization_started = True
                prep_work = graph.items.get(work_item_id)
                graph.update_details(
                    prep_work.id,
                    acceptance_criteria=tuple(
                        AcceptanceCriterion(item.text, verified=True)
                        for item in prep_work.acceptance_criteria
                    ),
                    reason="Memory hook upgrade terminal receipt verified",
                    now=terminal_moment,
                )
                graph.transition(prep_work.id, WorkState.VERIFICATION, now=terminal_moment)
                completion = ControlPlaneCompletionService(
                    PostgresControlPlaneCompletionRepository(context.connection, context.realm_id)
                ).complete(
                    ControlPlaneCompletionRequest(
                        project_id=project_id,
                        work_item_id=work_item_id,
                        task_plan_id=task_plan_id,
                        job_id=job.id,
                        attempt_id=claimed.attempt_id,
                        checkpoint_id=checkpoint_id,
                        source_authorization_id=authorization.id,
                        source_authorization_digest=authorization.authorization_digest,
                        source_claim_id=claim.id,
                        source_claim_digest=claim.claim_digest,
                        source_effect_receipt_id=terminal.id,
                        source_operation="memory-hook-upgrade",
                        source_consumed_by="cli:memory:hook-upgrade-apply",
                        source_effect_digest=plan.effect_digest,
                        source_adapter_digest=claim.adapter_digest,
                        source_adapter_evidence_digest=(
                            terminal.adapter_evidence_digest or result_digest
                        ),
                        source_resources=(plan.resource,),
                        source_effects=(EffectKind.DATABASE_WRITE.value,),
                        source_data_classifications=("local-only",),
                        evidence=(
                            EvidenceRef(
                                kind="runtime-receipt",
                                reference=str(terminal.id),
                                digest_value=result_digest,
                            ),
                        ),
                    )
                )
                _emit(
                    result
                    | {
                        "effect_receipt_id": str(terminal.id),
                        "checkpoint_id": str(checkpoint_id),
                        "completion_receipt_id": str(completion.effect_receipt_id),
                        "preparatory_work_id": str(work_item_id),
                        "task_plan_id": str(task_plan_id),
                        "authorization_id": str(authorization.id),
                    }
                )
                return
            except Exception as exc:
                if effect_started:
                    if not terminalization_started:
                        host.jobs.mark_recovery_required(
                            job.id,
                            (
                                "memory-hook-upgrade-success-receipt-recovery"
                                if receipt_known
                                else "memory-hook-upgrade-effect-uncertain"
                            ),
                        )
                    raise
                if host.ledger.receipt_for_claim(claim.id) is None:
                    host.record_failure(
                        claim,
                        category=(
                            FailureCategory.POLICY
                            if isinstance(exc, PolicyViolation)
                            else FailureCategory.INTERNAL
                        ),
                        failure_digest=digest(
                            {"error_type": type(exc).__name__, "plan_digest": plan.plan_digest}
                        ),
                    )
                host.finish(claimed, outcome=AttemptOutcome.FAILED)
                raise
    except ZekamError as exc:
        raise _raise(exc) from exc


def _document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationFailed("Memory JSON root object olmali")
    return cast(dict[str, Any], value)


def _read_json_payload(payload: bytes, *, schema_name: str | None = None) -> dict[str, Any]:
    if len(payload) > 1024 * 1024:
        raise ValidationFailed("Memory input bounded boyutu asti")
    try:
        document = _document(json.loads(payload.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Memory input JSON okunamadi") from exc
    if schema_name is not None:
        schema_path = core_root() / "schemas" / schema_name
        schema = _document(json.loads(schema_path.read_text(encoding="utf-8")))
        try:
            Draft202012Validator(schema).validate(document)
        except ValidationError as exc:
            raise ValidationFailed("Memory receipt strict schema ile uyusmuyor") from exc
    return document


def _read_json(path: Path, *, schema_name: str | None = None) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValidationFailed("Memory input regular, bounded file olmali")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValidationFailed("Memory input JSON okunamadi") from exc
    return _read_json_payload(payload, schema_name=schema_name)


def _time(value: Any) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailed("Memory timestamp gecersiz") from exc
    if parsed.tzinfo is None:
        raise ValidationFailed("Memory timestamp timezone-aware olmali")
    return parsed


def _truth(value: Any) -> TruthClass:
    try:
        return TruthClass(str(value))
    except ValueError as exc:
        raise ValidationFailed("Memory truth class gecersiz") from exc


def _digest_ref(value: Any) -> DigestReference:
    row = _document(value)
    return DigestReference(str(row["ref"]), str(row["digest"]), _truth(row["truth_class"]))


def _selection(value: Any) -> ContextSelectionReference:
    row = _document(value)
    return ContextSelectionReference(
        str(row["ref"]),
        str(row["content_digest"]),
        int(row["token_count"]),
        _truth(row["truth_class"]),
    )


def _omission(value: Any) -> ContextOmissionReference:
    row = _document(value)
    return ContextOmissionReference(str(row["ref"]), str(row["reason_code"]), bool(row["required"]))


def _freshness(value: Any) -> FreshnessDimension:
    row = _document(value)
    return FreshnessDimension(
        str(row["name"]),
        str(row["observed_digest"]),
        str(row["expected_digest"]),
        bool(row["current"]),
    )


def _hydration_plan(service: MemoryContinuityService, path: Path, *, idempotency_key: str) -> Any:
    row = _read_json(path, schema_name="session-hydration-receipt.schema.json")
    plan = service.prepare_hydration(
        HydrationPreparation(
            receipt_id=UUID(str(row["receipt_id"])),
            realm_id=UUID(str(row["realm_id"])),
            project_id=UUID(str(row["project_id"])),
            work_item_id=UUID(str(row["work_item_id"])),
            run_id=UUID(str(row["run_id"])),
            session_id=str(row["session_id"]),
            client_id=str(row["client_id"]),
            plan_ref=str(row["plan_ref"]),
            checkpoint_ref=str(row["checkpoint_ref"]),
            source_digest=str(row["source_digest"]),
            policy_digest=str(row["policy_digest"]),
            migration_digest=str(row["migration_digest"]),
            inventory_digest=str(row["inventory_digest"]),
            context_digest=str(row["context_digest"]),
            required_candidates=tuple(_selection(item) for item in row["required_selections"]),
            optional_candidates=tuple(_selection(item) for item in row["optional_selections"]),
            known_omissions=tuple(_omission(item) for item in row["omissions"]),
            token_budget=int(row["token_budget"]),
            freshness=tuple(_freshness(item) for item in row["freshness"]),
            projection_refs=tuple(_digest_ref(item) for item in row["projection_refs"]),
            hydration_event_digest=str(row["hydration_event_digest"]),
            idempotency_key=idempotency_key,
            created_at=_time(row["created_at"]),
        )
    )
    if plan.receipt_digest != str(row["receipt_digest"]):
        raise PolicyViolation("Hydration input receipt digest drift")
    return plan


def _session_close_receipt_document(row: Mapping[str, Any]) -> SessionCloseReceipt:
    document = dict(row)

    def refs(name: str) -> tuple[DigestReference, ...]:
        return tuple(_digest_ref(item) for item in document[name])

    receipt = SessionCloseReceipt(
        receipt_id=UUID(str(document["receipt_id"])),
        realm_id=UUID(str(document["realm_id"])),
        project_id=UUID(str(document["project_id"])),
        work_item_id=UUID(str(document["work_item_id"])),
        run_id=UUID(str(document["run_id"])),
        session_id=str(document["session_id"]),
        client_id=str(document["client_id"]),
        job_id=UUID(str(document["job_id"])),
        attempt_id=UUID(str(document["attempt_id"])),
        envelope_digest=str(document["envelope_digest"]),
        fencing_token=int(document["fencing_token"]),
        completed_steps=refs("completed_steps"),
        changed_artifacts=refs("changed_artifacts"),
        verified_outcomes=refs("verified_outcomes"),
        pending_steps=refs("pending_steps"),
        next_safe_action=(
            None
            if document["next_safe_action"] is None
            else _digest_ref(document["next_safe_action"])
        ),
        human_decisions=refs("human_decisions"),
        discovered_constraints=refs("discovered_constraints"),
        failure_recovery_refs=refs("failure_recovery_refs"),
        candidate_lessons=refs("candidate_lessons"),
        candidate_skills=refs("candidate_skills"),
        checkpoint_ref=_digest_ref(document["checkpoint_ref"]),
        journal_head=_digest_ref(document["journal_head"]),
        source_digest=str(document["source_digest"]),
        policy_digest=str(document["policy_digest"]),
        migration_digest=str(document["migration_digest"]),
        context_digest=str(document["context_digest"]),
        status=CloseStatus(str(document["status"])),
        closed_at=_time(document["closed_at"]),
    )
    if receipt.receipt_digest != str(document["receipt_digest"]):
        raise PolicyViolation("Close input receipt digest drift")
    return receipt


def _session_close_receipt_from_payload(payload: bytes) -> SessionCloseReceipt:
    return _session_close_receipt_document(
        _read_json_payload(payload, schema_name="session-close-receipt.schema.json")
    )


def _session_close_receipt(path: Path) -> SessionCloseReceipt:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValidationFailed("Memory input regular, bounded file olmali")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValidationFailed("Memory input JSON okunamadi") from exc
    return _session_close_receipt_from_payload(payload)


def _close_plan(service: MemoryContinuityService, path: Path, *, idempotency_key: str) -> Any:
    receipt = _session_close_receipt(path)
    return service.prepare_close(receipt, idempotency_key=idempotency_key)


def _verification(path: Path) -> MemoryVerificationEvidence:
    row = _read_json(path)
    expected = {
        "schema",
        "verified_snapshot_digest",
        "fresh_database_digest",
        "upgrade_database_digest",
        "hook_digest",
        "security_digest",
        "continuity_digest",
        "projection_digest",
        "full_suite_digest",
        "verifier_model",
        "verifier_execution_identity",
        "builder_model",
        "builder_execution_identity",
        "verified_at",
        "passed",
        "grants_authority",
    }
    if set(row) != expected or row["schema"] != "zekam-memory-upgrade-verification/v1":
        raise ValidationFailed("Memory verification exact schema ister")
    if row["grants_authority"] is not False:
        raise PolicyViolation("Memory verification authority tasiyamaz")
    return MemoryVerificationEvidence(
        verified_snapshot_digest=str(row["verified_snapshot_digest"]),
        fresh_database_digest=str(row["fresh_database_digest"]),
        upgrade_database_digest=str(row["upgrade_database_digest"]),
        hook_digest=str(row["hook_digest"]),
        security_digest=str(row["security_digest"]),
        continuity_digest=str(row["continuity_digest"]),
        projection_digest=str(row["projection_digest"]),
        full_suite_digest=str(row["full_suite_digest"]),
        verifier_model=str(row["verifier_model"]),
        verifier_execution_identity=str(row["verifier_execution_identity"]),
        builder_model=str(row["builder_model"]),
        builder_execution_identity=str(row["builder_execution_identity"]),
        verified_at=_time(row["verified_at"]),
        passed=row["passed"] is True,
    )


def _emit(value: dict[str, Any]) -> None:
    console.print_json(json.dumps(value, ensure_ascii=False, default=str))


def _raise(exc: ZekamError) -> typer.Exit:
    if isinstance(exc, AuthorizationRequired):
        return fail(str(exc), EXIT_POLICY_VIOLATION)
    return fail_from(exc)


def _services(context: Any) -> tuple[MemoryContinuityService, MemoryControlService]:
    repository = MemoryContinuityRepository(context.connection, context.realm_id)
    authorizations = AuthorizationRepository(context.connection, context.realm_id)
    return (
        MemoryContinuityService(cast(Any, repository), authorizations),
        MemoryControlService(
            cast(Any, PostgresMemoryControlRepository(repository)), authorizations
        ),
    )


def _read_view(home: str | None, realm: str, name: str) -> None:
    try:
        with RealmSession(home, realm) as context:
            reader = PostgresMemoryHealthReader(
                context.connection,
                core_root(),
                core_root().parent / ".zekam" / "global" / "bellek",
                context.realm_id,
            )
            service = MemoryObservabilityService(reader)
            if name == "doctor":
                result = service.doctor().as_dict()
            else:
                result = cast(dict[str, Any], getattr(service, name)())
    except ZekamError as exc:
        raise _raise(exc) from exc
    _emit(result)
    status = MemoryDimensionStatus(str(result["status"]))
    code = _READ_EXIT[status]
    if code:
        raise typer.Exit(code)


@app.command("continuity-status")
def continuity_status(
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read current migration, receipt, gap and feature-mode state."""
    _read_view(home, realm, "status")


@app.command("contract-check")
def contract_check(
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read deterministic Memory Contract dimensions."""
    _read_view(home, realm, "contract_check")


@app.command("gap-report")
def gap_report(
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read canonical gap/recovery counts without repair."""
    _read_view(home, realm, "gap_report")


@app.command("compiler-shadow-report")
def compiler_shadow_report(
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read compiler watermark/backlog/quarantine shadow state."""
    _read_view(home, realm, "compiler_shadow_report")


@app.command("projection-freshness")
def projection_freshness(
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read projection receipt and legacy parity freshness."""
    _read_view(home, realm, "projection_freshness")


@app.command("doctor")
def memory_doctor(
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read all 15 structured Memory Continuity health dimensions."""
    _read_view(home, realm, "doctor")


def _apply_receipt(
    *,
    kind: str,
    input_file: Path,
    idempotency_key: str,
    authorization_id: UUID | None,
    apply: bool,
    realm: str,
    home: str | None,
) -> None:
    try:
        with RealmSession(home, realm) as context:
            continuity, _ = _services(context)
            plan = (
                _hydration_plan(continuity, input_file, idempotency_key=idempotency_key)
                if kind == "hydration"
                else _close_plan(continuity, input_file, idempotency_key=idempotency_key)
            )
            if not apply:
                _emit(plan.body() | {"plan_digest": plan.plan_digest, "apply": False})
                return
            if authorization_id is None:
                raise fail("Apply exact authorization ister", 64)
            receipt = continuity.apply(
                plan,
                authorization_id=authorization_id,
            )
    except ZekamError as exc:
        raise _raise(exc) from exc
    _emit(
        {
            "schema": "zekam-continuity-cli-apply/v1",
            "kind": receipt.kind.value,
            "receipt_digest": receipt.receipt_digest,
            "plan_digest": receipt.plan_digest,
            "created": receipt.created,
            "result_digest": receipt.result_digest,
            "grants_authority": False,
        }
    )


@app.command("hydration-apply")
def hydration_apply(
    input_file: Annotated[Path, typer.Option("--girdi", exists=True, dir_okay=False)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Prepare or apply an exact hydration receipt plan."""
    _apply_receipt(
        kind="hydration",
        input_file=input_file,
        idempotency_key=idempotency_key,
        authorization_id=authorization_id,
        apply=apply,
        realm=realm,
        home=home,
    )


@app.command("close-apply")
def close_apply(
    input_file: Annotated[Path, typer.Option("--girdi", exists=True, dir_okay=False)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Prepare or apply an exact close receipt plan."""
    _apply_receipt(
        kind="close",
        input_file=input_file,
        idempotency_key=idempotency_key,
        authorization_id=authorization_id,
        apply=apply,
        realm=realm,
        home=home,
    )


def _control(
    *,
    operation: MemoryControlOperation,
    subject_id: str,
    evidence_ref: str,
    evidence_digest: str,
    target_state: str,
    apply: bool,
    authorization_id: UUID | None,
    realm: str,
    home: str | None,
) -> None:
    try:
        with RealmSession(home, realm) as context:
            _, service = _services(context)
            plan = service.prepare(
                operation=operation,
                subject_id=subject_id,
                evidence_ref=evidence_ref,
                evidence_digest=evidence_digest,
                target_state=target_state,
            )
            if not apply:
                _emit(plan.body() | {"plan_digest": plan.plan_digest, "apply": False})
                return
            if authorization_id is None:
                raise fail("Memory control --uygula exact --authorization-id ister", 64)
            receipt = service.apply(plan, authorization_id=authorization_id)
    except ZekamError as exc:
        raise _raise(exc) from exc
    _emit(
        {
            "schema": "zekam-memory-control-cli-receipt/v1",
            "operation": receipt.operation.value,
            "subject_id": receipt.subject_id,
            "target_state": receipt.target_state,
            "plan_digest": receipt.plan_digest,
            "receipt_digest": receipt.receipt_digest,
            "created": receipt.created,
            "grants_authority": False,
        }
    )


@app.command("close-finalize")
def close_finalize(
    outbox_id: Annotated[UUID, typer.Option("--outbox-id")],
    receipt_ref: Annotated[str, typer.Option("--receipt-ref")],
    receipt_digest: Annotated[str, typer.Option("--receipt-digest")],
    status: Annotated[str, typer.Option("--status")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Prepare or finalize lifecycle outbox with a terminal receipt."""
    _control(
        operation=MemoryControlOperation.CLOSE_FINALIZE,
        subject_id=str(outbox_id),
        evidence_ref=receipt_ref,
        evidence_digest=receipt_digest,
        target_state=status,
        apply=apply,
        authorization_id=authorization_id,
        realm=realm,
        home=home,
    )


@app.command("gap-repair-apply")
def gap_repair_apply(
    gap_id: Annotated[UUID, typer.Option("--gap-id")],
    recovery_receipt_ref: Annotated[str, typer.Option("--recovery-receipt-ref")],
    recovery_receipt_digest: Annotated[str, typer.Option("--recovery-receipt-digest")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Prepare or apply an evidence-bound continuity gap repair."""
    _control(
        operation=MemoryControlOperation.GAP_REPAIR,
        subject_id=str(gap_id),
        evidence_ref=recovery_receipt_ref,
        evidence_digest=recovery_receipt_digest,
        target_state="resolved",
        apply=apply,
        authorization_id=authorization_id,
        realm=realm,
        home=home,
    )


@app.command("candidate-promote")
def candidate_promote(
    candidate_id: Annotated[str, typer.Option("--candidate-id")],
    promotion_ref: Annotated[str, typer.Option("--promotion-ref")],
    promotion_digest: Annotated[str, typer.Option("--promotion-digest")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Promote only an already reviewed compiler candidate."""
    _control(
        operation=MemoryControlOperation.CANDIDATE_PROMOTE,
        subject_id=candidate_id,
        evidence_ref=promotion_ref,
        evidence_digest=promotion_digest,
        target_state="promoted",
        apply=apply,
        authorization_id=authorization_id,
        realm=realm,
        home=home,
    )


def _upgrade(
    context: Any,
    *,
    project_id: UUID | None = None,
    work_item_id: UUID | None = None,
) -> MemoryUpgradeService:
    if (project_id is None) is not (work_item_id is None):
        raise ValidationFailed("Memory upgrade project/work binding birlikte verilmeli")
    legacy = sum(
        (core_root() / name).is_file()
        for name in ("AKTIF_GOREV.yaml", "AKTIF_GOREV.md", "GLOBAL_DOD_DURUM.md", "SURUM_RAPORU.md")
    )
    repository = PostgresMemoryUpgradeRepository(
        context.connection,
        context.realm_id,
        legacy_projection_count=legacy,
        project_id=project_id,
        work_item_id=work_item_id,
    )
    return MemoryUpgradeService(
        cast(Any, repository), AuthorizationRepository(context.connection, context.realm_id)
    )


@app.command("upgrade-detect")
def upgrade_detect(
    project_id: Annotated[UUID | None, typer.Option("--project-id")] = None,
    work_item_id: Annotated[UUID | None, typer.Option("--work-item-id")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read current migration, feature policy, projection and legacy state."""
    try:
        with RealmSession(home, realm) as context:
            snapshot = _upgrade(context, project_id=project_id, work_item_id=work_item_id).detect()
    except ZekamError as exc:
        raise _raise(exc) from exc
    _emit(snapshot.body() | {"snapshot_digest": snapshot.snapshot_digest})


def _upgrade_plan(
    *,
    target: UpgradeTarget,
    rollback_ref: str,
    rollback_digest: str,
    verification_file: Path | None,
    package_digest: str | None,
    apply: bool,
    authorization_id: UUID | None,
    project_id: UUID,
    work_item_id: UUID,
    realm: str,
    home: str | None,
) -> None:
    try:
        verification = None if verification_file is None else _verification(verification_file)
        with RealmSession(home, realm) as context:
            service = _upgrade(context, project_id=project_id, work_item_id=work_item_id)
            plan = service.check_plan(
                target=target,
                rollback_ref=rollback_ref,
                rollback_digest=rollback_digest,
                verification=verification,
                package_digest=package_digest,
            )
            if not apply:
                _emit(plan.body() | {"plan_digest": plan.plan_digest, "apply": False})
                return
            current = service.detect()
            if authorization_id is None and not (
                target is UpgradeTarget.SHADOW
                and current.mode is MemoryFeatureMode.SHADOW
                and current.required_hook_invalid_count == 0
                and current.projection_current
            ):
                raise fail("Memory upgrade --uygula exact --authorization-id ister", 64)
            receipt = service.apply(
                plan,
                authorization_id=authorization_id or UUID(int=0),
            )
    except ZekamError as exc:
        raise _raise(exc) from exc
    _emit(
        {
            "schema": "zekam-memory-upgrade-cli-receipt/v1",
            "target": receipt.target.value,
            "component_revision": receipt.component_revision,
            "mode": receipt.mode.value,
            "policy_digest": receipt.policy_digest,
            "hook_set_digest": receipt.hook_set_digest,
            "projection_receipt_digest": receipt.projection_receipt_digest,
            "receipt_digest": receipt.receipt_digest,
            "created": receipt.created,
            "grants_authority": False,
        }
    )


@app.command("upgrade-plan")
def upgrade_plan(
    target: Annotated[UpgradeTarget, typer.Option("--target")],
    rollback_ref: Annotated[str, typer.Option("--rollback-ref")],
    rollback_digest: Annotated[str, typer.Option("--rollback-digest")],
    project_id: Annotated[UUID, typer.Option("--project-id")],
    work_item_id: Annotated[UUID, typer.Option("--work-item-id")],
    verification_file: Annotated[Path | None, typer.Option("--verification-file")] = None,
    package_digest: Annotated[str | None, typer.Option("--package-digest")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Build the exact conflict/confirmation/rollback plan without mutation."""
    _upgrade_plan(
        target=target,
        rollback_ref=rollback_ref,
        rollback_digest=rollback_digest,
        verification_file=verification_file,
        package_digest=package_digest,
        apply=False,
        authorization_id=None,
        project_id=project_id,
        work_item_id=work_item_id,
        realm=realm,
        home=home,
    )


@app.command("upgrade-apply-shadow")
def upgrade_apply_shadow(
    rollback_ref: Annotated[str, typer.Option("--rollback-ref")],
    rollback_digest: Annotated[str, typer.Option("--rollback-digest")],
    project_id: Annotated[UUID, typer.Option("--project-id")],
    work_item_id: Annotated[UUID, typer.Option("--work-item-id")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Apply only additive shadow state; never enforce or stamp success."""
    _upgrade_plan(
        target=UpgradeTarget.SHADOW,
        rollback_ref=rollback_ref,
        rollback_digest=rollback_digest,
        verification_file=None,
        package_digest=None,
        apply=apply,
        authorization_id=authorization_id,
        project_id=project_id,
        work_item_id=work_item_id,
        realm=realm,
        home=home,
    )


@app.command("upgrade-verify")
def upgrade_verify(
    verification_file: Annotated[Path, typer.Option("--verification-file", exists=True)],
    expected_snapshot_digest: Annotated[str, typer.Option("--expected-snapshot-digest")],
    project_id: Annotated[UUID, typer.Option("--project-id")],
    work_item_id: Annotated[UUID, typer.Option("--work-item-id")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Validate seven gates and independent verifier binding without mutation."""
    try:
        evidence = _verification(verification_file)
        with RealmSession(home, realm) as context:
            verification_digest = _upgrade(
                context, project_id=project_id, work_item_id=work_item_id
            ).verify(evidence, expected_snapshot_digest=expected_snapshot_digest)
    except ZekamError as exc:
        raise _raise(exc) from exc
    _emit(
        {
            "schema": "zekam-memory-upgrade-verify-result/v1",
            "passed": True,
            "verification_digest": verification_digest,
            "verifier_identity_digest": evidence.verifier_identity_digest,
            "grants_authority": False,
        }
    )


@app.command("upgrade-finalize")
def upgrade_finalize(
    rollback_ref: Annotated[str, typer.Option("--rollback-ref")],
    rollback_digest: Annotated[str, typer.Option("--rollback-digest")],
    verification_file: Annotated[Path, typer.Option("--verification-file", exists=True)],
    project_id: Annotated[UUID, typer.Option("--project-id")],
    work_item_id: Annotated[UUID, typer.Option("--work-item-id")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Recheck drift and transition shadow to enforced with exact authority."""
    _upgrade_plan(
        target=UpgradeTarget.ENFORCED,
        rollback_ref=rollback_ref,
        rollback_digest=rollback_digest,
        verification_file=verification_file,
        package_digest=None,
        apply=apply,
        authorization_id=authorization_id,
        project_id=project_id,
        work_item_id=work_item_id,
        realm=realm,
        home=home,
    )


@app.command("upgrade-stamp")
def upgrade_stamp(
    rollback_ref: Annotated[str, typer.Option("--rollback-ref")],
    rollback_digest: Annotated[str, typer.Option("--rollback-digest")],
    verification_file: Annotated[Path, typer.Option("--verification-file", exists=True)],
    package_digest: Annotated[str, typer.Option("--package-digest")],
    project_id: Annotated[UUID, typer.Option("--project-id")],
    work_item_id: Annotated[UUID, typer.Option("--work-item-id")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Append the final component success stamp only after enforced verification."""
    _upgrade_plan(
        target=UpgradeTarget.STAMPED,
        rollback_ref=rollback_ref,
        rollback_digest=rollback_digest,
        verification_file=verification_file,
        package_digest=package_digest,
        apply=apply,
        authorization_id=authorization_id,
        project_id=project_id,
        work_item_id=work_item_id,
        realm=realm,
        home=home,
    )


def _obsidian_bundle(
    context: Any, project_id: UUID, profile: ObsidianProfile
) -> ObsidianProjectionBundle:
    policy = load_memory_policy()
    records = PostgresMarkdownProjectionRepository(
        context.connection, context.realm_id
    ).load_obsidian_records(
        project_id,
        realm_slug=context.realm.slug,
    )
    return build_obsidian_projection(
        records,
        project_id=project_id,
        profile=profile,
        policy_digest=policy.policy_digest,
        realm_slug=context.realm.slug,
    )


def _obsidian_store(home: str | None) -> LocalObsidianProjectionStore:
    resolved = build_context(home=home).home
    return LocalObsidianProjectionStore(resolved / "global" / "bellek" / "obsidian")


@app.command("obsidian-plan")
def obsidian_plan(
    project_id: Annotated[UUID, typer.Option("--project-id")],
    profile: Annotated[ObsidianProfile, typer.Option("--profile")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Build a deterministic authority-free Obsidian publication plan."""

    try:
        with RealmSession(home, realm) as context:
            bundle = _obsidian_bundle(context, project_id, profile)
            store = _obsidian_store(home)
            plan = ObsidianApplyPlan.create(
                context.realm_id,
                bundle,
                store_identity_digest=store.identity_digest,
            )
    except ZekamError as exc:
        raise _raise(exc) from exc
    _emit(
        plan.body()
        | {
            "plan_digest": plan.plan_digest,
            "source_snapshot_digest": bundle.source_snapshot_digest,
            "privacy_scan_digest": bundle.privacy_scan_digest,
            "link_check_digest": bundle.link_check_digest,
            "file_count": len(bundle.files),
            "exclusion_count": len(bundle.exclusions),
            "apply": False,
        }
    )


@app.command("obsidian-apply")
def obsidian_apply(
    project_id: Annotated[UUID, typer.Option("--project-id")],
    profile: Annotated[ObsidianProfile, typer.Option("--profile")],
    expected_plan_digest: Annotated[str, typer.Option("--plan-digest")],
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Publish only the exact re-built plan with one-shot file-write authority."""

    try:
        with RealmSession(home, realm) as context:
            bundle = _obsidian_bundle(context, project_id, profile)
            store = _obsidian_store(home)
            plan = ObsidianApplyPlan.create(
                context.realm_id,
                bundle,
                store_identity_digest=store.identity_digest,
            )
            if plan.plan_digest != expected_plan_digest:
                raise PolicyViolation("Obsidian plan source/policy drift nedeniyle stale")
            if not apply:
                _emit(plan.body() | {"plan_digest": plan.plan_digest, "apply": False})
                return
            if authorization_id is None:
                raise AuthorizationRequired("Obsidian --uygula exact --authorization-id ister")
            service = ObsidianProjectionService(
                store,
                AuthorizationRepository(context.connection, context.realm_id),
            )
            result = service.apply(plan, authorization_id=authorization_id)
    except ZekamError as exc:
        raise _raise(exc) from exc
    _emit(result)


@app.command("obsidian-status")
def obsidian_status(
    project_id: Annotated[UUID, typer.Option("--project-id")],
    profile: Annotated[ObsidianProfile, typer.Option("--profile")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Fail closed unless CURRENT exactly matches the live canonical snapshot."""

    try:
        with RealmSession(home, realm) as context:
            bundle = _obsidian_bundle(context, project_id, profile)
        result = _obsidian_store(home).verify_current(
            realm,
            project_id,
            profile,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )
    except ZekamError as exc:
        raise _raise(exc) from exc
    _emit(
        result
        | {
            "source_snapshot_digest": bundle.source_snapshot_digest,
            "policy_digest": bundle.policy_digest,
            "current": True,
        }
    )
