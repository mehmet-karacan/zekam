"""Unit-test davranis plani: senaryo/risk, oracle kaynagi, oncelik, needs-specification (W05).

Plan bir test KOTASI degildir. Her senaryo bir davranis sozlesmesi, risk, beklenen
gozlemin dayanagi (oracle), oncelik ve kabul kriteri tasir. "Su dosya icin tam N test"
bagli hedefi yoktur; tahmin yalniz belirsiz, kalibre edilecek bir ongorudur ve plan
payload'inda ``test_count`` benzeri bir kota alani KABUL EDILMEZ.

Oracle belirsizligi yalniz ilgili senaryoyu ``NEEDS_SPECIFICATION`` yapar; ilgisiz
guvenli senaryolar surer. Planner ciktisi strict dogrulanir; bilinmeyen alan reddedilir.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Final

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.unit_test_engineering import UnitTestRequest, normalize_relative_source

PLAN_CONTRACT: Final = "zekam-unit-test-behavior-plan/v1"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_FORBIDDEN_QUOTA_KEYS: Final = frozenset(
    {"test_count", "tests_required", "required_tests", "exact_tests", "min_tests", "num_tests"}
)
MAX_TEXT: Final = 400
MAX_SCENARIOS: Final = 64


class OracleKind(StrEnum):
    EXPLICIT_CONTRACT = "explicit-contract"
    NORMATIVE_DOC_OR_TEST = "normative-doc-or-test"
    INVARIANT = "invariant"
    CHARACTERIZATION = "characterization"
    UNRESOLVED_ASSUMPTION = "unresolved-assumption"


#: Production kodundan bagimsiz sayilabilecek (yine de otomatik dogru olmayan) kaynaklar.
INDEPENDENT_ORACLES: Final = frozenset(
    {OracleKind.EXPLICIT_CONTRACT, OracleKind.NORMATIVE_DOC_OR_TEST, OracleKind.INVARIANT}
)


class OracleConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ScenarioPriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"critical": 0, "high": 1, "normal": 2, "low": 3}[self.value]


class VerificationMethod(StrEnum):
    EXAMPLE = "example"
    PARAMETERIZED = "parameterized"
    PROPERTY = "property"
    METAMORPHIC = "metamorphic"
    STATEFUL = "stateful"


class ScenarioState(StrEnum):
    PLANNED = "planned"
    NEEDS_SPECIFICATION = "needs-specification"
    PRODUCTION_DEFECT = "production-defect"
    FLAKY_QUARANTINED = "flaky-quarantined"
    UNRESOLVED = "unresolved"
    VERIFIED = "verified"
    ACCEPTED_EXCEPTION = "accepted-exception"


class RiskStatus(StrEnum):
    VERIFIED = "verified"
    UNRESOLVED = "unresolved"
    ACCEPTED_EXCEPTION = "accepted-exception"


def risk_status(state: ScenarioState) -> RiskStatus:
    if state is ScenarioState.VERIFIED:
        return RiskStatus.VERIFIED
    if state is ScenarioState.ACCEPTED_EXCEPTION:
        return RiskStatus.ACCEPTED_EXCEPTION
    return RiskStatus.UNRESOLVED


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ValidationFailed(f"{label} metin olmali")
    cleaned = value.strip()
    if not cleaned and not allow_empty:
        raise ValidationFailed(f"{label} bos olamaz")
    if len(cleaned) > MAX_TEXT or any(c in cleaned for c in ("\r", "\n", "\x00")):
        raise ValidationFailed(f"{label} tek satir ve en fazla {MAX_TEXT} karakter olmali")
    return cleaned


def _scenario_id(value: object, label: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        raise ValidationFailed(f"{label} gecersiz kimlik")
    return value


@dataclass(frozen=True, slots=True)
class ContributionEstimate:
    """Belirsiz, kalibre edilecek line katki ongorusu; garanti veya hedef degildir."""

    low_lines: int | None = None
    high_lines: int | None = None

    def __post_init__(self) -> None:
        for value in (self.low_lines, self.high_lines):
            if value is not None and (type(value) is not int or value < 0):
                raise ValidationFailed("Katki tahmini negatif olmayan tamsayi olmali")
        if (
            self.low_lines is not None
            and self.high_lines is not None
            and self.low_lines > self.high_lines
        ):
            raise ValidationFailed("Katki tahmini araligi ters")

    def to_payload(self) -> dict[str, Any]:
        return {"low_lines": self.low_lines, "high_lines": self.high_lines, "forecast": True}


@dataclass(frozen=True, slots=True)
class BehaviorScenario:
    scenario_id: str
    risk_id: str
    behavior_contract: str
    source_files: tuple[str, ...]
    oracle_kind: OracleKind
    oracle_ref: str
    oracle_confidence: OracleConfidence
    priority: ScenarioPriority
    method: VerificationMethod
    acceptance: str
    fixture_needs: tuple[str, ...] = ()
    estimate: ContributionEstimate | None = None
    material: bool | None = None

    def __post_init__(self) -> None:
        _scenario_id(self.scenario_id, "scenario_id")
        _scenario_id(self.risk_id, "risk_id")
        _text(self.behavior_contract, "behavior_contract")
        _text(self.acceptance, "acceptance")
        object.__setattr__(
            self, "source_files", tuple(normalize_relative_source(p) for p in self.source_files)
        )
        if not self.source_files:
            raise ValidationFailed("Senaryo en az bir kaynak dosyasi ister")
        for need in self.fixture_needs:
            _text(need, "fixture_need")
        _text(self.oracle_ref, "oracle_ref", allow_empty=True)
        if self.oracle_kind is not OracleKind.UNRESOLVED_ASSUMPTION and not self.oracle_ref.strip():
            raise ValidationFailed("Cozulmus oracle kaynagi referans tasimali")
        if self.material is None:
            object.__setattr__(
                self,
                "material",
                self.priority in {ScenarioPriority.CRITICAL, ScenarioPriority.HIGH},
            )

    @property
    def is_material(self) -> bool:
        return bool(self.material)

    @property
    def initial_state(self) -> ScenarioState:
        """Cozulmemis varsayim ile beklenen sonuc uretilemez: karar gerektirir."""

        if self.oracle_kind is OracleKind.UNRESOLVED_ASSUMPTION:
            return ScenarioState.NEEDS_SPECIFICATION
        return ScenarioState.PLANNED

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "risk_id": self.risk_id,
            "behavior_contract": self.behavior_contract,
            "source_files": list(self.source_files),
            "oracle_kind": self.oracle_kind.value,
            "oracle_ref": self.oracle_ref,
            "oracle_confidence": self.oracle_confidence.value,
            "priority": self.priority.value,
            "method": self.method.value,
            "acceptance": self.acceptance,
            "fixture_needs": list(self.fixture_needs),
            "estimate": None if self.estimate is None else self.estimate.to_payload(),
            "material": self.is_material,
        }

    @property
    def scenario_digest(self) -> str:
        return digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class BehaviorPlan:
    request_digest: str
    version: int
    scenarios: tuple[BehaviorScenario, ...]
    rejected_approach_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parse_digest(self.request_digest)
        if type(self.version) is not int or self.version < 1:
            raise ValidationFailed("Plan surumu 1 veya buyuk olmali")
        if not self.scenarios or len(self.scenarios) > MAX_SCENARIOS:
            raise ValidationFailed(f"Plan 1..{MAX_SCENARIOS} senaryo tasimali")
        ids = [s.scenario_id for s in self.scenarios]
        if len(set(ids)) != len(ids):
            raise ValidationFailed("Plan senaryo kimlikleri tekrar edemez")
        for item in self.rejected_approach_digests:
            parse_digest(item)

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract": PLAN_CONTRACT,
            "request_digest": self.request_digest,
            "version": self.version,
            "scenarios": [s.to_payload() for s in self.scenarios],
            "rejected_approach_digests": list(self.rejected_approach_digests),
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.to_payload())

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], request: UnitTestRequest) -> BehaviorPlan:
        allowed = frozenset(request.source_files)
        return cls(
            request_digest=str(payload["request_digest"]),
            version=int(payload["version"]),
            scenarios=tuple(
                parse_scenario(item, allowed_sources=allowed) for item in payload["scenarios"]
            ),
            rejected_approach_digests=tuple(str(d) for d in payload["rejected_approach_digests"]),
        )

    def scenario(self, scenario_id: str) -> BehaviorScenario:
        for item in self.scenarios:
            if item.scenario_id == scenario_id:
                return item
        raise ValidationFailed("Plan'da olmayan senaryo")

    def with_scenarios(self, extra: Iterable[BehaviorScenario]) -> BehaviorPlan:
        known = {s.scenario_id for s in self.scenarios}
        added = tuple(s for s in extra if s.scenario_id not in known)
        return replace(self, version=self.version + 1, scenarios=(*self.scenarios, *added))

    def with_rejected(self, approach_digests: Iterable[str]) -> BehaviorPlan:
        merged = tuple(dict.fromkeys((*self.rejected_approach_digests, *approach_digests)))
        return replace(self, rejected_approach_digests=merged)


@dataclass(frozen=True, slots=True)
class ScenarioRecord:
    scenario_digest: str
    state: ScenarioState
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ScenarioBoard:
    """Senaryo durumlari. Durum yalniz ayni ``scenario_digest`` icin gecerlidir."""

    records: tuple[tuple[str, ScenarioRecord], ...] = ()

    @classmethod
    def initial(cls, plan: BehaviorPlan) -> ScenarioBoard:
        return cls(
            tuple(
                (s.scenario_id, ScenarioRecord(s.scenario_digest, s.initial_state))
                for s in plan.scenarios
            )
        )

    def as_map(self) -> dict[str, ScenarioRecord]:
        return dict(self.records)

    def state_of(self, scenario_id: str) -> ScenarioState:
        record = self.as_map().get(scenario_id)
        if record is None:
            raise ValidationFailed("Board'da olmayan senaryo")
        return record.state

    def with_state(self, scenario_id: str, state: ScenarioState, reason: str = "") -> ScenarioBoard:
        current = self.as_map()
        old = current.get(scenario_id)
        if old is None:
            raise ValidationFailed("Board'da olmayan senaryo")
        if state is ScenarioState.ACCEPTED_EXCEPTION:
            raise PolicyViolation("Model kendine istisna veremez; yalniz kullanici kaydi")
        current[scenario_id] = ScenarioRecord(old.scenario_digest, state, reason[:160])
        return ScenarioBoard(tuple(sorted(current.items())))

    def carry_over(self, plan: BehaviorPlan, *, keep_verified: bool) -> ScenarioBoard:
        """Yeni plana gec. Degisen senaryonun eski durumu/basarisi TASINMAZ (M13)."""

        old = self.as_map()
        result: list[tuple[str, ScenarioRecord]] = []
        for scenario in plan.scenarios:
            previous = old.get(scenario.scenario_id)
            if (
                previous is not None
                and previous.scenario_digest == scenario.scenario_digest
                and (keep_verified or previous.state is not ScenarioState.VERIFIED)
            ):
                result.append((scenario.scenario_id, previous))
            else:
                result.append(
                    (
                        scenario.scenario_id,
                        ScenarioRecord(scenario.scenario_digest, scenario.initial_state),
                    )
                )
        return ScenarioBoard(tuple(sorted(result)))

    def reset_verified(self, plan: BehaviorPlan) -> ScenarioBoard:
        """Harici drift: dogrulanmis durumlar yeniden planlanir (eski basari tasinmaz)."""

        board = self
        for scenario_id, record in self.records:
            if record.state is ScenarioState.VERIFIED:
                board = board.with_state(scenario_id, ScenarioState.PLANNED, "source-drift")
        del plan
        return board

    def to_payload(self) -> list[list[str]]:
        return [[sid, r.scenario_digest, r.state.value, r.reason] for sid, r in self.records]

    @classmethod
    def from_payload(cls, payload: Iterable[Iterable[str]]) -> ScenarioBoard:
        rows: list[tuple[str, ScenarioRecord]] = []
        for row in payload:
            sid, sdigest, state, reason = list(row)
            rows.append((sid, ScenarioRecord(sdigest, ScenarioState(state), reason)))
        return cls(tuple(sorted(rows)))


def actionable_scenarios(plan: BehaviorPlan, board: ScenarioBoard) -> tuple[BehaviorScenario, ...]:
    """Sirali (oncelik, kimlik) uygulanabilir senaryolar; needs-spec/defect/flaky disinda."""

    planned = [s for s in plan.scenarios if board.state_of(s.scenario_id) is ScenarioState.PLANNED]
    return tuple(sorted(planned, key=lambda s: (s.priority.rank, s.scenario_id)))


def scenarios_in_state(
    plan: BehaviorPlan, board: ScenarioBoard, *states: ScenarioState
) -> tuple[BehaviorScenario, ...]:
    wanted = set(states)
    return tuple(s for s in plan.scenarios if board.state_of(s.scenario_id) in wanted)


def unresolved_material(plan: BehaviorPlan, board: ScenarioBoard) -> tuple[BehaviorScenario, ...]:
    """Kullanici istisnasi veya dogrulama olmayan materyal riskler (kalite gecildi denemez)."""

    return tuple(
        s
        for s in plan.scenarios
        if s.is_material and risk_status(board.state_of(s.scenario_id)) is RiskStatus.UNRESOLVED
    )


# -- strict parse ------------------------------------------------------------------------

_SCENARIO_KEYS: Final = frozenset(
    {
        "scenario_id",
        "risk_id",
        "behavior_contract",
        "source_files",
        "oracle_kind",
        "oracle_ref",
        "oracle_confidence",
        "priority",
        "method",
        "acceptance",
        "fixture_needs",
        "estimate",
        "material",
    }
)
_SCENARIO_REQUIRED: Final = frozenset(
    {
        "scenario_id",
        "risk_id",
        "behavior_contract",
        "source_files",
        "oracle_kind",
        "oracle_confidence",
        "priority",
        "method",
        "acceptance",
    }
)


def _enum[E: StrEnum](cls: type[E], value: object, label: str) -> E:
    try:
        return cls(str(value))
    except ValueError as exc:
        raise ValidationFailed(f"{label} gecersiz deger") from exc


def parse_scenario(
    document: Mapping[str, Any], *, allowed_sources: frozenset[str] | None = None
) -> BehaviorScenario:
    if not isinstance(document, Mapping):
        raise ValidationFailed("Senaryo sozluk olmali")
    quota = _FORBIDDEN_QUOTA_KEYS & set(document)
    if quota:
        raise PolicyViolation("Plan test kotasi degildir; kota alani kabul edilmez")
    unknown = set(document) - _SCENARIO_KEYS
    if unknown:
        raise ValidationFailed("Senaryo bilinmeyen alan tasiyor")
    missing = _SCENARIO_REQUIRED - set(document)
    if missing:
        raise ValidationFailed("Senaryo zorunlu alanlari eksik")
    sources = document["source_files"]
    if not isinstance(sources, list | tuple):
        raise ValidationFailed("source_files liste olmali")
    needs = document.get("fixture_needs", [])
    if not isinstance(needs, list | tuple):
        raise ValidationFailed("fixture_needs liste olmali")
    estimate_doc = document.get("estimate")
    estimate: ContributionEstimate | None = None
    if estimate_doc is not None:
        if not isinstance(estimate_doc, Mapping) or set(estimate_doc) - {
            "low_lines",
            "high_lines",
            "forecast",
        }:
            raise ValidationFailed("Katki tahmini yalniz low_lines/high_lines tasir")
        estimate = ContributionEstimate(
            estimate_doc.get("low_lines"), estimate_doc.get("high_lines")
        )
    material = document.get("material")
    if material is not None and type(material) is not bool:
        raise ValidationFailed("material mantiksal olmali")
    scenario = BehaviorScenario(
        scenario_id=_scenario_id(document["scenario_id"], "scenario_id"),
        risk_id=_scenario_id(document["risk_id"], "risk_id"),
        behavior_contract=_text(document["behavior_contract"], "behavior_contract"),
        source_files=tuple(str(p) for p in sources),
        oracle_kind=_enum(OracleKind, document["oracle_kind"], "oracle_kind"),
        oracle_ref=_text(document.get("oracle_ref", ""), "oracle_ref", allow_empty=True),
        oracle_confidence=_enum(OracleConfidence, document["oracle_confidence"], "confidence"),
        priority=_enum(ScenarioPriority, document["priority"], "priority"),
        method=_enum(VerificationMethod, document["method"], "method"),
        acceptance=_text(document["acceptance"], "acceptance"),
        fixture_needs=tuple(str(n) for n in needs),
        estimate=estimate,
        material=material,
    )
    if allowed_sources is not None and not set(scenario.source_files) <= allowed_sources:
        raise PolicyViolation("Senaryo istek scope'u disinda dosyaya baglanamaz")
    return scenario


def parse_behavior_plan(
    document: Mapping[str, Any], request: UnitTestRequest, *, version: int
) -> BehaviorPlan:
    """Planner ciktisini strict dogrular; kota/bilinmeyen alan reddedilir."""

    if not isinstance(document, Mapping):
        raise ValidationFailed("Plan sozluk olmali")
    if _FORBIDDEN_QUOTA_KEYS & set(document):
        raise PolicyViolation("Plan test kotasi degildir; kota alani kabul edilmez")
    unknown = set(document) - {"scenarios", "size_note", "rejected_approach_digests"}
    if unknown:
        raise ValidationFailed("Plan bilinmeyen alan tasiyor")
    raw = document.get("scenarios")
    if not isinstance(raw, list | tuple):
        raise ValidationFailed("Plan senaryo listesi ister")
    allowed = frozenset(request.source_files)
    scenarios = tuple(parse_scenario(item, allowed_sources=allowed) for item in raw)
    note = document.get("size_note")
    if note is not None:
        _text(note, "size_note")
    return BehaviorPlan(
        request_digest=request.request_digest,
        version=version,
        scenarios=scenarios,
        rejected_approach_digests=tuple(document.get("rejected_approach_digests", ())),
    )


@dataclass(frozen=True, slots=True)
class PlanDelta:
    """Builder'in kapsam icinde buldugu yeni davranislar; yeni yetki dogurmaz."""

    rationale: str
    new_scenarios: tuple[BehaviorScenario, ...] = ()
    requires_authority: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _text(self.rationale, "plan_delta rationale")


def parse_plan_delta(document: Mapping[str, Any], request: UnitTestRequest) -> PlanDelta:
    if not isinstance(document, Mapping) or set(document) - {
        "rationale",
        "new_scenarios",
        "requires_authority",
    }:
        raise ValidationFailed("plan_delta bilinmeyen alan tasiyor")
    raw = document.get("new_scenarios", [])
    if not isinstance(raw, list | tuple):
        raise ValidationFailed("new_scenarios liste olmali")
    auth = document.get("requires_authority", [])
    if not isinstance(auth, list | tuple):
        raise ValidationFailed("requires_authority liste olmali")
    allowed = frozenset(request.source_files)
    return PlanDelta(
        rationale=_text(document.get("rationale"), "plan_delta rationale"),
        new_scenarios=tuple(parse_scenario(item, allowed_sources=allowed) for item in raw),
        requires_authority=tuple(_text(a, "authority") for a in auth),
    )
