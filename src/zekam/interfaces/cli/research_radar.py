"""`zekam research radar` campaign plan/run/status/report/candidates surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.research_campaign_runtime import (
    CampaignPlan,
    build_radar_plan,
    run_radar_campaign,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed, ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.radar_campaign_store import RadarCampaignRepository
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, fail_from, sqlite_operational_store

app = typer.Typer(
    name="radar",
    help="Kapsam ve ilerleme gudumlu muhendislik arastirma kampanyasi",
    no_args_is_help=True,
)
console = Console()


def _read_plan_file(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        raise ValidationFailed("Plan dosyasi absolute path olmali")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationFailed(f"Plan dosyasi okunamadi: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationFailed("Plan dosyasi gecerli JSON degil") from exc
    if not isinstance(document, dict):
        raise ValidationFailed("Plan dosyasi JSON object olmali")
    return document


_PLAN_DERIVED_KEYS = frozenset({"plan_digest", "idempotency_key", "dry_run"})


def _load_plan(path: Path, expected_digest: str) -> CampaignPlan:
    document = _read_plan_file(path)
    stable = {k: v for k, v in document.items() if k not in _PLAN_DERIVED_KEYS}
    computed = digest(stable)
    if computed != expected_digest:
        raise PolicyViolation("Plan digest plan-file icerigiyle eslesmiyor")
    return CampaignPlan(document)


@app.command("plan")
def plan_command(
    project: Annotated[str, typer.Option("--project", help="Project slug, alias veya UUID")],
    stage: Annotated[str, typer.Option("--stage", help="discover veya analyse")],
    inventory_digest: Annotated[
        str | None,
        typer.Option("--inventory-digest", help="Analyse icin kayitli inventory digest"),
    ] = None,
    owners: Annotated[
        str | None,
        typer.Option("--owners", help="Virgulle ayrilmis GitHub owner listesi"),
    ] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Yerel scope/policy ile salt-okunur radar plani uretir; network/model cagrisi yok."""

    try:
        context = build_context(home=home)
        store = sqlite_operational_store(home, realm)
        assert store is not None
        owner_tuple: tuple[str, ...] | None = None
        if owners is not None:
            owner_tuple = tuple(part.strip() for part in owners.split(",") if part.strip())
        plan = build_radar_plan(
            store,
            context.home,
            project_ref=project,
            stage=stage,
            inventory_digest=inventory_digest,
            owners=owner_tuple,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(plan.body, ensure_ascii=False))
    else:
        console.print(f"plan_digest: {plan.plan_digest}")
        console.print(f"stage: {plan.stage}")
        console.print(f"project: {plan.project_slug}")
        console.print("Dry-run; calistirmak icin exact digest ve yetkileri verin.")


@app.command("run")
def run_command(
    plan_file: Annotated[Path, typer.Option("--plan-file", help="Plan JSON dosyasi yolu")],
    plan_digest: Annotated[str, typer.Option("--plan-digest", help="Exact plan digest")],
    apply: Annotated[bool, typer.Option("--uygula", help="Plani claim ederek calistir")] = False,
    authorize_public_source_read: Annotated[
        bool,
        typer.Option("--authorize-public-source-read", help="Public GitHub kaynak okuma yetkisi"),
    ] = False,
    authorize_agent_run: Annotated[
        bool,
        typer.Option("--authorize-agent-run", help="Bounded agent run yetkisi"),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Plan bytes/digest dogrulayarak stage'i calistirir."""

    try:
        context = build_context(home=home)
        store = sqlite_operational_store(home, realm)
        assert store is not None
        plan = _load_plan(plan_file, plan_digest)
        if not apply:
            document = {
                "plan_digest": plan.plan_digest,
                "stage": plan.stage,
                "dry_run": True,
                "grants_authority": False,
            }
        else:
            document = run_radar_campaign(
                store,
                context.home,
                plan,
                authorized_plan_digest=plan_digest,
                authorize_public_source_read=authorize_public_source_read,
                authorize_agent_run=authorize_agent_run,
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        state = document.get("state", "dry-run")
        cid = document.get("campaign_id", "")
        console.print(f"[green]{state}[/green] {cid}")


@app.command("status")
def status_command(
    campaign_id: Annotated[str, typer.Argument(help="Campaign UUID")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kampanya stage, coverage, usage ve terminal durumunu salt okunur gosterir."""

    try:
        context = build_context(home=home)
        repo = RadarCampaignRepository(context.home / "state" / "radar-campaigns.db")
        document = repo.campaign_status_document(campaign_id)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(
            f"{document['state']}\t{document['campaign_id']}\t{document['next_safe_action']}"
        )


@app.command("report")
def report_command(
    campaign_id: Annotated[str, typer.Argument(help="Campaign UUID")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kanonik kayittan okunabilir/makine-okur kampanya raporu gosterir."""

    try:
        context = build_context(home=home)
        repo = RadarCampaignRepository(context.home / "state" / "radar-campaigns.db")
        document = repo.campaign_report_document(campaign_id)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"{document['status']}\t{document['campaign_id']}")


@app.command("candidates")
def candidates_command(
    campaign_id: Annotated[str, typer.Argument(help="Campaign UUID")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Secilen/reddedilen/kismi adaylari gosterir; aktif task veya approval uretmez."""

    try:
        context = build_context(home=home)
        repo = RadarCampaignRepository(context.home / "state" / "radar-campaigns.db")
        document = repo.candidates_document(campaign_id)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(
            f"patterns: {len(document['pattern_cards'])}, "
            f"gaps: {len(document['gap_cards'])}, "
            f"decisions: {len(document['decisions'])}"
        )
