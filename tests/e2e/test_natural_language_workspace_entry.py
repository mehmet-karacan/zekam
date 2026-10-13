"""WP-02.01 — Dogal dil calisma alani girisi (e2e; gercek kok + taze oturum).

Coding CLI oturumu calisma alanina dogal dil ile girer: kanonik `AGENTS.md` tek
kaynaktir; OpenCode/Codex onu dogrudan kesfeder, Claude Code ve Gemini kokte ve
alt dizinlerdeki ince `@` redirector'leriyle baglanir. Bu dosya giris yuzeyinin
uctan uca sozlesmesini sabitler:

* Gercek Zekam kokunde tek kanonik `AGENTS.md` vardir; `CLAUDE.md`/`GEMINI.md`
  ince redirector'dir, kalin kopya degildir.
* Gercek kokun giris plani kanoniktir (inert degil), conflict tasimaz ve salt
  okunurdur: plan hicbir giris dosyasini degistirmez.
* Taze oturum alt dizinden giriste kanonik kaynaga goreli ince giris uretilir;
  kanonik kaynagin kopyasi olusmaz.
* Zekam disi dizinde taze oturum inerttir: hic hedef yok, hic yazim yok,
  kullanici dosyalari bayti baytina korunur.
* Giris yuzeyi (`opencode.json`) agent zorlamaz ve eager instruction yuklemez;
  dogal dil ajansi korunur.

SINIR: Gercek host kabulunun (OpenCode/Claude/Gemini CLI oturumunda ince girisin
fiili kesfi) yeri ayridir; bu makinede o CLI'lar calistirilamadigi icin bu dosya
gercek host iddiasi tasimaz. Ayrintili v2 `opencode.json` sema/deny siniri
`tests/unit/test_opencode_native_policy.py`, plan/apply CLI akisi ve inert
receipt `tests/e2e/test_cli_integration_entry.py`, birim davranis
`tests/unit/test_project_local_entry_bootstrap.py` kapsamindadir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application.client_instruction_bootstrap import (
    apply_project_local_entry,
    plan_project_local_entry,
)

pytestmark = pytest.mark.e2e

ROOT_THIN_BODY = "# Zekam\n\n@AGENTS.md\n"
SUB_THIN_BODY = "# Zekam\n\n@../../AGENTS.md\n"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _make_zekam_workspace(path: Path) -> Path:
    """Minimum Zekam workspace marker'i: `.ai` baglami + manifest + kanonik kaynak."""

    path.mkdir(parents=True, exist_ok=True)
    ai = path / ".ai"
    ai.mkdir()
    (ai / "repository-context.json").write_text(
        json.dumps(
            {
                "schema": "zekam-repository-context/v2",
                "project": "zekam",
                "entry": "AGENTS.md",
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    (path / "PROJE_MANIFESTI.yaml").write_text(
        "schema: zekam-proje-manifesti/v1\n", encoding="utf-8", newline="\n"
    )
    (path / "AGENTS.md").write_text(
        "# Zekam\n\nOrtak yetki ve kanit kaydi.\n", encoding="utf-8", newline="\n"
    )
    return path


def test_real_workspace_entry_is_single_canonical_source_with_thin_redirectors() -> None:
    root = _repo_root()
    canonical = root / "AGENTS.md"
    assert canonical.is_file(), "gercek kokte kanonik AGENTS.md olmali"
    duplicates = [
        path for path in root.rglob("AGENTS.md") if ".git" not in path.parts and path != canonical
    ]
    assert duplicates == [], f"kalin AGENTS.md kopyasi: {duplicates}"
    for name in ("CLAUDE.md", "GEMINI.md"):
        body = (root / name).read_text(encoding="utf-8")
        assert body == ROOT_THIN_BODY, f"{name} ince redirector degil"


def test_real_workspace_entry_plan_is_canonical_and_read_only() -> None:
    root = _repo_root()
    names = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
    before = {name: (root / name).read_bytes() for name in names}

    plan = plan_project_local_entry(project_root=root)

    assert plan.inert is False
    assert plan.conflicts == ()
    assert plan.changes_required is False
    assert {item.path.name for item in plan.files} == {"CLAUDE.md", "GEMINI.md"}
    assert all(item.action == "unchanged" for item in plan.files)
    assert all(item.content == ROOT_THIN_BODY for item in plan.files)
    # Plan salt okunurdur: gercek kokte bayti baytina hic degisiklik yok.
    assert {name: (root / name).read_bytes() for name in names} == before


def test_fresh_session_subdirectory_entry_redirects_to_single_root_source(tmp_path: Path) -> None:
    workspace = _make_zekam_workspace(tmp_path / "zekam-proje")
    session_dir = workspace / "src" / "zekam"
    session_dir.mkdir(parents=True)

    plan = plan_project_local_entry(project_root=workspace, subdirectory=session_dir)

    assert plan.inert is False
    assert plan.conflicts == ()
    assert {item.path for item in plan.files} == {
        session_dir / "CLAUDE.md",
        session_dir / "GEMINI.md",
    }
    assert all(item.content == SUB_THIN_BODY for item in plan.files)

    apply_project_local_entry(plan)

    assert (session_dir / "CLAUDE.md").read_text(encoding="utf-8") == SUB_THIN_BODY
    assert (session_dir / "GEMINI.md").read_text(encoding="utf-8") == SUB_THIN_BODY
    # Alt dizin girisi kanonik kaynagi kopyalamaz: tek AGENTS.md kokte kalir.
    assert list(workspace.rglob("AGENTS.md")) == [workspace / "AGENTS.md"]
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == (
        "# Zekam\n\nOrtak yetki ve kanit kaydi.\n"
    )


def test_fresh_session_outside_zekam_workspace_is_inert(tmp_path: Path) -> None:
    other = tmp_path / "baska-proje"
    other.mkdir()
    user_bodies = {
        "AGENTS.md": "# Baska proje\n\nKullanici talimatlari.\n",
        "CLAUDE.md": "# Kullanici claude talimatlari\n",
        "GEMINI.md": "# Kullanici gemini talimatlari\n",
    }
    for name, body in user_bodies.items():
        (other / name).write_text(body, encoding="utf-8", newline="\n")

    plan = plan_project_local_entry(project_root=other)

    # Workspace marker'i yok: plan hic hedef tasimaz, hic conflict uretmez.
    assert plan.inert is True
    assert plan.files == ()
    assert plan.conflicts == ()
    assert plan.changes_required is False

    apply_project_local_entry(plan)

    # Inert plan noop'tur: kullanici dosyalari bayti baytina korunur.
    for name, body in user_bodies.items():
        assert (other / name).read_text(encoding="utf-8") == body


def test_workspace_entry_config_keeps_natural_language_agency() -> None:
    """Giris yuzeyi agent zorlamaz ve eager instruction yuklemez.

    Boylece taze oturum dogal dil ajansini korur; ayrintili v2 sema, siralama
    (last-match-wins) ve push deny siniri
    `tests/unit/test_opencode_native_policy.py` kapsamindadir.
    """
    config = json.loads((_repo_root() / "opencode.json").read_text(encoding="utf-8"))
    assert "default_agent" not in config
    assert "instructions" not in config
