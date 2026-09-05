"""`zekam oracle` metadata-only project ingestion commands."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from typing import Annotated, Any
from uuid import UUID

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.execution import ExecutionHost
from zekam.application.governance import DEFAULT_POLICY_NAME, EffectRequest, GovernanceService
from zekam.application.local_embedding_composition import build_verified_mac_embedding
from zekam.application.oracle_metadata_index import (
    SUPPORTED_OBJECT_TYPES,
    OracleDatasource,
    OracleMetadataClient,
    OracleMetadataIndexPlan,
    OracleMetadataIndexResult,
    apply_oracle_metadata_index,
    build_oracle_metadata_index_plan,
    load_project_oracle_datasource,
)
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.realm_context import RealmContext
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import Checkpoint
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.policy import PolicyDocument, PolicyRule, RiskLevel
from zekam.domain.project import IntegrationStage
from zekam.domain.realm import DEFAULT_REALM_SLUG, ActorKind, LifecycleStatus
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, FailureCategory, Job, JobKind
from zekam.domain.security import AuthorizationState, DataClassification
from zekam.domain.work import AcceptanceCriterion, EffectKind, PlanStep, WorkState, WorkType
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.knowledge_repository import KnowledgeRepository
from zekam.infrastructure.postgres.project_repository import ProjectResolver
from zekam.infrastructure.postgres.retrieval_repository import RetrievalRepository
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail_from

app = typer.Typer(
    name="oracle",
    help="Oracle DBMS_METADATA salt-okunur proje indeksleme islemleri",
    no_args_is_help=True,
)
console = Console()


def _service(context: RealmContext) -> ProjectIntegrationService:
    return ProjectIntegrationService(context.connection, context.realm)


def _project(context: RealmContext, query: str) -> tuple[UUID, ProjectIntegrationService]:
    resolution = ProjectResolver(context.connection, context.realm_id).resolve(query)
    if resolution.resolved is None:
        raise PolicyViolation("Oracle metadata icin exact proje cozulemedi")
    service = _service(context)
    report = service.evaluate(resolution.resolved.project_id)
    if (
        report.stage is not IntegrationStage.CURRENT
        or report.is_stale
        or report.current_revision is None
    ):
        raise PolicyViolation("Oracle metadata yalniz current proje revision'ina baglanabilir")
    return resolution.resolved.project_id, service


def _actor_id(context: RealmContext, requested: UUID | None) -> UUID:
    actors = ActorRepository(context.connection, context.realm_id)
    if requested is not None:
        actor = actors.get(requested)
        if actor.kind is not ActorKind.HUMAN or actor.status is not LifecycleStatus.ACTIVE:
            raise PolicyViolation("Oracle metadata actor aktif human olmali")
        return actor.id
    candidates = tuple(
        actor
        for actor in actors.list_all()
        if actor.kind is ActorKind.HUMAN and actor.status is LifecycleStatus.ACTIVE
    )
    if len(candidates) != 1:
        raise PolicyViolation("Oracle metadata exact tek aktif human actor ister")
    return candidates[0].id


def _work_id(
    context: RealmContext,
    *,
    project_id: UUID,
    project_slug: str,
    actor_id: UUID,
    requested: UUID | None,
) -> UUID:
    graph = WorkGraphService(context.connection, context.realm, actor_id=actor_id)
    if requested is None:
        work = graph.create_item(
            project_id=project_id,
            type=WorkType.TASK,
            title=f"{project_slug} Oracle DBMS_METADATA DDL indeksi",
            summary="Satir verisi almadan tum izinli schema nesnelerini yerel vektor indeksine al",
            acceptance_criteria=(
                AcceptanceCriterion("Oracle schema ve nesne kapsami metadata-only dogrulanir"),
                AcceptanceCriterion("Her DBMS_METADATA DDL secret-safe chunk ve vektor uretir"),
                AcceptanceCriterion("Read ve write effectleri ayri terminal receipt ile kapanir"),
            ),
        )
        work = graph.transition(work.id, WorkState.READY)
        work = graph.transition(work.id, WorkState.ACTIVE)
        return work.id
    work = graph.items.get(requested)
    if work.project_id != project_id:
        raise PolicyViolation("Oracle metadata Work/project binding mismatch")
    if work.state is WorkState.PROPOSED:
        work = graph.transition(work.id, WorkState.READY)
    if work.state in {WorkState.READY, WorkState.BLOCKED, WorkState.VERIFICATION}:
        work = graph.transition(work.id, WorkState.ACTIVE)
    if work.state is not WorkState.ACTIVE:
        raise PolicyViolation("Oracle metadata Work active duruma getirilemedi")
    return work.id


def _record_failure(host: ExecutionHost, claim: Any, exc: Exception, evidence: str) -> None:
    if host.ledger.receipt_for_claim(claim.id) is None:
        host.record_failure(
            claim,
            category=(
                FailureCategory.POLICY
                if isinstance(exc, PolicyViolation)
                else FailureCategory.ADAPTER
            ),
            failure_digest=digest({"error_type": type(exc).__name__, "evidence": evidence}),
        )


def _read_snapshot(
    context: RealmContext,
    *,
    project_id: UUID,
    work_id: UUID,
    actor_id: UUID,
    datasource: OracleDatasource,
    source_revision: str,
) -> tuple[Any, dict[str, str]]:
    governance = GovernanceService(context.connection, context.realm, actor_id=actor_id)
    policy = governance.policies.current(DEFAULT_POLICY_NAME)
    if policy is None:
        raise PolicyViolation("Oracle metadata current policy ister")
    query_spec_digest = digest(
        {
            "schema": "zekam-oracle-get-ddl-query/v1",
            "connection_identity_digest": datasource.connection_identity_digest,
            "schema_name": datasource.schema_name,
            "object_types": [item[0] for item in SUPPORTED_OBJECT_TYPES],
            "row_data_included": False,
        }
    )
    resources = (
        f"project:{project_id}",
        f"db-object:{project_id}:oracle-schema:{datasource.connection_identity_digest}:"
        f"{datasource.schema_name}",
        f"db-object:{project_id}:oracle-query:{query_spec_digest}",
    )
    request = EffectRequest(
        action="oracle-metadata-read",
        effects=(EffectKind.NETWORK_CALL,),
        resources=resources,
        data_classifications=(DataClassification.LOCAL_ONLY,),
        reversible=False,
        touches_external_system=True,
        required_capabilities=("database.read",),
    )
    allow_rule = PolicyRule(
        name=f"exact-oracle-metadata-read-{query_spec_digest[-16:]}",
        effect_kinds=(EffectKind.NETWORK_CALL,),
        allow=True,
        max_risk=RiskLevel.CRITICAL,
        allowed_resources=resources,
        reason="Yalniz exact DBMS_METADATA GET_DDL metadata read hedefi",
    )
    retained: list[PolicyRule] = []
    for rule in policy.rules:
        if EffectKind.NETWORK_CALL not in rule.effect_kinds:
            retained.append(rule)
            continue
        remaining = tuple(item for item in rule.effect_kinds if item is not EffectKind.NETWORK_CALL)
        if remaining:
            retained.append(replace(rule, effect_kinds=remaining))
    candidate_policy = PolicyDocument.create(
        realm_id=policy.realm_id,
        name=policy.name,
        revision=policy.revision + 1,
        rules=(allow_rule, *retained),
        network_default_deny=True,
        push_default_deny=True,
    )
    graph = WorkGraphService(context.connection, context.realm, actor_id=actor_id)
    with context.connection.transaction():
        governance.policies.append(candidate_policy)
        task_plan = graph.create_plan(
            work_id,
            source_revision=f"{source_revision}:{query_spec_digest}",
            policy_digest=candidate_policy.policy_digest,
            steps=(
                PlanStep(
                    step_id="oracle-metadata-read",
                    title="Exact DBMS_METADATA GET_DDL metadata read",
                    effect=EffectKind.NETWORK_CALL,
                    logical_resources=resources,
                    risk="high",
                ),
            ),
        )
        authorization = governance.issue_authorization(
            request=request,
            actor_id=actor_id,
            plan=task_plan,
            lifetime=dt.timedelta(minutes=30),
        )
    capability = f"oracle.metadata.read.{query_spec_digest[-16:]}"
    host = ExecutionHost(context.connection, context.realm_id, worker_label="oracle-metadata-read")
    job, created = host.jobs.enqueue(
        Job.create(
            realm_id=context.realm_id,
            project_id=project_id,
            kind=JobKind.READ_ONLY,
            idempotency_key=f"oracle-metadata-read:{task_plan.id}",
            resources=parse_requests(read=resources),
            required_capabilities=(capability,),
            max_attempts=1,
            work_item_id=work_id,
            plan_id=task_plan.id,
            step_id="oracle-metadata-read",
        )
    )
    if not created:
        governance.revoke_authorization(authorization.id, "oracle-metadata-read-replay")
        raise PolicyViolation("Oracle metadata read runtime replay reddedildi")
    claimed = host.acquire_work(capabilities=(capability,))
    if claimed is None or claimed.job.id != job.id:
        governance.revoke_authorization(authorization.id, "oracle-metadata-read-acquire-failed")
        raise PolicyViolation("Oracle metadata read job claim edilemedi")
    claim = host.claim_effect(
        claimed,
        operation="oracle-metadata-read",
        effect_digest=request.effect_digest,
        authorization_digest=authorization.authorization_digest,
        authorization_id=authorization.id,
        idempotency_key=str(task_plan.id),
        resources=parse_requests(read=resources),
        adapter_digest=digest(
            {
                "adapter": "python-oracledb-thin-dbms-metadata",
                "query_spec_digest": query_spec_digest,
            }
        ),
    )
    try:
        governance.require_authorized(
            request,
            authorization=authorization,
            consumed_by="cli:oracle-metadata-read",
        )
        snapshot = OracleMetadataClient().collect(datasource)
    except Exception as exc:
        if governance.authorizations.get(authorization.id).state is AuthorizationState.ISSUED:
            governance.revoke_authorization(
                authorization.id, "oracle-metadata-read-failed-before-consumption"
            )
        _record_failure(host, claim, exc, query_spec_digest)
        host.finish(
            claimed,
            outcome=AttemptOutcome.FAILED,
            failure_category=(
                FailureCategory.POLICY
                if isinstance(exc, PolicyViolation)
                else FailureCategory.ADAPTER
            ),
        )
        raise
    result_digest = snapshot.revision_digest
    try:
        with context.connection.transaction():
            receipt = host.record_success(
                claim,
                result_digest=result_digest,
                adapter_evidence_digest=digest(snapshot.sanitized()),
            )
            checkpoint = Checkpoint(
                checkpoint_id=f"oracle-metadata-read-{job.id}",
                project_id=str(project_id),
                work_item_id=str(work_id),
                plan_revision_id=str(task_plan.id),
                source_revision=f"{source_revision}:{query_spec_digest}",
                plan_steps=("oracle-metadata-read",),
                completed_steps=("oracle-metadata-read",),
                pending_steps=(),
                step_results=(("oracle-metadata-read", result_digest),),
                context_manifest_digest=query_spec_digest,
                journal_head_digest=receipt.adapter_evidence_digest or result_digest,
                next_safe_action="oracle-metadata-local-index",
                created_at=dt.datetime.now(dt.UTC),
            )
            checkpoint_id = ContextContinuityRepository(
                context.connection, context.realm_id, project_id, work_id
            ).store_checkpoint(checkpoint, task_plan_id=task_plan.id, job_id=job.id)
            if not host.finish(
                claimed, outcome=AttemptOutcome.SUCCEEDED, result_digest=result_digest
            ):
                raise PolicyViolation("Oracle metadata read job tamamlanamadi")
    except Exception:
        host.jobs.mark_recovery_required(job.id, "oracle-metadata-read-finalization-failed")
        raise
    return snapshot, {
        "plan_id": str(task_plan.id),
        "authorization_id": str(authorization.id),
        "job_id": str(job.id),
        "claim_id": str(claim.id),
        "receipt_id": str(receipt.id),
        "checkpoint_id": str(checkpoint_id),
    }


def _write_index(
    context: RealmContext,
    *,
    plan: OracleMetadataIndexPlan,
    work_id: UUID,
    actor_id: UUID,
    source_revision: str,
    home: str | None,
) -> tuple[OracleMetadataIndexResult, dict[str, str]]:
    embedding = build_verified_mac_embedding(plan.chunks)
    plan = replace(
        plan,
        embedding_profile=replace(
            plan.embedding_profile,
            provider_profile_digest=embedding.profile.profile_digest,
        ),
    )
    governance = GovernanceService(context.connection, context.realm, actor_id=actor_id)
    policy = governance.policies.current(DEFAULT_POLICY_NAME)
    if policy is None:
        raise PolicyViolation("Oracle metadata current policy ister")
    resources = (
        f"project:{plan.project_id}",
        f"db-object:{plan.project_id}:oracle-metadata:{plan.plan_digest}",
    )
    request = EffectRequest(
        action="oracle-metadata-index",
        effects=(EffectKind.DATABASE_WRITE,),
        resources=resources,
        data_classifications=(DataClassification.LOCAL_ONLY,),
        reversible=True,
        touches_external_system=False,
        required_capabilities=("database.write",),
    )
    graph = WorkGraphService(context.connection, context.realm, actor_id=actor_id)
    with context.connection.transaction():
        task_plan = graph.create_plan(
            work_id,
            source_revision=f"{source_revision}:{plan.snapshot.revision_digest}:{plan.plan_digest}",
            policy_digest=policy.policy_digest,
            steps=(
                PlanStep(
                    step_id="oracle-metadata-index",
                    title=f"Exact local Oracle DDL index {plan.plan_digest}",
                    effect=EffectKind.DATABASE_WRITE,
                    logical_resources=resources,
                    risk="high",
                ),
            ),
        )
        authorization = governance.issue_authorization(
            request=request,
            actor_id=actor_id,
            plan=task_plan,
            lifetime=dt.timedelta(minutes=30),
        )
    capability = f"oracle.metadata.index.{plan.plan_digest[-16:]}"
    host = ExecutionHost(context.connection, context.realm_id, worker_label="oracle-metadata-index")
    job, created = host.jobs.enqueue(
        Job.create(
            realm_id=context.realm_id,
            project_id=plan.project_id,
            kind=JobKind.MUTATION,
            idempotency_key=f"oracle-metadata-index:{task_plan.id}",
            resources=parse_requests(write=resources),
            required_capabilities=(capability,),
            max_attempts=1,
            work_item_id=work_id,
            plan_id=task_plan.id,
            step_id="oracle-metadata-index",
        )
    )
    if not created:
        governance.revoke_authorization(authorization.id, "oracle-metadata-index-replay")
        raise PolicyViolation("Oracle metadata index runtime replay reddedildi")
    claimed = host.acquire_work(capabilities=(capability,))
    if claimed is None or claimed.job.id != job.id:
        governance.revoke_authorization(authorization.id, "oracle-metadata-index-acquire-failed")
        raise PolicyViolation("Oracle metadata index job claim edilemedi")
    claim = host.claim_effect(
        claimed,
        operation="oracle-metadata-index",
        effect_digest=request.effect_digest,
        authorization_digest=authorization.authorization_digest,
        authorization_id=authorization.id,
        idempotency_key=str(task_plan.id),
        resources=parse_requests(write=resources),
        adapter_digest=digest(
            {"adapter": "local-oracle-metadata-index", "plan_digest": plan.plan_digest}
        ),
    )
    try:
        governance.require_authorized(
            request,
            authorization=authorization,
            consumed_by="cli:oracle-metadata-index",
        )
        application_context = build_context(home=home)
        result = apply_oracle_metadata_index(
            plan,
            connection=context.connection,
            knowledge=KnowledgeRepository(
                context.connection, context.realm_id, project_id=plan.project_id
            ),
            retrieval=RetrievalRepository(context.connection, context.realm_id),
            object_store=LocalContentAddressedStore(
                application_context.home / application_context.settings.object_store_relative
            ),
            embedding_provider=embedding.provider,
            embedding_policy=embedding.policy,
        )
    except Exception as exc:
        if governance.authorizations.get(authorization.id).state is AuthorizationState.ISSUED:
            governance.revoke_authorization(
                authorization.id, "oracle-metadata-index-failed-before-consumption"
            )
        _record_failure(host, claim, exc, plan.plan_digest)
        host.finish(
            claimed,
            outcome=AttemptOutcome.FAILED,
            failure_category=(
                FailureCategory.POLICY
                if isinstance(exc, PolicyViolation)
                else FailureCategory.ADAPTER
            ),
        )
        raise
    result_digest = digest(result.as_dict())
    try:
        with context.connection.transaction():
            receipt = host.record_success(
                claim,
                result_digest=result_digest,
                adapter_evidence_digest=digest(
                    {
                        "plan_digest": plan.plan_digest,
                        "source_id": str(result.source_id),
                        "document_id": str(result.document_id),
                        "chunk_count": result.chunk_count,
                        "vector_count": result.vector_count,
                    }
                ),
            )
            checkpoint = Checkpoint(
                checkpoint_id=f"oracle-metadata-index-{job.id}",
                project_id=str(plan.project_id),
                work_item_id=str(work_id),
                plan_revision_id=str(task_plan.id),
                source_revision=(
                    f"{source_revision}:{plan.snapshot.revision_digest}:{plan.plan_digest}"
                ),
                plan_steps=("oracle-metadata-index",),
                completed_steps=("oracle-metadata-index",),
                pending_steps=(),
                step_results=(("oracle-metadata-index", result_digest),),
                context_manifest_digest=plan.plan_digest,
                journal_head_digest=receipt.adapter_evidence_digest or result_digest,
                next_safe_action="independent-oracle-metadata-retrieval-verification",
                created_at=dt.datetime.now(dt.UTC),
            )
            checkpoint_id = ContextContinuityRepository(
                context.connection, context.realm_id, plan.project_id, work_id
            ).store_checkpoint(checkpoint, task_plan_id=task_plan.id, job_id=job.id)
            if not host.finish(
                claimed, outcome=AttemptOutcome.SUCCEEDED, result_digest=result_digest
            ):
                raise PolicyViolation("Oracle metadata index job tamamlanamadi")
    except Exception:
        host.jobs.mark_recovery_required(job.id, "oracle-metadata-index-finalization-failed")
        raise
    return result, {
        "plan_id": str(task_plan.id),
        "authorization_id": str(authorization.id),
        "job_id": str(job.id),
        "claim_id": str(claim.id),
        "receipt_id": str(receipt.id),
        "checkpoint_id": str(checkpoint_id),
    }


@app.command("index")
def index_command(
    query: Annotated[str, typer.Argument(help="Kayitli proje slug, alias veya kimligi")],
    config_relative: Annotated[
        str,
        typer.Option(
            "--config-relative",
            help="Proje kokune gore Spring Oracle datasource YAML relative yolu",
        ),
    ],
    work_id: Annotated[
        UUID | None,
        typer.Option("--work", help="Mevcut kanonik Work UUID; yoksa otomatik olusturulur"),
    ] = None,
    actor_id: Annotated[
        UUID | None,
        typer.Option("--actor", help="Aktif human actor UUID; tek aday varsa otomatik secilir"),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--uygula",
            help="Exact read/write authorization ile GET_DDL ve yerel vektor indeksini uygular",
        ),
    ] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Oracle schema nesnelerini satir verisi almadan DBMS_METADATA ile indeksler."""

    try:
        with RealmSession(home, realm) as context:
            project_id, service = _project(context, query)
            project = service.projects.get(project_id)
            report = service.evaluate(project_id)
            assert report.current_revision is not None
            source_revision = (
                f"{report.current_revision.revision}:{report.current_revision.tree_digest}"
            )
            datasource = load_project_oracle_datasource(
                service.resolve_source_root(project_id), config_relative
            )
            preflight = {
                "schema": "zekam-oracle-metadata-index-preflight/v1",
                "project_id": str(project_id),
                "project_slug": project.slug,
                "source_revision": source_revision,
                "datasource": datasource.sanitized(),
                "object_types": [item[0] for item in SUPPORTED_OBJECT_TYPES],
                "metadata_only": True,
                "row_data_included": False,
                "network_default": "deny-without-exact-authorization",
                "applied": False,
            }
            if not apply:
                document: dict[str, Any] = preflight
            else:
                exact_actor_id = _actor_id(context, actor_id)
                exact_work_id = _work_id(
                    context,
                    project_id=project_id,
                    project_slug=project.slug,
                    actor_id=exact_actor_id,
                    requested=work_id,
                )
                snapshot, read_runtime = _read_snapshot(
                    context,
                    project_id=project_id,
                    work_id=exact_work_id,
                    actor_id=exact_actor_id,
                    datasource=datasource,
                    source_revision=source_revision,
                )
                plan = build_oracle_metadata_index_plan(
                    project_id=project_id,
                    project_slug=project.slug,
                    snapshot=snapshot,
                )
                result, write_runtime = _write_index(
                    context,
                    plan=plan,
                    work_id=exact_work_id,
                    actor_id=exact_actor_id,
                    source_revision=source_revision,
                    home=home,
                )
                document = result.as_dict() | {
                    "work_id": str(exact_work_id),
                    "read_runtime": read_runtime,
                    "write_runtime": write_runtime,
                    "applied": True,
                }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    if not apply:
        console.print("[yellow]Dry-run. GET_DDL ve indeks icin --uygula verin.[/yellow]")
