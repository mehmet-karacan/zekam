"""`zekam idea` read-only proposal, review and Work bridge plan commands."""

from __future__ import annotations

import json
from typing import Annotated, cast

import typer
from rich.console import Console

from zekam.application.idea_proposals import (
    CostEstimate,
    CostStatus,
    IdeaEvidence,
    build_idea_proposal,
    build_work_transition_plan,
    idea_proposal_from_dict,
    review_idea_proposals,
)
from zekam.domain.errors import ValidationFailed, ZekamError
from zekam.interfaces.cli.session import fail_from

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
