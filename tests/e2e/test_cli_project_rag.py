"""Packaged project RAG and OpenCode-facing CLI contracts."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from zekam.interfaces.cli import main as cli


def test_ask_requires_explicit_remote_query_authorization() -> None:
    result = CliRunner().invoke(cli.app, ["ask", "gpu-fusion hangi servis?"])

    assert result.exit_code == 77
    assert "authorize-remote-query" in result.output


def test_ask_routes_exact_question_and_wraps_retrieval(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    observed: dict[str, object] = {}

    def fake_query(home, project, question, *, opencode_config):  # type: ignore[no-untyped-def]
        observed.update(
            home=home,
            project=project,
            question=question,
            opencode_config=opencode_config,
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


def test_project_status_and_source_root_are_exposed(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
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

    root_result = runner.invoke(cli.app, ["project", "source-root", "gpu-fusion", "--json"])
    status_result = runner.invoke(cli.app, ["project", "status", "gpu-fusion", "--json"])

    assert root_result.exit_code == 0, root_result.output
    assert json.loads(root_result.output)["source_root"] == str(source)
    assert status_result.exit_code == 0, status_result.output
    assert json.loads(status_result.output)["chunk_count"] == 8496


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

    resolve_result = runner.invoke(cli.app, ["project", "resolve", "gpu", "--json"])
    show_result = runner.invoke(cli.app, ["project", "show", "gpu", "--json"])

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


def test_project_citation_opens_pinned_verified_chunk(monkeypatch) -> None:  # type: ignore[no-untyped-def]
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
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    observed: dict[str, object] = {}

    def fake_index(  # type: ignore[no-untyped-def]
        home,
        project,
        *,
        oracle_config,
        opencode_config,
        batch_size,
        authorize_odi_metadata,
    ):
        observed.update(
            home=home,
            project=project,
            oracle_config=oracle_config,
            opencode_config=opencode_config,
            batch_size=batch_size,
            authorize_odi_metadata=authorize_odi_metadata,
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
    result = CliRunner().invoke(
        cli.app,
        [
            "project",
            "index",
            "sky-ui",
            "--authorize-remote-source",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["database_access"] == "disabled"
    assert observed["project"] == "sky-spring-ui"
    assert observed["oracle_config"] is None


def test_project_index_requires_database_authorization_only_when_configured() -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            "project",
            "index",
            "gpu-fusion",
            "--oracle-config",
            "config/application.yaml",
            "--authorize-remote-source",
        ],
    )

    assert result.exit_code == 77
    assert "Database metadata" in result.output
