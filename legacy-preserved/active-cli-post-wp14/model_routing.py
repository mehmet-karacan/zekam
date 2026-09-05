"""Layered general/workload/project model routing CLI."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.config import core_root
from zekam.application.execution import ExecutionHost
from zekam.application.governance import DEFAULT_POLICY_NAME, EffectRequest, GovernanceService
from zekam.application.layered_model_routing import (
    PreparedProjectRoutingContext,
    RoutePreview,
    adopt_general_campaign_qualifications,
    build_role_policy,
    prepare_project_context,
    preview_route,
)
from zekam.application.local_runtime_boundary import (
    ActorRepository,
    ContextContinuityRepository,
    ModelCapabilityRepository,
    ModelCatalogRepository,
    ModelRoutingRepository,
    ProjectResolver,
)
from zekam.application.model_capability_benchmark import load_capability_registry
from zekam.application.model_family_policy import load_model_family_policy
from zekam.application.model_registry import load_inventory
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.project_routing_targets import load_project_routing_targets
from zekam.application.realm_context import RealmContext
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.context_continuity import Checkpoint
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.model_capability_benchmark import CapabilityCohortPlan
from zekam.domain.model_catalog import (
    CatalogFetchStatus,
    CatalogSource,
    CatalogVisibility,
    ModelCatalogEntry,
    ModelCatalogSnapshot,
)
from zekam.domain.model_routing import (
    AgentRole,
    ExecutionTargetSnapshot,
    LayeredRouteRequest,
    RouteCapabilityBinding,
    RoutingLayer,
)
from zekam.domain.realm import DEFAULT_REALM_SLUG, ActorKind, LifecycleStatus
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, FailureCategory, Job, JobKind
from zekam.domain.security import DataClassification
from zekam.domain.work import AcceptanceCriterion, EffectKind, PlanStep, WorkState, WorkType
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail_from

app = typer.Typer(name="route", help="Katmanli model ve subagent routing", no_args_is_help=True)
console = Console()


def _integration(context: RealmContext) -> ProjectIntegrationService:
    return ProjectIntegrationService(context.connection, context.realm)


def _project_id(context: RealmContext, query: str) -> UUID:
    resolved = ProjectResolver(context.connection, context.realm_id).resolve(query).resolved
    if resolved is None:
        raise PolicyViolation("Routing project exact cozumlenemedi")
    if resolved.slug not in load_project_routing_targets().projects:
        raise PolicyViolation("Project reviewed routing target setinde degil")
    return cast(UUID, resolved.project_id)


def _active_human_actor(context: RealmContext) -> UUID:
    actors = tuple(
        item
        for item in ActorRepository(context.connection, context.realm_id).list_all()
        if item.kind is ActorKind.HUMAN and item.status is LifecycleStatus.ACTIVE
    )
    if len(actors) != 1:
        raise PolicyViolation("Routing mutation exact tek aktif human actor ister")
    return cast(UUID, actors[0].id)


def _current_model_bindings(context: RealmContext) -> tuple[str, str]:
    inventory_digest = load_inventory().snapshot_digest
    policy = GovernanceService(context.connection, context.realm).policies.current(
        DEFAULT_POLICY_NAME
    )
    if policy is None:
        raise PolicyViolation("Routing current model/security policy ister")
    return inventory_digest, policy.policy_digest


def _prepared(context: RealmContext, project: str) -> PreparedProjectRoutingContext:
    inventory_digest, policy_digest = _current_model_bindings(context)
    prepared = prepare_project_context(
        _integration(context),
        _project_id(context, project),
        inventory_digest=inventory_digest,
        policy_digest=policy_digest,
    )
    latest = ModelRoutingRepository(context.connection, context.realm_id).latest_context(
        prepared.context.project_id
    )
    if latest is None or latest[1].stale_reasons(prepared.context):
        return prepared
    return PreparedProjectRoutingContext(
        project_slug=prepared.project_slug,
        workloads=prepared.workloads,
        evidence=prepared.evidence,
        context=latest[1],
    )


def _latest_campaign_id(context: RealmContext) -> UUID:
    with context.connection.cursor() as cursor:
        cursor.execute(
            "select c.id from models.opencode_benchmark_campaign c"
            " join models.opencode_benchmark_campaign_outcome o"
            "   on o.realm_id=c.realm_id and o.campaign_id=c.id"
            " where c.realm_id=%s and c.campaign_key='opencode-aihub'"
            " and o.status in ('passed','failed') order by c.revision desc limit 1",
            (context.realm_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise PolicyViolation("Routing icin terminal OpenCode kampanyasi bulunamadi")
    return UUID(str(row[0]))


def _campaign_catalog_snapshot(
    context: RealmContext,
    campaign_id: UUID,
    *,
    now: dt.datetime,
) -> ModelCatalogSnapshot:
    """Kanonik kampanya uyelerini package availability snapshot'ina donusturur."""

    with context.connection.cursor() as cursor:
        cursor.execute(
            "select c.provider_ref,c.revision,m.canonical_model_id,m.modality"
            " from models.opencode_benchmark_campaign c"
            " join models.opencode_benchmark_campaign_member m"
            " on m.realm_id=c.realm_id and m.campaign_id=c.id"
            " where c.realm_id=%s and c.id=%s and m.canonical_model_id is not null"
            " order by m.canonical_model_id",
            (context.realm_id, campaign_id),
        )
        rows = cursor.fetchall()
    if not rows:
        raise PolicyViolation("Campaign package catalog canonical model tasimiyor")
    provider_id = str(rows[0][0])
    revision = int(rows[0][1])
    if any(str(row[0]) != provider_id or int(row[1]) != revision for row in rows):
        raise PolicyViolation("Campaign package catalog provider/revision drift")
    entries = tuple(
        ModelCatalogEntry(
            model_id=str(row[2]),
            visibility=CatalogVisibility.AUTHENTICATED,
            authentication_required=True,
            endpoint_class=str(row[3]),
            capabilities=(str(row[3]),),
        )
        for row in rows
    )
    latest = ModelCatalogRepository(context.connection, context.realm_id).latest(provider_id)
    return ModelCatalogSnapshot(
        id=uuid4(),
        realm_id=context.realm_id,
        provider_id=provider_id,
        entries=entries,
        etag=None,
        fetched_at=now,
        expires_at=now + dt.timedelta(days=7),
        client_version=f"campaign-{revision}",
        source=CatalogSource.PACKAGE,
        fetch_status=CatalogFetchStatus.FETCHED,
        error_category=None,
        prior_snapshot_id=None if latest is None else latest.id,
    )


def _campaign_provider_id(context: RealmContext, campaign_id: UUID) -> str:
    with context.connection.cursor() as cursor:
        cursor.execute(
            "select provider_ref from models.opencode_benchmark_campaign"
            " where realm_id=%s and id=%s",
            (context.realm_id, campaign_id),
        )
        row = cursor.fetchone()
    if row is None:
        raise PolicyViolation("Routing campaign provider bulunamadi")
    return str(row[0])


def _execution_target(home: str | None, *, now: dt.datetime) -> ExecutionTargetSnapshot:
    clients = tuple(
        item for item in build_context(home=home).settings.clients if item.name == "opencode"
    )
    if len(clients) != 1:
        raise PolicyViolation("Routing exact tek configured OpenCode execution target ister")
    client = clients[0]
    return ExecutionTargetSnapshot(
        client_id="opencode",
        slot="default",
        execution_mode="native-parallel",
        model_selectable=True,
        structured_result=False,
        cancellation=False,
        max_concurrency=3,
        cost_evidence_digest=digest({"status": "unknown-no-guess", "client": "opencode"}),
        capability_digest=digest(
            {
                "client": "opencode",
                "executable_name": client.executable.name,
                "executable_digest": digest_of_bytes(client.executable.read_bytes()),
                "model_selectable": True,
                "parallel_dispatch": True,
                "max_concurrency": 3,
            }
        ),
        captured_at=now,
        expires_at=now + dt.timedelta(days=7),
    )


def _existing_prepare_result(
    context: RealmContext,
    repository: ModelRoutingRepository,
    prepared: PreparedProjectRoutingContext,
    *,
    expected_target: ExecutionTargetSnapshot,
    expected_catalog: ModelCatalogSnapshot,
    prepare_key: str,
) -> dict[str, Any] | None:
    """Return a zero-effect replay result when every persisted input is current."""

    latest = repository.latest_context(prepared.context.project_id)
    if latest is None or latest[1].stale_reasons(prepared.context):
        return None
    now = dt.datetime.now(dt.UTC)
    policies = tuple(
        repository.latest_policy(role, layer, at=now)
        for role in AgentRole
        for layer in RoutingLayer
    )
    if any(item is None for item in policies):
        return None
    execution_target = repository.execution_target_by_digest(
        expected_target.snapshot_digest, at=now
    )
    if execution_target is None:
        return None
    catalog = ModelCatalogRepository(context.connection, context.realm_id).latest(
        expected_catalog.provider_id
    )
    if (
        catalog is None
        or not catalog.is_fresh(now=now)
        or catalog.source is not CatalogSource.PACKAGE
        or catalog.catalog_digest != expected_catalog.catalog_digest
    ):
        return None
    with context.connection.cursor() as cursor:
        cursor.execute(
            "select array_agg(q.id order by q.id)"
            " from models.model_routing_qualification q"
            " join models.routing_suite_binding b"
            "   on b.realm_id=q.realm_id and b.id=q.suite_binding_id"
            " where q.realm_id=%s and b.layer='general'"
            " and q.qualified and not q.unsafe and q.inventory_digest=%s"
            " and q.policy_digest=%s and q.valid_from<=%s and q.expires_at>=%s",
            (
                context.realm_id,
                prepared.context.inventory_digest,
                prepared.context.policy_digest,
                now,
                now,
            ),
        )
        row = cursor.fetchone()
    qualification_ids = () if row is None or row[0] is None else tuple(row[0])
    if not qualification_ids:
        return None
    with context.connection.cursor() as cursor:
        cursor.execute(
            "select count(*) filter (where j.state='completed' and a.outcome='succeeded'"
            " and cp.pending_steps='{}'::text[] and er.status='completed'),"
            " count(*) filter (where j.state<>'completed')"
            " from runtime.job j"
            " left join runtime.job_attempt a on a.realm_id=j.realm_id and a.job_id=j.id"
            " left join work.checkpoint cp on cp.realm_id=j.realm_id and cp.job_id=j.id"
            " left join runtime.effect_claim ec on ec.realm_id=j.realm_id and ec.job_id=j.id"
            " left join runtime.effect_receipt er on er.realm_id=ec.realm_id and er.claim_id=ec.id"
            " where j.realm_id=%s and j.idempotency_key=%s and j.step_id='model-route-prepare'",
            (
                context.realm_id,
                f"model-route-prepare:{prepare_key}",
            ),
        )
        runtime_row = cursor.fetchone()
    if runtime_row is None or int(runtime_row[0]) != 1 or int(runtime_row[1]) != 0:
        return None
    return {
        "context_id": str(latest[0]),
        "context_inserted": False,
        "policy_ids": [str(item[0]) for item in policies if item is not None],
        "execution_target_id": str(execution_target[0]),
        "execution_target_inserted": False,
        "catalog_snapshot_id": str(catalog.id),
        "catalog_digest": catalog.catalog_digest,
        "catalog_inserted": False,
        "general_qualification_ids": [str(item) for item in qualification_ids],
        "general_qualification_count": len(qualification_ids),
        "workload_project_qualification_state": "pending",
        "replay": True,
        "provider_calls": 0,
        "db_effects": 0,
    }


Mutation = Callable[[UUID, UUID], tuple[str, str, dict[str, Any]]]


def _run_db_mutation(
    context: RealmContext,
    *,
    prepared: PreparedProjectRoutingContext,
    step_id: str,
    title: str,
    resources: tuple[str, ...],
    mutation: Mutation,
    mutation_key: str | None = None,
) -> dict[str, Any]:
    actor_id = _active_human_actor(context)
    graph = WorkGraphService(context.connection, context.realm, actor_id=actor_id)
    work = graph.create_item(
        project_id=prepared.context.project_id,
        type=WorkType.TASK,
        title=title,
        summary="Source-bound layered model routing evidence and decision ledger",
        acceptance_criteria=(
            AcceptanceCriterion("Current project context digest'e baglanir"),
            AcceptanceCriterion("Yalniz fresh qualified model kaniti kullanilir"),
            AcceptanceCriterion("Claim, receipt ve checkpoint exact eslesir"),
        ),
    )
    work = graph.transition(work.id, WorkState.READY)
    work = graph.transition(work.id, WorkState.ACTIVE)
    governance = GovernanceService(context.connection, context.realm, actor_id=actor_id)
    policy = governance.policies.current(DEFAULT_POLICY_NAME)
    if policy is None:
        raise PolicyViolation("Routing mutation current policy ister")
    request = EffectRequest(
        action=step_id,
        effects=(EffectKind.DATABASE_WRITE,),
        resources=resources,
        data_classifications=(DataClassification.LOCAL_ONLY,),
        reversible=True,
        touches_external_system=False,
        required_capabilities=("database.write",),
    )
    with context.connection.transaction():
        plan = graph.create_plan(
            work.id,
            source_revision=prepared.context.source_revision,
            policy_digest=policy.policy_digest,
            steps=(
                PlanStep(
                    step_id=step_id,
                    title=title,
                    effect=EffectKind.DATABASE_WRITE,
                    logical_resources=resources,
                    risk="medium",
                ),
            ),
        )
        authorization = governance.issue_authorization(
            request=request,
            actor_id=actor_id,
            plan=plan,
            lifetime=dt.timedelta(minutes=30),
        )
    exact_mutation_key = mutation_key or prepared.context.context_digest
    capability = f"model.route.{step_id}.{exact_mutation_key[-12:]}"
    host = ExecutionHost(
        context.connection, context.realm_id, worker_label=f"model-route-{step_id}"
    )
    job, created = host.jobs.enqueue(
        Job.create(
            realm_id=context.realm_id,
            project_id=prepared.context.project_id,
            kind=JobKind.MUTATION,
            idempotency_key=f"{step_id}:{exact_mutation_key}",
            resources=parse_requests(write=resources),
            required_capabilities=(capability,),
            max_attempts=1,
            work_item_id=work.id,
            plan_id=plan.id,
            step_id=step_id,
        )
    )
    if not created:
        governance.revoke_authorization(authorization.id, "model-route-runtime-replay")
        raise PolicyViolation("Routing mutation runtime replay reddedildi")
    claimed = host.acquire_work(capabilities=(capability,))
    if claimed is None or claimed.job.id != job.id:
        governance.revoke_authorization(authorization.id, "model-route-runtime-acquire-failed")
        host.jobs.mark_recovery_required(job.id, "model-route-acquire-failed-no-effect")
        raise PolicyViolation("Routing mutation job claim edilemedi")
    claim = None
    try:
        claim = host.claim_effect(
            claimed,
            operation=step_id,
            effect_digest=request.effect_digest,
            authorization_digest=authorization.authorization_digest,
            authorization_id=authorization.id,
            idempotency_key=exact_mutation_key,
            resources=parse_requests(write=resources),
            adapter_digest=digest({"adapter": "layered-model-routing/v1", "step": step_id}),
        )
        governance.require_authorized(
            request, authorization=authorization, consumed_by=f"cli:model-route-{step_id}"
        )
        with context.connection.transaction():
            result_digest, adapter_digest, payload = mutation(work.id, plan.id)
            receipt = host.record_success(
                claim,
                result_digest=result_digest,
                adapter_evidence_digest=adapter_digest,
            )
            checkpoint = Checkpoint(
                checkpoint_id=f"model-route-{step_id}-{job.id}",
                project_id=str(prepared.context.project_id),
                work_item_id=str(work.id),
                plan_revision_id=str(plan.id),
                source_revision=prepared.context.source_revision,
                plan_steps=(step_id,),
                completed_steps=(step_id,),
                pending_steps=(),
                step_results=((step_id, result_digest),),
                context_manifest_digest=prepared.context.context_digest,
                journal_head_digest=adapter_digest,
                next_safe_action="independent-routing-verification",
                created_at=dt.datetime.now(dt.UTC),
            )
            checkpoint_id = ContextContinuityRepository(
                context.connection, context.realm_id, prepared.context.project_id, work.id
            ).store_checkpoint(checkpoint, task_plan_id=plan.id, job_id=job.id)
            if not host.finish(
                claimed, outcome=AttemptOutcome.SUCCEEDED, result_digest=result_digest
            ):
                raise PolicyViolation("Routing mutation job finalization reddedildi")
    except Exception as exc:
        failure_category = (
            FailureCategory.POLICY if isinstance(exc, PolicyViolation) else FailureCategory.ADAPTER
        )
        failure_digest = digest(
            {"error_type": type(exc).__name__, "context": prepared.context.context_digest}
        )
        with context.connection.transaction():
            if claim is not None and host.ledger.receipt_for_claim(claim.id) is None:
                host.record_failure(
                    claim,
                    category=failure_category,
                    failure_digest=failure_digest,
                )
            if not host.finish(
                claimed,
                outcome=AttemptOutcome.RECOVERY_REQUIRED,
                result_digest=failure_digest,
                failure_category=failure_category,
            ):
                raise PolicyViolation("Routing recovery finalization reddedildi") from exc
        if claim is None:
            governance.revoke_authorization(authorization.id, "model-route-claim-failed")
        raise
    return payload | {
        "work_id": str(work.id),
        "task_plan_id": str(plan.id),
        "authorization_id": str(authorization.id),
        "job_id": str(job.id),
        "claim_id": str(claim.id),
        "receipt_id": str(receipt.id),
        "checkpoint_id": str(checkpoint_id),
        "grants_authority": False,
    }


@app.command("prepare")
def prepare_command(
    project: Annotated[str, typer.Option("--project", help="Reviewed project slug")],
    apply: Annotated[bool, typer.Option("--uygula", help="Context ve policy kaydeder")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Current project context'i planlar; apply provider cagrisi yapmaz."""

    try:
        with RealmSession(home, realm) as context:
            prepared = _prepared(context, project)
            document: dict[str, Any] = {
                "schema": "zekam-model-route-prepare/v1",
                "project": prepared.sanitized(),
                "apply": apply,
                "provider_calls": 0,
            }
            if apply:
                repository = ModelRoutingRepository(context.connection, context.realm_id)
                execution_target = _execution_target(home, now=prepared.context.captured_at)
                campaign_id = _latest_campaign_id(context)
                catalog_snapshot = _campaign_catalog_snapshot(
                    context,
                    campaign_id,
                    now=dt.datetime.now(dt.UTC),
                )
                prepare_key = digest(
                    {
                        "context": prepared.context.context_digest,
                        "execution_target": execution_target.snapshot_digest,
                        "campaign_id": str(campaign_id),
                        "catalog_digest": catalog_snapshot.catalog_digest,
                    }
                )
                existing = _existing_prepare_result(
                    context,
                    repository,
                    prepared,
                    expected_target=execution_target,
                    expected_catalog=catalog_snapshot,
                    prepare_key=prepare_key,
                )
                if existing is not None:
                    document["result"] = existing
                    if output_json:
                        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
                    else:
                        console.print(document)
                    return
                resources = (
                    f"project:{prepared.context.project_id}",
                    f"db-object:model-routing:context:{prepared.context.context_digest}",
                    f"db-object:model-routing:campaign:{campaign_id}",
                    f"db-object:model-catalog:{catalog_snapshot.catalog_digest}",
                )

                def mutation(_work_id: UUID, _plan_id: UUID) -> tuple[str, str, dict[str, Any]]:
                    context_id, inserted = repository.store_project_context(prepared.context)
                    execution_target_id, execution_target_inserted = (
                        repository.store_execution_target(execution_target)
                    )
                    catalog_id, catalog_inserted = ModelCatalogRepository(
                        context.connection, context.realm_id
                    ).store(catalog_snapshot)
                    with context.connection.cursor() as cursor:
                        cursor.execute(
                            "select distinct model_id"
                            " from models.opencode_model_qualification_event"
                            " where realm_id=%s and campaign_id=%s and action='qualified'"
                            " order by model_id",
                            (context.realm_id, campaign_id),
                        )
                        fallback_model_ids = tuple(str(row[0]) for row in cursor.fetchall())
                    if not fallback_model_ids:
                        raise PolicyViolation("Routing fallback modeli icin qualified set bos")
                    policy_ids = tuple(
                        repository.store_role_policy(
                            build_role_policy(
                                role,
                                layer,
                                fallback_model_ids=fallback_model_ids,
                            ),
                            effective_from=prepared.context.captured_at,
                        )
                        for role in AgentRole
                        for layer in RoutingLayer
                    )
                    qualification_ids = tuple(
                        qualification_id
                        for role in AgentRole
                        for qualification_id in adopt_general_campaign_qualifications(
                            repository,
                            campaign_id=campaign_id,
                            role=role,
                        )
                    )
                    payload = {
                        "context_id": str(context_id),
                        "context_inserted": inserted,
                        "execution_target_id": str(execution_target_id),
                        "execution_target_inserted": execution_target_inserted,
                        "catalog_snapshot_id": str(catalog_id),
                        "catalog_digest": catalog_snapshot.catalog_digest,
                        "catalog_inserted": catalog_inserted,
                        "policy_ids": [str(item) for item in policy_ids],
                        "general_qualification_ids": [str(item) for item in qualification_ids],
                        "general_qualification_count": len(qualification_ids),
                        "workload_project_qualification_state": "pending",
                    }
                    result_digest = digest(payload)
                    return (
                        result_digest,
                        digest(
                            {
                                "context": prepared.context.context_digest,
                                "campaign_id": str(campaign_id),
                                "result": result_digest,
                            }
                        ),
                        payload,
                    )

                document["result"] = _run_db_mutation(
                    context,
                    prepared=prepared,
                    step_id="model-route-prepare",
                    title=f"{prepared.project_slug} layered model routing context",
                    resources=resources,
                    mutation=mutation,
                    mutation_key=prepare_key,
                )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    else:
        console.print(document)


def _preview(
    context: RealmContext,
    *,
    home: str | None,
    project: str,
    role: AgentRole,
    target_layer: RoutingLayer,
    workload: str | None,
    technology: str | None,
    excluded_models: tuple[str, ...],
    excluded_executions: tuple[str, ...],
    risk: str,
) -> tuple[PreparedProjectRoutingContext, RoutePreview, LayeredRouteRequest, UUID, UUID]:
    prepared = _prepared(context, project)
    repository = ModelRoutingRepository(context.connection, context.realm_id)
    if target_layer is RoutingLayer.GENERAL:
        if workload is not None or technology is not None:
            raise PolicyViolation("General route workload/technology scope tasiyamaz")
        scoped_workload = None
        scoped_technology = None
    else:
        if not workload or not technology:
            raise PolicyViolation("Workload/project route workload ve technology ister")
        scoped_workload = workload.casefold()
        scoped_technology = technology.casefold()
    stored_policy = repository.latest_policy(role, target_layer, at=dt.datetime.now(dt.UTC))
    if stored_policy is None:
        policy = build_role_policy(role, target_layer)
        policy_id = UUID(int=0)
    else:
        policy_id, policy = stored_policy
    if excluded_models or excluded_executions:
        raise PolicyViolation("Role independence exclusions kanonik onceki route'tan turetilir")
    derived_models: set[str] = set()
    derived_executions: set[str] = set()
    derived_families: set[str] = set()
    family_policy = load_model_family_policy()
    for prior_role in policy.independent_from_roles:
        prior = repository.latest_decision(
            prior_role,
            target_layer,
            project_id=(
                prepared.context.project_id if target_layer is RoutingLayer.PROJECT else None
            ),
            workload=scoped_workload,
            technology=scoped_technology,
            risk=risk,
        )
        if prior is None or prior.decision.primary_model_id is None:
            continue
        selected_models = {
            item
            for item in (
                prior.decision.primary_model_id,
                prior.decision.fallback_model_id,
            )
            if item is not None
        }
        derived_models.update(selected_models)
        for model_id in selected_models:
            family = family_policy.family_for(model_id)
            if family is None:
                raise PolicyViolation("Prior route model family policy binding eksik")
            derived_families.add(family)
        derived_executions.update(
            item.tested_execution_identity
            for item in repository.qualifications_for(prior.decision.request)
            if item.model_id in selected_models
        )
    expected_target = _execution_target(home, now=prepared.context.captured_at)
    execution_target = repository.execution_target_by_digest(
        expected_target.snapshot_digest, at=dt.datetime.now(dt.UTC)
    )
    if execution_target is None:
        raise PolicyViolation(
            "Persisted OpenCode execution target current executable/capability ile stale"
        )
    routing_targets = load_project_routing_targets()
    capability_requirements = routing_targets.requirements_for(role)
    capability_source = ModelCapabilityRepository(
        context.connection, context.realm_id
    ).latest_source()
    registry, execution_profile, _ = load_capability_registry(
        core_root() / "config" / "model_capability_benchmark.yaml",
        repository_root=core_root(),
    )
    capability_plan = CapabilityCohortPlan(
        source_campaign_id=capability_source.campaign_id,
        source_revision=capability_source.source_revision,
        inventory_digest=capability_source.inventory_digest,
        policy_digest=capability_source.policy_digest,
        verifier_provenance_digest=capability_source.verifier_provenance_digest,
        model_ids=capability_source.model_ids,
        registry=registry,
        execution_profile=execution_profile,
        max_parallelism=len(capability_source.model_ids),
    )
    capability_binding = RouteCapabilityBinding(
        evidence_role=routing_targets.evidence_role_for(role),
        source_revision=capability_plan.source_revision,
        suite_digest=capability_plan.suite_digest,
        registry_digest=registry.registry_digest,
        execution_profile_digest=execution_profile.profile_digest,
        evaluator_provenance_digest=execution_profile.evaluator_provenance_digest,
    )
    request = LayeredRouteRequest(
        role=role,
        target_layer=target_layer,
        workload=scoped_workload,
        technology=scoped_technology,
        project_id=(prepared.context.project_id if target_layer is RoutingLayer.PROJECT else None),
        project_context_digest=(
            prepared.context.context_digest if target_layer is RoutingLayer.PROJECT else None
        ),
        inventory_digest=prepared.context.inventory_digest,
        routing_policy_digest=policy.policy_digest,
        policy_digest=prepared.context.policy_digest,
        execution_target_digest=execution_target[1].snapshot_digest,
        capability_requirements=capability_requirements,
        capability_binding=capability_binding,
        risk=risk,
        family_policy_digest=family_policy.policy_digest,
        excluded_model_families=tuple(sorted(derived_families)),
        excluded_model_ids=tuple(sorted(derived_models)),
        excluded_execution_identities=tuple(sorted(derived_executions)),
    )
    return (
        prepared,
        preview_route(
            repository,
            request,
            current_context=(prepared.context if target_layer is RoutingLayer.PROJECT else None),
            provider_id=_campaign_provider_id(context, _latest_campaign_id(context)),
            catalog=ModelCatalogRepository(context.connection, context.realm_id),
        ),
        request,
        policy_id,
        execution_target[0],
    )


def _route_options(
    project: str,
    role: AgentRole,
    target_layer: RoutingLayer,
    workload: str | None,
    technology: str | None,
    realm: str,
    home: str | None,
    risk: str,
) -> tuple[PreparedProjectRoutingContext, RoutePreview, LayeredRouteRequest, UUID, UUID]:
    with RealmSession(home, realm) as context:
        return _preview(
            context,
            home=home,
            project=project,
            role=role,
            target_layer=target_layer,
            workload=workload,
            technology=technology,
            excluded_models=(),
            excluded_executions=(),
            risk=risk,
        )


@app.command("preview")
def preview_command(
    project: Annotated[str, typer.Option("--project")],
    role: Annotated[AgentRole, typer.Option("--role")],
    target_layer: Annotated[RoutingLayer, typer.Option("--layer")] = RoutingLayer.PROJECT,
    workload: Annotated[str | None, typer.Option("--workload")] = None,
    technology: Annotated[str | None, typer.Option("--technology")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
    risk: Annotated[str, typer.Option("--risk")] = "medium",
) -> None:
    """Kanit kesisimini salt okunur gosterir; eksikte pending doner."""

    try:
        prepared, preview, _, _, _ = _route_options(
            project,
            role,
            target_layer,
            workload,
            technology,
            realm,
            home,
            risk,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(
        json.dumps(
            {"project": prepared.project_slug, "route": preview.sanitized()},
            ensure_ascii=False,
        )
    )


@app.command("decide")
def decide_command(
    project: Annotated[str, typer.Option("--project")],
    role: Annotated[AgentRole, typer.Option("--role")],
    target_layer: Annotated[RoutingLayer, typer.Option("--layer")] = RoutingLayer.PROJECT,
    workload: Annotated[str | None, typer.Option("--workload")] = None,
    technology: Annotated[str | None, typer.Option("--technology")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
    risk: Annotated[str, typer.Option("--risk")] = "medium",
) -> None:
    """Karari preview eder veya exact DB authority ile append-only kaydeder."""

    try:
        with RealmSession(home, realm) as context:
            prepared, preview, request, role_policy_id, execution_target_id = _preview(
                context,
                home=home,
                project=project,
                role=role,
                target_layer=target_layer,
                workload=workload,
                technology=technology,
                excluded_models=(),
                excluded_executions=(),
                risk=risk,
            )
            document: dict[str, Any] = {"route": preview.sanitized(), "applied": False}
            if apply:
                if role_policy_id.int == 0:
                    raise PolicyViolation("Route decide once persisted role policy ister")
                repository = ModelRoutingRepository(context.connection, context.realm_id)
                latest_context = repository.latest_context(prepared.context.project_id)
                if target_layer is RoutingLayer.PROJECT and (
                    latest_context is None
                    or latest_context[1].context_digest != prepared.context.context_digest
                ):
                    raise PolicyViolation("Route decide persisted current context ister")
                existing_decision = repository.decision_by_evidence(
                    preview.decision.evidence_digest
                )
                if existing_decision is not None:
                    if existing_decision.execution_target_id != execution_target_id:
                        raise PolicyViolation(
                            "Route decision replay stale execution target ile eslesmiyor"
                        )
                    document["result"] = {
                        "decision_id": str(existing_decision.id),
                        "inserted": False,
                        "replay": True,
                        "status": existing_decision.decision.status.value,
                        "primary_model_id": existing_decision.decision.primary_model_id,
                        "fallback_model_id": existing_decision.decision.fallback_model_id,
                        "evidence_digest": existing_decision.decision.evidence_digest,
                        "provider_calls": 0,
                        "db_effects": 0,
                    }
                    document["applied"] = True
                    console.print_json(json.dumps(document, ensure_ascii=False, default=str))
                    return
                resources = (
                    f"project:{prepared.context.project_id}",
                    f"db-object:model-routing:decision:{preview.decision.evidence_digest}",
                )

                def mutation(_work_id: UUID, _plan_id: UUID) -> tuple[str, str, dict[str, Any]]:
                    decision_id, inserted = repository.record_decision(
                        preview.decision,
                        role_policy_id=role_policy_id,
                        project_context_id=(
                            latest_context[0]
                            if target_layer is RoutingLayer.PROJECT and latest_context is not None
                            else None
                        ),
                        execution_target_id=execution_target_id,
                        decided_at=dt.datetime.now(dt.UTC),
                    )
                    payload = {
                        "decision_id": str(decision_id),
                        "inserted": inserted,
                        "status": preview.decision.status.value,
                        "primary_model_id": preview.decision.primary_model_id,
                        "fallback_model_id": preview.decision.fallback_model_id,
                        "evidence_digest": preview.decision.evidence_digest,
                    }
                    result_digest = digest(payload)
                    request_digest = digest(
                        {
                            "role": request.role,
                            "target_layer": request.target_layer,
                            "workload": request.workload,
                            "technology": request.technology,
                            "project_id": request.project_id,
                            "project_context_digest": request.project_context_digest,
                            "inventory_digest": request.inventory_digest,
                            "routing_policy_digest": request.routing_policy_digest,
                            "policy_digest": request.policy_digest,
                            "execution_target_digest": request.execution_target_digest,
                            "risk": request.risk,
                            "family_policy_digest": request.family_policy_digest,
                            "excluded_model_families": request.excluded_model_families,
                            "excluded_model_ids": request.excluded_model_ids,
                            "excluded_execution_identities": (
                                request.excluded_execution_identities
                            ),
                        }
                    )
                    return (
                        result_digest,
                        digest({"request": request_digest, "result": result_digest}),
                        payload,
                    )

                document["result"] = _run_db_mutation(
                    context,
                    prepared=prepared,
                    step_id="model-route-decide",
                    title=f"{prepared.project_slug} {role.value} route decision",
                    resources=resources,
                    mutation=mutation,
                    mutation_key=preview.decision.evidence_digest,
                )
                document["applied"] = True
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("status")
def status_command(
    project: Annotated[str, typer.Option("--project")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
    risk: Annotated[str, typer.Option("--risk")] = "medium",
) -> None:
    """Project context ve rol karar durumlarini salt okunur raporlar."""

    try:
        with RealmSession(home, realm) as context:
            prepared = _prepared(context, project)
            repository = ModelRoutingRepository(context.connection, context.realm_id)
            latest = repository.latest_context(prepared.context.project_id)
            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select distinct on (role, workload, technology) id, role, workload,"
                    " technology, risk, status, primary_model_id, fallback_model_id,"
                    " evidence_digest"
                    " from models.model_route_decision where realm_id=%s"
                    " and target_layer='project' and project_id=%s"
                    " and risk=%s"
                    " order by role, workload, technology, decided_at desc, id desc",
                    (context.realm_id, prepared.context.project_id, risk),
                )
                decision_rows = cursor.fetchall()
            decisions = [
                {
                    "decision_id": str(row[0]),
                    "role": str(row[1]),
                    "workload": str(row[2]),
                    "technology": str(row[3]),
                    "risk": str(row[4]),
                    "status": str(row[5]),
                    "primary_model_id": None if row[6] is None else str(row[6]),
                    "fallback_model_id": None if row[7] is None else str(row[7]),
                    "evidence_digest": str(row[8]),
                }
                for row in decision_rows
            ]
            document = {
                "project": prepared.project_slug,
                "risk": risk,
                "current_context_digest": prepared.context.context_digest,
                "persisted_context_digest": None if latest is None else latest[1].context_digest,
                "context_current": latest is not None
                and latest[1].context_digest == prepared.context.context_digest,
                "decisions": decisions,
                "provider_calls": 0,
            }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False))


@app.command("resolve")
def resolve_command(
    project: Annotated[str, typer.Option("--project")],
    role: Annotated[AgentRole, typer.Option("--role")],
    target_layer: Annotated[RoutingLayer, typer.Option("--layer")] = RoutingLayer.PROJECT,
    workload: Annotated[str | None, typer.Option("--workload")] = None,
    technology: Annotated[str | None, typer.Option("--technology")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
    risk: Annotated[str, typer.Option("--risk")] = "medium",
) -> None:
    """Latest persisted project route kararini authority vermeden cozer."""

    try:
        with RealmSession(home, realm) as context:
            prepared = _prepared(context, project)
            if target_layer is RoutingLayer.GENERAL:
                if workload is not None or technology is not None:
                    raise PolicyViolation("General route workload/technology scope tasiyamaz")
                scoped_workload = None
                scoped_technology = None
            else:
                if not workload or not technology:
                    raise PolicyViolation("Workload/project route scope ister")
                scoped_workload = workload.casefold()
                scoped_technology = technology.casefold()
            repository = ModelRoutingRepository(context.connection, context.realm_id)
            stored = repository.latest_decision(
                role,
                target_layer,
                project_id=(
                    prepared.context.project_id if target_layer is RoutingLayer.PROJECT else None
                ),
                workload=scoped_workload,
                technology=scoped_technology,
                risk=risk,
            )
            if stored is None:
                raise PolicyViolation("Persisted project route karari yok; pending")
            if target_layer is RoutingLayer.PROJECT and (
                stored.project_context_id is None
                or stored.decision.request.project_context_digest != prepared.context.context_digest
                or repository.staleness_of(stored.project_context_id, prepared.context)
            ):
                raise PolicyViolation(
                    "Persisted project route karari stale; yeniden decide gerekir"
                )
            (
                _,
                current_preview,
                _,
                _,
                current_execution_target_id,
            ) = _preview(
                context,
                home=home,
                project=project,
                role=role,
                target_layer=target_layer,
                workload=scoped_workload,
                technology=scoped_technology,
                excluded_models=(),
                excluded_executions=(),
                risk=risk,
            )
            if (
                stored.execution_target_id != current_execution_target_id
                or stored.decision.evidence_digest != current_preview.decision.evidence_digest
                or stored.decision.status is not current_preview.decision.status
                or stored.decision.primary_model_id != current_preview.decision.primary_model_id
                or stored.decision.fallback_model_id != current_preview.decision.fallback_model_id
            ):
                raise PolicyViolation(
                    "Persisted route karari current target/qualification ile stale;"
                    " yeniden decide gerekir"
                )
            document = {
                "project": prepared.project_slug,
                "decision_id": str(stored.id),
                "status": stored.decision.status.value,
                "primary_model_id": stored.decision.primary_model_id,
                "fallback_model_id": stored.decision.fallback_model_id,
                "evidence_digest": stored.decision.evidence_digest,
                "grants_authority": False,
            }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False))


app.command("explain")(status_command)
app.command("handoff")(resolve_command)
