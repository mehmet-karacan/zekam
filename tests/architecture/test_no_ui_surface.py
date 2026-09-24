"""AC-15 no-ui surface — mimari testi.

Zekam CLI/JSON/machine-readable bir yuzeydir; UI/dashboard/browser/TUI/HTML/
CSS/JS product surface EKLEMEZ. Bu test su gercekleri dogrular:

1. Import-graph/CLI registration uzerinden kayitli hicbir komut visual
   surface (ui, dashboard, browser, tui) uretmez.
2. Paket source tree'sinde visual product yok; yalniz CLI/machine-readable
   yuzeyler vardir.
3. "ziyaret/guvenli/read-only" degil; read-only CLI ses duzeyi (console.print)
   kabul edilir, girilen browser/UI/dashboard yok.
"""

from __future__ import annotations

import re
from pathlib import Path

import typer

from zekam.domain.observability import CANONICAL_COMMANDS

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "zekam"

#: Kayitli komut isimlerinde visual/UI ifadesi ariyoruz.
_UI_SURFACE_RE = re.compile(
    r"^(?:ui|dashboard|browser|tui|web|frontend|server-html|visual|graph-ui)$",
    re.IGNORECASE,
)
_UI_TERM_RE = re.compile(r"\b(?:dashboard|browser|frontend|webapp|tui|graphical-ui)\b", re.I)

#: Product dosya uzantilari; CSS/HTML/TSX/JSX ciktilari UI'dir.
_UI_EXTENSIONS = (".html", ".css", ".tsx", ".jsx", ".htm")
#: Dizin adlarinda visual surface isaretcileri.
_UI_DIR_RE = re.compile(
    r"(?:^|[/\\])(?:ui|dashboard|frontend|webapp|web|browser)(?:[/\\]|$)", re.I
)


def _iter_cli_names(application: typer.Typer, prefix: str = "") -> list[str]:
    names: list[str] = []
    for command in application.registered_commands:
        name = command.name or (command.callback.__name__ if command.callback else None)
        if name:
            names.append(f"{prefix}{name}".strip())
    for group in application.registered_groups:
        instance = group.typer_instance
        group_name = group.name or (instance.info.name if instance else None)
        if group_name is None or instance is None:
            continue
        subprefix = f"{prefix}{group_name} " if prefix or group_name else ""
        names.extend(_iter_cli_names(instance, subprefix))
    return names


def test_kayitli_cli_komutlari_visual_surface_icermez() -> None:
    from zekam.interfaces.cli.main import app

    names = _iter_cli_names(app)
    assert names, "CLI komut kaydi bos olmamali"
    # Komut adlarinda features: "surface", "capabilities" gibi komut olabilir ama
    # ad UI/dashboard/browser degil; bunlari disari birakiyoruz.
    for name in names:
        base = name.split()[-1]
        assert not _UI_SURFACE_RE.match(base), f"Visual surface komutu kayitli: {name}"
        assert not _UI_TERM_RE.search(name), f"Visual surface terimi tasiyan komut: {name}"


def test_kanonik_komut_sozlesmesinde_ui_yok() -> None:
    names = {item.name for item in CANONICAL_COMMANDS}
    assert names, "Kanonik komut sozlesmesi bos"
    for name in names:
        base = name.split()[-1]
        assert not _UI_SURFACE_RE.match(base), f"Sozlesmede visual surface var: {name}"


def test_paket_kokunde_ui_product_yuzeyi_yok() -> None:
    """Source tree'de CSS/HTML/TSX/JSX product dosyasi yok."""
    ui_files: list[str] = []
    for path in PACKAGE_ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in _UI_EXTENSIONS:
            ui_files.append(str(path.relative_to(PACKAGE_ROOT)))
    assert ui_files == [], f"UI product dosyasi bulundu: {ui_files}"


def test_paket_dizinlerinde_visual_surface_yok() -> None:
    """ui/dashboard/frontend/webapp/web/browser dizin adi yok."""
    hits: list[str] = []
    for path in PACKAGE_ROOT.rglob("*"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if _UI_DIR_RE.search(relative):
            hits.append(relative)
    assert hits == [], f"Visual surface dizini bulundu: {hits}"


def test_surf_comamnd_kayitli_yuzeyi_salt_okunur_sozlesme_olarak_korur() -> None:
    """`surface` komutu UI uretmez; CLI komut yuzeyi sozlesmesidir."""
    from zekam.interfaces.cli.main import app

    names = _iter_cli_names(app)
    assert "surface contract" in names or "surface check" in names
    assert "ui" not in names
    assert "dashboard" not in names
