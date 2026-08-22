"""Policy, capability ve risk siniflandirmasi.

Ayrim (`guvenlik/APPROVAL_VE_YETKI_POLITIKASI.md`):

```text
Policy        : neye izin verilebilir?
Capability    : adapter/worker ne yapabilir?
Authorization : exact effect icin izin var mi?
```

Biri digerinin yerine gecmez. Bir yetenegin var olmasi izin anlamina gelmez; bir
policy'nin izin vermesi de exact authorization yerine gecmez.

Risk seviyesi effect, blast radius, geri alinabilirlik, veri hassasiyeti ve dis
sistem etkisinden **turetilir**. Model veya istemci kendi beyaniyla riski
dusuremez.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.identifiers import new_uuid7
from zekam.domain.security import DataClassification
from zekam.domain.work import AUTHORIZED_EFFECTS, EffectKind


class RiskLevel(StrEnum):
    """Risk siniflari."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


#: Karsilastirma icin sirali degerler.
RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.NONE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


def max_risk(levels: Sequence[RiskLevel]) -> RiskLevel:
    """En yuksek riski dondurur."""
    if not levels:
        return RiskLevel.NONE
    return max(levels, key=lambda level: RISK_ORDER[level])


#: Etki turunun taban riski. Sinif buradan asagi inemez.
BASE_RISK_BY_EFFECT: dict[EffectKind, RiskLevel] = {
    EffectKind.NONE: RiskLevel.NONE,
    EffectKind.PROCESS_RUN: RiskLevel.MEDIUM,
    EffectKind.FILE_WRITE: RiskLevel.MEDIUM,
    EffectKind.NETWORK_CALL: RiskLevel.MEDIUM,
    EffectKind.PROVIDER_CALL: RiskLevel.MEDIUM,
    EffectKind.DATABASE_WRITE: RiskLevel.HIGH,
    EffectKind.GIT_COMMIT: RiskLevel.MEDIUM,
    EffectKind.GIT_PUSH: RiskLevel.HIGH,
}

#: Veri sinifinin taban riski.
BASE_RISK_BY_DATA: dict[DataClassification, RiskLevel] = {
    DataClassification.PUBLIC: RiskLevel.NONE,
    DataClassification.INTERNAL: RiskLevel.LOW,
    DataClassification.CONFIDENTIAL: RiskLevel.MEDIUM,
    DataClassification.RESTRICTED: RiskLevel.HIGH,
    DataClassification.SECRET: RiskLevel.CRITICAL,
    DataClassification.LOCAL_ONLY: RiskLevel.HIGH,
}

#: Bagimsiz verifier zorunlu olan risk seviyeleri.
VERIFIER_REQUIRED_FROM: RiskLevel = RiskLevel.HIGH

#: Onaysiz yurutulebilecek salt okunur islemler.
AUTO_APPROVED_ACTIONS: frozenset[str] = frozenset(
    {
        "status",
        "list",
        "history",
        "show",
        "doctor",
        "plan",
        "dry-run",
        "local-retrieval",
        "source-inspect",
        "derived-projection",
    }
)


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """Risk siniflandirmasinin sonucu ve gerekcesi."""

    level: RiskLevel
    factors: tuple[str, ...]
    requires_authorization: bool
    requires_independent_verifier: bool
    irreversible: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "factors": list(self.factors),
            "requires_authorization": self.requires_authorization,
            "requires_independent_verifier": self.requires_independent_verifier,
            "irreversible": self.irreversible,
        }


def classify_risk(
    *,
    effects: Sequence[EffectKind],
    data_classifications: Sequence[DataClassification] = (),
    resource_count: int = 0,
    reversible: bool = True,
    touches_external_system: bool = False,
    destructive: bool = False,
) -> RiskAssessment:
    """Etkilerden ve baglamdan risk seviyesi turetir.

    Kural: seviye yalnizca yukari cikabilir. Cagiran taraf riski dusuremez.
    """
    factors: list[str] = []
    levels: list[RiskLevel] = [RiskLevel.NONE]

    for effect in effects:
        level = BASE_RISK_BY_EFFECT[effect]
        if level is not RiskLevel.NONE:
            factors.append(f"effect:{effect.value}={level.value}")
        levels.append(level)

    for classification in data_classifications:
        level = BASE_RISK_BY_DATA[classification]
        if level is not RiskLevel.NONE:
            factors.append(f"data:{classification.value}={level.value}")
        levels.append(level)

    # Blast radius: cok sayida yazilabilir kaynak riski yukseltir.
    if resource_count > 20:
        factors.append(f"blast-radius:{resource_count}=high")
        levels.append(RiskLevel.HIGH)
    elif resource_count > 5:
        factors.append(f"blast-radius:{resource_count}=medium")
        levels.append(RiskLevel.MEDIUM)

    if not reversible:
        factors.append("irreversible=high")
        levels.append(RiskLevel.HIGH)
    if touches_external_system:
        factors.append("external-system=medium")
        levels.append(RiskLevel.MEDIUM)
    if destructive:
        factors.append("destructive=critical")
        levels.append(RiskLevel.CRITICAL)

    level = max_risk(levels)
    needs_authorization = any(effect in AUTHORIZED_EFFECTS for effect in effects) or (
        RISK_ORDER[level] >= RISK_ORDER[RiskLevel.MEDIUM]
    )
    return RiskAssessment(
        level=level,
        factors=tuple(sorted(set(factors))),
        requires_authorization=needs_authorization,
        requires_independent_verifier=RISK_ORDER[level] >= RISK_ORDER[VERIFIER_REQUIRED_FROM],
        irreversible=not reversible or destructive,
    )


def is_auto_approved(action: str, *, effects: Sequence[EffectKind] = ()) -> bool:
    """Islemin onaysiz yurutulebilir olup olmadigini soyler.

    Salt okunur bir eylem bile bir effect tasiyorsa otomatik degildir.
    """
    if any(effect is not EffectKind.NONE for effect in effects):
        return False
    return action in AUTO_APPROVED_ACTIONS


class GateOutcome(StrEnum):
    """Kapi karari."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Tek bir hard gate'in karari."""

    gate: str
    outcome: GateOutcome
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome is GateOutcome.ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "outcome": self.outcome.value, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class GateResult:
    """Sirali hard gate degerlendirmesinin sonucu."""

    decisions: tuple[GateDecision, ...]

    @property
    def allowed(self) -> bool:
        return all(decision.allowed for decision in self.decisions)

    @property
    def first_denial(self) -> GateDecision | None:
        return next((decision for decision in self.decisions if not decision.allowed), None)

    def as_dict(self) -> dict[str, Any]:
        denial = self.first_denial
        return {
            "allowed": self.allowed,
            "decisions": [decision.as_dict() for decision in self.decisions],
            "first_denial": None if denial is None else denial.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """Tek bir policy kurali."""

    name: str
    effect_kinds: tuple[EffectKind, ...]
    allow: bool
    max_risk: RiskLevel = RiskLevel.CRITICAL
    allowed_resources: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValidationFailed("Policy kurali adi bos olamaz")

    def applies_to(self, effect: EffectKind) -> bool:
        return not self.effect_kinds or effect in self.effect_kinds

    def body(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "effect_kinds": sorted(item.value for item in self.effect_kinds),
            "allow": self.allow,
            "max_risk": self.max_risk.value,
            "allowed_resources": sorted(self.allowed_resources),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PolicyDocument:
    """Surumlu policy belgesi."""

    id: UUID
    realm_id: UUID
    name: str
    revision: int
    rules: tuple[PolicyRule, ...]
    network_default_deny: bool = True
    push_default_deny: bool = True
    effective_from: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValidationFailed("Policy revision 1'den kucuk olamaz")
        names = [rule.name for rule in self.rules]
        if len(names) != len(set(names)):
            raise ValidationFailed("Policy kural adlari tekil olmali")

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        name: str,
        revision: int,
        rules: tuple[PolicyRule, ...],
        network_default_deny: bool = True,
        push_default_deny: bool = True,
        now: dt.datetime | None = None,
    ) -> PolicyDocument:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm_id,
            name=name,
            revision=revision,
            rules=rules,
            network_default_deny=network_default_deny,
            push_default_deny=push_default_deny,
            effective_from=moment,
        )

    def rule_for(self, effect: EffectKind) -> PolicyRule | None:
        """Etki icin gecerli ilk kurali dondurur."""
        return next((rule for rule in self.rules if rule.applies_to(effect)), None)

    def body(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "revision": self.revision,
            "rules": [rule.body() for rule in self.rules],
            "network_default_deny": self.network_default_deny,
            "push_default_deny": self.push_default_deny,
        }

    @property
    def policy_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"id": str(self.id), "policy_digest": self.policy_digest}


class CapabilityKind(StrEnum):
    """Yetenek turu."""

    READ = "read"
    FILESYSTEM = "filesystem"
    DATABASE = "database"
    NETWORK = "network"
    PROVIDER = "provider"
    PROCESS = "process"
    GIT = "git"


@dataclass(frozen=True, slots=True)
class Capability:
    """Bir adapter veya worker'in yapabildigi is.

    Yetenegin kayitli olmasi izin degildir; yalnizca teknik olarak mumkun
    oldugunu soyler.
    """

    id: UUID
    realm_id: UUID
    name: str
    revision: int
    kind: CapabilityKind
    description: str = ""
    definition: Mapping[str, Any] = field(default_factory=dict)
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValidationFailed("Capability adi bos olamaz")
        if self.revision < 1:
            raise ValidationFailed("Capability revision 1'den kucuk olamaz")

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        name: str,
        revision: int,
        kind: CapabilityKind,
        description: str = "",
        definition: Mapping[str, Any] | None = None,
        now: dt.datetime | None = None,
    ) -> Capability:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm_id,
            name=name,
            revision=revision,
            kind=kind,
            description=description,
            definition=dict(definition or {}),
            created_at=moment,
        )

    def body(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "revision": self.revision,
            "kind": self.kind.value,
            "description": self.description,
            "definition": dict(self.definition),
        }

    @property
    def capability_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"id": str(self.id), "capability_digest": self.capability_digest}


#: Varsayilan policy: ag ve push kapali, dosya/DB yazimi orta riske kadar acik.
def default_policy_rules() -> tuple[PolicyRule, ...]:
    """Kurulumla gelen guvenli varsayilan kurallar."""
    return (
        PolicyRule(
            name="salt-okunur-serbest",
            effect_kinds=(EffectKind.NONE,),
            allow=True,
            max_risk=RiskLevel.NONE,
            reason="Salt okunur islemler policy ile serbesttir",
        ),
        PolicyRule(
            name="dosya-yazimi-sinirli",
            effect_kinds=(EffectKind.FILE_WRITE,),
            allow=True,
            max_risk=RiskLevel.HIGH,
            reason="Dosya yazimi yalnizca sandbox icinde ve exact authorization ile",
        ),
        PolicyRule(
            name="surec-calistirma-sinirli",
            effect_kinds=(EffectKind.PROCESS_RUN,),
            allow=True,
            max_risk=RiskLevel.HIGH,
            reason="Test/build calistirma typed runner ile",
        ),
        PolicyRule(
            name="veritabani-yazimi-yuksek-risk",
            effect_kinds=(EffectKind.DATABASE_WRITE,),
            allow=True,
            max_risk=RiskLevel.HIGH,
            reason="DB yazimi yedek ve rollback plani ister",
        ),
        PolicyRule(
            name="ag-varsayilan-kapali",
            effect_kinds=(EffectKind.NETWORK_CALL, EffectKind.PROVIDER_CALL),
            allow=False,
            max_risk=RiskLevel.NONE,
            reason="Ag varsayilan olarak kapalidir; exact allowlist gerekir",
        ),
        PolicyRule(
            name="commit-sinirli",
            effect_kinds=(EffectKind.GIT_COMMIT,),
            allow=True,
            max_risk=RiskLevel.MEDIUM,
            reason="Commit yalnizca test ve verifier gectikten sonra",
        ),
        PolicyRule(
            name="push-varsayilan-kapali",
            effect_kinds=(EffectKind.GIT_PUSH,),
            allow=False,
            max_risk=RiskLevel.NONE,
            reason="Push varsayilan olarak yasaktir; acik kullanici talebi gerekir",
        ),
    )
