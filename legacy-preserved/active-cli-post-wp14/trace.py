"""Diagnostic Trace Plane CLI; raw trace varsayilan kapali ve authoritysizdir."""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Annotated, Any
from uuid import UUID, uuid4

import typer
from rich.console import Console

from zekam.application.diagnostic_trace import (
    AesGcmTraceCipher,
    DiagnosticTraceReducer,
    DiagnosticTraceRetentionService,
    decode_trace_key,
)
from zekam.application.home import resolve_home
from zekam.application.local_runtime_boundary import (
    AuthorizationRepository,
    PostgresDiagnosticTraceRepository,
    SecretRefRepository,
)
from zekam.application.secret_broker import EnvironmentSecretStore, SecretBroker
from zekam.domain.diagnostic_trace import DiagnosticTracePolicy, TraceBundle
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.security import SecretBackend
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail_from

app = typer.Typer(name="trace", help="Encrypted diagnostic trace islemleri", no_args_is_help=True)
console = Console()


def _print(document: dict[str, Any], as_json: bool) -> None:
    if as_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    else:
        for key, value in document.items():
            console.print(f"{key}: {value}")


@app.command("start")
def start_trace(
    trace_ref: Annotated[str, typer.Option("--trace-ref", help="Portable trace kimligi")],
    client_session: Annotated[str, typer.Option("--client-session")],
    encryption_key_ref: Annotated[str, typer.Option("--encryption-key-ref")],
    apply: Annotated[bool, typer.Option("--apply", help="Exact plani uygular")] = False,
    retention_days: Annotated[int, typer.Option("--retention-days")] = 7,
    project_id: Annotated[UUID | None, typer.Option("--project-id")] = None,
    work_item_id: Annotated[UUID | None, typer.Option("--work-item-id")] = None,
    run_id: Annotated[UUID | None, typer.Option("--run-id")] = None,
    root_assignment_id: Annotated[UUID | None, typer.Option("--root-assignment-id")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Explicit opt-in trace manifestini planlar veya olusturur; payload yazmaz."""
    policy = DiagnosticTracePolicy(
        enabled=True,
        retention_days=retention_days,
        encryption_key_ref=encryption_key_ref,
    )
    plan = {
        "schema": "zekam-trace-start-plan/v1",
        "trace_ref": trace_ref,
        "client_session": client_session,
        "retention_days": retention_days,
        "encryption_key_ref": encryption_key_ref,
        "apply": apply,
        "grants_authority": False,
    }
    if not apply:
        _print(plan | {"status": "preview", "next_action": "--apply gerekir"}, as_json)
        return
    try:
        now = dt.datetime.now(dt.UTC)
        realm_session = RealmSession(home, realm)
        with realm_session as context:
            identity = realm_session.resolved_runtime_identity
            if identity is None:
                raise PolicyViolation("Trace start exact resolved runtime kimligi ister")
            bundle = TraceBundle(
                id=uuid4(),
                realm_id=context.realm_id,
                trace_ref=trace_ref,
                project_id=identity.project_id,
                work_item_id=identity.work_item_id,
                run_id=identity.run_id,
                root_assignment_id=root_assignment_id,
                root_client_session_id=identity.session_id,
                policy=policy,
                created_at=now,
                expires_at=now + dt.timedelta(days=retention_days),
            )
            PostgresDiagnosticTraceRepository(context.connection, context.realm_id).create_bundle(
                bundle
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _print(
        plan
        | {
            "status": "open",
            "trace_id": str(bundle.id),
            "manifest_digest": bundle.manifest_digest,
        },
        as_json,
    )


@app.command("stop")
def stop_trace(
    trace_id: Annotated[UUID, typer.Argument()],
    apply: Annotated[bool, typer.Option("--apply")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Trace'i kapatir; apply olmadan yalniz plan gosterir."""
    if not apply:
        _print(
            {"trace_id": str(trace_id), "status": "preview", "next_action": "--apply gerekir"},
            as_json,
        )
        return
    try:
        with RealmSession(home, realm) as context:
            PostgresDiagnosticTraceRepository(context.connection, context.realm_id).close(trace_id)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _print({"trace_id": str(trace_id), "status": "closed"}, as_json)


@app.command("explain")
def explain_trace(
    trace_id: Annotated[UUID, typer.Argument()],
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Raw payload acmadan manifest, usage ve reduction digestlerini okur."""
    try:
        with RealmSession(home, realm) as context:
            repository = PostgresDiagnosticTraceRepository(context.connection, context.realm_id)
            bundle = repository.get_bundle(trace_id)
            event_count, total_bytes = repository.usage(trace_id)
            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select output_digest,state from diagnostics.reduction"
                    " where realm_id=%s and trace_id=%s order by created_at",
                    (context.realm_id, trace_id),
                )
                reductions = [
                    {"output_digest": str(row[0]), "state": str(row[1])}
                    for row in cursor.fetchall()
                ]
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _print(
        {
            "schema": "zekam-trace-explain/v1",
            "trace_id": str(trace_id),
            "state": bundle.state,
            "manifest_digest": bundle.manifest_digest,
            "event_count": event_count,
            "ciphertext_bytes": total_bytes,
            "reductions": reductions,
            "raw_payload_exposed": False,
            "grants_authority": False,
        },
        as_json,
    )


@app.command("reduce")
def reduce_trace(
    trace_id: Annotated[UUID, typer.Argument()],
    secret_ref_id: Annotated[UUID, typer.Option("--secret-ref-id")],
    authorization_id: Annotated[UUID, typer.Option("--authorization-id")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Closed bundle'i exact SecretRef ve one-shot authorization ile offline reduce eder."""
    try:
        with RealmSession(home, realm) as context:
            repository = PostgresDiagnosticTraceRepository(context.connection, context.realm_id)
            bundle = repository.get_bundle(trace_id)
            secret_ref = SecretRefRepository(context.connection, context.realm_id).get(
                secret_ref_id
            )
            authorization_repository = AuthorizationRepository(context.connection, context.realm_id)
            authorization = authorization_repository.get(authorization_id)
            resource = f"diagnostics.trace:{trace_id}"
            if not authorization.scope.covers_effect("diagnostic-trace-decrypt") or not (
                authorization.scope.covers_resource(resource)
            ):
                raise PolicyViolation("Trace reduction authorization exact scope mismatch")
            store = LocalContentAddressedStore(
                resolve_home(home) / "global" / "diagnostic-traces"
            ).ensure()
            reducer = DiagnosticTraceReducer(
                repository,
                store,
                AesGcmTraceCipher(os.urandom),
                lambda _: key,
            )
            prepared = reducer.preflight(bundle, authorization_ref=str(authorization.id))
            broker = SecretBroker({SecretBackend.ENVIRONMENT: EnvironmentSecretStore()})
            with broker.resolve(
                secret_ref,
                operation="diagnostic-trace-decrypt",
                authorization=authorization,
            ) as secret:
                consumed = authorization_repository.consume(
                    authorization.id,
                    effect_digest=authorization.effect_digest,
                    consumed_by="zekam-trace-reducer",
                )
                if not consumed.consumed:
                    raise PolicyViolation("Trace reduction authorization tuketilemedi")
                key = decode_trace_key(secret.reveal())
                reduced = reducer.reduce(
                    bundle,
                    reduced_at=dt.datetime.now(dt.UTC),
                    authorization_ref=str(authorization.id),
                    prepared=prepared,
                )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _print(
        {
            "trace_id": str(trace_id),
            "status": "completed",
            "output_digest": reduced.output_digest,
            "event_count": reduced.event_count,
            "grants_authority": False,
        },
        as_json,
    )


@app.command("purge-expired")
def purge_expired_traces(
    apply: Annotated[bool, typer.Option("--apply")] = False,
    authorization_id: Annotated[UUID | None, typer.Option("--authorization-id")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Expired trace ciphertext'lerini exact authorization ile geri alinmaz siler."""
    now = dt.datetime.now(dt.UTC)
    try:
        with RealmSession(home, realm) as context:
            repository = PostgresDiagnosticTraceRepository(context.connection, context.realm_id)
            candidates = repository.expired_candidates(now=now, limit=limit)
            preview = {
                "schema": "zekam-trace-purge-plan/v1",
                "candidate_count": len(candidates),
                "trace_ids": [str(item.bundle_id) for item in candidates],
                "payload_count": sum(len(item.payload_refs) for item in candidates),
                "destructive": True,
                "grants_authority": False,
            }
            if not apply:
                _print(preview | {"status": "preview", "next_action": "--apply gerekir"}, as_json)
                return
            if authorization_id is None:
                raise PolicyViolation("Trace purge --authorization-id ister")
            authorizations = AuthorizationRepository(context.connection, context.realm_id)
            authorization = authorizations.get(authorization_id)
            if not authorization.scope.covers_effect("diagnostic-trace-purge") or not (
                authorization.scope.covers_resource("diagnostics.trace-expired")
            ):
                raise PolicyViolation("Trace purge authorization exact scope mismatch")
            consumed = authorizations.consume(
                authorization.id,
                effect_digest=authorization.effect_digest,
                consumed_by="zekam-trace-retention",
            )
            if not consumed.consumed:
                raise PolicyViolation("Trace purge authorization tuketilemedi")
            store = LocalContentAddressedStore(
                resolve_home(home) / "global" / "diagnostic-traces"
            ).ensure()
            result = DiagnosticTraceRetentionService(repository, store).purge_expired(
                now=now,
                authorization_ref=str(authorization.id),
                limit=limit,
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    _print(
        preview
        | {
            "status": "completed",
            "purged_trace_ids": [str(item) for item in result.purged_trace_ids],
            "deleted_payload_count": result.deleted_payload_count,
            "missing_payload_count": result.missing_payload_count,
            "authorization_id": str(authorization_id),
        },
        as_json,
    )
