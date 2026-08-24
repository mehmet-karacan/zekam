"""`zekam project` komutlari.

Kurallar:

- Harici kaynak koku hicbir komutta yazilmaz.
- `add`, `remove`, `restore`, `rebind` ve `scan` mutation'dir; `--uygula` olmadan plan yazar.
- `list`, `show`, `source-root`, `resolve` ve `resume` salt okunurdur ve onay istemez.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.composition import build_context
from zekam.application.execution import ExecutionHost
from zekam.application.governance import DEFAULT_POLICY_NAME, EffectRequest, GovernanceService
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.project_knowledge_index import (
    ProjectIndexPlan,
    ProjectIndexResult,
    apply_project_index,
    build_project_index_plan,
)
from zekam.application.realm_context import RealmContext
from zekam.application.source_discovery import DiscoveryPolicy, discover
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import Checkpoint
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.identifiers import normalize_slug, validate_slug
from zekam.domain.project import IntegrationStage, ResolutionKind
from zekam.domain.realm import DEFAULT_REALM_SLUG, ActorKind, LifecycleStatus
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, FailureCategory, Job, JobKind
from zekam.domain.security import DataClassification
from zekam.domain.work import (
    AcceptanceCriterion,
    EffectKind,
    EvidenceRef,
    PlanStep,
    WorkState,
    WorkType,
)
from zekam.infrastructure.git import source_reader
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.project_repository import ProjectRepository, ProjectResolver
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli.session import (
    EXIT_AMBIGUOUS,
    EXIT_NOT_FOUND,
    HOME_HELP,
    REALM_HELP,
    RealmSession,
    fail,
    fail_from,
    sqlite_repository,
)

app = typer.Typer(name="project", help="Proje kayit defteri islemleri", no_args_is_help=True)
console = Console()


def _service(realm_context: RealmContext) -> ProjectIntegrationService:
    return ProjectIntegrationService(realm_context.connection, realm_context.realm)


def _resolve_project_id(realm_context: RealmContext, query: str) -> UUID:
    resolution = ProjectResolver(realm_context.connection, realm_context.realm_id).resolve(query)
    if resolution.resolved is None:
        raise _fail_resolution(resolution.kind, query)
    return resolution.resolved.project_id


def _resolve_exact_project_id(
    realm_context: RealmContext, query: str, *, include_archived: bool = False
) -> UUID:
    resolution = ProjectResolver(
        realm_context.connection,
        realm_context.realm_id,
        include_archived=include_archived,
    ).resolve(query)
    if resolution.resolved is None:
        raise _fail_resolution(resolution.kind, query)
    if resolution.kind not in {
        ResolutionKind.EXACT_ID,
        ResolutionKind.EXACT_SLUG,
        ResolutionKind.EXACT_ALIAS,
    }:
        raise PolicyViolation("Project mutation exact kimlik, slug veya alias ister")
    return resolution.resolved.project_id


def _index_plan(service: ProjectIntegrationService, project_id: Any) -> ProjectIndexPlan:
    report = service.evaluate(project_id)
    if report.is_stale or report.current_revision is None:
        raise PolicyViolation("Kaynak current revision olmadan indekslenemez")
    project = service.projects.get(project_id)
    return build_project_index_plan(
        project_id=project_id,
        project_slug=project.slug,
        source_root=service.resolve_source_root(project_id),
        source_revision=report.current_revision.revision,
        expected_tree_digest=report.current_revision.tree_digest,
    )


def _fresh_integration_plan(
    service: ProjectIntegrationService, project_id: UUID
) -> ProjectIndexPlan:
    """DB mutation olmadan mevcut source revision/tree icin tam plan uretir."""

    project = service.projects.get(project_id)
    root = service.resolve_source_root(project_id)
    discovery = discover(root, policy=DiscoveryPolicy())
    observation = source_reader.observe(root)
    source_revision = observation.commit if observation is not None else discovery.tree_digest
    return build_project_index_plan(
        project_id=project_id,
        project_slug=project.slug,
        source_root=root,
        source_revision=source_revision,
        expected_tree_digest=discovery.tree_digest,
    )


def _apply_index(
    realm_context: RealmContext,
    service: ProjectIntegrationService,
    plan: ProjectIndexPlan,
    *,
    home: str | None,
) -> ProjectIndexResult:
    context = build_context(home=home)
    result = apply_project_index(
        plan,
        connection=realm_context.connection,
        realm_id=realm_context.realm_id,
        object_store=LocalContentAddressedStore(
            context.home / context.settings.object_store_relative
        ),
    )
    stage, observed_revision_id, detail = service.states.get(plan.project_id)
    if stage is not IntegrationStage.CURRENT:
        raise PolicyViolation("Indeks yalniz current entegrasyona baglanabilir")
    detail["knowledge_index"] = {
        "state": "ready",
        "source_revision": plan.source_revision,
        "tree_digest": plan.tree_digest,
        "source_id": str(result.source_id),
        "document_id": str(result.document_id),
        "chunk_count": result.chunk_count,
        "vector_count": result.vector_count,
        "embedding_model_ref": plan.embedding_profile.model_ref,
        "embedding_dimension": plan.embedding_profile.dimension,
        "embedding_profile_digest": plan.embedding_profile.profile_digest,
        "plan_digest": plan.plan_digest,
        "remote_provider_used": False,
    }
    service.states.set(
        plan.project_id,
        stage=stage,
        observed_revision_id=observed_revision_id,
        detail=detail,
    )
    return result


def _integration_actor_id(realm_context: RealmContext, requested: UUID | None) -> UUID:
    actors = ActorRepository(realm_context.connection, realm_context.realm_id)
    if requested is not None:
        actor = actors.get(requested)
        if actor.kind is not ActorKind.HUMAN or actor.status is not LifecycleStatus.ACTIVE:
            raise PolicyViolation("Project integrate actor aktif human olmali")
        return actor.id
    candidates = tuple(
        actor
        for actor in actors.list_all()
        if actor.kind is ActorKind.HUMAN and actor.status is LifecycleStatus.ACTIVE
    )
    if len(candidates) != 1:
        raise PolicyViolation("Project integrate exact tek aktif human actor ister")
    return candidates[0].id


def _integration_work_id(
    realm_context: RealmContext,
    *,
    project_id: UUID,
    project_slug: str,
    actor_id: UUID,
    requested: UUID | None,
) -> UUID:
    graph = WorkGraphService(realm_context.connection, realm_context.realm, actor_id=actor_id)
    if requested is None:
        work = graph.create_item(
            project_id=project_id,
            type=WorkType.TASK,
            title=f"{project_slug} tam source ve vector entegrasyonu",
            summary="Capability scan, secret-safe chunk ve yerel vector indeksini kanoniklestir",
            acceptance_criteria=(
                AcceptanceCriterion("Kaynak revision ve guvenli dosya kapsami dogrulanir"),
                AcceptanceCriterion("Chunk ve vector sayilari exact eslesir"),
                AcceptanceCriterion("Retrieval ve runtime receipt bagimsiz dogrulanir"),
            ),
        )
        work = graph.transition(work.id, WorkState.READY)
        work = graph.transition(work.id, WorkState.ACTIVE)
        return work.id
    work = graph.items.get(requested)
    if work.project_id != project_id:
        raise PolicyViolation("Project integrate Work/project binding mismatch")
    if work.state is WorkState.PROPOSED:
        work = graph.transition(work.id, WorkState.READY)
    if work.state in {WorkState.READY, WorkState.BLOCKED, WorkState.VERIFICATION}:
        work = graph.transition(work.id, WorkState.ACTIVE)
    if work.state is not WorkState.ACTIVE:
        raise PolicyViolation("Project integrate Work active duruma getirilemedi")
    return work.id


def _apply_project_lifecycle_with_runtime(
    realm_context: RealmContext,
    repository: ProjectRepository,
    plan: dict[str, Any],
    *,
    actor_id: UUID,
) -> tuple[Any, dict[str, Any]]:
    """Project archive/restore effect'ini authority, claim ve receipt ile yurutur."""

    project_id = UUID(str(plan["project_id"]))
    action = "project-remove" if plan["target_status"] == "archived" else "project-restore"
    plan_digest = digest(plan)
    source_revision = f"project-revision:{plan['expected_revision']}:{plan_digest}"
    resources = (f"project:{project_id}:lifecycle",)
    request = EffectRequest(
        action=action,
        effects=(EffectKind.DATABASE_WRITE,),
        resources=resources,
        data_classifications=(DataClassification.LOCAL_ONLY,),
        reversible=True,
        required_capabilities=("database.write",),
    )
    governance = GovernanceService(realm_context.connection, realm_context.realm, actor_id=actor_id)
    policy = governance.ensure_default_policy()
    graph = WorkGraphService(realm_context.connection, realm_context.realm, actor_id=actor_id)
    work = graph.create_item(
        project_id=project_id,
        type=WorkType.MAINTENANCE,
        title=f"{plan['slug']} project lifecycle {plan['target_status']}",
        summary="Project registry durumunu kanonik gecmisi silmeden degistir",
        acceptance_criteria=(
            AcceptanceCriterion("Exact project revision yeniden dogrulanir"),
            AcceptanceCriterion("Source dosyalari ve kanonik gecmis silinmez"),
            AcceptanceCriterion("Authorization, claim ve terminal receipt yazilir"),
        ),
    )
    work = graph.transition(work.id, WorkState.READY)
    work = graph.transition(work.id, WorkState.ACTIVE)
    with realm_context.connection.transaction():
        task_plan = graph.create_plan(
            work.id,
            source_revision=source_revision,
            policy_digest=policy.policy_digest,
            steps=(
                PlanStep(
                    step_id=action,
                    title=f"Exact {action} {plan_digest}",
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
            lifetime=dt.timedelta(minutes=15),
        )
    capability = f"project.lifecycle.{plan_digest[-16:]}"
    host = ExecutionHost(realm_context.connection, realm_context.realm_id, worker_label=action)
    job, created = host.jobs.enqueue(
        Job.create(
            realm_id=realm_context.realm_id,
            project_id=project_id,
            kind=JobKind.MUTATION,
            idempotency_key=f"{action}:{plan_digest}",
            resources=parse_requests(write=resources),
            required_capabilities=(capability,),
            max_attempts=1,
            work_item_id=work.id,
            plan_id=task_plan.id,
            step_id=action,
        )
    )
    if not created:
        governance.revoke_authorization(authorization.id, f"{action}-runtime-replay")
        graph.transition(work.id, WorkState.CANCELLED, reason=f"{action} runtime replay")
        raise PolicyViolation("Project lifecycle runtime replay reddedildi")
    claimed = host.acquire_work(capabilities=(capability,))
    if claimed is None or claimed.job.id != job.id:
        governance.revoke_authorization(authorization.id, f"{action}-acquire-failed")
        host.jobs.mark_recovery_required(job.id, f"{action}-acquire-failed-no-effect")
        graph.transition(work.id, WorkState.CANCELLED, reason=f"{action} acquire failed")
        raise PolicyViolation("Project lifecycle runtime job claim edilemedi")
    claim = host.claim_effect(
        claimed,
        operation=action,
        effect_digest=request.effect_digest,
        authorization_digest=authorization.authorization_digest,
        authorization_id=authorization.id,
        idempotency_key=plan_digest,
        resources=parse_requests(write=resources),
        adapter_digest=digest({"adapter": "project-lifecycle/v1", "plan": plan_digest}),
    )
    try:
        governance.require_authorized(
            request, authorization=authorization, consumed_by=f"cli:{action}"
        )
        if plan["target_status"] == LifecycleStatus.ARCHIVED.value:
            result = repository.archive(
                project_id,
                expected_revision=int(plan["expected_revision"]),
                exclude_work_id=work.id,
                exclude_job_id=job.id,
            )
        else:
            result = repository.set_status(
                project_id,
                LifecycleStatus.ACTIVE,
                expected_revision=int(plan["expected_revision"]),
            )
        result_digest = digest(result.as_dict())
        receipt = host.record_success(
            claim,
            result_digest=result_digest,
            adapter_evidence_digest=digest(
                {"plan_digest": plan_digest, "project_revision": result.revision}
            ),
        )
        checkpoint = Checkpoint(
            checkpoint_id=f"{action}-{job.id}",
            project_id=str(project_id),
            work_item_id=str(work.id),
            plan_revision_id=str(task_plan.id),
            source_revision=source_revision,
            plan_steps=(action,),
            completed_steps=(action,),
            pending_steps=(),
            step_results=((action, result_digest),),
            context_manifest_digest=plan_digest,
            journal_head_digest=receipt.adapter_evidence_digest or result_digest,
            next_safe_action=(
                "project-rebind" if result.status is LifecycleStatus.ACTIVE else "none"
            ),
            created_at=dt.datetime.now(dt.UTC),
        )
        stored_checkpoint = ContextContinuityRepository(
            realm_context.connection,
            realm_context.realm_id,
            project_id,
            work.id,
        ).store_checkpoint(checkpoint, task_plan_id=task_plan.id, job_id=job.id)
        host.finish(claimed, outcome=AttemptOutcome.SUCCEEDED, result_digest=result_digest)
        current_work = graph.items.get(work.id)
        current_work = graph.transition(current_work.id, WorkState.VERIFICATION)
        graph.transition(
            current_work.id,
            WorkState.COMPLETED,
            evidence=(
                EvidenceRef(
                    kind="runtime-receipt",
                    reference=str(receipt.id),
                    digest_value=result_digest,
                ),
            ),
        )
    except Exception as exc:
        if host.ledger.receipt_for_claim(claim.id) is None:
            host.record_failure(
                claim,
                category=(
                    FailureCategory.POLICY
                    if isinstance(exc, PolicyViolation)
                    else FailureCategory.ADAPTER
                ),
                failure_digest=digest(
                    {"error_type": type(exc).__name__, "plan_digest": plan_digest}
                ),
            )
        host.finish(claimed, outcome=AttemptOutcome.FAILED)
        current_work = graph.items.get(work.id)
        if current_work.state is not WorkState.CANCELLED:
            graph.transition(
                current_work.id,
                WorkState.CANCELLED,
                reason=f"{action} failed with terminal receipt",
            )
        raise
    return result, {
        "work_id": str(work.id),
        "task_plan_id": str(task_plan.id),
        "authorization_id": str(authorization.id),
        "job_id": str(job.id),
        "claim_id": str(claim.id),
        "receipt_id": str(receipt.id),
        "checkpoint_id": str(stored_checkpoint),
        "result_digest": result_digest,
        "grants_authority": False,
    }


def _apply_index_with_runtime(
    realm_context: RealmContext,
    service: ProjectIntegrationService,
    plan: ProjectIndexPlan,
    *,
    home: str | None,
    actor_id: UUID,
    work_id: UUID,
    refresh_scan: bool,
) -> tuple[ProjectIndexResult, dict[str, Any]]:
    """DB mutation'i exact authority, claim, receipt ve checkpoint ile yurutur."""

    governance = GovernanceService(realm_context.connection, realm_context.realm, actor_id=actor_id)
    policy = governance.policies.current(DEFAULT_POLICY_NAME)
    if policy is None:
        raise PolicyViolation("Project integrate current policy ister")
    resources = (
        f"project:{plan.project_id}",
        f"db-object:{plan.project_id}:knowledge:index:{plan.plan_digest}",
    )
    request = EffectRequest(
        action="project-knowledge-index",
        effects=(EffectKind.DATABASE_WRITE,),
        resources=resources,
        data_classifications=(DataClassification.LOCAL_ONLY,),
        reversible=True,
        touches_external_system=False,
        required_capabilities=("database.write",),
    )
    source_revision = f"{plan.source_revision}:{plan.tree_digest}:{plan.plan_digest}"
    graph = WorkGraphService(realm_context.connection, realm_context.realm, actor_id=actor_id)
    with realm_context.connection.transaction():
        task_plan = graph.create_plan(
            work_id,
            source_revision=source_revision,
            policy_digest=policy.policy_digest,
            steps=(
                PlanStep(
                    step_id="project-knowledge-index",
                    title=f"Exact local knowledge index {plan.plan_digest}",
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
    capability = f"knowledge.index.{plan.plan_digest[-16:]}"
    host = ExecutionHost(
        realm_context.connection,
        realm_context.realm_id,
        worker_label="project-knowledge-index",
    )
    job, created = host.jobs.enqueue(
        Job.create(
            realm_id=realm_context.realm_id,
            project_id=plan.project_id,
            kind=JobKind.MUTATION,
            idempotency_key=f"project-knowledge-index:{plan.plan_digest}",
            resources=parse_requests(write=resources),
            required_capabilities=(capability,),
            max_attempts=1,
            work_item_id=work_id,
            plan_id=task_plan.id,
            step_id="project-knowledge-index",
        )
    )
    if not created:
        governance.revoke_authorization(authorization.id, "project-knowledge-index-runtime-replay")
        raise PolicyViolation("Project knowledge index runtime replay reddedildi")
    claimed = host.acquire_work(capabilities=(capability,))
    if claimed is None or claimed.job.id != job.id:
        governance.revoke_authorization(
            authorization.id, "project-knowledge-index-runtime-acquire-failed"
        )
        host.jobs.mark_recovery_required(job.id, "project-knowledge-index-acquire-failed-no-effect")
        raise PolicyViolation("Project knowledge index runtime job claim edilemedi")
    claim = host.claim_effect(
        claimed,
        operation="project-knowledge-index",
        effect_digest=request.effect_digest,
        authorization_digest=authorization.authorization_digest,
        authorization_id=authorization.id,
        idempotency_key=plan.plan_digest,
        resources=parse_requests(write=resources),
        adapter_digest=digest(
            {"adapter": "local-project-knowledge-index", "plan_digest": plan.plan_digest}
        ),
    )
    try:
        governance.require_authorized(
            request,
            authorization=authorization,
            consumed_by="cli:project-integrate-index",
        )
        if refresh_scan:
            scan = service.scan(plan.project_id)
            if (
                scan.revision.revision != plan.source_revision
                or scan.revision.tree_digest != plan.tree_digest
            ):
                raise PolicyViolation("Project source scan preflight sonrasi drift")
        result = _apply_index(realm_context, service, plan, home=home)
        result_digest = digest(result.as_dict())
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
            checkpoint_id=f"project-knowledge-index-{job.id}",
            project_id=str(plan.project_id),
            work_item_id=str(work_id),
            plan_revision_id=str(task_plan.id),
            source_revision=source_revision,
            plan_steps=("project-knowledge-index",),
            completed_steps=("project-knowledge-index",),
            pending_steps=(),
            step_results=(("project-knowledge-index", result_digest),),
            context_manifest_digest=plan.plan_digest,
            journal_head_digest=receipt.adapter_evidence_digest or result_digest,
            next_safe_action="independent-retrieval-verification",
            created_at=dt.datetime.now(dt.UTC),
        )
        stored_checkpoint = ContextContinuityRepository(
            realm_context.connection,
            realm_context.realm_id,
            plan.project_id,
            work_id,
        ).store_checkpoint(checkpoint, task_plan_id=task_plan.id, job_id=job.id)
        host.finish(claimed, outcome=AttemptOutcome.SUCCEEDED, result_digest=result_digest)
    except Exception as exc:
        if host.ledger.receipt_for_claim(claim.id) is None:
            host.record_failure(
                claim,
                category=(
                    FailureCategory.POLICY
                    if isinstance(exc, PolicyViolation)
                    else FailureCategory.ADAPTER
                ),
                failure_digest=digest(
                    {"error_type": type(exc).__name__, "plan_digest": plan.plan_digest}
                ),
            )
        host.jobs.mark_recovery_required(job.id, "project-knowledge-index-failed-no-silent-retry")
        raise
    return result, {
        "work_id": str(work_id),
        "task_plan_id": str(task_plan.id),
        "authorization_id": str(authorization.id),
        "job_id": str(job.id),
        "claim_id": str(claim.id),
        "receipt_id": str(receipt.id),
        "receipt_status": receipt.status.value,
        "checkpoint_id": str(stored_checkpoint),
        "result_digest": result_digest,
        "grants_authority": False,
    }


@app.command("add")
def add_command(
    source: Annotated[Path, typer.Argument(help="Kaynak proje kok dizini")],
    name: Annotated[str | None, typer.Option("--name", help="Gorunen ad")] = None,
    slug: Annotated[str | None, typer.Option("--slug", help="Portable slug")] = None,
    alias: Annotated[
        list[str] | None, typer.Option("--alias", help="Ek takma ad (tekrarlanabilir)")
    ] = None,
    apply: Annotated[
        bool, typer.Option("--uygula", help="Gercekten kaydeder; verilmezse yalniz plan yazar")
    ] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Projeyi salt okunur baglantiyla kaydeder."""
    resolved = source.expanduser()
    if not resolved.is_dir():
        raise fail("Kaynak koku bir dizin olmali")
    resolved = resolved.resolve()

    kind = "git-repository" if source_reader.is_git_repository(resolved) else "directory"
    if not apply:
        table = Table(title="Proje kayit plani")
        table.add_column("Alan")
        table.add_column("Deger")
        table.add_row("kaynak turu", kind)
        table.add_row("kok etiketi", resolved.name)
        table.add_row("slug", slug or "(dizin adindan turetilir)")
        table.add_row("gorunen ad", name or resolved.name)
        table.add_row("alias", ", ".join(alias or []) or "-")
        table.add_row("erisim", "read-only")
        table.add_row("kaynak agacina yazma", "hayir")
        console.print(table)
        console.print("[yellow]Dry-run. Kaydetmek icin --uygula verin.[/yellow]")
        return

    try:
        sqlite = sqlite_repository(home, realm)
        if sqlite is not None:
            if alias:
                raise PolicyViolation("SQLite minimum profili proje alias'i desteklemez")
            selected_slug = validate_slug(slug) if slug else normalize_slug(resolved.name)
            sqlite_project = sqlite.create_project(
                slug=selected_slug,
                display_name=name or resolved.name,
                source_ref=f"source:{selected_slug}",
            )
            console.print(f"[green]Kaydedildi:[/green] {sqlite_project.slug} ({sqlite_project.id})")
            return
        with RealmSession(home, realm, create_realm=True) as realm_context:
            project = _service(realm_context).register(
                source_path=resolved,
                slug=slug,
                display_name=name,
                aliases=tuple(alias or []),
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Kaydedildi:[/green] {project.slug} ({project.id})")


@app.command("list")
def list_command(
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    include_archived: Annotated[
        bool, typer.Option("--include-archived", help="Arsivlenmis projeleri de gosterir")
    ] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kayitli projeleri listeler."""
    try:
        sqlite = sqlite_repository(home, realm)
        if sqlite is not None:
            rows: list[dict[str, Any]] = [
                {
                    "slug": project.slug,
                    "display_name": project.display_name,
                    "status": "active",
                    "id": str(project.id),
                    "aliases": [],
                }
                for project in sqlite.list_projects()
            ]
        else:
            with RealmSession(home, realm) as realm_context:
                repository = ProjectRepository(realm_context.connection, realm_context.realm_id)
                projects = repository.list_all(include_archived=include_archived)
                rows = [
                    {
                        "slug": project.slug,
                        "display_name": project.display_name,
                        "status": project.status.value,
                        "id": str(project.id),
                        "aliases": [item.alias for item in repository.aliases_of(project.id)],
                    }
                    for project in projects
                ]
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if output_json:
        console.print_json(json.dumps(rows, ensure_ascii=False))
        return
    table = Table(title="Projeler")
    table.add_column("Slug")
    table.add_column("Ad")
    table.add_column("Durum")
    table.add_column("Alias")
    for row in rows:
        table.add_row(
            str(row["slug"]),
            str(row["display_name"]),
            str(row["status"]),
            ", ".join(row["aliases"]),
        )
    console.print(table)


@app.command("remove")
def remove_command(
    query: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    apply: Annotated[
        bool, typer.Option("--uygula", help="Projeyi arsivler; kaynak dosyalari silmez")
    ] = False,
    expected_revision: Annotated[
        int | None, typer.Option("--expected-revision", help="Optimistic concurrency revision'i")
    ] = None,
    actor_id: Annotated[UUID | None, typer.Option("--actor", help="Aktif human actor UUID")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Projeyi ve gecmisini silmeden aktif kayit defterinden kaldirir."""

    try:
        if sqlite_repository(home, realm) is not None:
            raise PolicyViolation("Project remove SQLite minimum profilinde desteklenmiyor")
        with RealmSession(home, realm) as realm_context:
            repository = ProjectRepository(realm_context.connection, realm_context.realm_id)
            project_id = _resolve_exact_project_id(realm_context, query)
            project = repository.get(project_id)
            exact_revision = project.revision if expected_revision is None else expected_revision
            blockers = repository.archive_preflight(project.id)
            plan = {
                "schema": "zekam-project-removal-plan/v1",
                "project_id": str(project.id),
                "slug": project.slug,
                "current_status": project.status.value,
                "target_status": LifecycleStatus.ARCHIVED.value,
                "expected_revision": exact_revision,
                "source_files_deleted": False,
                "local_source_binding_removed": True,
                "history_deleted": False,
                "blockers": blockers,
                "preserved_records": [
                    "project-history",
                    "aliases",
                    "source-revisions",
                    "work-runtime-receipts",
                    "knowledge-memory-evidence",
                ],
            }
            document = plan | {"plan_digest": digest(plan), "applied": apply}
            if apply:
                exact_actor_id = _integration_actor_id(realm_context, actor_id)
                archived, runtime = _apply_project_lifecycle_with_runtime(
                    realm_context, repository, plan, actor_id=exact_actor_id
                )
                document["revision"] = archived.revision
                document["runtime"] = runtime
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False))
    if not apply:
        console.print("[yellow]Dry-run. Arsivlemek icin --uygula verin.[/yellow]")


@app.command("restore")
def restore_command(
    query: Annotated[str, typer.Argument(help="Arsivlenmis proje slug, alias veya kimlik")],
    apply: Annotated[bool, typer.Option("--uygula", help="Projeyi yeniden aktif eder")] = False,
    expected_revision: Annotated[
        int | None, typer.Option("--expected-revision", help="Optimistic concurrency revision'i")
    ] = None,
    actor_id: Annotated[UUID | None, typer.Option("--actor", help="Aktif human actor UUID")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Arsivlenmis projeyi geri getirir; source icin rebind gerekebilir."""

    try:
        if sqlite_repository(home, realm) is not None:
            raise PolicyViolation("Project restore SQLite minimum profilinde desteklenmiyor")
        with RealmSession(home, realm) as realm_context:
            repository = ProjectRepository(realm_context.connection, realm_context.realm_id)
            project_id = _resolve_exact_project_id(realm_context, query, include_archived=True)
            project = repository.get(project_id)
            if project.status is not LifecycleStatus.ARCHIVED:
                raise PolicyViolation("Yalniz arsivlenmis proje restore edilebilir")
            exact_revision = project.revision if expected_revision is None else expected_revision
            plan = {
                "schema": "zekam-project-restore-plan/v1",
                "project_id": str(project.id),
                "slug": project.slug,
                "current_status": project.status.value,
                "target_status": LifecycleStatus.ACTIVE.value,
                "expected_revision": exact_revision,
                "source_rebind_required": True,
            }
            document = plan | {"plan_digest": digest(plan), "applied": apply}
            if apply:
                exact_actor_id = _integration_actor_id(realm_context, actor_id)
                restored, runtime = _apply_project_lifecycle_with_runtime(
                    realm_context, repository, plan, actor_id=exact_actor_id
                )
                document["revision"] = restored.revision
                document["runtime"] = runtime
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False))
    if not apply:
        console.print("[yellow]Dry-run. Geri getirmek icin --uygula verin.[/yellow]")


@app.command("resolve")
def resolve_command(
    query: Annotated[str, typer.Argument(help="Dogal dil proje ifadesi")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Dogal dil ifadesini exact projeye cozer. Belirsizlikte secim ister."""
    try:
        with RealmSession(home, realm) as realm_context:
            resolution = ProjectResolver(realm_context.connection, realm_context.realm_id).resolve(
                query
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc

    console.print_json(json.dumps(resolution.as_dict(), ensure_ascii=False))
    if resolution.kind is ResolutionKind.AMBIGUOUS:
        raise typer.Exit(EXIT_AMBIGUOUS)
    if resolution.kind is ResolutionKind.NOT_FOUND:
        raise typer.Exit(EXIT_NOT_FOUND)


@app.command("show")
def show_command(
    query: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Projenin entegrasyon durumunu ve capability profilini gosterir."""
    try:
        with RealmSession(home, realm) as realm_context:
            resolution = ProjectResolver(realm_context.connection, realm_context.realm_id).resolve(
                query
            )
            if resolution.resolved is None:
                raise _fail_resolution(resolution.kind, query)
            service = _service(realm_context)
            report = service.evaluate(resolution.resolved.project_id)
            profile = service.profiles.latest_for_project(resolution.resolved.project_id)
            document = report.as_dict()
            document["capability_profile"] = None if profile is None else profile[1]
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("source-root")
def source_root_command(
    query: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Registry'de bagli bu makineye ozel exact gercek kaynak kokunu cozer."""
    try:
        with RealmSession(home, realm) as realm_context:
            project_id = _resolve_exact_project_id(realm_context, query)
            root = _service(realm_context).resolve_source_root(project_id)
            document = {
                "project_id": str(project_id),
                "source_root": str(root),
                "scope": "local-only",
                "project_copy": False,
                "detached_worktree": False,
                "grants_authority": False,
            }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False))


@app.command("scan")
def scan_command(
    query: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    apply: Annotated[
        bool, typer.Option("--uygula", help="Sonucu kaydeder; verilmezse yalniz rapor yazar")
    ] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kaynak agacini salt okunur tarar ve capability profili uretir."""
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            resolution = ProjectResolver(realm_context.connection, realm_context.realm_id).resolve(
                query
            )
            if resolution.resolved is None:
                raise _fail_resolution(resolution.kind, query)
            project_id = resolution.resolved.project_id
            if not apply:
                root = service.resolve_source_root(project_id)
                report = discover(root, policy=DiscoveryPolicy())
                console.print_json(json.dumps(report.as_dict(), ensure_ascii=False))
                console.print("[yellow]Dry-run. Kaydetmek icin --uygula verin.[/yellow]")
                return
            result = service.scan(project_id)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(result.as_dict(), ensure_ascii=False, default=str))


@app.command("index")
def index_command(
    query: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
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
            help="Lexical chunk ve yerel deterministik vektorleri kanonik store'a yazar",
        ),
    ] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Current source revision'i secret-safe bicimde chunk ve vektor indeksine alir."""

    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _resolve_project_id(realm_context, query)
            plan = _index_plan(service, project_id)
            if not apply:
                document = plan.as_dict() | {"applied": False}
            else:
                exact_actor_id = _integration_actor_id(realm_context, actor_id)
                exact_work_id = _integration_work_id(
                    realm_context,
                    project_id=project_id,
                    project_slug=plan.project_slug,
                    actor_id=exact_actor_id,
                    requested=work_id,
                )
                result, runtime = _apply_index_with_runtime(
                    realm_context,
                    service,
                    plan,
                    home=home,
                    actor_id=exact_actor_id,
                    work_id=exact_work_id,
                    refresh_scan=False,
                )
                document = result.as_dict() | {"runtime": runtime}
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    if not apply:
        console.print("[yellow]Dry-run. Yazmak icin --uygula verin.[/yellow]")


@app.command("integrate")
def integrate_command(
    query: Annotated[str, typer.Argument(help="Kayitli proje slug, alias veya kimligi")],
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
            help="Capability taramasi, lexical index ve yerel vektorleri birlikte kaydeder",
        ),
    ] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kayitli projeyi scan + source/chunk/vector zinciriyle tam entegre eder."""

    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _resolve_project_id(realm_context, query)
            plan = _fresh_integration_plan(service, project_id)
            if apply:
                exact_actor_id = _integration_actor_id(realm_context, actor_id)
                exact_work_id = _integration_work_id(
                    realm_context,
                    project_id=project_id,
                    project_slug=plan.project_slug,
                    actor_id=exact_actor_id,
                    requested=work_id,
                )
                index, runtime = _apply_index_with_runtime(
                    realm_context,
                    service,
                    plan,
                    home=home,
                    actor_id=exact_actor_id,
                    work_id=exact_work_id,
                    refresh_scan=True,
                )
                document = {
                    "schema": "zekam-project-integration-result/v1",
                    "project": service.projects.get(project_id).as_dict(),
                    "scan": service.evaluate(project_id).as_dict(),
                    "index": index.as_dict(),
                    "runtime": runtime,
                    "stage": "current",
                    "applied": True,
                }
            else:
                document = {
                    "schema": "zekam-project-integration-plan/v1",
                    "project_id": str(project_id),
                    "index": plan.as_dict(),
                    "scan_will_refresh": True,
                    "applied": False,
                }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    if not apply:
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")


@app.command("rebind")
def rebind_command(
    query: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    source: Annotated[Path, typer.Argument(help="Yeni kaynak kok dizini")],
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten uygular")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kaynak baska bir konuma tasindiginda baglantiyi tazeler."""
    resolved = source.expanduser()
    if not resolved.is_dir():
        raise fail("Kaynak koku bir dizin olmali")
    if not apply:
        console.print(f"yeni kok etiketi: {resolved.resolve().name}")
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            resolution = ProjectResolver(realm_context.connection, realm_context.realm_id).resolve(
                query
            )
            if resolution.resolved is None:
                raise _fail_resolution(resolution.kind, query)
            binding = service.rebind(resolution.resolved.project_id, source_path=resolved)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Yeniden baglandi:[/green] {binding.root_label} ({binding.status.value})")


@app.command("resume")
def resume_command(
    query: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Nerede kalindigini ve bir sonraki guvenli aksiyonu yazar."""
    try:
        with RealmSession(home, realm) as realm_context:
            resolution = ProjectResolver(realm_context.connection, realm_context.realm_id).resolve(
                query
            )
            if resolution.resolved is None:
                raise _fail_resolution(resolution.kind, query)
            report = _service(realm_context).evaluate(resolution.resolved.project_id)
    except ZekamError as exc:
        raise fail_from(exc) from exc

    table = Table(title=f"Devam durumu: {report.project.slug}")
    table.add_column("Alan")
    table.add_column("Deger")
    table.add_row("asama", report.stage.value)
    table.add_row("guncel mi", "evet" if report.is_current else "hayir")
    table.add_row("stale", "evet" if report.is_stale else "hayir")
    table.add_row("blocker", ", ".join(report.blockers) or "-")
    table.add_row("sonraki guvenli aksiyon", report.next_action)
    console.print(table)


def _fail_resolution(kind: ResolutionKind, query: str) -> typer.Exit:
    if kind is ResolutionKind.AMBIGUOUS:
        return fail(
            f"Belirsiz proje ifadesi: {query}. `project resolve` ile secin.", EXIT_AMBIGUOUS
        )
    return fail(f"Proje bulunamadi: {query}", EXIT_NOT_FOUND)
