"""`zekam ask` ve `zekam research` komutlari.

`ask` salt okunurdur: istegi siniflandirir, exact kimlikleri korur ve belirsizlik
varsa mutation baslatmadan netlestirme sorar. `research` alt komutlari kanonik rol
DAG'ini gosterir ve arastirma sorusunu kanonik store'a yazar.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.home import HomeLayout, resolve_home
from zekam.application.intake_service import IntakeOutcome, IntakeService
from zekam.application.local_embedding_composition import build_verified_mac_embedding
from zekam.application.local_runtime_boundary import (
    IntegrationStateRepository,
    ProjectRepository,
    ResearchRepository,
    RetrievalRepository,
)
from zekam.application.opencode_benchmark_campaign import (
    default_scope_file,
    discover_campaign,
    prepare_campaign_manifest,
)
from zekam.application.opencode_remote_benchmark import EVALUATOR_PROVENANCE_DIGEST
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.project_knowledge_index import build_project_index_plan
from zekam.application.project_rag_query import query_project_knowledge
from zekam.application.realm_context import RealmContext
from zekam.application.research_report_projection import (
    materialize_research_report,
    projection_path,
)
from zekam.application.research_service import default_dag_nodes
from zekam.domain.errors import ZekamError
from zekam.domain.identifiers import new_uuid7
from zekam.domain.intake import RequestClass, normalize_text
from zekam.domain.project import IntegrationStage, Project
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.research import (
    ResearchBudget,
    ResearchDag,
    ResearchNode,
    ResearchQuestion,
    SourceKind,
    SourcePolicy,
)
from zekam.interfaces.cli.session import (
    EXIT_AMBIGUOUS,
    HOME_HELP,
    REALM_HELP,
    RealmSession,
    fail,
    fail_from,
)

app = typer.Typer(name="research", help="Kanitli arastirma islemleri", no_args_is_help=True)
report_app = typer.Typer(name="report", help="Kanonik arastirma raporu gorunurlugu")
app.add_typer(report_app)
console = Console()

_ALL_BENCHMARK_CUES = ("tum", "hepsi", "butun", "all", "full")
_SINGLE_BENCHMARK_CUES = ("tek model", "bir model", "single model", "one model")


def _full_benchmark_plan() -> dict[str, object]:
    """Reviewed tam kampanyayi effect/authority uretmeden sanitize eder."""

    discovery = discover_campaign(
        scope_file=default_scope_file(),
        verifier_provenance_digest=EVALUATOR_PROVENANCE_DIGEST,
    )
    manifest = prepare_campaign_manifest(discovery)
    return {
        "status": "ready-for-explicit-authorization",
        "scope": "all-reviewed-aihub",
        "campaign_key": "opencode-aihub",
        "configured_model_count": discovery.configured_model_count,
        "canonical_target_count": discovery.canonical_target_count,
        "eligible_target_count": (
            discovery.canonical_target_count - discovery.audio_excluded_count
        ),
        "audio_excluded_count": discovery.audio_excluded_count,
        "health_call_count": discovery.health_call_count,
        "benchmark_call_count": discovery.tested_call_count,
        "maximum_provider_call_count": discovery.provider_call_budget,
        "repetitions": discovery.scope.repetitions,
        "manifest_digest": manifest.manifest_digest,
        "scope_digest": discovery.scope.scope_digest,
        "inventory_digest": discovery.inventory_digest,
        "fixture_registry_digest": discovery.fixture_registry_digest,
        "verifier_provenance_digest": discovery.verifier_provenance_digest,
        "question": "Bu exact tam kampanya planini authorize edip baslatalim mi?",
        "authority_records_created": 0,
        "provider_calls_made": 0,
        "network_calls_made": 0,
        "audio_provider_calls_made": 0,
        "grants_authority": False,
    }


def _benchmark_prepare(text: str) -> dict[str, object]:
    normalized = normalize_text(text)

    def _matches(cue: str) -> bool:
        return re.search(rf"(?<!\w){re.escape(cue)}(?!\w)", normalized) is not None

    wants_all = any(_matches(cue) for cue in _ALL_BENCHMARK_CUES)
    wants_single = any(_matches(cue) for cue in _SINGLE_BENCHMARK_CUES)
    wants_project = re.search(r"(?<!\w)(?:proje|project)\w*", normalized) is not None
    zero_effect = {
        "authority_records_created": 0,
        "provider_calls_made": 0,
        "network_calls_made": 0,
        "audio_provider_calls_made": 0,
        "grants_authority": False,
    }
    if wants_project:
        return {
            "status": "project-suite-required",
            "scope": "project-specific",
            "question": "Hangi exact proje ve reviewed project benchmark suite kullanilsin?",
            "limitation": "Global AIHub kampanyasi proje-ozel benchmark yerine kullanilamaz.",
            **zero_effect,
        }
    if wants_all == wants_single:
        return {
            "status": "scope-required",
            "scope": None,
            "question": "Tam reviewed AIHub kampanyasi mi, tek model tanilamasi mi?",
            "options": ["all-reviewed-aihub", "single-model-diagnostic"],
            **zero_effect,
        }
    if wants_single:
        return {
            "status": "unsupported-by-remote-campaign",
            "scope": "single-model-diagnostic",
            "question": "Hangi exact model kimligi yerel tek-model tanilamasinda kullanilsin?",
            "limitation": (
                "Remote OpenCode/AIHub campaign exact tek-model kapsam desteklemiyor; "
                "yerel tek-model sonucu ZEKAM-DOD-025 veya 83/83 kaniti sayilmaz."
            ),
            **zero_effect,
        }
    return _full_benchmark_plan()


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
    retrieval: dict[str, object] | None = None
    try:
        local_outcome = IntakeService().resolve(
            text,
            now=_now(),
            project_required=False,
        )
        if local_outcome.resolution.request_class is RequestClass.BENCHMARK:
            outcome = local_outcome
        else:
            with RealmSession(home, realm) as realm_context:
                outcome = _intake(realm_context, text)
                resolution = outcome.resolution
                if (
                    resolution.request_class is RequestClass.RESEARCH
                    and resolution.project_ref is not None
                ):
                    projects = ProjectRepository(realm_context.connection, realm_context.realm_id)
                    project = projects.find_by_slug(resolution.project_ref)
                    if project is not None:
                        stage, _revision_id, detail = IntegrationStateRepository(
                            realm_context.connection, realm_context.realm_id
                        ).get(project.id)
                        embedding = None
                        knowledge_index = detail.get("knowledge_index") or {}
                        if (
                            stage is IntegrationStage.CURRENT
                            and knowledge_index.get("state") == "ready"
                        ):
                            integration = ProjectIntegrationService(
                                realm_context.connection, realm_context.realm
                            )
                            plan = build_project_index_plan(
                                project_id=project.id,
                                project_slug=project.slug,
                                source_root=integration.resolve_source_root(project.id),
                                source_revision=str(knowledge_index.get("source_revision", "")),
                                expected_tree_digest=str(knowledge_index.get("tree_digest", "")),
                            )
                            embedding = build_verified_mac_embedding(plan.chunks)
                        retrieval = query_project_knowledge(
                            repository=RetrievalRepository(
                                realm_context.connection,
                                realm_context.realm_id,
                                project_id=project.id,
                            ),
                            project_ref=project.slug,
                            query=text,
                            integration_stage=stage,
                            integration_detail=detail,
                            embedding_provider=(
                                embedding.provider if embedding is not None else None
                            ),
                            embedding_policy=(embedding.policy if embedding is not None else None),
                        )
    except ZekamError as exc:
        raise fail_from(exc) from exc

    benchmark_prepare = (
        _benchmark_prepare(text)
        if outcome.resolution.request_class is RequestClass.BENCHMARK
        else None
    )
    document = outcome.as_dict()
    if retrieval is not None:
        document["retrieval"] = retrieval
    if benchmark_prepare is not None:
        document["benchmark_prepare"] = benchmark_prepare
    if as_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        _render_intake(outcome)
        if benchmark_prepare is not None:
            console.print_json(json.dumps(benchmark_prepare, ensure_ascii=False))
    if (not outcome.may_start_work and retrieval is None) or (
        benchmark_prepare is not None
        and benchmark_prepare["status"] != "ready-for-explicit-authorization"
    ):
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


def _report_scope(realm_context: RealmContext, project: str) -> tuple[Project, ResearchRepository]:
    found = ProjectRepository(realm_context.connection, realm_context.realm_id).find_by_slug(
        project
    )
    if found is None:
        raise fail(f"Proje bulunamadi: {project}")
    repository = ResearchRepository(
        realm_context.connection,
        realm_context.realm_id,
        found.id,
        UUID(int=0),
    )
    return found, repository


@report_app.command("list")
def report_list_command(
    project: Annotated[str, typer.Argument(help="Proje slug")],
    limit: Annotated[int, typer.Option("--limit", help="En fazla 500 rapor")] = 100,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """DB authority raporlarini ve projection durumunu bounded listeler."""

    try:
        with RealmSession(home, realm) as realm_context:
            found, repository = _report_scope(realm_context, project)
            layout = HomeLayout(resolve_home(home))
            rows = []
            for row in repository.list_reports(limit=limit):
                report_id = row["report_id"]
                path = (
                    None if report_id is None else projection_path(layout, str(found.id), report_id)
                )
                rows.append(
                    dict(
                        row,
                        projection_path=None if path is None else str(path),
                        projected=bool(path and path.is_file()),
                    )
                )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    payload = {"reports": rows, "count": len(rows), "grants_authority": False}
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        console.print_json(json.dumps(payload, ensure_ascii=False, default=str))


@report_app.command("show")
def report_show_command(
    project: Annotated[str, typer.Argument(help="Proje slug")],
    report: Annotated[UUID, typer.Argument(help="DB report UUID")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Raporu DB'den yukler ve digest'ini yeniden dogrular."""

    try:
        with RealmSession(home, realm) as realm_context:
            _, repository = _report_scope(realm_context, project)
            document = repository.report_document(report)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@report_app.command("rebuild")
def report_rebuild_command(
    project: Annotated[str, typer.Argument(help="Proje slug")],
    report: Annotated[UUID, typer.Argument(help="DB report UUID")],
    apply: Annotated[bool, typer.Option("--uygula", help="Projection dosyasini yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Eksik veya stale Markdown projection'i DB authority kaydindan yeniden kurar."""

    try:
        with RealmSession(home, realm) as realm_context:
            found, repository = _report_scope(realm_context, project)
            document = repository.report_document(report)
            if not apply:
                target = projection_path(
                    HomeLayout(resolve_home(home)), str(found.id), str(document["report_id"])
                )
                console.print_json(
                    json.dumps(
                        {
                            "dry_run": True,
                            "path": str(target),
                            "report_digest": document["report_digest"],
                            "grants_authority": False,
                        }
                    )
                )
                return
            result = materialize_research_report(
                HomeLayout(resolve_home(home)), str(found.id), document
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(result.as_dict(), ensure_ascii=False, default=str))
