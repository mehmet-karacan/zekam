"""WP-02.01 - `zekam integration entry` CLI plan/apply yuzeyi (kok + alt dizin).

Yuzey, registry'de kayitli exact proje kokunu `_bound_project_root` ile cozup
`plan_project_local_entry` plani uretir; apply yalniz exact plan digest'i ve
`_run_claimed_effect` claim/receipt zinciriyle yapilir. Plan salt okunurdur;
apply operasyonel kaydi gunceller fakat global istemci ayarina yazmaz.
Conflict varsa hic proje dosyasi degisikligi olmadan red edilir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.project_rag_runtime import bind_project_source
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.e2e

ROOT_THIN_BODY = "# Zekam\n\n@AGENTS.md\n"
SUB_THIN_BODY = "# Zekam\n\n@../../AGENTS.md\n"


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")


def _make_zekam_root(path: Path) -> Path:
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
    _write(path / "PROJE_MANIFESTI.yaml", "schema: zekam-proje-manifesti/v1\n")
    _write(path / "AGENTS.md", "# Zekam\n\nOrtak yetki ve kanit kaydi.\n")
    return path


def _binded_project(tmp_path: Path) -> tuple[Path, Path]:
    """Bos ZEKAM_HOME, kayitli proje, exact kaynak baglantisi ve runtime acilisi."""

    home = tmp_path / "home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    source = _make_zekam_root(tmp_path / "zekam-proje")
    database = home / "state" / "operational.db"
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        uow.create_project(slug="zekam-proje", display_name="Zekam Proje")
        uow.commit()
    bind_project_source(home, "zekam-proje", source)
    # Local runtime 'admitted config' satirini kur (init bunu yapmaz).
    SQLiteLocalRuntimeStore(database, existing_only=False)
    return home, source


def _home_snapshot(home: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(home)): path.read_bytes()
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


#: Global client instruction yuzeyleri. Entry bu dizinlere/top-seviye dosyalara
#: hic yazmaz; yalniz kayitli proje koku altina yazar. Zekam'in kendi operational
#: ledger'i (`state/`, `runtime/`) bu alanin disindadir.
_CLIENT_AREA_PREFIXES = (".codex", ".claude", ".config", ".gemini")
_CLIENT_AREA_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")


def _client_area_snapshot(home: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(home.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(home)
        parts = relative.parts
        is_client_dir = len(parts) >= 2 and parts[0] in _CLIENT_AREA_PREFIXES
        is_top_file = len(parts) == 1 and parts[0] in _CLIENT_AREA_FILES
        if is_client_dir or is_top_file:
            result[str(relative)] = path.read_bytes()
    return result


def test_entry_plan_is_read_only_and_never_writes_home(tmp_path: Path) -> None:
    home, source = _binded_project(tmp_path)
    home_before = _home_snapshot(home)
    runner = CliRunner()

    planned = runner.invoke(
        app,
        ["integration", "entry", "--project-root", str(source), "--home", str(home)],
    )

    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["apply"] is False
    assert plan["inert"] is False
    assert plan["conflicts"] == []
    assert plan["plan_digest"].startswith("sha256:")
    assert plan["project_root"] == str(source.resolve())
    names = {item["path"]: item for item in plan["files"]}
    assert set(names) == {
        str(source / "CLAUDE.md"),
        str(source / "GEMINI.md"),
    }
    assert all(item["action"] == "create" for item in plan["files"])
    assert all(item["content"] == ROOT_THIN_BODY for item in plan["files"])
    # Salt okunur: proje kokunde ve HOME'da hic dosya degisikligi yok.
    assert not (source / "CLAUDE.md").exists()
    assert not (source / "GEMINI.md").exists()
    assert _home_snapshot(home) == home_before


def test_entry_apply_writes_thin_shims_under_registered_root_only(tmp_path: Path) -> None:
    home, source = _binded_project(tmp_path)
    client_before = _client_area_snapshot(home)
    agents_before = (source / "AGENTS.md").read_bytes()
    runner = CliRunner()

    planned = runner.invoke(
        app,
        ["integration", "entry", "--project-root", str(source), "--home", str(home)],
    )
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)

    applied = runner.invoke(
        app,
        [
            "integration",
            "entry",
            "--project-root",
            str(source),
            "--plan-digest",
            plan["plan_digest"],
            "--uygula",
            "--home",
            str(home),
        ],
    )

    assert applied.exit_code == 0, applied.output
    receipt = json.loads(applied.output)
    assert receipt["apply"] is True
    assert receipt["plan_digest"] == plan["plan_digest"]
    assert receipt["provider_calls"] == 0
    assert receipt["network_calls"] == 0
    # Claim/receipt zinciri terminal kanit tasiyor.
    assert receipt["operational_terminal_evidence_digest"].startswith("sha256:")
    assert receipt["receipt_digest"].startswith("sha256:")
    # Yalniz kayitli proje kokune ince shim yazildi; HOME/genel ve kanonik kaynak dokunmadi.
    assert (source / "CLAUDE.md").read_text(encoding="utf-8") == ROOT_THIN_BODY
    assert (source / "GEMINI.md").read_text(encoding="utf-8") == ROOT_THIN_BODY
    assert (source / "AGENTS.md").read_bytes() == agents_before
    # Global client yuzeylerine hic yazim yok.
    assert _client_area_snapshot(home) == client_before


def test_entry_subdirectory_plan_and_apply_write_thin_relative_entry(tmp_path: Path) -> None:
    home, source = _binded_project(tmp_path)
    sub = source / "src" / "zekam"
    sub.mkdir(parents=True)
    client_before = _client_area_snapshot(home)
    runner = CliRunner()

    planned = runner.invoke(
        app,
        [
            "integration",
            "entry",
            "--project-root",
            str(source),
            "--subdirectory",
            str(sub),
            "--home",
            str(home),
        ],
    )
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["inert"] is False
    assert plan["plan_digest"].startswith("sha256:")
    assert {item["path"] for item in plan["files"]} == {
        str(sub / "CLAUDE.md"),
        str(sub / "GEMINI.md"),
    }
    assert all(item["content"] == SUB_THIN_BODY for item in plan["files"])

    applied = runner.invoke(
        app,
        [
            "integration",
            "entry",
            "--project-root",
            str(source),
            "--subdirectory",
            str(sub),
            "--plan-digest",
            plan["plan_digest"],
            "--uygula",
            "--home",
            str(home),
        ],
    )

    assert applied.exit_code == 0, applied.output
    receipt = json.loads(applied.output)
    assert receipt["apply"] is True
    assert receipt["plan_digest"] == plan["plan_digest"]
    # Alt dizin shim'i kokteki TEK kanonik kaynaga ince giris yapar (kopya yok).
    assert (sub / "CLAUDE.md").read_text(encoding="utf-8") == SUB_THIN_BODY
    assert (sub / "GEMINI.md").read_text(encoding="utf-8") == SUB_THIN_BODY
    assert list(source.rglob("AGENTS.md")) == [source / "AGENTS.md"]
    # Kokte hic dosya hedeflenmedi.
    assert not (source / "CLAUDE.md").exists()
    assert not (source / "GEMINI.md").exists()
    # Global client yuzeylerine hic yazim yok.
    assert _client_area_snapshot(home) == client_before


def test_entry_plan_is_inert_outside_zekam_workspace(tmp_path: Path) -> None:
    home, _source = _binded_project(tmp_path)
    other = tmp_path / "baska-proje"
    other.mkdir()
    (other / "AGENTS.md").write_text("# Baska proje\n", encoding="utf-8")
    (other / "CLAUDE.md").write_text("# Baska proje\n", encoding="utf-8")
    claude_before = (other / "CLAUDE.md").read_bytes()
    # `baska-proje` adindan kayitli/bagli ikinci bir kaynak: registry baglanti
    # diger koku isaretlesede plan inert kalmali.
    store = SQLiteOperationalStore(home / "state" / "operational.db")
    with store.unit_of_work() as uow:
        uow.create_project(slug="baska-proje", display_name="Baska Proje")
        uow.commit()
    bind_project_source(home, "baska-proje", other)
    home_before = _home_snapshot(home)
    client_before = _client_area_snapshot(home)
    runner = CliRunner()

    planned = runner.invoke(
        app,
        ["integration", "entry", "--project-root", str(other), "--home", str(home)],
    )

    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["inert"] is True
    assert plan["files"] == []
    assert plan["conflicts"] == []
    assert plan["changes_required"] is False
    assert plan["plan_digest"].startswith("sha256:")
    # Inert planda hic hedef yok, hic yazim yok.
    assert (other / "CLAUDE.md").read_bytes() == claude_before
    assert _home_snapshot(home) == home_before

    applied = runner.invoke(
        app,
        [
            "integration",
            "entry",
            "--project-root",
            str(other),
            "--plan-digest",
            plan["plan_digest"],
            "--uygula",
            "--home",
            str(home),
        ],
    )

    assert applied.exit_code == 0, applied.output
    receipt = json.loads(applied.output)
    assert receipt["apply"] is True
    assert receipt["inert"] is True
    assert receipt["state"] == "inert-noop"
    assert (other / "CLAUDE.md").read_bytes() == claude_before
    # Global client yuzeylerine hic yazim yok.
    assert _client_area_snapshot(home) == client_before


def test_entry_apply_without_exact_plan_digest_is_rejected(tmp_path: Path) -> None:
    home, source = _binded_project(tmp_path)
    runner = CliRunner()

    missing_digest = runner.invoke(
        app,
        ["integration", "entry", "--project-root", str(source), "--uygula", "--home", str(home)],
    )
    assert missing_digest.exit_code != 0, missing_digest.output
    assert not (source / "CLAUDE.md").exists()
    assert not (source / "GEMINI.md").exists()

    planned = runner.invoke(
        app,
        ["integration", "entry", "--project-root", str(source), "--home", str(home)],
    )
    assert planned.exit_code == 0, planned.output

    mismatched = runner.invoke(
        app,
        [
            "integration",
            "entry",
            "--project-root",
            str(source),
            "--plan-digest",
            "sha256:" + "0" * 64,
            "--uygula",
            "--home",
            str(home),
        ],
    )

    assert mismatched.exit_code != 0, mismatched.output
    assert "Hata" in mismatched.output
    # Yanlis digest hic dosya yazdirmaz.
    assert not (source / "CLAUDE.md").exists()
    assert not (source / "GEMINI.md").exists()


def test_entry_apply_rejects_user_file_conflict_before_any_file_change(tmp_path: Path) -> None:
    home, source = _binded_project(tmp_path)
    # Plan oncesi kullanici icerigi: plan conflict tasiyacak.
    (source / "CLAUDE.md").write_text("kullanici icerigi\n", encoding="utf-8")
    before = (source / "CLAUDE.md").read_bytes()
    client_before = _client_area_snapshot(home)
    runner = CliRunner()

    planned = runner.invoke(
        app,
        ["integration", "entry", "--project-root", str(source), "--home", str(home)],
    )

    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["inert"] is False
    assert plan["conflicts"]
    assert any(
        item["reason"] == "entry-content-not-canonical-thin-entry" for item in plan["conflicts"]
    )

    applied = runner.invoke(
        app,
        [
            "integration",
            "entry",
            "--project-root",
            str(source),
            "--plan-digest",
            plan["plan_digest"],
            "--uygula",
            "--home",
            str(home),
        ],
    )

    assert applied.exit_code != 0, applied.output
    # Conflict tamamiyle red edildi: hic dosya degisikligi yok.
    assert (source / "CLAUDE.md").read_bytes() == before
    assert not (source / "GEMINI.md").exists()
    leftovers = [
        entry.name
        for entry in source.iterdir()
        if entry.name.startswith(".CLAUDE.md.") or entry.name.startswith(".GEMINI.md.")
    ]
    assert leftovers == []
    # Global client yuzeylerine hic yazim yok.
    assert _client_area_snapshot(home) == client_before


def test_entry_apply_rejects_later_file_drift_without_partial_write(tmp_path: Path) -> None:
    home, source = _binded_project(tmp_path)
    runner = CliRunner()

    planned = runner.invoke(
        app,
        ["integration", "entry", "--project-root", str(source), "--home", str(home)],
    )
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)

    # Plan sonrasi, apply oncesi: kullanici GEMINI.md'ye kendi icerigini yazdi.
    # Preflight tum hedefleri once taradigi icin ilkinin (CLAUDE.md) yazimindan
    # once red etmek zorunda; partial state uretilmez.
    (source / "GEMINI.md").write_text("kullanici icerigi\n", encoding="utf-8")
    drift = (source / "GEMINI.md").read_bytes()
    client_before = _client_area_snapshot(home)

    applied = runner.invoke(
        app,
        [
            "integration",
            "entry",
            "--project-root",
            str(source),
            "--plan-digest",
            plan["plan_digest"],
            "--uygula",
            "--home",
            str(home),
        ],
    )

    assert applied.exit_code != 0, applied.output
    # No-partial-write: drift'li dosya korunur, drift'siz hedef de yazilmaz.
    assert (source / "GEMINI.md").read_bytes() == drift
    assert not (source / "CLAUDE.md").exists()
    leftovers = [
        entry.name
        for entry in source.iterdir()
        if entry.name.startswith(".CLAUDE.md.") or entry.name.startswith(".GEMINI.md.")
    ]
    assert leftovers == []
    # Global client yuzeylerine hic yazim yok.
    assert _client_area_snapshot(home) == client_before


def test_entry_unregistered_project_root_is_rejected(tmp_path: Path) -> None:
    home, _source = _binded_project(tmp_path)
    unregistered = _make_zekam_root(tmp_path / "kayitsiz-proje")
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["integration", "entry", "--project-root", str(unregistered), "--home", str(home)],
    )

    assert result.exit_code != 0, result.output
    assert not (unregistered / "CLAUDE.md").exists()
    assert not (unregistered / "GEMINI.md").exists()


def test_entry_apply_is_replayed_from_terminal_receipt(tmp_path: Path) -> None:
    home, source = _binded_project(tmp_path)
    runner = CliRunner()

    planned = runner.invoke(
        app,
        ["integration", "entry", "--project-root", str(source), "--home", str(home)],
    )
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    arguments = [
        "integration",
        "entry",
        "--project-root",
        str(source),
        "--plan-digest",
        plan["plan_digest"],
        "--uygula",
        "--home",
        str(home),
    ]
    applied = runner.invoke(app, arguments)
    assert applied.exit_code == 0, applied.output
    first = json.loads(applied.output)

    replay = runner.invoke(app, arguments)
    assert replay.exit_code == 0, replay.output
    second = json.loads(replay.output)
    assert second["plan_digest"] == first["plan_digest"]
    assert second["receipt_digest"] == first["receipt_digest"]
    assert (source / "CLAUDE.md").read_text(encoding="utf-8") == ROOT_THIN_BODY


def test_entry_plan_does_not_execute_client_executables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as subprocess_module

    home, source = _binded_project(tmp_path)

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("entry plan launched an external process")

    # Plan yolu hic external process baslatmaz (host surum probe'i yok).
    monkeypatch.setattr(subprocess_module, "run", forbidden_run)
    monkeypatch.setattr(subprocess_module, "Popen", forbidden_run)
    runner = CliRunner()

    planned = runner.invoke(
        app,
        ["integration", "entry", "--project-root", str(source), "--home", str(home)],
    )

    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.output)
    assert plan["inert"] is False
    assert plan["provider_calls"] == 0
    assert plan["network_calls"] == 0
