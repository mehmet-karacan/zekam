"""AgentHarness prepare/apply sozlesmesi.

```text
prepare : salt okunur. Provider cagrisi yok, secret cozumu yok, ag yok,
          mutation yok. Yalnizca plani, kapsami ve digest'leri hesaplar.
apply   : once drift'i yeniden dogrular, sonra exact authorization ile
          etkiyi yurutur.
```

Drift kaynaklari: plan revision, source revision, policy digest, effect digest,
kaynak kumesi ve risk seviyesi. Bunlardan biri degistiyse eski hazirlik
gecersizdir ve yeni plan revision gerekir.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.application.governance import EffectRequest, GovernanceService, GovernanceVerdict
from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.policy import RiskAssessment, classify_risk
from zekam.domain.resources import ResourceRequest, lock_order
from zekam.domain.security import Authorization, DataClassification
from zekam.domain.work import EffectKind, TaskPlan


class PreparePhase(StrEnum):
    """Hazirligin hangi asamada oldugu."""

    PREPARED = "prepared"
    STALE = "stale"
    APPLIED = "applied"


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """`prepare` ciktisi. Hicbir yan etki uretmemistir.

    Bu kayit yetki tasimaz; yalnizca "ne yapilacak" sorusunu digest'e baglar.
    """

    plan_id: UUID | None
    plan_revision: int
    plan_digest: str
    source_revision: str
    policy_digest: str
    effect_request: EffectRequest
    risk: RiskAssessment
    resources: tuple[ResourceRequest, ...]
    verdict: GovernanceVerdict
    prepared_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise ValidationFailed("Hazirlik kaydi authority tasiyamaz")

    @property
    def effect_digest(self) -> str:
        return self.effect_request.effect_digest

    @property
    def requires_authorization(self) -> bool:
        return self.risk.requires_authorization

    @property
    def is_read_only(self) -> bool:
        return all(effect is EffectKind.NONE for effect in self.effect_request.effects)

    def body(self) -> dict[str, Any]:
        """Drift tespitinde karsilastirilan degismez govde."""
        return {
            "plan_revision": self.plan_revision,
            "plan_digest": self.plan_digest,
            "source_revision": self.source_revision,
            "policy_digest": self.policy_digest,
            "effect_digest": self.effect_digest,
            "resources": [request.as_dict() for request in self.resources],
            "risk": self.risk.level.value,
        }

    @property
    def preparation_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {
            "preparation_digest": self.preparation_digest,
            "requires_authorization": self.requires_authorization,
            "is_read_only": self.is_read_only,
            "allowed": self.verdict.allowed,
            "denial_reason": self.verdict.denial_reason,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Hazirlik ile guncel durum arasindaki fark."""

    drifted: bool
    changed_fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"drifted": self.drifted, "changed_fields": list(self.changed_fields)}


def detect_drift(
    prepared: PreparedRequest,
    *,
    plan_digest: str,
    source_revision: str,
    policy_digest: str,
    effect_digest: str | None = None,
) -> DriftReport:
    """Hazirligin hala gecerli olup olmadigini soyler."""
    changed: list[str] = []
    if prepared.plan_digest != plan_digest:
        changed.append("plan_digest")
    if prepared.source_revision != source_revision:
        changed.append("source_revision")
    if prepared.policy_digest != policy_digest:
        changed.append("policy_digest")
    if effect_digest is not None and prepared.effect_digest != effect_digest:
        changed.append("effect_digest")
    return DriftReport(drifted=bool(changed), changed_fields=tuple(changed))


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """`apply` sonucu."""

    applied: bool
    reason: str
    prepared: PreparedRequest
    authorization: Authorization | None = None
    drift: DriftReport | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "reason": self.reason,
            "preparation_digest": self.prepared.preparation_digest,
            "authorization_id": (
                None if self.authorization is None else str(self.authorization.id)
            ),
            "drift": None if self.drift is None else self.drift.as_dict(),
        }


class NoEffectViolation(PolicyViolation):
    """`prepare` sirasinda yan etki uretme girisimi."""

    code = "prepare-must-have-no-effect"


@dataclass(frozen=True, slots=True)
class AgentHarness:
    """Istek hazirlama ve uygulama akisi."""

    governance: GovernanceService

    # -- prepare -----------------------------------------------------------------

    def prepare(
        self,
        *,
        action: str,
        plan: TaskPlan | None = None,
        effects: Sequence[EffectKind] = (),
        resources: Sequence[ResourceRequest] = (),
        source_revision: str = "",
        required_capabilities: Sequence[str] = (),
        provider_refs: Sequence[str] = (),
        data_classifications: Sequence[DataClassification] = (),
        reversible: bool = True,
        destructive: bool = False,
        touches_external_system: bool = False,
        now: dt.datetime | None = None,
    ) -> PreparedRequest:
        """Salt okunur hazirlik yapar.

        Bu metot hicbir kosulda provider cagirmaz, secret cozmez, ag kullanmaz
        veya mutation yapmaz. Yalnizca policy/risk degerlendirmesi ve digest
        hesaplamasi yapar.
        """
        moment = now or dt.datetime.now(dt.UTC)
        policy = self.governance.active_policy()

        if plan is not None:
            plan_effects = tuple(step.effect for step in plan.steps)
            plan_resources = lock_order(
                request
                for step in plan.steps
                for request in _step_requests(step.effect, step.logical_resources)
            )
            resolved_effects = tuple(effects) or plan_effects
            resolved_resources = tuple(resources) or plan_resources
            resolved_source = source_revision or plan.source_revision
        else:
            resolved_effects = tuple(effects) or (EffectKind.NONE,)
            resolved_resources = lock_order(resources)
            resolved_source = source_revision

        request = EffectRequest(
            action=action,
            effects=resolved_effects,
            resources=tuple(item.resource.text for item in resolved_resources),
            data_classifications=tuple(data_classifications),
            provider_refs=tuple(provider_refs),
            reversible=reversible,
            destructive=destructive,
            touches_external_system=touches_external_system,
            required_capabilities=tuple(required_capabilities),
        )
        risk = classify_risk(
            effects=request.effects,
            data_classifications=request.data_classifications,
            resource_count=len(request.resources),
            reversible=reversible,
            touches_external_system=touches_external_system,
            destructive=destructive,
        )
        verdict = self.governance.evaluate(request, authorization=None, policy=policy, now=moment)

        return PreparedRequest(
            plan_id=None if plan is None else plan.id,
            plan_revision=0 if plan is None else plan.revision,
            plan_digest=digest(request.body()) if plan is None else plan.plan_digest,
            source_revision=resolved_source,
            policy_digest=policy.policy_digest,
            effect_request=request,
            risk=risk,
            resources=resolved_resources,
            verdict=verdict,
            prepared_at=moment,
        )

    # -- apply -------------------------------------------------------------------

    def apply(
        self,
        prepared: PreparedRequest,
        *,
        authorization: Authorization | None,
        consumed_by: str,
        current_source_revision: str | None = None,
        current_plan_digest: str | None = None,
        now: dt.datetime | None = None,
    ) -> ApplyOutcome:
        """Drift'i yeniden dogrular ve exact yetkiyi tuketir.

        Yetki tuketilmeden hicbir etki yurutulemez. Drift varsa eski hazirlik
        kullanilamaz; yeni plan revision gerekir.
        """
        moment = now or dt.datetime.now(dt.UTC)
        policy = self.governance.active_policy()
        drift = detect_drift(
            prepared,
            plan_digest=current_plan_digest or prepared.plan_digest,
            source_revision=(
                prepared.source_revision
                if current_source_revision is None
                else current_source_revision
            ),
            policy_digest=policy.policy_digest,
        )
        if drift.drifted:
            self.governance.audit.record(
                action="harness.apply",
                subject_type="preparation",
                subject_id=prepared.preparation_digest,
                decision="deny",
                reason=f"stale-preparation:{','.join(drift.changed_fields)}",
                evidence=drift.as_dict(),
                actor_id=self.governance.actor_id,
                correlation_id=self.governance.correlation_id,
                now=moment,
            )
            return ApplyOutcome(
                applied=False,
                reason=f"stale-preparation:{','.join(drift.changed_fields)}",
                prepared=prepared,
                drift=drift,
            )

        if prepared.is_read_only and not prepared.requires_authorization:
            self.governance.audit.record(
                action="harness.apply",
                subject_type="preparation",
                subject_id=prepared.preparation_digest,
                decision="allow",
                reason="salt-okunur-islem",
                evidence=prepared.as_dict(),
                actor_id=self.governance.actor_id,
                correlation_id=self.governance.correlation_id,
                now=moment,
            )
            return ApplyOutcome(
                applied=True, reason="salt-okunur-islem", prepared=prepared, drift=drift
            )

        try:
            consumed = self.governance.require_authorized(
                prepared.effect_request,
                authorization=authorization,
                consumed_by=consumed_by,
                now=moment,
            )
        except (AuthorizationRequired, PolicyViolation) as exc:
            return ApplyOutcome(applied=False, reason=str(exc), prepared=prepared, drift=drift)
        return ApplyOutcome(
            applied=True,
            reason="yetki-tuketildi",
            prepared=prepared,
            authorization=consumed,
            drift=drift,
        )


def _step_requests(effect: EffectKind, resources: Sequence[str]) -> tuple[ResourceRequest, ...]:
    from zekam.domain.resources import parse_requests

    if effect is EffectKind.NONE:
        return parse_requests(read=resources)
    return parse_requests(write=resources)
