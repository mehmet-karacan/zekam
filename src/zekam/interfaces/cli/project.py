"""Local operational project registry commands."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console

from zekam.application.code_graph import (
    CodeGraphBuildPlan,
    apply_graph_build,
    graph_store_path,
    plan_graph_build,
)
from zekam.application.code_graph_python import PythonAstExtractor
from zekam.application.code_graph_query import (
    graph_find,
    graph_impact,
    graph_map,
    graph_outline,
)
from zekam.application.config import EmbeddingRoute
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
    build_project_source_binding_plan,
    classify_project_source,
    index_registered_project,
    project_embedding_route,
    project_rag_status,
    query_registered_project,
    read_project_citation,
    resolve_project_source,
)
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.identifiers import normalize_slug, validate_slug
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore
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


def _print_json(document: object) -> None:
    """Emit one ASCII-safe JSON document for locale-independent automation."""

    typer.echo(json.dumps(document, ensure_ascii=True, sort_keys=True))


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
        _print_json({"slug": selected_slug, "source_kind": "read-only", "apply": False})
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
                source_kind=classify_project_source(resolved),
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
        _print_json(rows)
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
        _print_json({"project": project, "alias": alias, "apply": False})
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
    _print_json(
        {"project": resolved.slug, "alias": alias, "aliases": aliases, "apply": True}
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
        _print_json({"project": project, "alias": alias, "apply": False})
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
    _print_json(
        {"project": resolved.slug, "alias": alias, "aliases": aliases, "apply": True}
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
        _print_json(document)
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
        _print_json(document)
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

    try:
        resolved_home = resolve_home(home)
        slug = _canonical_slug(project, home=home)
        result = (
            bind_project_source(resolved_home, slug, source)
            if apply
            else build_project_source_binding_plan(resolved_home, slug, source)
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _print_json(result)


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
        _print_json(document)
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
        _print_json(document)
    else:
        _print_json(document)


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
    _print_json(document)


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
        _print_json(document)
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
        _print_json({"project": slug, "source_root": str(root)})
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
        _print_json(result)
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
        _print_json(result)
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

    try:
        resolved_home = resolve_home(home)
        result = query_registered_project(
            resolved_home,
            _canonical_slug(project, home=home),
            question,
            opencode_config=opencode_config,
            authorize_remote_query=authorize_remote_query,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        _print_json(result)
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

    if oracle_config is not None and not authorize_database_metadata:
        raise fail("Database metadata disclosure explicit authorization ister", 77)
    try:
        resolved_home = resolve_home(home)
        route = project_embedding_route(resolved_home)
        if route is EmbeddingRoute.REMOTE and not authorize_remote_source:
            raise fail("Remote source disclosure explicit --authorize-remote-source ister", 77)
        result = index_registered_project(
            resolved_home,
            _canonical_slug(project, home=home),
            oracle_config=oracle_config,
            opencode_config=opencode_config,
            batch_size=batch_size,
            authorize_odi_metadata=authorize_odi_metadata,
            authorize_remote_source=authorize_remote_source,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        _print_json(result)
    else:
        console.print(
            f"[green]Aktif:[/green] {result['generation_digest']} chunks={result['chunk_count']}"
        )


# --- Context Graph Engine (G0/G1): graph plan|build|status|check ------------

graph_app = typer.Typer(
    name="graph",
    help="Proje structural code graph (rebuildable, tied to plan)",
    no_args_is_help=True,
)
_GRAPH_EXTRACTOR = PythonAstExtractor()


def _source_revision(source_root: Path) -> str:
    """Best-effort git HEAD label; fallback to a stable local label."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode == 0:
            head = completed.stdout.strip()
            if len(head) == 40:
                return head
    except OSError:
        pass
    return "working"


def _graph_plan(
    project_ref: str,
    *,
    source_root: Path,
    resolved: dict[str, object],
    created_at: str,
) -> CodeGraphBuildPlan:
    return plan_graph_build(
        source_root,
        project_id=str(resolved["id"]),
        project_slug=str(resolved["slug"]),
        source_revision=_source_revision(source_root),
        extractor=_GRAPH_EXTRACTOR,
        created_at=created_at,
    )


@graph_app.command("plan")
def graph_plan_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Mutation yapmadan exact graph build planini uretir."""
    try:
        resolved = _resolve_project_document(project, home=home, realm=DEFAULT_REALM_SLUG)
        source_root = resolve_project_source(resolve_home(home), str(resolved["slug"]))
        plan = _graph_plan(project, source_root=source_root, resolved=resolved,
                           created_at=dt.datetime.now(dt.UTC).isoformat())
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        _print_json(
            {
                "schema": "zekam-project-graph-plan/v1",
                "project": str(resolved["slug"]),
                "plan_digest": plan.plan_digest,
                "project_id": plan.project_id,
                "source_revision": plan.source_revision,
                "tree_digest": plan.tree_digest,
                "source_manifest_digest": plan.source_manifest_digest,
                "extractor_profile_digest": plan.extractor_profile_digest,
                "file_count": len(plan.file_manifests),
                "apply": False,
            }
        )
    else:
        console.print(
            f"[green]Graph plan:[/green] {resolved['slug']} files={len(plan.file_manifests)}"
        )
        console.print(f"Plan digest: {plan.plan_digest}")


@graph_app.command("build")
def graph_build_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Digest-bound graph generation build; varsayilan dry-run'dir."""
    try:
        resolved = _resolve_project_document(project, home=home, realm=DEFAULT_REALM_SLUG)
        resolved_home = resolve_home(home)
        source_root = resolve_project_source(resolved_home, str(resolved["slug"]))
        plan = _graph_plan(project, source_root=source_root, resolved=resolved,
                           created_at=dt.datetime.now(dt.UTC).isoformat())
        if not apply:
            document = {
                "schema": "zekam-project-graph-plan/v1",
                "project": str(resolved["slug"]),
                "plan_digest": plan.plan_digest,
                "file_count": len(plan.file_manifests),
                "apply": False,
                "build_dry_run": True,
            }
        else:
            if plan_digest is None:
                raise PolicyViolation("Graph build --uygula exact --plan-digest ister")
            if plan_digest != plan.plan_digest:
                raise PolicyViolation("Graph build stale/yanlis plan digest")
            store_path = graph_store_path(resolved_home, str(resolved["slug"]))
            with SQLiteCodeGraphStore(store_path, create=True) as store:
                generation = apply_graph_build(store, _GRAPH_EXTRACTOR, source_root, plan)
                document = {
                    "schema": "zekam-project-graph-build/v1",
                    "project": str(resolved["slug"]),
                    "plan_digest": plan.plan_digest,
                    "generation_digest": generation.generation_digest,
                    "project_id": generation.project_id,
                    "source_revision": generation.source_revision,
                    "file_count": generation.file_count,
                    "symbol_count": generation.symbol_count,
                    "edge_count": generation.edge_count,
                    "error_count": generation.error_count,
                    "state": generation.state,
                    "store_path": store_path.as_posix(),
                    "apply": True,
                }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        _print_json(document)
    else:
        console.print(
            f"[green]Graph build:[/green] {resolved['slug']} "
            f"{document['file_count']} files plan={document['plan_digest']}"
        )
        if not apply:
            console.print(
                "[yellow]Dry-run. Uygulamak icin --uygula ve exact --plan-digest verin.[/yellow]"
            )


@graph_app.command("status")
def graph_status_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Aktif graph generation durumunu yazar (mutation yapmaz)."""
    try:
        resolved = _resolve_project_document(project, home=home, realm=DEFAULT_REALM_SLUG)
        resolved_home = resolve_home(home)
        store_path = graph_store_path(resolved_home, str(resolved["slug"]))
        document: dict[str, object] = {
            "schema": "zekam-project-graph-status/v1",
            "project": str(resolved["slug"]),
        }
        if not store_path.is_file():
            document["state"] = "unavailable"
        else:
            with SQLiteCodeGraphStore(store_path) as store:
                document.update(store.status(str(resolved["id"])))
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        _print_json(document)
    else:
        console.print(f"{document['project']}\t{document['state']}")


@graph_app.command("check")
def graph_check_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Graph store butunlugunu (quick_check + FK + gen count) raporlar."""
    try:
        resolved = _resolve_project_document(project, home=home, realm=DEFAULT_REALM_SLUG)
        resolved_home = resolve_home(home)
        store_path = graph_store_path(resolved_home, str(resolved["slug"]))
        document: dict[str, object] = {
            "schema": "zekam-project-graph-check/v1",
            "project": str(resolved["slug"]),
        }
        if not store_path.is_file():
            document["status"] = "unavailable"
        else:
            with SQLiteCodeGraphStore(store_path) as store:
                document.update(store.integrity())
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        _print_json(document)
    else:
        console.print(f"{document['project']}\t{document['status']}")


# --- Context Graph Query (G3): find|outline|impact|map (read-only) -----------


def _graph_read_store(store_path: Path) -> SQLiteCodeGraphStore:
    """Open the graph store in immutable read-only mode for query commands."""
    return SQLiteCodeGraphStore(store_path, read_only=True)


@graph_app.command("find")
def graph_find_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    query: Annotated[str, typer.Argument(help="Symbol veya dosya adi")],
    limit: Annotated[int, typer.Option("--limit")] = 50,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Salt-okunur exact + lexical symbol/file eslesmesi."""
    try:
        resolved = _resolve_project_document(project, home=home, realm=DEFAULT_REALM_SLUG)
        resolved_home = resolve_home(home)
        store_path = graph_store_path(resolved_home, str(resolved["slug"]))
        document: dict[str, object] = {
            "schema": "zekam-project-graph-find/v1",
            "project": str(resolved["slug"]),
            "query": query,
        }
        if not store_path.is_file():
            document["state"] = "unavailable"
            document["matches"] = []
        else:
            with _graph_read_store(store_path) as store:
                matches = graph_find(store, str(resolved["id"]), query, limit=limit)
                document["state"] = "ready"
                document["count"] = len(matches)
                document["matches"] = matches
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        _print_json(document)
    else:
        console.print(
            f"{document['project']}\t{document['state']} count={document.get('count', 0)}"
        )


@graph_app.command("outline")
def graph_outline_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    file: Annotated[str, typer.Argument(help="Proje-relative dosya yolu")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Salt-okunur dosya hiyerarsik outline'i (module->class->function/method)."""
    try:
        resolved = _resolve_project_document(project, home=home, realm=DEFAULT_REALM_SLUG)
        resolved_home = resolve_home(home)
        store_path = graph_store_path(resolved_home, str(resolved["slug"]))
        document: dict[str, object] = {
            "schema": "zekam-project-graph-outline/v1",
            "project": str(resolved["slug"]),
            "relative_path": file,
        }
        if not store_path.is_file():
            document["state"] = "unavailable"
            document["outline"] = []
        else:
            with _graph_read_store(store_path) as store:
                outline = graph_outline(store, str(resolved["id"]), file)
                document["state"] = "ready"
                document["count"] = len(outline)
                document["outline"] = outline
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        _print_json(document)
    else:
        console.print(
            f"{document['project']}\t{document['state']} count={document.get('count', 0)}"
        )


@graph_app.command("impact")
def graph_impact_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    symbol: Annotated[str, typer.Argument(help="Nitelikli symbol adi")],
    max_depth: Annotated[int, typer.Option("--max-depth")] = 100,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Salt-okunur symbol blast radius'i (dependency edges, contains haric)."""
    try:
        resolved = _resolve_project_document(project, home=home, realm=DEFAULT_REALM_SLUG)
        resolved_home = resolve_home(home)
        store_path = graph_store_path(resolved_home, str(resolved["slug"]))
        document: dict[str, object] = {
            "schema": "zekam-project-graph-impact/v1",
            "project": str(resolved["slug"]),
            "symbol": symbol,
        }
        if not store_path.is_file():
            document["state"] = "unavailable"
        else:
            with _graph_read_store(store_path) as store:
                impact = graph_impact(
                    store, str(resolved["id"]), symbol, max_depth=max_depth
                )
                document["state"] = "ready" if impact.get("found") else "not-found"
                document.update(impact)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        _print_json(document)
    else:
        console.print(f"{document['project']}\t{document['state']}")


@graph_app.command("map")
def graph_map_command(
    project: Annotated[str, typer.Argument(help="Proje slug veya alias")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Salt-okunur repository map (per-file aggregation)."""
    try:
        resolved = _resolve_project_document(project, home=home, realm=DEFAULT_REALM_SLUG)
        resolved_home = resolve_home(home)
        store_path = graph_store_path(resolved_home, str(resolved["slug"]))
        document: dict[str, object] = {
            "schema": "zekam-project-graph-map/v1",
            "project": str(resolved["slug"]),
        }
        if not store_path.is_file():
            document["state"] = "unavailable"
            document["files"] = []
        else:
            with _graph_read_store(store_path) as store:
                files = graph_map(store, str(resolved["id"]))
                document["state"] = "ready"
                document["count"] = len(files)
                document["files"] = files
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        _print_json(document)
    else:
        console.print(
            f"{document['project']}\t{document['state']} files={document.get('count', 0)}"
        )


app.add_typer(graph_app)
