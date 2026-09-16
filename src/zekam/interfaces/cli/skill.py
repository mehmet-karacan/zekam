"""Provider-free personal-skill lifecycle and managed projection CLI."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer

from zekam.application.client_integrations import integration_mutation_resource
from zekam.application.composition import build_context
from zekam.application.local_runtime_service import (
    LocalEffectDispatcher,
    LocalEffectRequest,
    LocalEffectResult,
    LocalRuntimeService,
)
from zekam.application.project_rag_runtime import resolve_project_source
from zekam.application.skill_packages import (
    apply_projection_plan,
    build_projection_plan,
    projection_receipt,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.personal_skill import (
    PersonalSkillRevision,
    SkillOriginEvidence,
    SkillOriginKind,
    SkillScopeKind,
)
from zekam.domain.skill_package import SkillPackage
from zekam.infrastructure.local_runtime_effects import LocalJournalOutboxPublisher
from zekam.infrastructure.sqlite.local_learning import SQLiteLocalLearning
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.skill_lifecycle import SQLiteSkillLifecycle

app = typer.Typer(name="skill", help="Kisisel skill katalogu ve guvenli paket yasam dongusu")


def _emit(document: object) -> None:
    typer.echo(json.dumps(document, ensure_ascii=True, sort_keys=True, default=str))


def _paths(home: str | None) -> tuple[SQLiteLocalLearning, SQLiteSkillLifecycle, Path]:
    context = build_context(home=home)
    learning_path = context.home / "state" / "learning.db"
    operational_path = context.settings.database.sqlite_path(context.home)
    learning = SQLiteLocalLearning(learning_path, operational_path=operational_path)
    return learning, SQLiteSkillLifecycle(learning_path, operational_path), context.home


def _scope(kind: str, reference: str) -> tuple[tuple[SkillScopeKind, str], ...]:
    try:
        scope_kind = SkillScopeKind(kind)
    except ValueError as exc:
        raise typer.BadParameter("scope-kind realm, project veya user olmali") from exc
    if not reference.strip():
        raise typer.BadParameter("scope-ref bos olamaz")
    return ((scope_kind, reference),)


def _cli_scope(
    lifecycle: SQLiteSkillLifecycle,
    home: Path,
    kind: str,
    reference: str,
) -> tuple[tuple[SkillScopeKind, str], ...]:
    requested = _scope(kind, reference)[0]
    if requested[0] is not SkillScopeKind.PROJECT:
        raise PolicyViolation("Personal skill CLI currently requires project scope")
    canonical = lifecycle.canonical_project_scope(requested[1])
    bound_root = resolve_project_source(home, canonical)
    current = Path.cwd().resolve(strict=True)
    try:
        current.relative_to(bound_root)
    except ValueError as exc:
        raise PolicyViolation(
            "Personal skill CLI scope is not bound to the current project"
        ) from exc
    return ((SkillScopeKind.PROJECT, canonical),)


def _run_claimed_effect(
    lifecycle: SQLiteSkillLifecycle,
    home: Path,
    *,
    operation: str,
    effect: dict[str, object],
    idempotency_key: str,
    apply_effect: Callable[[], dict[str, object]],
    replay_effect: Callable[[], dict[str, object]],
    logical_resources: tuple[str, ...] = (),
) -> dict[str, object]:
    """Run one bounded local mutation with a durable claim/receipt chain."""

    normalized_effect = json.loads(canonical_json(effect))
    if not isinstance(normalized_effect, dict):
        raise PolicyViolation("Skill local effect canonical body invalid")
    store = SQLiteLocalRuntimeStore(lifecycle.operational_path, existing_only=True)
    before = store.status()
    if (
        before.pending_outbox
        or before.claimed_outbox
        or before.recovery_outbox
        or before.recovery_jobs
        or before.open_recovery_cases
    ):
        raise PolicyViolation("Skill local effect refuses unrelated unresolved runtime work")
    completed: dict[str, dict[str, object]] = {}

    def evidence(document: dict[str, object]) -> str:
        value = document.get("receipt_digest")
        if not isinstance(value, str):
            raise PolicyViolation("Skill local effect receipt digest missing")
        from zekam.domain.canonical import parse_digest

        parse_digest(value)
        return value

    def execute(request: LocalEffectRequest) -> LocalEffectResult:
        if request.operation != operation or request.payload != normalized_effect:
            raise PolicyViolation("Skill local runtime effect binding drift")
        document = apply_effect()
        completed["document"] = document
        return LocalEffectResult("completed", evidence(document))

    service = LocalRuntimeService(
        store,
        effect_dispatcher=LocalEffectDispatcher(((operation, execute),)),
        outbox_publisher=LocalJournalOutboxPublisher(home / "runtime" / "local-effects"),
    )
    job, _created = store.enqueue(
        idempotency_key=idempotency_key,
        payload={"operation": operation, "effect": normalized_effect},
    )
    if job.state == "ready":
        service.run_worker_once(
            owner_id=f"skill-effect-{os.getpid()}",
            owner_pid=os.getpid(),
            owner_token=str(uuid4()),
            lease_seconds=30,
            job_id=job.id,
            resources=logical_resources,
        )
    elif job.state != "completed":
        raise PolicyViolation("Skill local effect existing runtime job needs recovery")
    document = completed.get("document")
    if document is None:
        document = replay_effect()
    terminal_evidence = evidence(document)
    snapshot = store.job_snapshot(job.id)
    if (
        snapshot is None
        or snapshot.get("state") != "completed"
        or snapshot.get("terminal_evidence_digest") != terminal_evidence
        or not isinstance(snapshot.get("effects"), list)
        or len(snapshot["effects"]) != 1
        or snapshot["effects"][0].get("operation") != operation
        or snapshot["effects"][0].get("receipt_status") != "completed"
        or snapshot["effects"][0].get("evidence_digest") != terminal_evidence
    ):
        raise PolicyViolation("Skill local effect terminal runtime receipt readback failed")
    publisher_token = str(uuid4())
    for _index in range(3):
        if store.status().pending_outbox == 0:
            break
        service.publish_outbox_once(
            owner_id=f"skill-effect-outbox-{os.getpid()}",
            owner_pid=os.getpid(),
            owner_token=publisher_token,
            lease_seconds=30,
        )
    after = store.status()
    if after.pending_outbox or after.claimed_outbox or after.recovery_outbox:
        raise PolicyViolation("Skill local effect outbox terminal readback failed")
    return document | {
        "operational_job_id": job.id,
        "operational_terminal_evidence_digest": terminal_evidence,
    }


def _apply_proposal_with_runtime(
    lifecycle: SQLiteSkillLifecycle,
    home: Path,
    revision: PersonalSkillRevision,
    origins: tuple[SkillOriginEvidence, ...],
    *,
    request_evidence_digest: str,
    plan_digest: str,
) -> dict[str, object]:
    lifecycle.validate_revision_scope(revision)
    operation = "skill.propose-v2"
    effect = {
        "schema": "zekam-skill-proposal-effect/v2",
        "plan_digest": plan_digest,
        "revision": revision.body(),
        "origin_digests": [item.evidence_digest for item in origins],
        "explicit_request_evidence_digest": request_evidence_digest,
    }
    store = SQLiteLocalRuntimeStore(lifecycle.operational_path, existing_only=True)
    before = store.status()
    if before.pending_outbox or before.claimed_outbox or before.recovery_outbox:
        raise PolicyViolation("Skill proposal refuses unrelated open outbox work")
    result: dict[str, str] = {}

    def execute(request: LocalEffectRequest) -> LocalEffectResult:
        if request.operation != operation or request.payload != effect:
            raise PolicyViolation("Skill proposal runtime effect binding drift")
        revision_digest = lifecycle.propose_revision(
            revision,
            origins,
            explicit_request_evidence_digest=request_evidence_digest,
            now=dt.datetime.now(dt.UTC),
        )
        evidence = digest(
            {
                "schema": "zekam-skill-proposal-effect-result/v2",
                "plan_digest": plan_digest,
                "revision_digest": revision_digest,
                "state": "candidate",
            }
        )
        result.update(revision_digest=revision_digest, evidence_digest=evidence)
        return LocalEffectResult("completed", evidence)

    service = LocalRuntimeService(
        store,
        effect_dispatcher=LocalEffectDispatcher(((operation, execute),)),
        outbox_publisher=LocalJournalOutboxPublisher(home / "runtime" / "local-effects"),
    )
    job, _created = store.enqueue(
        idempotency_key=f"skill-propose-v2:{plan_digest}",
        payload={"operation": operation, "effect": effect},
    )
    if job.state == "ready":
        token = str(uuid4())
        service.run_worker_once(
            owner_id=f"skill-propose-{os.getpid()}",
            owner_pid=os.getpid(),
            owner_token=token,
            lease_seconds=30,
            job_id=job.id,
        )
    elif job.state != "completed":
        raise PolicyViolation("Skill proposal existing runtime job needs recovery")
    if not result:
        revision_digest = lifecycle.propose_revision(
            revision,
            origins,
            explicit_request_evidence_digest=request_evidence_digest,
            now=dt.datetime.now(dt.UTC),
        )
        result.update(
            revision_digest=revision_digest,
            evidence_digest=digest(
                {
                    "schema": "zekam-skill-proposal-effect-result/v2",
                    "plan_digest": plan_digest,
                    "revision_digest": revision_digest,
                    "state": "candidate",
                }
            ),
        )
    snapshot = store.job_snapshot(job.id)
    if (
        snapshot is None
        or snapshot.get("state") != "completed"
        or snapshot.get("terminal_evidence_digest") != result["evidence_digest"]
        or not isinstance(snapshot.get("effects"), list)
        or len(snapshot["effects"]) != 1
        or snapshot["effects"][0].get("operation") != operation
        or snapshot["effects"][0].get("receipt_status") != "completed"
        or snapshot["effects"][0].get("evidence_digest") != result["evidence_digest"]
    ):
        raise PolicyViolation("Skill proposal terminal runtime receipt readback failed")
    publisher_token = str(uuid4())
    for _index in range(2):
        service.publish_outbox_once(
            owner_id=f"skill-propose-outbox-{os.getpid()}",
            owner_pid=os.getpid(),
            owner_token=publisher_token,
            lease_seconds=30,
        )
    after = store.status()
    if after.pending_outbox or after.claimed_outbox or after.recovery_outbox:
        raise PolicyViolation("Skill proposal outbox terminal readback failed")
    return {
        "revision_digest": result["revision_digest"],
        "evidence_digest": result["evidence_digest"],
        "operational_job_id": job.id,
    }


@app.command("prepare")
def prepare_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Plan or apply the online-backup-protected learning schema v2 migration."""

    try:
        learning, lifecycle, resolved_home = _paths(home)
        backup = resolved_home / "backups" / "learning-v1-before-v2-4d5d9f79.db"
        plan = learning.migration_plan_v2(backup)
        if not apply:
            _emit(plan)
            return
        if plan_digest is None:
            raise PolicyViolation("Skill prepare --plan-digest ister")
        effect: dict[str, object] = {
            "schema": "zekam-learning-migration-effect/v2",
            "plan_digest": plan_digest,
            "backup_ref": plan["backup_ref"],
        }
        _emit(
            _run_claimed_effect(
                lifecycle,
                resolved_home,
                operation="skill.prepare-v2",
                effect=effect,
                idempotency_key=f"skill-prepare-v2:{plan_digest}",
                apply_effect=lambda: learning.migrate_v1_to_v2(
                    backup, authorized_plan_digest=plan_digest
                ),
                replay_effect=lambda: learning.migrate_v1_to_v2(
                    backup, authorized_plan_digest=plan_digest
                ),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("status")
def status_command(
    home: Annotated[str | None, typer.Option("--home")] = None,
    project_root: Annotated[
        Path | None, typer.Option("--project-root", exists=True, file_okay=False)
    ] = None,
    scope_kind: Annotated[str, typer.Option("--scope-kind")] = "project",
    scope_ref: Annotated[str, typer.Option("--scope-ref")] = "zekam",
) -> None:
    """Report catalog, learning, distribution and acceptance separately."""

    try:
        learning, lifecycle, resolved_home = _paths(home)
        try:
            status = lifecycle.status()
        except PolicyViolation:
            backup = resolved_home / "backups" / "learning-v1-before-v2-4d5d9f79.db"
            _emit(
                {
                    "schema": "zekam-personal-skill-status/v2",
                    "state": "migration-required",
                    "migration": learning.migration_plan_v2(backup),
                    "skill_catalog": "blocked",
                    "skill_learning": "blocked",
                    "client_distribution": "not_configured",
                    "verified_execution": "not_run",
                    "native_acceptance": "not_run",
                    "grants_authority": False,
                }
            )
            return
        package_root = Path(__file__).resolve().parents[2] / "skills" / "zekam-arastirma-uygulama"
        distribution: dict[str, object] = {
            "support_level": "not_configured",
            "targets": (),
        }
        if package_root.is_dir():
            package = SkillPackage.read_directory(package_root)
            resolved_project = (
                Path.cwd().resolve() if project_root is None else project_root.resolve()
            )
            projection = build_projection_plan(resolved_project, package)
            states = tuple(str(item["state"]) for item in projection.targets)
            activation: dict[str, str] | None = None
            try:
                scopes = _cli_scope(lifecycle, resolved_home, scope_kind, scope_ref)
                bound_root = resolve_project_source(resolved_home, scopes[0][1]).resolve(
                    strict=True
                )
                if resolved_project.resolve(strict=True) != bound_root:
                    raise PolicyViolation(
                        "Skill status project root is not the registered source root"
                    )
                activation = lifecycle.require_active_package(
                    package.package_digest, allowed_scopes=scopes
                )
            except PolicyViolation:
                support_level = "blocked-unactivated"
            else:
                support_level = (
                    "instruction-distribution-only"
                    if states and all(state == "current" for state in states)
                    else "not_configured"
                )
            distribution = {
                "support_level": support_level,
                "targets": projection.targets,
                "package_digest": package.package_digest,
                "semantic_digest": package.semantic_digest,
                "active_revision": activation,
            }
        _emit(
            status
            | {
                "client_distribution": distribution,
                "installed_clients": {
                    name: shutil.which(executable) is not None
                    for name, executable in (
                        ("codex", "codex"),
                        ("opencode", "opencode"),
                        ("claude-code", "claude"),
                    )
                },
            }
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("list")
def list_command(
    scope_kind: Annotated[str, typer.Option("--scope-kind")] = "project",
    scope_ref: Annotated[str, typer.Option("--scope-ref")] = "zekam",
    maximum: Annotated[int, typer.Option("--maximum", min=1, max=50)] = 20,
    after: Annotated[str | None, typer.Option("--after")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """List only metadata visible in one explicitly allowed scope."""

    try:
        _learning, lifecycle, resolved_home = _paths(home)
        _emit(
            lifecycle.catalog(
                allowed_scopes=_cli_scope(lifecycle, resolved_home, scope_kind, scope_ref),
                maximum=maximum,
                after=after,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("inspect")
def inspect_command(
    revision_digest: Annotated[str, typer.Argument()],
    scope_kind: Annotated[str, typer.Option("--scope-kind")] = "project",
    scope_ref: Annotated[str, typer.Option("--scope-ref")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Load one authorized revision body and its provenance."""

    try:
        _learning, lifecycle, resolved_home = _paths(home)
        _emit(
            lifecycle.inspect_revision(
                revision_digest,
                allowed_scopes=_cli_scope(lifecycle, resolved_home, scope_kind, scope_ref),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("explain")
def explain_command(
    query: Annotated[str, typer.Argument()],
    scope_kind: Annotated[str, typer.Option("--scope-kind")] = "project",
    scope_ref: Annotated[str, typer.Option("--scope-ref")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Explain deterministic lexical selection or an explicit abstention."""

    try:
        _learning, lifecycle, resolved_home = _paths(home)
        _emit(
            lifecycle.discover(
                query,
                allowed_scopes=_cli_scope(lifecycle, resolved_home, scope_kind, scope_ref),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("propose")
def propose_command(
    package_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    scope_kind: Annotated[str, typer.Option("--scope-kind")] = "project",
    scope_ref: Annotated[str, typer.Option("--scope-ref")] = "zekam",
    version: Annotated[int, typer.Option("--version", min=1, max=100_000)] = 1,
    author_ref: Annotated[str, typer.Option("--author-ref")] = "user-requested-builder",
    trigger: Annotated[list[str] | None, typer.Option("--trigger")] = None,
    non_trigger: Annotated[list[str] | None, typer.Option("--non-trigger")] = None,
    request_evidence_digest: Annotated[
        str | None, typer.Option("--request-evidence-digest")
    ] = None,
    user_ref: Annotated[str, typer.Option("--user-ref")] = "local-user-explicit-request",
    work_ref: Annotated[str, typer.Option("--work-ref")] = "skill-proposal",
    run_ref: Annotated[str, typer.Option("--run-ref")] = "skill-proposal",
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Plan or persist an explicit-request candidate; this never activates it."""

    try:
        package = SkillPackage.read_directory(package_root.resolve())
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, scope_kind, scope_ref)
        scope = scopes[0]
        triggers = tuple(trigger or ())
        negatives = tuple(non_trigger or ())
        revision = PersonalSkillRevision(
            skill_id=package.name,
            name=package.name,
            description=package.description,
            version=version,
            scope_kind=scope[0],
            scope_ref=scope[1],
            package_digest=package.package_digest,
            author_ref=author_ref,
            trigger_terms=triggers,
            non_trigger_terms=negatives,
        )
        origins: tuple[SkillOriginEvidence, ...] = ()
        if request_evidence_digest is not None:
            origins = (
                SkillOriginEvidence(
                    kind=SkillOriginKind.USER_REQUEST,
                    evidence_digest=request_evidence_digest,
                    work_ref=work_ref,
                    run_ref=run_ref,
                    user_ref=user_ref,
                ),
            )
        body = {
            "schema": "zekam-skill-proposal-plan/v2",
            "revision": revision.body(),
            "package_digest": package.package_digest,
            "semantic_digest": package.semantic_digest,
            "origins": [item.as_dict() for item in origins],
            "explicit_request_evidence_digest": request_evidence_digest,
            "origin_policy": "two-independent-observations-or-explicit-request",
            "state": "candidate-plan",
            "provider_calls": 0,
            "network_calls": 0,
            "apply": False,
            "grants_authority": False,
        }
        exact_plan_digest = digest(body)
        if not apply:
            _emit(body | {"plan_digest": exact_plan_digest})
            return
        if plan_digest != exact_plan_digest:
            raise PolicyViolation("Skill propose exact --plan-digest ister")
        if request_evidence_digest is None:
            raise PolicyViolation("CLI apply currently exact explicit request evidence ister")
        applied = _apply_proposal_with_runtime(
            lifecycle,
            resolved_home,
            revision,
            origins,
            request_evidence_digest=request_evidence_digest,
            plan_digest=exact_plan_digest,
        )
        _emit(
            {
                "schema": "zekam-skill-proposal-receipt/v2",
                "plan_digest": exact_plan_digest,
                **applied,
                "state": "candidate",
                "activated": False,
                "provider_calls": 0,
                "network_calls": 0,
                "grants_authority": False,
            }
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("evaluate")
def evaluate_command(revision_digest: Annotated[str, typer.Argument()]) -> None:
    """Emit the exact provider-free evaluation contract without fabricating trials."""

    try:
        body = {
            "schema": "zekam-skill-evaluation-plan/v2",
            "revision_digest": revision_digest,
            "graders": ("artifact", "process", "preference", "efficiency", "safety"),
            "minimum_trials": 5,
            "holdout_required": True,
            "same_model_harness_tools_budget": True,
            "record_failed_equal_regressed_blocked": True,
            "provider_calls": 0,
            "apply": False,
            "grants_authority": False,
        }
        from zekam.domain.canonical import parse_digest

        parse_digest(revision_digest)
        _emit(body | {"plan_digest": digest(body)})
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("export")
def export_command(
    package_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    project_root: Annotated[Path, typer.Option("--project-root", exists=True, file_okay=False)],
    scope_kind: Annotated[str, typer.Option("--scope-kind")] = "project",
    scope_ref: Annotated[str, typer.Option("--scope-ref")] = "zekam",
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Plan or apply managed Codex/OpenCode/Claude instruction projections."""

    try:
        package = SkillPackage.read_directory(package_root.resolve())
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, scope_kind, scope_ref)
        bound_root = resolve_project_source(resolved_home, scopes[0][1]).resolve(strict=True)
        requested_root = project_root.resolve(strict=True)
        if requested_root != bound_root:
            raise PolicyViolation("Skill export requires exact registered project source root")
        activation = lifecycle.require_active_package(package.package_digest, allowed_scopes=scopes)
        policy = build_context(home=home).settings.cli.integrations
        quarantine_root = (
            resolved_home
            / "quarantine"
            / "client-integrations"
            / digest(os.path.normcase(str(requested_root))).removeprefix("sha256:")
        )
        plan = build_projection_plan(
            requested_root,
            package,
            policy=policy,
            quarantine_root=quarantine_root,
        )
        if not apply:
            _emit(plan.as_dict() | {"active_revision": activation})
            return
        if plan_digest is None:
            raise PolicyViolation("Skill export --plan-digest ister")
        effect: dict[str, object] = {
            "schema": "zekam-skill-export-effect/v1",
            "plan": plan.as_dict(),
            "active_revision": activation,
        }
        _emit(
            _run_claimed_effect(
                lifecycle,
                resolved_home,
                operation="skill.export-v1",
                effect=effect,
                idempotency_key=f"skill-export-v1-runtime-v2:{plan_digest}",
                apply_effect=lambda: apply_projection_plan(
                    plan, authorized_plan_digest=plan_digest
                ),
                replay_effect=lambda: projection_receipt(plan, authorized_plan_digest=plan_digest),
                logical_resources=(
                    integration_mutation_resource(
                        scope="project",
                        native_user_root=Path.home(),
                        project_root=requested_root,
                    ),
                ),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
