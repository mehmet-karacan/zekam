"""WP-03.02-A: deterministic, authority-free project signal precedence."""

from __future__ import annotations

from zekam.application.natural_request_scope import (
    ContextSignal,
    NaturalRequestScope,
    resolve_natural_request_scope,
)
from zekam.application.request_routing import (
    RegisteredProject,
    load_project_families,
    route_request,
)

REGISTERED = frozenset({"gpu-fusion", "sky-spring-ui", "sky-microservis"})
PROJECTS = tuple(RegisteredProject(ref) for ref in sorted(REGISTERED))
CATALOG = load_project_families()


def _scope(
    question: str,
    *,
    explicit_project_ref: str | None = None,
    work: ContextSignal | None = None,
    session: ContextSignal | None = None,
) -> NaturalRequestScope:
    route = route_request(question, catalog=CATALOG, registered_projects=PROJECTS)
    return resolve_natural_request_scope(
        route,
        registered_project_refs=REGISTERED,
        explicit_project_ref=explicit_project_ref,
        work=work,
        session=session,
    )


def test_general_question_has_no_project_or_authority() -> None:
    scope = _scope("Dunya cevresi kac kilometredir?")
    assert scope.status == "general"
    assert scope.project_refs == ()
    assert scope.as_dict()["grants_authority"] is False
    assert scope.as_dict()["read_only"] is True


def test_explicit_project_matching_route_is_selected() -> None:
    scope = _scope("GPU servisleri hangileri?", explicit_project_ref="gpu-fusion")
    assert scope.project_refs == ("gpu-fusion",)
    assert scope.selected_by == "explicit"


def test_explicit_project_conflicting_with_jira_prefix_clarifies() -> None:
    scope = _scope("SKYRSM-5077 detaylari", explicit_project_ref="sky-microservis")
    assert scope.status == "clarification-required"
    assert scope.project_refs == ()
    assert scope.jira_issue_key is None
    assert "explicit-route-conflict" in scope.reason_codes


def test_jira_issue_comes_only_from_resolved_route() -> None:
    issue = _scope("SKYRSM-5077 detaylari")
    port = _scope("gpu-fusion backend portu 9001 mi?")
    assert issue.jira_issue_key == "SKYRSM-5077"
    assert port.jira_issue_key is None


def test_verified_work_matching_route_is_context_only() -> None:
    scope = _scope("GPU servisleri hangileri?", work=ContextSignal("gpu-fusion", True))
    assert scope.status == "selected"
    assert scope.project_refs == ("gpu-fusion",)
    assert scope.as_dict()["grants_authority"] is False


def test_work_conflicting_with_route_clarifies() -> None:
    scope = _scope("GPU servisleri hangileri?", work=ContextSignal("sky-spring-ui", True))
    assert scope.reason_codes == ("work-route-conflict",)


def test_unverified_work_or_session_cannot_select_scope() -> None:
    for scope in (
        _scope("GPU servisleri hangileri?", work=ContextSignal("gpu-fusion", False)),
        _scope("GPU servisleri hangileri?", session=ContextSignal("gpu-fusion", False)),
    ):
        assert scope.status == "clarification-required"
        assert scope.project_refs == ()


def test_explicit_and_verified_session_conflict_clarifies() -> None:
    scope = _scope(
        "GPU servisleri hangileri?",
        explicit_project_ref="gpu-fusion",
        session=ContextSignal("sky-spring-ui", True),
    )
    assert scope.status == "clarification-required"
    assert scope.project_refs == ()


def test_selected_family_fanout_stays_scoped() -> None:
    scope = _scope("Sky musteri akisi nasil calisiyor?")
    assert scope.status == "selected"
    assert scope.project_refs == ("sky-spring-ui", "sky-microservis")
    assert scope.selected_by == "route-fanout"


def test_explicit_project_narrows_family_route() -> None:
    scope = _scope("Sky musteri akisi nasil calisiyor?", explicit_project_ref="sky-spring-ui")
    assert scope.project_refs == ("sky-spring-ui",)


def test_multi_project_mutation_does_not_select_scope() -> None:
    scope = _scope("Sky musteri akisini degistir")
    assert scope.status == "clarification-required"
    assert scope.project_refs == ()


def test_unregistered_explicit_project_clarifies() -> None:
    scope = _scope("GPU servisleri hangileri?", explicit_project_ref="another-project")
    assert scope.status == "clarification-required"
    assert scope.reason_codes == ("explicit-project-unregistered",)


def test_result_is_deterministic() -> None:
    first = _scope("GPU servisleri hangileri?").as_dict()
    second = _scope("GPU servisleri hangileri?").as_dict()
    assert first == second
    assert first["scope_digest"].startswith("sha256:")
