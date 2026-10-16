"""Personal-skill lifecycle, explicit bounded trials and managed projections."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import typer

from zekam.application.bounded_file import read_bounded_text
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
from zekam.application.skill_evidence import load_skill_evidence, skill_evidence_record_plan
from zekam.application.native_skill_binding import resolve_native_skill_binding
from zekam.application.skill_native_invocation import (
    build_native_invocation_plan,
    read_native_invocation,
    run_native_invocation,
)
from zekam.application.skill_native_execution import (
    build_native_execution_plan,
    read_native_execution,
    run_native_execution,
)
from zekam.application.skill_execution_invocation import (
    build_execution_invocation_plan,
    run_execution_invocation,
)
from zekam.application.skill_outcome_verification import (
    build_outcome_verification_plan,
    read_outcome_verification,
    run_outcome_verification,
)
from zekam.application.skill_outcome_recording import (
    build_outcome_record_plan,
    read_native_outcome,
    run_outcome_recording,
)
from zekam.application.skill_audits import build_skill_audit_plan, read_skill_audit, run_skill_audit
from zekam.application.skill_measured_aggregate import (
    build_measured_aggregate_plan,
    read_measured_aggregate,
    run_measured_aggregate,
)
from zekam.application.skill_native_projection import (
    build_native_projection_plan,
    read_native_projection,
    run_native_projection,
)
from zekam.application.skill_trials import build_skill_trial_plan, load_trial_case, run_skill_trial
from zekam.application.skill_trial_aggregation import aggregate_skill_trials
from zekam.application.tournament_runtime import (
    build_tournament_plan,
    read_tournament,
    run_tournament,
)
from zekam.application.unit_test_provider_profile import load_batch_provider_profile
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.personal_skill import (
    PersonalSkillRevision,
    SkillEvaluationV2,
    SkillOriginEvidence,
    SkillOriginKind,
    SkillScopeKind,
)
from zekam.domain.skill_package import SkillPackage
from zekam.infrastructure.clients.opencode_executable import resolve_opencode_executable
from zekam.infrastructure.local_runtime_effects import LocalJournalOutboxPublisher
from zekam.infrastructure.sqlite.local_learning import SQLiteLocalLearning
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.sqlite.skill_lifecycle import SQLiteSkillLifecycle, activation_effect_body
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli.work_binding import resolve_optional_work_binding, work_binding_document

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
    apply_effect: Callable[[], dict[str, object]] | None = None,
    replay_effect: Callable[[], dict[str, object]] | None = None,
    job_bound_effect: Callable[[str], dict[str, object]] | None = None,
    logical_resources: tuple[str, ...] = (),
) -> dict[str, object]:
    """Run one bounded local mutation with a durable claim/receipt chain."""

    normalized_effect = json.loads(canonical_json(effect))
    if job_bound_effect is None and (apply_effect is None or replay_effect is None):
        raise PolicyViolation("Skill effect apply/replay handler gerekli")
    if job_bound_effect is not None and (apply_effect is not None or replay_effect is not None):
        raise PolicyViolation("Skill effect tek handler ailesi ister")
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
        if job_bound_effect is not None:
            document = job_bound_effect(job.id)
        else:
            assert apply_effect is not None
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
        if job_bound_effect is not None:
            document = job_bound_effect(job.id)
        else:
            assert replay_effect is not None
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


@app.command("native-binding")
def native_binding_command(
    observation_stdin: Annotated[bool, typer.Option("--observation-stdin")] = False,
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Resolve a content-free SDK load query; never creates invocation/Work state."""
    try:
        if not observation_stdin:
            raise PolicyViolation("Native skill SDK observation stdin required")
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            raise PolicyViolation("Native skill observation query exceeds limit")
        from zekam.application.skill_evidence import _constant, _pairs

        observation = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
        )
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        root = resolve_project_source(resolved_home, scopes[0][1])
        _emit(
            resolve_native_skill_binding(
                lifecycle,
                home=resolved_home,
                project_root=root,
                policy=build_context(home=home).settings.cli.integrations,
                allowed_scopes=scopes,
                observation=observation,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Native skill binding query okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("invocation-record")
def invocation_record_command(
    event_id: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    work_run: Annotated[str | None, typer.Option("--work-run")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    work_step: Annotated[str | None, typer.Option("--work-step")] = None,
    work_binding_digest: Annotated[str | None, typer.Option("--work-binding-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Associate one retained native load with explicit Work; never grade its effects."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        root = resolve_project_source(resolved_home, scopes[0][1])
        plan = build_native_invocation_plan(
            resolved_home,
            lifecycle=lifecycle,
            event_id=event_id,
            project_root=root,
            allowed_scopes=scopes,
        )
        binding = resolve_optional_work_binding(
            resolved_home,
            run_id=work_run,
            work_revision=work_revision,
            run_step_id=work_step,
            expected_binding_digest=work_binding_digest,
            project_id=plan["project_id"],
            plan_digest=plan["plan_digest"],
            apply=apply,
        )
        if not apply:
            _emit(
                plan
                | ({"work_binding": work_binding_document(binding)} if binding is not None else {})
            )
            return
        if binding is None or plan_digest != plan["plan_digest"]:
            raise PolicyViolation("Native invocation exact Work/plan binding required")
        _emit(
            run_native_invocation(
                resolved_home,
                lifecycle=lifecycle,
                plan=plan,
                binding=binding,
                project_root=root,
                allowed_scopes=scopes,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Native skill invocation gozlemi okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("invocation-report")
def invocation_report_command(
    reference: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Read historical recording receipt and immutable unverified chain."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        _emit(
            read_native_invocation(
                resolved_home,
                reference,
                lifecycle=lifecycle,
                project_id=registered.id,
                allowed_scopes=scopes,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Native invocation kayit kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("outcome-record")
def outcome_record_command(
    verifier_ref: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    work_run: Annotated[str | None, typer.Option("--work-run")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    work_step: Annotated[str | None, typer.Option("--work-step")] = None,
    work_binding_digest: Annotated[str | None, typer.Option("--work-binding-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Record one actual verifier receipt as an immutable bounded artifact outcome."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        plan = build_outcome_record_plan(
            resolved_home,
            lifecycle=lifecycle,
            verifier_ref=verifier_ref,
            project_id=registered.id,
            allowed_scopes=scopes,
        )
        binding = resolve_optional_work_binding(
            resolved_home,
            run_id=work_run,
            work_revision=work_revision,
            run_step_id=work_step,
            expected_binding_digest=work_binding_digest,
            project_id=registered.id,
            plan_digest=plan["plan_digest"],
            apply=apply,
        )
        if not apply:
            _emit(
                plan
                | ({"work_binding": work_binding_document(binding)} if binding is not None else {})
            )
            return
        if binding is None or plan_digest != plan["plan_digest"]:
            raise PolicyViolation("Native outcome exact recording Work/plan required")
        _emit(
            run_outcome_recording(
                resolved_home,
                lifecycle=lifecycle,
                plan=plan,
                binding=binding,
                allowed_scopes=scopes,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Native skill outcome recording kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("outcome-report")
def outcome_report_command(
    reference: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        _emit(
            read_native_outcome(
                resolved_home,
                reference,
                lifecycle=lifecycle,
                project_id=registered.id,
                allowed_scopes=scopes,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Native skill outcome kaydi okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("outcome-verify")
def outcome_verify_command(
    invocation_ref: Annotated[str, typer.Argument()],
    model: Annotated[str, typer.Option("--model")],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    provider_profile: Annotated[Path | None, typer.Option("--provider-profile")] = None,
    executable: Annotated[str, typer.Option("--opencode")] = "opencode",
    timeout_seconds: Annotated[int, typer.Option("--timeout-seconds", min=10, max=600)] = 180,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    authorize_remote: Annotated[bool, typer.Option("--authorize-remote")] = False,
    work_run: Annotated[str | None, typer.Option("--work-run")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    work_step: Annotated[str | None, typer.Option("--work-step")] = None,
    work_binding_digest: Annotated[str | None, typer.Option("--work-binding-digest")] = None,
    realm_id: Annotated[str | None, typer.Option("--realm-id")] = None,
    coordinator_id: Annotated[str | None, typer.Option("--coordinator-assignment-id")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Plan or execute a separate native verifier for actual invocation evidence."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        root = resolve_project_source(resolved_home, scopes[0][1])
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        profile = load_batch_provider_profile(provider_profile)
        binary = resolve_opencode_executable(executable)
        plan, context = build_outcome_verification_plan(
            resolved_home,
            lifecycle=lifecycle,
            invocation_ref=invocation_ref,
            project_id=registered.id,
            allowed_scopes=scopes,
            verifier_model=model,
            executable=binary,
            root=root,
            provider_profile=profile,
            timeout_seconds=timeout_seconds,
        )
        binding = resolve_optional_work_binding(
            resolved_home,
            run_id=work_run,
            work_revision=work_revision,
            run_step_id=work_step,
            expected_binding_digest=work_binding_digest,
            project_id=registered.id,
            plan_digest=plan["plan_digest"],
            apply=apply,
        )
        if not apply:
            _emit(
                plan
                | ({"work_binding": work_binding_document(binding)} if binding is not None else {})
            )
            return
        if binding is None or plan_digest != plan["plan_digest"]:
            raise PolicyViolation("Outcome verifier exact Work/plan required")
        if load_batch_provider_profile(provider_profile) != profile:
            raise PolicyViolation("Outcome verifier reviewed provider profile changed")
        _emit(
            run_outcome_verification(
                resolved_home,
                lifecycle=lifecycle,
                plan=plan,
                context=context,
                binding=binding,
                allowed_scopes=scopes,
                root=root,
                executable=binary,
                realm_id=UUID(realm_id or ""),
                coordinator_id=UUID(coordinator_id or ""),
                provider_profile=profile,
                authorize_remote=authorize_remote,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Native skill outcome verifier girdisi okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("outcome-verification-report")
def outcome_verification_report_command(
    reference: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        _emit(
            read_outcome_verification(
                resolved_home,
                reference,
                lifecycle=lifecycle,
                project_id=registered.id,
                allowed_scopes=scopes,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Native skill outcome verifier kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("invocation-from-execution")
def invocation_from_execution_command(
    execution_ref: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    work_run: Annotated[str | None, typer.Option("--work-run")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    work_step: Annotated[str | None, typer.Option("--work-step")] = None,
    work_binding_digest: Annotated[str | None, typer.Option("--work-binding-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Record actual native execution using its original Work/run identity."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        plan = build_execution_invocation_plan(
            resolved_home,
            lifecycle=lifecycle,
            execution_ref=execution_ref,
            project_id=registered.id,
            allowed_scopes=scopes,
        )
        binding = resolve_optional_work_binding(
            resolved_home,
            run_id=work_run,
            work_revision=work_revision,
            run_step_id=work_step,
            expected_binding_digest=work_binding_digest,
            project_id=registered.id,
            plan_digest=plan["plan_digest"],
            apply=apply,
        )
        if not apply:
            _emit(
                plan
                | ({"work_binding": work_binding_document(binding)} if binding is not None else {})
            )
            return
        if binding is None or plan_digest != plan["plan_digest"]:
            raise PolicyViolation("Execution invocation exact recording Work/plan required")
        _emit(
            run_execution_invocation(
                resolved_home,
                lifecycle=lifecycle,
                plan=plan,
                binding=binding,
                allowed_scopes=scopes,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Native execution invocation kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("execute-native")
def execute_native_command(
    package_root: Annotated[Path, typer.Option("--package-root")],
    case_file: Annotated[Path, typer.Option("--case-file")],
    model: Annotated[str, typer.Option("--model")],
    runtime_root: Annotated[Path, typer.Option("--runtime-root")],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    provider_profile: Annotated[Path | None, typer.Option("--provider-profile")] = None,
    executable: Annotated[str, typer.Option("--opencode")] = "opencode",
    timeout_seconds: Annotated[int, typer.Option("--timeout-seconds", min=10, max=600)] = 180,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    authorize_remote: Annotated[bool, typer.Option("--authorize-remote")] = False,
    work_run: Annotated[str | None, typer.Option("--work-run")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    work_step: Annotated[str | None, typer.Option("--work-step")] = None,
    work_binding_digest: Annotated[str | None, typer.Option("--work-binding-digest")] = None,
    realm_id: Annotated[str | None, typer.Option("--realm-id")] = None,
    coordinator_id: Annotated[str | None, typer.Option("--coordinator-assignment-id")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Plan or execute one actual native skill tool task; oracle stays local."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        root = resolve_project_source(resolved_home, scopes[0][1])
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        package = SkillPackage.read_directory(package_root.resolve(strict=True))
        case = load_trial_case(case_file)
        profile = load_batch_provider_profile(provider_profile)
        binary = resolve_opencode_executable(executable)
        runtime = runtime_root.resolve(strict=True)
        plan = build_native_execution_plan(
            resolved_home,
            lifecycle=lifecycle,
            package=package,
            case=case,
            project_id=registered.id,
            model_id=model,
            executable=binary,
            root=root,
            runtime_root=runtime,
            allowed_scopes=scopes,
            provider_profile=profile,
            timeout_seconds=timeout_seconds,
        )
        binding = resolve_optional_work_binding(
            resolved_home,
            run_id=work_run,
            work_revision=work_revision,
            run_step_id=work_step,
            expected_binding_digest=work_binding_digest,
            project_id=registered.id,
            plan_digest=plan["plan_digest"],
            apply=apply,
        )
        if not apply:
            _emit(
                plan
                | ({"work_binding": work_binding_document(binding)} if binding is not None else {})
            )
            return
        if binding is None or plan_digest != plan["plan_digest"]:
            raise PolicyViolation("Native execution exact Work/plan binding required")

        def validate_inputs():
            if (
                SkillPackage.read_directory(package_root.resolve(strict=True)).package_digest
                != package.package_digest
                or digest(load_trial_case(case_file)) != digest(case)
                or load_batch_provider_profile(provider_profile) != profile
                or resolve_project_source(resolved_home, scopes[0][1]) != root
            ):
                raise PolicyViolation("Native execution package/case/profile/source input drift")

        _emit(
            run_native_execution(
                resolved_home,
                lifecycle=lifecycle,
                binding=binding,
                package=package,
                case=case,
                plan=plan,
                root=root,
                runtime_root=runtime,
                executable=binary,
                allowed_scopes=scopes,
                realm_id=UUID(realm_id or ""),
                coordinator_id=UUID(coordinator_id or ""),
                validate_inputs=validate_inputs,
                authorize_remote=authorize_remote,
                provider_profile=profile,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Native skill execution girdisi okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("execution-report")
def execution_report_command(
    reference: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Regrade actual native tool/final artifact from its canonical receipt chain."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        _emit(
            read_native_execution(
                resolved_home,
                reference,
                lifecycle=lifecycle,
                project_id=registered.id,
                allowed_scopes=scopes,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Native skill execution kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("tournament-run")
def tournament_run_command(
    trial: Annotated[list[str], typer.Option("--trial")],
    case_file: Annotated[Path, typer.Option("--case-file")],
    model: Annotated[str, typer.Option("--model")],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    provider_profile: Annotated[Path | None, typer.Option("--provider-profile")] = None,
    executable: Annotated[str, typer.Option("--opencode")] = "opencode",
    timeout_seconds: Annotated[int, typer.Option("--timeout-seconds", min=10, max=600)] = 180,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    authorize_remote: Annotated[bool, typer.Option("--authorize-remote")] = False,
    authorize_unmetered: Annotated[bool, typer.Option("--authorize-unmetered-selector")] = False,
    work_run: Annotated[str | None, typer.Option("--work-run")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    work_step: Annotated[str | None, typer.Option("--work-step")] = None,
    work_binding_digest: Annotated[str | None, typer.Option("--work-binding-digest")] = None,
    realm_id: Annotated[str | None, typer.Option("--realm-id")] = None,
    coordinator_id: Annotated[str | None, typer.Option("--coordinator-assignment-id")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Plan or run one native selection over actual receipt-bound paired trials."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        root = resolve_project_source(resolved_home, scopes[0][1])
        binary = resolve_opencode_executable(executable)
        profile = load_batch_provider_profile(provider_profile)
        case = load_trial_case(case_file)

        def prepare():
            if (
                load_batch_provider_profile(provider_profile) != profile
                or digest(load_trial_case(case_file)) != digest(case)
                or resolve_project_source(resolved_home, scopes[0][1]) != root
            ):
                raise PolicyViolation("Tournament case/profile/source changed after plan")
            return build_tournament_plan(
                resolved_home,
                references=tuple(trial),
                case=case,
                project_id=registered.id,
                model_id=model,
                root=root,
                executable=binary,
                provider_profile=profile,
                timeout_seconds=timeout_seconds,
            )

        plan, _context = prepare()
        binding = resolve_optional_work_binding(
            resolved_home,
            run_id=work_run,
            work_revision=work_revision,
            run_step_id=work_step,
            expected_binding_digest=work_binding_digest,
            project_id=registered.id,
            plan_digest=plan["plan_digest"],
            apply=apply,
        )
        if not apply:
            _emit(
                plan
                | ({"work_binding": work_binding_document(binding)} if binding is not None else {})
            )
            return
        if plan_digest != plan["plan_digest"] or binding is None:
            raise PolicyViolation("Tournament exact plan digest and canonical Work required")
        _emit(
            run_tournament(
                resolved_home,
                binding=binding,
                plan=plan,
                case=case,
                root=root,
                executable=binary,
                realm_id=UUID(realm_id or ""),
                coordinator_id=UUID(coordinator_id or ""),
                validate_inputs=lambda: prepare(),
                provider_profile=profile,
                authorize_remote=authorize_remote,
                authorize_unmetered=authorize_unmetered,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Tournament bounded girdi/kanit okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("tournament-report")
def tournament_report_command(
    reference: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Reopen selection, builder sources and actual native dispatch without calls."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        _emit(read_tournament(resolved_home, reference, project_id=registered.id))
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Tournament terminal kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("trial-run")
def trial_run_command(
    revision_digest: Annotated[str, typer.Argument()],
    package_root: Annotated[Path, typer.Option("--package-root")],
    case_file: Annotated[Path, typer.Option("--case-file")],
    model: Annotated[str, typer.Option("--model")],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    provider_profile: Annotated[Path | None, typer.Option("--provider-profile")] = None,
    executable: Annotated[str, typer.Option("--opencode")] = "opencode",
    timeout_seconds: Annotated[int, typer.Option("--timeout-seconds", min=10, max=600)] = 180,
    repetition: Annotated[int, typer.Option("--repetition", min=0, max=99)] = 0,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    authorize_remote: Annotated[bool, typer.Option("--authorize-remote")] = False,
    work_run: Annotated[str | None, typer.Option("--work-run")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    work_step: Annotated[str | None, typer.Option("--work-step")] = None,
    work_binding_digest: Annotated[str | None, typer.Option("--work-binding-digest")] = None,
    realm_id: Annotated[str | None, typer.Option("--realm-id")] = None,
    coordinator_id: Annotated[str | None, typer.Option("--coordinator-assignment-id")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Plan or run one real paired trial; the local oracle is never disclosed."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        context = build_context(home=home)
        store = SQLiteOperationalStore(lifecycle.operational_path)
        with store.unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        source_root = resolve_project_source(resolved_home, scopes[0][1])
        package = SkillPackage.read_directory(package_root.resolve(strict=True))
        case = load_trial_case(case_file)
        profile = load_batch_provider_profile(provider_profile)
        binary = resolve_opencode_executable(executable)
        objects = LocalContentAddressedStore(resolved_home / context.settings.object_store_relative)

        def validate_inputs() -> None:
            current = lifecycle.evaluation_revision(revision_digest, allowed_scopes=scopes)
            if current["package_digest"] != package.package_digest:
                raise PolicyViolation("Skill trial candidate/package binding drift")
            if (
                SkillPackage.read_directory(package_root.resolve(strict=True)).package_digest
                != package.package_digest
            ):
                raise PolicyViolation("Skill trial package changed after plan")
            if digest(load_trial_case(case_file)) != digest(case):
                raise PolicyViolation("Skill trial case changed after plan")
            if resolve_project_source(resolved_home, scopes[0][1]) != source_root:
                raise PolicyViolation("Skill trial source root drift")
            current_profile = load_batch_provider_profile(provider_profile)
            if current_profile != profile:
                raise PolicyViolation("Skill trial provider profile drift")

        validate_inputs()
        plan = build_skill_trial_plan(
            revision_digest=revision_digest,
            package=package,
            case=case,
            project_id=registered.id,
            model_id=model,
            executable=binary,
            objects=objects,
            provider_profile=profile,
            timeout_seconds=timeout_seconds,
            source_root=source_root,
            repetition=repetition,
        )
        binding = resolve_optional_work_binding(
            resolved_home,
            run_id=work_run,
            work_revision=work_revision,
            run_step_id=work_step,
            expected_binding_digest=work_binding_digest,
            project_id=registered.id,
            plan_digest=plan["plan_digest"],
            apply=apply,
        )
        if not apply:
            _emit(
                plan
                | ({"work_binding": work_binding_document(binding)} if binding is not None else {})
            )
            return
        if plan_digest != plan["plan_digest"] or binding is None:
            raise PolicyViolation(
                "Skill trial exact plan digest and canonical Work binding required"
            )
        _emit(
            run_skill_trial(
                resolved_home,
                binding=binding,
                package=package,
                case=case,
                plan=plan,
                root=source_root,
                executable=binary,
                realm_id=UUID(realm_id or ""),
                coordinator_id=UUID(coordinator_id or ""),
                validate_inputs=validate_inputs,
                authorize_remote=authorize_remote,
                provider_profile=profile,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Skill trial bounded girdi/kimlik okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("trial-aggregate")
def trial_aggregate_command(
    revision_digest: Annotated[str, typer.Argument()],
    trial: Annotated[list[str], typer.Option("--trial")],
    case_file: Annotated[list[Path], typer.Option("--case-file")],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Read actual scoped terminal trials and regrade their output artifacts."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        candidate = lifecycle.evaluation_revision(revision_digest, allowed_scopes=scopes)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        if not 1 <= len(trial) <= 100 or not 1 <= len(case_file) <= 100:
            raise PolicyViolation("Skill aggregate bounded trial/case list required")
        _emit(
            aggregate_skill_trials(
                resolved_home,
                references=tuple(trial),
                cases=tuple(load_trial_case(path) for path in case_file),
                project_id=registered.id,
                revision_digest=revision_digest,
                package_digest=str(candidate["package_digest"]),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Skill aggregate kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("audit-run")
def audit_run_command(
    revision_digest: Annotated[str, typer.Argument()],
    trial: Annotated[list[str], typer.Option("--trial")],
    case_file: Annotated[list[Path], typer.Option("--case-file")],
    model: Annotated[str, typer.Option("--model")],
    role: Annotated[str, typer.Option("--role")] = "verifier",
    prior_verifier: Annotated[str | None, typer.Option("--prior-verifier")] = None,
    project: Annotated[str, typer.Option("--project")] = "zekam",
    provider_profile: Annotated[Path | None, typer.Option("--provider-profile")] = None,
    executable: Annotated[str, typer.Option("--opencode")] = "opencode",
    timeout_seconds: Annotated[int, typer.Option("--timeout-seconds", min=10, max=600)] = 180,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    authorize_remote: Annotated[bool, typer.Option("--authorize-remote")] = False,
    work_run: Annotated[str | None, typer.Option("--work-run")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    work_step: Annotated[str | None, typer.Option("--work-step")] = None,
    work_binding_digest: Annotated[str | None, typer.Option("--work-binding-digest")] = None,
    realm_id: Annotated[str | None, typer.Option("--realm-id")] = None,
    coordinator_id: Annotated[str | None, typer.Option("--coordinator-assignment-id")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Plan or execute one distinct-model native verifier/reviewer audit."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        candidate = lifecycle.evaluation_revision(revision_digest, allowed_scopes=scopes)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        if not 1 <= len(trial) <= 100 or not 1 <= len(case_file) <= 100:
            raise PolicyViolation("Skill audit bounded trial/case list required")
        root = resolve_project_source(resolved_home, scopes[0][1])
        binary = resolve_opencode_executable(executable)
        profile = load_batch_provider_profile(provider_profile)

        def prepare():
            if lifecycle.evaluation_revision(revision_digest, allowed_scopes=scopes) != candidate:
                raise PolicyViolation("Skill audit candidate drift")
            if resolve_project_source(resolved_home, scopes[0][1]) != root:
                raise PolicyViolation("Skill audit source root drift")
            current_profile = load_batch_provider_profile(provider_profile)
            if current_profile != profile:
                raise PolicyViolation("Skill audit provider profile drift")
            return build_skill_audit_plan(
                resolved_home,
                references=tuple(trial),
                cases=tuple(load_trial_case(path) for path in case_file),
                project_id=registered.id,
                revision_digest=revision_digest,
                package_digest=str(candidate["package_digest"]),
                root=root,
                executable=binary,
                model_id=model,
                role=role,
                prior_reference=prior_verifier,
                provider_profile=profile,
                timeout_seconds=timeout_seconds,
            )

        plan, context = prepare()
        binding = resolve_optional_work_binding(
            resolved_home,
            run_id=work_run,
            work_revision=work_revision,
            run_step_id=work_step,
            expected_binding_digest=work_binding_digest,
            project_id=registered.id,
            plan_digest=plan["plan_digest"],
            apply=apply,
        )
        if not apply:
            _emit(
                plan
                | ({"work_binding": work_binding_document(binding)} if binding is not None else {})
            )
            return
        if plan_digest != plan["plan_digest"] or binding is None:
            raise PolicyViolation(
                "Skill audit exact plan digest and canonical Work binding required"
            )

        def validate_inputs():
            current_plan, current_context = prepare()
            if canonical_json(current_plan) != canonical_json(plan) or canonical_json(
                current_context
            ) != canonical_json(context):
                raise PolicyViolation("Skill audit current terminal evidence changed")

        _emit(
            run_skill_audit(
                resolved_home,
                binding=binding,
                plan=plan,
                context=context,
                root=root,
                executable=binary,
                realm_id=UUID(realm_id or ""),
                coordinator_id=UUID(coordinator_id or ""),
                validate_inputs=validate_inputs,
                authorize_remote=authorize_remote,
                provider_profile=profile,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Skill audit bounded girdi/kanit okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("audit-report")
def audit_report_command(
    reference: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Read one exact scoped terminal audit and reopen its native decision evidence."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        _emit(read_skill_audit(resolved_home, reference, project_id=registered.id))
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Skill audit terminal kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("aggregate-record")
def aggregate_record_command(
    revision_digest: Annotated[str, typer.Argument()],
    trial: Annotated[list[str], typer.Option("--trial")],
    case_file: Annotated[list[Path], typer.Option("--case-file")],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    work_run: Annotated[str | None, typer.Option("--work-run")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    work_step: Annotated[str | None, typer.Option("--work-step")] = None,
    work_binding_digest: Annotated[str | None, typer.Option("--work-binding-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Record a real local evaluator result under a canonical Work claim/receipt."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        candidate = lifecycle.evaluation_revision(revision_digest, allowed_scopes=scopes)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        if not 1 <= len(trial) <= 100 or not 1 <= len(case_file) <= 100:
            raise PolicyViolation("Skill aggregate record bounded trial/case list required")
        cases = tuple(load_trial_case(path) for path in case_file)
        plan, _preview = build_measured_aggregate_plan(
            resolved_home,
            references=tuple(trial),
            cases=cases,
            project_id=registered.id,
            revision_digest=revision_digest,
            package_digest=str(candidate["package_digest"]),
        )
        binding = resolve_optional_work_binding(
            resolved_home,
            run_id=work_run,
            work_revision=work_revision,
            run_step_id=work_step,
            expected_binding_digest=work_binding_digest,
            project_id=registered.id,
            plan_digest=plan["plan_digest"],
            apply=apply,
        )
        if not apply:
            _emit(
                plan
                | ({"work_binding": work_binding_document(binding)} if binding is not None else {})
            )
            return
        if binding is None or plan_digest != plan["plan_digest"]:
            raise PolicyViolation("Skill aggregate record exact Work/plan binding required")

        def validate_inputs():
            if lifecycle.evaluation_revision(revision_digest, allowed_scopes=scopes) != candidate:
                raise PolicyViolation("Skill aggregate record candidate scope/body drift")
            if canonical_json([load_trial_case(path) for path in case_file]) != canonical_json(
                cases
            ):
                raise PolicyViolation("Skill aggregate record case file drift")

        _emit(
            run_measured_aggregate(
                resolved_home,
                binding=binding,
                plan=plan,
                cases=cases,
                validate_inputs=validate_inputs,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Skill aggregate record kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("aggregate-report")
def aggregate_report_command(
    reference: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Reopen a scoped evaluator receipt and regrade its retained case/source chain."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        _emit(read_measured_aggregate(resolved_home, reference, project_id=registered.id))
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Skill aggregate terminal kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("evaluation-from-audits")
def evaluation_from_audits_command(
    evaluator: Annotated[str, typer.Option("--evaluator")],
    verifier: Annotated[str, typer.Option("--verifier")],
    reviewer: Annotated[str, typer.Option("--reviewer")],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    work_run: Annotated[str | None, typer.Option("--work-run")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    work_step: Annotated[str | None, typer.Option("--work-step")] = None,
    work_binding_digest: Annotated[str | None, typer.Option("--work-binding-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Derive lifecycle rows from actual native evidence without granting activation."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        plan, _evaluation, _review = build_native_projection_plan(
            resolved_home,
            project_id=registered.id,
            evaluator_ref=evaluator,
            verifier_ref=verifier,
            reviewer_ref=reviewer,
        )
        candidate = lifecycle.evaluation_revision(plan["revision_digest"], allowed_scopes=scopes)
        if candidate["package_digest"] != plan["package_digest"]:
            raise PolicyViolation("Skill native projection candidate/package drift")
        binding = resolve_optional_work_binding(
            resolved_home,
            run_id=work_run,
            work_revision=work_revision,
            run_step_id=work_step,
            expected_binding_digest=work_binding_digest,
            project_id=registered.id,
            plan_digest=plan["plan_digest"],
            apply=apply,
        )
        if not apply:
            _emit(
                plan
                | ({"work_binding": work_binding_document(binding)} if binding is not None else {})
            )
            return
        if binding is None or plan_digest != plan["plan_digest"]:
            raise PolicyViolation("Skill native projection exact Work/plan binding required")

        def validate_inputs():
            if (
                lifecycle.evaluation_revision(plan["revision_digest"], allowed_scopes=scopes)
                != candidate
            ):
                raise PolicyViolation("Skill native projection candidate scope/body changed")

        _emit(
            run_native_projection(
                resolved_home,
                lifecycle=lifecycle,
                plan=plan,
                binding=binding,
                allowed_scopes=scopes,
                validate_inputs=validate_inputs,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Skill native projection kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("evaluation-report")
def evaluation_report_command(
    reference: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Read a receipt-bound native lifecycle projection and both ledger rows."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        with SQLiteOperationalStore(lifecycle.operational_path).unit_of_work() as uow:
            registered = uow.resolve_project(scopes[0][1])
        _emit(
            read_native_projection(
                resolved_home,
                reference,
                lifecycle=lifecycle,
                project_id=registered.id,
                allowed_scopes=scopes,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        typer.echo("Hata: Skill evaluation terminal kaniti okunamadi", err=True)
        raise typer.Exit(70) from exc


def _record_evidence_command(
    evidence_file: Path,
    *,
    kind: str,
    project: str,
    apply: bool,
    plan_digest: str | None,
    home: str | None,
) -> None:
    """Consume existing terminal evidence behind a separate recording claim."""
    try:
        body = load_skill_evidence(evidence_file, kind=kind)
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        lifecycle.require_revision_scope(body["revision_digest"], allowed_scopes=scopes)
        plan = skill_evidence_record_plan(body, kind=kind, project_ref=scopes[0][1])
        if not apply:
            _emit(plan | {"apply": False, "execution_evidence_verified": False})
            return
        if plan_digest != plan["plan_digest"]:
            raise PolicyViolation("Skill evidence --plan-digest exact olmali")

        def result_document(value: str) -> dict[str, object]:
            result = {
                "schema": "zekam-skill-evidence-record-result/v2",
                "kind": kind,
                f"{kind}_digest": value,
                "plan_digest": plan["plan_digest"],
                "activated": False,
                "provider_calls": 0,
                "grants_authority": False,
            }
            return result | {"receipt_digest": digest(result)}

        def persist() -> dict[str, object]:
            # Re-read scope at effect time; the input cannot grant foreign scope.
            lifecycle.require_revision_scope(body["revision_digest"], allowed_scopes=scopes)
            if kind == "evaluation":
                evaluation = SkillEvaluationV2(
                    **{
                        key: value
                        for key, value in body.items()
                        if key not in {"schema", "activatable", "grants_authority"}
                    }
                )
                value = lifecycle.record_evaluation(evaluation, now=dt.datetime.now(dt.UTC))
            else:
                value = lifecycle.record_review(
                    **{
                        key: value
                        for key, value in body.items()
                        if key not in {"schema", "grants_authority"}
                    },
                    now=dt.datetime.now(dt.UTC),
                )
            return result_document(value)

        def replay() -> dict[str, object]:
            return result_document(lifecycle.recorded_evidence_digest(body, kind=kind))

        _emit(
            _run_claimed_effect(
                lifecycle,
                resolved_home,
                operation=plan["effect"]["operation"],
                effect=plan["effect"],
                idempotency_key=f"skill-{kind}-record-v2:{plan['plan_digest']}",
                apply_effect=persist,
                replay_effect=replay,
                logical_resources=(f"skill-registry:personal-v2:{scopes[0][1]}",),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError) as exc:
        typer.echo("Hata: Skill evidence dosyasi okunamadi", err=True)
        raise typer.Exit(70) from exc


@app.command("evaluation-record")
def evaluation_record_command(
    evidence_file: Annotated[Path, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Record measured evaluation only when both exact terminal receipts exist."""
    _record_evidence_command(
        evidence_file,
        kind="evaluation",
        project=project,
        apply=apply,
        plan_digest=plan_digest,
        home=home,
    )


@app.command("review-record")
def review_record_command(
    evidence_file: Annotated[Path, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Record a review backed by its distinct terminal verification job."""
    _record_evidence_command(
        evidence_file,
        kind="review",
        project=project,
        apply=apply,
        plan_digest=plan_digest,
        home=home,
    )


@app.command("lifecycle-plan")
def lifecycle_plan_command(
    revision_digest: Annotated[str, typer.Argument()],
    action: Annotated[str, typer.Option("--action")] = "active",
    rollback_from: Annotated[str | None, typer.Option("--rollback-from")] = None,
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Plan a reviewed activation, rollback or lifecycle action without granting it."""
    try:
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", project)
        _emit(
            lifecycle.activation_plan(
                revision_digest,
                action=action,
                allowed_scopes=scopes,
                rollback_from_revision_digest=rollback_from,
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc


@app.command("lifecycle-apply")
def lifecycle_apply_command(
    plan_file: Annotated[Path, typer.Argument()],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Apply an exact scoped plan behind canonical claim/receipt and CAS."""
    try:
        plan = json.loads(read_bounded_text(plan_file, max_bytes=64 * 1024))
        if (
            not isinstance(plan, dict)
            or set(plan)
            != {"schema", "effect", "scope", "effect_digest", "grants_authority", "plan_digest"}
            or plan["schema"] != "zekam-skill-activation-plan/v2"
            or plan["grants_authority"] is not False
            or digest({k: v for k, v in plan.items() if k != "plan_digest"}) != plan["plan_digest"]
        ):
            raise PolicyViolation("Skill activation strict plan/digest gerekli")
        effect, scope = plan["effect"], plan["scope"]
        if (
            not isinstance(effect, dict)
            or not isinstance(scope, dict)
            or set(scope) != {"kind", "ref"}
            or scope["kind"] != "project"
            or effect.get("operation") != "skill.activate-v2"
            or digest(effect) != plan["effect_digest"]
        ):
            raise PolicyViolation("Skill activation effect/scope drift")
        for field in ("revision_digest", "evaluation_digest", "review_digest"):
            if not isinstance(effect[field], str):
                raise PolicyViolation("Skill activation digest tipi gecersiz")
            parse_digest(effect[field])
        for value in (
            effect["expected_previous_event_digest"],
            effect.get("rollback_from_revision_digest"),
        ):
            if value is not None:
                if not isinstance(value, str):
                    raise PolicyViolation("Skill activation CAS digest tipi gecersiz")
                parse_digest(value)
        if (
            not isinstance(scope["ref"], str)
            or not isinstance(effect["action"], str)
            or effect["action"] not in {"active", "deprecated", "revoked", "retired", "superseded"}
        ):
            raise PolicyViolation("Skill activation scope/action gecersiz")
        expected = activation_effect_body(
            effect["revision_digest"],
            effect["evaluation_digest"],
            effect["review_digest"],
            action=effect["action"],
            expected_previous_event_digest=effect["expected_previous_event_digest"],
            transition=effect.get("transition"),
            rollback_from_revision_digest=effect.get("rollback_from_revision_digest"),
        )
        if effect != expected:
            raise PolicyViolation("Skill activation exact effect shape gerekli")
        if apply and plan_digest != plan["plan_digest"]:
            raise PolicyViolation("Skill activation --plan-digest exact olmali")
        _learning, lifecycle, resolved_home = _paths(home)
        scopes = _cli_scope(lifecycle, resolved_home, "project", scope["ref"])
        lifecycle.require_revision_scope(effect["revision_digest"], allowed_scopes=scopes)
        if not apply:
            _emit(plan | {"apply": False})
            return

        def mutate(job_id: str) -> dict[str, object]:
            event = lifecycle.append_activation(
                effect["revision_digest"],
                action=effect["action"],
                expected_previous_event_digest=effect["expected_previous_event_digest"],
                authorization_job_id=job_id,
                now=dt.datetime.now(dt.UTC),
                transition=effect.get("transition"),
                rollback_from_revision_digest=effect.get("rollback_from_revision_digest"),
            )
            evidence = digest(
                {"activation_event_digest": event, "effect_digest": plan["effect_digest"]}
            )
            return {
                "activation_event_digest": event,
                "receipt_digest": evidence,
                "action": effect["action"],
                "grants_authority": False,
            }

        _emit(
            _run_claimed_effect(
                lifecycle,
                resolved_home,
                operation="skill.activate-v2",
                effect=effect,
                idempotency_key=f"skill-activation-v2:{plan_digest}",
                job_bound_effect=mutate,
                logical_resources=(f"skill-registry:personal-v2:{scope['ref']}",),
            )
        )
    except ZekamError as exc:
        typer.echo(f"Hata: {exc}", err=True)
        raise typer.Exit(70) from exc
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        typer.echo("Hata: Skill activation plan okunamadi", err=True)
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
