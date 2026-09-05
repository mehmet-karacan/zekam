"""Portable paket projection kaniti testleri."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from scripts.paket_dogrula import (
    removed_product_identity_hits,
    schema_root_is_strict,
    validate_portable_phase_baseline,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_schema_strictness_resolves_every_root_union_reference() -> None:
    strict_object = {"type": "object", "additionalProperties": False}
    document = {
        "$defs": {
            "strict": strict_object,
            "union": {"anyOf": [{"$ref": "#/$defs/strict"}, strict_object]},
        },
        "anyOf": [{"$ref": "#/$defs/union"}],
    }
    assert schema_root_is_strict(document)

    document["$defs"]["union"]["anyOf"].append(  # type: ignore[index]
        {"type": "object", "additionalProperties": True}
    )
    assert not schema_root_is_strict(document)


def test_portable_phase_baseline_passes_without_local_state() -> None:
    graph = yaml.safe_load(
        (ROOT / "kalite" / "UYGULAMA_IS_GRAFIGI.yaml").read_text(encoding="utf-8")
    )
    baseline = json.loads(
        (ROOT / "kalite" / "PHASE_PROJECTION_BASELINE.json").read_text(encoding="utf-8")
    )

    checked, findings = validate_portable_phase_baseline(graph["phases"], baseline)

    assert checked == 18
    assert findings == []


def test_portable_phase_baseline_rejects_digest_drift() -> None:
    graph = yaml.safe_load(
        (ROOT / "kalite" / "UYGULAMA_IS_GRAFIGI.yaml").read_text(encoding="utf-8")
    )
    baseline = json.loads(
        (ROOT / "kalite" / "PHASE_PROJECTION_BASELINE.json").read_text(encoding="utf-8")
    )
    tampered = copy.deepcopy(baseline)
    tampered["phases"][0]["completed_digest"] = "sha256:" + "0" * 64

    _, findings = validate_portable_phase_baseline(graph["phases"], tampered)

    assert "Portable completed projection drift: ZEKAM-P00" in findings


def test_removed_product_identity_scan_covers_source_sql_and_dotfiles(tmp_path: Path) -> None:
    removed = "".join(chr(item) for item in (101, 110, 97, 105))
    (tmp_path / "module.py").write_text("provider = 'openai'\n", encoding="utf-8")
    (tmp_path / "migration.sql").write_text(f"grant usage to {removed}_app;\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(f".{removed}/\n", encoding="utf-8")

    assert removed_product_identity_hits(tmp_path) == [".gitignore", "migration.sql"]


def test_removed_product_identity_scan_rejects_empty_or_binary_legacy_path(
    tmp_path: Path,
) -> None:
    removed = "".join(chr(item) for item in (101, 110, 97, 105))
    legacy_directory = tmp_path / f"src-{removed}"
    legacy_directory.mkdir()
    (legacy_directory / "empty.bin").write_bytes(b"")

    assert removed_product_identity_hits(tmp_path) == [f"src-{removed}/empty.bin"]


def test_removed_product_identity_scan_includes_untracked_delivery_file(
    tmp_path: Path,
) -> None:
    removed = "".join(chr(item) for item in (101, 110, 97, 105))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.py").write_text("provider = 'openai'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.py"], check=True)
    (tmp_path / "new_delivery.md").write_text(
        f"old product: {removed}\n",
        encoding="utf-8",
    )

    assert removed_product_identity_hits(tmp_path) == ["new_delivery.md"]
