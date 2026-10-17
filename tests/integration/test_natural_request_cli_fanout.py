"""WP-03.03: ``zekam ask`` fan-out zincirinin integration kaniti.

Bu suite, CLI'in kanonik yolunun alt katmanlarini (``route_request`` +
``resolve_natural_request_scope`` + ``retrieve_natural_request``) gercek
katalog, gercek operasyonel store ve gercek ``HomeLayout`` uzerinden
dogrular. RAG query seam'i mock'tur: bu suite'te gercek coklu indeks
fixture'i YOKTUR, dolayisiyla "cevap" kaniti mock seam sonucu seviyesindedir.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zekam.application import natural_request_retrieval as subject
from zekam.application.home import HomeLayout
from zekam.application.natural_request_retrieval import retrieve_natural_request
from zekam.application.natural_request_scope import resolve_natural_request_scope
from zekam.application.request_routing import (
    RegisteredProject,
    load_project_families,
    route_request,
)
from zekam.domain.canonical import digest
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli import main as cli

pytestmark = pytest.mark.integration

REGISTERED = frozenset({"sky-spring-ui", "sky-microservis"})
PROJECTS = tuple(RegisteredProject(ref) for ref in sorted(REGISTERED))
CATALOG = load_project_families()

UI_ID = "22222222-2222-4222-8222-222222222222"
MS_ID = "33333333-3333-4333-8333-333333333333"
READY = {"state": "ready", "index_readable": True}

#: "sky-tile" aile alias'idir; iki kayitli uye (sky-spring-ui, sky-microservis)
#: paralel RAG stratejisiyle secilir. Proje slug'larinin ikisini de aciklamak
#: ``multiple-direct-projects`` clarification'i uretir, o yuzden kullanilmaz.
COMPARISON_QUESTION = "sky-tile mimarisini karsilastir"
NO_COMPARISON_QUESTION = "sky-tile mimarisi"
SINGLE_QUESTION = "sky-spring-ui servisi nasil calisiyor?"


def _comparison_home(tmp_path: Path) -> Path:
    """Iki kayitli proje tasiyan, indeks dosyasi olmayan gecici ZEKAM_HOME."""

    layout = HomeLayout(tmp_path / "zekam-home").ensure()
    for slug in sorted(REGISTERED):
        layout.ensure_project(slug)
    database = layout.root / "state" / "operational.db"
    bootstrap(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        uow.create_project(slug="sky-spring-ui", display_name="Sky Spring UI")
        uow.create_project(slug="sky-microservis", display_name="Sky Microservis")
        uow.commit()
    return layout.root


def _resolve_scope(question: str, **kwargs: Any) -> Any:
    route = route_request(
        question,
        catalog=CATALOG,
        registered_projects=PROJECTS,
    )
    return resolve_natural_request_scope(route, registered_project_refs=REGISTERED, **kwargs)


def test_open_comparison_question_resolves_to_route_fanout_scope() -> None:
    """Acik iki kayitli proje karsilastirma sorusu deterministic olarak
    ``route-fanout`` kapsamina iner (yalniz route/scope karari)."""

    scope = _resolve_scope(COMPARISON_QUESTION)

    assert scope.status == "selected"
    assert scope.project_refs == ("sky-spring-ui", "sky-microservis")
    assert scope.selected_by == "route-fanout"
    assert scope.reason_codes == ()


def test_single_project_question_resolves_to_route_scope() -> None:
    """Tek proje sorusu ``route`` kapsamina iner; fan-out'a acilmaz."""

    scope = _resolve_scope(SINGLE_QUESTION)

    assert scope.status == "selected"
    assert scope.project_refs == ("sky-spring-ui",)
    assert scope.selected_by == "route"


def test_multi_project_question_without_comparison_intent_clarifies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Coklu proje sinyali acik karsilastirma ifadesi olmadan clarification
    ister; retrieval probe/query calistirir (sessiz fallback yok)."""

    question = NO_COMPARISON_QUESTION
    calls: list[str] = []

    def fake_status(home: Path, slug: str) -> dict[str, Any]:
        calls.append(f"status:{slug}")
        return dict(READY) | {"project_id": UI_ID}

    def fake_query(home: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(f"query:{slug}")
        return {}

    monkeypatch.setattr(subject, "project_rag_status", fake_status)
    monkeypatch.setattr(subject, "query_registered_project", fake_query)
    scope = _resolve_scope(question)
    assert scope.status == "selected"
    assert scope.selected_by == "route-fanout"

    outcome = retrieve_natural_request(
        scope, tmp_path, question, registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "clarification-required"
    assert outcome["reason_codes"] == ["comparison-intent-required"]
    assert outcome["query_count"] == 0
    assert outcome["results"] == []
    assert calls == []


def test_explicit_project_ref_narrows_fanout_to_single_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--project`` eslenmesi acik fan-out sorusunu tek projeye daraltir ve
    tam olarak bir scoped query uretir (mock seam kaniti)."""

    calls: list[tuple[str, str]] = []

    def fake_status(home: Path, slug: str) -> dict[str, Any]:
        calls.append(("status", slug))
        return dict(READY) | {"project_id": UI_ID if slug == "sky-spring-ui" else MS_ID}

    def fake_query(home: Path, slug: str, question: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(("query", slug))
        return {
            "state": "answered",
            "project_id": UI_ID if slug == "sky-spring-ui" else MS_ID,
            "project_slug": slug,
        }

    monkeypatch.setattr(subject, "project_rag_status", fake_status)
    monkeypatch.setattr(subject, "query_registered_project", fake_query)
    scope = _resolve_scope(COMPARISON_QUESTION, explicit_project_ref="sky-spring-ui")
    assert scope.selected_by == "explicit"

    outcome = subject.retrieve_natural_request(
        scope, tmp_path, COMPARISON_QUESTION, registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "answered"
    assert outcome["query_count"] == 1
    assert [slug for kind, slug in calls if kind == "query"] == ["sky-spring-ui"]
    assert outcome["results"][0]["result"]["project_slug"] == "sky-spring-ui"


def test_canonical_chain_fans_out_two_scoped_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """route -> scope -> retrieve zinciri iki proje icin ayri, etiketli
    query'ler uretir; sessiz fallback yok (mock seam kaniti)."""

    calls: list[tuple[str, str, str]] = []

    def fake_status(home: Path, slug: str) -> dict[str, Any]:
        calls.append(("status", slug, ""))
        return dict(READY) | {"project_id": UI_ID if slug == "sky-spring-ui" else MS_ID}

    def fake_query(home: Path, slug: str, question: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(("query", slug, question))
        return {
            "state": "answered",
            "project_id": UI_ID if slug == "sky-spring-ui" else MS_ID,
            "project_slug": slug,
            "answer_excerpt": f"{slug} cevabi",
        }

    monkeypatch.setattr(subject, "project_rag_status", fake_status)
    monkeypatch.setattr(subject, "query_registered_project", fake_query)
    scope = _resolve_scope(COMPARISON_QUESTION)

    outcome = subject.retrieve_natural_request(
        scope, tmp_path, COMPARISON_QUESTION, registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "answered"
    assert outcome["project_refs"] == ["sky-spring-ui", "sky-microservis"]
    assert outcome["query_count"] == 2
    assert outcome["index_triggered"] is False
    assert outcome["grants_authority"] is False
    by_ref = {item["project_ref"]: item for item in outcome["results"]}
    assert set(by_ref) == REGISTERED
    for slug in sorted(REGISTERED):
        entry = by_ref[slug]
        assert entry["status"] == "answered"
        assert entry["result"]["project_slug"] == slug
        assert entry["result"]["project_id"] in (UI_ID, MS_ID)
    assert [(slug, question) for _kind, slug, question in calls if _kind == "query"] == [
        ("sky-spring-ui", COMPARISON_QUESTION),
        ("sky-microservis", COMPARISON_QUESTION),
    ]
    # Ayni girdiyle tekrar calistirmak birebir ayni govdeyi uretir (deterministik).
    again = subject.retrieve_natural_request(
        scope, tmp_path, COMPARISON_QUESTION, registered_project_refs=REGISTERED
    )
    assert again == outcome


def test_missing_indices_report_unavailable_without_indexing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gercek home'da iki kayitli projenin indeksi yoksa zincir her projeyi
    ``index-unavailable`` raporlar; query yok, indeksleme yok, dosya agaci
    degismez (gercek probe + mock query seam)."""

    home = _comparison_home(tmp_path)
    before = {path.relative_to(home) for path in home.rglob("*")}
    query_calls: list[str] = []

    def fake_query(home_arg: Path, slug: str, question: str, **kwargs: Any) -> dict[str, Any]:
        query_calls.append(slug)
        return {}

    monkeypatch.setattr(subject, "query_registered_project", fake_query)
    scope = _resolve_scope(COMPARISON_QUESTION)
    outcome = retrieve_natural_request(
        scope, home, COMPARISON_QUESTION, registered_project_refs=REGISTERED
    )

    assert outcome["status"] == "unavailable"
    assert outcome["query_count"] == 0
    assert outcome["index_triggered"] is False
    assert "index-unavailable" in outcome["reason_codes"]
    assert [(item["status"], item["unavailable_reason"]) for item in outcome["results"]] == [
        ("unavailable", "index-unavailable"),
        ("unavailable", "index-unavailable"),
    ]
    assert query_calls == []
    assert {path.relative_to(home) for path in home.rglob("*")} == before


def test_unregistered_explicit_ref_clarifies_before_any_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kayitli olmayan bir proje referansi probe/query'den once clarification
    olur (sessiz indeksleme/fallback yok)."""

    calls: list[str] = []

    def fake_status(home: Path, slug: str) -> dict[str, Any]:
        calls.append(f"status:{slug}")
        return dict(READY) | {"project_id": UI_ID}

    monkeypatch.setattr(subject, "project_rag_status", fake_status)
    scope = _resolve_scope(COMPARISON_QUESTION, explicit_project_ref="ghost-project")
    assert scope.status == "clarification-required"

    outcome = retrieve_natural_request(
        scope,
        tmp_path,
        COMPARISON_QUESTION,
        registered_project_refs=REGISTERED,
    )
    assert outcome["status"] == "clarification-required"
    assert "explicit-project-unregistered" in outcome["reason_codes"]
    assert outcome["query_count"] == 0
    assert calls == []


@pytest.mark.parametrize("output_json", [False, True])
def test_ask_comparison_outputs_scoped_sources_or_preserves_json_citations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output_json: bool
) -> None:
    import json

    home = _comparison_home(tmp_path)

    def fake_status(_home: Path, slug: str) -> dict[str, Any]:
        return dict(READY) | {"project_id": UI_ID if slug == "sky-spring-ui" else MS_ID}

    def fake_query(_home: Path, slug: str, _question: str, **_kwargs: Any) -> dict[str, Any]:
        project_id = UI_ID if slug == "sky-spring-ui" else MS_ID
        return {
            "state": "answered", "project_id": project_id, "project_slug": slug,
            "answer_excerpt": "Servis [bold]veriyi[/bold] okur.", "evidence_found": True,
            "citations": [{
                "project_scope": project_id, "source_ref": "src/service.py",
                "source_digest": digest({"source": slug}),
                "locator": {"relative_path": "src/service.py", "line_start": 3, "line_end": 8},
            }],
        }

    monkeypatch.setattr(subject, "project_rag_status", fake_status)
    monkeypatch.setattr(subject, "query_registered_project", fake_query)
    args = ["ask", COMPARISON_QUESTION, "--home", str(home)]
    if output_json:
        args.append("--json")
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    if output_json:
        payload = json.loads(result.output)
        assert payload["answer"]["proposed_explanation_accepted"] is True
        assert len(payload["retrieval"]["results"]) == 2
        for entry in payload["retrieval"]["results"]:
            assert entry["result"]["citations"][0]["project_scope"] == entry["project_id"]
    else:
        assert "Kaynaklar:" in result.output
        assert "sky-spring-ui: src/service.py (satir 3-8)" in result.output
        assert "sky-microservis: src/service.py (satir 3-8)" in result.output
        assert "[bold]veriyi[/bold]" in result.output


@pytest.mark.parametrize("change", [{"project_slug": "foreign"}, {"grants_authority": True}])
def test_single_answer_wrapper_rejects_foreign_scope_or_authority(change: dict[str, Any]) -> None:
    payload = {"project_id": UI_ID, "state": "answered", **change}
    wrapped = cli._single_natural_request_retrieval(
        "sky-spring-ui", SINGLE_QUESTION, payload, authorize_remote_query=False,
    )
    assert wrapped["status"] == "unavailable"
    assert wrapped["results"][0]["result"] is None


def test_legacy_citations_do_not_invent_an_evidence_found_flag() -> None:
    payload = {"project_id": UI_ID, "state": "answered", "citations": [{"chunk_id": "legacy"}]}
    wrapped = cli._single_natural_request_retrieval(
        "sky-spring-ui", SINGLE_QUESTION, payload, authorize_remote_query=False,
    )
    assert wrapped["results"][0]["result"]["evidence_found"] is False


@pytest.mark.parametrize("failure", [OSError, sqlite3.OperationalError])
def test_single_ask_storage_outage_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: type[Exception]
) -> None:
    home = _comparison_home(tmp_path)

    def broken(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise failure("private-path-and-raw-error")

    monkeypatch.setattr(cli, "query_registered_project", broken)
    result = CliRunner().invoke(
        cli.app, ["ask", SINGLE_QUESTION, "--project", "sky-spring-ui", "--home", str(home)]
    )
    assert result.exit_code != 0
    assert "Proje arama kaynagi su anda okunamiyor" in result.output
    assert "private-path-and-raw-error" not in result.output
