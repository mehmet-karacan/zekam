"""Native-agent publication surface backed by signed SQLite authority."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console

from zekam.application.bounded_file import read_bounded_bytes
from zekam.application.canonical_job_runtime import replay_canonical_job
from zekam.application.composition import build_context
from zekam.application.git_publication import GitPublicationPlan, GitPublicationService
from zekam.application.git_publication_trust import _strict_json, load_publication_trust
from zekam.application.git_publication_rotation import (
    build_trust_rotation_plan,
    preview_trust_rotation,
    read_registry_source,
    rotate_publication_trust,
)
from zekam.application.git_publication_enrollment import (
    build_trust_enrollment_plan,
    enroll_publication_trust,
    load_trust_enrollment_plan,
)
from zekam.application.git_publication_wire import (
    publication_authorization_from_body,
    publication_evidence_from_body,
    publication_plan_from_body,
)
from zekam.application.project_rag_runtime import resolve_project_source, resolve_registered_project
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed, ZekamError
from zekam.infrastructure.git.publication import BoundGitPushTransport
from zekam.infrastructure.sqlite.git_publication import (
    SQLiteGitPublicationLedger,
    approval_attestation_body,
    evidence_attestation_body,
    revocation_attestation_body,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli.session import HOME_HELP, fail_from

app = typer.Typer(name="publication", help="Imzali canonical Git yayini", no_args_is_help=True)
console = Console()


@app.command("trust-rotation-plan")
def trust_rotation_plan_command(
    registry_file: Annotated[Path, typer.Argument()],
    previous: Annotated[str, typer.Option("--previous")],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    try:
        context = _context(home)
        slug = resolve_registered_project(context.home, project)
        with SQLiteOperationalStore(
            context.settings.database.sqlite_path(context.home)
        ).unit_of_work() as uow:
            registered = uow.resolve_project(slug)
        plan = build_trust_rotation_plan(
            context.home,
            read_bounded_bytes(registry_file, max_bytes=32 * 1024),
            previous_reference=previous,
            project_id=registered.id,
        )
        console.print_json(json.dumps(plan))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise fail_from(
            ValidationFailed("Publication rotation source/registry input unavailable")
        ) from exc
    except ZekamError as exc:
        raise fail_from(exc) from exc


@app.command("trust-source")
def trust_source_command(
    reference: Annotated[str, typer.Argument()],
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    try:
        context = _context(home)
        console.print_json(json.dumps(read_registry_source(context.home, reference)))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise fail_from(
            ValidationFailed("Publication registry source receipt unavailable")
        ) from exc
    except ZekamError as exc:
        raise fail_from(exc) from exc


@app.command("trust-rotate")
def trust_rotate_command(
    plan_file: Annotated[Path, typer.Argument()],
    approval_file: Annotated[Path, typer.Option("--approval-file")],
    run_id: Annotated[UUID | None, typer.Option("--run-id")] = None,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    try:
        context = _context(home)
        plan = load_trust_enrollment_plan(plan_file)
        approval = _read(approval_file)
        if not apply:
            console.print_json(json.dumps(preview_trust_rotation(context.home, plan, approval)))
            return
        if run_id is None or plan_digest != plan["plan_digest"]:
            raise PolicyViolation("Publication rotation exact Work/plan apply binding required")
        with SQLiteOperationalStore(
            context.settings.database.sqlite_path(context.home)
        ).unit_of_work() as uow:
            work = uow.get_work(uow.get_run(str(run_id)).work_item_id)
        runtime = SQLiteLocalRuntimeStore(
            context.settings.database.sqlite_path(context.home), existing_only=True
        )
        binding = runtime.resolve_work_binding(
            run_id=str(run_id),
            work_revision=work.revision if work_revision is None else work_revision,
            project_id=plan["project_id"],
            plan_digest=plan_digest,
        )

        def validate():
            if canonical_json(load_trust_enrollment_plan(plan_file)) != canonical_json(
                plan
            ) or canonical_json(_read(approval_file)) != canonical_json(approval):
                raise PolicyViolation("Publication rotation plan/approval file drift")

        console.print_json(
            json.dumps(
                rotate_publication_trust(
                    context.home,
                    plan=plan,
                    binding=binding,
                    approval=approval,
                    validate_inputs=validate,
                )
            )
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise fail_from(
            ValidationFailed("Publication rotation plan/approval/readback unavailable")
        ) from exc
    except ZekamError as exc:
        raise fail_from(exc) from exc


@app.command("trust-plan")
def trust_plan_command(
    registry_file: Annotated[Path, typer.Argument()],
    project: Annotated[str, typer.Option("--project")] = "zekam",
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read a reviewed public registry and the existing admitted persistent pin."""
    try:
        context = _context(home)
        slug = resolve_registered_project(context.home, project)
        with SQLiteOperationalStore(
            context.settings.database.sqlite_path(context.home)
        ).unit_of_work() as uow:
            registered = uow.resolve_project(slug)
        plan = build_trust_enrollment_plan(
            context.home,
            read_bounded_bytes(registry_file, max_bytes=32 * 1024),
            project_id=registered.id,
        )
        console.print_json(json.dumps(plan))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise fail_from(
            ValidationFailed("Publication enrollment registry input unavailable")
        ) from exc
    except ZekamError as exc:
        raise fail_from(exc) from exc


@app.command("trust-enroll")
def trust_enroll_command(
    plan_file: Annotated[Path, typer.Argument()],
    run_id: Annotated[UUID | None, typer.Option("--run-id")] = None,
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    work_revision: Annotated[int | None, typer.Option("--work-revision")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Initial public registry only; exact canonical Work/claim/receipt, no keys."""
    try:
        context = _context(home)
        plan = load_trust_enrollment_plan(plan_file)
        if not apply:
            rebuilt = build_trust_enrollment_plan(
                context.home,
                canonical_json(plan["registry"]).encode(),
                project_id=plan["project_id"],
            )
            if canonical_json(rebuilt) != canonical_json(plan):
                raise PolicyViolation("Publication enrollment preview plan drift")
            console.print_json(
                json.dumps({"plan": plan, "apply": False, "grants_authority": False})
            )
            return
        if run_id is None or plan_digest != plan["plan_digest"]:
            raise PolicyViolation("Publication enrollment exact Work/plan apply binding required")
        store = SQLiteOperationalStore(context.settings.database.sqlite_path(context.home))
        with store.unit_of_work() as uow:
            work = uow.get_work(uow.get_run(str(run_id)).work_item_id)
        runtime = SQLiteLocalRuntimeStore(
            context.settings.database.sqlite_path(context.home), existing_only=True
        )
        binding = runtime.resolve_work_binding(
            run_id=str(run_id),
            work_revision=work.revision if work_revision is None else work_revision,
            project_id=plan["project_id"],
            plan_digest=plan_digest,
        )

        def validate():
            if canonical_json(load_trust_enrollment_plan(plan_file)) != canonical_json(plan):
                raise PolicyViolation("Publication enrollment plan file drift")

        result = enroll_publication_trust(
            context.home, plan=plan, binding=binding, validate_inputs=validate
        )
        console.print_json(json.dumps(result))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise fail_from(
            ValidationFailed("Publication enrollment plan/readback unavailable")
        ) from exc
    except ZekamError as exc:
        raise fail_from(exc) from exc


def _read(path: Path) -> dict:
    try:
        body = _strict_json(read_bounded_bytes(path, max_bytes=128 * 1024))
        if not isinstance(body, dict):
            raise ValueError("object")
        return body
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValidationFailed("Publication input bounded JSON object olmali") from exc


def _context(home):
    context = build_context(home=home)
    if (
        context.settings.database.sqlite_path(context.home).resolve()
        != (context.home / "state" / "operational.db").resolve()
    ):
        raise PolicyViolation("Publication canonical runtime exact operational.db yolu ister")
    return context


def _components(home):
    context = _context(home)
    configured_pin = context.settings.git_publication.trust_digest
    if configured_pin is None:
        raise ConfigurationError("Publication trust pin kalici config'te hazir degil")
    trust = load_publication_trust(context.home)
    if trust.trust_digest != configured_pin:
        raise PolicyViolation("Publication loaded settings/active registry trust digest drift")
    objects = LocalContentAddressedStore(context.home / context.settings.object_store_relative)
    ledger = SQLiteGitPublicationLedger(
        context.settings.database.sqlite_path(context.home),
        objects=objects,
        trusted_verifiers=trust.verifiers,
    )
    return context, trust, ledger


def _envelope(document, expected):
    if canonical_json(
        {key: value for key, value in document.items() if key != "attestation"}
    ) != canonical_json(expected):
        raise ValidationFailed("Publication attested envelope alanlari uyusmuyor")
    if type(document.get("attestation")) is not str or not 1 <= len(document["attestation"]) <= 512:
        raise ValidationFailed("Publication attestation eksik/bounded degil")


@app.command("plan")
def plan_command(
    project: Annotated[str, typer.Argument()],
    run_id: Annotated[UUID, typer.Argument()],
    executor_id: Annotated[UUID, typer.Option("--executor-id")],
    remote: Annotated[str, typer.Option("--remote")] = "origin",
    branch: Annotated[str, typer.Option("--branch")] = "main",
    run_step: Annotated[str | None, typer.Option("--run-step")] = None,
    test_evidence: Annotated[str | None, typer.Option("--test-evidence")] = None,
    verifier_evidence: Annotated[str | None, typer.Option("--verifier-evidence")] = None,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Observe subject first, then an exact full plan after real evidence exists."""
    try:
        context, trust, ledger = _components(home)
        slug = resolve_registered_project(context.home, project)
        root = resolve_project_source(context.home, slug)
        store = SQLiteOperationalStore(context.settings.database.sqlite_path(context.home))
        with store.unit_of_work() as uow:
            canonical_project = uow.resolve_project(slug)
            run = uow.get_run(str(run_id))
            work = uow.get_work(run.work_item_id)
        runtime = SQLiteLocalRuntimeStore(
            context.settings.database.sqlite_path(context.home), existing_only=True
        )
        binding = runtime.resolve_work_binding(
            run_id=str(run_id),
            work_revision=work.revision,
            project_id=canonical_project.id,
            plan_digest=run.plan_digest,
            run_step_id=run_step,
        )
        if (
            binding.config_revision_id != trust.config_revision_id
            or binding.config_digest != trust.config_digest
        ):
            raise PolicyViolation("Publication plan active trust/config binding drift")
        target = BoundGitPushTransport(root).inspect(
            remote=remote,
            ref=branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}",
        )
        plan = GitPublicationPlan(
            trust.realm_id,
            executor_id,
            binding,
            target,
            test_evidence or digest("test-evidence-pending"),
            verifier_evidence or digest("verification-evidence-pending"),
        )
        if (test_evidence is None) != (verifier_evidence is None):
            raise ValidationFailed("Publication full plan iki gercek evidence ister")
        if test_evidence is None:
            document = {
                "schema": "zekam-git-publication-subject/v1",
                "subject_digest": plan.subject_digest,
                "plan": plan.body(),
                "evidence_bound": False,
                "binding": binding.body(),
                "target": target.body(),
                "realm_id": str(trust.realm_id),
                "trust_digest": trust.trust_digest,
                "evidence_required": ["test", "verification"],
                "provider_calls": 0,
                "network_calls": 0,
                "read_only": True,
                "grants_authority": False,
            }
        else:
            for ref, kind in ((test_evidence, "test"), (verifier_evidence, "verification")):
                evidence = ledger.publication_evidence(ref)
                if (
                    evidence.kind != kind
                    or evidence.subject_digest != plan.subject_digest
                    or not evidence.passed
                ):
                    raise PolicyViolation("Publication plan evidence subject uyusmuyor")
            document = {
                "plan": plan.body(),
                "plan_digest": plan.plan_digest,
                "read_only": True,
                "grants_authority": False,
            }
        console.print_json(json.dumps(document, ensure_ascii=False))
    except ZekamError as exc:
        raise fail_from(exc) from exc


@app.command("test-source")
def test_source_command(
    plan_file: Annotated[Path, typer.Argument()],
    source_reference: Annotated[str, typer.Argument()],
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Test receipt'ini exact current source/HEAD ve yerel ham raporlarla eslestirir."""
    from zekam.application.git_publication_test_source import read_publication_test_source

    try:
        document = _read(plan_file)
        plan = publication_plan_from_body(document.get("plan", document))
        context = _context(home)
        result = read_publication_test_source(context.home, source_reference, plan=plan)
        console.print_json(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise fail_from(ValidationFailed("Publication test source kaniti okunamadi")) from exc
    except ZekamError as exc:
        raise fail_from(exc) from exc


@app.command("import-evidence")
def evidence_command(
    input_file: Annotated[Path, typer.Argument()],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    try:
        document = _read(input_file)
        evidence = publication_evidence_from_body(document["evidence"])
        producer = UUID(document["producer_id"])
        expected = evidence_attestation_body(
            evidence,
            source_job_id=document["source_job_id"],
            source_evidence_digest=document["source_evidence_digest"],
            producer_id=producer,
        )
        _envelope(document, expected)
        if apply:
            _context, _trust, ledger = _components(home)
            ledger.record_evidence(
                evidence,
                source_job_id=document["source_job_id"],
                source_evidence_digest=document["source_evidence_digest"],
                producer_id=producer,
                attestation=document["attestation"],
            )
        console.print_json(
            json.dumps(
                {
                    "evidence_digest": evidence.evidence_digest,
                    "apply": apply,
                    "signature_validation": "validated" if apply else "apply-required",
                    "grants_authority": False,
                }
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise fail_from(ValidationFailed("Publication evidence envelope gecersiz")) from exc
    except ZekamError as exc:
        raise fail_from(exc) from exc


@app.command("import-approval")
def approval_command(
    input_file: Annotated[Path, typer.Argument()],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    try:
        document = _read(input_file)
        plan = publication_plan_from_body(document["plan"])
        authorization = publication_authorization_from_body(
            document["authorization"], realm_id=UUID(document["realm_id"])
        )
        producer = UUID(document["producer_id"])
        _envelope(document, approval_attestation_body(plan, authorization, producer))
        if apply:
            _context, trust, ledger = _components(home)
            if (
                plan.realm_id != trust.realm_id
                or plan.binding.config_revision_id != trust.config_revision_id
            ):
                raise PolicyViolation("Publication approval active realm/config scope drift")
            ledger.record_authorization(
                plan, authorization, producer_id=producer, attestation=document["attestation"]
            )
        console.print_json(
            json.dumps(
                {
                    "authorization_id": str(authorization.id),
                    "plan_digest": plan.plan_digest,
                    "apply": apply,
                    "signature_validation": "validated" if apply else "apply-required",
                    "grants_authority": False,
                }
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise fail_from(ValidationFailed("Publication approval envelope gecersiz")) from exc
    except ZekamError as exc:
        raise fail_from(exc) from exc


@app.command("apply")
def apply_command(
    input_file: Annotated[Path, typer.Argument()],
    authorization_id: Annotated[UUID, typer.Option("--authorization-id")],
    plan_digest: Annotated[str, typer.Option("--plan-digest")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    try:
        document = _read(input_file)
        plan = publication_plan_from_body(document.get("plan", document))
        if plan.plan_digest != plan_digest:
            raise PolicyViolation("Publication exact plan digest drift")
        if not apply:
            console.print_json(
                json.dumps(
                    {"plan_digest": plan.plan_digest, "apply": False, "grants_authority": False}
                )
            )
            return
        context = _context(home)
        replay = replay_canonical_job(
            context.home,
            binding=plan.binding,
            operation="git.push",
            input_digest=digest(
                {"plan_digest": plan.plan_digest, "authorization_id": str(authorization_id)}
            ),
        )
        if replay is not None:
            console.print_json(json.dumps(replay))
            return
        context, trust, ledger = _components(home)
        if (
            plan.realm_id != trust.realm_id
            or plan.binding.config_revision_id != trust.config_revision_id
            or plan.binding.config_digest != trust.config_digest
        ):
            raise PolicyViolation("Publication apply current realm/config drift")
        with SQLiteOperationalStore(
            context.settings.database.sqlite_path(context.home)
        ).unit_of_work() as uow:
            project = uow.resolve_project(plan.binding.project_id)
        root = resolve_project_source(context.home, project.slug)
        result = GitPublicationService(context.home, ledger, BoundGitPushTransport(root)).apply(
            plan, authorization_id=authorization_id
        )
        console.print_json(json.dumps(result))
    except ZekamError as exc:
        raise fail_from(exc) from exc


@app.command("revoke")
def revoke_command(
    input_file: Annotated[Path, typer.Argument()],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    try:
        document = _read(input_file)
        identity, producer = UUID(document["authorization_id"]), UUID(document["producer_id"])
        moment = dt.datetime.fromisoformat(document["revoked_at"])
        _envelope(document, revocation_attestation_body(identity, moment, producer))
        if apply:
            _context, trust, ledger = _components(home)
            if ledger.get(identity).realm_id != trust.realm_id:
                raise PolicyViolation("Publication revocation realm drift")
            ledger.revoke(
                identity,
                revoked_at=moment,
                producer_id=producer,
                attestation=document["attestation"],
            )
        console.print_json(
            json.dumps(
                {"authorization_id": str(identity), "apply": apply, "grants_authority": False}
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise fail_from(ValidationFailed("Publication revocation envelope gecersiz")) from exc
    except ZekamError as exc:
        raise fail_from(exc) from exc


@app.command("status")
def status_command(
    authorization_id: Annotated[UUID, typer.Argument()],
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    try:
        _context, trust, ledger = _components(home)
        authorization = ledger.get(authorization_id)
        if authorization.realm_id != trust.realm_id:
            raise PolicyViolation("Publication status realm drift")
        console.print_json(
            json.dumps(
                {
                    "authorization_id": str(authorization.id),
                    "state": authorization.state.value,
                    "plan_digest": authorization.plan_digest,
                    "effect_digest": authorization.effect_digest,
                    "rejection_reason": authorization.rejection_reason(dt.datetime.now(dt.UTC)),
                    "read_only": True,
                    "grants_authority": False,
                }
            )
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
