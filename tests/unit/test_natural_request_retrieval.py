"""WP-03.03: proje kapsamina bagli natural request retrieval sozlesmesi."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zekam.application import natural_request_retrieval as subject
from zekam.application.home import HomeLayout
from zekam.application.natural_request_retrieval import retrieve_natural_request
from zekam.application.natural_request_scope import (
    NaturalRequestScope,
    resolve_natural_request_scope,
)
from zekam.application.request_routing import (
    RegisteredProject,
    load_project_families,
    route_request,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

REGISTERED = frozenset({"gpu-fusion", "sky-spring-ui", "sky-microservis"})
PROJECTS = tuple(RegisteredProject(ref) for ref in sorted(REGISTERED))
CATALOG = load_project_families()

READY = {"state": "ready", "index_readable": True, "query_available": True}
NO_INDEX = {"state": "unavailable", "index_readable": False, "query_available": False}


def _selected_scope(refs: tuple[str, ...], selected_by: str) -> NaturalRequestScope:
    return NaturalRequestScope(
        status="selected",
        project_refs=refs,
        reason_codes=(),
        jira_issue_key=None,
        selected_by=selected_by,
        route_digest=digest({"route": "fixture", "refs": list(refs)}),
    )


def _routed_scope(question: str) -> NaturalRequestScope:
    route = route_request(question, catalog=CATALOG, registered_projects=PROJECTS)
    return resolve_natural_request_scope(route, registered_project_refs=REGISTERED)


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    raise_error: type[Exception] | None = None,
) -> list[Any]:
    calls: list[Any] = []

    def fake_status(home: Path, slug: str) -> dict[str, Any]:
        calls.append(("status", home, slug))
        if raise_error is not None:
            raise raise_error(f"fake {slug}")
        result = status if status is not None else READY
        return dict(result)

    def fake_query(home: Path, slug: str, question: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(("query", home, slug, question, dict(kwargs)))
        return {"state": "answered", "project": slug, "citations": []}

    monkeypatch.setattr(subject, "project_rag_status", fake_status)
    monkeypatch.setattr(subject, "query_registered_project", fake_query)
    return calls


def _query_calls(calls: list[Any]) -> list[tuple[Path, str, str, dict[str, Any]]]:
    return [item[1:] for item in calls if item[0] == "query"]


def test_clarification_scope_returns_minimal_clarification_without_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(monkeypatch)
    scope = _routed_scope("Bu kod nasil calisiyor?")
    assert scope.status == "clarification-required"

    outcome = retrieve_natural_request(
        scope, tmp_path, "Bu kod nasil calisiyor?", registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "clarification-required"
    assert outcome["reason_codes"] == ["project-context-without-project"]
    assert outcome["results"] == []
    assert outcome["query_count"] == 0
    assert calls == []


def test_general_scope_does_not_query_or_clarify_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(monkeypatch)
    scope = _routed_scope("Dunya cevresi kac kilometredir?")
    assert scope.status == "general"

    outcome = retrieve_natural_request(
        scope, tmp_path, "Dunya cevresi kac kilometredir?", registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "clarification-required"
    assert outcome["reason_codes"] == ["no-project-signal"]
    assert outcome["project_refs"] == []
    assert calls == []


def test_single_selected_project_makes_exactly_one_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fakes(monkeypatch)
    scope = _selected_scope(("gpu-fusion",), "route")

    outcome = retrieve_natural_request(
        scope, tmp_path, "GPU servisleri hangileri?", registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "answered"
    assert outcome["query_count"] == 1
    entry = outcome["results"][0]
    assert entry["project_ref"] == "gpu-fusion"
    assert entry["selected_by"] == "route"
    assert entry["status"] == "answered"
    assert entry["unavailable_reason"] is None
    assert entry["result"]["project"] == "gpu-fusion"


def test_single_selected_project_forwards_question_and_remote_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(monkeypatch)
    scope = _selected_scope(("gpu-fusion",), "explicit")

    retrieve_natural_request(
        scope,
        tmp_path,
        "GPU servisleri hangileri?",
        registered_project_refs=REGISTERED,
    )

    ((home, slug, question, kwargs),) = _query_calls(calls)
    assert home == tmp_path.resolve()
    assert slug == "gpu-fusion"
    assert question == "GPU servisleri hangileri?"
    assert kwargs == {"authorize_remote_query": False}


def test_remote_authorization_is_forwarded_only_when_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = _selected_scope(("gpu-fusion",), "route")
    for authorized in (False, True):
        calls = _install_fakes(monkeypatch)
        outcome = retrieve_natural_request(
            scope,
            tmp_path,
            "GPU servisleri hangileri?",
            registered_project_refs=REGISTERED,
            authorize_remote_query=authorized,
        )
        ((_, _, _, kwargs),) = _query_calls(calls)
        assert kwargs == {"authorize_remote_query": authorized}
        assert outcome["remote_query_authorized"] is authorized


def test_route_fanout_queries_each_ref_with_separately_tagged_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(monkeypatch)
    scope = _routed_scope("Sky musteri akisini karsilastir")
    assert scope.status == "selected"
    assert scope.selected_by == "route-fanout"

    outcome = retrieve_natural_request(
        scope, tmp_path, "Sky musteri akisini karsilastir", registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "answered"
    assert outcome["query_count"] == 2
    assert [item["project_ref"] for item in outcome["results"]] == [
        "sky-spring-ui",
        "sky-microservis",
    ]
    assert all(item["selected_by"] == "route-fanout" for item in outcome["results"])
    assert all(item["status"] == "answered" for item in outcome["results"])
    assert {item["result"]["project"] for item in outcome["results"]} == {
        "sky-spring-ui",
        "sky-microservis",
    }
    assert [(item[1], item[2]) for item in _query_calls(calls)] == [
        ("sky-spring-ui", "Sky musteri akisini karsilastir"),
        ("sky-microservis", "Sky musteri akisini karsilastir"),
    ]


def test_multi_refs_without_route_fanout_do_not_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for selected_by in ("route", "explicit"):
        calls = _install_fakes(monkeypatch)
        scope = _selected_scope(("sky-spring-ui", "sky-microservis"), selected_by)

        outcome = retrieve_natural_request(
            scope, tmp_path, "Sky musteri akisi", registered_project_refs=REGISTERED
        )

        assert outcome["status"] == "clarification-required"
        assert outcome["reason_codes"] == ["retrieval-fanout-not-authorized"]
        assert outcome["results"] == []
        assert outcome["query_count"] == 0
        assert _query_calls(calls) == []


def test_family_fanout_requires_explicit_comparison(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(monkeypatch)
    scope = _routed_scope("Sky musteri akisi nasil calisiyor?")
    assert scope.selected_by == "route-fanout"
    outcome = retrieve_natural_request(
        scope, tmp_path, "Sky musteri akisi nasil calisiyor?", registered_project_refs=REGISTERED
    )
    assert outcome["status"] == "clarification-required"
    assert outcome["reason_codes"] == ["comparison-intent-required"]
    assert calls == []


def test_single_ref_with_fanout_selected_by_does_not_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(monkeypatch)
    scope = _selected_scope(("gpu-fusion",), "route-fanout")

    outcome = retrieve_natural_request(
        scope, tmp_path, "GPU servisleri", registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "clarification-required"
    assert outcome["reason_codes"] == ["retrieval-selected-by-invalid"]
    assert _query_calls(calls) == []


def test_selected_ref_missing_from_registered_set_clarifies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(monkeypatch)
    scope = _selected_scope(("gpu-fusion", "unknown-project"), "route-fanout")

    outcome = retrieve_natural_request(
        scope,
        tmp_path,
        "Karstir",
        registered_project_refs=frozenset({"gpu-fusion"}),
    )

    assert outcome["status"] == "clarification-required"
    assert outcome["reason_codes"] == ["retrieval-project-unregistered"]
    assert _query_calls(calls) == []


def test_missing_index_reports_unavailable_without_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(monkeypatch, status=NO_INDEX)
    scope = _selected_scope(("gpu-fusion",), "route")

    outcome = retrieve_natural_request(
        scope, tmp_path, "GPU servisleri", registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "unavailable"
    assert outcome["query_count"] == 0
    assert outcome["reason_codes"] == ["index-unavailable"]
    entry = outcome["results"][0]
    assert entry["status"] == "unavailable"
    assert entry["unavailable_reason"] == "index-unavailable"
    assert entry["result"] is None
    assert _query_calls(calls) == []


def test_registered_project_without_index_does_not_start_indexing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    HomeLayout(home).ensure()
    database = home / "state" / "operational.db"
    bootstrap(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        uow.create_project(slug="gpu-fusion", display_name="GPU")
        uow.commit()
    before = {path.relative_to(home) for path in home.rglob("*")}
    scope = _selected_scope(("gpu-fusion",), "route")
    outcome = retrieve_natural_request(
        scope, home, "GPU servisleri", registered_project_refs=frozenset({"gpu-fusion"})
    )
    assert outcome["status"] == "unavailable"
    assert outcome["reason_codes"] == ["index-unavailable"]
    assert outcome["query_count"] == 0
    assert outcome["observation_write_possible"] is False
    assert {path.relative_to(home) for path in home.rglob("*")} == before


def test_unresolvable_project_is_unavailable_and_siblings_still_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(monkeypatch)
    scope = _routed_scope("Sky musteri akisini karsilastir")

    def flaky_status(home: Path, slug: str) -> dict[str, Any]:
        calls.append(("status", home, slug))
        if slug == "sky-microservis":
            raise ValidationFailed("fake unresolvable")
        return dict(READY)

    monkeypatch.setattr(subject, "project_rag_status", flaky_status)

    outcome = retrieve_natural_request(
        scope, tmp_path, "Sky musteri akisini karsilastir", registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "answered"
    assert outcome["query_count"] == 1
    assert outcome["reason_codes"] == ["project-unavailable"]
    assert [item["unavailable_reason"] for item in outcome["results"]] == [
        None,
        "project-unavailable",
    ]
    assert [(item[1],) for item in _query_calls(calls)] == [("sky-spring-ui",)]


def test_blocked_scope_acl_reports_unavailable_without_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(monkeypatch, raise_error=PolicyViolation)
    scope = _selected_scope(("gpu-fusion",), "route")

    outcome = retrieve_natural_request(
        scope, tmp_path, "GPU servisleri", registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "unavailable"
    assert outcome["reason_codes"] == ["knowledge-scope-blocked"]
    assert outcome["results"][0]["unavailable_reason"] == "knowledge-scope-blocked"
    assert _query_calls(calls) == []


def test_blocked_status_is_not_reported_as_missing_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(
        monkeypatch,
        status={"state": "blocked", "index_readable": False, "blocked_reason": "acl"},
    )
    scope = _selected_scope(("gpu-fusion",), "route")
    outcome = retrieve_natural_request(
        scope, tmp_path, "GPU servisleri", registered_project_refs=REGISTERED
    )
    assert outcome["reason_codes"] == ["knowledge-scope-blocked"]
    assert _query_calls(calls) == []


def test_query_failure_is_isolated_per_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_fakes(monkeypatch)
    scope = _routed_scope("Sky musteri akisini karsilastir")

    def flaky_query(home: Path, slug: str, question: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(("query", home, slug, question, dict(kwargs)))
        if slug == "sky-spring-ui":
            raise PolicyViolation("query blocked")
        return {"state": "answered", "project": slug, "citations": []}

    monkeypatch.setattr(subject, "query_registered_project", flaky_query)
    outcome = retrieve_natural_request(
        scope, tmp_path, "Sky musteri akisini karsilastir", registered_project_refs=REGISTERED
    )
    assert outcome["query_count"] == 2
    assert outcome["reason_codes"] == ["query-unavailable"]
    assert [item["status"] for item in outcome["results"]] == ["unavailable", "answered"]


def test_result_document_reports_observation_write_and_is_deterministic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fakes(monkeypatch)
    scope = _selected_scope(("gpu-fusion",), "route")

    first = retrieve_natural_request(
        scope, tmp_path, "GPU servisleri", registered_project_refs=REGISTERED
    )
    second = retrieve_natural_request(
        scope, tmp_path, "GPU servisleri", registered_project_refs=REGISTERED
    )

    assert first == second
    assert first["source_access"] == "read-only"
    assert first["observation_write_possible"] is True
    assert first["index_triggered"] is False
    assert first["grants_authority"] is False
    assert first["scope"]["grants_authority"] is False
    assert first["retrieval_digest"].startswith("sha256:")


def test_question_must_be_bounded_text(tmp_path: Path) -> None:
    scope = _selected_scope(("gpu-fusion",), "route")
    for question in ("", "   "):
        with pytest.raises(ValidationFailed, match="bounded non-empty"):
            retrieve_natural_request(scope, tmp_path, question, registered_project_refs=REGISTERED)
