"""`zekam ask` ve `zekam research` komutlari.

`ask` salt okunurdur: istegi siniflandirir, exact kimlikleri korur ve belirsizlik
varsa mutation baslatmadan netlestirme sorar. `research` alt komutlari kanonik rol
DAG'ini gosterir ve arastirma sorusunu kanonik store'a yazar.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.intake_service import IntakeOutcome, IntakeService
from zekam.application.realm_context import RealmContext
from zekam.application.research_service import default_dag_nodes
from zekam.domain.errors import ZekamError
from zekam.domain.identifiers import new_uuid7
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.research import (
    ResearchBudget,
    ResearchDag,
    ResearchNode,
    ResearchQuestion,
    SourceKind,
    SourcePolicy,
)
from zekam.infrastructure.postgres.project_repository import ProjectRepository
from zekam.infrastructure.postgres.research_repository import ResearchRepository
from zekam.interfaces.cli.session import (
    EXIT_AMBIGUOUS,
    HOME_HELP,
    REALM_HELP,
    RealmSession,
    fail,
    fail_from,
)

app = typer.Typer(name="research", help="Kanitli arastirma islemleri", no_args_is_help=True)
console = Console()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _intake(realm_context: RealmContext, text: str) -> IntakeOutcome:
    repository = ProjectRepository(realm_context.connection, realm_context.realm_id)
    projects = repository.list_all()
    aliases = {project.slug: repository.aliases_of(project.id) for project in projects}
    return IntakeService().resolve(
        text,
        now=_now(),
        projects=projects,
        aliases=aliases,
        project_required=bool(projects),
    )


def _render_intake(outcome: IntakeOutcome) -> None:
    resolution = outcome.resolution
    table = Table(title="Intake cozumu")
    table.add_column("Alan")
    table.add_column("Deger")
    table.add_row("sinif", str(resolution.request_class))
    table.add_row("proje", resolution.project_ref or "-")
    table.add_row("is", resolution.work_ref or "-")
    table.add_row(
        "exact kimlikler",
        ", ".join(item.value for item in resolution.exact_identifiers) or "-",
    )
    table.add_row("ipuclari", ", ".join(resolution.matched_cues) or "-")
    table.add_row("konu", resolution.subject_used or "-")
    table.add_row("digest", resolution.resolution_digest)
    console.print(table)
    for clarification in outcome.clarifications:
        options = ", ".join(clarification.options)
        suffix = f" ({options})" if options else ""
        console.print(f"[yellow]Netlestirme:[/yellow] {clarification.question}{suffix}")


def ask_command(
    text: Annotated[str, typer.Argument(help="Dogal dil istek metni")],
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Dogal dil istegini cozer. Salt okunur; hicbir kaydi degistirmez."""
    try:
        with RealmSession(home, realm) as realm_context:
            outcome = _intake(realm_context, text)
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if as_json:
        console.print_json(json.dumps(outcome.as_dict(), ensure_ascii=False))
    else:
        _render_intake(outcome)
    if not outcome.may_start_work:
        raise typer.Exit(EXIT_AMBIGUOUS)


@app.command("dag")
def dag_command(
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Kanonik rol DAG'ini ve paralel gruplarini gosterir."""
    dag = ResearchDag(
        question_id="contract",
        nodes=tuple(
            ResearchNode(node_id=node_id, role=role, depends_on=deps)
            for node_id, role, deps in default_dag_nodes()
        ),
    )
    payload = dict(dag.as_dict(), parallel_groups=[list(group) for group in dag.parallel_groups()])
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title="Research DAG")
    table.add_column("Grup")
    table.add_column("Node")
    for index, group in enumerate(dag.parallel_groups(), start=1):
        table.add_row(str(index), ", ".join(group))
    console.print(table)
    console.print(f"gercek subagent sayisi: {dag.subagent_count} (koordinator sayilmaz)")


@app.command("start")
def start_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya kimlik")],
    work: Annotated[str, typer.Argument(help="Is kaydi kimligi")],
    question: Annotated[str, typer.Argument(help="Arastirma sorusu")],
    intent_digest: Annotated[str, typer.Option("--intent-digest", help="Guncel intent digest")],
    source_revision: Annotated[str, typer.Option("--kaynak-revizyon", help="Source revision")],
    host: Annotated[
        list[str] | None, typer.Option("--host", help="Izinli HTTPS host (tekrarlanabilir)")
    ] = None,
    max_tokens: Annotated[int, typer.Option("--token-butcesi")] = 50_000,
    max_cost: Annotated[int, typer.Option("--maliyet-butcesi")] = 100,
    max_seconds: Annotated[int, typer.Option("--sure-butcesi")] = 600,
    apply: Annotated[bool, typer.Option("--uygula", help="Kanonik store'a yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Scope ve butceye bagli arastirma sorusu olusturur."""
    kinds = {SourceKind.FILE, SourceKind.REPOSITORY, SourceKind.IMPORT}
    if host:
        kinds.add(SourceKind.HTTPS)
    try:
        with RealmSession(home, realm) as realm_context:
            repository = ProjectRepository(realm_context.connection, realm_context.realm_id)
            found = repository.find_by_slug(project)
            if found is None:
                raise fail(f"Proje bulunamadi: {project}")
            record = ResearchQuestion(
                question_id=str(new_uuid7()),
                question=question,
                project_ref=found.slug,
                work_ref=work,
                intent_digest=intent_digest,
                source_revision=source_revision,
                policy=SourcePolicy(
                    allowed_kinds=frozenset(kinds),
                    allowed_hosts=frozenset(host or ()),
                    project_scope=found.slug,
                ),
                budget=ResearchBudget(
                    max_tokens=max_tokens,
                    max_cost_units=max_cost,
                    max_seconds=max_seconds,
                ),
                created_at=_now(),
            )
            if not apply:
                console.print(f"hazirlanan soru digest: {record.question_digest}")
                console.print("[yellow]Dry-run. Yazmak icin --uygula verin.[/yellow]")
                return
            stored = ResearchRepository(
                realm_context.connection,
                realm_context.realm_id,
                found.id,
                UUID(work),
            ).store_question(record)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"kaydedildi: {stored}")
