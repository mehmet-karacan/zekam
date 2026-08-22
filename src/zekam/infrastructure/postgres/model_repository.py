"""Model envanteri, probe ve rapor kayitlari icin PostgreSQL adapterleri."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.errors import NotFound
from zekam.domain.identifiers import new_uuid7
from zekam.domain.model_health import (
    CapabilityCheck,
    ContractCapability,
    ProbeFailure,
    ProbeOutcome,
    ProbeStatus,
)
from zekam.domain.model_inventory import (
    BenchmarkState,
    CostEvidence,
    HealthState,
    Modality,
    ModelProvenance,
    ModelRecord,
    ProviderProtocol,
)

_MODEL_COLUMNS = (
    "model_id, inventory_index, access_name, backend_model, provider_protocol, declared_mode,"
    " declared_category, endpoint_ref, credential_ref, endpoint_scope,"
    " declared_parameter_profile, reasoning_effort, enabled, status, health_state,"
    " benchmark_state, quarantine_until, capabilities_declared, capabilities_verified,"
    " cost_evidence, provenance"
)


def _record_from_row(row: Sequence[Any]) -> ModelRecord:
    provenance = row[20] or {}
    return ModelRecord(
        model_id=row[0],
        inventory_index=row[1],
        access_name=row[2],
        backend_model=row[3],
        provider_protocol=ProviderProtocol(row[4]),
        declared_mode=row[5],
        declared_category=row[6],
        endpoint_ref=row[7],
        credential_ref=row[8],
        endpoint_scope=row[9],
        declared_parameter_profile=row[10],
        reasoning_effort=row[11],
        enabled=row[12],
        status=row[13],
        health_state=HealthState(row[14]),
        benchmark_state=BenchmarkState(row[15]),
        quarantine_until=row[16],
        capabilities_declared=tuple(row[17] or ()),
        capabilities_verified=tuple(row[18] or ()),
        cost=CostEvidence.from_mapping(row[19]),
        provenance=ModelProvenance(
            canonical_report=provenance.get("canonical_report", "bilinmiyor"),
            technical_report=provenance.get("technical_report"),
            technical_profile_available=bool(provenance.get("technical_profile_available", False)),
            verification_note=provenance.get("verification_note", ""),
        ),
    )


@dataclass(frozen=True, slots=True)
class ModelInventoryRepository:
    """Model envanteri kayitlari."""

    connection: Any
    realm_id: UUID

    def upsert(self, record: ModelRecord) -> str:
        """Kaydi ekler veya gunceller. Ayni digest ise `unchanged` doner."""
        existing = self.find(record.model_id)
        stored_modality = self._stored_modality(record.model_id)
        if (
            existing is not None
            and existing.inventory_digest == record.inventory_digest
            and stored_modality is record.modality
        ):
            return "unchanged"

        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.model_inventory"
                " (id, realm_id, model_id, inventory_index, access_name, backend_model,"
                "  provider_protocol, declared_mode, declared_category, modality, endpoint_ref,"
                "  credential_ref, endpoint_scope, declared_parameter_profile, reasoning_effort,"
                "  enabled, status, health_state, benchmark_state, quarantine_until,"
                "  capabilities_declared, capabilities_verified, cost_evidence, provenance,"
                "  technical_profile_available, inventory_digest)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                "         %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)"
                " on conflict (realm_id, model_id) do update set"
                "   inventory_index = excluded.inventory_index,"
                "   access_name = excluded.access_name,"
                "   backend_model = excluded.backend_model,"
                "   provider_protocol = excluded.provider_protocol,"
                "   declared_mode = excluded.declared_mode,"
                "   declared_category = excluded.declared_category,"
                "   modality = excluded.modality,"
                "   endpoint_ref = excluded.endpoint_ref,"
                "   credential_ref = excluded.credential_ref,"
                "   endpoint_scope = excluded.endpoint_scope,"
                "   declared_parameter_profile = excluded.declared_parameter_profile,"
                "   reasoning_effort = excluded.reasoning_effort,"
                "   enabled = excluded.enabled,"
                "   status = excluded.status,"
                "   capabilities_declared = excluded.capabilities_declared,"
                "   cost_evidence = excluded.cost_evidence,"
                "   provenance = excluded.provenance,"
                "   technical_profile_available = excluded.technical_profile_available,"
                "   inventory_digest = excluded.inventory_digest",
                (
                    new_uuid7(),
                    self.realm_id,
                    record.model_id,
                    record.inventory_index,
                    record.access_name,
                    record.backend_model,
                    record.provider_protocol.value,
                    record.declared_mode,
                    record.declared_category,
                    record.modality.value,
                    record.endpoint_ref,
                    record.credential_ref,
                    record.endpoint_scope,
                    record.declared_parameter_profile,
                    record.reasoning_effort,
                    record.enabled,
                    record.status,
                    record.health_state.value,
                    record.benchmark_state.value,
                    record.quarantine_until,
                    list(record.capabilities_declared),
                    list(record.capabilities_verified),
                    canonical_json(record.cost.as_dict()),
                    canonical_json(record.provenance.as_dict()),
                    record.provenance.technical_profile_available,
                    record.inventory_digest,
                ),
            )
        return "inserted" if existing is None else "updated"

    def _stored_modality(self, model_id: str) -> Modality | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select modality from models.model_inventory where model_id = %s",
                (model_id,),
            )
            row = cursor.fetchone()
        return None if row is None else Modality(str(row[0]))

    def find(self, model_id: str) -> ModelRecord | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_MODEL_COLUMNS} from models.model_inventory where model_id = %s",
                (model_id,),
            )
            row = cursor.fetchone()
        return None if row is None else _record_from_row(row)

    def get(self, model_id: str) -> ModelRecord:
        found = self.find(model_id)
        if found is None:
            raise NotFound(f"Model bulunamadi: {model_id}")
        return found

    def list_all(self) -> tuple[ModelRecord, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_MODEL_COLUMNS} from models.model_inventory order by inventory_index"
            )
            rows = cursor.fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def list_by_modality(self, modality: Modality) -> tuple[ModelRecord, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_MODEL_COLUMNS} from models.model_inventory"
                " where modality = %s order by inventory_index",
                (modality.value,),
            )
            rows = cursor.fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def set_health(
        self,
        model_id: str,
        *,
        state: HealthState,
        quarantine_until: dt.datetime | None,
        benchmark_state: BenchmarkState,
        policy_digest: str,
        inventory_digest: str,
        verified_capabilities: Sequence[str] | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        moment = now or dt.datetime.now(dt.UTC)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update models.model_inventory set health_state = %s, quarantine_until = %s,"
                " benchmark_state = %s, last_health_at = %s, last_health_policy_digest = %s,"
                " last_health_inventory_digest = %s,"
                " capabilities_verified = coalesce(%s, capabilities_verified)"
                " where model_id = %s",
                (
                    state.value,
                    quarantine_until,
                    benchmark_state.value,
                    moment,
                    policy_digest,
                    inventory_digest,
                    None if verified_capabilities is None else list(verified_capabilities),
                    model_id,
                ),
            )
            if cursor.rowcount == 0:
                raise NotFound(f"Model bulunamadi: {model_id}")

    def health_metadata(self, model_id: str) -> tuple[dt.datetime | None, str | None, str | None]:
        """Son health zamani, policy digest'i ve envanter digest'i."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select last_health_at, last_health_policy_digest, last_health_inventory_digest"
                " from models.model_inventory where model_id = %s",
                (model_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound(f"Model bulunamadi: {model_id}")
        return row[0], row[1], row[2]


@dataclass(frozen=True, slots=True)
class HealthProbeRepository:
    """Probe sonuclari (append-only)."""

    connection: Any
    realm_id: UUID

    def record(self, outcome: ProbeOutcome, *, policy_digest: str, inventory_digest: str) -> UUID:
        record_id = new_uuid7(now=outcome.observed_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.health_probe"
                " (id, realm_id, model_id, modality, fixture_name, fixture_digest, status,"
                "  failure_category, detail, latency_ms, response_digest, policy_digest,"
                "  inventory_digest, observed_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record_id,
                    self.realm_id,
                    outcome.model_id,
                    outcome.modality.value,
                    outcome.fixture_name,
                    outcome.fixture_digest,
                    outcome.status.value,
                    None if outcome.failure is None else outcome.failure.value,
                    outcome.detail,
                    outcome.latency_ms,
                    outcome.response_digest,
                    policy_digest,
                    inventory_digest,
                    outcome.observed_at,
                ),
            )
        return record_id

    def history(self, model_id: str, *, limit: int = 20) -> tuple[ProbeOutcome, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select model_id, modality, fixture_name, status, latency_ms, failure_category,"
                " detail, fixture_digest, response_digest, observed_at"
                " from models.health_probe where model_id = %s"
                " order by observed_at desc, id desc limit %s",
                (model_id, limit),
            )
            rows = cursor.fetchall()
        outcomes = [
            ProbeOutcome(
                model_id=row[0],
                modality=Modality(row[1]),
                fixture_name=row[2],
                status=ProbeStatus(row[3]),
                latency_ms=row[4],
                failure=None if row[5] is None else ProbeFailure(row[5]),
                detail=row[6],
                fixture_digest=row[7],
                response_digest=row[8],
                observed_at=row[9],
            )
            for row in rows
        ]
        return tuple(reversed(outcomes))


@dataclass(frozen=True, slots=True)
class CapabilityCheckRepository:
    """Sozlesme kontrolleri (append-only)."""

    connection: Any
    realm_id: UUID

    def record(self, check: CapabilityCheck) -> UUID:
        record_id = new_uuid7(now=check.checked_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.capability_check"
                " (id, realm_id, model_id, capability, verified, evidence, failure_category,"
                "  evidence_digest, checked_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record_id,
                    self.realm_id,
                    check.model_id,
                    check.capability.value,
                    check.verified,
                    check.evidence,
                    None if check.failure is None else check.failure.value,
                    check.evidence_digest,
                    check.checked_at,
                ),
            )
        return record_id

    def latest_for_model(self, model_id: str) -> tuple[CapabilityCheck, ...]:
        """Her yetenek icin en son kontrol."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select distinct on (capability) model_id, capability, verified, evidence,"
                " failure_category, checked_at"
                " from models.capability_check where model_id = %s"
                " order by capability, checked_at desc",
                (model_id,),
            )
            rows = cursor.fetchall()
        return tuple(
            CapabilityCheck(
                model_id=row[0],
                capability=ContractCapability(row[1]),
                verified=row[2],
                evidence=row[3],
                failure=None if row[4] is None else ProbeFailure(row[4]),
                checked_at=row[5],
            )
            for row in rows
        )


@dataclass(frozen=True, slots=True)
class QuarantineRepository:
    """Karantina olaylari (append-only)."""

    connection: Any
    realm_id: UUID

    def record(
        self,
        *,
        model_id: str,
        action: str,
        reason: str,
        consecutive_failures: int,
        cooldown_until: dt.datetime | None,
        policy_digest: str,
        now: dt.datetime | None = None,
    ) -> UUID:
        moment = now or dt.datetime.now(dt.UTC)
        record_id = new_uuid7(now=moment)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.quarantine_event"
                " (id, realm_id, model_id, action, reason, consecutive_failures, cooldown_until,"
                "  policy_digest, occurred_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record_id,
                    self.realm_id,
                    model_id,
                    action,
                    reason,
                    consecutive_failures,
                    cooldown_until,
                    policy_digest,
                    moment,
                ),
            )
        return record_id

    def history(self, model_id: str) -> tuple[dict[str, Any], ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select action, reason, consecutive_failures, cooldown_until, occurred_at"
                " from models.quarantine_event where model_id = %s order by occurred_at",
                (model_id,),
            )
            rows = cursor.fetchall()
        return tuple(
            {
                "action": row[0],
                "reason": row[1],
                "consecutive_failures": row[2],
                "cooldown_until": row[3],
                "occurred_at": row[4],
            }
            for row in rows
        )


@dataclass(frozen=True, slots=True)
class HealthReportRepository:
    """Gunluk saglik raporlari (append-only)."""

    connection: Any
    realm_id: UUID

    def store(
        self,
        *,
        report_date: dt.date,
        summary: dict[str, Any],
        evidence_digest: str,
        markdown_digest: str,
        json_digest: str,
        now: dt.datetime | None = None,
    ) -> UUID:
        moment = now or dt.datetime.now(dt.UTC)
        record_id = new_uuid7(now=moment)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.health_report"
                " (id, realm_id, report_date, summary, evidence_digest, markdown_digest,"
                "  json_digest, generated_at)"
                " values (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)"
                " on conflict (realm_id, report_date) do nothing"
                " returning id",
                (
                    record_id,
                    self.realm_id,
                    report_date,
                    canonical_json(summary),
                    evidence_digest,
                    markdown_digest,
                    json_digest,
                    moment,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0]))
            cursor.execute(
                "select id from models.health_report where report_date = %s", (report_date,)
            )
            existing = cursor.fetchone()
        if existing is None:  # pragma: no cover - conflict sonrasi kayit vardir
            raise NotFound("Rapor kaydedilemedi")
        return UUID(str(existing[0]))

    def find(self, report_date: dt.date) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select report_date, summary, evidence_digest, markdown_digest, json_digest,"
                " generated_at from models.health_report where report_date = %s",
                (report_date,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "report_date": row[0],
            "summary": row[1],
            "evidence_digest": row[2],
            "markdown_digest": row[3],
            "json_digest": row[4],
            "generated_at": row[5],
        }
