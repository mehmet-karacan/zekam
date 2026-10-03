"""Ayri testability refactor kapisi: proposal, onay ve baseline karsilastirma sozlesmeleri (W07).

AKTIF_GOREV 9.4: test-only dongu production koduna dokunamaz. Somut bir testability problemi
kanitlanirsa ayri bir refactor onerisi uretilir; kullanici exact onerisini (digest) onaylamadan
production yazma izni verilmez.

Saf ve I/O'suz kisim (bu modul):

- ``RefactorProposal``: gozlenebilir davranis, denenmis test-only yollar, sorunlu dependency/state,
  en kucuk refactor, etkilenen API/yan etki, risk, rollback, dogrulama plani ve exact hedef
  production dosyalari (path + preimage/postimage digest). Digest'lidir.
- Butce bitmesi tek basina gerekce DEGILDIR: problem kaydi kaynak terminal kanitindaki somut
  testability bulgusuna (finding digest) baglanmak zorundadir; ``BUDGET_EXHAUSTED``/
  ``STAGNATION_REVIEW`` kaynakli oneri bulgusuz gecersizdir (servis katmaninda dogrulanir).
- Is kurali, performans, transaction, concurrency semantigi degisikligi ve yeni ozellik onayin
  parcasi degildir: proposal bunlari beyan ediyorsa ``approvable`` False olur ve onay verilemez.
- ``RefactorApproval``: yalniz o proposal digest'ine, o kaynak revizyonuna, o plan digest'ine ve o
  hedef digest'lerine baglidir; baska birine tasinamaz.
- ``SourceBaseline`` + ``compare_coverage``: refactor sonrasi line/branch paydasi degisebilir;
  yeni baseline olusturulur, eski kanit korunur ve farkli baseline'lar arasi kazanim hesaplanmaz.

Bu modul hicbir yerde davranisin ayni kaldigini ispatladigini iddia etmez: gecen testler ve
bagimsiz inceleme kanit degeri tasir, ispat degildir.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Any, Final

from zekam.application.unit_test_measurement import MeasurementRecord, MeasurementStatus
from zekam.application.unit_test_patch import (
    _DEVICE_RE,
    _PROTECTED_BASENAMES,
    _PROTECTED_NAME_RE,
    _PROTECTED_SEGMENTS,
    _SHORT_NAME_RE,
    fold_path,
)
from zekam.application.unit_test_secret_scan import find_secret_value
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.unit_test_engineering import (
    CoverageMetric,
    CoveragePolicy,
    CoverageState,
    UnitTestRequest,
    UnitTestStopReason,
    normalize_relative_source,
)

PROPOSAL_CONTRACT: Final = "zekam-unit-test-refactor-proposal/v1"
APPROVAL_CONTRACT: Final = "zekam-unit-test-refactor-approval/v1"
BASELINE_CONTRACT: Final = "zekam-unit-test-source-baseline/v1"
LINEAGE_CONTRACT: Final = "zekam-unit-test-refactor-lineage/v1"

MAX_TARGETS: Final = 10
MAX_PROBLEMS: Final = 10
MAX_TEXT: Final = 600
LINEAGE_MARKER: Final = "refactor-lineage:"
VERIFICATION_MARKER: Final = "refactor-verification:"

#: Refactor onerisine kaynak olabilen terminal nedenleri.
PROPOSAL_ORIGINS: Final = frozenset(
    {
        UnitTestStopReason.REFACTOR_APPROVAL_REQUIRED,
        UnitTestStopReason.BUDGET_EXHAUSTED,
        UnitTestStopReason.STAGNATION_REVIEW,
    }
)
#: Butce/plateau kaynakli oneriler: tek basina gerekce degil, bulgu kaniti zorunlu.
BUDGET_LIKE_ORIGINS: Final = frozenset(
    {UnitTestStopReason.BUDGET_EXHAUSTED, UnitTestStopReason.STAGNATION_REVIEW}
)


class RefactorError(PolicyViolation):
    """Refactor kapisi reddi; ``reason`` makine-okunur ayrintidir."""

    code = "unit-test-refactor-refused"

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"Refactor kapisi: {reason}" + (f" ({detail})" if detail else ""))
        self.reason = reason


class ProposalInvalid(RefactorError):
    """Proposal yapisal/kanit olarak gecersiz."""


class ApprovalRefused(RefactorError):
    """Onay verilemez (ornegin ayri kapsam karari gerektiren oneri)."""


class ApprovalMismatch(RefactorError):
    """Onay bu proposal/kaynak/plan/hedef icin gecerli degil; hicbir sey yazilmaz."""


class ScopeViolation(RefactorError):
    """Yazma proposal'daki exact dosyalarin disina cikiyor."""


class IncomparableBaselines(RefactorError):
    """Farkli payda/baseline'lar arasi kazanim hesabi reddedildi."""


class ProblemKind(StrEnum):
    HARD_DEPENDENCY = "hard-dependency"
    STATIC_OR_GLOBAL_STATE = "static-or-global-state"
    AMBIENT_CLOCK_OR_RANDOM = "ambient-clock-or-random"
    EMBEDDED_IO_OR_NETWORK = "embedded-io-or-network"
    CONSTRUCTION_SIDE_EFFECT = "construction-side-effect"
    UNREACHABLE_COLLABORATOR = "unreachable-collaborator"


class OutOfScopeKind(StrEnum):
    """Bu kategorilerden biri gerekiyorsa onay degil AYRI kapsam karari gerekir."""

    BUSINESS_RULE = "business-rule-change"
    PERFORMANCE = "performance-semantics"
    TRANSACTION = "transaction-semantics"
    CONCURRENCY = "concurrency-semantics"
    NEW_FEATURE = "new-feature"


class ReviewArea(StrEnum):
    API = "api"
    CONFIG = "config"
    EXCEPTION = "exception"
    SIDE_EFFECT = "side-effect"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _text(value: object, label: str, *, empty_ok: bool = False) -> str:
    if type(value) is not str:
        raise ProposalInvalid("text-required", label)
    stripped = value.strip()
    if not stripped and not empty_ok:
        raise ProposalInvalid("text-empty", label)
    if len(value) > MAX_TEXT or "\x00" in value:
        raise ProposalInvalid("text-too-long", label)
    return value


def _texts(values: Iterable[str], label: str, *, nonempty: bool) -> tuple[str, ...]:
    result = tuple(_text(v, label) for v in values)
    if nonempty and not result:
        raise ProposalInvalid("list-empty", label)
    return result


def check_production_target(path: str) -> None:
    """Hedef gercek production dosyasi olmali: test agaci/build ayari/korumali dizin yok."""

    try:
        normalize_relative_source(path)
    except ValidationFailed as exc:
        raise ScopeViolation("invalid-path", path) from exc
    folded = fold_path(path)
    segments = folded.split("/")
    if any(seg == "" or ":" in seg for seg in segments):
        raise ScopeViolation("invalid-path-component", path)
    for segment in segments:
        if _SHORT_NAME_RE.match(segment):
            raise ScopeViolation("windows-short-name", path)
        if _DEVICE_RE.match(segment.split(".")[0].rstrip(". ")):
            raise ScopeViolation("windows-device-name", path)
    if any(segments[i] == "src" and segments[i + 1] == "test" for i in range(len(segments) - 1)):
        raise ScopeViolation("test-tree-path", path)
    base = segments[-1]
    if base in _PROTECTED_BASENAMES or _PROTECTED_NAME_RE.search(base):
        raise ScopeViolation("build-or-coverage-config", path)
    if any(seg in _PROTECTED_SEGMENTS for seg in segments):
        raise ScopeViolation("protected-directory", path)


@dataclass(frozen=True, slots=True)
class TestabilityProblem:
    """Hangi gozlenebilir davranis neden test edilemiyor; kaynak kanita bagli."""

    __test__ = False

    observable: str
    kind: ProblemKind
    dependency_or_state: str
    tried_test_only: tuple[str, ...]
    finding_digest: str
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.observable, "observable")
        if not isinstance(self.kind, ProblemKind):
            raise ProposalInvalid("kind-enum", "problem.kind")
        _text(self.dependency_or_state, "dependency_or_state")
        object.__setattr__(
            self, "tried_test_only", _texts(self.tried_test_only, "tried_test_only", nonempty=True)
        )
        object.__setattr__(
            self, "source_refs", _texts(self.source_refs, "source_refs", nonempty=True)
        )
        parse_digest(self.finding_digest)

    def to_payload(self) -> dict[str, Any]:
        return {
            "observable": self.observable,
            "kind": self.kind.value,
            "dependency_or_state": self.dependency_or_state,
            "tried_test_only": list(self.tried_test_only),
            "finding_digest": self.finding_digest,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True, slots=True)
class ProposalTarget:
    """Exact hedef production dosyasi. ``preimage_digest=None`` yeni dosya demektir."""

    path: str
    preimage_digest: str | None
    postimage_digest: str

    def __post_init__(self) -> None:
        check_production_target(self.path)
        if self.preimage_digest is not None:
            parse_digest(self.preimage_digest)
        parse_digest(self.postimage_digest)
        if self.preimage_digest == self.postimage_digest:
            raise ProposalInvalid("no-op-target", self.path)

    def to_payload(self) -> list[str | None]:
        return [self.path, self.preimage_digest, self.postimage_digest]


@dataclass(frozen=True, slots=True)
class ImpactDeclaration:
    """Etkilenebilecek API/config/exception/yan etki beyani (reviewer bununla karsilastirir)."""

    area: ReviewArea
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.area, ReviewArea):
            raise ProposalInvalid("area-enum", "impact.area")
        _text(self.description, "impact.description")

    def to_payload(self) -> list[str]:
        return [self.area.value, self.description]


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    """Refactor sonrasi dogrulama: mevcut testler, characterization, regression, inceleme."""

    existing_tests: bool
    characterization_refs: tuple[str, ...]
    regression_scope: tuple[str, ...]
    review_areas: tuple[ReviewArea, ...]

    def __post_init__(self) -> None:
        if self.existing_tests is not True:
            raise ProposalInvalid("existing-tests-required", "verification")
        object.__setattr__(
            self,
            "characterization_refs",
            _texts(self.characterization_refs, "characterization_refs", nonempty=True),
        )
        for item in self.regression_scope:
            normalize_relative_source(item)
        areas = tuple(self.review_areas)
        if set(areas) != set(ReviewArea) or len(areas) != len(set(areas)):
            raise ProposalInvalid("review-areas-incomplete", "api/config/exception/side-effect")
        object.__setattr__(self, "review_areas", tuple(sorted(areas, key=lambda a: a.value)))

    def to_payload(self) -> dict[str, Any]:
        return {
            "existing_tests": True,
            "characterization_refs": list(self.characterization_refs),
            "regression_scope": list(self.regression_scope),
            "review_areas": [a.value for a in self.review_areas],
        }


@dataclass(frozen=True, slots=True)
class RefactorOrigin:
    """Onerinin turedigi terminal: istek, neden, kanit nesnesi ve davranis plani."""

    request_digest: str
    stop_reason: UnitTestStopReason
    terminal_evidence_digest: str
    plan_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.request_digest)
        parse_digest(self.terminal_evidence_digest)
        parse_digest(self.plan_digest)
        if self.stop_reason not in PROPOSAL_ORIGINS:
            raise ProposalInvalid("origin-not-eligible", self.stop_reason.value)

    def to_payload(self) -> dict[str, str]:
        return {
            "request_digest": self.request_digest,
            "stop_reason": self.stop_reason.value,
            "terminal_evidence_digest": self.terminal_evidence_digest,
            "plan_digest": self.plan_digest,
        }


@dataclass(frozen=True, slots=True)
class RefactorProposal:
    """Exact scope + digest'li testability refactor onerisi (yetki DOGURMAZ)."""

    origin: RefactorOrigin
    source_revision: str
    problems: tuple[TestabilityProblem, ...]
    technique: str
    minimal_change: str
    impact: tuple[ImpactDeclaration, ...]
    risk: RiskLevel
    risk_notes: str
    rollback_plan: str
    verification: VerificationPlan
    targets: tuple[ProposalTarget, ...]
    out_of_scope: tuple[OutOfScopeKind, ...] = ()

    def __post_init__(self) -> None:
        _text(self.source_revision, "source_revision")
        if not self.problems or len(self.problems) > MAX_PROBLEMS:
            raise ProposalInvalid("problems-required", f"1..{MAX_PROBLEMS}")
        _text(self.technique, "technique")
        _text(self.minimal_change, "minimal_change")
        _text(self.risk_notes, "risk_notes")
        _text(self.rollback_plan, "rollback_plan")
        if not isinstance(self.risk, RiskLevel):
            raise ProposalInvalid("risk-enum", "risk")
        if not self.targets or len(self.targets) > MAX_TARGETS:
            raise ProposalInvalid("targets-required", f"1..{MAX_TARGETS}")
        paths = [t.path for t in self.targets]
        if len({fold_path(p) for p in paths}) != len(paths):
            raise ProposalInvalid("duplicate-target", "buyuk-kucuk harf dahil")
        object.__setattr__(self, "targets", tuple(sorted(self.targets, key=lambda t: t.path)))
        object.__setattr__(
            self,
            "impact",
            tuple(sorted(self.impact, key=lambda i: (i.area.value, i.description))),
        )
        object.__setattr__(
            self, "out_of_scope", tuple(sorted(set(self.out_of_scope), key=lambda k: k.value))
        )
        if find_secret_value(self.to_payload()) is not None:
            raise ProposalInvalid("secret-value", "proposal")

    @property
    def approvable(self) -> bool:
        """Ayri kapsam karari gerektiren oneri onaylanamaz."""

        return not self.out_of_scope

    @property
    def target_paths(self) -> tuple[str, ...]:
        return tuple(t.path for t in self.targets)

    def declares(self, area: ReviewArea) -> bool:
        return any(i.area is area for i in self.impact)

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract": PROPOSAL_CONTRACT,
            "origin": self.origin.to_payload(),
            "source_revision": self.source_revision,
            "problems": [p.to_payload() for p in self.problems],
            "technique": self.technique,
            "minimal_change": self.minimal_change,
            "impact": [i.to_payload() for i in self.impact],
            "risk": self.risk.value,
            "risk_notes": self.risk_notes,
            "rollback_plan": self.rollback_plan,
            "verification": self.verification.to_payload(),
            "targets": [t.to_payload() for t in self.targets],
            "out_of_scope": [k.value for k in self.out_of_scope],
        }

    @property
    def proposal_digest(self) -> str:
        return digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class UserApprovalDecision:
    """Kullanicinin sohbetteki exact onayi; ``proposal_digest`` kullanicinin gordugu digest'tir."""

    proposal_digest: str
    decision_ref: str

    def __post_init__(self) -> None:
        parse_digest(self.proposal_digest)
        if type(self.decision_ref) is not str or not self.decision_ref.strip():
            raise ApprovalRefused("decision-ref-required")


@dataclass(frozen=True, slots=True)
class RefactorApproval:
    """Onay yalniz proposal digest'ine, kaynak revizyonuna, plana ve hedef digest'lerine bagli."""

    proposal_digest: str
    source_revision: str
    plan_digest: str
    targets: tuple[tuple[str, str | None, str], ...]
    decision_ref: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract": APPROVAL_CONTRACT,
            "proposal_digest": self.proposal_digest,
            "source_revision": self.source_revision,
            "plan_digest": self.plan_digest,
            "targets": [list(t) for t in self.targets],
            "decision_ref": self.decision_ref,
        }

    @property
    def approval_digest(self) -> str:
        return digest(self.to_payload())

    def verify_for(
        self,
        proposal: RefactorProposal,
        *,
        current_source_revision: str,
        current_target_digests: Mapping[str, str | None],
    ) -> None:
        """Onay baska proposal/revizyon/plan/hedefe TASINMAZ; her uyusmazlik yazmadan reddedilir."""

        if self.proposal_digest != proposal.proposal_digest:
            raise ApprovalMismatch("proposal-digest-mismatch")
        if not proposal.approvable:
            raise ApprovalMismatch("scope-decision-required")
        if self.source_revision != proposal.source_revision:
            raise ApprovalMismatch("approval-source-revision-differs")
        if self.plan_digest != proposal.origin.plan_digest:
            raise ApprovalMismatch("plan-digest-mismatch")
        expected = tuple((t.path, t.preimage_digest, t.postimage_digest) for t in proposal.targets)
        if self.targets != expected:
            raise ApprovalMismatch("target-digests-mismatch")
        if current_source_revision != self.source_revision:
            raise ApprovalMismatch("source-revision-changed")
        for target in proposal.targets:
            if current_target_digests.get(target.path) != target.preimage_digest:
                raise ApprovalMismatch("production-digest-changed", target.path)


def grant_approval(proposal: RefactorProposal, decision: UserApprovalDecision) -> RefactorApproval:
    """Kullanici kararini exact proposal'a baglar. Ayri kapsam karari gereken oneri onaylanamaz."""

    if decision.proposal_digest != proposal.proposal_digest:
        raise ApprovalMismatch("decision-for-other-proposal")
    if not proposal.approvable:
        kinds = ",".join(k.value for k in proposal.out_of_scope)
        raise ApprovalRefused("scope-decision-required", kinds)
    return RefactorApproval(
        proposal_digest=proposal.proposal_digest,
        source_revision=proposal.source_revision,
        plan_digest=proposal.origin.plan_digest,
        targets=tuple((t.path, t.preimage_digest, t.postimage_digest) for t in proposal.targets),
        decision_ref=decision.decision_ref,
    )


# -- baseline ve karsilastirma -----------------------------------------------------------


def _pairs(items: Iterable[tuple[str, str]], label: str) -> tuple[tuple[str, str], ...]:
    result = tuple(sorted((str(a), str(b)) for a, b in items))
    if len({a for a, _ in result}) != len(result):
        raise ValidationFailed(f"{label} tekrar eden anahtar")
    return result


@dataclass(frozen=True, slots=True)
class SourceBaseline:
    """Belirli bir kaynak durumundaki tamsayi sayaclar; payda (``total``) kimligin PARCASIDIR.

    ``lineage_digest`` refactor sonrasi uretilen yeni baseline'i eskisinden ayirir; production
    digest'leri ayni kalsa bile refactor kuşaklari arasinda dogrudan kazanim hesaplanmaz.
    """

    request_digest: str
    source_revision: str
    metric: CoverageMetric
    policy: CoveragePolicy
    production_digests: tuple[tuple[str, str], ...]
    counters: tuple[tuple[str, int, int], ...]
    measurement_digest: str | None
    build_config_digest: str | None = None
    toolchain_digest: str | None = None
    lineage_digest: str | None = None

    def __post_init__(self) -> None:
        parse_digest(self.request_digest)
        if not self.source_revision.strip():
            raise ValidationFailed("source_revision bos olamaz")
        object.__setattr__(
            self, "production_digests", _pairs(self.production_digests, "production_digests")
        )
        rows = tuple(sorted((str(f), int(c), int(t)) for f, c, t in self.counters))
        if len({f for f, _, _ in rows}) != len(rows):
            raise ValidationFailed("counters tekrar eden dosya")
        for _, covered, total in rows:
            if covered < 0 or total <= 0 or covered > total:
                raise ValidationFailed("counters 0 <= covered <= total, total > 0 olmali")
        if not rows:
            raise ValidationFailed("Baseline en az bir olculmus dosya ister")
        object.__setattr__(self, "counters", rows)
        for optional in (self.measurement_digest, self.lineage_digest):
            if optional is not None:
                parse_digest(optional)

    @property
    def denominators(self) -> tuple[tuple[str, int], ...]:
        return tuple((f, t) for f, _, t in self.counters)

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract": BASELINE_CONTRACT,
            "request_digest": self.request_digest,
            "source_revision": self.source_revision,
            "metric": self.metric.value,
            "policy": self.policy.value,
            "production_digests": [list(p) for p in self.production_digests],
            "counters": [list(c) for c in self.counters],
            "measurement_digest": self.measurement_digest,
            "build_config_digest": self.build_config_digest,
            "toolchain_digest": self.toolchain_digest,
            "lineage_digest": self.lineage_digest,
        }

    @property
    def baseline_digest(self) -> str:
        return digest(self.to_payload())

    @property
    def baseline_id(self) -> str:
        """Karsilastirilabilirlik kimligi: metrik, politika, dosya+payda, production, kusak."""

        return digest(
            {
                "metric": self.metric.value,
                "policy": self.policy.value,
                "denominators": [list(d) for d in self.denominators],
                "production_digests": [list(p) for p in self.production_digests],
                "lineage_digest": self.lineage_digest,
            }
        )

    @classmethod
    def from_measurement(
        cls,
        request: UnitTestRequest,
        record: MeasurementRecord,
        *,
        source_revision: str,
        lineage_digest: str | None = None,
    ) -> SourceBaseline:
        """Yalniz kabul edilmis, bagli olcumden; olculmemis/N/A dosya sayac uretmez."""

        if record.status is not MeasurementStatus.ACCEPTED or record.binding is None:
            raise ValidationFailed("Baseline yalniz kabul edilmis, bagli olcumden uretilir")
        rows = [
            (o.source_file, o.covered, o.total)
            for o in record.evidence.observations
            if o.metric is request.metric
            and o.state is CoverageState.MEASURED
            and o.covered is not None
            and o.total is not None
        ]
        return cls(
            request_digest=request.request_digest,
            source_revision=source_revision,
            metric=request.metric,
            policy=request.policy,
            production_digests=record.binding.production_digests,
            counters=tuple(rows),
            measurement_digest=record.record_digest,
            build_config_digest=record.binding.build_config_digest,
            toolchain_digest=record.binding.toolchain.toolchain_digest,
            lineage_digest=lineage_digest,
        )

    @classmethod
    def from_loop_state(
        cls,
        request: UnitTestRequest,
        state: Mapping[str, Any],
        *,
        lineage_digest: str | None = None,
    ) -> SourceBaseline | None:
        """Kaynak terminal kanitindaki (degismeyen) dongu durumundan ESKI baseline; yoksa None."""

        observation = state.get("baseline_obs")
        accepted = state.get("accepted_observations") or []
        if not observation or not accepted:
            return None
        measurement = state.get("accepted_measurement_digest")
        return cls(
            request_digest=request.request_digest,
            source_revision=str(observation["source_revision"]),
            metric=request.metric,
            policy=request.policy,
            production_digests=tuple(
                (str(a), str(b)) for a, b in observation["production_digests"]
            ),
            counters=tuple((str(f), int(c), int(t)) for f, c, t in accepted),
            measurement_digest=None if measurement is None else str(measurement),
            build_config_digest=str(observation["build_config_digest"]),
            toolchain_digest=str(observation["launcher_digest"]),
            lineage_digest=lineage_digest,
        )


def incomparability_reasons(before: SourceBaseline, after: SourceBaseline) -> tuple[str, ...]:
    reasons: list[str] = []
    if before.metric is not after.metric:
        reasons.append("metric")
    if before.policy is not after.policy:
        reasons.append("policy")
    if before.denominators != after.denominators:
        reasons.append("denominator")
    if before.production_digests != after.production_digests:
        reasons.append("production-digests")
    if before.lineage_digest != after.lineage_digest:
        reasons.append("refactor-lineage")
    return tuple(reasons)


@dataclass(frozen=True, slots=True)
class CoverageGain:
    """Ayni baseline icindeki iki sayac ani arasi tamsayi kazanim."""

    baseline_id: str
    per_file: tuple[tuple[str, int], ...]
    covered_delta: int
    total: int

    @property
    def ratio_delta(self) -> Fraction:
        return Fraction(self.covered_delta, self.total)


def compare_coverage(before: SourceBaseline, after: SourceBaseline) -> CoverageGain:
    """Yalniz AYNI baseline icinde kazanim hesaplar; farkli payda/baseline reddedilir.

    Refactor sonrasi yeni baseline ile eski baseline arasinda (ya da eski yuzdeyle) dogrudan
    kazanim hesabi yapilmaz; yeni baseline'dan itibaren sayaclar kendi aralarinda karsilastirilir.
    """

    reasons = incomparability_reasons(before, after)
    if reasons:
        raise IncomparableBaselines("different-baselines", ",".join(reasons))
    per_file = tuple(
        (f, c_after - c_before)
        for (f, c_before, _), (_, c_after, _) in zip(before.counters, after.counters, strict=True)
    )
    return CoverageGain(
        before.baseline_id,
        per_file,
        sum(delta for _, delta in per_file),
        sum(t for _, t in before.denominators),
    )


# -- iliskili istekler -------------------------------------------------------------------


def lineage_digest(
    *,
    origin_request_digest: str,
    proposal_digest: str,
    approval_digest: str,
    verification_request_digest: str,
    post_target_digests: Iterable[tuple[str, str]],
    old_baseline_id: str | None,
) -> str:
    return digest(
        {
            "contract": LINEAGE_CONTRACT,
            "origin_request_digest": origin_request_digest,
            "proposal_digest": proposal_digest,
            "approval_digest": approval_digest,
            "verification_request_digest": verification_request_digest,
            "post_target_digests": sorted([a, b] for a, b in post_target_digests),
            "old_baseline_id": old_baseline_id,
        }
    )


def _hex(value: str) -> str:
    return value.split(":", 1)[-1][:32]


def derive_verification_request(
    origin: UnitTestRequest, *, proposal_digest: str, approval_digest: str
) -> UnitTestRequest:
    """Refactor dogrulamasinin kendi istek kimligi: origin'in final terminal'ine dokunmaz."""

    return _clone(
        origin,
        origin.source_revision,
        (*origin.defaults_applied, f"{VERIFICATION_MARKER}{_hex(proposal_digest)}"),
        approval_digest,
    )


def derive_followup_request(
    origin: UnitTestRequest, *, source_revision: str, lineage: str
) -> UnitTestRequest:
    """Ayni hedef politikasi (metrik/esik/politika/butce/kapsam) ile yeni kaynak istegi."""

    return _clone(
        origin, source_revision, (*origin.defaults_applied, f"{LINEAGE_MARKER}{_hex(lineage)}")
    )


def _clone(
    origin: UnitTestRequest,
    source_revision: str,
    defaults: tuple[str, ...],
    salt: str | None = None,
) -> UnitTestRequest:
    applied = defaults if salt is None else (*defaults, f"approval:{_hex(salt)}")
    return UnitTestRequest(
        project_id=origin.project_id,
        source_binding_id=origin.source_binding_id,
        source_revision=source_revision,
        source_files=origin.source_files,
        metric=origin.metric,
        threshold=origin.threshold,
        policy=origin.policy,
        budget=origin.budget,
        allowed_test_paths=origin.allowed_test_paths,
        forbidden_paths=origin.forbidden_paths,
        regression_scope=origin.regression_scope,
        defaults_applied=applied,
    )
