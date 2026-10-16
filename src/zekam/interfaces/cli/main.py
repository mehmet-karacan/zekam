"""Zekam komut satiri giris noktasi.

CLI ayri urun kurali tanimlamaz; application katmanindaki servisleri cagirir.
Cikis kodlari otomasyon icin kararlidir.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from zekam import __version__
from zekam.application.active_task_contract import ActiveTaskContract
from zekam.application.capability_inventory import capability_inventory
from zekam.application.composition import ApplicationContext, build_context, build_doctor
from zekam.application.config import USER_CONFIG_FILE, PersistenceBackend
from zekam.application.diagnostics import DoctorReport, OverallStatus, Severity
from zekam.application.fresh_bootstrap import apply_fresh_bootstrap, plan_fresh_bootstrap
from zekam.application.home import resolve_home
from zekam.application.local_config_structure import parse_local_config_structure
from zekam.application.mutation_admission import (
    CLI_MUTATION_REGISTRY_META_KEY,
    DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY,
)
from zekam.application.natural_request_answer import answer_natural_request
from zekam.application.natural_request_retrieval import retrieve_natural_request
from zekam.application.natural_request_scope import (
    NaturalRequestScope,
    resolve_natural_request_scope,
)
from zekam.application.natural_work_read import WorkReadRequest, parse_work_read, read_work_request
from zekam.application.opencode_embedding import default_opencode_config_file
from zekam.application.project_history_query import query_issue_history
from zekam.application.project_rag_runtime import (
    AskProjectUnresolved,
    query_registered_project,
    resolve_project_source,
    resolve_question_project,
    resolve_registered_project,
)
from zekam.application.request_routing import (
    RegisteredProject,
    load_project_families,
    route_request,
)
from zekam.application.setup import (
    build_setup_plan,
    setup_plan_digest,
    setup_plan_payload,
    setup_readiness,
)
from zekam.application.workspace_resume import build_resume_packet, render_resume_prompt
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.identity import PRODUCT
from zekam.domain.intake import normalize_text
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.work import OPEN_STATES
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.infrastructure.sqlite.operational_schema import SQLiteOperationalSchema
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli import backup as backup_commands
from zekam.interfaces.cli import client as client_commands
from zekam.interfaces.cli import configuration as configuration_commands
from zekam.interfaces.cli import continuity as continuity_commands
from zekam.interfaces.cli import db as db_commands
from zekam.interfaces.cli import evolve as evolve_commands
from zekam.interfaces.cli import idea as idea_commands
from zekam.interfaces.cli import integration as integration_commands
from zekam.interfaces.cli import jira as jira_commands
from zekam.interfaces.cli import knowledge as knowledge_commands
from zekam.interfaces.cli import local_core as local_core_commands
from zekam.interfaces.cli import local_runtime as local_runtime_commands
from zekam.interfaces.cli import mcp as mcp_commands
from zekam.interfaces.cli import memory as memory_commands
from zekam.interfaces.cli import model as model_commands
from zekam.interfaces.cli import opencode as opencode_commands
from zekam.interfaces.cli import project as project_commands
from zekam.interfaces.cli import protocol as protocol_commands
from zekam.interfaces.cli import research as research_commands
from zekam.interfaces.cli import route as route_commands
from zekam.interfaces.cli import sandbox as sandbox_commands
from zekam.interfaces.cli import scheduler as scheduler_commands
from zekam.interfaces.cli import skill as skill_commands
from zekam.interfaces.cli import surface as surface_commands
from zekam.interfaces.cli import unit_test as unit_test_commands
from zekam.interfaces.cli import work as work_commands
from zekam.interfaces.cli import worker as worker_commands
from zekam.interfaces.cli.session import REALM_HELP, fail_from

#: Toplam duruma karsilik gelen kararli cikis kodlari.
EXIT_CODES: dict[OverallStatus, int] = {
    OverallStatus.HEALTHY: 0,
    OverallStatus.DEGRADED: 1,
    OverallStatus.BLOCKED: 2,
    OverallStatus.RECOVERY_REQUIRED: 3,
}

_HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"

EXIT_USAGE_ERROR = 64
EXIT_RUNTIME_ERROR = 70
#: Deterministic kapsam belirsizligi; `zekam test` kapi koduyla ayni.
EXIT_CLARIFICATION = 65

app = typer.Typer(
    name=PRODUCT.cli,
    help=f"{PRODUCT.name} - kanit tabanli calisma ve bilgi platformu",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
error_console = Console(stderr=True)


def _print_json(document: object) -> None:
    """Emit machine JSON independently of the active Windows code page."""

    typer.echo(json.dumps(document, ensure_ascii=True, sort_keys=True, default=str))


_CONFIG_PATH = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:[A-Za-z0-9_./-]*/)?"
    r"(?:\.env(?:\.[A-Za-z0-9_-]+)?|[A-Za-z0-9_.-]+\.properties)"
    r"(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_CONFIG_INTENT = (
    "yapi",
    "yapı",
    "anahtar",
    "analiz",
    "incele",
    "ogren",
    "öğren",
    "structure",
    "keys",
    "inspect",
    "learn",
    "oku",
)


def _config_paths_for_question(question: str) -> tuple[str, ...]:
    lowered = question.casefold()
    if not any(intent in lowered for intent in _CONFIG_INTENT):
        return ()
    if "\\" in question and (".env" in lowered or "properties" in lowered):
        raise PolicyViolation("Config icin proje kokune gore slash'li exact yol gerekli")
    paths = tuple(dict.fromkeys(match.group(0) for match in _CONFIG_PATH.finditer(question)))
    if len(paths) > 8:
        raise PolicyViolation("Config yapisi istegi en fazla 8 exact dosya kabul eder")
    if "properties" in lowered and not any(
        path.casefold().endswith(".properties") for path in paths
    ):
        raise PolicyViolation("Properties icin exact goreli dosya yolu gerekli")
    if ".env" in lowered and not any(
        path.rsplit("/", 1)[-1].casefold().startswith(".env") for path in paths
    ):
        raise PolicyViolation("Env icin exact goreli dosya yolu gerekli")
    return paths


_OPERATIONAL_SCHEMA = SQLiteOperationalSchema()
_DEFAULT_OPENCODE_CONFIG_FILE = default_opencode_config_file()

app.add_typer(db_commands.app)
app.add_typer(evolve_commands.app)
app.add_typer(project_commands.app)
app.add_typer(protocol_commands.app)
app.add_typer(work_commands.app)
app.add_typer(model_commands.app)
app.add_typer(opencode_commands.app)
app.add_typer(backup_commands.app)
app.add_typer(client_commands.app)
app.add_typer(sandbox_commands.sandbox_app)
app.add_typer(sandbox_commands.git_app)
app.add_typer(knowledge_commands.app)
app.add_typer(local_runtime_commands.app)
app.add_typer(local_core_commands.app)
app.add_typer(continuity_commands.app)
app.add_typer(configuration_commands.config_app)
app.add_typer(configuration_commands.permission_app)
app.add_typer(jira_commands.app)
app.add_typer(idea_commands.app)
app.add_typer(route_commands.app)
app.add_typer(research_commands.app)
app.add_typer(surface_commands.app)
app.add_typer(worker_commands.app)
app.add_typer(scheduler_commands.app)
app.add_typer(skill_commands.app)
app.add_typer(integration_commands.app)
app.add_typer(mcp_commands.app)
app.add_typer(memory_commands.app)
app.add_typer(unit_test_commands.app)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"{PRODUCT.name} {__version__}")
        raise typer.Exit(0)


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Surumu yazar"),
    ] = False,
) -> None:
    """Ortak secenekler."""

    # Child command contexts inherit this immutable registry through Click
    # ``meta``.  The registry is authority-free; leaf services retain their
    # existing exact plan/authorization/claim/receipt gates.
    ctx.find_root().meta[CLI_MUTATION_REGISTRY_META_KEY] = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY


@app.command("capabilities")
def capabilities_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Reviewed inventory plus provider-free local readiness gaps."""

    context = build_context(home=home) if home is not None else None
    document = capability_inventory(
        root=None if context is None else context.core_path,
        settings=None if context is None else context.settings,
    )
    if output_json:
        _print_json(document)
        return
    counts = document["counts"]
    readiness_counts = document["readiness_counts"]
    console.print(
        f"ready={counts['ready']} partial={counts['partial']} scaffold={counts['scaffold']}"
    )
    console.print(
        "readiness="
        f"{readiness_counts['ready']} ready, "
        f"{readiness_counts['partial']} partial, "
        f"{readiness_counts['blocked']} blocked"
    )


@app.command("resume")
def resume_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    prompt: Annotated[bool, typer.Option("--prompt")] = False,
    session_id: Annotated[str | None, typer.Option("--session")] = None,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Model-independent work, checkpoint, project and capability resume packet."""

    if output_json and prompt:
        error_console.print("[red]Hata:[/red] --json ve --prompt birlikte kullanilamaz")
        raise typer.Exit(EXIT_USAGE_ERROR)
    try:
        document = build_resume_packet(resolve_home(home), session_id=session_id)
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    if prompt:
        typer.echo(render_resume_prompt(document))
    elif output_json:
        _print_json(document)
    else:
        checkpoint = document["latest_semantic_checkpoint"]
        completed = checkpoint["completed"] if checkpoint else "semantic checkpoint yok"
        console.print(f"{document['semantic_state']}: {completed}")
        console.print(f"siradaki: {document['next_safe_action']}")


def _registered_project_refs(resolved_home: Path) -> frozenset[str]:
    """Operasyonel store'daki aktif proje slug'larini dogrulanmis set olarak dondurur."""

    store = SQLiteOperationalStore(resolved_home / "state" / "operational.db")
    with store.unit_of_work() as uow:
        refs = frozenset(project.slug for project in uow.list_projects(include_archived=False))
        uow.commit()
    return refs


def _natural_request_scope(
    resolved_home: Path, question: str
) -> tuple[NaturalRequestScope, frozenset[str]]:
    """Return one deterministic scope decision and its verified registry snapshot."""

    registered_refs = _registered_project_refs(resolved_home)
    route = route_request(
        question,
        catalog=load_project_families(),
        registered_projects=tuple(RegisteredProject(ref) for ref in sorted(registered_refs)),
    )
    scope = resolve_natural_request_scope(route, registered_project_refs=registered_refs)
    return scope, registered_refs


def _ask_project_history(
    resolved_home: Path,
    question: str,
    *,
    explicit_project: str | None,
    output_json: bool,
) -> bool:
    """Route exact Jira history questions to bounded local Git evidence."""

    if not re.search(
        r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,15}-[0-9]{1,7}(?![A-Za-z0-9])", question, re.I
    ):
        return False
    if not re.search(
        r"(?i)(?<!\w)(?:degisiklik|değişiklik|degisti|değişti|commit|diff|gecmis|geçmiş|hangi kod)",
        question,
    ):
        return False
    registered_refs = _registered_project_refs(resolved_home)
    route = route_request(
        question,
        catalog=load_project_families(),
        registered_projects=tuple(RegisteredProject(ref) for ref in sorted(registered_refs)),
    )
    if route.status != "selected" or route.intent != "jira-history" or route.jira_issue_key is None:
        return False
    scope = resolve_natural_request_scope(
        route,
        registered_project_refs=registered_refs,
        explicit_project_ref=explicit_project,
    )
    if scope.status != "selected" or len(scope.project_refs) != 1:
        document = {
            "schema": "zekam-ask-project-history/v1",
            "status": "clarification-required",
            "question": "Bu Jira gecmisini hangi Zekam projesiyle eslestirelim?",
            "project_refs": list(scope.project_refs),
            "jira_issue_key": route.jira_issue_key,
            "provider_calls": 0,
            "index_triggered": False,
            "read_only": True,
            "grants_authority": False,
        }
        if output_json:
            _print_json(document)
        else:
            console.print(document["question"])
        raise typer.Exit(EXIT_CLARIFICATION)
    project_ref = scope.project_refs[0]
    source_root = resolve_project_source(resolved_home, project_ref)
    history = query_issue_history(source_root, route.jira_issue_key).as_dict()
    document = {
        "schema": "zekam-ask-project-history/v1",
        "status": "reported",
        "route": "project-history",
        "project_ref": project_ref,
        "jira_issue_key": route.jira_issue_key,
        "history": history,
        "provider_calls": 0,
        "index_triggered": False,
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        _print_json(document)
    else:
        console.print(
            f"{project_ref}: {route.jira_issue_key} icin {history['commit_count']} bounded commit"
        )
        for commit in history["commits"]:
            console.print(f"{commit['commit'][:8]}\t{commit['subject']}")
    return True


def _clarification_question(exc: AskProjectUnresolved) -> str:
    """Kullanici tarafina donen en kisa Turkce aciklama sorusu (WP-03.03).

    Teknik reason code veya komut ismi kullanicidan istenmez; soru, soruda
    sinyal edilen proje adlariyla (kayitli olsun veya olmasin) dogrudan
    netlestirilmeye davet eder.
    """

    if "jira-project-unresolved" in exc.reason_codes:
        return "Sordugunuz Jira is kaydi hangi Zekam projesine ait? Projeyi acikca belirtin."
    if len(exc.project_refs) > 1:
        refs = ", ".join(sorted(exc.project_refs)[:8])
        return (
            f"{refs} arasindan hangisi icin sordunuzu netlestiremedim; "
            "lutfen projeyi acikca belirtin."
        )
    if exc.project_refs:
        return (
            "Hangi Zekam projesi icin sormak istediginizi netlestiremedim; "
            "lutfen projeyi acikca belirtin."
        )
    if "selected-projects-unavailable" in exc.reason_codes and exc.matched_terms:
        term = max(exc.matched_terms, key=len)
        return (
            f"'{term}' kayitli bir Zekam projesi degil; hangi Zekam projesini sormak istiyorsunuz?"
        )
    return "Hangi Zekam projesi icin sordugunuzu netlestiremedim; lutfen projeyi acikca belirtin."


def _ask_clarification(exc: AskProjectUnresolved, *, output_json: bool) -> None:
    """Proje sinyali olan belirsiz/kayitsiz istekte sorgu/indeksleme olmadan
    kullanicidan netlestirme ister; JSON ciktisi makine-okunur kalir."""

    document = {
        "schema": "zekam-ask-clarification/v1",
        "status": "clarification-required",
        "route": "natural-request-scope",
        "reason_codes": list(exc.reason_codes),
        "jira_issue_key": exc.jira_issue_key,
        "project_refs": list(exc.project_refs),
        "question": _clarification_question(exc),
        "next_step": "Proje adini soruya ekleyip tekrar deneyin veya --project verin.",
        "provider_calls": 0,
        "index_triggered": False,
        "grants_authority": False,
    }
    if output_json:
        _print_json(document)
    else:
        console.print(document["question"])
        console.print(f"Sonraki adim: {document['next_step']}")
    raise typer.Exit(EXIT_CLARIFICATION)


_WORK_STATUS_OPEN_LIMIT = 100
_WORK_STATUS_CLOSED_LIMIT = 20
_WORK_STATUS_EVENT_LIMIT = 20
_WORK_STATUS_RUN_LIMIT = 5
_WORK_STATUS_STEP_LIMIT = 20
_WORK_STATUS_CHECKPOINT_LIMIT = 5
_WORK_STATUS_BOUND_JOB_LIMIT = 50

_WORK_STATUS_PHRASES = (
    "ne kadar is kaldi",
    "kalan isler",
    "kalan is",
    "hangi islerimiz",
    "hangi isler var",
    "nerede kaldik",
    "nerde kaldik",
    "durum",
    "ne yaptin",
    "ne yapildi",
    "hangi model",
    "ne kadar surdu",
    "work status",
    "status",
)


def _is_work_status_question(question: str) -> bool:
    """Read-only Work raporunu RAG'den once secen bounded niyet kapisi."""

    normalized = normalize_text(question)
    return any(phrase in normalized for phrase in _WORK_STATUS_PHRASES)


def _work_status_refs(
    resolved_home: Path,
    question: str,
    explicit_project: str | None,
) -> tuple[tuple[str, ...], str | None]:
    """Work status icin yalniz registry'de dogrulanmis proje slug'lari dondurur."""

    registered = _registered_project_refs(resolved_home)
    if explicit_project is not None:
        selected = resolve_registered_project(resolved_home, explicit_project)
        return (selected,), None
    route = route_request(
        question,
        catalog=load_project_families(),
        registered_projects=tuple(RegisteredProject(ref) for ref in sorted(registered)),
    )
    if route.status == "clarification-required":
        return (), "Hangi Zekam projesinin is durumunu gormek istiyorsunuz?"
    refs = tuple(ref for ref in route.project_refs if ref in registered)
    if route.project_refs and not refs:
        return (), "Sordugunuz proje kayitli degil; once dogru Zekam projesini belirtin."
    return (refs or tuple(sorted(registered))), None


def _ask_work_record(
    resolved_home: Path,
    question: str,
    request: WorkReadRequest,
    *,
    explicit_project: str | None,
    output_json: bool,
) -> None:
    refs, clarification = _work_status_refs(resolved_home, question, explicit_project)
    clarification = request.clarification or clarification
    if clarification is None and len(refs) != 1:
        clarification = "Hangi projenin is kaydini inceleyelim?"
    if clarification is not None:
        document = {
            "schema": "zekam-natural-work-read/v1",
            "status": "clarification-required",
            "question": clarification,
            "read_only": True,
            "grants_authority": False,
            "provider_calls": 0,
            "index_triggered": False,
        }
        if output_json:
            _print_json(document)
        else:
            console.print(clarification)
        raise typer.Exit(EXIT_CLARIFICATION)
    store = SQLiteOperationalStore(resolved_home / "state" / "operational.db")
    with store.unit_of_work() as uow:
        document = read_work_request(uow, request, project_ref=refs[0])
    if output_json:
        _print_json(document)
    else:
        work = document["work"]
        console.print(f"{work['title']} — {work['state']}; revizyon {work['revision']}")
        if request.operation == "history":
            for event in document["events"]:
                console.print(f"{event['revision']}\t{event['event_kind']}\t{event['to_state']}")
        elif request.operation == "dependencies":
            for dependency in document["dependencies"]:
                console.print(f"{dependency['title']}\t{dependency['state']}")
            if not document["dependencies"]:
                console.print("Kayitli bagimlilik yok.")
        elif request.operation == "checkpoints":
            for run in document["runs"]:
                console.print(f"Run {run['run_id']}: {run['status']}")
                for checkpoint in run["checkpoints"]:
                    console.print(f"  {checkpoint['sequence']}\t{checkpoint['checkpoint_digest']}")
            if not document["runs"]:
                console.print("Kayitli Run/checkpoint yok.")
        elif work["summary"]:
            console.print(work["summary"])
        if (
            document.get("truncated")
            or document.get("runs_truncated")
            or any(run["checkpoints_truncated"] for run in document.get("runs", []))
        ):
            console.print("Sonuclar bounded pencereyle sinirlandi; daha eski kayitlar var.")


def _ask_work_status(
    resolved_home: Path,
    question: str,
    *,
    explicit_project: str | None,
    output_json: bool,
) -> None:
    """Kanonik SQLite Work durumunu retrieval/provider baslatmadan raporlar."""

    refs, clarification = _work_status_refs(resolved_home, question, explicit_project)
    if clarification is not None:
        document = {
            "schema": "zekam-work-status/v1",
            "status": "clarification-required",
            "question": clarification,
            "project_refs": [],
            "provider_calls": 0,
            "index_triggered": False,
            "read_only": True,
            "grants_authority": False,
        }
        if output_json:
            _print_json(document)
        else:
            console.print(clarification)
        raise typer.Exit(EXIT_CLARIFICATION)

    store = SQLiteOperationalStore(resolved_home / "state" / "operational.db")
    projects: list[dict[str, Any]] = []
    with store.unit_of_work() as uow:
        for ref in refs:
            project = uow.resolve_project(ref)
            counts = uow.work_state_counts(project_id=project.id)
            open_window = uow.list_work_limited(
                project_id=project.id,
                states=tuple(sorted(OPEN_STATES)),
                limit=_WORK_STATUS_OPEN_LIMIT + 1,
            )
            closed_window = uow.list_work_limited(
                project_id=project.id,
                states=("completed", "cancelled", "archived"),
                limit=_WORK_STATUS_CLOSED_LIMIT + 1,
            )
            rows = (
                *open_window[:_WORK_STATUS_OPEN_LIMIT],
                *closed_window[:_WORK_STATUS_CLOSED_LIMIT],
            )
            bound_jobs = uow.list_bound_jobs_limited(
                work_item_ids=tuple(item.id for item in open_window[:_WORK_STATUS_OPEN_LIMIT]),
                limit=_WORK_STATUS_BOUND_JOB_LIMIT + 1,
            )
            dependencies = uow.work_dependency_summaries(tuple(item.id for item in rows))
            items: list[dict[str, Any]] = []
            for item in rows:
                events = list(uow.list_work_events(item.id, limit=_WORK_STATUS_EVENT_LIMIT + 1))
                runs: list[dict[str, Any]] = []
                run_window = uow.list_runs(item.id, limit=_WORK_STATUS_RUN_LIMIT + 1)
                for run in run_window[:_WORK_STATUS_RUN_LIMIT]:
                    step_window = uow.list_run_steps(run.id, limit=_WORK_STATUS_STEP_LIMIT + 1)
                    checkpoint_window = uow.list_checkpoints(
                        run.id, limit=_WORK_STATUS_CHECKPOINT_LIMIT + 1
                    )
                    receipt_status = "recorded" if run.terminal_receipt_digest else "unknown"
                    runs.append(
                        {
                            "id": run.id,
                            "status": run.status,
                            "plan_digest": run.plan_digest,
                            "terminal_receipt_digest": run.terminal_receipt_digest,
                            "receipt": {
                                "status": receipt_status,
                                "digest": run.terminal_receipt_digest,
                            },
                            "steps": [
                                {
                                    "id": step.id,
                                    "step_key": step.step_key,
                                    "status": step.status,
                                    "evidence_digest": step.evidence_digest,
                                }
                                for step in step_window[:_WORK_STATUS_STEP_LIMIT]
                            ],
                            "checkpoints": [
                                {
                                    "id": checkpoint.id,
                                    "sequence": checkpoint.sequence,
                                    "checkpoint_digest": checkpoint.checkpoint_digest,
                                }
                                for checkpoint in checkpoint_window[:_WORK_STATUS_CHECKPOINT_LIMIT]
                            ],
                            "steps_truncated": len(step_window) > _WORK_STATUS_STEP_LIMIT,
                            "checkpoints_truncated": len(checkpoint_window)
                            > _WORK_STATUS_CHECKPOINT_LIMIT,
                            # Run budget is a limit, never an actual usage measurement.
                            "usage": {
                                "status": "unknown",
                                "model": "unknown",
                                "provider_calls": None,
                                "tokens": None,
                                "duration_seconds": None,
                                "cost_micros": None,
                                "reason": "run-usage-receipt-missing",
                            },
                        }
                    )
                items.append(
                    {
                        "id": item.id,
                        "type": item.kind,
                        "state": item.state,
                        "title": item.title,
                        "revision": item.revision,
                        "evidence_digest": item.evidence_digest,
                        "dependencies": dependencies[item.id],
                        "events": events[-_WORK_STATUS_EVENT_LIMIT:],
                        "events_truncated": len(events) > _WORK_STATUS_EVENT_LIMIT,
                        "runs": runs,
                        "runs_truncated": len(run_window) > _WORK_STATUS_RUN_LIMIT,
                        "usage": {
                            "status": "unknown",
                            "reason": "run-usage-receipt-missing",
                        },
                    }
                )
            projects.append(
                {
                    "project_ref": ref,
                    "project_id": project.id,
                    "counts": counts,
                    "counts_scope": "all-project-work",
                    "dependency_blocked_count": sum(
                        bool(dependencies[item.id]["blocked"])
                        for item in open_window[:_WORK_STATUS_OPEN_LIMIT]
                    ),
                    "dependency_blocked_count_scope": "returned-open-window",
                    "open_count": sum(
                        count for state, count in counts.items() if state in OPEN_STATES
                    ),
                    "open_truncated": len(open_window) > _WORK_STATUS_OPEN_LIMIT,
                    "closed_truncated": len(closed_window) > _WORK_STATUS_CLOSED_LIMIT,
                    "bound_jobs": list(bound_jobs[:_WORK_STATUS_BOUND_JOB_LIMIT]),
                    "bound_jobs_truncated": len(bound_jobs) > _WORK_STATUS_BOUND_JOB_LIMIT,
                    "bound_jobs_scope": "returned-open-work-window",
                    "items": items,
                    "open_items": [
                        item
                        for item in items
                        if item["state"] not in {"completed", "cancelled", "archived"}
                    ],
                    "usage": {
                        "status": "unknown",
                        "reason": "run-usage-receipt-missing",
                    },
                }
            )
        # No commit: this report is a read-only snapshot of operational authority.
    document = {
        "schema": "zekam-work-status/v1",
        "status": "reported",
        "project_refs": list(refs),
        "projects": projects,
        "bounded": True,
        "detail_limits": {
            "open_work_per_project": _WORK_STATUS_OPEN_LIMIT,
            "closed_work_per_project": _WORK_STATUS_CLOSED_LIMIT,
            "events_per_work": _WORK_STATUS_EVENT_LIMIT,
            "runs_per_work": _WORK_STATUS_RUN_LIMIT,
            "steps_per_run": _WORK_STATUS_STEP_LIMIT,
            "checkpoints_per_run": _WORK_STATUS_CHECKPOINT_LIMIT,
            "bound_jobs_per_project": _WORK_STATUS_BOUND_JOB_LIMIT,
        },
        "usage": {
            "status": "unknown",
            "model": "unknown",
            "provider_calls": None,
            "tokens": None,
            "duration_seconds": None,
            "cost_micros": None,
            "reason": "run-usage-receipt-missing",
        },
        "provider_calls": 0,
        "index_triggered": False,
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        _print_json(document)
    else:
        total = sum(item["open_count"] for item in projects)
        completed = sum(item["counts"].get("completed", 0) for item in projects)
        total_items = sum(sum(item["counts"].values()) for item in projects)
        console.print(f"toplam is: {total_items}; tamamlanan: {completed}; acik: {total}")
        console.print("model/sure/maliyet: unknown (usage receipt yok)")
        for project in projects:
            console.print(f"{project['project_ref']}: {project['open_count']} acik is")
            if project["dependency_blocked_count"]:
                console.print(
                    f"  gosterilen acik islerde bagimlilik bekleyen: "
                    f"{project['dependency_blocked_count']}"
                )


def _ask_natural_request_retrieval(
    resolved_home: Path,
    question: str,
    scope: NaturalRequestScope,
    registered_refs: frozenset[str],
    *,
    output_json: bool,
    authorize_remote_query: bool,
    opencode_config: Path,
) -> None:
    """``--project`` verilmeyen ask'ta kanonik route/scope/retrieval yolunu calistirir.

    WP-03.03: acik iki kayitli proje karsilastirma sorusu deterministic
    ``route_request`` + ``resolve_natural_request_scope`` +
    ``retrieve_natural_request`` zincirinden iki ayri scoped sonuc uretir;
    sessiz fallback veya indeksleme baslatilmaz.
    """

    retrieval = retrieve_natural_request(
        scope,
        resolved_home,
        question,
        registered_project_refs=registered_refs,
        authorize_remote_query=authorize_remote_query,
        opencode_config=opencode_config,
    )
    answer_retrieval = _retrieval_for_answer(retrieval)
    answer = answer_natural_request(answer_retrieval, _retrieval_explanation(retrieval))
    result: dict[str, Any] = {
        "schema": "zekam-ask-multi-result/v1",
        "project_refs": list(scope.project_refs),
        "route": "natural-request-retrieval",
        "retrieval": retrieval,
        "answer": answer,
    }
    if output_json:
        _print_json(result)
        return
    console.print(answer["answer"])


def _retrieval_explanation(retrieval: dict[str, Any]) -> str | None:
    """Retrieval kanitindan metadata'siz, ayri aciklama onerisi olusturur."""

    excerpts: list[str] = []
    for entry in retrieval.get("results", []):
        if not isinstance(entry, dict) or entry.get("status") != "answered":
            continue
        payload = entry.get("result")
        if not isinstance(payload, dict):
            continue
        excerpt = payload.get("answer_excerpt")
        if isinstance(excerpt, str) and excerpt.strip():
            excerpts.append(excerpt.strip())
    return "\n\n".join(excerpts) or None


def _retrieval_for_answer(retrieval: dict[str, Any]) -> dict[str, Any]:
    """Eski RAG sonucundaki eksik citation alanlarini fail-closed normalleştirir."""

    body = dict(retrieval)
    normalized_results: list[Any] = []
    for entry in retrieval.get("results", []):
        if not isinstance(entry, dict):
            normalized_results.append(entry)
            continue
        normalized = dict(entry)
        result = entry.get("result")
        if normalized.get("status") == "answered" and isinstance(result, dict):
            safe_result = dict(result)
            citations = safe_result.get("citations")
            if not isinstance(citations, list):
                safe_result["citations"] = []
            safe_result.setdefault("evidence_found", bool(safe_result["citations"]))
            normalized["result"] = safe_result
        normalized_results.append(normalized)
    body["results"] = normalized_results
    body.pop("retrieval_digest", None)
    return body | {"retrieval_digest": digest(body)}


def _single_natural_request_retrieval(
    project_ref: str,
    question: str,
    retrieval: dict[str, Any],
    *,
    authorize_remote_query: bool,
) -> dict[str, Any]:
    """Legacy tek-proje sonucunu cevap kapisinin bekledigi envelope'a sarar."""

    raw = dict(retrieval)
    project_id = raw.get("project_id")
    state = raw.get("state")
    answered = (
        isinstance(project_id, str)
        and bool(project_id)
        and state
        in {
            "answered",
            "lexical-only-degraded",
            "abstained-low-evidence",
        }
    )
    if answered:
        citations = raw.get("citations")
        if not isinstance(citations, list):
            raw["citations"] = []
        raw.setdefault("evidence_found", bool(raw.get("citations")))
        entry: dict[str, Any] = {
            "project_ref": project_ref,
            "project_id": project_id,
            "selected_by": "explicit",
            "query_attempted": True,
            "status": "answered",
            "unavailable_reason": None,
            "result": raw,
        }
        status = "answered"
        reasons: list[str] = []
    else:
        entry = {
            "project_ref": project_ref,
            "selected_by": "explicit",
            "query_attempted": True,
            "status": "unavailable",
            "unavailable_reason": "query-unavailable",
            "result": None,
        }
        status = "unavailable"
        reasons = ["query-unavailable"]
    body: dict[str, Any] = {
        "schema": "zekam-natural-request-retrieval/v1",
        "status": status,
        "question_digest": digest({"question": question}),
        "project_refs": [project_ref],
        "reason_codes": reasons,
        "results": [entry],
        "query_count": 1,
        "remote_query_authorized": bool(authorize_remote_query),
        "source_access": "read-only",
        "observation_write_possible": True,
        "index_triggered": False,
        "grants_authority": False,
    }
    return body | {"retrieval_digest": digest(body)}


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Proje bilgisi icin exact kullanici sorusu")],
    project: Annotated[str | None, typer.Option("--project", help="Exact proje slug")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="RAG sonucunu JSON yazar")] = False,
    authorize_remote_query: Annotated[
        bool,
        typer.Option(
            "--authorize-remote-query", help="Sorgu embedding'ini uzak saglayiciya yollar"
        ),
    ] = False,
    opencode_config: Annotated[
        Path, typer.Option("--opencode-config")
    ] = _DEFAULT_OPENCODE_CONFIG_FILE,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
) -> None:
    """Dogal dil sorusunu aktif project-scoped hybrid RAG indeksine yonlendirir."""

    try:
        resolved_home = resolve_home(home).resolve(strict=True)
        work_request = parse_work_read(question)
        if work_request is not None:
            _ask_work_record(
                resolved_home,
                question,
                work_request,
                explicit_project=project,
                output_json=output_json,
            )
            return
        if _is_work_status_question(question):
            _ask_work_status(
                resolved_home,
                question,
                explicit_project=project,
                output_json=output_json,
            )
            return
        if _ask_project_history(
            resolved_home,
            question,
            explicit_project=project,
            output_json=output_json,
        ):
            return
        if project is None:
            natural_scope, registered_refs = _natural_request_scope(resolved_home, question)
            if natural_scope.status == "selected" and len(natural_scope.project_refs) > 1:
                # WP-03.03: acik coklu proje karsilastirma; kanonik route/scope/
                # retrieval yolu iki ayri scoped sonuc dondurur.
                _ask_natural_request_retrieval(
                    resolved_home,
                    question,
                    natural_scope,
                    registered_refs,
                    output_json=output_json,
                    authorize_remote_query=authorize_remote_query,
                    opencode_config=opencode_config,
                )
                return
            try:
                selected_project = resolve_question_project(resolved_home, question)
            except AskProjectUnresolved as exc:
                # WP-03.03: proje sinyali olan belirsiz/kayitsiz istek; sorgu
                # veya indeksleme baslatmadan kullanicidan netlestirme istenir.
                _ask_clarification(exc, output_json=output_json)
                raise AssertionError("unreachable: clarification exits") from exc
        else:
            selected_project = resolve_registered_project(resolved_home, project)
        config_paths = _config_paths_for_question(question)
        if config_paths:
            source_root = resolve_project_source(resolved_home, selected_project)
            structures = [
                parse_local_config_structure(source_root, path).as_dict() for path in config_paths
            ]
            result = {
                "schema": "zekam-ask-local-config/v1",
                "project_ref": selected_project,
                "route": "local-config-structure",
                "provider_calls": 0,
                "structures": structures,
            }
            if output_json:
                _print_json(result)
            else:
                console.print(
                    f"{selected_project}: {len(structures)} yerel config yapisi, "
                    f"{sum(len(item['keys']) for item in structures)} guvenli anahtar"
                )
                for index, structure in enumerate(structures, start=1):
                    console.print(
                        f"{index}. {structure['format']}: redakte={structure['redacted_key_count']}"
                    )
                    for key in structure["keys"][:100]:
                        console.print(f"  {key['line']}\t{key['name']}")
                    if len(structure["keys"]) > 100:
                        console.print("  daha fazla guvenli anahtar JSON ciktida")
            return
        retrieval = query_registered_project(
            resolved_home,
            selected_project,
            question,
            opencode_config=opencode_config,
            authorize_remote_query=authorize_remote_query,
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    result = {
        "schema": "zekam-ask-result/v1",
        "project_ref": selected_project,
        "retrieval": retrieval,
    }
    answer_retrieval = _single_natural_request_retrieval(
        selected_project,
        question,
        retrieval,
        authorize_remote_query=authorize_remote_query,
    )
    answer = answer_natural_request(answer_retrieval, _retrieval_explanation(answer_retrieval))
    result["answer"] = answer
    if output_json:
        _print_json(result)
    else:
        console.print(answer["answer"])


@app.command()
def doctor(
    output_json: Annotated[bool, typer.Option("--json", help="Raporu JSON olarak yazar")] = False,
    category: Annotated[
        list[str] | None,
        typer.Option("--category", "-c", help="Yalnizca verilen kategorileri calistirir"),
    ] = None,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
    repair_plan: Annotated[
        bool,
        typer.Option(
            "--repair-plan",
            help="Mutation yapmadan digest-bound Git/DB onarim planini yazar",
        ),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option(
            "--uygula",
            help="Exact --plan-digest ile siradaki tek onarim adimini uygular",
        ),
    ] = False,
    prepare: Annotated[
        bool,
        typer.Option(
            "--hazirla",
            help="Git sonrasi pending migration ve routine onarimlarini bounded uygular",
        ),
    ] = False,
    plan_digest: Annotated[
        str | None,
        typer.Option("--plan-digest", help="Uygulanacak exact doctor repair plan digest'i"),
    ] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    project_id: Annotated[
        UUID | None,
        typer.Option("--project-id", help="Exact Zekam source project UUID"),
    ] = None,
    actor_id: Annotated[
        UUID | None,
        typer.Option("--actor-id", help="Exact aktif human actor UUID"),
    ] = None,
) -> None:
    """Kurulum ve butunlugu raporlar; mutation yalniz explicit repair ile olur."""
    try:
        context = build_context(home=home)
        service = build_doctor(context)
        report = service.run(categories=category or None)
        local_services: LocalCoreServices | None = None
        selected_plan: dict[str, object] | None = None
        applied_result: dict[str, object] | None = None
        if prepare and (apply or plan_digest is not None):
            raise PolicyViolation("--hazirla, --uygula veya --plan-digest ile birlikte kullanilmaz")
        if repair_plan or apply or prepare:
            local_services = LocalCoreServices.from_context(
                context, read_only=not (apply or prepare)
            )
            selected_plan = local_services.repair_plan()
        automatic_results: list[dict[str, object]] = []
        if prepare:
            assert selected_plan is not None and local_services is not None
            if selected_plan["action"] is not None:
                automatic_results.append(
                    local_services.apply_repair(str(selected_plan["plan_digest"]))
                )
                report = service.run(categories=category or None)
        elif apply:
            assert local_services is not None
            if plan_digest is None:
                raise PolicyViolation("--uygula exact --plan-digest ister")
            applied_result = local_services.apply_repair(plan_digest)
            report = service.run(categories=category or None)
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc

    if output_json:
        document = report.as_dict()
        if selected_plan is not None:
            document["doctor_repair_plan"] = selected_plan
        if applied_result is not None:
            document["doctor_repair_result"] = applied_result
        if automatic_results:
            document["doctor_prepare_results"] = automatic_results
        _print_json(document)
    else:
        _render_report(report)
        if selected_plan is not None:
            _render_doctor_repair_plan(selected_plan)
        if applied_result is not None:
            console.print(
                "[green]Onarim dogrulandi:[/green] "
                f"{applied_result['step']} receipt={applied_result['receipt_id']}"
            )
        for item in automatic_results:
            console.print(
                "[green]Hazirlama adimi dogrulandi:[/green] "
                f"{item['step']} receipt={item['receipt_id']}"
            )
    raise typer.Exit(EXIT_CODES[report.overall])


@app.command()
def init(
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Degisiklik yapmadan planlanan yerlesimi yazar")
    ] = False,
    persistence: Annotated[
        PersistenceBackend | None,
        typer.Option(
            "--persistence",
            "--veritabani",
            help="Fresh bootstrap motoru: sqlite",
        ),
    ] = None,
) -> None:
    """ZEKAM_HOME v2'yi sifir verili ve atomik olarak kurar."""
    del persistence  # Kept as a backwards-compatible, SQLite-only CLI option.
    try:
        resolved_home = resolve_home(home)
        context = build_context(home=home)
        authority = ActiveTaskContract.load(context.core_path / "AKTIF_GOREV.md")
        bootstrap_plan = plan_fresh_bootstrap(
            home=resolved_home,
            core_root=context.core_path,
            authority_digest=authority.source_digest,
            schema=_OPERATIONAL_SCHEMA,
        )
        if dry_run:
            table = Table(title=f"{PRODUCT.data_root_env} plani: {context.home}")
            table.add_column("Dizin")
            table.add_column("Sahiplik")
            table.add_column("Durum")
            for entry in context.layout.entries():
                exists = (context.home / entry.relative).is_dir()
                table.add_row(
                    entry.relative,
                    entry.ownership.value,
                    "mevcut" if exists else "olusturulacak",
                )
            table.add_row(
                USER_CONFIG_FILE,
                "user-data",
                f"persistence=sqlite; action={bootstrap_plan.action}",
            )
            table.add_row("state/operational.db", "runtime", "schema-v1")
            table.add_row("bootstrap receipt", "artifact", bootstrap_plan.plan_digest)
            console.print(table)
            raise typer.Exit(0)
        receipt = apply_fresh_bootstrap(bootstrap_plan, schema=_OPERATIONAL_SCHEMA)
        LocalCoreServices.from_context(build_context(home=context.home)).bootstrap_extensions()
    except ZekamError as exc:
        error_console.print(f"[red]Hata:[/red] {exc}")
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    console.print(f"[green]Hazir:[/green] {context.home} receipt={receipt['receipt_digest']}")


@app.command()
def setup(
    apply: Annotated[
        bool,
        typer.Option(
            "--uygula",
            help="Digest-bound SQLite/local-core kurulum adimlarini gercekten uygular",
        ),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json", help="Sanitize plan JSON yazar")] = False,
    home: Annotated[str | None, typer.Option("--home", help=_HOME_HELP)] = None,
    plan_digest: Annotated[
        str | None,
        typer.Option("--plan-digest", help="Uygulanacak exact setup plan digest'i"),
    ] = None,
) -> None:
    """Yeni makine kurulumunu planlar; varsayilan davranis salt okunur dry-run'dir."""

    plan = build_setup_plan(home=resolve_home(home))
    exact_plan_digest = setup_plan_digest(plan)
    if output_json and not apply:
        plan_document = setup_plan_payload(plan)
        _print_json(
            {
                **plan_document,
                "apply": apply,
                "plan_digest": exact_plan_digest,
            }
        )
        return
    elif not apply:
        table = Table(title="Yeni makine kurulum plani")
        table.add_column("Adim")
        table.add_column("Aciklama")
        for step in plan:
            table.add_row(step.step_id, step.description)
        console.print(table)
        console.print(f"Plan digest: {exact_plan_digest}")
        console.print(
            "[yellow]Dry-run. Uygulamak icin --uygula ve exact --plan-digest verin.[/yellow]"
        )
        return

    if plan_digest != exact_plan_digest:
        error_console.print("[red]Hata:[/red] setup exact --plan-digest ister")
        raise typer.Exit(EXIT_USAGE_ERROR)

    cli_prefix = (sys.executable, "-m", "zekam.interfaces.cli.main")
    receipts: list[dict[str, object]] = []
    for step in plan:
        argv = step.argv if step.step_id == "windows-git-ca" else (*cli_prefix, *step.argv)
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=output_json,
            text=output_json,
        )
        receipts.append({"step_id": step.step_id, "returncode": completed.returncode})
        if completed.returncode != 0:
            if output_json:
                _print_json(
                    {
                        "schema": "zekam-setup-result/v1",
                        "status": "failed",
                        "acceptance_profile": "core-only",
                        "readiness": setup_readiness("failed"),
                        "plan_digest": exact_plan_digest,
                        "receipts": receipts,
                    }
                )
            else:
                error_console.print(f"[red]Kurulum durdu:[/red] {step.step_id}")
            raise typer.Exit(completed.returncode)
        if not output_json:
            console.print(f"[green]Tamam:[/green] {step.step_id}")
    if output_json:
        _print_json(
            {
                "schema": "zekam-setup-result/v1",
                "status": "completed",
                "acceptance_profile": "core-only",
                "readiness": setup_readiness("ready"),
                "plan_digest": exact_plan_digest,
                "receipts": receipts,
            }
        )


def _opencode_executable(context: ApplicationContext) -> Path | None:
    """Config kaydi veya PATH uzerinden OpenCode'u bulur."""

    for client in context.settings.clients:
        if client.name.casefold() == "opencode":
            return client.executable
    discovered = shutil.which("opencode")
    return Path(discovered).resolve() if discovered else None


_SEVERITY_STYLES: dict[Severity, str] = {
    Severity.INFO: "cyan",
    Severity.WARNING: "yellow",
    Severity.ERROR: "red",
    Severity.CRITICAL: "bold red",
}

_STATUS_STYLES: dict[OverallStatus, str] = {
    OverallStatus.HEALTHY: "green",
    OverallStatus.DEGRADED: "yellow",
    OverallStatus.BLOCKED: "red",
    OverallStatus.RECOVERY_REQUIRED: "bold red",
}


def _render_report(report: DoctorReport) -> None:
    table = Table(title=f"{PRODUCT.cli} doctor")
    table.add_column("Kontrol")
    table.add_column("Durum")
    table.add_column("Ozet")
    for result in report.results:
        table.add_row(result.check_id, result.status.value, result.summary)
    console.print(table)

    if report.findings:
        console.print()
        for finding in report.findings:
            style = _SEVERITY_STYLES[finding.severity]
            console.print(f"[{style}]{finding.severity.value}[/{style}] {finding.code}")
            console.print(f"  {finding.title}: {finding.detail}")
            console.print(f"  Sonraki adim: {finding.next_action}")
            if finding.authority_required:
                console.print("  Yetki gerekir: evet")

    style = _STATUS_STYLES[report.overall]
    console.print()
    console.print(f"Toplam durum: [{style}]{report.overall.value}[/{style}]")


def _render_doctor_repair_plan(document: dict[str, object]) -> None:
    table = Table(title="Doctor repair plani (yetki degildir)")
    table.add_column("Alan")
    table.add_column("Deger")
    table.add_row("Plan digest", str(document["plan_digest"]))
    table.add_row("Siradaki adim", str(document["action"] or "yok"))
    blocked = document["blocked_reasons"]
    assert isinstance(blocked, list)
    table.add_row("Bloke", ", ".join(str(item) for item in blocked) or "hayir")
    table.add_row(
        "Uygulanabilir",
        "evet" if document["action"] is not None and not document["blocked_reasons"] else "hayir",
    )
    console.print()
    console.print(table)
    if document["action"] is not None:
        console.print(
            "Uygulamak icin exact plan digest ile tekrar calistirin: "
            f"`{PRODUCT.cli} doctor --uygula --plan-digest {document['plan_digest']}`"
        )


def run() -> None:
    """Konsol giris noktasi."""
    try:
        app()
    except ZekamError as exc:  # pragma: no cover - beklenmeyen sizinti icin son savunma
        error_console.print(f"[red]Hata:[/red] {exc}")
        sys.exit(EXIT_RUNTIME_ERROR)


if __name__ == "__main__":  # pragma: no cover
    run()
