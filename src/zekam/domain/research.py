"""Kanitli arastirma sozlesmesi.

Arastirma yalniz *kanit* uretir. Authority, approval veya mutation izni uretmez.
Her finding exact source snapshot, locator ve digest tasir. Direct contradiction
sentezle kaybolmaz; gorunur ve unresolved kalir.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlparse

from zekam.domain.canonical import digest, digests_match, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_QUESTION_CHARS = 1000
MAX_ROUNDS = 2
MAX_DELIBERATION_SECONDS = 600

# Yalniz gercek secret isaretleri engellenir. "authorization" veya "approval"
# gibi kelimeler mesru arastirma konusudur; yetki sinirlari kelime filtresiyle
# degil yapisal bayrak ve constraint'lerle zorlanir.
_SENSITIVE = re.compile(
    r"(?:secret|credential|password|parola|api[-_ ]?key|private[-_ ]?key|owner[-_ ]?token)",
    re.IGNORECASE,
)


class SourceKind(StrEnum):
    FILE = "file"
    REPOSITORY = "repository"
    HTTPS = "https"
    IMPORT = "import"


class ResearchRole(StrEnum):
    COORDINATOR = "coordinator"
    RESEARCHER = "researcher"
    DOMAIN_REVIEWER = "domain-reviewer"
    CRITIC = "critic"
    SYNTHESIZER = "synthesizer"
    CITATION_VERIFIER = "citation-verifier"


#: Koordinator child sayilmaz; bu kume gercek subagent rolleridir.
BUILDER_ROLES = frozenset(
    {
        ResearchRole.RESEARCHER,
        ResearchRole.DOMAIN_REVIEWER,
        ResearchRole.CRITIC,
        ResearchRole.SYNTHESIZER,
        ResearchRole.CITATION_VERIFIER,
    }
)


class RoleOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    ABSTAINED = "abstained"
    RECOVERY_REQUIRED = "recovery-required"


#: Bu sonuclar fan-in tarafindan yutulamaz.
NON_SUCCESS_OUTCOMES = frozenset(RoleOutcome) - {RoleOutcome.SUCCESS}


class ConflictKind(StrEnum):
    COMPATIBLE = "compatible"
    SCOPE_OR_TERMINOLOGY = "scope-or-terminology"
    STALE_SOURCE = "stale-source"
    EVIDENCE_GAP = "evidence-gap"
    DIRECT_CONTRADICTION = "direct-contradiction"


class ReportStatus(StrEnum):
    ANSWERED = "answered"
    PARTIAL = "partial"
    ABSTAINED = "abstained"


def _reject_sensitive(value: str, label: str) -> None:
    if _SENSITIVE.search(value):
        raise PolicyViolation(f"{label} secret benzeri icerik tasiyamaz")


def _assert_relative(value: str, label: str) -> None:
    if PureWindowsPath(value).is_absolute() or value.startswith("/") or "\\" in value:
        raise PolicyViolation(f"{label} absolute path tasiyamaz")
    if ".." in PurePosixPath(value).parts:
        raise PolicyViolation(f"{label} traversal tasiyamaz")


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """Arastirmanin hangi kaynaklara dokunabilecegini sinirlar."""

    allowed_kinds: frozenset[SourceKind]
    allowed_hosts: frozenset[str] = frozenset()
    project_scope: str | None = None
    allow_row_data: bool = False

    def __post_init__(self) -> None:
        if not self.allowed_kinds:
            raise ValidationFailed("source policy en az bir kaynak turu ister")
        if SourceKind.HTTPS in self.allowed_kinds and not self.allowed_hosts:
            raise PolicyViolation("HTTPS kaynagi exact host allowlist ister")
        for host in self.allowed_hosts:
            if not host or host != host.lower().strip() or "/" in host:
                raise ValidationFailed("host girdisi normalize degil")

    def permits(self, kind: SourceKind, *, host: str | None = None) -> bool:
        if kind not in self.allowed_kinds:
            return False
        if kind is SourceKind.HTTPS:
            return host is not None and host in self.allowed_hosts
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed_kinds": sorted(str(item) for item in self.allowed_kinds),
            "allowed_hosts": sorted(self.allowed_hosts),
            "project_scope": self.project_scope,
            "allow_row_data": self.allow_row_data,
        }


@dataclass(frozen=True, slots=True)
class ResearchBudget:
    """Token, maliyet, sure ve tur butcesi. Sinirsiz arastirma yoktur."""

    max_tokens: int
    max_cost_units: int
    max_seconds: int
    max_rounds: int = MAX_ROUNDS

    def __post_init__(self) -> None:
        if min(self.max_tokens, self.max_cost_units, self.max_seconds) <= 0:
            raise ValidationFailed("butce degerleri pozitif olmali")
        if not 1 <= self.max_rounds <= MAX_ROUNDS:
            raise ValidationFailed(f"tur sayisi 1..{MAX_ROUNDS} araliginda olmali")
        if self.max_seconds > MAX_DELIBERATION_SECONDS:
            raise ValidationFailed("sure butcesi bounded siniri asiyor")

    def as_dict(self) -> dict[str, int]:
        return {
            "max_tokens": self.max_tokens,
            "max_cost_units": self.max_cost_units,
            "max_seconds": self.max_seconds,
            "max_rounds": self.max_rounds,
        }


@dataclass(frozen=True, slots=True)
class ResearchQuestion:
    """Project/work/intent scope'una ve source revision'a bagli arastirma sorusu."""

    question_id: str
    question: str
    project_ref: str
    work_ref: str
    intent_digest: str
    source_revision: str
    policy: SourcePolicy
    budget: ResearchBudget
    created_at: dt.datetime

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValidationFailed("arastirma sorusu bos olamaz")
        if len(self.question) > MAX_QUESTION_CHARS:
            raise ValidationFailed("arastirma sorusu bounded sinirini asiyor")
        _reject_sensitive(self.question, "arastirma sorusu")
        for label, value in (
            ("project_ref", self.project_ref),
            ("work_ref", self.work_ref),
            ("source_revision", self.source_revision),
        ):
            if not value.strip():
                raise ValidationFailed(f"{label} bos olamaz")
        parse_digest(self.intent_digest)
        if self.policy.project_scope not in (None, self.project_ref):
            raise PolicyViolation("source policy scope soru projesiyle uyusmuyor")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")

    def is_stale(self, *, current_source_revision: str, current_intent_digest: str) -> bool:
        return self.source_revision != current_source_revision or not digests_match(
            self.intent_digest, current_intent_digest
        )

    def assert_current(self, *, current_source_revision: str, current_intent_digest: str) -> None:
        if self.is_stale(
            current_source_revision=current_source_revision,
            current_intent_digest=current_intent_digest,
        ):
            raise PolicyViolation("stale arastirma sorusu; yeni revision gerekiyor")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-research-question/v1",
            "question_id": self.question_id,
            "question": self.question,
            "project_ref": self.project_ref,
            "work_ref": self.work_ref,
            "intent_digest": self.intent_digest,
            "source_revision": self.source_revision,
            "policy": self.policy.as_dict(),
            "budget": self.budget.as_dict(),
        }

    @property
    def question_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Kaynak icerigin degismezligini kanitlayan provenance kaydi."""

    snapshot_id: str
    kind: SourceKind
    locator: str
    content_digest: str
    captured_at: dt.datetime
    revision: str | None = None
    host: str | None = None

    def __post_init__(self) -> None:
        parse_digest(self.content_digest)
        if not self.locator.strip():
            raise ValidationFailed("locator bos olamaz")
        _reject_sensitive(self.locator, "locator")
        if self.captured_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")
        if self.kind in (SourceKind.FILE, SourceKind.REPOSITORY):
            _assert_relative(self.locator, "locator")
        if self.kind is SourceKind.REPOSITORY and not self.revision:
            raise ValidationFailed("repository snapshot revision ister")
        if self.kind is SourceKind.HTTPS:
            parsed = urlparse(self.locator)
            if parsed.scheme != "https" or not parsed.hostname:
                raise PolicyViolation("HTTPS kaynagi gecerli https URL ister")
            if self.host != parsed.hostname:
                raise ValidationFailed("host alani locator ile uyusmuyor")
            if parsed.query:
                raise PolicyViolation("HTTPS locator query string tasiyamaz")
        if self.kind is SourceKind.IMPORT and not self.revision:
            raise ValidationFailed("import snapshot kaynak surumu ister")

    def assert_permitted(self, policy: SourcePolicy) -> None:
        if not policy.permits(self.kind, host=self.host):
            raise PolicyViolation("kaynak source policy tarafindan reddedildi")

    def body(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "kind": str(self.kind),
            "locator": self.locator,
            "content_digest": self.content_digest,
            "revision": self.revision,
            "host": self.host,
        }

    @property
    def snapshot_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return dict(self.body(), captured_at=self.captured_at.isoformat())


@dataclass(frozen=True, slots=True)
class Citation:
    """Snapshot icindeki exact konum."""

    snapshot_id: str
    locator_detail: str
    content_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.content_digest)
        if not self.locator_detail.strip():
            raise ValidationFailed("citation locator detayi bos olamaz")

    def as_dict(self) -> dict[str, str]:
        return {
            "snapshot_id": self.snapshot_id,
            "locator_detail": self.locator_detail,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class Finding:
    """Tek bir kanitli bulgu. Citation'siz finding kabul edilmez."""

    finding_id: str
    claim: str
    citations: tuple[Citation, ...]
    confidence: str

    def __post_init__(self) -> None:
        if not self.claim.strip():
            raise ValidationFailed("bulgu metni bos olamaz")
        _reject_sensitive(self.claim, "bulgu")
        if not self.citations:
            raise ValidationFailed("kanitsiz bulgu kabul edilmez")
        if self.confidence not in {"low", "medium", "high"}:
            raise ValidationFailed("confidence degeri taninmiyor")

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "claim": self.claim,
            "citations": [item.as_dict() for item in self.citations],
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class RoleResult:
    """Strict child result envelope. Free-text authoritative sonuc degildir."""

    role: ResearchRole
    agent_ref: str
    outcome: RoleOutcome
    findings: tuple[Finding, ...] = ()
    objections: tuple[str, ...] = ()
    blocker: str | None = None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("role result authority veremez")
        if self.role is ResearchRole.COORDINATOR:
            raise PolicyViolation("koordinator child result uretemez")
        if self.outcome is RoleOutcome.SUCCESS and not self.findings:
            raise ValidationFailed("success sonucu en az bir bulgu ister")
        if (
            self.outcome in {RoleOutcome.BLOCKED, RoleOutcome.RECOVERY_REQUIRED}
            and not self.blocker
        ):
            raise ValidationFailed("blocked sonucu gerekce ister")
        if not self.agent_ref.strip():
            raise ValidationFailed("agent referansi bos olamaz")

    @property
    def is_success(self) -> bool:
        return self.outcome is RoleOutcome.SUCCESS

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": str(self.role),
            "agent_ref": self.agent_ref,
            "outcome": str(self.outcome),
            "findings": [item.as_dict() for item in self.findings],
            "objections": list(self.objections),
            "blocker": self.blocker,
            "grants_authority": False,
        }

    @property
    def result_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class ResearchNode:
    node_id: str
    role: ResearchRole
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise ValidationFailed("node kimligi bos olamaz")
        if self.node_id in self.depends_on:
            raise ValidationFailed("node kendine bagimli olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "role": str(self.role),
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True, slots=True)
class ResearchDag:
    """Rol DAG'i. Koordinator subagent sayilmaz; en az bir gercek child gerekir."""

    question_id: str
    nodes: tuple[ResearchNode, ...]

    def __post_init__(self) -> None:
        if not self.nodes:
            raise ValidationFailed("research DAG bos olamaz")
        identifiers = [node.node_id for node in self.nodes]
        if len(set(identifiers)) != len(identifiers):
            raise ValidationFailed("node kimlikleri tekrar edemez")
        known = set(identifiers)
        for node in self.nodes:
            missing = tuple(dep for dep in node.depends_on if dep not in known)
            if missing:
                raise ValidationFailed("tanimsiz bagimlilik")
        self.execution_order()
        if self.subagent_count < 1:
            raise PolicyViolation("agentic arastirma en az bir gercek subagent ister")

    @property
    def subagent_count(self) -> int:
        return sum(1 for node in self.nodes if node.role in BUILDER_ROLES)

    def execution_order(self) -> tuple[str, ...]:
        pending = {node.node_id: set(node.depends_on) for node in self.nodes}
        order: list[str] = []
        while pending:
            ready = sorted(name for name, deps in pending.items() if not deps)
            if not ready:
                raise ValidationFailed("research DAG dongu iceriyor")
            for name in ready:
                del pending[name]
                order.append(name)
            for deps in pending.values():
                deps.difference_update(ready)
        return tuple(order)

    def parallel_groups(self) -> tuple[tuple[str, ...], ...]:
        """Bagimsiz ilk roller ayni grupta paralel calisabilir."""

        pending = {node.node_id: set(node.depends_on) for node in self.nodes}
        groups: list[tuple[str, ...]] = []
        while pending:
            ready = tuple(sorted(name for name, deps in pending.items() if not deps))
            if not ready:
                raise ValidationFailed("research DAG dongu iceriyor")
            groups.append(ready)
            for name in ready:
                del pending[name]
            for deps in pending.values():
                deps.difference_update(ready)
        return tuple(groups)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "nodes": [node.as_dict() for node in self.nodes],
            "subagent_count": self.subagent_count,
        }


@dataclass(frozen=True, slots=True)
class Conflict:
    """Bulgular arasi celiski. Direct contradiction cozulmus sayilamaz."""

    conflict_id: str
    kind: ConflictKind
    left_finding_id: str
    right_finding_id: str
    detail: str
    resolved_by: str | None = None

    def __post_init__(self) -> None:
        if self.left_finding_id == self.right_finding_id:
            raise ValidationFailed("celiski iki farkli bulgu ister")
        if not self.detail.strip():
            raise ValidationFailed("celiski aciklamasi bos olamaz")
        if (
            self.kind is ConflictKind.DIRECT_CONTRADICTION
            and self.resolved_by is not None
            and self.resolved_by not in {"verifier", "human-review"}
        ):
            raise PolicyViolation(
                "direct contradiction yalniz verifier veya insan review ile cozulur"
            )

    @property
    def is_unresolved(self) -> bool:
        return self.kind is ConflictKind.DIRECT_CONTRADICTION and self.resolved_by is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "kind": str(self.kind),
            "left_finding_id": self.left_finding_id,
            "right_finding_id": self.right_finding_id,
            "detail": self.detail,
            "resolved_by": self.resolved_by,
        }


@dataclass(frozen=True, slots=True)
class CitationVerification:
    """Bagimsiz citation dogrulamasi; arastirmaciyla ayni kimlik olamaz."""

    verifier_ref: str
    verified_finding_ids: tuple[str, ...]
    rejected_finding_ids: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.verifier_ref.strip():
            raise ValidationFailed("verifier referansi bos olamaz")
        if len(self.rejected_finding_ids) != len(self.rejection_reasons):
            raise ValidationFailed("her red bir gerekce ister")
        overlap = set(self.verified_finding_ids) & set(self.rejected_finding_ids)
        if overlap:
            raise ValidationFailed("bir bulgu hem dogrulanip hem reddedilemez")

    def assert_independent(self, researcher_refs: frozenset[str]) -> None:
        if self.verifier_ref in researcher_refs:
            raise PolicyViolation("citation verifier arastirmaciyla ayni kimlik olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "verifier_ref": self.verifier_ref,
            "verified_finding_ids": list(self.verified_finding_ids),
            "rejected_finding_ids": list(self.rejected_finding_ids),
            "rejection_reasons": list(self.rejection_reasons),
        }


def synthesize(
    results: tuple[RoleResult, ...],
    *,
    conflicts: tuple[Conflict, ...],
    verification: CitationVerification,
) -> tuple[tuple[Finding, ...], tuple[Conflict, ...], tuple[RoleResult, ...]]:
    """Fan-in. Non-success sonuc ve unresolved celiski yutulamaz.

    Dondurulen ucgen: dogrulanmis bulgular, unresolved celiskiler ve non-success
    role sonuclari.
    """

    if not results:
        raise ValidationFailed("sentez icin en az bir role sonucu gerekiyor")
    researcher_refs = frozenset(item.agent_ref for item in results)
    verification.assert_independent(researcher_refs)

    verified = frozenset(verification.verified_finding_ids)
    rejected = frozenset(verification.rejected_finding_ids)
    accepted: list[Finding] = []
    for result in results:
        for finding in result.findings:
            if finding.finding_id in rejected:
                continue
            if finding.finding_id not in verified:
                continue
            accepted.append(finding)

    unresolved = tuple(item for item in conflicts if item.is_unresolved)
    non_success = tuple(item for item in results if not item.is_success)
    return tuple(accepted), unresolved, non_success


@dataclass(frozen=True, slots=True)
class ResearchReport:
    """Arastirma ciktisi. Kanit yetersizse abstain uretir, uydurma yapmaz."""

    report_id: str
    question_id: str
    question_digest: str
    findings: tuple[Finding, ...]
    unresolved_conflicts: tuple[Conflict, ...]
    non_success_results: tuple[RoleResult, ...]
    verification: CitationVerification
    snapshots: tuple[SourceSnapshot, ...]
    status: ReportStatus
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("arastirma raporu authority veremez")
        parse_digest(self.question_digest)
        if self.status is ReportStatus.ANSWERED:
            if not self.findings:
                raise ValidationFailed("answered rapor en az bir dogrulanmis bulgu ister")
            if self.unresolved_conflicts or self.non_success_results:
                raise ValidationFailed(
                    "unresolved celiski veya non-success sonuc varken answered olamaz"
                )
        if self.status is ReportStatus.ABSTAINED and self.findings:
            raise ValidationFailed("abstained rapor bulgu tasiyamaz")
        known = {item.snapshot_id for item in self.snapshots}
        for finding in self.findings:
            for citation in finding.citations:
                if citation.snapshot_id not in known:
                    raise ValidationFailed("citation bilinmeyen snapshot'a isaret ediyor")

    @property
    def is_actionable(self) -> bool:
        """Plan candidate uretmeye uygun mu? Authority ile karistirma."""

        return self.status is ReportStatus.ANSWERED

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-research-report/v1",
            "report_id": self.report_id,
            "question_id": self.question_id,
            "question_digest": self.question_digest,
            "status": str(self.status),
            "findings": [item.as_dict() for item in self.findings],
            "unresolved_conflicts": [item.as_dict() for item in self.unresolved_conflicts],
            "non_success_results": [item.as_dict() for item in self.non_success_results],
            "verification": self.verification.as_dict(),
            "snapshots": [item.body() for item in self.snapshots],
            "grants_authority": False,
        }

    @property
    def report_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return dict(self.body(), report_digest=self.report_digest)


@dataclass(frozen=True, slots=True)
class PlanCandidate:
    """Arastirmadan turetilen plan onerisi. Hicbir kosulda approval tasimaz."""

    candidate_id: str
    report_id: str
    report_digest: str
    work_ref: str
    source_revision: str
    proposed_steps: tuple[str, ...]
    writable_resources: tuple[str, ...]
    acceptance: tuple[str, ...]
    rollback: str
    risk: str
    requires_authorization: bool = True
    approval_inherited: bool = False
    grants_authority: bool = False
    open_questions: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.grants_authority or self.approval_inherited or not self.requires_authorization:
            raise PolicyViolation("plan candidate authority veya approval tasiyamaz")
        parse_digest(self.report_digest)
        if not self.proposed_steps:
            raise ValidationFailed("plan candidate en az bir adim ister")
        if not self.acceptance:
            raise ValidationFailed("plan candidate acceptance ister")
        if not self.rollback.strip():
            raise ValidationFailed("plan candidate rollback ister")
        if self.risk not in {"low", "medium", "high", "critical"}:
            raise ValidationFailed("risk degeri taninmiyor")
        for resource in self.writable_resources:
            _assert_relative(resource.split(":")[-1], "writable resource")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-plan-candidate/v1",
            "candidate_id": self.candidate_id,
            "report_id": self.report_id,
            "report_digest": self.report_digest,
            "work_ref": self.work_ref,
            "source_revision": self.source_revision,
            "proposed_steps": list(self.proposed_steps),
            "writable_resources": list(self.writable_resources),
            "acceptance": list(self.acceptance),
            "rollback": self.rollback,
            "risk": self.risk,
            "open_questions": list(self.open_questions),
            "requires_authorization": True,
            "approval_inherited": False,
            "grants_authority": False,
        }

    @property
    def candidate_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return dict(self.body(), candidate_digest=self.candidate_digest)


def derive_plan_candidate(
    report: ResearchReport,
    *,
    candidate_id: str,
    work_ref: str,
    source_revision: str,
    proposed_steps: tuple[str, ...],
    writable_resources: tuple[str, ...],
    acceptance: tuple[str, ...],
    rollback: str,
    risk: str,
) -> PlanCandidate:
    """Yalniz answered rapordan plan candidate turetir."""

    if not report.is_actionable:
        raise PolicyViolation("actionable olmayan rapordan plan candidate turetilemez")
    return PlanCandidate(
        candidate_id=candidate_id,
        report_id=report.report_id,
        report_digest=report.report_digest,
        work_ref=work_ref,
        source_revision=source_revision,
        proposed_steps=proposed_steps,
        writable_resources=writable_resources,
        acceptance=acceptance,
        rollback=rollback,
        risk=risk,
    )
