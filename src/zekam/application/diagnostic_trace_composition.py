"""Explicit config/SecretRef/authorization ile production trace sink composition."""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from zekam.application.config import DiagnosticTraceSettings
from zekam.application.diagnostic_trace import (
    AesGcmTraceCipher,
    BoundDiagnosticTraceSink,
    DiagnosticTraceRetentionService,
    DiagnosticTraceWriter,
    decode_trace_key,
)
from zekam.application.secret_broker import EnvironmentSecretStore, SecretBroker
from zekam.domain.diagnostic_trace import DiagnosticTracePolicy
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.security import SecretBackend
from zekam.infrastructure.postgres.diagnostic_trace_repository import (
    PostgresDiagnosticTraceRepository,
)
from zekam.infrastructure.postgres.security_repository import (
    AuthorizationRepository,
    SecretRefRepository,
)
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

TRACE_ID_ENV = "ZEKAM_DIAGNOSTIC_TRACE_ID"
TRACE_AUTHORIZATION_ENV = "ZEKAM_DIAGNOSTIC_TRACE_AUTHORIZATION_ID"
TRACE_PURGE_AUTHORIZATION_ENV = "ZEKAM_DIAGNOSTIC_TRACE_PURGE_AUTHORIZATION_ID"


def compose_diagnostic_trace_sink(
    *,
    connection: Any,
    realm_id: UUID,
    home: Path,
    settings: DiagnosticTraceSettings,
    environ: Mapping[str, str] | None = None,
) -> BoundDiagnosticTraceSink | None:
    """Disabled ise no-op; enabled ise exact bundle, SecretRef ve auth olmadan fail-closed."""
    if not settings.enabled:
        return None
    values = os.environ if environ is None else environ
    trace_id_raw = values.get(TRACE_ID_ENV, "").strip()
    authorization_raw = values.get(TRACE_AUTHORIZATION_ENV, "").strip()
    if not trace_id_raw or not authorization_raw:
        raise PolicyViolation(
            f"Diagnostic trace enabled iken {TRACE_ID_ENV} ve {TRACE_AUTHORIZATION_ENV} gerekir"
        )
    try:
        trace_id = UUID(trace_id_raw)
        authorization_id = UUID(authorization_raw)
    except ValueError as exc:
        raise ValidationFailed("Diagnostic trace environment kimligi UUID olmali") from exc
    policy = DiagnosticTracePolicy(
        enabled=True,
        retention_days=settings.retention_days,
        max_payload_bytes=settings.max_payload_bytes,
        max_events=settings.max_events,
        max_total_bytes=settings.max_total_bytes,
        encryption_key_ref=settings.encryption_key_ref,
        export_allowed=settings.export_allowed,
        redaction_profile=settings.redaction_profile,
    )
    repository = PostgresDiagnosticTraceRepository(connection, realm_id)
    bundle = repository.get_bundle(trace_id)
    if bundle.policy.policy_digest != policy.policy_digest or bundle.state != "open":
        raise PolicyViolation("Configured trace policy/open bundle exact binding mismatch")
    key_name = settings.encryption_key_ref or ""
    secret_ref = SecretRefRepository(connection, realm_id).current_by_name(key_name)
    if secret_ref is None or secret_ref.store_backend is not SecretBackend.ENVIRONMENT:
        raise PolicyViolation("Diagnostic trace current environment SecretRef ister")
    authorizations = AuthorizationRepository(connection, realm_id)
    authorization = authorizations.get(authorization_id)
    resource = f"diagnostics.trace:{trace_id}"
    if not authorization.scope.covers_effect("diagnostic-trace-encrypt") or not (
        authorization.scope.covers_resource(resource)
        and secret_ref.id in authorization.scope.secret_ref_ids
    ):
        raise PolicyViolation("Diagnostic trace writer authorization exact scope mismatch")
    broker = SecretBroker({SecretBackend.ENVIRONMENT: EnvironmentSecretStore(environ=values)})
    with broker.resolve(
        secret_ref,
        operation="diagnostic-trace-encrypt",
        authorization=authorization,
    ) as secret:
        consumed = authorizations.consume(
            authorization.id,
            effect_digest=authorization.effect_digest,
            consumed_by="zekam-diagnostic-trace-composition",
        )
        if not consumed.consumed:
            raise PolicyViolation("Diagnostic trace writer authorization tuketilemedi")
        key = decode_trace_key(secret.reveal())
    store = LocalContentAddressedStore(home / "global" / "diagnostic-traces").ensure()
    writer = DiagnosticTraceWriter(repository, store, AesGcmTraceCipher(os.urandom), lambda _: key)
    return BoundDiagnosticTraceSink(writer, bundle, policy)


def compose_diagnostic_trace_purge_handler(
    *,
    connection: Any,
    realm_id: UUID,
    home: Path,
    environ: Mapping[str, str] | None = None,
) -> Callable[[dt.datetime], str] | None:
    """Exact one-shot purge yetkisi varsa production scheduler handler'i kurar."""

    values = os.environ if environ is None else environ
    raw = values.get(TRACE_PURGE_AUTHORIZATION_ENV, "").strip()
    if not raw:
        return None
    try:
        authorization_id = UUID(raw)
    except ValueError as exc:
        raise ValidationFailed(f"{TRACE_PURGE_AUTHORIZATION_ENV} UUID olmali") from exc
    authorizations = AuthorizationRepository(connection, realm_id)
    authorization = authorizations.get(authorization_id)
    if not authorization.scope.covers_effect("diagnostic-trace-purge") or not (
        authorization.scope.covers_resource("diagnostics.trace-expired")
    ):
        raise PolicyViolation("Scheduled trace purge authorization exact scope mismatch")
    repository = PostgresDiagnosticTraceRepository(connection, realm_id)
    store = LocalContentAddressedStore(home / "global" / "diagnostic-traces").ensure()

    def purge(now: dt.datetime) -> str:
        # Worker dis katmanda hatayi incident'e cevirerek yakalar. Savepoint consume
        # kaydini kismi CAS hatasinda geri alir; CAS delete idempotent retry edilir.
        with connection.transaction():
            consumed = authorizations.consume(
                authorization.id,
                effect_digest=authorization.effect_digest,
                consumed_by="zekam-worker-diagnostic-trace-purge",
                now=now,
            )
            if not consumed.consumed:
                raise PolicyViolation("Scheduled trace purge authorization tuketilemedi")
            result = DiagnosticTraceRetentionService(repository, store).purge_expired(
                now=now,
                authorization_ref=str(authorization.id),
            )
        return (
            "diagnostic trace purge tamamlandi: "
            f"{len(result.purged_trace_ids)} trace, "
            f"{result.deleted_payload_count} payload silindi, "
            f"{result.missing_payload_count} payload zaten yoktu"
        )

    return purge
