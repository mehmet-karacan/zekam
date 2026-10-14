"""`zekam idea` read-only proposal, review and Work bridge plan commands."""

from __future__ import annotations

import json
import os
from typing import Annotated, cast
from uuid import uuid4

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.idea_proposals import (
    CostEstimate,
    CostStatus,
    IdeaEvidence,
    IdeaProposal,
    build_idea_proposal,
    build_work_transition_plan,
    idea_proposal_from_dict,
    review_idea_proposals,
)
from zekam.application.local_runtime_service import (
    LocalEffectDispatcher,
    LocalEffectRequest,
    LocalEffectResult,
    LocalRuntimeService,
)
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ValidationFailed, ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.local_runtime_effects import LocalJournalOutboxPublisher
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, fail_from, sqlite_operational_store

app = typer.Typer(name="idea", help="Salt okunur fikir proposal ve review", no_args_is_help=True)
console = Console()


def _emit(payload: object, output_json: bool) -> None:
    if output_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
    elif isinstance(payload, dict):
        console.print(
            payload.get("proposal_digest")
            or payload.get("review_digest")
            or payload.get("plan_digest")
        )
    else:
        console.print(payload)


def _parse_json(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationFailed("fikir JSON girdisi gecersiz") from exc
    if not isinstance(parsed, dict):
        raise ValidationFailed("fikir JSON nesne olmali")
    return cast(dict[str, object], parsed)


def _run_claimed_work_create(
    *,
    home: str | None,
    realm: str,
    plan: dict[str, object],
    proposal: IdeaProposal,
    project_ref: str,
    approval_scope_digest: str,
) -> dict[str, object]:
    """Create one idea Work through the local claim/receipt runtime boundary."""

    context = build_context(home=home)
    runtime_home = context.home
    operational_path = context.settings.database.sqlite_path(runtime_home)
    runtime_store = SQLiteLocalRuntimeStore(operational_path, existing_only=True)
    operation = "idea.work-create"
    effect = {
        "schema": "zekam-idea-work-create-effect/v1",
        "plan_digest": plan["plan_digest"],
        "proposal_digest": plan["proposal_digest"],
        "project_ref": project_ref,
        "approval_scope_digest": approval_scope_digest,
    }
    normalized_effect = json.loads(canonical_json(effect))
    if not isinstance(normalized_effect, dict):
        raise ValidationFailed("Idea Work effect canonical body invalid")
    completed: dict[str, dict[str, object]] = {}

    def _document_from_work(work: object) -> dict[str, object]:
        body = {
            "schema": "zekam-idea-work-create-receipt/v1",
            "operation": operation,
            "plan_digest": plan["plan_digest"],
            "proposal_digest": plan["proposal_digest"],
            "project_ref": project_ref,
            "approval_scope_digest": approval_scope_digest,
            "work_id": work.id,
            "work_revision": work.revision,
        }
        receipt_digest = digest(body)
        return plan | {
            "state": "work-created",
            "creates_work": True,
            "receipt_digest": receipt_digest,
            "work": {
                "id": work.id,
                "project_id": work.project_id,
                "kind": work.kind,
                "state": work.state,
                "title": work.title,
                "revision": work.revision,
            },
        }

    def _create_work() -> dict[str, object]:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            project_row = uow.resolve_project(project_ref)
            work = uow.create_work(
                project_id=project_row.id,
                kind="idea",
                title=proposal.title,
                state="proposed",
                payload={
                    "summary": proposal.summary,
                    "acceptance_criteria": [
                        "Idea proposal review and explicit follow-up",
                        f"proposal-digest:{proposal.proposal_digest}",
                        f"scope-digest:{plan['scope_digest']}",
                        f"impact:{proposal.impact}",
                        f"risk:{proposal.risk}",
                    ],
                },
            )
            uow.commit()
        return _document_from_work(work)

    def _replay_work() -> dict[str, object]:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            project_row = uow.resolve_project(project_ref)
            candidates = tuple(
                item
                for item in uow.list_work(project_id=project_row.id)
                if f"proposal-digest:{proposal.proposal_digest}" in item.acceptance_criteria
            )
            uow.commit()
        if len(candidates) != 1:
            raise ValidationFailed("Idea Work replay tekil kayit bulamadi")
        return _document_from_work(candidates[0])

    def _evidence(document: dict[str, object]) -> str:
        value = document.get("receipt_digest")
        if not isinstance(value, str):
            raise ValidationFailed("Idea Work terminal receipt digest missing")
        parse_digest(value)
        return value

    def _execute(request: LocalEffectRequest) -> LocalEffectResult:
        if request.operation != operation or request.payload != normalized_effect:
            raise ValidationFailed("Idea Work local effect binding drift")
        document = _create_work()
        completed["document"] = document
        return LocalEffectResult("completed", _evidence(document))

    service = LocalRuntimeService(
        runtime_store,
        effect_dispatcher=LocalEffectDispatcher(((operation, _execute),)),
        outbox_publisher=LocalJournalOutboxPublisher(runtime_home / "runtime" / "local-effects"),
    )
    job, _created = runtime_store.enqueue(
        idempotency_key=f"idea-work-create:{plan['plan_digest']}",
        payload={"operation": operation, "effect": normalized_effect},
    )
    if job.state == "ready":
        service.run_worker_once(
            owner_id=f"idea-work-{os.getpid()}",
            owner_pid=os.getpid(),
            owner_token=str(uuid4()),
            lease_seconds=30,
            job_id=job.id,
            resources=(f"project:{project_ref}",),
        )
    elif job.state != "completed":
        raise ValidationFailed("Idea Work existing runtime job needs recovery")
    document = completed.get("document") or _replay_work()
    terminal_evidence = _evidence(document)
    snapshot = runtime_store.job_snapshot(job.id)
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
        raise ValidationFailed("Idea Work terminal runtime receipt readback failed")
    publisher_token = str(uuid4())
    for _index in range(3):
        if runtime_store.status().pending_outbox == 0:
            break
        service.publish_outbox_once(
            owner_id=f"idea-work-outbox-{os.getpid()}",
            owner_pid=os.getpid(),
            owner_token=publisher_token,
            lease_seconds=30,
        )
    after = runtime_store.status()
    if after.pending_outbox or after.claimed_outbox or after.recovery_outbox:
        raise ValidationFailed("Idea Work outbox terminal readback failed")
    return document | {
        "operational_job_id": job.id,
        "operational_terminal_evidence_digest": terminal_evidence,
    }


@app.command("propose")  # type: ignore[untyped-decorator]
def propose_command(
    title: Annotated[str, typer.Argument(help="Fikir basligi")],
    summary: Annotated[str, typer.Argument(help="Kisa fikir ozeti")],
    impact: Annotated[str, typer.Option("--impact", help="Beklenen etki")],
    risk: Annotated[str, typer.Option("--risk", help="unknown/low/medium/high/critical")],
    project: Annotated[str | None, typer.Option("--project")] = None,
    cost_status: Annotated[str, typer.Option("--cost-status")] = "unknown",
    cost_duration_ms: Annotated[int | None, typer.Option("--cost-duration-ms")] = None,
    cost_tokens: Annotated[int | None, typer.Option("--cost-tokens")] = None,
    cost_micros: Annotated[int | None, typer.Option("--cost-micros")] = None,
    cost_reason: Annotated[str, typer.Option("--cost-reason")] = "not-measured",
    evidence_ref: Annotated[str | None, typer.Option("--evidence-ref")] = None,
    evidence_digest: Annotated[str | None, typer.Option("--evidence-digest")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Provider/model cagirmadan secret-free fikir adayi uretir."""

    try:
        if (evidence_ref is None) != (evidence_digest is None):
            raise ValidationFailed("evidence-ref ve evidence-digest birlikte verilmeli")
        evidence = (
            (IdeaEvidence(kind="reference", ref=evidence_ref, digest=evidence_digest),)
            if evidence_ref is not None and evidence_digest is not None
            else ()
        )
        proposal = build_idea_proposal(
            title=title,
            summary=summary,
            project_ref=project,
            impact=impact,
            cost=CostEstimate(
                status=cast(CostStatus, cost_status),
                duration_ms=cost_duration_ms,
                tokens=cost_tokens,
                cost_micros=cost_micros,
                reason=cost_reason,
            ),
            risk=risk,
            evidence=evidence,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _emit(proposal.as_dict(), output_json)


@app.command("review")  # type: ignore[untyped-decorator]
def review_command(
    proposal_json: Annotated[
        list[str] | None, typer.Option("--proposal", help="Proposal JSON (tekrarlanabilir)")
    ] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Proposal JSON'larini deterministic dedup review'dan gecirir."""

    try:
        proposals = tuple(
            idea_proposal_from_dict(_parse_json(item)) for item in (proposal_json or [])
        )
        _emit(review_idea_proposals(proposals), output_json)
    except ZekamError as exc:
        raise fail_from(exc) from exc


@app.command("work-plan")  # type: ignore[untyped-decorator]
def work_plan_command(
    proposal_json: Annotated[str, typer.Argument(help="Proposal JSON")],
    project: Annotated[str, typer.Option("--project", help="Explicit project scope")],
    approved: Annotated[bool, typer.Option("--approved")] = False,
    approval_scope_digest: Annotated[str | None, typer.Option("--approval-scope-digest")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Work gecisi icin authority-free scope/approval plan uretir."""

    try:
        proposal = idea_proposal_from_dict(_parse_json(proposal_json))
        plan = build_work_transition_plan(
            proposal,
            project_ref=project,
            approved=approved,
            approval_scope_digest=approval_scope_digest,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _emit(plan, output_json)


@app.command("work-create")  # type: ignore[untyped-decorator]
def work_create_command(
    proposal_json: Annotated[str, typer.Argument(help="Proposal JSON")],
    project: Annotated[str, typer.Option("--project", help="Explicit project scope")],
    approval_scope_digest: Annotated[str, typer.Option("--approval-scope-digest")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Explicit approval sonrasi fikir proposal'ini SQLite Work olarak kaydeder."""

    try:
        proposal = idea_proposal_from_dict(_parse_json(proposal_json))
        plan = build_work_transition_plan(
            proposal,
            project_ref=project,
            approved=True,
            approval_scope_digest=approval_scope_digest,
        )
        if not apply:
            _emit(plan, output_json)
            return
        document = _run_claimed_work_create(
            home=home,
            realm=realm,
            plan=plan,
            proposal=proposal,
            project_ref=project,
            approval_scope_digest=approval_scope_digest,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _emit(document, output_json)
