"""Agent Result Envelope ve bagimsiz verifier kurallari.

Sozlesme (`harness/SUBAGENT_VE_RESULT_ENVELOPE.md`):

- Her child **strict** bir envelope dondurur; serbest metin authoritative sonuc
  degildir.
- `partial`, `failed`, `blocked`, `recovery-required` ve `abstained` kaybolmaz;
  fan-in bunlari yutamaz.
- Agentic iste en az bir gercek subagent gerekir; koordinator subagent sayilmaz.
- Yuksek/kritik riskte verifier kimligi builder'dan farkli olmalidir.
"""

from __future__ import annotations

import datetime as dt
import hmac
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

ENVELOPE_SCHEMA_V1 = "zekam-agent-result-envelope/v1"
ENVELOPE_SCHEMA_V2 = "zekam-agent-result-envelope/v2"
ENVELOPE_SCHEMA = ENVELOPE_SCHEMA_V2
SUPPORTED_ENVELOPE_SCHEMAS = frozenset({ENVELOPE_SCHEMA_V1, ENVELOPE_SCHEMA_V2})


class EnvelopeStatus(StrEnum):
    """Child sonucunun durumu."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    RECOVERY_REQUIRED = "recovery-required"
    ABSTAINED = "abstained"


#: Basarili sayilan tek durum.
SUCCESS_STATUSES: frozenset[EnvelopeStatus] = frozenset({EnvelopeStatus.COMPLETED})

#: Fan-in sirasinda gorunur kalmasi zorunlu durumlar.
NON_SUCCESS_STATUSES: frozenset[EnvelopeStatus] = frozenset(
    {
        EnvelopeStatus.PARTIAL,
        EnvelopeStatus.FAILED,
        EnvelopeStatus.BLOCKED,
        EnvelopeStatus.RECOVERY_REQUIRED,
        EnvelopeStatus.ABSTAINED,
    }
)


class AgentRole(StrEnum):
    """Bir child'in ustlendigi rol."""

    COORDINATOR = "coordinator"
    RESEARCHER = "researcher"
    BUILDER = "builder"
    REVIEWER = "reviewer"
    CRITIC = "critic"
    SYNTHESIZER = "synthesizer"
    VERIFIER = "verifier"


#: Koordinator subagent sayilmaz.
SUBAGENT_ROLES: frozenset[AgentRole] = frozenset(
    {
        AgentRole.RESEARCHER,
        AgentRole.BUILDER,
        AgentRole.REVIEWER,
        AgentRole.CRITIC,
        AgentRole.SYNTHESIZER,
        AgentRole.VERIFIER,
    }
)


@dataclass(frozen=True, slots=True)
class EnvelopeEvidence:
    """Sonucu destekleyen tek bir kanit."""

    kind: str
    reference: str
    content_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.reference.strip():
            raise ValidationFailed("Kanit turu ve referansi bos olamaz")
        if self.content_digest is not None:
            parse_digest(self.content_digest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reference": self.reference,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class AgentResultEnvelope:
    """Bir child'in dondurdugu katı sonuc kaydi."""

    schema: str
    agent_id: str
    role: AgentRole
    status: EnvelopeStatus
    summary: str
    evidence: tuple[EnvelopeEvidence, ...] = ()
    findings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    produced_resources: tuple[str, ...] = ()
    token_count: int = 0
    cost_micros: int = 0
    latency_ms: int = 0
    produced_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if self.schema not in SUPPORTED_ENVELOPE_SCHEMAS:
            raise ValidationFailed(f"Desteklenmeyen envelope semasi: {self.schema}")
        if not self.agent_id.strip():
            raise ValidationFailed("Agent kimligi bos olamaz")
        if not self.summary.strip():
            raise ValidationFailed("Envelope ozeti bos olamaz")
        if self.status is EnvelopeStatus.COMPLETED and not self.evidence:
            raise ValidationFailed("Completed envelope en az bir kanit tasimali")
        if self.status is EnvelopeStatus.BLOCKED and not self.blockers:
            raise ValidationFailed("Blocked envelope blocker aciklamasi tasimali")
        if self.token_count < 0 or self.cost_micros < 0 or self.latency_ms < 0:
            raise ValidationFailed("Olcumler negatif olamaz")
        if self.produced_at.tzinfo is None or self.produced_at.tzinfo.utcoffset(
            self.produced_at
        ) is None:
            raise ValidationFailed("Envelope produced_at timezone tasimali")

    @classmethod
    def create(
        cls,
        *,
        agent_id: str,
        role: AgentRole,
        status: EnvelopeStatus,
        summary: str,
        evidence: tuple[EnvelopeEvidence, ...] = (),
        findings: tuple[str, ...] = (),
        blockers: tuple[str, ...] = (),
        produced_resources: tuple[str, ...] = (),
        token_count: int = 0,
        cost_micros: int = 0,
        latency_ms: int = 0,
        now: dt.datetime | None = None,
    ) -> AgentResultEnvelope:
        return cls(
            schema=ENVELOPE_SCHEMA,
            agent_id=agent_id,
            role=role,
            status=status,
            summary=summary,
            evidence=evidence,
            findings=findings,
            blockers=blockers,
            produced_resources=tuple(sorted(produced_resources)),
            token_count=token_count,
            cost_micros=cost_micros,
            latency_ms=latency_ms,
            produced_at=now or dt.datetime.now(dt.UTC),
        )

    @property
    def is_success(self) -> bool:
        return self.status in SUCCESS_STATUSES

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": self.schema,
            "agent_id": self.agent_id,
            "role": self.role.value,
            "status": self.status.value,
            "summary": self.summary,
            "evidence": [item.as_dict() for item in self.evidence],
            "findings": list(self.findings),
            "blockers": list(self.blockers),
            "produced_resources": list(self.produced_resources),
        }
        if self.schema == ENVELOPE_SCHEMA_V2:
            body.update(
                {
                    "token_count": self.token_count,
                    "cost_micros": self.cost_micros,
                    "latency_ms": self.latency_ms,
                    "produced_at": self.produced_at.astimezone(dt.UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
        return body

    @property
    def result_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        document = self.body() | {"result_digest": self.result_digest}
        if self.schema == ENVELOPE_SCHEMA_V1:
            document.update(
                {
                    "token_count": self.token_count,
                    "cost_micros": self.cost_micros,
                    "latency_ms": self.latency_ms,
                }
            )
        return document


def _parse_produced_at(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise ValidationFailed("Envelope produced_at RFC3339 metni olmali")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailed("Envelope produced_at gecersiz") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValidationFailed("Envelope produced_at timezone tasimali")
    return parsed


def parse_envelope(document: dict[str, Any]) -> AgentResultEnvelope:
    """Ham belgeyi strict envelope'a cevirir.

    Eksik veya bilinmeyen alan sessizce tolere edilmez; serbest metin sonuc
    olarak kabul edilmez.
    """
    if not isinstance(document, dict):
        raise ValidationFailed("Envelope bir sozluk olmali")
    required = {"schema", "agent_id", "role", "status", "summary", "result_digest"}
    schema = document.get("schema")
    if schema == ENVELOPE_SCHEMA_V2:
        required |= {"token_count", "cost_micros", "latency_ms", "produced_at"}
    elif schema != ENVELOPE_SCHEMA_V1:
        raise ValidationFailed(f"Desteklenmeyen envelope semasi: {schema}")
    missing = required - set(document)
    if missing:
        raise ValidationFailed(f"Envelope zorunlu alanlari eksik: {sorted(missing)}")
    unknown = set(document) - (
        required
        | {
            "evidence",
            "findings",
            "blockers",
            "produced_resources",
            "token_count",
            "cost_micros",
            "latency_ms",
            "produced_at",
            "result_digest",
        }
    )
    if unknown:
        raise ValidationFailed(f"Envelope bilinmeyen alan tasiyor: {sorted(unknown)}")

    try:
        role = AgentRole(document["role"])
        status = EnvelopeStatus(document["status"])
    except ValueError as exc:
        raise ValidationFailed(f"Envelope gecersiz enum degeri: {exc}") from exc

    evidence = tuple(
        EnvelopeEvidence(
            kind=item["kind"],
            reference=item["reference"],
            content_digest=item.get("content_digest"),
        )
        for item in document.get("evidence", [])
    )
    envelope = AgentResultEnvelope(
        schema=str(document["schema"]),
        agent_id=str(document["agent_id"]),
        role=role,
        status=status,
        summary=str(document["summary"]),
        evidence=evidence,
        findings=tuple(document.get("findings", [])),
        blockers=tuple(document.get("blockers", [])),
        produced_resources=tuple(document.get("produced_resources", [])),
        token_count=int(document.get("token_count", 0)),
        cost_micros=int(document.get("cost_micros", 0)),
        latency_ms=int(document.get("latency_ms", 0)),
        produced_at=(
            _parse_produced_at(document["produced_at"])
            if schema == ENVELOPE_SCHEMA_V2
            else dt.datetime.now(dt.UTC)
        ),
    )
    supplied_digest = str(document["result_digest"])
    parse_digest(supplied_digest)
    if not hmac.compare_digest(supplied_digest, envelope.result_digest):
        raise ValidationFailed("Envelope result_digest canonical body ile uyusmuyor")
    return envelope


@dataclass(frozen=True, slots=True)
class FanInResult:
    """Koordinatorun child sonuclarini birlestirmesi."""

    status: EnvelopeStatus
    envelopes: tuple[AgentResultEnvelope, ...]
    subagent_count: int
    unresolved: tuple[AgentResultEnvelope, ...]
    total_tokens: int
    total_cost_micros: int

    @property
    def is_success(self) -> bool:
        return self.status is EnvelopeStatus.COMPLETED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "subagent_count": self.subagent_count,
            "envelope_count": len(self.envelopes),
            "unresolved": [item.as_dict() for item in self.unresolved],
            "total_tokens": self.total_tokens,
            "total_cost_micros": self.total_cost_micros,
        }


#: Fan-in sirasinda en agir basan durum once gelir.
_STATUS_SEVERITY: dict[EnvelopeStatus, int] = {
    EnvelopeStatus.COMPLETED: 0,
    EnvelopeStatus.ABSTAINED: 1,
    EnvelopeStatus.PARTIAL: 2,
    EnvelopeStatus.BLOCKED: 3,
    EnvelopeStatus.FAILED: 4,
    EnvelopeStatus.RECOVERY_REQUIRED: 5,
}


def count_subagents(envelopes: Sequence[AgentResultEnvelope]) -> int:
    """Koordinator disindaki benzersiz agent sayisi."""
    return len({item.agent_id for item in envelopes if item.role in SUBAGENT_ROLES})


def fan_in(envelopes: Sequence[AgentResultEnvelope], *, agentic: bool = True) -> FanInResult:
    """Child sonuclarini birlestirir.

    Basarisiz, kismi, bloklu, recovery gerektiren ve cekimser sonuclar
    kaybolmaz; toplam durum en agir basan duruma esittir.
    """
    if not envelopes:
        raise ValidationFailed("Fan-in en az bir envelope ister")

    subagents = count_subagents(envelopes)
    if agentic and subagents < 1:
        raise PolicyViolation("Agentic is en az bir gercek subagent ister; koordinator sayilmaz")

    unresolved = tuple(item for item in envelopes if item.status in NON_SUCCESS_STATUSES)
    status = max(envelopes, key=lambda item: _STATUS_SEVERITY[item.status]).status
    return FanInResult(
        status=status,
        envelopes=tuple(envelopes),
        subagent_count=subagents,
        unresolved=unresolved,
        total_tokens=sum(item.token_count for item in envelopes),
        total_cost_micros=sum(item.cost_micros for item in envelopes),
    )


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    """Bagimsiz verifier icin girdi."""

    builder_agent_id: str
    verifier_agent_id: str
    risk: str
    builder_result: AgentResultEnvelope

    def __post_init__(self) -> None:
        if not self.verifier_agent_id.strip():
            raise ValidationFailed("Verifier kimligi bos olamaz")


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """Verifier karari."""

    passed: bool
    reason: str
    verifier_agent_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "verifier_agent_id": self.verifier_agent_id,
        }


#: Bagimsiz verifier zorunlu risk seviyeleri.
VERIFIER_REQUIRED_RISKS: frozenset[str] = frozenset({"high", "critical"})


def assert_independent_verifier(
    *, builder_agent_id: str, verifier_agent_id: str, risk: str
) -> None:
    """Builder ve verifier kimliklerinin ayri oldugunu dogrular."""
    if risk in VERIFIER_REQUIRED_RISKS and builder_agent_id == verifier_agent_id:
        raise PolicyViolation(
            "Yuksek riskli iste builder kendi isini dogrulayamaz; bagimsiz verifier gerekir"
        )


def verify(request: VerificationRequest) -> VerificationOutcome:
    """Builder sonucunu bagimsiz olarak degerlendirir."""
    assert_independent_verifier(
        builder_agent_id=request.builder_agent_id,
        verifier_agent_id=request.verifier_agent_id,
        risk=request.risk,
    )
    result = request.builder_result
    if not result.is_success:
        return VerificationOutcome(
            passed=False,
            reason=f"builder-status-{result.status.value}",
            verifier_agent_id=request.verifier_agent_id,
        )
    if not result.evidence:
        return VerificationOutcome(
            passed=False,
            reason="kanit-yok",
            verifier_agent_id=request.verifier_agent_id,
        )
    return VerificationOutcome(
        passed=True,
        reason="kanit-dogrulandi",
        verifier_agent_id=request.verifier_agent_id,
    )


def assert_subagent_policy(
    envelopes: Sequence[AgentResultEnvelope],
    *,
    agentic: bool,
    minimum_subagents: int = 1,
) -> None:
    """Minimum subagent politikasini uygular.

    Deterministik islerde subagent zorunlu degildir; agentic islerde en az bir
    gercek subagent gerekir ve koordinator bu sayiya dahil edilmez.
    """
    if not agentic:
        return
    count = count_subagents(envelopes)
    if count < minimum_subagents:
        raise PolicyViolation(
            f"Agentic is en az {minimum_subagents} subagent ister; bulunan {count}"
        )


@dataclass(frozen=True, slots=True)
class SubagentAssignment:
    """Bir alt probleme atanan agent."""

    agent_id: str
    role: AgentRole
    writable_resources: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "role": self.role.value,
            "writable_resources": list(self.writable_resources),
        }


def assert_single_builder_per_resource(assignments: Sequence[SubagentAssignment]) -> None:
    """Ayni yazilabilir kaynakta birden fazla builder olmasini reddeder."""
    owners: dict[str, str] = {}
    for assignment in assignments:
        if assignment.role is not AgentRole.BUILDER:
            continue
        for resource in assignment.writable_resources:
            existing = owners.get(resource)
            if existing is not None and existing != assignment.agent_id:
                raise PolicyViolation(f"Ayni yazilabilir kaynakta iki builder: {resource}")
            owners[resource] = assignment.agent_id


def assert_distinct_verifier(assignments: Sequence[SubagentAssignment], *, risk: str) -> None:
    """Yuksek riskte verifier'in builder'lardan farkli oldugunu dogrular."""
    if risk not in VERIFIER_REQUIRED_RISKS:
        return
    builders = {item.agent_id for item in assignments if item.role is AgentRole.BUILDER}
    verifiers = {item.agent_id for item in assignments if item.role is AgentRole.VERIFIER}
    if not verifiers:
        raise PolicyViolation("Yuksek riskli is bagimsiz verifier ister")
    if builders & verifiers:
        raise PolicyViolation("Verifier builder ile ayni kimlik olamaz")


def envelope_identity(envelope: AgentResultEnvelope, run_id: UUID) -> str:
    """Envelope'u ana run'a baglayan kararli kimlik."""
    return digest({"run_id": str(run_id), "agent_id": envelope.agent_id})
