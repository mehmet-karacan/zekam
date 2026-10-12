"""Dogal dil isteginin izinli yetkinlik ve deterministic kapsam dogrulamasi.

WP-03.01 (AKTIF_GOREV ZEKAM-NATURAL-LANGUAGE-CAPABILITY-COMPLETION-001) kapsamindadir.

Bu ince katman, ``route_request`` sonucunu ve modelin yorumladigi niyeti allowlisted
capability + deterministic project/scope validation sozlesmesine baglar:

- Modelin yorumladigi niyet ``ModelIntentProposal`` (typed, immutable, untrusted)
  olarak girilir; allowlist disindaki oneriler fail-closed ``PolicyViolation`` dir.
- Niyet veya proje deterministic route ile celisirse fail-closed clarification
  uretilir; celiskide route kapsam ve capability'si yetkiye cevrilmez.
- Niyet disinda freeform capability injection kabul edilmez; capability yalniz
  route'un deterministic sozlesmesinden gelir.
- Model onerisi asla execution veya onay yetkisi uretmez; ``code-change`` onerisi
  bile ``grants_authority=False`` kalir. Gercek tool invocation bu asamanin
  disindadir.
- ``ready`` alani her zaman ``False`` dir; planned karari ifade eden baska bir
  etiket yok. Plan uretimi native readiness degildir; planin varligi
  ``status == "planned"`` ile okunur.
- Genel soru Zekam veya gpu gibi bir projeye yanlis hedeflenmez; kapsam bos kalir.
- Kayitsiz/belirsiz proje mutation veya retrieval baslatmaz; clarification uretir.
- Dogrudan soru ile proje secimi celisiyorsa route fail-closed clarification'dir;
  bu katman celiskiyi tahminle bastirmaz.

Mevut ``route_request`` ve family/Jira config davranisi yeniden yazilmaz; bu modul
yalniz okuyarak typed, immutable bir plan uretir. ``proposal=None`` cagrisi
proposal'siz donus yolunu birebir korur.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from zekam.application.request_routing import (
    ProjectFamilyCatalog,
    RegisteredProject,
    RequestRoute,
    route_request,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import validate_slug

SCHEMA: Final = "zekam-natural-language-dispatch/v1"

#: Route katmaninin uretebilecegi izinli intent kumesi (exact allowlist).
ALLOWED_INTENTS: Final = frozenset(
    {
        "general-knowledge",
        "project-question",
        "code-change",
        "code-review",
        "test",
        "jira-detail",
        "jira-history",
    }
)

#: Her izinli intent'in baglanabildigi izinli capability'ler (exact allowlist).
ALLOWED_CAPABILITIES: Final = frozenset(
    {
        "general-research",
        "single-project-rag",
        "parallel-project-rag",
        "project-agentic",
        "project-review",
        "project-test",
        "project-history",
        "jira-mcp",
        "clarify-project",
        "clarify-project-family",
        "clarify-project-role",
        "clarify-jira-project",
        "project-unavailable",
        "project-family-incomplete",
    }
)

#: Intent → capability baglamasi: route ``strategy`` degeri bu kumeden cikmalidir.
_INTENT_CAPABILITIES: Final = {
    "general-knowledge": frozenset({"general-research"}),
    "project-question": frozenset(
        {
            "single-project-rag",
            "parallel-project-rag",
            "project-history",
            "clarify-project",
            "clarify-project-family",
            "project-unavailable",
            "project-family-incomplete",
            "jira-mcp",
        }
    ),
    "code-change": frozenset({"project-agentic", "clarify-project-role"}),
    "code-review": frozenset({"project-review"}),
    "test": frozenset({"project-test"}),
    "jira-detail": frozenset({"jira-mcp", "clarify-jira-project"}),
    "jira-history": frozenset({"project-history"}),
}

#: Capability → varsayilan agent rolleri (route ``agent_roles`` ile tutarli).
_CAPABILITY_AGENTS: Final = {
    "general-research": ("researcher",),
    "single-project-rag": ("researcher",),
    "parallel-project-rag": ("researcher",),
    "project-agentic": ("builder", "reviewer", "verifier"),
    "project-review": ("reviewer", "verifier"),
    "project-test": ("verifier",),
    "project-history": ("researcher",),
    "jira-mcp": ("researcher",),
    "clarify-project": (),
    "clarify-project-family": (),
    "clarify-project-role": (),
    "clarify-jira-project": (),
    "project-unavailable": (),
    "project-family-incomplete": (),
}

#: Route durumu → dispatch karari. Yalniz ``selected`` plan uretebilir;
#: clarification ve general read-only kalir, kimse capability'ye baglanamaz.
_INTENT_BY_STATUS: Final = {"selected", "clarification-required", "general"}


class DispatchStatus(StrEnum):
    """Dispatch karar durumu: plan uretimi execution demek degildir."""

    PLANNED = "planned"
    CLARIFICATION_REQUIRED = "clarification-required"
    DECLINED = "declined"


@dataclass(frozen=True, slots=True)
class ProjectScope:
    """Deterministik, dogrulanmis proje kapsami; kayitsiz proje icermeyebilir."""

    project_refs: tuple[str, ...]
    unavailable_project_refs: tuple[str, ...]
    family_ref: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_refs": list(self.project_refs),
            "unavailable_project_refs": list(self.unavailable_project_refs),
            "family_ref": self.family_ref,
        }


@dataclass(frozen=True, slots=True)
class ModelIntentProposal:
    """Modelin yorumladigi niyet onerisi; guvensiz ve yetkisiz giristir.

    Bu tip bir model ciktisinin typed, immutable tasimacisidir. Icerigi
    execution, onay veya yetki kaynagi degildir:

    - ``intent`` ``ALLOWED_INTENTS`` allowlist'inde olmalidir; disindaki deger
      fail-closed olarak reddedilir (``ValidationFailed``).
    - ``project_ref`` opsiyoneldir; verildiginde portable slug biciminde
      olmali. Kayit ve kapsam uyumu route sonucu ile ayrica sinanir.
    - Onerinin onaylanmasi, capability'ye baglanmasi ya da execution/onay
      yetkisi vermesi demek degildir; sozlesme bunu asla yapmaz.
    """

    intent: str
    project_ref: str | None = None

    def __post_init__(self) -> None:
        if self.intent not in ALLOWED_INTENTS:
            raise ValidationFailed(f"Niyet onerisi allowlist disinda: {self.intent}")
        if self.project_ref is not None:
            validate_slug(self.project_ref)


@dataclass(frozen=True, slots=True)
class ValidatedIntent:
    """Allowlist'e baglanmis, immutable niyet karari; authority uretmez."""

    intent: str
    capability: str
    status: DispatchStatus
    reason_codes: tuple[str, ...]
    clarifications: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.intent not in ALLOWED_INTENTS:
            raise PolicyViolation(f"Niyet allowlist disinda: {self.intent}")
        if self.capability not in ALLOWED_CAPABILITIES:
            raise PolicyViolation(f"Capability allowlist disinda: {self.capability}")
        if self.capability not in _INTENT_CAPABILITIES[self.intent]:
            raise PolicyViolation(
                f"Capability {self.capability} intent {self.intent} icin izinli degil"
            )
        if self.status is DispatchStatus.PLANNED and self.clarifications:
            raise PolicyViolation("Planned karar clarification tasiyamaz")
        if self.status is not DispatchStatus.PLANNED and not self.clarifications:
            raise PolicyViolation("Planned olmayan karar clarification ister")


@dataclass(frozen=True, slots=True)
class CapabilityDispatch:
    """Route + dogrulanmis niyetin tek kanit zincirinde immutable sonucu."""

    validated_intent: ValidatedIntent
    scope: ProjectScope
    query_digest: str
    route_digest: str
    catalog_digest: str

    def __post_init__(self) -> None:
        if not self.query_digest or len(self.query_digest) > 256:
            raise ValidationFailed("Dispatch query digest gecersiz")

    @property
    def ready(self) -> bool:
        """Her zaman ``False``: bu katman native readiness degildir.

        ``ready`` sahte "native uyumlu" izlenimi verdigi icin false olarak
        sabitlenmistir. Planin varligi ``plan_available`` ile ifade edilir;
        gercek tool invocation hala bu katmanin disindadir.
        """

        return False

    @property
    def plan_available(self) -> bool:
        """Planned bir plan uretildiyse True; native readiness degildir."""

        return self.validated_intent.status is DispatchStatus.PLANNED

    @property
    def grants_authority(self) -> bool:
        """Model onerisi asla execution/onay yetkisi uretmez."""

        return False

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": SCHEMA,
            "status": self.validated_intent.status.value,
            "intent": self.validated_intent.intent,
            "capability": self.validated_intent.capability,
            "reason_codes": list(self.validated_intent.reason_codes),
            "clarifications": list(self.validated_intent.clarifications),
            "scope": self.scope.as_dict(),
            "query_digest": self.query_digest,
            "route_digest": self.route_digest,
            "catalog_digest": self.catalog_digest,
            "ready": self.ready,
            "plan_available": self.plan_available,
            "provider_calls": 0,
            "grants_authority": False,
            "tool_invocation_performed": False,
        }
        return body


def _dispatch_status(route: RequestRoute) -> DispatchStatus:
    if route.status == "selected":
        return DispatchStatus.PLANNED
    if route.status == "clarification-required":
        return DispatchStatus.CLARIFICATION_REQUIRED
    if route.status == "general":
        return DispatchStatus.DECLINED
    raise PolicyViolation(f"Route durumu dispatch sozlesmesinde tanimli degil: {route.status}")


def _clarifications(route: RequestRoute, status: DispatchStatus) -> tuple[str, ...]:
    if status is DispatchStatus.PLANNED:
        return ()
    if route.reason_codes:
        return tuple(route.reason_codes)
    return (f"{route.strategy}-required",)


def _validate_proposal(proposal: ModelIntentProposal) -> None:
    """Proposal'i fail-closed dogrular; yetki, plan veya capability uretmez.

    Dataclass sozlesmesinin (intent allowlist + portable slug) dispatch
    sinirinda yeniden dogrulanmasidir; bozuk bir onerinin kacinak yoldan
    gelmesi bile plana giremez.
    """

    if proposal.intent not in ALLOWED_INTENTS:
        raise PolicyViolation(f"Proposal intent allowlist disinda: {proposal.intent}")
    if proposal.project_ref is not None:
        validate_slug(proposal.project_ref)


def _proposal_conflict_reason(
    proposal: ModelIntentProposal,
    *,
    route: RequestRoute,
) -> str | None:
    """Deterministik route ile celisen oneriyi tek kararli sebeple raporlar."""

    if proposal.intent != route.intent:
        return "proposal-intent-conflicts-with-route"
    if proposal.project_ref is not None:
        route_scope = set(route.project_refs) | set(route.unavailable_project_refs)
        if proposal.project_ref not in route_scope:
            return "proposal-project-conflicts-with-route"
    return None


#: Celiskide kullanilan safe clarification niyet/capability baglamasi;
#: route'un planned capability'si celiskide asla yetkiye cevrilmez.
_CONFLICT_CLARIFICATION: Final = {
    "jira-detail": ("jira-detail", "clarify-jira-project"),
    "jira-history": ("jira-detail", "clarify-jira-project"),
    "general-knowledge": ("project-question", "clarify-project"),
    "project-question": ("project-question", "clarify-project"),
    "code-change": ("project-question", "clarify-project"),
    "code-review": ("project-question", "clarify-project"),
    "test": ("project-question", "clarify-project"),
}


def _proposal_conflict_dispatch(
    route: RequestRoute,
    *,
    conflict_reason: str,
    proposal_intent: str,
) -> CapabilityDispatch:
    """Route/proposal celiskisinde authority'siz safe clarification uretir.

    Plan uretilmez, route kapsam ve capability'si yetkiye cevrilmez;
    execution/onay yetkisi verilmeyecek sekilde clarification doner.
    """

    intent, capability = _CONFLICT_CLARIFICATION[route.intent]
    return CapabilityDispatch(
        validated_intent=ValidatedIntent(
            intent=intent,
            capability=capability,
            status=DispatchStatus.CLARIFICATION_REQUIRED,
            reason_codes=(conflict_reason, "proposal-not-applied"),
            clarifications=(conflict_reason, f"proposal-intent:{proposal_intent}-ignored"),
        ),
        scope=ProjectScope(
            project_refs=route.project_refs,
            unavailable_project_refs=route.unavailable_project_refs,
            family_ref=None,
        ),
        query_digest=route.query_digest,
        route_digest=route.as_dict()["route_digest"],
        catalog_digest=route.catalog_digest,
    )


def plan_capability_dispatch(
    question: str,
    *,
    catalog: ProjectFamilyCatalog,
    registered_projects: tuple[RegisteredProject, ...],
    proposal: ModelIntentProposal | None = None,
) -> CapabilityDispatch:
    """Route sonucunu allowlisted capability ve deterministic scope'a baglar.

    Saf plan fonksiyonudur: dosya okumaz, provider cagirmaz, tool calistirmaz ve
    yetki uretmez. ``route_request`` davranisi birebir korunur; allowlist veya
    scope sozlesmesi bozulursa fail-closed ``PolicyViolation`` yukseltir.

    Opsiyonel ``proposal`` (modelin yorumladigi niyet):
    - ``intent`` allowlist'te olmali; disindaki deger fail-closed
      ``PolicyViolation`` olarak reddedilir.
    - ``project_ref`` verilmisse portable slug olmali. Route kapsaminda
      bulunmayan proje safe clarification uretir.
    - Niyet veya proje deterministic route ile celisirse safe clarification
      uretilir; celiskide route kapsam ve capability'si yetkiye cevrilmez.
    - Oneri plan'da capability'ye baglanmaz ve execution/onay yetkisi vermez;
      ``code-change`` onerisi dahi salt girdi olarak kalir.
    - Niyet disinda freeform capability injection kabul edilmez; capability
      yalniz route'un deterministic sozlesmesinden gelir.
    """

    route = route_request(question, catalog=catalog, registered_projects=registered_projects)
    if route.status not in _INTENT_BY_STATUS:
        raise PolicyViolation(f"Route durumu allowlist disinda: {route.status}")
    if route.intent not in ALLOWED_INTENTS:
        raise PolicyViolation(f"Route intent allowlist disinda: {route.intent}")
    if route.strategy not in ALLOWED_CAPABILITIES:
        raise PolicyViolation(f"Route strategy allowlist disinda: {route.strategy}")
    if route.strategy not in _INTENT_CAPABILITIES[route.intent]:
        raise PolicyViolation(
            f"Route strategy {route.strategy} intent {route.intent} icin izinli degil"
        )
    for role in route.agent_roles:
        if role not in _CAPABILITY_AGENTS[route.strategy]:
            raise PolicyViolation(
                f"Agent rolu {role} capability {route.strategy} icin izinli degil"
            )
    status = _dispatch_status(route)
    expected_agents = _CAPABILITY_AGENTS[route.strategy]
    if status is DispatchStatus.PLANNED and route.agent_roles != expected_agents:
        raise PolicyViolation(f"Planned capability agent rolleri exact olmali: {route.strategy}")
    if status is not DispatchStatus.PLANNED and route.agent_roles not in (
        (),
        expected_agents,
    ):
        raise PolicyViolation(f"Planned olmayan karar agent rolu tasiyamaz: {route.agent_roles}")
    if route.intent == "general-knowledge" and (
        route.project_refs or route.unavailable_project_refs
    ):
        raise PolicyViolation("Genel soru proje kapsami tasiyamaz")
    if (
        status is DispatchStatus.PLANNED
        and route.intent
        in {
            "code-change",
            "code-review",
            "test",
            "project-question",
        }
        and not route.project_refs
    ):
        raise PolicyViolation("Planned mutation/retrieval niyeti kayitsiz kapsam uretemez")
    if (
        status is DispatchStatus.PLANNED
        and route.intent in {"jira-detail", "jira-history"}
        and route.jira_issue_key is None
    ):
        raise PolicyViolation("Jira niyeti resolved issue key olmadan planned olamaz")
    if proposal is not None:
        _validate_proposal(proposal)
        conflict = _proposal_conflict_reason(proposal, route=route)
        if conflict is not None:
            return _proposal_conflict_dispatch(
                route,
                conflict_reason=conflict,
                proposal_intent=proposal.intent,
            )
    return _route_dispatch(route, status)


def _route_dispatch(route: RequestRoute, status: DispatchStatus) -> CapabilityDispatch:
    """Dogrulanmis route'un deterministic, allowlisted plan sonucunu uretir."""

    scope = ProjectScope(
        project_refs=route.project_refs,
        unavailable_project_refs=route.unavailable_project_refs,
        family_ref=route.family_ref,
    )
    return CapabilityDispatch(
        validated_intent=ValidatedIntent(
            intent=route.intent,
            capability=route.strategy,
            status=status,
            reason_codes=route.reason_codes,
            clarifications=_clarifications(route, status),
        ),
        scope=scope,
        query_digest=route.query_digest,
        route_digest=route.as_dict()["route_digest"],
        catalog_digest=route.catalog_digest,
    )
