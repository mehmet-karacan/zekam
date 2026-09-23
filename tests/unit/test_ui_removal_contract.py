"""ZEKAM-UI-REMOVAL-001 negatif kabul testleri.

UI/dashboard yuzeyinin urunden tamamen kaldirildigini dogrular. Bu test yalniz
salt-okunur dosya varligi ve CLI yuzeyi kontrol eder; mutation yapmaz.
"""

from __future__ import annotations

import re
from pathlib import Path

from zekam.interfaces.cli.main import app as zekam_app

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cli_help_ui_komutu_yok() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(zekam_app, ["--help"])
    assert result.exit_code == 0
    lines = [line.strip() for line in result.output.splitlines()]
    standalone_ui = [line for line in lines if line == "ui"]
    assert not standalone_ui, "CLI help 'ui' komutu gostermemeli"
    assert not any("Gozleme Merkezi" in line or "Neuro Observatory" in line for line in lines)


def test_ui_kaynak_dosyalari_yok() -> None:
    assert not (REPO_ROOT / "src/zekam/interfaces/cli/ui.py").exists()
    assert not (REPO_ROOT / "src/zekam/interfaces/api/observatory.py").exists()
    assert not (REPO_ROOT / "src/zekam/interfaces/api/static").exists()
    assert not (REPO_ROOT / "src/zekam/application/observatory.py").exists()
    assert not (REPO_ROOT / "schemas/observatory_snapshot.schema.json").exists()


def test_ui_route_iz_kalmadi() -> None:
    forbidden = re.compile(r"/api/observatory|observatory-assets|ui serve")
    hits: list[str] = []
    for path in (REPO_ROOT / "src/zekam").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if forbidden.search(line):
                hits.append(f"{path}: {line.strip()}")
    assert not hits, f"Kaynak agacinda UI izi kaldi: {hits}"


def test_readme_ui_talimati_icermez() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "zekam ui" not in readme
    assert "ui serve" not in readme
    assert "Gozleme Merkezi" not in readme
