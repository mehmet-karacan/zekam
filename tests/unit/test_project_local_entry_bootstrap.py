"""WP-02.01 - Proje-yerel ince giris (kok + alt dizin), tekillestirme, inert davranis.

Bu testler `client_instruction_bootstrap` icindeki kanonik generator'in proje-yerel
ince giris davranisini sabitler: Zekam workspace kokunde/alt dizinlerinde kalin
kopya degil, tek `AGENTS.md` kaynagina ince `@` girisi; Zekam disi oturumda inert
(hic hedef, hic yazim). Gerçek hostta yeni istemci oturumu acmayi gerektiren canlı
kabul bu unit testlerin disiadir (GATE-U); burada determinist inert siniri kanitlanir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application.client_instruction_bootstrap import (
    PROJECT_ENTRY_CANONICAL_SOURCE,
    apply_project_local_entry,
    plan_project_local_entry,
    thin_entry_body,
)
from zekam.domain.client_integration import ClientIntegrationId
from zekam.domain.errors import ConfigurationError

pytestmark = pytest.mark.unit

# Mevcut kok shim'leriyle birebir eslesmeli ince govde (kullanici degisikligi yok).
ROOT_THIN_BODY = "# Zekam\n\n@AGENTS.md\n"


def _w(path: Path, body: str) -> None:
    # Repo tracked shim'leri LF; CRLF'e cevirmeden dogruLF ile yaz.
    path.write_text(body, encoding="utf-8", newline="\n")


def _make_zekam_root(tmp_path: Path) -> Path:
    root = tmp_path / "zekam-proje"
    root.mkdir()
    ai = root / ".ai"
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
    _w(root / "PROJE_MANIFESTI.yaml", "schema: zekam-proje-manifesti/v1\n")
    _w(root / "AGENTS.md", "# Zekam\n\nOrtak yetki ve kanit kaydi.\n")
    return root


def test_non_zekam_root_is_inert_and_writes_nothing(tmp_path: Path) -> None:
    other = tmp_path / "baska-proje"
    other.mkdir()
    (other / "AGENTS.md").write_text("# Baska proje\n", encoding="utf-8")
    (other / "CLAUDE.md").write_text("# Baska proje\n", encoding="utf-8")

    plan = plan_project_local_entry(project_root=other)

    assert plan.inert is True
    assert plan.files == ()
    assert plan.conflicts == ()
    assert plan.changes_required is False
    # Inert planda dosya icerigi dokunulmuyor.
    assert (other / "CLAUDE.md").read_text(encoding="utf-8") == "# Baska proje\n"


def test_missing_marker_is_inert_even_with_manifest(tmp_path: Path) -> None:
    root = tmp_path / "yarim-proje"
    root.mkdir()
    (root / "PROJE_MANIFESTI.yaml").write_text("x\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# yok\n", encoding="utf-8")

    assert plan_project_local_entry(project_root=root).inert is True


def test_root_thin_entry_creates_missing_shims_and_keeps_canonical_source(tmp_path: Path) -> None:
    root = _make_zekam_root(tmp_path)
    agents_before = (root / "AGENTS.md").read_bytes()

    plan = plan_project_local_entry(project_root=root)

    assert plan.inert is False
    assert plan.conflicts == ()
    # Yalniz shim dosyalari hedeflenir; kanonik kaynak AGENTS.md yeniden yazilmiyor.
    managed_paths = {item.path.name for item in plan.files}
    assert managed_paths == {"CLAUDE.md", "GEMINI.md"}
    assert PROJECT_ENTRY_CANONICAL_SOURCE not in {item.path.name for item in plan.files}
    for item in plan.files:
        assert item.action == "create"
        assert item.content == ROOT_THIN_BODY
    # Kanonik kaynak planda hedef degil.
    assert (root / "AGENTS.md") not in {item.path for item in plan.files}

    apply_project_local_entry(plan)
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == ROOT_THIN_BODY
    assert (root / "GEMINI.md").read_text(encoding="utf-8") == ROOT_THIN_BODY
    assert (root / "AGENTS.md").read_bytes() == agents_before  # kayit degismedi


def test_existing_root_shims_are_unchanged_and_thin(tmp_path: Path) -> None:
    root = _make_zekam_root(tmp_path)
    _w(root / "CLAUDE.md", ROOT_THIN_BODY)
    _w(root / "GEMINI.md", ROOT_THIN_BODY)

    plan = plan_project_local_entry(project_root=root)

    assert plan.inert is False
    assert plan.changes_required is False
    assert all(item.action == "unchanged" for item in plan.files)
    assert {item.path.name for item in plan.files} == {"CLAUDE.md", "GEMINI.md"}


def test_fat_duplicate_shim_is_conflict_and_preserved(tmp_path: Path) -> None:
    root = _make_zekam_root(tmp_path)
    # Kalin kopya: AGENTS.md icerigi CLAUDE.md icine yapistirilmis (tekillestirilecek durum).
    fat = "# Zekam\n\nOrtak yetki ve kanit kaydi.\n"
    (root / "CLAUDE.md").write_text(fat, encoding="utf-8")
    before = (root / "CLAUDE.md").read_bytes()

    plan = plan_project_local_entry(project_root=root, collect_conflicts=True)

    conflict_paths = {item.path.name for item in plan.conflicts}
    assert "CLAUDE.md" in conflict_paths
    # Giris hedef planlanmiyor (user-owned drift dokunulmuyor).
    assert (root / "CLAUDE.md") not in {item.path for item in plan.files}
    assert (root / "CLAUDE.md").read_bytes() == before


def test_subdirectory_thin_entry_points_to_root_source_without_copy(tmp_path: Path) -> None:
    root = _make_zekam_root(tmp_path)
    sub = root / "src" / "zekam"
    sub.mkdir(parents=True)

    plan = plan_project_local_entry(project_root=root, subdirectory=sub)

    assert plan.inert is False
    assert plan.conflicts == ()
    by_name = {item.path.name: item for item in plan.files}
    assert by_name["CLAUDE.md"].path == sub / "CLAUDE.md"
    assert by_name["GEMINI.md"].path == sub / "GEMINI.md"
    # Alt dizin shim'i kokteki TEK kanonik kaynaga ince giris yapar (kalin kopya yok).
    assert by_name["CLAUDE.md"].content == "# Zekam\n\n@../../AGENTS.md\n"
    assert by_name["GEMINI.md"].content == "# Zekam\n\n@../../AGENTS.md\n"
    # Kok AGENTS.md'ye gore dogru gonderel yol (iki ust dizin).
    assert thin_entry_body(sub, root) == "# Zekam\n\n@../../AGENTS.md\n"
    # Kokte ince govde.
    assert thin_entry_body(root, root) == ROOT_THIN_BODY

    apply_project_local_entry(plan)
    assert (sub / "CLAUDE.md").read_text(encoding="utf-8") == "# Zekam\n\n@../../AGENTS.md\n"
    # Kanonik kaynak tek ve alt dizine kopyalanmamis.
    assert list(root.rglob("AGENTS.md")) == [root / "AGENTS.md"]


def test_subdirectory_outside_root_is_rejected(tmp_path: Path) -> None:
    root = _make_zekam_root(tmp_path)
    outside = tmp_path / "dis"
    outside.mkdir()

    with pytest.raises(ConfigurationError):
        plan_project_local_entry(project_root=root, subdirectory=outside)


def test_plan_is_read_only_until_applied(tmp_path: Path) -> None:
    root = _make_zekam_root(tmp_path)

    plan_project_local_entry(project_root=root)

    assert not (root / "CLAUDE.md").exists()
    assert not (root / "GEMINI.md").exists()


def test_apply_rejects_stale_plan_when_user_edits_entry(tmp_path: Path) -> None:
    root = _make_zekam_root(tmp_path)
    plan = plan_project_local_entry(project_root=root)
    # Plan sonrasi kullanici kendi icerigini yazdi.
    (root / "CLAUDE.md").write_text("kullanici icerigi\n", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        apply_project_local_entry(plan)


def test_apply_writes_only_under_project_root(tmp_path: Path) -> None:
    root = _make_zekam_root(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    plan = plan_project_local_entry(project_root=root)
    before_home = {p.name for p in home.rglob("*")}

    apply_project_local_entry(plan)

    after_home = {p.name for p in home.rglob("*")}
    assert before_home == after_home  # HOME/genel yazi yok
    # Tumu proje koku altina yazildi.
    for item in plan.files:
        assert item.path.is_relative_to(root)


def test_missing_canonical_source_is_conflict_not_create(tmp_path: Path) -> None:
    root = _make_zekam_root(tmp_path)
    (root / "AGENTS.md").unlink()

    plan = plan_project_local_entry(project_root=root, collect_conflicts=True)

    assert plan.inert is False
    assert plan.files == ()
    reasons = {item.reason for item in plan.conflicts}
    assert "canonical-source-missing" in reasons


def test_known_client_ids_are_the_only_entry_targets(tmp_path: Path) -> None:
    root = _make_zekam_root(tmp_path)
    plan = plan_project_local_entry(project_root=root)
    client_ids = {item.client_id for item in plan.files}
    assert client_ids <= {
        ClientIntegrationId.CLAUDE_CODE.value,
        ClientIntegrationId.GEMINI.value,
    }


def test_crlf_existing_shim_is_planned_unchanged_and_apply_keeps_bytes(tmp_path: Path) -> None:
    # EOL farki (CRLF) plan'da "unchanged" kabul edilir; apply de ayni eslesmeyi
    # yapmali: CRLF baytlari byte-exact kanonik LF govdeyle karsilastirilip
    # "stale" sayilamaz ve dosya dokunulmaz kalir.
    root = _make_zekam_root(tmp_path)
    crlf_body = ROOT_THIN_BODY.replace("\n", "\r\n")
    (root / "CLAUDE.md").write_bytes(crlf_body.encode("utf-8"))
    (root / "GEMINI.md").write_bytes(crlf_body.encode("utf-8"))

    plan = plan_project_local_entry(project_root=root)

    assert plan.conflicts == ()
    assert all(item.action == "unchanged" for item in plan.files)
    assert plan.changes_required is False

    apply_project_local_entry(plan)

    # Apply CRLF baytlarini LF'e cevirmeden korur.
    assert (root / "CLAUDE.md").read_bytes() == crlf_body.encode("utf-8")
    assert (root / "GEMINI.md").read_bytes() == crlf_body.encode("utf-8")


def test_apply_rejects_whole_plan_when_any_file_has_conflict(tmp_path: Path) -> None:
    # Plan conflict tasimasi, diger hedeflerin yarisinda partial state uretmesine
    # izin vermez: apply baslangicta red eder ve hic dosyaya yazmaz.
    root = _make_zekam_root(tmp_path)
    (root / "CLAUDE.md").write_text("kullanici icerigi\n", encoding="utf-8")
    before = (root / "CLAUDE.md").read_bytes()

    plan = plan_project_local_entry(project_root=root, collect_conflicts=True)

    assert plan.conflicts, "premise: CLAUDE.md drift conflict olusturdu"
    # Conflict'ten bagimsiz diger hedef henuz planlanir (GEMINI.md create).
    assert any(item.action == "create" for item in plan.files)

    with pytest.raises(ConfigurationError, match="conflict"):
        apply_project_local_entry(plan)

    # Partial state yok: conflict'li dosya korunur, conflict disindaki create
    # hedefi de yazilmaz; gecici dosya kalmaz.
    assert (root / "CLAUDE.md").read_bytes() == before
    assert not (root / "GEMINI.md").exists()
    leftovers = [
        entry.name
        for entry in root.iterdir()
        if entry.name.startswith(".CLAUDE.md.") or entry.name.startswith(".GEMINI.md.")
    ]
    assert leftovers == []


def test_broken_symlink_entry_is_preserved(tmp_path: Path) -> None:
    root = _make_zekam_root(tmp_path)
    target = root / "CLAUDE.md"
    try:
        target.symlink_to(root / "missing-instruction.md")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink olusturulamadi: {exc}")

    with pytest.raises(ConfigurationError, match="regular file"):
        plan_project_local_entry(project_root=root)
    assert target.is_symlink()
    assert not (root / "GEMINI.md").exists()
