"""`zekam backup` uctan uca akisi: manifest uret, dogrula, bozulmayi yakala."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.backup import BACKUP_MANIFEST_SCHEMA
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.e2e

runner = CliRunner()


def _prepare(home: Path) -> LocalContentAddressedStore:
    runner.invoke(app, ["init", "--home", str(home)])
    return LocalContentAddressedStore(home / "global" / "artifacts").ensure()


def test_manifest_is_created_and_verified(home_root: Path) -> None:
    store = _prepare(home_root)
    store.put(b"rapor icerigi", media_type="text/plain")
    manifest_path = home_root / "yedek.json"

    created = runner.invoke(
        app, ["backup", "create", "--home", str(home_root), "--cikti", str(manifest_path)]
    )
    assert created.exit_code == 0, created.stdout

    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert document["schema"] == BACKUP_MANIFEST_SCHEMA
    assert len(document["artifacts"]) == 1

    verified = runner.invoke(
        app, ["backup", "verify", str(manifest_path), "--home", str(home_root)]
    )
    assert verified.exit_code == 0
    assert '"valid"' in verified.stdout


def test_verification_fails_after_artifact_is_removed(home_root: Path) -> None:
    store = _prepare(home_root)
    info = store.put(b"silinecek")
    manifest_path = home_root / "yedek.json"
    runner.invoke(
        app, ["backup", "create", "--home", str(home_root), "--cikti", str(manifest_path)]
    )

    store.delete(info.digest)
    result = runner.invoke(app, ["backup", "verify", str(manifest_path), "--home", str(home_root)])
    assert result.exit_code == 2
    assert "incomplete" in result.stdout


def test_verification_fails_after_manifest_is_edited(home_root: Path) -> None:
    _prepare(home_root)
    manifest_path = home_root / "yedek.json"
    runner.invoke(
        app, ["backup", "create", "--home", str(home_root), "--cikti", str(manifest_path)]
    )

    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["product_version"] = "99.0.0"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    result = runner.invoke(app, ["backup", "verify", str(manifest_path), "--home", str(home_root)])
    assert result.exit_code == 2
    assert "altered" in result.stdout


def test_manifest_to_stdout_is_json(home_root: Path) -> None:
    _prepare(home_root)
    result = runner.invoke(app, ["backup", "create", "--home", str(home_root)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["product"] == "Zekam"
