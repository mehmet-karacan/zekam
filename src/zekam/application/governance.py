"""Governance servisi: hard gate zinciri, exact authorization ve provider gate.

Hard gate sirasi sabittir ve ilk reddedende durur:

```text
1. capability   : adapter bunu teknik olarak yapabiliyor mu?
2. policy       : bu etki turune izin veriliyor mu?
3. risk         : risk seviyesi policy tavaninin altinda mi?
4. scope        : istenen kaynak ve etki yetkinin kapsaminda mi?
5. authorization: yetki gecerli, tuketilmemis ve suresi dolmamis mi?
```

Her karar denetim kaydina yazilir. "Sessiz izin" veya "sessiz red" yoktur.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, ConfigurationError, NotFound, PolicyViolation
from zekam.domain.policy import (
    RISK_ORDER,
    Capability,
    GateDecision,
    GateOutcome,
    GateResult,
    PolicyDocument,
    RiskAssessment,
    classify_risk,
    default_policy_rules,
    is_auto_approved,
)
from zekam.domain.realm import Realm
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    DataClassification,
    OutboundRequest,
    OutboundState,
)
from zekam.domain.work import EffectKind, TaskPlan

#: Varsayilan policy adi.
DEFAULT_POLICY_NAME = "varsayilan"

#: Yetkinin varsayilan omru.
DEFAULT_AUTHORIZATION_LIFETIME = dt.timedelta(minutes=30)


class GovernanceRepositoryProvider(Protocol):
    """Outer-composition port for governance repositories.

    The local-first wheel deliberately provides no implicit database-backed
    implementation.  A reviewed composition must inject narrow repositories;
    absence is a deterministic fail-closed state.
    """

    def __call__(
        self,
        kind: str,
        connection: Any,
        realm_id: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class EffectRequest:
    """Yurutulmek istenen exact etki."""

    action: str
    effects: tuple[EffectKind, ...]
    resources: tuple[str, ...] = ()
    data_classifications: tuple[DataClassification, ...] = ()
    provider_refs: tuple[str, ...] = ()
    reversible: bool = True
    destructive: bool = False
    touches_external_system: bool = False
    required_capabilities: tuple[str, ...] = ()

    def body(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "effects": sorted(item.value for item in self.effects),
            "resources": sorted(self.resources),
            "data_classifications": sorted(item.value for item in self.data_classifications),
            "provider_refs": sorted(self.provider_refs),
            "reversible": self.reversible,
            "destructive": self.destructive,
            "touches_external_system": self.touches_external_system,
        }

    @property
    def effect_digest(self) -> str:
        """Yetkiye baglanacak exact etki digest'i."""
        return digest(
            [
                {"effect": item.value, "resources": sorted(self.resources)}
                for item in sorted(self.effects, key=lambda entry: entry.value)
                if item is not EffectKind.NONE
            ]
        )


@dataclass(frozen=True, slots=True)
class GovernanceVerdict:
    """Kapi zincirinin toplam karari."""

    request: EffectRequest
    risk: RiskAssessment
    gates: GateResult
    auto_approved: bool
    authorization: Authorization | None = None

    @property
    def allowed(self) -> bool:
        return self.gates.allowed

    @property
    def denial_reason(self) -> str | None:
        denial = self.gates.first_denial
        return None if denial is None else denial.reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.body(),
            "effect_digest": self.request.effect_digest,
            "risk": self.risk.as_dict(),
            "gates": self.gates.as_dict(),
            "auto_approved": self.auto_approved,
            "allowed": self.allowed,
            "authorization_id": (
                None if self.authorization is None else str(self.authorization.id)
            ),
        }


@dataclass(frozen=True, slots=True)
class GovernanceService:
    """Policy, capability, risk, yetki ve denetim islemleri."""

    connection: Any
    realm: Realm
    actor_id: UUID | None = None
    correlation_id: UUID | None = None
    repository_provider: GovernanceRepositoryProvider | None = None
    _cached_policy: list[PolicyDocument] = field(default_factory=list, repr=False)

    # -- depolar ------------------------------------------------------------------

    def _repository(self, kind: str) -> Any:
        provider = self.repository_provider
        if provider is None:
            raise ConfigurationError(
                "Governance repository provider local-first composition root'ta kurulmamıs"
            )
        return provider(kind, self.connection, self.realm.id)

    @property
    def policies(self) -> Any:
        return self._repository("policy")

    @property
    def capabilities(self) -> Any:
        return self._repository("capability")

    @property
    def secrets(self) -> Any:
        return self._repository("secret_ref")

    @property
    def authorizations(self) -> Any:
        return self._repository("authorization")

    @property
    def outbound(self) -> Any:
        return self._repository("outbound_request")

    @property
    def audit(self) -> Any:
        return self._repository("audit")

    # -- policy -------------------------------------------------------------------

    def ensure_default_policy(self, *, now: dt.datetime | None = None) -> PolicyDocument:
        """Varsayilan policy yoksa olusturur (idempotent)."""
        current = cast(PolicyDocument | None, self.policies.current(DEFAULT_POLICY_NAME))
        if current is not None:
            return current
        document = PolicyDocument.create(
            realm_id=self.realm.id,
            name=DEFAULT_POLICY_NAME,
            revision=1,
            rules=default_policy_rules(),
            now=now,
        )
        return cast(PolicyDocument, self.policies.append(document))

    def active_policy(self, name: str = DEFAULT_POLICY_NAME) -> PolicyDocument:
        current = cast(PolicyDocument | None, self.policies.current(name))
        if current is None:
            raise NotFound(f"Policy bulunamadi: {name}")
        return current

    # -- kapi zinciri ---------------------------------------------------------------

    def evaluate(
        self,
        request: EffectRequest,
        *,
        authorization: Authorization | None = None,
        policy: PolicyDocument | None = None,
        now: dt.datetime | None = None,
        record_audit: bool = True,
    ) -> GovernanceVerdict:
        """Hard gate zincirini sirayla degerlendirir. Hicbir etki yurutmez."""
        moment = now or dt.datetime.now(dt.UTC)
        document = policy or self.active_policy()
        risk = classify_risk(
            effects=request.effects,
            data_classifications=request.data_classifications,
            resource_count=len(request.resources),
            reversible=request.reversible,
            touches_external_system=request.touches_external_system,
            destructive=request.destructive,
        )
        auto = is_auto_approved(request.action, effects=request.effects)
        decisions: list[GateDecision] = []

        decisions.append(self._capability_gate(request))
        if decisions[-1].allowed:
            decisions.append(self._policy_gate(request, document))
        if decisions[-1].allowed:
            decisions.append(self._risk_gate(request, document, risk))
        if decisions[-1].allowed and not auto and risk.requires_authorization:
            decisions.append(self._scope_gate(request, authorization))
            if decisions[-1].allowed:
                decisions.append(self._authorization_gate(authorization, request, moment))
        elif decisions[-1].allowed:
            decisions.append(
                GateDecision(
                    gate="authorization",
                    outcome=GateOutcome.ALLOW,
                    reason="salt-okunur-islem-otomatik",
                )
            )

        verdict = GovernanceVerdict(
            request=request,
            risk=risk,
            gates=GateResult(tuple(decisions)),
            auto_approved=auto,
            authorization=authorization,
        )
        if record_audit:
            self.audit.record(
                action=f"gate.{request.action}",
                subject_type="effect",
                subject_id=request.effect_digest,
                decision="allow" if verdict.allowed else "deny",
                reason=verdict.denial_reason or "all-gates-passed",
                evidence=verdict.as_dict(),
                actor_id=self.actor_id,
                authorization_id=None if authorization is None else authorization.id,
                correlation_id=self.correlation_id,
                now=moment,
            )
        return verdict

    def _capability_gate(self, request: EffectRequest) -> GateDecision:
        missing = [
            name
            for name in request.required_capabilities
            if self.capabilities.current(name) is None
        ]
        if missing:
            return GateDecision(
                gate="capability",
                outcome=GateOutcome.DENY,
                reason=f"capability-missing:{','.join(sorted(missing))}",
            )
        return GateDecision(
            gate="capability", outcome=GateOutcome.ALLOW, reason="capabilities-registered"
        )

    def _policy_gate(self, request: EffectRequest, document: PolicyDocument) -> GateDecision:
        for effect in request.effects:
            rule = document.rule_for(effect)
            if rule is None:
                return GateDecision(
                    gate="policy",
                    outcome=GateOutcome.DENY,
                    reason=f"policy-rule-missing:{effect.value}",
                )
            if not rule.allow:
                return GateDecision(
                    gate="policy",
                    outcome=GateOutcome.DENY,
                    reason=f"policy-denies:{effect.value}:{rule.name}",
                )
            if rule.allowed_resources:
                uncovered = tuple(
                    resource
                    for resource in request.resources
                    if not any(
                        resource == allowed
                        or (allowed.endswith("*") and resource.startswith(allowed[:-1]))
                        for allowed in rule.allowed_resources
                    )
                )
                if uncovered:
                    return GateDecision(
                        gate="policy",
                        outcome=GateOutcome.DENY,
                        reason=f"resource-out-of-policy:{effect.value}:{','.join(sorted(uncovered))}",
                    )
        return GateDecision(gate="policy", outcome=GateOutcome.ALLOW, reason="policy-allows")

    def _risk_gate(
        self, request: EffectRequest, document: PolicyDocument, risk: RiskAssessment
    ) -> GateDecision:
        for effect in request.effects:
            rule = document.rule_for(effect)
            if rule is None:  # pragma: no cover - policy gate bunu zaten yakalar
                continue
            if RISK_ORDER[risk.level] > RISK_ORDER[rule.max_risk]:
                return GateDecision(
                    gate="risk",
                    outcome=GateOutcome.DENY,
                    reason=(
                        f"risk-above-policy-ceiling:{risk.level.value}>{rule.max_risk.value}"
                        f":{rule.name}"
                    ),
                )
        return GateDecision(
            gate="risk", outcome=GateOutcome.ALLOW, reason=f"risk-{risk.level.value}-within-policy"
        )

    def _scope_gate(
        self, request: EffectRequest, authorization: Authorization | None
    ) -> GateDecision:
        if authorization is None:
            return GateDecision(
                gate="scope", outcome=GateOutcome.DENY, reason="authorization-required"
            )
        for effect in request.effects:
            if effect is EffectKind.NONE:
                continue
            if not authorization.scope.covers_effect(effect.value):
                return GateDecision(
                    gate="scope",
                    outcome=GateOutcome.DENY,
                    reason=f"effect-out-of-scope:{effect.value}",
                )
        for resource in request.resources:
            if not authorization.scope.covers_resource(resource):
                return GateDecision(
                    gate="scope",
                    outcome=GateOutcome.DENY,
                    reason=f"resource-out-of-scope:{resource}",
                )
        for provider in request.provider_refs:
            if provider not in authorization.scope.provider_refs:
                return GateDecision(
                    gate="scope",
                    outcome=GateOutcome.DENY,
                    reason=f"provider-out-of-scope:{provider}",
                )
        if authorization.effect_digest != request.effect_digest:
            return GateDecision(
                gate="scope", outcome=GateOutcome.DENY, reason="effect-digest-mismatch"
            )
        return GateDecision(gate="scope", outcome=GateOutcome.ALLOW, reason="scope-matches")

    def _authorization_gate(
        self,
        authorization: Authorization | None,
        request: EffectRequest,
        moment: dt.datetime,
    ) -> GateDecision:
        del request
        if authorization is None:  # pragma: no cover - scope gate bunu yakalar
            return GateDecision(
                gate="authorization", outcome=GateOutcome.DENY, reason="authorization-required"
            )
        rejection = authorization.rejection_reason(moment)
        if rejection is not None:
            return GateDecision(gate="authorization", outcome=GateOutcome.DENY, reason=rejection)
        return GateDecision(
            gate="authorization", outcome=GateOutcome.ALLOW, reason="authorization-valid"
        )

    # -- yetki --------------------------------------------------------------------------

    def issue_authorization(
        self,
        *,
        request: EffectRequest,
        actor_id: UUID,
        plan: TaskPlan | None = None,
        plan_digest: str | None = None,
        work_item_id: UUID | None = None,
        plan_id: UUID | None = None,
        lifetime: dt.timedelta = DEFAULT_AUTHORIZATION_LIFETIME,
        secret_ref_ids: tuple[UUID, ...] = (),
        now: dt.datetime | None = None,
    ) -> Authorization:
        """Exact yetki uretir.

        Kapsam istegin kendisinden turetilir; genel kapsam verilemez.
        """
        moment = now or dt.datetime.now(dt.UTC)
        if plan is not None and (work_item_id is not None or plan_id is not None):
            raise PolicyViolation("Authorization plan nesnesi ile explicit plan kimligi karisamaz")
        if (work_item_id is None) != (plan_id is None):
            raise PolicyViolation("Authorization work ve plan kimliklerini birlikte ister")
        risk = classify_risk(
            effects=request.effects,
            data_classifications=request.data_classifications,
            resource_count=len(request.resources),
            reversible=request.reversible,
            touches_external_system=request.touches_external_system,
            destructive=request.destructive,
        )
        scope = AuthorizationScope(
            allowed_resources=tuple(sorted(request.resources)),
            allowed_effects=tuple(
                sorted(item.value for item in request.effects if item is not EffectKind.NONE)
            )
            or ("none",),
            provider_refs=tuple(sorted(request.provider_refs)),
            secret_ref_ids=tuple(secret_ref_ids),
            data_classifications=tuple(request.data_classifications),
        )
        authorization = Authorization.issue(
            realm_id=self.realm.id,
            actor_id=actor_id,
            plan_digest=plan_digest or (plan.plan_digest if plan else digest(request.body())),
            effect_digest=request.effect_digest,
            scope=scope,
            risk=risk.level.value,
            lifetime=lifetime,
            work_item_id=plan.work_item_id if plan else work_item_id,
            plan_id=plan.id if plan else plan_id,
            now=moment,
        )
        self.authorizations.issue(authorization)
        self.audit.record(
            action="authorization.issued",
            subject_type="authorization",
            subject_id=str(authorization.id),
            decision="record",
            reason=f"risk-{risk.level.value}",
            evidence=authorization.as_dict() | {"risk": risk.as_dict()},
            actor_id=actor_id,
            authorization_id=authorization.id,
            correlation_id=self.correlation_id,
            now=moment,
        )
        return authorization

    def consume_authorization(
        self,
        authorization_id: UUID,
        *,
        request: EffectRequest,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> Any:
        """Yetkiyi atomik tuketir ve sonucu denetime yazar."""
        moment = now or dt.datetime.now(dt.UTC)
        result = self.authorizations.consume(
            authorization_id,
            effect_digest=request.effect_digest,
            consumed_by=consumed_by,
            now=moment,
        )
        self.audit.record(
            action="authorization.consume",
            subject_type="authorization",
            subject_id=str(authorization_id),
            decision="allow" if result.consumed else "deny",
            reason=result.reason,
            evidence=result.as_dict() | {"effect_digest": request.effect_digest},
            actor_id=self.actor_id,
            # Var olmayan bir yetkiye foreign key verilemez; deneme yine de kayda gecer.
            authorization_id=None if result.authorization is None else authorization_id,
            correlation_id=self.correlation_id,
            now=moment,
        )
        return result

    def revoke_authorization(
        self, authorization_id: UUID, reason: str, *, now: dt.datetime | None = None
    ) -> bool:
        moment = now or dt.datetime.now(dt.UTC)
        revoked = self.authorizations.revoke(authorization_id, reason, now=moment)
        self.audit.record(
            action="authorization.revoke",
            subject_type="authorization",
            subject_id=str(authorization_id),
            decision="allow" if revoked else "deny",
            reason=reason if revoked else "authorization-not-revocable",
            evidence={"revoked": revoked},
            actor_id=self.actor_id,
            authorization_id=self._existing_authorization_id(authorization_id),
            correlation_id=self.correlation_id,
            now=moment,
        )
        return cast(bool, revoked)

    def _existing_authorization_id(self, authorization_id: UUID) -> UUID | None:
        """Denetim kaydinin foreign key'i icin yalnizca var olan kimligi dondurur."""
        try:
            return cast(UUID, self.authorizations.get(authorization_id).id)
        except NotFound:
            return None

    def require_authorized(
        self,
        request: EffectRequest,
        *,
        authorization: Authorization | None,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> Authorization:
        """Kapilari gecirir ve yetkiyi tuketir; aksi halde hata verir.

        Bu, effect yurutmeden once cagrilan tek giris noktasidir.
        """
        moment = now or dt.datetime.now(dt.UTC)
        verdict = self.evaluate(request, authorization=authorization, now=moment)
        if not verdict.allowed:
            reason = verdict.denial_reason or "denied"
            if reason.startswith("authorization") or "out-of-scope" in reason:
                raise AuthorizationRequired(f"Etki yetkilendirilmedi: {reason}")
            raise PolicyViolation(f"Etki reddedildi: {reason}")
        if authorization is None:  # pragma: no cover - evaluate bunu yakalar
            raise AuthorizationRequired("Yetki gerekli")
        result = self.consume_authorization(
            authorization.id, request=request, consumed_by=consumed_by, now=moment
        )
        if not result.consumed or result.authorization is None:
            raise AuthorizationRequired(f"Yetki tuketilemedi: {result.reason}")
        return cast(Authorization, result.authorization)


@dataclass(frozen=True, slots=True)
class ProviderGate:
    """Disari acilan istekleri denetler.

    `prepare` salt okunurdur: ag cagrisi yapmaz, secret cozmez, kayit disinda
    hicbir yan etki uretmez. `apply` istegi yetkiyle **yeniden** eslestirir.
    """

    governance: GovernanceService

    def prepare(
        self, request: OutboundRequest, *, now: dt.datetime | None = None
    ) -> OutboundRequest:
        """Istegi kaydeder ve veri siniflandirmasini kontrol eder."""
        moment = now or dt.datetime.now(dt.UTC)
        blocking = request.blocking_classifications()
        if blocking:
            denied = request.with_state(
                OutboundState.DENIED,
                denial_reason=f"forbidden-data-class:{','.join(item.value for item in blocking)}",
            )
            self.governance.outbound.record(denied)
            self.governance.audit.record(
                action="outbound.prepare",
                subject_type="outbound",
                subject_id=str(request.id),
                decision="deny",
                reason=denied.denial_reason or "denied",
                evidence=denied.as_dict(),
                actor_id=self.governance.actor_id,
                correlation_id=self.governance.correlation_id,
                now=moment,
            )
            return denied

        self.governance.outbound.record(request)
        self.governance.audit.record(
            action="outbound.prepare",
            subject_type="outbound",
            subject_id=str(request.id),
            decision="record",
            reason="prepared-without-network",
            evidence=request.as_dict(),
            actor_id=self.governance.actor_id,
            correlation_id=self.governance.correlation_id,
            now=moment,
        )
        return request

    def apply(
        self,
        request: OutboundRequest,
        *,
        authorization: Authorization,
        now: dt.datetime | None = None,
    ) -> OutboundRequest:
        """Istegi yetkiyle exact eslestirip yurutmeye acar."""
        moment = now or dt.datetime.now(dt.UTC)
        reason = self._mismatch_reason(request, authorization, moment)
        if reason is not None:
            denied = request.with_state(OutboundState.DENIED, denial_reason=reason)
            self.governance.outbound.record(denied)
            self.governance.audit.record(
                action="outbound.apply",
                subject_type="outbound",
                subject_id=str(request.id),
                decision="deny",
                reason=reason,
                evidence=denied.as_dict(),
                actor_id=self.governance.actor_id,
                authorization_id=authorization.id,
                correlation_id=self.governance.correlation_id,
                now=moment,
            )
            raise PolicyViolation(f"Outbound istek reddedildi: {reason}")

        approved = request.with_state(OutboundState.APPROVED, authorization_id=authorization.id)
        self.governance.outbound.record(approved)
        self.governance.audit.record(
            action="outbound.apply",
            subject_type="outbound",
            subject_id=str(request.id),
            decision="allow",
            reason="request-matches-authorization",
            evidence=approved.as_dict(),
            actor_id=self.governance.actor_id,
            authorization_id=authorization.id,
            correlation_id=self.governance.correlation_id,
            now=moment,
        )
        return approved

    def _mismatch_reason(
        self, request: OutboundRequest, authorization: Authorization, moment: dt.datetime
    ) -> str | None:
        rejection = authorization.rejection_reason(moment)
        if rejection is not None:
            return rejection
        if request.blocking_classifications():
            return "forbidden-data-class"
        if request.provider_ref not in authorization.scope.provider_refs:
            return f"provider-out-of-scope:{request.provider_ref}"
        if not authorization.scope.covers_resource(request.target):
            return f"endpoint-out-of-scope:{request.target}"
        review = request.review_required_classifications()
        for classification in review:
            if classification not in authorization.scope.data_classifications:
                return f"data-class-not-reviewed:{classification.value}"
        return None


def registered_capability_names(capabilities: Sequence[Capability]) -> tuple[str, ...]:
    """Kayitli yetenek adlarini dondurur."""
    return tuple(sorted(item.name for item in capabilities))


def default_capabilities(realm_id: UUID) -> tuple[Capability, ...]:
    """Kurulumla gelen temel yetenekler."""
    from zekam.domain.policy import CapabilityKind

    definitions: tuple[tuple[str, CapabilityKind, str], ...] = (
        ("source.read", CapabilityKind.READ, "Harici kaynagi salt okunur okur"),
        ("sandbox.write", CapabilityKind.FILESYSTEM, "Yalitilmis calisma alanina yazar"),
        ("database.read", CapabilityKind.DATABASE, "Kanonik store'dan okur"),
        ("database.write", CapabilityKind.DATABASE, "Kanonik store'a yazar"),
        ("provider.call", CapabilityKind.PROVIDER, "Model saglayicisina istek gonderir"),
        ("process.run", CapabilityKind.PROCESS, "Typed runner ile surec calistirir"),
        ("git.read", CapabilityKind.GIT, "Salt okunur Git komutlari calistirir"),
    )
    return tuple(
        Capability.create(
            realm_id=realm_id, name=name, revision=1, kind=kind, description=description
        )
        for name, kind, description in definitions
    )
