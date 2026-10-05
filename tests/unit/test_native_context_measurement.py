"""Native giris: dort istemci icin etkin yuk kaynaklari, tekrar yuk ve skill kesif tanisi."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application.context_instruction_measurement import (
    duplicate_skill_discovery,
    measure_instruction_load,
)
from zekam.domain.client_integration import ClientIntegrationId

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SKILL = (
    "---\nname: ornek\ndescription: Yalniz ilgili istekte kullan.\n---\n"
    "Govde asla on yuke girmez.\n"
)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("# Giris\n", encoding="utf-8")
    (project / "CLAUDE.md").write_text("# Z\n\n@AGENTS.md\n", encoding="utf-8")
    (project / "GEMINI.md").write_text("# Z\n\n@AGENTS.md\n", encoding="utf-8")
    for relative in (".agents", ".claude"):
        skill = project / relative / "skills" / "ornek"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    return project


def _refs(report: object, kind: str) -> list[str]:
    return [item.logical_ref for item in report.entries if item.source_kind == kind]  # type: ignore[attr-defined]


def test_each_client_loads_agents_once_through_its_own_entry(tmp_path: Path) -> None:
    project = _project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    claude = measure_instruction_load(
        user_home=home, client=ClientIntegrationId.CLAUDE_CODE, project_root=project
    )
    gemini = measure_instruction_load(
        user_home=home, client=ClientIntegrationId.GEMINI, project_root=project
    )
    codex = measure_instruction_load(
        user_home=home, client=ClientIntegrationId.CODEX, project_root=project
    )

    assert _refs(claude, "instruction-project") == ["client/claude-code/project-instructions"]
    assert _refs(claude, "instruction-import") == ["project/AGENTS.md"]
    assert _refs(gemini, "instruction-project") == ["client/gemini/project-instructions"]
    assert _refs(gemini, "instruction-import") == ["project/AGENTS.md"]
    assert _refs(codex, "instruction-project") == ["client/codex/project-instructions"]
    assert _refs(codex, "instruction-import") == []
    # Gizli system prompt ve tool serialization olculemez; tam token iddiasi yoktur.
    assert any(item.quality == "unobservable" for item in codex.entries)


def test_config_instructions_duplicate_agents_only_when_configured(tmp_path: Path) -> None:
    project = _project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (project / "opencode.json").write_text(
        json.dumps({"$schema": "https://opencode.ai/config.json"}), encoding="utf-8"
    )
    clean = measure_instruction_load(
        user_home=home, client=ClientIntegrationId.OPENCODE, project_root=project
    )
    assert _refs(clean, "instruction-import") == []

    (project / "opencode.json").write_text(
        json.dumps({"instructions": ["AGENTS.md"]}), encoding="utf-8"
    )
    duplicated = measure_instruction_load(
        user_home=home, client=ClientIntegrationId.OPENCODE, project_root=project
    )
    assert _refs(duplicated, "instruction-import") == ["project/AGENTS.md"]


def test_skill_metadata_is_measured_without_body_and_duplicates_are_reported(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    opencode = measure_instruction_load(
        user_home=home, client=ClientIntegrationId.OPENCODE, project_root=project
    )
    codex = measure_instruction_load(
        user_home=home, client=ClientIntegrationId.CODEX, project_root=project
    )

    assert sorted(_refs(opencode, "skill")) == [
        "project/.agents/skills/ornek",
        "project/.claude/skills/ornek",
    ]
    assert duplicate_skill_discovery(opencode) == {
        "ornek": ("project/.agents/skills/ornek", "project/.claude/skills/ornek")
    }
    assert duplicate_skill_discovery(codex) == {}
    sizes = {item.size.bytes_count for item in opencode.entries if item.source_kind == "skill"}
    assert sizes and all(size < len(_SKILL.encode()) for size in sizes if size is not None)


def test_repository_entry_is_thin_for_every_client() -> None:
    home = _ROOT / "nonexistent-home"
    for client in ClientIntegrationId:
        report = measure_instruction_load(user_home=home, client=client, project_root=_ROOT)
        loaded = [
            item
            for item in report.entries
            if item.source_kind in {"instruction-project", "instruction-import"}
        ]
        total = sum(item.size.bytes_count or 0 for item in loaded)
        assert 0 < total < 6000, (client.value, total)
        assert len({item.content_digest for item in loaded}) == len(loaded)
        assert not any("00_BASLA" in item.logical_ref for item in loaded)
