"""`zekam work` komutlari.

Butun yanitlar kanonik Work Graph'tan gelir; semantic index veya vector store
kullanilmaz. Durum degistiren komutlar `--uygula` ister.
"""

from __future__ import annotations

import hashlib
import json
import re
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
_MAX_ACTIVE_SPEC_BYTES = 1_048_576


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


def _read_active_spec(path: Path, expected_digest: str) -> tuple[str, tuple[str, ...], str]:
    """Read the bounded UTF-8 living spec and extract its exact completion checklist."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ZekamError("Aktif gorev spec dosyasi okunamadi") from exc
    if not raw or len(raw) > _MAX_ACTIVE_SPEC_BYTES:
        raise ZekamError("Aktif gorev spec dosyasi bounded boyutta olmali")
    actual_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if expected_digest.strip().lower() != actual_digest:
        raise ZekamError("Aktif gorev spec SHA-256 drift")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ZekamError("Aktif gorev spec UTF-8 olmali") from exc
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ZekamError("Aktif gorev spec H1 basligi ister")
    title = lines[0][2:].strip()
    if "—" in title:
        title = title.split("—", 1)[1].strip()
    section_start = next(
        (index for index, line in enumerate(lines) if line.startswith("## 17.")),
        None,
    )
    if section_start is None:
        raise ZekamError("Aktif gorev spec tamamlanma olcutleri bolumu ister")
    criteria: list[str] = []
    for line in lines[section_start + 1 :]:
        if line.startswith("## "):
            break
        match = re.fullmatch(r"- \[[ xX]\] (.+)", line.strip())
        if match:
            criteria.append(match.group(1).strip())
    if not title or not criteria:
        raise ZekamError("Aktif gorev spec title ve kabul kriterleri ister")
    if len(criteria) != len(set(criteria)):
        raise ZekamError("Aktif gorev spec kabul kriterleri tekil olmali")
    return title, tuple(criteria), actual_digest


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
    verify_all_criteria: Annotated[
        bool,
        typer.Option(
            "--tum-kriterleri-dogrula",
            help="Yalniz verification gecisinde tum kriterleri exact kanitlarla dogrular",
        ),
    ] = False,
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten uygular")] = False,
    run_id: Annotated[
        UUID | None,
        typer.Option("--run-id", help="Hydration admission icin exact aktif run UUID"),
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Hydration admission icin exact client session"),
    ] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Is durumunu degistirir. `completed` icin kanit zorunludur."""
    references: list[EvidenceRef] = []
    del run_id, session_id  # Degerler merkezi mutation-admission registry tarafindan kullanilir.
    for entry in evidence or []:
        kind, separator, value = entry.partition("=")
        if not separator or not value.strip():
            raise fail("Kanit bicimi `kind=reference` olmali")
        references.append(EvidenceRef(kind=kind.strip(), reference=value.strip()))

    if verify_all_criteria and target is not WorkState.VERIFICATION:
        raise fail("--tum-kriterleri-dogrula yalniz verification hedefinde kullanilir")
    if verify_all_criteria and not references:
        raise fail("Kabul kriteri dogrulamasi en az bir exact --kanit ister")

    if not apply:
        console.print(
            f"hedef durum: {target.value}, kanit: {len(references)}, "
            f"tum kriterler: {str(verify_all_criteria).lower()}"
        )
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return
    if target is WorkState.COMPLETED:
        raise fail(
            "Raw work transition completed icin kapali: projection-aware close/release "
            "zinciri kullanilmali",
            64,
        )
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            work_item_id = _work_id(service, project_id, reference)
            with realm_context.connection.transaction():
                if verify_all_criteria:
                    current = service.items.get(work_item_id)
                    if not current.acceptance_criteria:
                        raise ZekamError("Dogrulanacak kabul kriteri bulunamadi")
                    service.update_details(
                        work_item_id,
                        acceptance_criteria=tuple(
                            AcceptanceCriterion(item.text, verified=True)
                            for item in current.acceptance_criteria
                        ),
                        reason="exact acceptance evidence ile kriterler dogrulandi",
                    )
                updated = service.transition(
                    work_item_id,
                    target,
                    evidence=tuple(references),
                )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Yeni durum:[/green] {updated.state.value} (revision {updated.revision})")


@app.command("verify")
def verify_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    reference: Annotated[str, typer.Argument(help="Is kimligi veya dis numara")],
    evidence: Annotated[
        list[str] | None,
        typer.Option("--kanit", help="kind=reference bicimli exact kanit"),
    ] = None,
    apply: Annotated[bool, typer.Option("--uygula", help="Kriterleri atomik dogrular")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Exact kanitlarla tum kriterleri dogrular ve Work'u verification'a alir."""

    references: list[EvidenceRef] = []
    for entry in evidence or []:
        kind, separator, value = entry.partition("=")
        if not separator or not value.strip():
            raise fail("Kanit bicimi `kind=reference` olmali")
        references.append(EvidenceRef(kind=kind.strip(), reference=value.strip()))
    if not references:
        raise fail("Work verification en az bir exact --kanit ister")
    if not apply:
        console.print(f"verification kanit: {len(references)}")
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            work_item_id = _work_id(service, project_id, reference)
            with realm_context.connection.transaction():
                current = service.items.get(work_item_id)
                if current.state is not WorkState.ACTIVE or not current.acceptance_criteria:
                    raise ZekamError("Work verification exact active ve kriterli Work ister")
                service.update_details(
                    work_item_id,
                    acceptance_criteria=tuple(
                        AcceptanceCriterion(item.text, verified=True)
                        for item in current.acceptance_criteria
                    ),
                    reason="exact acceptance evidence ile kriterler dogrulandi",
                )
                updated = service.transition(
                    work_item_id,
                    WorkState.VERIFICATION,
                    evidence=tuple(references),
                )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Verification:[/green] revision {updated.revision}")


@app.command("activate")
def activate_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    reference: Annotated[str, typer.Argument(help="Is kimligi veya dis numara")],
    evidence: Annotated[
        list[str] | None,
        typer.Option("--kanit", help="kind=reference bicimli exact aktivasyon kaniti"),
    ] = None,
    apply: Annotated[bool, typer.Option("--uygula", help="Work'u atomik active yapar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Yalniz proposed Work'u exact kanitlarla control-plane'de active yapar."""

    references: list[EvidenceRef] = []
    for entry in evidence or []:
        kind, separator, value = entry.partition("=")
        if not separator or not kind.strip() or not value.strip():
            raise fail("Kanit bicimi `kind=reference` olmali")
        references.append(EvidenceRef(kind=kind.strip(), reference=value.strip()))
    if not references:
        raise fail("Work activation en az bir exact --kanit ister")
    if not apply:
        console.print(f"hedef durum: active, aktivasyon kanit: {len(references)}")
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            work_item_id = _work_id(service, project_id, reference)
            with realm_context.connection.transaction():
                current = service.items.get(work_item_id)
                if current.project_id != project_id or current.state is not WorkState.PROPOSED:
                    raise ZekamError("Work activation exact proposed Work ister")
                service.transition(
                    work_item_id,
                    WorkState.READY,
                    evidence=tuple(references),
                    reason="exact input ve baseline kaniti ile control-plane activation",
                )
                updated = service.transition(
                    work_item_id,
                    WorkState.ACTIVE,
                    reason="control-plane activation actionable gate",
                )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Active:[/green] revision {updated.revision}")


@app.command("activation-rollback")
def activation_rollback_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    reference: Annotated[str, typer.Argument(help="Is kimligi veya dis numara")],
    expected_revision: Annotated[
        int, typer.Option("--beklenen-revision", help="Exact active Work revision")
    ],
    evidence: Annotated[str, typer.Option("--kanit", help="Exact rollback kanit referansi")],
    apply: Annotated[
        bool, typer.Option("--uygula", help="Unbootstrapped activation'i atomik geri alir")
    ] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Yalniz Intent/Plan uretmemis exact active Work aktivasyonunu geri alir."""

    if expected_revision < 1:
        raise fail("Activation rollback pozitif --beklenen-revision ister")
    if not evidence.strip():
        raise fail("Activation rollback exact --kanit ister")
    if not apply:
        console.print(f"hedef durum: proposed, beklenen revision: {expected_revision}, kanit: 1")
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            work_item_id = _work_id(service, project_id, reference)
            with realm_context.connection.transaction():
                current = service.items.get(work_item_id)
                snapshot = service.snapshot(work_item_id)
                if (
                    current.project_id != project_id
                    or current.state is not WorkState.ACTIVE
                    or current.revision != expected_revision
                    or snapshot.intent is not None
                    or snapshot.plan is not None
                ):
                    raise ZekamError("Activation rollback exact unbootstrapped active Work ister")
                service.transition(
                    work_item_id,
                    WorkState.READY,
                    evidence=(EvidenceRef(kind="activation-rollback", reference=evidence.strip()),),
                    reason="unbootstrapped control-plane activation rollback",
                )
                updated = service.transition(
                    work_item_id,
                    WorkState.PROPOSED,
                    reason="client runtime bootstrap icin proposed state restore",
                )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Proposed:[/green] revision {updated.revision}")


@app.command("sync-spec")
def sync_spec_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    reference: Annotated[str, typer.Argument(help="Is kimligi veya dis numara")],
    input_file: Annotated[Path, typer.Option("--input-file", help="Exact UTF-8 living spec")],
    input_digest: Annotated[
        str, typer.Option("--input-digest", help="Exact sha256:<hex> spec digest")
    ],
    expected_revision: Annotated[
        int, typer.Option("--beklenen-revision", help="Exact current Work revision")
    ],
    run_id: Annotated[
        UUID | None, typer.Option("--run-id", help="Mutation admission exact active run")
    ] = None,
    session_id: Annotated[
        str | None, typer.Option("--session-id", help="Hydration admission exact session")
    ] = None,
    apply: Annotated[
        bool, typer.Option("--uygula", help="Spec revision'ini atomik kaydeder")
    ] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Bounded UTF-8 living spec'i exact digest ile kanonik Work revision'ina alir."""

    del run_id, session_id  # Merkezi mutation-admission exact kimligi dogrular.
    if expected_revision < 1:
        raise fail("Spec sync pozitif --beklenen-revision ister")
    try:
        title, criteria, actual_digest = _read_active_spec(input_file, input_digest)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if not apply:
        console.print(
            f"spec digest: {actual_digest}, kriter: {len(criteria)}, "
            f"beklenen revision: {expected_revision}"
        )
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            work_item_id = _work_id(service, project_id, reference)
            with realm_context.connection.transaction():
                current = service.items.get(work_item_id)
                if (
                    current.project_id != project_id
                    or current.state is not WorkState.ACTIVE
                    or current.revision != expected_revision
                ):
                    raise ZekamError("Spec sync exact active Work revision ister")
                updated = service.update_details(
                    work_item_id,
                    title=title,
                    acceptance_criteria=tuple(
                        AcceptanceCriterion(text=value, verified=False) for value in criteria
                    ),
                    reason=f"living spec exact UTF-8 sync {actual_digest}",
                )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(
        f"[green]Spec synchronized:[/green] revision {updated.revision}, kriter {len(criteria)}"
    )


@app.command("reopen")
def reopen_command(
    project: Annotated[str, typer.Argument(help="Proje slug, alias veya kimlik")],
    reference: Annotated[str, typer.Argument(help="Is kimligi veya dis numara")],
    evidence: Annotated[str, typer.Option("--kanit", help="Exact reopen kanit referansi")],
    apply: Annotated[bool, typer.Option("--uygula", help="Work'u active duruma geri alir")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Stale close sonrasi verification Work'u kanitla yeniden active yapar."""

    if not evidence.strip():
        raise fail("Work reopen exact --kanit ister")
    reference_evidence = EvidenceRef(kind="reopen", reference=evidence.strip())
    if not apply:
        console.print("hedef durum: active, kanit: 1")
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            project_id = _project_id(realm_context, project)
            updated = service.transition(
                _work_id(service, project_id, reference),
                WorkState.ACTIVE,
                evidence=(reference_evidence,),
                reason="stale projection close sonrasi exact reproject",
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Yeniden active:[/green] revision {updated.revision}")


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
