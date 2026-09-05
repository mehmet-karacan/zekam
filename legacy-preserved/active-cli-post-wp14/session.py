"""CLI icin ortak realm oturumu ve hata kodu esleme.

Butun komutlar ayni baglanti/rol/realm kurulumunu kullanir; yuzey kendi kuralini
tanimlamaz.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import typer
from rich.console import Console
from typer._click.globals import get_current_context

from zekam.application.composition import build_context
from zekam.application.diagnostic_trace_composition import compose_diagnostic_trace_sink
from zekam.application.local_runtime_boundary import (
    AuthorizationRepository,
    MemoryContinuityRepository,
    PostgresRealmSessionOperations,
    connect,
)
from zekam.application.memory_continuity import MemoryContinuityService, MemoryContinuityStore
from zekam.application.mutation_admission import (
    CLI_MUTATION_REGISTRY_META_KEY,
    DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY,
    ActiveRuntimeContinuityIdentity,
    CliMutationAdmission,
    CliMutationAdmissionRegistry,
    CliMutationInvocationSnapshot,
    CliMutationTargetHints,
    assert_cli_mutation_admission,
)
from zekam.application.realm_context import RealmContext, attach_realm
from zekam.domain.errors import NotFound, PolicyViolation, ZekamError
from zekam.domain.identity import PRODUCT
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.sqlite.repository import SQLitePersistence

#: Kararli cikis kodlari.
EXIT_RUNTIME_ERROR = 70
EXIT_NOT_FOUND = 4
EXIT_AMBIGUOUS = 5
EXIT_POLICY_VIOLATION = 6

HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"
REALM_HELP = "Kullanilacak realm slug'i"

error_console = Console(stderr=True)


def _current_cli_invocation() -> CliMutationInvocationSnapshot:
    """Read the parsed leaf invocation; never infer authority from argv text."""

    current = get_current_context(silent=True)
    if current is None:
        # RealmSession outside a parsed Typer leaf has no trustworthy read/write
        # intent.  Treat it as mutation-capable so it cannot bypass hydration.
        return DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.snapshot(("realm-session",), {"apply": True})
    names: list[str] = []
    cursor: Any = current
    while cursor is not None and cursor.parent is not None:
        if cursor.info_name:
            names.append(str(cursor.info_name))
        cursor = cursor.parent
    path = tuple(reversed(names))
    if not path:
        return DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.snapshot(("realm-session",), {"apply": True})
    candidate: Any = current.find_root().meta.get(CLI_MUTATION_REGISTRY_META_KEY)
    registry = (
        candidate
        if isinstance(candidate, CliMutationAdmissionRegistry)
        else DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY
    )
    return registry.snapshot(path, current.params)


def _current_cli_admission() -> CliMutationAdmission:
    """Compatibility accessor for callers that need only admission metadata."""

    return _current_cli_invocation().admission


def _active_runtime_continuity_identity(
    connection: Any,
    *,
    realm_id: UUID,
    target_hints: CliMutationTargetHints,
) -> ActiveRuntimeContinuityIdentity:
    """Resolve exactly one live run; zero or ambiguity is an admission denial."""

    predicates: list[str] = []
    parameters: list[object] = [realm_id]
    if target_hints.project_ref is not None:
        predicates.append(
            " and (run.project_id::text=%s or 'project/'||run.project_id::text=%s"
            " or exists(select 1 from projects.project project"
            " where project.realm_id=run.realm_id and project.id=run.project_id"
            " and (project.slug=%s or exists(select 1 from projects.project_alias alias"
            " where alias.realm_id=project.realm_id and alias.project_id=project.id"
            " and (alias.alias=%s or alias.normalized=%s)))))"
        )
        parameters.extend([target_hints.project_ref] * 5)
    if target_hints.work_ref is not None:
        predicates.append(
            " and (run.work_item_id::text=%s or 'work/'||run.work_item_id::text=%s"
            " or exists(select 1 from work.work_item item"
            " where item.realm_id=run.realm_id and item.id=run.work_item_id"
            " and (item.external_number=%s or 'work-digest:'||item.record_digest=%s)))"
        )
        parameters.extend([target_hints.work_ref] * 4)
    if target_hints.run_ref is not None:
        predicates.append(" and (run.id::text=%s or 'run/'||run.id::text=%s)")
        parameters.extend([target_hints.run_ref] * 2)
    if target_hints.session_ref is not None:
        predicates.append(" and run.session_id=%s")
        parameters.append(target_hints.session_ref)
    if target_hints.client_ref is not None:
        predicates.append(" and run.client_id=%s")
        parameters.append(target_hints.client_ref)
    if target_hints.authorization_ref is not None:
        predicates.append(
            " and exists(select 1 from security.authorization authz"
            " where authz.realm_id=run.realm_id"
            " and (authz.id::text=%s"
            " or 'authorization/'||authz.id::text=%s)"
            " and authz.work_item_id=run.work_item_id"
            " and authz.plan_id=run.plan_id)"
        )
        parameters.extend([target_hints.authorization_ref] * 2)
    if target_hints.candidate_ref is not None:
        predicates.append(
            " and exists(select 1 from memory.compiler_candidate candidate"
            " join memory.compiler_run compiler_run"
            " on compiler_run.realm_id=candidate.realm_id"
            " and compiler_run.id=candidate.compiler_run_id"
            " where candidate.realm_id=run.realm_id"
            " and (candidate.id::text=%s or 'candidate/'||candidate.id::text=%s"
            " or candidate.logical_candidate_id=%s"
            " or 'candidate/'||candidate.logical_candidate_id=%s)"
            " and compiler_run.project_id=run.project_id"
            " and compiler_run.work_item_id=run.work_item_id"
            " and compiler_run.run_id=run.id and candidate.is_current=true)"
        )
        parameters.extend([target_hints.candidate_ref] * 4)
    if target_hints.trace_ref is not None:
        predicates.append(
            " and exists(select 1 from diagnostics.trace_bundle trace"
            " where trace.realm_id=run.realm_id"
            " and (trace.id::text=%s or 'trace/'||trace.id::text=%s"
            " or trace.trace_ref=%s)"
            " and trace.project_id=run.project_id"
            " and trace.work_item_id=run.work_item_id and trace.run_id=run.id)"
        )
        parameters.extend([target_hints.trace_ref] * 3)

    with connection.cursor() as cursor:
        cursor.execute(
            "select run.project_id,run.work_item_id,run.id,run.session_id,run.client_id"
            " from runtime.execution_run run"
            " where run.realm_id=%s and run.state='active'"
            " and run.deadline>clock_timestamp()"
            " and run.session_id is not null and btrim(run.session_id)<>''"
            " and btrim(run.client_id)<>''"
            + "".join(predicates)
            + " order by run.created_at desc,run.id desc limit 2",
            tuple(parameters),
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise PolicyViolation(
            "CLI mutating admission realm icinde exact bir aktif runtime execution "
            f"kimligi ister; bulunan={len(rows)}"
        )
    row = rows[0]
    return ActiveRuntimeContinuityIdentity(
        realm_id=realm_id,
        project_id=UUID(str(row[0])),
        work_item_id=UUID(str(row[1])),
        run_id=UUID(str(row[2])),
        session_id=str(row[3]),
        client_id=str(row[4]),
    )


def _assert_existing_hydration(
    connection: Any,
    *,
    realm_id: UUID,
    admission: CliMutationAdmission,
) -> ActiveRuntimeContinuityIdentity | None:
    """Enforce the real Memory Continuity admission for non-exempt mutations."""

    if not admission.requires_existing_hydration:
        return None
    identity = _active_runtime_continuity_identity(
        connection,
        realm_id=realm_id,
        target_hints=admission.target_hints,
    )
    MemoryContinuityService(
        cast(MemoryContinuityStore, MemoryContinuityRepository(connection, realm_id)),
        AuthorizationRepository(connection, realm_id),
    ).assert_mutating_admission(
        project_id=identity.project_id,
        work_item_id=identity.work_item_id,
        run_id=identity.run_id,
        session_id=identity.session_id,
        client_id=identity.client_id,
    )
    return identity


def assert_cli_invocation_backend(
    home: str | None,
    invocation: CliMutationInvocationSnapshot,
) -> None:
    """Run the common backend gate without opening a database connection."""

    backend = build_context(home=home).settings.database.backend
    assert_cli_mutation_admission(
        backend=backend.value,
        supports_full_continuity=True,
        admission=invocation.admission,
        realm_session_required=True,
    )


class RealmSession:
    """Realm kapsamli, uygulama rolu altinda calisan CLI oturumu.

    Baglanti context manager'i acikca tutulur; aksi halde uretici nesne toplanir
    ve baglanti beklenmedik sekilde kapanir.
    """

    def __init__(
        self,
        home: str | None,
        realm: str,
        *,
        create_realm: bool = False,
        enable_runtime_trace: bool = False,
        invocation: CliMutationInvocationSnapshot | None = None,
    ) -> None:
        self._context = build_context(home=home)
        self._realm_slug = realm
        self._create_realm = create_realm
        self._enable_runtime_trace = enable_runtime_trace
        self._invocation = invocation
        self._resolved_runtime_identity: ActiveRuntimeContinuityIdentity | None = None
        self._stack = ExitStack()

    @property
    def resolved_runtime_identity(self) -> ActiveRuntimeContinuityIdentity | None:
        return self._resolved_runtime_identity

    def __enter__(self) -> RealmContext:
        backend = self._context.settings.database.backend
        admission = (
            self._invocation.admission if self._invocation is not None else _current_cli_admission()
        )
        assert_cli_mutation_admission(
            backend=backend.value,
            supports_full_continuity=True,
            admission=admission,
            realm_session_required=True,
        )
        try:
            connection = self._stack.enter_context(connect(self._context.settings.database))
            realm_context = attach_realm(
                connection,
                PostgresRealmSessionOperations(),
                slug=self._realm_slug,
                create_if_missing=self._create_realm,
            )
            self._resolved_runtime_identity = _assert_existing_hydration(
                connection,
                realm_id=realm_context.realm_id,
                admission=admission,
            )
            trace_sink = None
            if self._enable_runtime_trace:
                trace_sink = compose_diagnostic_trace_sink(
                    connection=connection,
                    realm_id=realm_context.realm_id,
                    home=self._context.home,
                    settings=self._context.settings.diagnostic_trace,
                )
            return (
                replace(realm_context, trace_sink=trace_sink)
                if trace_sink is not None
                else realm_context
            )
        except BaseException as exc:
            # ``__enter__`` failures do not trigger this object's ``__exit__``.
            # Forward the real exception so the connection context rolls back.
            self._stack.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Exception bilgisini connection context'ine iletmek transaction rollback'i icin
        # zorunludur. ``close()`` her cikisi basarili gibi gosterip kismi commit uretebilir.
        self._stack.__exit__(exc_type, exc, traceback)


def sqlite_repository(home: str | None, realm: str) -> SQLitePersistence | None:
    """Return the local minimum repository."""
    context = build_context(home=home)
    if realm != DEFAULT_REALM_SLUG:
        raise ZekamError("SQLite minimum profili yalniz varsayilan realm'i destekler")
    return SQLitePersistence(context.settings.database.sqlite_path(context.home))


def sqlite_operational_store(home: str | None, realm: str) -> SQLiteOperationalStore | None:
    """Return the fresh operational authority for the local SQLite profile."""
    context = build_context(home=home)
    if realm != DEFAULT_REALM_SLUG:
        raise ZekamError("Yerel operational store yalniz varsayilan realm'i destekler")
    return SQLiteOperationalStore(context.settings.database.sqlite_path(context.home))


def fail(message: str, code: int = EXIT_RUNTIME_ERROR) -> typer.Exit:
    """Sanitize edilmis hata yazar ve kararli cikis kodu uretir."""
    error_console.print(f"[red]Hata:[/red] {message}")
    return typer.Exit(code)


def fail_from(exc: ZekamError) -> typer.Exit:
    """Hata turune gore kararli cikis kodu uretir."""
    from zekam.domain.errors import PolicyViolation

    if isinstance(exc, NotFound):
        code = EXIT_NOT_FOUND
    elif isinstance(exc, PolicyViolation):
        code = EXIT_POLICY_VIOLATION
    else:
        code = EXIT_RUNTIME_ERROR
    return fail(str(exc), code)
