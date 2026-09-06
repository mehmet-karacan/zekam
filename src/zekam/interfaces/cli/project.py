"""Local operational project registry commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console

from zekam.application.home import resolve_home
from zekam.application.odi11g_export import (
    Odi11gExportPlan,
    bind_odi11g_export,
    build_odi11g_export_plan,
)
from zekam.application.odi11g_smart_export import (
    build_sanitized_odi_plan,
    build_smart_import_plan,
    import_smart_export,
)
from zekam.application.opencode_embedding import default_opencode_config_file
from zekam.application.project_rag_runtime import (
    bind_project_source,
    index_registered_project,
    project_rag_status,
    query_registered_project,
    read_project_citation,
    resolve_project_source,
)
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.identifiers import normalize_slug, validate_slug
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.interfaces.cli.session import (
    HOME_HELP,
    REALM_HELP,
    fail,
    fail_from,
    sqlite_operational_store,
)

app = typer.Typer(name="project", help="Yerel proje kayitlari", no_args_is_help=True)
console = Console()
_DEFAULT_OPENCODE_CONFIG_FILE = default_opencode_config_file()


def _resolve_project_document(
    reference: str,
    *,
    home: str | None,
    realm: str,
) -> dict[str, object]:
    store = sqlite_operational_store(home, realm)
    assert store is not None
    with store.unit_of_work() as uow:
        project = uow.resolve_project(reference)
        aliases = list(uow.list_project_aliases(project.id))
        uow.commit()
    return {
        "id": project.id,
        "slug": project.slug,
        "display_name": project.display_name,
        "status": project.status,
        "revision": project.revision,
        "aliases": aliases,
    }


def _canonical_slug(reference: str, *, home: str | None) -> str:
    return str(_resolve_project_document(reference, home=home, realm=DEFAULT_REALM_SLUG)["slug"])


@app.command("add")
def add_command(
    source: Annotated[Path, typer.Argument(help="Kaynak proje kok dizini")],
    name: Annotated[str | None, typer.Option("--name")] = None,
    slug: Annotated[str | None, typer.Option("--slug")] = None,
    alias: Annotated[list[str] | None, typer.Option("--alias")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    resolved = source.expanduser()
    if not resolved.is_dir():
        raise fail("Kaynak koku bir dizin olmali")
    selected_slug = validate_slug(slug) if slug else normalize_slug(resolved.name)
    if not apply:
        console.print_json(
            json.dumps({"slug": selected_slug, "source_kind": "read-only", "apply": False})
        )
        return
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            project = uow.create_project(slug=selected_slug, display_name=name or resolved.name)
            for item in alias or ():
                uow.add_project_alias(project_id=project.id, alias=item)
            uow.bind_source(
                project_id=project.id,
                portable_ref=f"source:{selected_slug}",
                source_kind="git" if (resolved / ".git").is_dir() else "directory",
            )
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Kaydedildi:[/green] {project.slug} ({project.id})")


@app.command("list")
def list_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    include_archived: Annotated[bool, typer.Option("--include-archived")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            rows = [
                {
                    "id": item.id,
                    "slug": item.slug,
                    "display_name": item.display_name,
                    "status": item.status,
                    "revision": item.revision,
                    "aliases": list(uow.list_project_aliases(item.id)),
                }
                for item in uow.list_projects(include_archived=include_archived)
            ]
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(rows, ensure_ascii=False))
    else:
        for row in rows:
            console.print(f"{row['slug']}\t{row['display_name']}\t{row['status']}")


@app.command("alias-add")
def alias_add_command(
    project: Annotated[str, typer.Argument(help="Proje id, slug veya mevcut alias")],
    alias: Annotated[str, typer.Argument(help="Eklenecek unique proje aliasi")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Tekil proje aliasini operational registry'ye ekler."""

    if not apply:
        console.print_json(
            json.dumps({"project": project, "alias": alias, "apply": False}, ensure_ascii=False)
        )
        return
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            resolved = uow.resolve_project(project)
            uow.add_project_alias(project_id=resolved.id, alias=alias)
            aliases = list(uow.list_project_aliases(resolved.id))
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(
        json.dumps(
            {"project": resolved.slug, "alias": alias, "aliases": aliases, "apply": True},
            ensure_ascii=False,
        )
    )


@app.command("alias-remove")
def alias_remove_command(
    project: Annotated[str, typer.Argument(help="Proje id, slug veya mevcut alias")],
    alias: Annotated[str, typer.Argument(help="Kaldirilacak proje aliasi")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Tekil proje aliasini operational registry'den kaldirir."""

    if not apply:
        console.print_json(
            json.dumps({"project": project, "alias": alias, "apply": False}, ensure_ascii=False)
        )
        return
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            resolved = uow.resolve_project(project)
            uow.remove_project_alias(project_id=resolved.id, alias=alias)
            aliases = list(uow.list_project_aliases(resolved.id))
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(
        json.dumps(
            {"project": resolved.slug, "alias": alias, "aliases": aliases, "apply": True},
            ensure_ascii=False,
        )
    )


@app.command("resolve")
def resolve_command(
    project: Annotated[str, typer.Argument(help="Proje id, slug veya alias")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Proje referansini kanonik operasyonel kayda cozer."""

    try:
        result = _resolve_project_document(project, home=home, realm=realm)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    document = {"schema": "zekam-project-resolution/v1", "reference": project} | result
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"{document['slug']}\t{document['display_name']}\t{document['status']}")


@app.command("show")
def show_command(
    project: Annotated[str, typer.Argument(help="Proje id, slug veya alias")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kanonik proje, yerel kaynak ve RAG durumunu birlikte gosterir."""

    try:
        resolved = _resolve_project_document(project, home=home, realm=realm)
        resolved_home = resolve_home(home)
        slug = str(resolved["slug"])
        source_root = resolve_project_source(resolved_home, slug)
        rag = project_rag_status(resolved_home, slug)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    document = {
        "schema": "zekam-project-detail/v1",
        "reference": project,
        **resolved,
        "source_root": str(source_root),
        "rag": rag,
    }
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(
            f"{document['slug']}\t{document['status']}\t"
            f"rag={rag['state']}\t{document['source_root']}"
        )


@app.command("bind")
def bind_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    source: Annotated[Path, typer.Argument(help="Bu makinedeki exact kaynak koku")],
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Projeyi bu makinedeki salt-okunur kaynak kokune baglar."""

    if not apply:
        console.print_json(
            json.dumps(
                {
                    "project": project,
                    "source": str(source),
                    "source_kind": "read-only",
                    "apply": False,
                }
            )
        )
        return
    try:
        result = bind_project_source(
            resolve_home(home), _canonical_slug(project, home=home), source
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(result, ensure_ascii=False))


def _odi_plan_document(
    project: str, source: Path, *, home: str | None, realm: str
) -> Odi11gExportPlan:
    resolved = _resolve_project_document(project, home=home, realm=realm)
    return build_odi11g_export_plan(
        home=resolve_home(home),
        project_id=str(resolved["id"]),
        project_slug=str(resolved["slug"]),
        export_root=source,
    )


@app.command("odi-preflight")
def odi_preflight_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    source: Annotated[Path, typer.Argument(help="ODI 11g export bundle koku")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """ODI XML bundle'ini provider/persist islemi olmadan fail-closed tarar."""

    try:
        document = _odi_plan_document(project, source, home=home, realm=realm).body
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(
            f"{document['project_slug']}\taccepted={document['accepted']}\t"
            f"files={document['file_count']}\tissues={len(document['issues'])}"
        )


@app.command("odi-bind")
def odi_bind_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    source: Annotated[Path, typer.Argument(help="ODI 11g export bundle koku")],
    expected_plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Dogrulanmis exact ODI bundle'ini local-only proje baglantisi yapar."""

    try:
        plan = _odi_plan_document(project, source, home=home, realm=realm)
        if not apply:
            document = plan.body
        else:
            if expected_plan_digest is None:
                raise PolicyViolation("ODI binding --plan-digest ister")
            document = bind_odi11g_export(
                home=resolve_home(home),
                plan=plan,
                expected_plan_digest=expected_plan_digest,
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print_json(json.dumps(document, ensure_ascii=False))


@app.command("odi-smart-import")
def odi_smart_import_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    source: Annotated[Path, typer.Argument(help="Exact SmartExport.xml")],
    library_root: Annotated[Path, typer.Option("--library-root")] = Path("C:/innova/odi"),
    library_name: Annotated[str, typer.Option("--library-name")] = "gpu",
    expected_plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """SmartExport.xml'i content-addressed local library'ye claim/receipt ile alir."""

    try:
        resolved = _resolve_project_document(project, home=home, realm=realm)
        plan = build_smart_import_plan(
            project_id=str(resolved["id"]),
            project_slug=str(resolved["slug"]),
            source=source,
            library_root=library_root,
            library_name=library_name,
        )
        if not apply:
            document = plan.body
        else:
            if expected_plan_digest is None:
                raise PolicyViolation("ODI Smart import --plan-digest ister")
            document = import_smart_export(
                home=resolve_home(home), plan=plan, expected_plan_digest=expected_plan_digest
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False))


@app.command("odi-smart-status")
def odi_smart_status_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    source: Annotated[Path, typer.Argument(help="Exact imported SmartExport.xml")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Provider cagrisi olmadan sanitizer/chunk/lineage ozetini verir."""

    try:
        resolved = _resolve_project_document(project, home=home, realm=realm)
        plan = build_sanitized_odi_plan(
            project_id=UUID(str(resolved["id"])),
            project_slug=str(resolved["slug"]),
            source=source,
        )
        document = json.loads(plan.manifest)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(
            f"{resolved['slug']}\tchunks={document['chunk_count']}\tedges={document['lineage_edge_count']}"
        )


@app.command("source-root")
def source_root_command(
    project: Annotated[str, typer.Argument(help="Proje slug")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Bu makinedeki dogrulanmis, local-only kaynak kokunu cozer."""

    try:
        slug = _canonical_slug(project, home=home)
        root = resolve_project_source(resolve_home(home), slug)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps({"project": slug, "source_root": str(root)}))
    else:
        console.print(str(root))


@app.command("status")
def status_command(
    project: Annotated[str, typer.Argument(help="Proje slug")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Aktif RAG jenerasyonunu DB/provider cagrisi yapmadan raporlar."""

    try:
        result = project_rag_status(resolve_home(home), _canonical_slug(project, home=home))
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
    else:
        console.print(
            f"{result['project_slug']}\t{result['state']}\tchunks={result.get('chunk_count', 0)}"
        )


@app.command("citation")
def citation_command(
    project: Annotated[str, typer.Argument(help="Proje slug")],
    chunk_id: Annotated[str, typer.Argument(help="Retrieval citation chunk id")],
    generation_digest: Annotated[str | None, typer.Option("--generation-digest")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Citation govdesini pinned local indeksten digest dogrulamali acar."""

    try:
        result = read_project_citation(
            resolve_home(home),
            _canonical_slug(project, home=home),
            chunk_id,
            generation_digest=generation_digest,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
    else:
        console.print(str(result["body"]))


@app.command("query")
def query_command(
    project: Annotated[str, typer.Argument(help="Proje slug")],
    question: Annotated[str, typer.Argument(help="Exact kullanici sorusu")],
    authorize_remote_query: Annotated[
        bool,
        typer.Option(
            "--authorize-remote-query",
            help="Sorgu embedding'ini uzak saglayiciya yollar",
        ),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    opencode_config: Annotated[
        Path, typer.Option("--opencode-config")
    ] = _DEFAULT_OPENCODE_CONFIG_FILE,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Aktif exact/lexical/dense proje indeksini sorgular."""

    if not authorize_remote_query:
        raise fail("Remote query embedding explicit --authorize-remote-query ister", 77)
    try:
        result = query_registered_project(
            resolve_home(home),
            _canonical_slug(project, home=home),
            question,
            opencode_config=opencode_config,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
    else:
        console.print(str(result.get("answer_excerpt", "")))


@app.command("index")
def index_command(
    project: Annotated[str, typer.Argument(help="Proje slug")],
    oracle_config: Annotated[str | None, typer.Option("--oracle-config")] = None,
    authorize_remote_source: Annotated[bool, typer.Option("--authorize-remote-source")] = False,
    authorize_database_metadata: Annotated[
        bool, typer.Option("--authorize-database-metadata")
    ] = False,
    authorize_odi_metadata: Annotated[bool, typer.Option("--authorize-odi-metadata")] = False,
    batch_size: Annotated[int, typer.Option("--batch-size", min=1, max=64)] = 64,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    opencode_config: Annotated[
        Path, typer.Option("--opencode-config")
    ] = _DEFAULT_OPENCODE_CONFIG_FILE,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kaynak kodu ve Oracle metadata snapshot'ini atomik yeniler."""

    if not authorize_remote_source:
        raise fail("Remote source disclosure explicit --authorize-remote-source ister", 77)
    if oracle_config is not None and not authorize_database_metadata:
        raise fail("Database metadata disclosure explicit authorization ister", 77)
    try:
        result = index_registered_project(
            resolve_home(home),
            _canonical_slug(project, home=home),
            oracle_config=oracle_config,
            opencode_config=opencode_config,
            batch_size=batch_size,
            authorize_odi_metadata=authorize_odi_metadata,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
    else:
        console.print(
            f"[green]Aktif:[/green] {result['generation_digest']} chunks={result['chunk_count']}"
        )
