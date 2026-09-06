from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.request_routing import (
    RegisteredProject,
    load_project_families,
    route_request,
)
from zekam.domain.errors import ValidationFailed

PROJECTS = (
    RegisteredProject("gpu-fusion", ("gpu",)),
    RegisteredProject("sky-spring-ui", ("sky-ui",)),
    RegisteredProject("sky-microservis", ("sky-backend",)),
)


def _route(question: str):  # type: ignore[no-untyped-def]
    return route_request(
        question,
        catalog=load_project_families(),
        registered_projects=PROJECTS,
    )


def test_general_question_never_falls_back_to_only_or_ready_project() -> None:
    route = _route("Dünya çevresi kaç kilometredir?")

    assert route.status == "general"
    assert route.intent == "general-knowledge"
    assert route.strategy == "general-research"
    assert route.project_refs == ()
    assert route.as_dict()["provider_calls"] == 0
    assert route.as_dict()["grants_authority"] is False


def test_router_instruction_does_not_turn_general_question_into_project_work() -> None:
    route = _route(
        "Dünya'nın çevresi kaç kilometredir? Zekam router kararını uygula ve kısa cevap ver."
    )

    assert route.status == "general"
    assert route.strategy == "general-research"


def test_flow_instruction_does_not_turn_project_question_into_mutation() -> None:
    route = _route("Sky UI'da BASE_URL hangi değere ayarlı? Router ve RAG akışını uygula.")

    assert route.status == "selected"
    assert route.intent == "project-question"
    assert route.strategy == "single-project-rag"


def test_family_alias_is_token_bounded_and_skype_is_not_sky() -> None:
    assert _route("Skype nasıl çalışır?").status == "general"
    assert _route("Sky nasıl çalışır?").project_refs == (
        "sky-spring-ui",
        "sky-microservis",
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Sky login butonunun CSS sınıfı nedir?", ("sky-spring-ui",)),
        ("Sky JWT controller endpointi nedir?", ("sky-microservis",)),
        ("Sky müşteri akışı nasıl çalışıyor?", ("sky-spring-ui", "sky-microservis")),
        ("sky-spring-ui hangi routerı kullanıyor?", ("sky-spring-ui",)),
        ("GPU servisleri hangileri?", ("gpu-fusion",)),
        ("Gelir Paylaşımı Uygulaması servisleri hangileri?", ("gpu-fusion",)),
        ("TTBP login butonunun CSS sınıfı nedir?", ("sky-spring-ui",)),
        ("Türk Telekom Bayi Portalı controllerları neler?", ("sky-microservis",)),
        (
            "Satış Kanalları Yönetim müşteri akışı nasıl çalışıyor?",
            ("sky-spring-ui", "sky-microservis"),
        ),
    ],
)
def test_family_and_role_routing(question: str, expected: tuple[str, ...]) -> None:
    route = _route(question)

    assert route.status == "selected"
    assert route.project_refs == expected


def test_multi_project_mutation_requires_role_clarification() -> None:
    route = _route("Sky müşteri akışını değiştir")

    assert route.status == "clarification-required"
    assert route.intent == "code-change"
    assert route.strategy == "clarify-project-role"


def test_multiple_direct_projects_of_different_lengths_fail_closed() -> None:
    route = _route("gpu-fusion ve sky-microservis ilişkisini açıkla")

    assert route.status == "clarification-required"
    assert route.strategy == "clarify-project"
    assert route.project_refs == ("gpu-fusion", "sky-microservis")
    assert route.reason_codes == ("multiple-direct-projects",)


def test_direct_project_and_other_family_alias_fail_closed() -> None:
    route = _route("GPU ve Sky müşteri akışı nasıl çalışıyor?")

    assert route.status == "clarification-required"
    assert route.strategy == "clarify-project-family"
    assert route.project_refs == (
        "gpu-fusion",
        "sky-microservis",
        "sky-spring-ui",
    )
    assert "multiple-project-families" in route.reason_codes


@pytest.mark.parametrize(
    ("question", "family", "issue_key"),
    [
        ("GPU 5077 Jira işi", "gpu", "SKYRSM-5077"),
        ("SKYRSM-5077 detayları", "gpu", "SKYRSM-5077"),
        ("Sky 11306", "sky", "TLCSKY-11306"),
        ("TLCSKY-11306 detayları", "sky", "TLCSKY-11306"),
        ("Gelir Paylaşımı Uygulaması 5077", "gpu", "SKYRSM-5077"),
        ("TTBP 11306", "sky", "TLCSKY-11306"),
    ],
)
def test_jira_routes_use_reviewed_family_prefixes(
    question: str, family: str, issue_key: str
) -> None:
    route = _route(question)

    assert route.status == "selected"
    assert route.intent == "jira-detail"
    assert route.family_ref == family
    assert route.jira_issue_key == issue_key


def test_bare_issue_number_and_conflicting_alias_key_fail_closed() -> None:
    bare = _route("11306 detayları")
    conflict = _route("GPU TLCSKY-11306")

    assert bare.status == "general"
    assert conflict.status == "clarification-required"
    assert conflict.strategy == "clarify-jira-project"


@pytest.mark.parametrize("retired_alias", ["GPO", "GOP"])
def test_retired_gpu_aliases_are_not_project_signals(retired_alias: str) -> None:
    route = _route(f"{retired_alias} servisleri hangileri?")

    assert route.status == "general"
    assert route.project_refs == ()


def test_missing_family_member_is_visible_and_not_invented() -> None:
    route = route_request(
        "Sky müşteri akışı",
        catalog=load_project_families(),
        registered_projects=(RegisteredProject("sky-spring-ui"),),
    )

    assert route.status == "clarification-required"
    assert route.strategy == "project-family-incomplete"
    assert route.project_refs == ("sky-spring-ui",)
    assert route.unavailable_project_refs == ("sky-microservis",)


def test_catalog_rejects_cross_family_alias_collision(tmp_path: Path) -> None:
    path = tmp_path / "families.yaml"
    path.write_text(
        """schema: zekam-project-families/v1
families:
  - family_ref: one
    display_name: One
    aliases: [same]
    jira_prefix: ONE
    members: [{project_ref: one-app, role: ui}]
  - family_ref: two
    display_name: Two
    aliases: [same]
    jira_prefix: TWO
    members: [{project_ref: two-app, role: backend}]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationFailed, match="global unique"):
        load_project_families(path)


def test_route_digest_is_deterministic_and_question_is_not_echoed() -> None:
    first = _route("Sky controller nerede?").as_dict()
    second = _route("Sky controller nerede?").as_dict()

    assert first == second
    assert "question" not in first
