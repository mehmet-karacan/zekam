"""WP-03.03: ``zekam ask`` belirsiz/kayitsiz proje sinirinin integration kaniti.

Bu suite, ``--project`` verilmeyen ``ask`` ucu icin kanonik
``route_request`` + alias-farkindali ``resolve_question_project`` yolunu
gercek katalog, gercek operasyonel store ve gercek ``HomeLayout`` uzerinden
dogrular:

- Proje sinyali olan belirsiz/kayitsiz istek, sorgu veya indeksleme
  baslatmadan kullanicinin anlayacagi en kisa Turkce aciklama sorusunu
  uretir (``zekam-ask-clarification/v1``, exit 65).
- Teknik reason code veya komut ismi kullanicidan istenmez.
- Genel (proje sinyali olmayan) soru bilincli olarak Zekam projesine
  zorlanmaz; mevcut fail-fast davranisi korunur.
- ``--project`` ghost icin fail-fast korunur; hata metni insanca ve
  kayitli proje slug'larini onerir.
- Tek proje ve acik coklu karsilastirma (fan-out) JSON sozlesmeleri
  degismez.

RAG query seam'i mock'tur; bu suite'te gercek indeks fixture'i yoktur.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zekam.application import natural_request_retrieval as retrieval_subject
from zekam.application.home import HomeLayout
from zekam.application.project_rag_runtime import (
    AskProjectUnresolved,
    resolve_question_project,
    resolve_registered_project,
)
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli import main as cli

pytestmark = pytest.mark.integration

UI_ID = "22222222-2222-4222-8222-222222222222"
MS_ID = "33333333-3333-4333-8333-333333333333"


def _comparison_home(tmp_path: Path) -> Path:
    """Iki kayitli proje tasiyan, indeks dosyasi olmayan gecici ZEKAM_HOME."""

    layout = HomeLayout(tmp_path / "zekam-home").ensure()
    for slug in ("sky-spring-ui", "sky-microservis"):
        layout.ensure_project(slug)
    database = layout.root / "state" / "operational.db"
    bootstrap(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        uow.create_project(slug="sky-spring-ui", display_name="Sky Spring UI")
        uow.create_project(slug="sky-microservis", display_name="Sky Microservis")
        uow.commit()
    return layout.root


def _empty_home(tmp_path: Path) -> Path:
    """Kayitli proje tasiyacak bos gecici ZEKAM_HOME."""

    layout = HomeLayout(tmp_path / "zekam-home").ensure()
    bootstrap(layout.root / "state" / "operational.db")
    return layout.root


def _assert_no_query(result: Any, query_calls: list[str]) -> dict[str, Any]:
    """Clarification ciktisinin sozlesmesini ve sorgu olmamasini dogrular."""

    assert result.exit_code == 65, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-ask-clarification/v1"
    assert document["status"] == "clarification-required"
    assert document["route"] == "natural-request-scope"
    assert document["provider_calls"] == 0
    assert document["index_triggered"] is False
    assert document["grants_authority"] is False
    assert isinstance(document["reason_codes"], list)
    assert document["question"], "kullanicinin anlayacagi Turkce soru zorunlu"
    assert "next_step" in document
    assert query_calls == []
    return document


def _norm(output: str) -> str:
    """Konsol sari/boyut sarmalamasini normalize eder."""

    return " ".join(output.split())


def test_ambiguous_unregistered_family_clarifies_without_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kayitli olmayan aile alias'i (proje sinyali) aciklama sorusu uretir;
    sorgu/indeksleme baslatilmaz, sessiz fallback yok, teknik kod istenmez.

    Bos home kullanilir: aile uyeleri kayitli olmadigindan route
    ``selected-projects-unavailable`` olarak clarification doner ve CLI
    alias-farkindali cozumleyiciden netlestirme ister."""

    home = _empty_home(tmp_path)
    query_calls: list[str] = []

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        query_calls.append(slug)
        return {}

    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)
    monkeypatch.setattr(cli, "query_registered_project", fake_query)

    result = CliRunner().invoke(
        cli.app, ["ask", "sky-tile mimarisi nedir?", "--home", str(home), "--json"]
    )

    document = _assert_no_query(result, query_calls)
    assert document["reason_codes"] == ["family-all-members", "selected-projects-unavailable"]
    assert document["project_refs"] == []
    # Soru, soruda sinyal edilen aile adini dogrudan sorar; teknik code istemez.
    assert "sky-tile" in document["question"]
    assert "reason" not in document["question"]
    assert "--project" in document["next_step"]


def test_ambiguous_context_question_clarifies_without_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proje baglam terimi tasiyan baglamsiz soru aciklama sorusu uretir;
    sorgu/indeksleme baslatilmaz."""

    home = _comparison_home(tmp_path)
    query_calls: list[str] = []

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        query_calls.append(slug)
        return {}

    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)
    monkeypatch.setattr(cli, "query_registered_project", fake_query)

    result = CliRunner().invoke(
        cli.app, ["ask", "Bu proje nasil calisiyor?", "--home", str(home), "--json"]
    )

    document = _assert_no_query(result, query_calls)
    assert document["reason_codes"] == ["project-context-without-project"]
    assert document["project_refs"] == []
    assert "proje" in document["question"].casefold()


def test_ambiguous_multi_project_route_clarifies_without_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kayitli iki proje sinyal eden, karsilastirma icermeyen istek
    aciklama sorusu uretir; tek projeye sessizce yonlendirilmez."""

    home = _comparison_home(tmp_path)
    query_calls: list[str] = []

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        query_calls.append(slug)
        return {}

    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)
    monkeypatch.setattr(cli, "query_registered_project", fake_query)

    result = CliRunner().invoke(
        cli.app,
        ["ask", "sky-spring-ui ve sky-microservis mimarisi nedir?", "--home", str(home), "--json"],
    )

    document = _assert_no_query(result, query_calls)
    assert document["reason_codes"] == ["multiple-direct-projects"]
    assert document["project_refs"] == ["sky-microservis", "sky-spring-ui"]
    assert "sky-spring-ui" in document["question"]
    assert "sky-microservis" in document["question"]


def test_jira_project_signal_clarifies_without_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cozulemeyen Jira prefix'i aciklama sorusu uretir; Jira MCP/probe
    baslatilmaz, teknik komut ismi kullanicidan istenmez."""

    home = _comparison_home(tmp_path)
    query_calls: list[str] = []

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        query_calls.append(slug)
        return {}

    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)
    monkeypatch.setattr(cli, "query_registered_project", fake_query)

    result = CliRunner().invoke(cli.app, ["ask", "ACME-1234 ne?", "--home", str(home), "--json"])

    document = _assert_no_query(result, query_calls)
    assert document["reason_codes"] == ["jira-project-unresolved"]
    assert document["jira_issue_key"] is None
    assert "Jira" in document["question"]


def test_general_question_keeps_failure_without_project_forcing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proje sinyali tasiyacak genel soru bilincli olarak Zekam projesine
    zorlanmaz; mevcut fail-fast davranisi (exit 70) korunur."""

    home = _comparison_home(tmp_path)
    query_calls: list[str] = []

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        query_calls.append(slug)
        return {}

    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)
    monkeypatch.setattr(cli, "query_registered_project", fake_query)

    result = CliRunner().invoke(
        cli.app, ["ask", "Dunya cevresi kac kilometredir?", "--home", str(home), "--json"]
    )

    assert result.exit_code == 70, result.output
    assert "Soru bir Zekam projesine baglanmiyor" in _norm(result.output)
    assert query_calls == []


def test_ghost_project_keeps_fail_fast_with_human_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--project`` ghost icin fail-fast korunur; hata metni insanca net ve
    kayitli proje slug'larini onerir; sorgu baslatilmaz."""

    home = _comparison_home(tmp_path)
    query_calls: list[str] = []

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        query_calls.append(slug)
        return {}

    monkeypatch.setattr(cli, "query_registered_project", fake_query)

    result = CliRunner().invoke(
        cli.app,
        [
            "ask",
            "Bu proje nasil calisiyor?",
            "--project",
            "ghost-project",
            "--home",
            str(home),
            "--json",
        ],
    )

    assert result.exit_code == 70, result.output
    output = _norm(result.output)
    assert "ghost-project kayitli bir Zekam projesi degil" in output
    assert "Kayitli projeler: sky-spring-ui, sky-microservis." in output
    assert query_calls == []


def test_ghost_project_without_registered_projects_stays_human(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kayitli proje yokken ghost fail-fast hata metni onerim basligi
    uretmez ama yine insanca kalir."""

    home = _empty_home(tmp_path)
    query_calls: list[str] = []

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        query_calls.append(slug)
        return {}

    monkeypatch.setattr(cli, "query_registered_project", fake_query)

    result = CliRunner().invoke(
        cli.app,
        [
            "ask",
            "Bu proje nasil calisiyor?",
            "--project",
            "ghost-project",
            "--home",
            str(home),
            "--json",
        ],
    )

    assert result.exit_code == 70, result.output
    output = _norm(result.output)
    assert "ghost-project kayitli bir Zekam projesi degil" in output
    assert "Henüz kayitli proje yok." in output
    assert query_calls == []


def test_single_project_keeps_legacy_ask_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tek kayitli proje sorusu (``--project`` ya da acik) mevcut
    ``zekam-ask-result/v1`` sozlesmesini ve dogrudan query semantigini korur."""

    home = _comparison_home(tmp_path)
    question = "sky-spring-ui servisi nasil calisiyor?"
    observed: dict[str, Any] = {}

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        observed.update(home=home_arg, project=slug, question=q, **kwargs)
        return {
            "schema": "zekam-embedded-rag-result/v1",
            "state": "answered",
            "project_id": UI_ID,
            "project_slug": slug,
            "answer_excerpt": "sky-spring-ui cevabi",
        }

    monkeypatch.setattr(cli, "query_registered_project", fake_query)

    explicit = CliRunner().invoke(
        cli.app,
        [
            "ask",
            question,
            "--project",
            "sky-spring-ui",
            "--home",
            str(home),
            "--json",
        ],
    )
    assert explicit.exit_code == 0, explicit.output
    document = json.loads(explicit.output)
    assert document["schema"] == "zekam-ask-result/v1"
    assert "route" not in document
    assert document["project_ref"] == "sky-spring-ui"
    assert document["retrieval"]["answer_excerpt"] == "sky-spring-ui cevabi"

    natural = CliRunner().invoke(cli.app, ["ask", question, "--home", str(home), "--json"])
    assert natural.exit_code == 0, natural.output
    document = json.loads(natural.output)
    assert document["schema"] == "zekam-ask-result/v1"
    assert "route" not in document
    assert document["project_ref"] == "sky-spring-ui"
    assert observed["project"] == "sky-spring-ui"
    assert observed["question"] == question


def test_open_comparison_keeps_fanout_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Acik iki kayitli proje karsilastirma sorusu mevcut
    ``zekam-ask-multi-result/v1`` fan-out sozlesmesini korur."""

    home = _comparison_home(tmp_path)
    question = "sky-tile mimarisini karsilastir"
    docs = {
        "sky-spring-ui": {
            "schema": "zekam-embedded-rag-result/v1",
            "state": "answered",
            "project_id": UI_ID,
            "answer_excerpt": "sky-spring-ui cevabi",
        },
        "sky-microservis": {
            "schema": "zekam-embedded-rag-result/v1",
            "state": "answered",
            "project_id": MS_ID,
            "answer_excerpt": "sky-microservis cevabi",
        },
    }
    calls: list[str] = []

    def fake_status(home_arg: Path, slug: str) -> dict[str, Any]:
        return {
            "state": "ready",
            "index_readable": True,
            "project_id": UI_ID if slug == "sky-spring-ui" else MS_ID,
        }

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(slug)
        return docs[slug] | {"project_slug": slug}

    monkeypatch.setattr(retrieval_subject, "project_rag_status", fake_status)
    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)

    result = CliRunner().invoke(cli.app, ["ask", question, "--home", str(home), "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-ask-multi-result/v1"
    assert document["route"] == "natural-request-retrieval"
    retrieval = document["retrieval"]
    assert retrieval["status"] == "answered"
    assert retrieval["query_count"] == 2
    assert retrieval["index_triggered"] is False
    assert [item["project_ref"] for item in retrieval["results"]] == [
        "sky-spring-ui",
        "sky-microservis",
    ]
    assert calls == ["sky-spring-ui", "sky-microservis"]


def test_resolver_raises_ask_unresolved_for_unregistered_family(tmp_path: Path) -> None:
    """Gercek registry: alias-farkindali cozumleyici kayitli olmayan aile
    sinyalinde ``AskProjectUnresolved`` doner; genel soruda ValidationFailed."""

    home = _empty_home(tmp_path)

    with pytest.raises(AskProjectUnresolved) as excinfo:
        resolve_question_project(home, "sky-tile mimarisi nedir?")
    assert excinfo.value.project_refs == ()
    assert excinfo.value.jira_issue_key is None
    assert "selected-projects-unavailable" in excinfo.value.reason_codes
    assert "sky-tile" in excinfo.value.matched_terms

    with pytest.raises(ValidationFailed, match="Soru bir Zekam projesine baglanmiyor"):
        resolve_question_project(home, "Dunya cevresi kac kilometredir?")


def test_resolver_single_project_still_resolves(tmp_path: Path) -> None:
    """Gercek registry: tek kayitli proje sorusu alias-farkindaligiyla
    cozulmeye devam eder (davranis degismez)."""

    home = _comparison_home(tmp_path)

    assert (
        resolve_question_project(home, "sky-spring-ui servisi nasil calisiyor?") == "sky-spring-ui"
    )


def test_registered_project_resolver_ghost_message(tmp_path: Path) -> None:
    """Gercek registry: ghost ``--project`` cozumlemesi fail-fast kalir ve
    onerilen kayitli slug'lari tasiyor."""

    home = _comparison_home(tmp_path)

    with pytest.raises(ValidationFailed) as excinfo:
        resolve_registered_project(home, "ghost-project")
    message = str(excinfo.value)
    assert "ghost-project kayitli bir Zekam projesi degil" in message
    assert "Kayitli projeler:" in message
    assert "sky-spring-ui" in message
    assert "sky-microservis" in message
