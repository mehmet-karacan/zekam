"""Read-only local continuity diagnostics; client hook activation is separate."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console

from zekam.application.composition import build_context
from zekam.application.config import PersistenceBackend
from zekam.application.local_continuity_close import CloseCandidateBundle, CloseSummary
from zekam.application.local_continuity_source_authority import (
    MAX_COMMAND_BYTES,
    PortableSourcePlanRecord,
)
from zekam.application.local_continuity_source_plan import ContinuitySourceRecipe
from zekam.application.local_continuity_startup import StartupRequest
from zekam.application.mutation_admission import (
    _issue_gate_a_source_capability,
    assert_local_effect_admission,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed, ZekamError
from zekam.infrastructure.local_continuity_source_plan import (
    BoundedContinuitySource,
)
from zekam.infrastructure.process_identity import process_incarnation_token
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_source_authority import (
    SQLiteLocalSourceAuthority,
)

if TYPE_CHECKING:
    from zekam.infrastructure.local_continuity_composition import LocalContinuityRuntime

app = typer.Typer(
    name="continuity", help="Yerel oturum ve checkpoint kanitlari", no_args_is_help=True
)
local_app = typer.Typer(
    name="local",
    help="Onceden kabul edilmis yerel oturumun bounded lifecycle islemleri",
    no_args_is_help=True,
)
app.add_typer(local_app)


@dataclass(frozen=True, slots=True)
class _LocalOptions:
    home: Path
    session_id: str
    source_root: Path
    source_paths: tuple[str, ...]
    index_path: Path | None


@dataclass(frozen=True, slots=True)
class _JSONDocument:
    body: dict[str, Any]
    identity: tuple[int, int, int, int]


@local_app.callback()
def local_options(
    ctx: typer.Context,
    home: Annotated[Path, typer.Option("--home")],
    session_id: Annotated[str, typer.Option("--session-id")],
    source_root: Annotated[Path, typer.Option("--source-root")],
    source_file: Annotated[list[str], typer.Option("--source-file")],
    index: Annotated[Path | None, typer.Option("--index")] = None,
) -> None:
    """Yalniz mevcut state; bootstrap, yeni binding veya hook aktivasyonu yapmaz."""
    ctx.obj = _LocalOptions(home, session_id, source_root, tuple(source_file), index)


def _runtime(ctx: typer.Context) -> LocalContinuityRuntime:
    # The lightweight observation hook and DB-only inspect do not load this composition.
    from zekam.infrastructure.local_continuity_composition import (
        LocalContinuityArguments,
        LocalContinuityRuntime,
    )

    options = ctx.obj
    if not isinstance(options, _LocalOptions):
        raise ValidationFailed("Local continuity explicit arguments required")
    return LocalContinuityRuntime(
        LocalContinuityArguments(
            home=options.home,
            session_id=options.session_id,
            source_root=options.source_root,
            source_paths=options.source_paths,
            index_path=options.index_path,
        )
    )


def _execute(
    ctx: typer.Context,
    command: str,
    action: Callable[[LocalContinuityRuntime], dict[str, Any]],
    *,
    mutating: bool,
) -> None:
    try:
        if mutating:
            assert_local_effect_admission(("continuity", "local", command))
        document = action(_runtime(ctx))
        Console().print_json(json.dumps(document, allow_nan=False))
    except (ZekamError, OSError, sqlite3.Error, ValueError, TypeError, RecursionError) as exc:
        # Even a typed downstream exception may contain caller input. Never echo it.
        if isinstance(exc, PolicyViolation):
            code, message = "policy-violation", "Local continuity policy or evidence rejected"
        elif isinstance(exc, (ValidationFailed, ValueError, TypeError, RecursionError)):
            code, message = "validation-failed", "Local continuity bounded input rejected"
        elif isinstance(exc, ZekamError):
            code, message = "zekam-error", "Local continuity state requires attention"
        else:
            code, message = "io-error", "Local continuity input or evidence unavailable"
        Console(stderr=True).print_json(json.dumps({"error": code, "message": message}))
        raise typer.Exit(70) from None


def _observed_at(value: str) -> dt.datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValidationFailed("Explicit timezone-aware ISO observation time required")
    try:
        result = dt.datetime.fromisoformat(value)
    except ValueError:
        raise ValidationFailed("Explicit timezone-aware ISO observation time required") from None
    if result.tzinfo is None:
        raise ValidationFailed("Explicit timezone-aware ISO observation time required")
    return result


def _unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationFailed("Local close duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValidationFailed("Local close nonfinite JSON value rejected")


def _json_document(path: Path) -> _JSONDocument:
    # Read a single bounded regular-file buffer; no automatic repair or path writes.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= 32768:
            raise ValidationFailed("Local close bounded regular file required")
        payload = stream.read(32769)
        after = os.fstat(stream.fileno())
    if not 1 <= len(payload) <= 32768:
        raise ValidationFailed("Local close JSON byte bound exceeded")
    if (
        (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_size)
        != (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_size)
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or len(payload) != after.st_size
    ):
        raise PolicyViolation("Local close JSON changed while captured")
    body = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_json,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(body, dict):
        raise ValidationFailed("Local close JSON object required")
    return _JSONDocument(
        body,
        (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode), after.st_uid),
    )


def _summary(path: Path) -> CloseSummary:
    return CloseSummary.from_body(_json_document(path).body)


def _source_authority_execute(
    *,
    home: Path,
    project_id: str,
    source_binding_id: str,
    source_snapshot_id: str,
    device_id: str,
    source_root: Path,
    source_files: tuple[str, ...],
    previous_revision: str | None,
    rebind: bool,
    confirmed: bool,
) -> None:
    try:
        command = ("continuity", "source-rebind" if rebind else "source-bind")
        capability = _issue_gate_a_source_capability(command, confirmed=confirmed)
        inputs = (
            project_id,
            source_binding_id,
            source_snapshot_id,
            device_id,
            str(source_root),
            *source_files,
        )
        if sum(len(value.encode("utf-8")) for value in inputs) > MAX_COMMAND_BYTES:
            raise ValidationFailed("Source authority command byte bound exceeded")
        if not source_root.is_absolute() or ".." in source_root.parts:
            raise ValidationFailed("Source authority exact absolute root required")
        context = build_context(home=home)
        if context.settings.database.backend is not PersistenceBackend.SQLITE:
            raise PolicyViolation("Source authority requires local operational SQLite")
        database = context.settings.database.sqlite_path(context.home)
        with closing(
            sqlite3.connect(f"{database.as_uri()}?mode=ro&nofollow=1", uri=True, timeout=0.125)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("pragma query_only=on")
            db.execute("begin")
            rows = db.execute(
                "select p.id as project_id,b.id as binding_id,r.realm_id,s.id as snapshot_id,"
                "s.revision_ref,s.tree_digest,s.content_digest,s.config_digest,"
                "c.task_digest,c.config_digest as policy_digest "
                "from project p join source_binding b on b.project_id=p.id "
                "join source_snapshot s on s.source_binding_id=b.id "
                "join project_knowledge_realm r on r.project_id=p.id "
                "join config_revision c on c.active=1 "
                "where p.id=? and b.id=? and s.id=? and p.status='active' "
                "and b.active=1 and b.source_kind='git' limit 2",
                (project_id, source_binding_id, source_snapshot_id),
            ).fetchall()
            latest = db.execute(
                "select id from source_snapshot where source_binding_id=? "
                "order by captured_at desc,id desc limit 1",
                (source_binding_id,),
            ).fetchone()
        if len(rows) != 1 or latest is None or latest[0] != source_snapshot_id:
            raise PolicyViolation("Source authority operational binding rejected")
        row = rows[0]
        recipe = ContinuitySourceRecipe(
            project_id,
            str(row["realm_id"]),
            source_binding_id,
            source_files,
            str(row["task_digest"]),
            str(row["policy_digest"]),
        )
        source = BoundedContinuitySource(source_root, recipe)
        plan = source.capture()
        if (
            plan.revision_ref != row["revision_ref"]
            or plan.tree_digest != row["tree_digest"]
            or plan.content_digest != row["content_digest"]
            or plan.config_digest != row["config_digest"]
        ):
            raise PolicyViolation("Source authority plan differs from operational snapshot")
        record = PortableSourcePlanRecord(source_snapshot_id, plan)
        result = SQLiteLocalSourceAuthority(context.home, database).execute(
            capability=capability,
            record=record,
            source=source,
            device_id=device_id,
            root=source_root,
            previous_revision_digest=previous_revision,
            rebind=rebind,
        )
        Console().print_json(json.dumps(result.body(), allow_nan=False))
    except (ZekamError, OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
        Console(stderr=True).print_json(
            json.dumps(
                {"error": "source-authority-rejected", "message": "Source evidence rejected"}
            )
        )
        raise typer.Exit(70) from None


def _source_authority_options(
    *,
    home: Path,
    project_id: str,
    source_binding_id: str,
    source_snapshot_id: str,
    device_id: str,
    source_root: Path,
    source_file: list[str],
    onayliyorum: bool,
    previous_revision: str | None,
    rebind: bool,
) -> None:
    _source_authority_execute(
        home=home,
        project_id=project_id,
        source_binding_id=source_binding_id,
        source_snapshot_id=source_snapshot_id,
        device_id=device_id,
        source_root=source_root,
        source_files=tuple(source_file),
        previous_revision=previous_revision,
        rebind=rebind,
        confirmed=onayliyorum,
    )


@app.command("source-bind")
def source_bind(
    home: Annotated[Path, typer.Option("--home")],
    project_id: Annotated[str, typer.Option("--project-id")],
    source_binding_id: Annotated[str, typer.Option("--source-binding-id")],
    source_snapshot_id: Annotated[str, typer.Option("--source-snapshot-id")],
    device_id: Annotated[str, typer.Option("--device-id")],
    source_root: Annotated[Path, typer.Option("--source-root")],
    source_file: Annotated[list[str], typer.Option("--source-file")],
    onayliyorum: Annotated[bool, typer.Option("--onayliyorum")] = False,
) -> None:
    _source_authority_options(
        home=home,
        project_id=project_id,
        source_binding_id=source_binding_id,
        source_snapshot_id=source_snapshot_id,
        device_id=device_id,
        source_root=source_root,
        source_file=source_file,
        onayliyorum=onayliyorum,
        previous_revision=None,
        rebind=False,
    )


@app.command("source-rebind")
def source_rebind(
    home: Annotated[Path, typer.Option("--home")],
    project_id: Annotated[str, typer.Option("--project-id")],
    source_binding_id: Annotated[str, typer.Option("--source-binding-id")],
    source_snapshot_id: Annotated[str, typer.Option("--source-snapshot-id")],
    device_id: Annotated[str, typer.Option("--device-id")],
    source_root: Annotated[Path, typer.Option("--source-root")],
    source_file: Annotated[list[str], typer.Option("--source-file")],
    previous_revision: Annotated[str, typer.Option("--previous-revision")],
    onayliyorum: Annotated[bool, typer.Option("--onayliyorum")] = False,
) -> None:
    _source_authority_options(
        home=home,
        project_id=project_id,
        source_binding_id=source_binding_id,
        source_snapshot_id=source_snapshot_id,
        device_id=device_id,
        source_root=source_root,
        source_file=source_file,
        onayliyorum=onayliyorum,
        previous_revision=previous_revision,
        rebind=True,
    )


@local_app.command("doctor")
def local_doctor(ctx: typer.Context) -> None:
    """Mevcut source/spool/DB kanitlarini raporlar; repair veya ACK yazmaz."""
    _execute(ctx, "doctor", lambda runtime: runtime.doctor(), mutating=False)


@local_app.command("drain")
def local_drain(ctx: typer.Context) -> None:
    """Yalniz gercek durable spool olaylarini mevcut oturuma aktarir."""
    _execute(ctx, "drain", lambda runtime: runtime.drain(), mutating=True)


@local_app.command("hydrate")
def local_hydrate(
    ctx: typer.Context,
    source_ref: Annotated[list[str], typer.Option("--source-ref")],
    token_budget: Annotated[int, typer.Option("--token-budget")],
    key: Annotated[str, typer.Option("--key")],
    observed_at: Annotated[str, typer.Option("--observed-at")],
    query: Annotated[str | None, typer.Option("--query")] = None,
    note_limit: Annotated[int, typer.Option("--note-limit")] = 0,
) -> None:
    """Reviewed SessionStart sonrasi bounded context/receipt; native ACK degildir."""
    _execute(
        ctx,
        "hydrate",
        lambda runtime: runtime.hydrate(
            StartupRequest(
                tuple(source_ref), token_budget, key, _observed_at(observed_at), note_limit, query
            )
        ),
        mutating=True,
    )


@local_app.command("checkpoint")
def local_checkpoint(
    ctx: typer.Context,
    context_digest: Annotated[str, typer.Option("--context-digest")],
    key: Annotated[str, typer.Option("--key")],
) -> None:
    """Gercek PRE_COMPACTION sinirini checkpoint eder; hook olayi icat etmez."""
    _execute(
        ctx, "checkpoint", lambda runtime: runtime.checkpoint(context_digest, key), mutating=True
    )


@local_app.command("resume")
def local_resume(
    ctx: typer.Context,
    checkpoint_digest: Annotated[str, typer.Option("--checkpoint-digest")],
) -> None:
    """Exact checkpoint kanitini yeniden dogrular; yetki miras almaz."""
    _execute(ctx, "resume", lambda runtime: runtime.resume(checkpoint_digest), mutating=False)


@local_app.command("freeze")
def local_freeze(
    ctx: typer.Context,
    summary: Annotated[Path, typer.Option("--summary")],
    context_digest: Annotated[str, typer.Option("--context-digest")],
    key: Annotated[str, typer.Option("--key")],
) -> None:
    """Bounded summary ile gercek PRE_CLOSE sinirini dondurur; complete iddia etmez."""
    _execute(
        ctx,
        "freeze",
        lambda runtime: runtime.freeze(_summary(summary), context_digest, key),
        mutating=True,
    )


@local_app.command("freeze-v2")
def local_freeze_v2(
    ctx: typer.Context,
    summary: Annotated[Path, typer.Option("--summary")],
    candidates_file: Annotated[Path, typer.Option("--candidates-file")],
    context_digest: Annotated[str, typer.Option("--context-digest")],
    key: Annotated[str, typer.Option("--key")],
) -> None:
    """Explicit reviewed v2 candidate bundle; v1 freeze remains unchanged."""

    def action(runtime: LocalContinuityRuntime) -> dict[str, Any]:
        summary_document = _json_document(summary)
        candidate_document = _json_document(candidates_file)
        if summary_document.identity == candidate_document.identity:
            raise PolicyViolation("Close summary and candidate bundle require distinct files")
        return runtime.freeze_v2(
            CloseSummary.from_body(summary_document.body),
            CloseCandidateBundle.from_body(candidate_document.body),
            context_digest,
            key,
        )

    _execute(ctx, "freeze-v2", action, mutating=True)


@local_app.command("close-tick")
def local_close_tick(
    ctx: typer.Context,
    request_digest: Annotated[str, typer.Option("--request-digest")],
    phase: Annotated[str, typer.Option("--phase")],
    owner_id: Annotated[str, typer.Option("--owner-id")] = "zekam-continuity-worker",
    repair_key: Annotated[str | None, typer.Option("--repair-key")] = None,
) -> None:
    """Exact request icin tek worker asamasi; PID/token bu surecten alinir."""

    def action(runtime: LocalContinuityRuntime) -> dict[str, Any]:
        if phase not in {"compile", "deliver", "finalize", "repair", "reconcile-delivery"}:
            raise ValidationFailed("Local close phase outside exact supported set")
        if (phase == "repair") != (repair_key is not None):
            raise ValidationFailed("Local close repair phase requires exclusive repair key")
        pid = os.getpid()
        token = process_incarnation_token(pid)
        if token is None:
            raise PolicyViolation("Current process incarnation unavailable")
        return runtime.close_tick(
            request_digest,
            phase,
            owner_id,
            pid,
            token,
            repair_key=repair_key,
        )

    _execute(ctx, "close-tick", action, mutating=True)


@app.command("inspect")
def inspect_command(
    session_id: Annotated[str, typer.Argument()],
    home: Annotated[str | None, typer.Option("--home")] = None,
    database: Annotated[Path | None, typer.Option("--database")] = None,
) -> None:
    """DB kanitlarini salt okunur inceler; dosya kaynagini veya hook aktivasyonunu onaylamaz."""
    try:
        if database is None:
            context = build_context(home=home)
            if context.settings.database.backend is not PersistenceBackend.SQLITE:
                raise PolicyViolation("Continuity requires local operational SQLite")
            database = context.settings.database.sqlite_path(context.home)
        elif home is not None:
            raise PolicyViolation("Choose one explicit home or database, not both")
        store = SQLiteContinuityStore(database)
        report = store.inspect(store.get_binding(session_id))
        report |= {
            "verification_scope": "operational-db-only",
            "filesystem_source_verified": False,
            "hook_activation_verified": False,
            "global_acceptance": False,
        }
        Console().print_json(json.dumps(report))
    except ZekamError as exc:
        Console(stderr=True).print(f"Continuity error: {exc}")
        raise typer.Exit(70) from exc
