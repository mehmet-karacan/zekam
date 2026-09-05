"""Governance icin PostgreSQL adapterleri.

Kritik nokta: yetki tuketimi **tek bir atomik UPDATE** ile yapilir. Iki surec
ayni yetkiyi ayni anda tuketmeye calisirsa yalnizca biri satiri gunceller;
digeri bos sonuc alir ve reddedilir.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.policy import Capability, CapabilityKind, PolicyDocument, PolicyRule, RiskLevel
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    AuthorizationState,
    DataClassification,
    OutboundRequest,
    OutboundState,
    SecretBackend,
    SecretRef,
    SecretStatus,
)
from zekam.domain.work import EffectKind


@dataclass(frozen=True, slots=True)
class PolicyRepository:
    """Surumlu policy belgeleri."""

    connection: Any
    realm_id: UUID

    def append(self, document: PolicyDocument) -> PolicyDocument:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into security.policy"
                " (id, realm_id, name, revision, document, policy_digest, effective_from)"
                " values (%s, %s, %s, %s, %s::jsonb, %s, %s)",
                (
                    document.id,
                    document.realm_id,
                    document.name,
                    document.revision,
                    canonical_json(document.body()),
                    document.policy_digest,
                    document.effective_from,
                ),
            )
        return document

    def current(self, name: str) -> PolicyDocument | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, document, effective_from from security.policy"
                " where name = %s order by revision desc limit 1",
                (name,),
            )
            row = cursor.fetchone()
        return None if row is None else _policy_from_row(row)

    def next_revision(self, name: str) -> int:
        current = self.current(name)
        return 1 if current is None else current.revision + 1


def _policy_from_row(row: Sequence[Any]) -> PolicyDocument:
    body = row[2]
    return PolicyDocument(
        id=row[0],
        realm_id=row[1],
        name=body["name"],
        revision=int(body["revision"]),
        rules=tuple(
            PolicyRule(
                name=rule["name"],
                effect_kinds=tuple(EffectKind(item) for item in rule["effect_kinds"]),
                allow=bool(rule["allow"]),
                max_risk=RiskLevel(rule["max_risk"]),
                allowed_resources=tuple(rule.get("allowed_resources") or []),
                reason=rule.get("reason", ""),
            )
            for rule in body["rules"]
        ),
        network_default_deny=bool(body["network_default_deny"]),
        push_default_deny=bool(body["push_default_deny"]),
        effective_from=row[3],
    )


@dataclass(frozen=True, slots=True)
class CapabilityRepository:
    """Surumlu yetenek kayitlari."""

    connection: Any
    realm_id: UUID

    def append(self, capability: Capability) -> Capability:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into security.capability"
                " (id, realm_id, name, revision, kind, description, definition,"
                "  capability_digest, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                (
                    capability.id,
                    capability.realm_id,
                    capability.name,
                    capability.revision,
                    capability.kind.value,
                    capability.description,
                    canonical_json(dict(capability.definition)),
                    capability.capability_digest,
                    capability.created_at,
                ),
            )
        return capability

    def current(self, name: str) -> Capability | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, name, revision, kind, description, definition, created_at"
                " from security.capability where name = %s order by revision desc limit 1",
                (name,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return Capability(
            id=row[0],
            realm_id=row[1],
            name=row[2],
            revision=row[3],
            kind=CapabilityKind(row[4]),
            description=row[5],
            definition=dict(row[6] or {}),
            created_at=row[7],
        )

    def list_all(self) -> tuple[Capability, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute("select distinct name from security.capability order by name")
            names = [row[0] for row in cursor.fetchall()]
        found = [self.current(name) for name in names]
        return tuple(item for item in found if item is not None)

    def next_revision(self, name: str) -> int:
        current = self.current(name)
        return 1 if current is None else current.revision + 1


@dataclass(frozen=True, slots=True)
class SecretRefRepository:
    """SecretRef metadata kayitlari. Deger hicbir zaman yazilmaz."""

    connection: Any
    realm_id: UUID

    def add(self, reference: SecretRef) -> SecretRef:
        if reference.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm SecretRef reddedildi")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into security.secret_ref"
                " (id, realm_id, project_id, name, provider, purpose, allowed_operations,"
                "  store_backend, store_locator, version, status, expires_at, metadata_digest,"
                "  created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    reference.id,
                    reference.realm_id,
                    reference.project_id,
                    reference.name,
                    reference.provider,
                    reference.purpose,
                    list(reference.allowed_operations),
                    reference.store_backend.value,
                    reference.store_locator,
                    reference.version,
                    reference.status.value,
                    reference.expires_at,
                    reference.metadata_digest,
                    reference.created_at,
                ),
            )
        return reference

    def get(self, secret_ref_id: UUID) -> SecretRef:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_SECRET_COLUMNS} from security.secret_ref where id = %s", (secret_ref_id,)
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("SecretRef bulunamadi")
        return _secret_from_row(row)

    def current_by_name(self, name: str) -> SecretRef | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_SECRET_COLUMNS} from security.secret_ref"
                " where name = %s and status <> 'revoked' order by version desc limit 1",
                (name,),
            )
            row = cursor.fetchone()
        return None if row is None else _secret_from_row(row)

    def list_all(self) -> tuple[SecretRef, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_SECRET_COLUMNS} from security.secret_ref order by name, version desc"
            )
            rows = cursor.fetchall()
        return tuple(_secret_from_row(row) for row in rows)

    def set_status(self, secret_ref_id: UUID, status: SecretStatus) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update security.secret_ref set status = %s where id = %s",
                (status.value, secret_ref_id),
            )
            if cursor.rowcount == 0:
                raise NotFound("SecretRef bulunamadi")


_SECRET_COLUMNS = (
    "id, realm_id, project_id, name, provider, purpose, allowed_operations, store_backend,"
    " store_locator, version, status, expires_at, created_at"
)


def _secret_from_row(row: Sequence[Any]) -> SecretRef:
    return SecretRef(
        id=row[0],
        realm_id=row[1],
        project_id=row[2],
        name=row[3],
        provider=row[4],
        purpose=row[5],
        allowed_operations=tuple(row[6] or ()),
        store_backend=SecretBackend(row[7]),
        store_locator=row[8],
        version=row[9],
        status=SecretStatus(row[10]),
        expires_at=row[11],
        created_at=row[12],
    )


@dataclass(frozen=True, slots=True)
class ConsumeResult:
    """Yetki tuketiminin sonucu."""

    consumed: bool
    reason: str
    authorization: Authorization | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "consumed": self.consumed,
            "reason": self.reason,
            "authorization_id": (
                None if self.authorization is None else str(self.authorization.id)
            ),
        }


@dataclass(frozen=True, slots=True)
class AuthorizationRepository:
    """Exact authorization ledgeri."""

    connection: Any
    realm_id: UUID

    def issue(self, authorization: Authorization) -> Authorization:
        if authorization.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm yetki reddedildi")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into security.authorization"
                " (id, realm_id, actor_id, work_item_id, plan_id, plan_digest, effect_digest,"
                "  scope, allowed_resources, allowed_effects, provider_refs, secret_ref_ids,"
                "  risk, state, issued_at, expires_at, authorization_digest)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb,"
                "         %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    authorization.id,
                    authorization.realm_id,
                    authorization.actor_id,
                    authorization.work_item_id,
                    authorization.plan_id,
                    authorization.plan_digest,
                    authorization.effect_digest,
                    canonical_json(authorization.scope.body()),
                    list(authorization.scope.allowed_resources),
                    list(authorization.scope.allowed_effects),
                    list(authorization.scope.provider_refs),
                    [str(item) for item in authorization.scope.secret_ref_ids],
                    authorization.risk,
                    authorization.state.value,
                    authorization.issued_at,
                    authorization.expires_at,
                    authorization.authorization_digest,
                ),
            )
        return authorization

    def get(self, authorization_id: UUID) -> Authorization:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_AUTHORIZATION_COLUMNS} from security.authorization where id = %s",
                (authorization_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Yetki bulunamadi")
        return _authorization_from_row(row)

    def list_active(self, *, now: dt.datetime | None = None) -> tuple[Authorization, ...]:
        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_AUTHORIZATION_COLUMNS} from security.authorization"
                " where state = 'issued' and expires_at > %s order by issued_at desc",
                (moment,),
            )
            rows = cursor.fetchall()
        return tuple(_authorization_from_row(row) for row in rows)

    def consume(
        self,
        authorization_id: UUID,
        *,
        effect_digest: str,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> ConsumeResult:
        """Yetkiyi atomik olarak tuketir.

        Tek bir UPDATE ile hem durum hem effect eslesmesi kontrol edilir; boylece
        iki surecin ayni yetkiyi tuketmesi mumkun degildir.
        """
        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update security.authorization"
                " set state = 'consumed', consumed_at = %s, consumed_by = %s"
                " where id = %s"
                "   and state = 'issued'"
                "   and expires_at > %s"
                "   and effect_digest = %s"
                f" returning {_AUTHORIZATION_COLUMNS}",
                (moment, consumed_by, authorization_id, moment, effect_digest),
            )
            row = cursor.fetchone()
        if row is not None:
            return ConsumeResult(
                consumed=True, reason="consumed", authorization=_authorization_from_row(row)
            )

        # Neden basarisiz oldugunu ayrica raporla; sessiz reddetme yok.
        try:
            existing = self.get(authorization_id)
        except NotFound:
            return ConsumeResult(consumed=False, reason="authorization-not-found")
        if existing.effect_digest != effect_digest:
            return ConsumeResult(
                consumed=False, reason="effect-digest-mismatch", authorization=existing
            )
        rejection = existing.rejection_reason(moment) or "authorization-unavailable"
        return ConsumeResult(consumed=False, reason=rejection, authorization=existing)

    def revoke(
        self, authorization_id: UUID, reason: str, *, now: dt.datetime | None = None
    ) -> bool:
        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update security.authorization"
                " set state = 'revoked', revoked_at = %s, revocation_reason = %s"
                " where id = %s and state = 'issued'",
                (moment, reason, authorization_id),
            )
            return bool(cursor.rowcount)

    def expire_stale(self, *, now: dt.datetime | None = None) -> int:
        """Suresi dolmus yetkileri terminal duruma alir."""
        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update security.authorization set state = 'expired'"
                " where state = 'issued' and expires_at <= %s",
                (moment,),
            )
            return int(cursor.rowcount)


_AUTHORIZATION_COLUMNS = (
    "id, realm_id, actor_id, work_item_id, plan_id, plan_digest, effect_digest, scope,"
    " risk, state, issued_at, expires_at, consumed_at, consumed_by, revoked_at, revocation_reason"
)


def _authorization_from_row(row: Sequence[Any]) -> Authorization:
    scope = row[7]
    return Authorization(
        id=row[0],
        realm_id=row[1],
        actor_id=row[2],
        work_item_id=row[3],
        plan_id=row[4],
        plan_digest=row[5],
        effect_digest=row[6],
        scope=AuthorizationScope(
            allowed_resources=tuple(scope.get("allowed_resources") or ()),
            allowed_effects=tuple(scope.get("allowed_effects") or ()),
            provider_refs=tuple(scope.get("provider_refs") or ()),
            secret_ref_ids=tuple(UUID(item) for item in scope.get("secret_ref_ids") or ()),
            data_classifications=tuple(
                DataClassification(item) for item in scope.get("data_classifications") or ()
            ),
        ),
        risk=row[8],
        state=AuthorizationState(row[9]),
        issued_at=row[10],
        expires_at=row[11],
        consumed_at=row[12],
        consumed_by=row[13],
        revoked_at=row[14],
        revocation_reason=row[15],
    )


@dataclass(frozen=True, slots=True)
class OutboundRequestRepository:
    """Disari acilan istek kayitlari."""

    connection: Any
    realm_id: UUID

    def record(self, request: OutboundRequest) -> OutboundRequest:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into security.outbound_request"
                " (id, realm_id, authorization_id, provider_ref, endpoint_ref, operation,"
                "  data_categories, payload_digest, retention_assumption, region,"
                "  request_identity, state, denial_reason, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (id) do update set"
                "   state = excluded.state,"
                "   authorization_id = excluded.authorization_id,"
                "   denial_reason = excluded.denial_reason",
                (
                    request.id,
                    request.realm_id,
                    request.authorization_id,
                    request.provider_ref,
                    request.endpoint_ref,
                    request.operation,
                    [item.value for item in request.data_categories],
                    request.payload_digest,
                    request.retention_assumption,
                    request.region,
                    request.request_identity,
                    request.state.value,
                    request.denial_reason,
                    request.created_at,
                ),
            )
        return request

    def get(self, request_id: UUID) -> OutboundRequest:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, authorization_id, provider_ref, endpoint_ref, operation,"
                " data_categories, payload_digest, retention_assumption, region,"
                " request_identity, state, denial_reason, created_at"
                " from security.outbound_request where id = %s",
                (request_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Outbound istek bulunamadi")
        return OutboundRequest(
            id=row[0],
            realm_id=row[1],
            authorization_id=row[2],
            provider_ref=row[3],
            endpoint_ref=row[4],
            operation=row[5],
            data_categories=tuple(DataClassification(item) for item in row[6] or ()),
            payload_digest=row[7],
            retention_assumption=row[8],
            region=row[9],
            request_identity=row[10],
            state=OutboundState(row[11]),
            denial_reason=row[12],
            created_at=row[13],
        )


@dataclass(frozen=True, slots=True)
class AuditRepository:
    """Append-only denetim kaydi."""

    connection: Any
    realm_id: UUID

    def record(
        self,
        *,
        action: str,
        subject_type: str,
        subject_id: str,
        decision: str,
        reason: str,
        evidence: dict[str, Any] | None = None,
        actor_id: UUID | None = None,
        authorization_id: UUID | None = None,
        correlation_id: UUID | None = None,
        now: dt.datetime | None = None,
    ) -> UUID:
        moment = now or dt.datetime.now(dt.UTC)
        record_id = new_uuid7(now=moment)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into security.audit_event"
                " (id, realm_id, actor_id, correlation_id, action, subject_type, subject_id,"
                "  authorization_id, decision, reason, evidence_digest, occurred_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record_id,
                    self.realm_id,
                    actor_id,
                    correlation_id,
                    action,
                    subject_type,
                    subject_id,
                    authorization_id,
                    decision,
                    reason,
                    digest(evidence or {}),
                    moment,
                ),
            )
        return record_id

    def for_subject(self, subject_type: str, subject_id: str) -> tuple[dict[str, Any], ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select sequence, action, decision, reason, authorization_id, actor_id,"
                " evidence_digest, occurred_at from security.audit_event"
                " where subject_type = %s and subject_id = %s order by sequence",
                (subject_type, subject_id),
            )
            rows = cursor.fetchall()
        return tuple(
            {
                "sequence": int(row[0]),
                "action": row[1],
                "decision": row[2],
                "reason": row[3],
                "authorization_id": None if row[4] is None else str(row[4]),
                "actor_id": None if row[5] is None else str(row[5]),
                "evidence_digest": row[6],
                "occurred_at": row[7],
            }
            for row in rows
        )

    def recent(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select sequence, action, subject_type, subject_id, decision, reason,"
                " authorization_id, occurred_at from security.audit_event"
                " order by sequence desc limit %s",
                (limit,),
            )
            rows = cursor.fetchall()
        return tuple(
            {
                "sequence": int(row[0]),
                "action": row[1],
                "subject_type": row[2],
                "subject_id": row[3],
                "decision": row[4],
                "reason": row[5],
                "authorization_id": None if row[6] is None else str(row[6]),
                "occurred_at": row[7],
            }
            for row in rows
        )
