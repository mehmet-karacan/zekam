"""WP-03.01: Dogal dil dispatch katmaninin izinli yetkinlik ve kapsam testleri."""

from __future__ import annotations

import pytest

from zekam.application.natural_language_dispatch import (
    ALLOWED_CAPABILITIES,
    ALLOWED_INTENTS,
    CapabilityDispatch,
    DispatchStatus,
    ModelIntentProposal,
    plan_capability_dispatch,
)
from zekam.application.request_routing import (
    RegisteredProject,
    load_project_families,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed

PROJECTS = (
    RegisteredProject("gpu-fusion", ("gpu",)),
    RegisteredProject("sky-spring-ui", ("sky-ui",)),
    RegisteredProject("sky-microservis", ("sky-backend",)),
)


def _plan(question: str, projects: tuple[RegisteredProject, ...] = PROJECTS):  # type: ignore[no-untyped-def]
    return plan_capability_dispatch(
        question, catalog=load_project_families(), registered_projects=projects
    )


def _plan_with(
    question: str,
    proposal: ModelIntentProposal | None,
    projects: tuple[RegisteredProject, ...] = PROJECTS,
) -> CapabilityDispatch:
    return plan_capability_dispatch(
        question,
        catalog=load_project_families(),
        registered_projects=projects,
        proposal=proposal,
    )


# ---------------------------------------------------------------------------
# Genel soru: Zekam veya bir projeye yanlis hedeflenmez
# ---------------------------------------------------------------------------


def test_general_question_declines_without_project_scope() -> None:
    dispatch = _plan("Dünya çevresi kaç kilometredir?")

    assert dispatch.validated_intent.status is DispatchStatus.DECLINED
    assert dispatch.validated_intent.intent == "general-knowledge"
    assert dispatch.validated_intent.capability == "general-research"
    assert dispatch.scope.project_refs == ()
    assert dispatch.scope.unavailable_project_refs == ()
    assert dispatch.scope.family_ref is None
    assert dispatch.ready is False
    assert dispatch.grants_authority is False


def test_router_instruction_does_not_target_zekam_or_any_project() -> None:
    dispatch = _plan(
        "Dünya'nın çevresi kaç kilometredir? Zekam router kararını uygula ve kısa cevap ver."
    )

    assert dispatch.validated_intent.status is DispatchStatus.DECLINED
    assert dispatch.validated_intent.intent == "general-knowledge"
    assert dispatch.scope.project_refs == ()


def test_general_dispatch_dict_declares_no_provider_and_no_authority() -> None:
    body = _plan("Istatistik nedir?").as_dict()

    assert body["provider_calls"] == 0
    assert body["grants_authority"] is False
    assert body["ready"] is False
    assert body["tool_invocation_performed"] is False


# ---------------------------------------------------------------------------
# Uygun proje: selected route planned plana baglanir
# ---------------------------------------------------------------------------


def test_registered_project_question_plans_single_project_rag() -> None:
    dispatch = _plan("Sky UI'da BASE_URL hangi değere ayarlı?")

    assert dispatch.validated_intent.status is DispatchStatus.PLANNED
    assert dispatch.validated_intent.intent == "project-question"
    assert dispatch.validated_intent.capability == "single-project-rag"
    assert dispatch.validated_intent.clarifications == ()
    assert dispatch.scope.project_refs == ("sky-spring-ui",)
    assert dispatch.scope.unavailable_project_refs == ()
    assert dispatch.scope.family_ref == "sky"
    # Plan uretildi: ready sahte native readiness degil, plan_available plan varligini soylar.
    assert dispatch.ready is False
    assert dispatch.plan_available is True
    assert dispatch.grants_authority is False


def test_family_role_question_plans_parallel_project_rag() -> None:
    dispatch = _plan("Sky müşteri akışı nasıl çalışıyor?")

    assert dispatch.validated_intent.capability == "parallel-project-rag"
    assert dispatch.scope.project_refs == ("sky-spring-ui", "sky-microservis")


def test_real_change_request_plans_agentic_capability() -> None:
    dispatch = _plan("gpu-fusion içindeki BatchJobController'a yeni endpoint ekle")

    assert dispatch.validated_intent.status is DispatchStatus.PLANNED
    assert dispatch.validated_intent.intent == "code-change"
    assert dispatch.validated_intent.capability == "project-agentic"
    assert dispatch.scope.project_refs == ("gpu-fusion",)


def test_jira_detail_plans_jira_capability_with_resolved_key() -> None:
    dispatch = _plan("GPU 5077 Jira işi")

    assert dispatch.validated_intent.status is DispatchStatus.PLANNED
    assert dispatch.validated_intent.intent == "jira-detail"
    assert dispatch.validated_intent.capability == "jira-mcp"
    assert dispatch.query_digest.startswith("sha256:")
    assert dispatch.route_digest.startswith("sha256:")


def test_jira_history_question_plans_project_history_capability() -> None:
    dispatch = _plan("SKYRSM-5659 kapsamında hangi değişiklikler yapıldı?")

    assert dispatch.validated_intent.intent == "jira-history"
    assert dispatch.validated_intent.capability == "project-history"
    # SKYRSM öneki incelenen family config'te gpu ailesine bağlıdır.
    assert dispatch.scope.project_refs == ("gpu-fusion",)


def test_planned_dispatch_dict_is_immutable_shape_and_deterministic() -> None:
    first = _plan("GPU servisleri hangileri?").as_dict()
    second = _plan("GPU servisleri hangileri?").as_dict()

    assert first == second
    assert first["schema"] == "zekam-natural-language-dispatch/v1"
    assert first["status"] == "planned"
    # ready sahte native readiness degildir; her zaman False kalir.
    assert first["ready"] is False
    # plan uretildigi icin plan_available True'dir; bu native readiness degildir.
    assert first["plan_available"] is True
    assert first["provider_calls"] == 0
    assert first["grants_authority"] is False
    assert first["tool_invocation_performed"] is False


def test_declined_dispatch_dict_declares_no_plan_available() -> None:
    body = _plan("Istatistik nedir?").as_dict()

    assert body["status"] == "declined"
    assert body["ready"] is False
    assert body["plan_available"] is False


# ---------------------------------------------------------------------------
# Yanlis/halucine model niyeti: allowlist disi niyet fail-closed
# ---------------------------------------------------------------------------


def test_allowlist_rejects_unmodeled_execution_intent() -> None:
    """Execution ve yayin capability'leri allowlist'te modellenmemistir."""

    assert "execute-command" not in ALLOWED_INTENTS
    assert "run-shell" not in ALLOWED_CAPABILITIES
    assert "git-push" not in ALLOWED_CAPABILITIES
    assert "provider-call" not in ALLOWED_CAPABILITIES


def test_capability_allowlist_matches_route_strategy_vocabulary_exactly() -> None:
    """Allowlist, mevcut route intent/strategy degerlerinden kurulur; genisletilemez."""

    assert {
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
    } == ALLOWED_CAPABILITIES
    assert {
        "general-knowledge",
        "project-question",
        "code-change",
        "code-review",
        "test",
        "jira-detail",
        "jira-history",
    } == ALLOWED_INTENTS


def test_validated_intent_rejects_capability_outside_its_intent() -> None:
    from zekam.application.natural_language_dispatch import ValidatedIntent

    with pytest.raises(PolicyViolation, match="izinli degil"):
        ValidatedIntent(
            intent="general-knowledge",
            capability="project-agentic",
            status=DispatchStatus.DECLINED,
            reason_codes=(),
            clarifications=("general-research-required",),
        )


def test_validated_intent_rejects_unknown_capability() -> None:
    from zekam.application.natural_language_dispatch import ValidatedIntent

    with pytest.raises(PolicyViolation, match="allowlist"):
        ValidatedIntent(
            intent="project-question",
            capability="execute-command",
            status=DispatchStatus.CLARIFICATION_REQUIRED,
            reason_codes=(),
            clarifications=("x",),
        )


def test_validated_intent_rejects_planned_with_clarifications() -> None:
    from zekam.application.natural_language_dispatch import ValidatedIntent

    with pytest.raises(PolicyViolation, match="clarification"):
        ValidatedIntent(
            intent="project-question",
            capability="single-project-rag",
            status=DispatchStatus.PLANNED,
            reason_codes=(),
            clarifications=("target-missing",),
        )


# ---------------------------------------------------------------------------
# Kayitsiz proje: mutation/retrieval baslatmaz
# ---------------------------------------------------------------------------


def test_unregistered_project_family_fails_closed_without_plan() -> None:
    dispatch = plan_capability_dispatch(
        "Sky müşteri akışı",
        catalog=load_project_families(),
        registered_projects=(RegisteredProject("sky-spring-ui"),),
    )

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.capability == "project-family-incomplete"
    assert dispatch.ready is False
    assert dispatch.grants_authority is False
    assert "sky-microservis" in dispatch.scope.unavailable_project_refs


def test_unavailable_single_project_does_not_start_retrieval() -> None:
    dispatch = plan_capability_dispatch(
        "GPU servisleri hangileri?",
        catalog=load_project_families(),
        registered_projects=(),
    )

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.capability == "project-unavailable"
    assert dispatch.scope.project_refs == ()
    assert dispatch.scope.unavailable_project_refs == ("gpu-fusion",)
    assert dispatch.ready is False


def test_retired_alias_is_not_a_project_signal_and_declines() -> None:
    dispatch = _plan("GPO servisleri hangileri?")

    assert dispatch.validated_intent.status is DispatchStatus.DECLINED
    assert dispatch.scope.project_refs == ()
    assert dispatch.scope.unavailable_project_refs == ()


def test_project_context_without_registered_project_requires_clarification() -> None:
    dispatch = plan_capability_dispatch(
        "Sky müşteri akışı",
        catalog=load_project_families(),
        registered_projects=(),
    )

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.ready is False
    assert dispatch.scope.project_refs == ()


# ---------------------------------------------------------------------------
# Celiski: dogrudan soru ve proje secimi celistiginde fail-closed
# ---------------------------------------------------------------------------


def test_direct_project_and_other_family_alias_conflict_requires_clarification() -> None:
    dispatch = _plan("GPU ve Sky müşteri akışı nasıl çalışıyor?")

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.capability == "clarify-project-family"
    assert dispatch.ready is False
    assert set(dispatch.scope.project_refs) == {
        "gpu-fusion",
        "sky-spring-ui",
        "sky-microservis",
    }


def test_multiple_direct_projects_conflict_requires_clarification() -> None:
    dispatch = _plan("gpu-fusion ve sky-microservis ilişkisini açıkla")

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.capability == "clarify-project"
    assert dispatch.scope.project_refs == ("gpu-fusion", "sky-microservis")


def test_conflicting_jira_alias_and_key_requires_clarification() -> None:
    dispatch = _plan("GPU TLCSKY-11306")

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.capability == "clarify-jira-project"
    assert dispatch.ready is False


def test_multi_project_mutation_requires_role_clarification() -> None:
    dispatch = _plan("Sky müşteri akışını değiştir")

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.capability == "clarify-project-role"
    # Sinyal edilen mevcut projeler clarification'da görünür ama plan üretilmez.
    assert set(dispatch.scope.project_refs) == {"sky-spring-ui", "sky-microservis"}
    assert dispatch.ready is False


# ---------------------------------------------------------------------------
# Model onerisi (ModelIntentProposal): allowlist disindaki niyet fail-closed
# ---------------------------------------------------------------------------


def test_model_intent_proposal_accepts_only_allowlisted_intent() -> None:
    for intent in ALLOWED_INTENTS:
        proposal = ModelIntentProposal(intent=intent)
        assert proposal.intent == intent
        assert proposal.project_ref is None


def test_model_intent_proposal_rejects_hallucinated_intent_on_construction() -> None:
    with pytest.raises(ValidationFailed, match="allowlist"):
        ModelIntentProposal(intent="execute-command")


def test_model_intent_proposal_rejects_unknown_capability_intent() -> None:
    with pytest.raises(ValidationFailed, match="allowlist"):
        ModelIntentProposal(intent="run-shell")


def test_model_intent_proposal_rejects_non_portable_project_ref() -> None:
    with pytest.raises(ValidationFailed, match="Slug"):
        ModelIntentProposal(intent="project-question", project_ref="Sky UI")


def test_model_intent_proposal_rejects_invalid_slug_project_ref() -> None:
    with pytest.raises(ValidationFailed, match="Slug"):
        ModelIntentProposal(intent="project-question", project_ref="sky_spring_ui")


def test_dispatch_rejects_invalid_proposed_intent_even_if_constructed() -> None:
    """Bozuk onerinin dispatch sinirinden kacmasi bile plana giremez."""

    # Dataclass sozlesmesi bu oneriyi kurarken reddeder; dispatch sinirinin
    # fail-closed oldugunu gostermek icin bozuk oneriyi dogrudan olusturur.
    broken = object.__new__(ModelIntentProposal)
    object.__setattr__(broken, "intent", "execute-command")
    object.__setattr__(broken, "project_ref", None)
    with pytest.raises(PolicyViolation, match="allowlist"):
        plan_capability_dispatch(
            "Sky UI'da BASE_URL hangi değere ayarlı?",
            catalog=load_project_families(),
            registered_projects=PROJECTS,
            proposal=broken,
        )


def test_dispatch_clarifies_proposal_with_unknown_project_ref() -> None:
    """Kayitsiz model projesi hedef secmez; kullaniciya kapsam aciklatir."""

    dispatch = _plan_with(
        "Sky UI'da BASE_URL hangi değere ayarlı?",
        ModelIntentProposal(intent="project-question", project_ref="nonexistent"),
    )
    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.capability == "clarify-project"
    assert dispatch.plan_available is False
    assert dispatch.grants_authority is False


def test_proposal_intent_conflicting_with_route_requires_safe_clarification() -> None:
    """Niyet celiskisi: planned retrieval capability yetkiye cevrilmez."""

    dispatch = _plan_with(
        "Sky UI'da BASE_URL hangi değere ayarlı?",
        ModelIntentProposal(intent="code-change", project_ref="sky-spring-ui"),
    )

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.intent == "project-question"
    assert dispatch.validated_intent.capability == "clarify-project"
    assert dispatch.validated_intent.capability != "single-project-rag"
    assert dispatch.plan_available is False
    assert dispatch.ready is False
    assert "proposal-intent-conflicts-with-route" in dispatch.validated_intent.reason_codes
    # Celiskide route kapsam yetki/plan olarak tasinmaz; family baglamasizdir.
    assert dispatch.scope.family_ref is None


def test_proposal_project_conflicting_with_route_requires_safe_clarification() -> None:
    """Proje celiskisi: modelin secmise baglanmaz, clarification uretilir."""

    dispatch = _plan_with(
        "Sky UI'da BASE_URL hangi değere ayarlı?",
        ModelIntentProposal(intent="project-question", project_ref="gpu-fusion"),
    )

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.capability == "clarify-project"
    assert dispatch.plan_available is False
    assert "proposal-project-conflicts-with-route" in dispatch.validated_intent.reason_codes
    # Sorunun deterministic kapsamı korur ama plan uretilmez.
    assert dispatch.scope.project_refs == ("sky-spring-ui",)
    assert dispatch.scope.family_ref is None


def test_proposal_intent_conflicts_with_general_route_declines_not_plans() -> None:
    """Genel soruya mutation onerisi: general-research plana cevrilmez."""

    dispatch = _plan_with(
        "Dünya çevresi kaç kilometredir?",
        ModelIntentProposal(intent="code-change", project_ref="gpu-fusion"),
    )

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.capability == "clarify-project"
    assert dispatch.scope.project_refs == ()
    assert dispatch.scope.unavailable_project_refs == ()
    assert dispatch.ready is False
    assert dispatch.plan_available is False
    assert dispatch.grants_authority is False


def test_general_gpu_question_with_general_proposal_does_not_select_project() -> None:
    dispatch = _plan_with(
        "GPU mimarisi genel hatlariyla nasil calisir?",
        ModelIntentProposal(intent="general-knowledge"),
    )
    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.capability == "clarify-project"
    assert dispatch.plan_available is False
    assert dispatch.grants_authority is False


def test_jira_detail_route_with_different_proposal_intent_requires_clarification() -> None:
    """Jira route'unda celisen oneride jira-mcp capability yetkiye cevrilmez."""

    dispatch = _plan_with(
        "GPU 5077 Jira işi",
        ModelIntentProposal(intent="test", project_ref="gpu-fusion"),
    )

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.intent == "jira-detail"
    assert dispatch.validated_intent.capability == "clarify-jira-project"
    assert dispatch.validated_intent.capability != "jira-mcp"
    assert dispatch.plan_available is False
    assert "proposal-intent-conflicts-with-route" in dispatch.validated_intent.reason_codes


def test_code_change_proposal_never_grants_execution_or_authority() -> None:
    """Modelin `code-change` onerisi execution/onay yetkisi vermez."""

    dispatch = _plan_with(
        "gpu-fusion içindeki BatchJobController'a yeni endpoint ekle",
        ModelIntentProposal(intent="code-change", project_ref="gpu-fusion"),
    )
    body = dispatch.as_dict()

    assert dispatch.validated_intent.capability == "project-agentic"
    assert dispatch.plan_available is True
    assert dispatch.ready is False
    assert dispatch.grants_authority is False
    assert body["grants_authority"] is False
    assert body["provider_calls"] == 0
    assert body["tool_invocation_performed"] is False


def test_mutation_proposal_on_general_question_stays_declined_side() -> None:
    """Genel soruya mutation onerisi: ne plan ne capability uretir."""

    dispatch = _plan_with(
        "Istatistik nedir?",
        ModelIntentProposal(intent="code-change", project_ref="sky-spring-ui"),
    )

    assert dispatch.validated_intent.status is DispatchStatus.CLARIFICATION_REQUIRED
    assert dispatch.validated_intent.intent != "code-change"
    assert dispatch.validated_intent.capability != "project-agentic"
    assert dispatch.scope.project_refs == ()
    assert dispatch.plan_available is False
    assert dispatch.ready is False
    assert dispatch.grants_authority is False


def test_no_freeform_capability_injection_surface() -> None:
    """Niyet disinda freeform capability injection kabul edilmez; capability API yok."""

    with pytest.raises(TypeError):
        plan_capability_dispatch(
            "Sky UI'da BASE_URL hangi değere ayarlı?",
            catalog=load_project_families(),
            registered_projects=PROJECTS,
            capability="run-shell",  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        ModelIntentProposal(
            intent="project-question",
            capability="project-agentic",  # type: ignore[call-arg]
        )


def test_matching_proposal_preserves_deterministic_route_plan() -> None:
    """Celiskisiz oneride mevcut route_request davranisi birebir korunur."""

    baseline = _plan("Sky UI'da BASE_URL hangi değere ayarlı?")
    with_proposal = _plan_with(
        "Sky UI'da BASE_URL hangi değere ayarlı?",
        ModelIntentProposal(intent="project-question", project_ref="sky-spring-ui"),
    )

    # Onerinin varligi planin deterministik kimligini (digest/status/capability) degistirmez.
    assert baseline.as_dict() == with_proposal.as_dict()
    assert with_proposal.validated_intent.capability == "single-project-rag"
    assert with_proposal.scope.project_refs == ("sky-spring-ui",)
    assert with_proposal.scope.family_ref == "sky"
    assert with_proposal.ready is False
    assert with_proposal.plan_available is True


def test_proposal_does_not_grant_authority_or_invocation() -> None:
    """Oneri execution/onay yetkisi veya tool invocation uretmez."""

    dispatch = _plan_with(
        "Sky UI'da BASE_URL hangi değere ayarlı?",
        ModelIntentProposal(intent="project-question", project_ref="sky-spring-ui"),
    )
    body = dispatch.as_dict()

    assert body["grants_authority"] is False
    assert body["provider_calls"] == 0
    assert body["tool_invocation_performed"] is False
    assert body["ready"] is False


# ---------------------------------------------------------------------------
# Scope ve authority sozlesmesi
# ---------------------------------------------------------------------------


def test_dispatch_never_grants_authority_for_any_outcome() -> None:
    cases = (
        "Dünya çevresi kaç kilometredir?",
        "Sky UI'da BASE_URL hangi değere ayarlı?",
        "Sky müşteri akışını değiştir",
        "GPU TLCSKY-11306",
        "GPU servisleri hangileri?",
    )
    for question in cases:
        dispatch = _plan(question)
        assert dispatch.grants_authority is False, question
        assert dispatch.as_dict()["grants_authority"] is False, question
        assert dispatch.as_dict()["provider_calls"] == 0, question
        assert dispatch.as_dict()["tool_invocation_performed"] is False, question


def test_scope_carries_unavailable_refs_without_inventing_registered_ones() -> None:
    dispatch = plan_capability_dispatch(
        "Sky müşteri akışı",
        catalog=load_project_families(),
        registered_projects=(RegisteredProject("sky-spring-ui"),),
    )

    assert dispatch.scope.project_refs == ("sky-spring-ui",)
    assert dispatch.scope.unavailable_project_refs == ("sky-microservis",)


def test_query_digest_is_deterministic_and_question_is_not_echoed() -> None:
    first = _plan("Sky controller nerede?").as_dict()
    second = _plan("Sky controller nerede?").as_dict()

    assert first == second
    assert "question" not in first
    assert first["query_digest"] == second["query_digest"]


def test_planned_route_digest_matches_upstream_route_digest() -> None:
    from zekam.application.request_routing import route_request

    question = "GPU servisleri hangileri?"
    route = route_request(question, catalog=load_project_families(), registered_projects=PROJECTS)
    dispatch = _plan(question)

    assert dispatch.route_digest == route.as_dict()["route_digest"]
    assert dispatch.catalog_digest == route.catalog_digest


def test_validated_intent_rejects_planned_without_agent_roles_contract() -> None:
    """Planned karar için capability agent rolleri exact sözleşmeden gelir."""

    dispatch = _plan("gpu-fusion içindeki BatchJobController'a yeni endpoint ekle")

    assert dispatch.validated_intent.capability == "project-agentic"
    assert dispatch.validated_intent.status is DispatchStatus.PLANNED
