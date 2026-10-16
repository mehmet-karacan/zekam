"""Packaged project RAG and OpenCode-facing CLI contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zekam.application import natural_request_retrieval as retrieval_subject
from zekam.application.config import EmbeddingRoute
from zekam.application.home import HomeLayout
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli import main as cli
from zekam.application.request_routing import RequestRoute


def _gpu_fusion_home(tmp_path: Path) -> Path:
    """Tek kayitli gpu-fusion projesi tasiyan gecici ZEKAM_HOME (indeks dosyasi yok)."""

    layout = HomeLayout(tmp_path / "zekam-home").ensure()
    layout.ensure_project("gpu-fusion")
    bootstrap(layout.root / "state" / "operational.db")
    store = SQLiteOperationalStore(layout.root / "state" / "operational.db")
    with store.unit_of_work() as uow:
        uow.create_project(slug="gpu-fusion", display_name="GPU Fusion")
        uow.commit()
    return layout.root


def test_ask_can_return_stale_lexical_snapshot_without_remote_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _gpu_fusion_home(tmp_path)
    monkeypatch.setattr(cli, "resolve_question_project", lambda *_: "gpu-fusion")
    monkeypatch.setattr(
        cli,
        "query_registered_project",
        lambda *_args, **_kwargs: {
            "state": "lexical-only-degraded",
            "answer_excerpt": "son indekslenmis bilgi",
            "index_freshness": "stale",
            "stale_reasons": ["project-source-revision-stale"],
            "remote_provider_used": False,
        },
    )

    result = CliRunner().invoke(
        cli.app, ["ask", "gpu-fusion hangi servis?", "--home", str(home), "--json"]
    )

    assert result.exit_code == 0, result.output
    retrieval = json.loads(result.output)["retrieval"]
    assert retrieval["index_freshness"] == "stale"
    assert retrieval["remote_provider_used"] is False


def test_ask_preserves_remote_query_authorization_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _gpu_fusion_home(tmp_path)
    monkeypatch.setattr(cli, "resolve_question_project", lambda *_: "gpu-fusion")
    monkeypatch.setattr(
        cli,
        "query_registered_project",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PolicyViolation("Remote query embedding explicit authorization ister")
        ),
    )

    result = CliRunner().invoke(
        cli.app, ["ask", "gpu-fusion hangi servis?", "--home", str(home), "--json"]
    )

    assert result.exit_code == 77
    assert "Remote query embedding explicit authorization ister" in result.output


def test_ask_routes_exact_question_and_wraps_retrieval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _gpu_fusion_home(tmp_path)
    observed: dict[str, object] = {}

    def fake_query(  # type: ignore[no-untyped-def]
        home, project, question, *, opencode_config, authorize_remote_query
    ):
        observed.update(
            home=home,
            project=project,
            question=question,
            opencode_config=opencode_config,
            authorize_remote_query=authorize_remote_query,
        )
        return {
            "schema": "zekam-embedded-rag-result/v1",
            "state": "answered",
            "searched_channels": ["exact", "lexical", "dense"],
            "retrieval_digest": "sha256:" + "a" * 64,
            "answer_excerpt": "CREATE TABLE",
        }

    monkeypatch.setattr(cli, "resolve_question_project", lambda _home, _question: "gpu-fusion")
    monkeypatch.setattr(cli, "query_registered_project", fake_query)
    result = CliRunner().invoke(
        cli.app,
        [
            "ask",
            "GPU_USER.LOG_REPORT_CREATION:TABLE nedir?",
            "--home",
            str(home),
            "--json",
            "--authorize-remote-query",
        ],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["project_ref"] == "gpu-fusion"
    assert document["retrieval"]["state"] == "answered"
    assert document["retrieval"]["searched_channels"] == ["exact", "lexical", "dense"]
    assert observed["question"] == "GPU_USER.LOG_REPORT_CREATION:TABLE nedir?"


def test_ask_issue_history_uses_bounded_local_git_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _gpu_fusion_home(tmp_path)
    source = tmp_path / "gpu-fusion-source"
    source.mkdir()
    route = RequestRoute(
        "selected",
        "jira-history",
        "project-history",
        None,
        ("gpu-fusion",),
        (),
        ("researcher",),
        ("jira-key-resolved", "history-question"),
        (),
        (),
        "GPU-42",
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    )
    monkeypatch.setattr(cli, "route_request", lambda *_args, **_kwargs: route)
    monkeypatch.setattr(cli, "resolve_project_source", lambda *_args: source)
    monkeypatch.setattr(
        cli,
        "query_issue_history",
        lambda *_args: type(
            "History",
            (),
            {"as_dict": lambda self: {"commit_count": 1, "commits": [], "partial": False}},
        )(),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "ask",
            "GPU-42 hangi commitlerde degisti?",
            "--project",
            "gpu-fusion",
            "--home",
            str(home),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-ask-project-history/v1"
    assert document["route"] == "project-history"
    assert document["project_ref"] == "gpu-fusion"
    assert document["jira_issue_key"] == "GPU-42"
    assert document["provider_calls"] == 0
    assert document["read_only"] is True


def test_project_status_and_source_root_are_exposed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _gpu_fusion_home(tmp_path)
    source = tmp_path / "gpu-fusion"
    source.mkdir()
    monkeypatch.setattr(cli.project_commands, "resolve_project_source", lambda *_: source)
    monkeypatch.setattr(
        cli.project_commands,
        "project_rag_status",
        lambda *_: {
            "schema": "zekam-project-rag-status/v1",
            "project_slug": "gpu-fusion",
            "state": "ready",
            "chunk_count": 8496,
        },
    )
    runner = CliRunner()

    root_result = runner.invoke(
        cli.app, ["project", "source-root", "gpu-fusion", "--home", str(home), "--json"]
    )
    status_result = runner.invoke(
        cli.app, ["project", "status", "gpu-fusion", "--home", str(home), "--json"]
    )

    assert root_result.exit_code == 0, root_result.output
    assert json.loads(root_result.output)["source_root"] == str(source)
    assert status_result.exit_code == 0, status_result.output
    assert json.loads(status_result.output)["chunk_count"] == 8496


def test_project_json_output_is_ascii_safe_for_windows_code_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "Türkçe Örnek Proje"
    source.mkdir()
    monkeypatch.setattr(cli.project_commands, "_canonical_slug", lambda *_args, **_kwargs: "ornek")
    monkeypatch.setattr(cli.project_commands, "resolve_project_source", lambda *_args: source)

    result = CliRunner().invoke(
        cli.app, ["project", "source-root", "ornek", "--home", str(tmp_path), "--json"]
    )

    assert result.exit_code == 0, result.output
    assert result.output.isascii()
    assert "\\u00fc" in result.output
    assert json.loads(result.output)["source_root"] == str(source)


def test_project_resolve_and_show_are_exposed(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "gpu-fusion"
    source.mkdir()
    resolved = {
        "id": "project-1",
        "slug": "gpu-fusion",
        "display_name": "GPU Fusion",
        "status": "active",
        "revision": 1,
        "aliases": ["gpu"],
    }
    monkeypatch.setattr(
        cli.project_commands,
        "_resolve_project_document",
        lambda *_args, **_kwargs: resolved,
    )
    monkeypatch.setattr(cli.project_commands, "resolve_project_source", lambda *_: source)
    monkeypatch.setattr(
        cli.project_commands,
        "project_rag_status",
        lambda *_: {
            "schema": "zekam-project-rag-status/v1",
            "project_slug": "gpu-fusion",
            "state": "ready",
            "chunk_count": 8496,
        },
    )
    runner = CliRunner()

    resolve_result = runner.invoke(
        cli.app, ["project", "resolve", "gpu", "--home", str(tmp_path), "--json"]
    )
    show_result = runner.invoke(
        cli.app, ["project", "show", "gpu", "--home", str(tmp_path), "--json"]
    )

    assert resolve_result.exit_code == 0, resolve_result.output
    resolution = json.loads(resolve_result.output)
    assert resolution["schema"] == "zekam-project-resolution/v1"
    assert resolution["slug"] == "gpu-fusion"
    assert resolution["reference"] == "gpu"
    assert show_result.exit_code == 0, show_result.output
    detail = json.loads(show_result.output)
    assert detail["schema"] == "zekam-project-detail/v1"
    assert detail["source_root"] == str(source)
    assert detail["rag"]["state"] == "ready"


def test_project_citation_opens_pinned_verified_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _gpu_fusion_home(tmp_path)
    observed: dict[str, object] = {}

    def fake_read(home, project, chunk_id, *, generation_digest):  # type: ignore[no-untyped-def]
        observed.update(
            home=home,
            project=project,
            chunk_id=chunk_id,
            generation_digest=generation_digest,
        )
        return {
            "schema": "zekam-project-citation/v1",
            "locator_type": "database-object",
            "body": 'CREATE TABLE "GPU_USER"."LOG_REPORT_CREATION"',
            "verified": True,
        }

    monkeypatch.setattr(cli.project_commands, "read_project_citation", fake_read)
    result = CliRunner().invoke(
        cli.app,
        [
            "project",
            "citation",
            "gpu-fusion",
            "chunk-1",
            "--generation-digest",
            "sha256:" + "a" * 64,
            "--home",
            str(home),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["verified"] is True
    assert document["locator_type"] == "database-object"
    assert observed["chunk_id"] == "chunk-1"
    assert observed["generation_digest"] == "sha256:" + "a" * 64


def test_project_index_allows_source_only_without_database_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    def fake_index(  # type: ignore[no-untyped-def]
        home,
        project,
        *,
        oracle_config,
        opencode_config,
        batch_size,
        timeout_seconds,
        max_remote_provider_calls,
        authorize_odi_metadata,
        authorize_remote_source,
    ):
        observed.update(
            home=home,
            project=project,
            oracle_config=oracle_config,
            opencode_config=opencode_config,
            batch_size=batch_size,
            timeout_seconds=timeout_seconds,
            max_remote_provider_calls=max_remote_provider_calls,
            authorize_odi_metadata=authorize_odi_metadata,
            authorize_remote_source=authorize_remote_source,
        )
        return {
            "generation_digest": "sha256:" + "b" * 64,
            "chunk_count": 3,
            "database_access": "disabled",
        }

    monkeypatch.setattr(
        cli.project_commands,
        "_canonical_slug",
        lambda *_args, **_kwargs: "sky-spring-ui",
    )
    monkeypatch.setattr(cli.project_commands, "index_registered_project", fake_index)
    monkeypatch.setattr(
        cli.project_commands,
        "project_embedding_route",
        lambda _home: EmbeddingRoute.REMOTE,
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "project",
            "index",
            "sky-ui",
            "--authorize-remote-source",
            "--home",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["database_access"] == "disabled"
    assert observed["project"] == "sky-spring-ui"
    assert observed["oracle_config"] is None
    assert observed["authorize_remote_source"] is True
    assert observed["timeout_seconds"] == 1800.0
    assert observed["max_remote_provider_calls"] == 256

    configured = CliRunner().invoke(
        cli.app,
        [
            "project",
            "index",
            "sky-ui",
            "--authorize-remote-source",
            "--timeout-seconds",
            "45",
            "--max-provider-calls",
            "7",
            "--home",
            str(tmp_path),
            "--json",
        ],
    )
    assert configured.exit_code == 0, configured.output
    assert observed["timeout_seconds"] == 45.0
    assert observed["max_remote_provider_calls"] == 7


def test_project_local_route_does_not_require_remote_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        cli.project_commands,
        "project_embedding_route",
        lambda _home: EmbeddingRoute.LOCAL,
    )
    monkeypatch.setattr(
        cli.project_commands,
        "_canonical_slug",
        lambda *_args, **_kwargs: "local-project",
    )

    def fake_query(*_args, **kwargs):  # type: ignore[no-untyped-def]
        observed.update(kwargs)
        return {"state": "answered", "answer_excerpt": "local"}

    monkeypatch.setattr(cli.project_commands, "query_registered_project", fake_query)

    result = CliRunner().invoke(
        cli.app,
        [
            "project",
            "query",
            "local-project",
            "yerel soru",
            "--json",
            "--home",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "answered"
    assert observed["authorize_remote_query"] is False


def test_project_index_requires_database_authorization_only_when_configured(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            "project",
            "index",
            "gpu-fusion",
            "--oracle-config",
            "config/application.yaml",
            "--authorize-remote-source",
            "--home",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 77
    assert "Database metadata" in result.output


# -- WP7 (B08): retrieval-only vs generated-answer JSON contract ---------------


def test_ask_json_is_single_valid_document_with_wp7_fields_and_backward_compat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WP7-C-1 (B08): ``ask --json`` emits ONE valid JSON document carrying the
    new WP7 additive fields while preserving the existing v1 consumer fields
    (state, answer_excerpt, citations).  No progress/log is mixed into stdout."""
    home = _gpu_fusion_home(tmp_path)
    monkeypatch.setattr(cli, "resolve_question_project", lambda *_: "gpu-fusion")
    monkeypatch.setattr(
        cli,
        "query_registered_project",
        lambda *_args, **_kwargs: {
            "schema": "zekam-embedded-rag-result/v1",
            "state": "answered",
            "answer_excerpt": "CREATE TABLE LOG_REPORT_CREATION",
            "citations": [
                {
                    "source_id": "sha256:" + "b" * 64,
                    "project_scope": "gpu-fusion",
                    "source_ref": "oracle/table/LOG_REPORT_CREATION",
                    "source_revision": "rev-1",
                    "source_digest": "sha256:" + "c" * 64,
                    "content_digest": "sha256:" + "d" * 64,
                    "chunk_id": "chunk-1",
                    "locator_type": "database-object",
                    "locator": {"object_name": "LOG_REPORT_CREATION:TABLE"},
                    "retrieval_channels": ["exact"],
                    "rank_trace": {"fused_rank": 1},
                }
            ],
            "evidence_found": True,
            "retrieval_state": "answered-evidence",
            "generation_state": "not_generated",
            "answer_kind": "retrieval_evidence",
            "searched_channels": ["exact", "lexical", "dense"],
        },
    )

    result = CliRunner().invoke(
        cli.app, ["ask", "LOG_REPORT_CREATION nedir?", "--home", str(home), "--json"]
    )

    assert result.exit_code == 0, result.output
    # ONE valid JSON document, no stdout pollution.
    document = json.loads(result.output)
    assert isinstance(document, dict)
    retrieval = document["retrieval"]
    # Backward-compatible v1 fields still parse.
    assert retrieval["state"] == "answered"
    assert retrieval["answer_excerpt"] == "CREATE TABLE LOG_REPORT_CREATION"
    assert retrieval["citations"][0]["source_ref"] == "oracle/table/LOG_REPORT_CREATION"
    # New WP7 fields present and explicit that no generated answer was produced.
    assert retrieval["evidence_found"] is True
    assert retrieval["retrieval_state"] == "answered-evidence"
    assert retrieval["generation_state"] == "not_generated"
    assert retrieval["answer_kind"] == "retrieval_evidence"
    assert retrieval["answer_kind"] != "generated_answer"


def test_ask_json_providerless_never_claims_generated_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WP7-C-2 (B08): in a provider-less / abstained / stale result the CLI JSON
    contract reports generation_state=not_generated and answer_kind=abstained —
    never a fabricated generated answer."""
    home = _gpu_fusion_home(tmp_path)
    monkeypatch.setattr(cli, "resolve_question_project", lambda *_: "gpu-fusion")
    monkeypatch.setattr(
        cli,
        "query_registered_project",
        lambda *_args, **_kwargs: {
            "schema": "zekam-embedded-rag-result/v1",
            "state": "abstained-low-evidence",
            "answer_excerpt": None,
            "citations": [],
            "evidence_found": False,
            "retrieval_state": "abstained-low-evidence",
            "generation_state": "not_generated",
            "answer_kind": "abstained",
            "searched_channels": ["exact", "lexical"],
            "degraded_reason": "provider-unavailable",
            "index_freshness": "stale",
        },
    )

    result = CliRunner().invoke(
        cli.app, ["ask", "kuantum muz sulama protokolu", "--home", str(home), "--json"]
    )

    assert result.exit_code == 0, result.output
    retrieval = json.loads(result.output)["retrieval"]
    assert retrieval["state"] == "abstained-low-evidence"
    assert retrieval["evidence_found"] is False
    assert retrieval["generation_state"] == "not_generated"
    assert retrieval["answer_kind"] == "abstained"
    assert retrieval["answer_kind"] != "generated_answer"
    assert retrieval["citations"] == []


# -- WP-03.03: acik iki kayitli proje karsilastirma fan-out'u -------------------


def _comparison_question() -> str:
    """Aci iki kayitli proje sinyali: ``sky-tile`` aile alias'i iki uyeyi de
    (sky-spring-ui, sky-microservis) paralel RAG stratejisiyle secilir;
    ``karsilastir`` acik karsilastirma ifadesidir."""
    return "sky-tile mimarisini karsilastir"


def _comparison_home(tmp_path: Path) -> Path:
    """Iki kayitli proje tasiyan gecici ZEKAM_HOME (indeks dosyasi yok)."""

    layout = HomeLayout(tmp_path / "zekam-home").ensure()
    layout.ensure_project("sky-spring-ui")
    layout.ensure_project("sky-microservis")
    bootstrap(layout.root / "state" / "operational.db")
    store = SQLiteOperationalStore(layout.root / "state" / "operational.db")
    with store.unit_of_work() as uow:
        uow.create_project(slug="sky-spring-ui", display_name="Sky Spring UI")
        uow.create_project(slug="sky-microservis", display_name="Sky Microservis")
        uow.commit()
    return layout.root


def _fanout_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "schema": "zekam-embedded-rag-result/v1",
            "state": "answered",
            "project_id": "22222222-2222-4222-8222-222222222222",
            "answer_excerpt": "sky-spring-ui cevabi",
        },
        {
            "schema": "zekam-embedded-rag-result/v1",
            "state": "answered",
            "project_id": "33333333-3333-4333-8333-333333333333",
            "answer_excerpt": "sky-microservis cevabi",
        },
    )


def test_ask_without_project_on_open_comparison_fans_out_to_two_scoped_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Acik iki kayitli proje karsilastirma sorusu (``--project`` verilmeden)
    deterministic ``route_request`` + ``resolve_natural_request_scope`` +
    kanonik ``retrieve_natural_request`` yolundan iki AYRI scoped sonuc
    dondurur; sessiz fallback veya indeksleme olmaz."""

    home = _comparison_home(tmp_path)
    question = _comparison_question()
    docs = _fanout_documents()
    calls: list[tuple[str, dict[str, Any]]] = []
    project_ids = {
        "sky-spring-ui": "22222222-2222-4222-8222-222222222222",
        "sky-microservis": "33333333-3333-4333-8333-333333333333",
    }

    def fake_status(home_arg: Path, slug: str) -> dict[str, Any]:
        return {
            "state": "ready",
            "index_readable": True,
            "project_id": project_ids[slug],
        }

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((slug, q, dict(kwargs)))
        return (docs[0] if slug == "sky-spring-ui" else docs[1]) | {"project_slug": slug}

    monkeypatch.setattr(retrieval_subject, "project_rag_status", fake_status)
    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)
    # Legacy yolun fan-out sorusunda calismamasi gerekir (sessiz fallback yok).
    monkeypatch.setattr(cli, "resolve_question_project", lambda *_: pytest.fail("legacy fallback"))

    result = CliRunner().invoke(cli.app, ["ask", question, "--home", str(home), "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["schema"] == "zekam-ask-multi-result/v1"
    assert document["project_refs"] == ["sky-spring-ui", "sky-microservis"]
    assert document["route"] == "natural-request-retrieval"
    retrieval = document["retrieval"]
    assert retrieval["schema"] == "zekam-natural-request-retrieval/v1"
    assert retrieval["status"] == "answered"
    assert retrieval["project_refs"] == ["sky-spring-ui", "sky-microservis"]
    assert retrieval["index_triggered"] is False
    assert retrieval["query_count"] == 2
    assert retrieval["grants_authority"] is False
    by_ref = {item["project_ref"]: item for item in retrieval["results"]}
    assert set(by_ref) == {"sky-spring-ui", "sky-microservis"}
    assert by_ref["sky-spring-ui"]["status"] == "answered"
    assert by_ref["sky-spring-ui"]["result"]["answer_excerpt"] == "sky-spring-ui cevabi"
    assert by_ref["sky-spring-ui"]["result"]["project_slug"] == "sky-spring-ui"
    assert by_ref["sky-microservis"]["status"] == "answered"
    assert by_ref["sky-microservis"]["result"]["answer_excerpt"] == "sky-microservis cevabi"
    assert by_ref["sky-microservis"]["result"]["project_slug"] == "sky-microservis"
    # Deterministik scope sirasi: route_refs (aile ureye) ile birebir ayni.
    assert [item["project_ref"] for item in retrieval["results"]] == [
        "sky-spring-ui",
        "sky-microservis",
    ]
    # Her iki proje icin ayri, ayni soruyu tasiyan sorgu; sessiz fallback yok.
    assert [(slug, q) for slug, q, _kwargs in calls] == [
        ("sky-spring-ui", question),
        ("sky-microservis", question),
    ]
    assert all(
        kwargs["opencode_config"] == cli._DEFAULT_OPENCODE_CONFIG_FILE
        for _slug, _q, kwargs in calls
    )
    assert all(kwargs["authorize_remote_query"] is False for _slug, _q, kwargs in calls)


def test_ask_fanout_reports_missing_indices_without_indexing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fan-out'ta indeks eksik ise her proje ``unavailable``/``index-unavailable``
    olarak raporlanir; query cagrisi ve indeksleme baslatilmaz."""

    home = _comparison_home(tmp_path)
    question = _comparison_question()
    query_calls: list[str] = []

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        query_calls.append(slug)
        return {}

    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)

    result = CliRunner().invoke(cli.app, ["ask", question, "--home", str(home), "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["route"] == "natural-request-retrieval"
    retrieval = document["retrieval"]
    assert retrieval["status"] == "unavailable"
    assert retrieval["query_count"] == 0
    assert retrieval["index_triggered"] is False
    assert "index-unavailable" in retrieval["reason_codes"]
    assert [(item["status"], item["unavailable_reason"]) for item in retrieval["results"]] == [
        ("unavailable", "index-unavailable"),
        ("unavailable", "index-unavailable"),
    ]
    assert query_calls == []


def test_ask_fanout_partial_index_keeps_scoped_results_apart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fan-out'ta bir projenin indeksi eksikse o proje ``unavailable`` kalir;
    diyer proje kendi indeksinden cevap verir, sonuclar karistirilmez."""

    home = _comparison_home(tmp_path)
    question = _comparison_question()
    docs = _fanout_documents()
    queried: list[str] = []

    def fake_status(home_arg: Path, slug: str) -> dict[str, Any]:
        if slug == "sky-spring-ui":
            return {
                "state": "ready",
                "index_readable": True,
                "project_id": "22222222-2222-4222-8222-222222222222",
            }
        return {"state": "unavailable", "index_readable": False}

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        queried.append(slug)
        return docs[0] | {"project_slug": slug}

    monkeypatch.setattr(retrieval_subject, "project_rag_status", fake_status)
    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)

    result = CliRunner().invoke(cli.app, ["ask", question, "--home", str(home), "--json"])

    assert result.exit_code == 0, result.output
    retrieval = json.loads(result.output)["retrieval"]
    assert retrieval["status"] == "answered"
    assert retrieval["query_count"] == 1
    assert retrieval["index_triggered"] is False
    by_ref = {item["project_ref"]: item for item in retrieval["results"]}
    assert by_ref["sky-microservis"]["status"] == "unavailable"
    assert by_ref["sky-microservis"]["unavailable_reason"] == "index-unavailable"
    assert by_ref["sky-microservis"]["result"] is None
    assert by_ref["sky-spring-ui"]["status"] == "answered"
    assert by_ref["sky-spring-ui"]["result"]["project_slug"] == "sky-spring-ui"
    assert by_ref["sky-spring-ui"]["result"]["project_id"] == docs[0]["project_id"]
    assert queried == ["sky-spring-ui"]


def test_ask_fanout_preserves_remote_query_authorization_propagation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fan-out yolunda ``--authorize-remote-query`` her scoped sorguya birebir
    iletilir (semantik bozulmaz)."""

    home = _comparison_home(tmp_path)
    question = _comparison_question()
    docs = _fanout_documents()
    observed: list[bool] = []
    project_ids = {
        "sky-spring-ui": "22222222-2222-4222-8222-222222222222",
        "sky-microservis": "33333333-3333-4333-8333-333333333333",
    }

    def fake_status(home_arg: Path, slug: str) -> dict[str, Any]:
        return {
            "state": "ready",
            "index_readable": True,
            "project_id": project_ids[slug],
        }

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        observed.append(bool(kwargs["authorize_remote_query"]))
        return (docs[0] if slug == "sky-spring-ui" else docs[1]) | {"project_slug": slug}

    monkeypatch.setattr(retrieval_subject, "project_rag_status", fake_status)
    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)

    result = CliRunner().invoke(
        cli.app,
        ["ask", question, "--home", str(home), "--json", "--authorize-remote-query"],
    )

    assert result.exit_code == 0, result.output
    retrieval = json.loads(result.output)["retrieval"]
    assert retrieval["remote_query_authorized"] is True
    assert observed == [True, True]


def test_ask_fanout_refuses_cross_project_result_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bir projenin query sonucunu baska projeye baglandigini bildirse fail-closed
    olur; diyer projenin gecersiz sonucunu sessizce kabul etmez."""

    home = _comparison_home(tmp_path)
    question = _comparison_question()
    docs = _fanout_documents()
    project_ids = {
        "sky-spring-ui": "22222222-2222-4222-8222-222222222222",
        "sky-microservis": "33333333-3333-4333-8333-333333333333",
    }

    def fake_status(home_arg: Path, slug: str) -> dict[str, Any]:
        return {
            "state": "ready",
            "index_readable": True,
            "project_id": project_ids[slug],
        }

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        # sky-microservis indeksi, sky-spring-ui sonucunu dondurdu (cross-project).
        if slug == "sky-microservis":
            return docs[0] | {"project_slug": "sky-spring-ui"}
        return docs[0] | {"project_slug": slug}

    monkeypatch.setattr(retrieval_subject, "project_rag_status", fake_status)
    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)

    result = CliRunner().invoke(cli.app, ["ask", question, "--home", str(home), "--json"])

    assert result.exit_code == 0, result.output
    retrieval = json.loads(result.output)["retrieval"]
    by_ref = {item["project_ref"]: item for item in retrieval["results"]}
    # Baska projenin UUID/project_slug'u tasiyan cevap fail-closed olur.
    assert by_ref["sky-microservis"]["status"] == "unavailable"
    assert by_ref["sky-microservis"]["unavailable_reason"] == "project-binding-mismatch"
    assert by_ref["sky-microservis"]["result"] is None
    assert by_ref["sky-spring-ui"]["status"] == "answered"
    assert "project-binding-mismatch" in retrieval["reason_codes"]


def test_ask_multi_project_without_comparison_intent_clarifies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Acik iki kayitli proje sinyali acik karsilastirma ifadesi tasiyamazsa
    kanonik yol clarification doner; query/indeksleme/fallback yok."""

    home = _comparison_home(tmp_path)
    question = "sky-tile mimarisi"
    query_calls: list[str] = []

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        query_calls.append(slug)
        return {}

    monkeypatch.setattr(retrieval_subject, "query_registered_project", fake_query)

    result = CliRunner().invoke(cli.app, ["ask", question, "--home", str(home), "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["route"] == "natural-request-retrieval"
    retrieval = document["retrieval"]
    assert retrieval["status"] == "clarification-required"
    assert retrieval["reason_codes"] == ["comparison-intent-required"]
    assert retrieval["results"] == []
    assert retrieval["query_count"] == 0
    assert retrieval["index_triggered"] is False
    assert query_calls == []


def test_ask_single_project_keeps_legacy_ask_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tek proje sorusu (``--project`` ya da acik) mevcut
    ``zekam-ask-result/v1`` + dogrudan query semantigini korur;
    retrieval govdesiyle karistirilmaz."""

    home = _comparison_home(tmp_path)
    question = "sky-spring-ui servisi nasil calisiyor?"
    observed: dict[str, Any] = {}

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        observed.update(home=home_arg, project=slug, question=q, **kwargs)
        return {
            "schema": "zekam-embedded-rag-result/v1",
            "state": "answered",
            "project_id": "22222222-2222-4222-8222-222222222222",
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
            "--authorize-remote-query",
        ],
    )

    assert explicit.exit_code == 0, explicit.output
    document = json.loads(explicit.output)
    assert document["schema"] == "zekam-ask-result/v1"
    assert "route" not in document
    assert document["project_ref"] == "sky-spring-ui"
    assert document["retrieval"]["state"] == "answered"
    assert document["retrieval"]["answer_excerpt"] == "sky-spring-ui cevabi"
    assert observed["project"] == "sky-spring-ui"
    assert observed["question"] == question
    assert observed["opencode_config"] == cli._DEFAULT_OPENCODE_CONFIG_FILE
    assert observed["authorize_remote_query"] is True

    natural = CliRunner().invoke(
        cli.app,
        ["ask", question, "--home", str(home), "--json"],
    )

    assert natural.exit_code == 0, natural.output
    document = json.loads(natural.output)
    assert "route" not in document
    assert document["project_ref"] == "sky-spring-ui"
    assert document["retrieval"]["state"] == "answered"


def test_ask_unregistered_project_reference_still_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--project`` ile kayitli olmayan slug mevcut fail-fast hata davranisini
    korur (sessiz clarification'a donusturulmez)."""

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

    assert result.exit_code != 0
    assert query_calls == []


def test_ask_general_question_keeps_failure_without_silent_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proje sinyali tasiyacak genel soru mevcut fail-fast davranisini korur;
    sessiz proje fallback'i yok."""

    home = _comparison_home(tmp_path)
    query_calls: list[str] = []

    def fake_query(home_arg: Path, slug: str, q: str, **kwargs: Any) -> dict[str, Any]:
        query_calls.append(slug)
        return {}

    monkeypatch.setattr(cli, "query_registered_project", fake_query)

    result = CliRunner().invoke(
        cli.app,
        ["ask", "Dunya cevresi kac kilometredir?", "--home", str(home), "--json"],
    )

    assert result.exit_code != 0
    assert query_calls == []
