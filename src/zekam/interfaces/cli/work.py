"""`zekam work` komutlari.

Butun yanitlar kanonik Work Graph'tan gelir; semantic index veya vector store
kullanilmaz. Durum degistiren komutlar `--uygula` ister.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.home import resolve_home
from zekam.application.opencode_lifecycle import resume_projection
from zekam.application.realm_context import RealmContext
from zekam.application.resume_coordinator import ResumeCoordinator
from zekam.application.work_graph import WorkGraphService
from zekam.domain.errors import ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.work import AcceptanceCriterion, EvidenceRef, RelationKind, WorkState, WorkType
from zekam.infrastructure.postgres.active_work_projection_repository import (
    ActiveWorkProjectionRepository,
)
from zekam.infrastructure.postgres.project_repository import ProjectResolver
from zekam.infrastructure.postgres.resume_repository import ResumeRepository
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

app = typer.Typer(name="work", help="Work Graph islemleri", no_args_is_help=True)
console = Console()


class ProjectionFormat(StrEnum):
    MARKDOWN = "markdown"
    YAML = "yaml"


def _service(realm_context: RealmContext) -> WorkGraphService:
    return WorkGraphService(realm_context.connection, realm_context.realm)


def _project_id(realm_context: RealmContext, query: str) -> UUID:
    resolution = ProjectResolver(realm_context.connection, realm_context.realm_id).resolve(query)
    if resolution.resolved is None:
        code = EXIT_AMBIGUOUS if resolution.requires_user_choice else EXIT_NOT_FOUND
        raise fail(f"Proje cozulemedi: {query}", code)
    return resolution.resolved.project_id


def _work_id(service: WorkGraphService, project_id: UUID, reference: str) -> UUID:
    """Kimlik veya exact dis numara ile is kaydini bulur."""
    try:
        return UUID(reference)
    except ValueError:
        return service.find_exact(project_id=project_id, external_number=reference).id


def _render(items: tuple[object, ...]) -> None:
    console.print_json(json.dumps(items, ensure_ascii=False, default=str))


@app.command("create")
def create_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    title: Annotated[str, typer.Argument(help="Is basligi")],
    work_type: Annotated[WorkType, typer.Option("--tur", help="Is turu")] = WorkType.TASK,
    summary: Annotated[str, typer.Option("--ozet", help="Kisa aciklama")] = "",
    number: Annotated[
        str | None, typer.Option("--numara", help="Dis talep/defect numarasi")
    ] = None,
    criterion: Annotated[
        list[str] | None, typer.Option("--kriter", help="Kabul kriteri (tekrarlanabilir)")
    ] = None,
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten olusturur")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Yeni is kaydi olusturur."""
    if not apply:
        console.print(f"olusturulacak: {work_type.value} / {title}")
        console.print("[yellow]Dry-run. Olusturmak icin --uygula verin.[/yellow]")
        return
    try:
        sqlite = sqlite_repository(home, realm)
        if sqlite is not None:
            if summary or number is not None or criterion:
                raise ZekamError(
                    "SQLite minimum profili ozet, dis numara ve kabul kriterini desteklemez"
                )
            project_row = sqlite.get_project(project)
            sqlite_item = sqlite.create_work(
                project_id=project_row.id,
                kind=work_type.value,
                title=title,
            )
            console.print(f"[green]Olusturuldu:[/green] {sqlite_item.id} ({sqlite_item.state})")
            return
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            item = service.create_item(
                project_id=_project_id(realm_context, project),
                type=work_type,
                title=title,
                summary=summary,
                external_number=number,
                acceptance_criteria=tuple(
                    AcceptanceCriterion(text=text) for text in (criterion or [])
                ),
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Olusturuldu:[/green] {item.id} ({item.state.value})")


@app.command("list")
def list_command(
    project: Annotated[str | None, typer.Option("--proje", help="Proje filtresi")] = None,
    state: Annotated[list[WorkState] | None, typer.Option("--durum", help="Durum filtresi")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Isleri listeler."""
    try:
        sqlite = sqlite_repository(home, realm)
        if sqlite is not None:
            project_id = sqlite.get_project(project).id if project is not None else None
            state_values = {item.value for item in state or ()}
            rows: list[dict[str, object]] = [
                {
                    "id": item.id,
                    "external_number": None,
                    "type": item.kind,
                    "state": item.state,
                    "title": item.title,
                    "project_id": item.project_id,
                    "revision": item.revision,
                    "evidence_digest": item.evidence_digest,
                }
                for item in sqlite.list_work(project_id=project_id)
                if not state_values or item.state in state_values
            ]
        else:
            with RealmSession(home, realm) as realm_context:
                service = _service(realm_context)
                if project is None:
                    items = service.items.list_open()
                    if state:
                        items = tuple(item for item in items if item.state in set(state))
                else:
                    items = service.items.list_for_project(
                        _project_id(realm_context, project), states=state
                    )
                rows = [dict(item.as_dict()) for item in items]
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if output_json:
        _render(tuple(rows))
        return
    table = Table(title="Isler")
    table.add_column("Numara")
    table.add_column("Tur")
    table.add_column("Durum")
    table.add_column("Baslik")
    for row in rows:
        table.add_row(
            str(row["external_number"] or "-"),
            str(row["type"]),
            str(row["state"]),
            str(row["title"]),
        )
    console.print(table)


@app.command("show")
def show_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    reference: Annotated[str, typer.Argument(help="Is kimligi veya dis numara")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Isin tam gorunumunu yazar."""
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            snapshot = service.snapshot(_work_id(service, project_id, reference))
            document = snapshot.as_dict()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("history")
def history_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    reference: Annotated[str, typer.Argument(help="Is kimligi veya dis numara")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Degisim tarihcesini ve zincir butunlugunu yazar."""
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            work_item_id = _work_id(service, project_id, reference)
            document = {
                "work_item_id": str(work_item_id),
                "chain_valid": service.verify_history(work_item_id),
                "revisions": list(service.history(work_item_id)),
            }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("next")
def next_command(
    project: Annotated[str | None, typer.Option("--proje", help="Proje filtresi")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Bagimliligi karsilanmis, bloklanmamis bir sonraki isi yazar."""
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = None if project is None else _project_id(realm_context, project)
            candidate = service.next_actionable(project_id)
            document = None if candidate is None else candidate.as_dict()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps({"next": document}, ensure_ascii=False, default=str))


@app.command("today")
def today_command(
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Bugun uzerinde calisilabilecek isleri yazar."""
    try:
        with RealmSession(home, realm) as realm_context:
            items = _service(realm_context).today()
            rows = [item.as_dict() for item in items]
    except ZekamError as exc:
        raise fail_from(exc) from exc
    table = Table(title="Bugunku isler")
    table.add_column("Durum")
    table.add_column("Tur")
    table.add_column("Baslik")
    for row in rows:
        table.add_row(row["state"], row["type"], row["title"])
    console.print(table)


@app.command("resume")
def resume_command(
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """`nerede kaldik` sorusunu kanonik kayittan yanitlar."""
    document: dict[str, Any]
    try:
        sqlite = sqlite_repository(home, realm)
        if sqlite is not None:
            items = sqlite.list_work()
            open_items = [
                {
                    "id": item.id,
                    "project_id": item.project_id,
                    "type": item.kind,
                    "state": item.state,
                    "title": item.title,
                    "revision": item.revision,
                }
                for item in items
                if item.state not in {"completed", "cancelled"}
            ]
            document = {
                "source": "sqlite-work-graph",
                "open_items": open_items,
                "blocked": [item for item in open_items if item["state"] == "blocked"],
                "recent_activity": [],
                "next_safe_action": ("ready work item sec" if open_items else "acik is bulunmuyor"),
            }
        else:
            with RealmSession(home, realm) as realm_context:
                document = _service(realm_context).where_did_we_stop()
        document["client_continuity"] = resume_projection(resolve_home(home))
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
        return
    table = Table(title="Nerede kaldik")
    table.add_column("Alan")
    table.add_column("Deger")
    table.add_row("kaynak", document["source"])
    table.add_row("acik is", str(len(document["open_items"])))
    table.add_row("bloklu is", str(len(document["blocked"])))
    table.add_row("son olay", str(len(document["recent_activity"])))
    table.add_row("sonraki guvenli aksiyon", document["next_safe_action"])
    console.print(table)


@app.command("resume-plan")
def resume_plan_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    reference: Annotated[str, typer.Argument(help="Is kimligi veya dis numara")],
    client: Annotated[str, typer.Option("--client", help="Hedef istemci kimligi")],
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Mutation yapmadan exact checkpoint tabanli resume plani uretir."""
    try:
        if sqlite_repository(home, realm) is not None:
            raise ZekamError("Resume plan checkpoint v2 PostgreSQL kaniti ister")
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            work_item_id = _work_id(service, project_id, reference)
            plan = ResumeCoordinator(
                ResumeRepository(realm_context.connection, realm_context.realm_id)
            ).prepare(work_item_id, client_id=client)
            document = plan.as_dict()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
        return
    table = Table(title="Resume plani (salt okunur)")
    table.add_column("Alan")
    table.add_column("Deger")
    table.add_row("durum", plan.disposition.value)
    table.add_row("checkpoint", str(plan.checkpoint_id))
    table.add_row("sonraki step", plan.next_step_id or "-")
    table.add_row("engel", ", ".join(plan.blockers) or "-")
    table.add_row("plan digest", plan.plan_digest)
    table.add_row("authority", "false")
    console.print(table)


@app.command("active-projection")
def active_projection_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    reference: Annotated[str, typer.Argument(help="Is kimligi veya dis numara")],
    output_format: Annotated[
        ProjectionFormat, typer.Option("--format", help="Deterministik cikti bicimi")
    ] = ProjectionFormat.YAML,
    check_root: Annotated[
        bool, typer.Option("--check-root", help="Kok projeksiyon dosyalariyla exact parity")
    ] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Render the root active-work projection from canonical PostgreSQL state."""
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            work_item_id = _work_id(service, project_id, reference)
            root = Path(__file__).resolve().parents[4]
            projection = ActiveWorkProjectionRepository(
                realm_context.connection, realm_context.realm_id, root
            ).load(project_id=project_id, work_item_id=work_item_id)
            markdown = projection.render_markdown()
            rendered_yaml = projection.render_yaml()
            if check_root:
                markdown_current = (root / "AKTIF_GOREV.md").read_text(encoding="utf-8")
                yaml_current = (root / "AKTIF_GOREV.yaml").read_text(encoding="utf-8")
                if markdown_current != markdown or yaml_current != rendered_yaml:
                    raise ZekamError("Root active-work projection parity mismatch")
                console.print_json(
                    json.dumps(
                        {
                            "schema": "zekam-active-work-projection-check/v1",
                            "current": True,
                            "work_item_id": str(work_item_id),
                            "projection_digest": projection.projection_digest,
                            "grants_authority": False,
                        }
                    )
                )
                return
    except (OSError, ZekamError) as exc:
        raise fail_from(exc if isinstance(exc, ZekamError) else ZekamError(str(exc))) from exc
    typer.echo(markdown if output_format is ProjectionFormat.MARKDOWN else rendered_yaml, nl=False)


@app.command("transition")
def transition_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    reference: Annotated[str, typer.Argument(help="Is kimligi veya dis numara")],
    target: Annotated[WorkState, typer.Argument(help="Hedef durum")],
    evidence: Annotated[
        list[str] | None,
        typer.Option("--kanit", help="kind=reference bicimli kanit (tekrarlanabilir)"),
    ] = None,
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten uygular")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Is durumunu degistirir. `completed` icin kanit zorunludur."""
    references: list[EvidenceRef] = []
    for entry in evidence or []:
        kind, separator, value = entry.partition("=")
        if not separator or not value.strip():
            raise fail("Kanit bicimi `kind=reference` olmali")
        references.append(EvidenceRef(kind=kind.strip(), reference=value.strip()))

    if not apply:
        console.print(f"hedef durum: {target.value}, kanit: {len(references)}")
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            updated = service.transition(
                _work_id(service, project_id, reference),
                target,
                evidence=tuple(references),
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Yeni durum:[/green] {updated.state.value} (revision {updated.revision})")


@app.command("relate")
def relate_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    source: Annotated[str, typer.Argument(help="Kaynak is kimligi veya numarasi")],
    kind: Annotated[RelationKind, typer.Argument(help="Iliski turu")],
    target: Annotated[str, typer.Argument(help="Hedef is kimligi veya numarasi")],
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten uygular")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Iki isi iliskilendirir. Dongu ve cross-project reddedilir."""
    if not apply:
        console.print(f"{source} -{kind.value}-> {target}")
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            relation = service.relate(
                source_id=_work_id(service, project_id, source),
                target_id=_work_id(service, project_id, target),
                kind=kind,
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Iliski eklendi:[/green] {relation.id}")
