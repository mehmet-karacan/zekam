"""Bootstrap phase evidence generator/schema consistency test."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType


def _load_script(root: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("faz_kaniti", root / "scripts" / "faz_kaniti.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_checkpoint_and_continuity_match_canonical_schemas() -> None:
    root = Path(__file__).resolve().parents[2]
    module = _load_script(root)
    module.__dict__["_source_revision"] = lambda: "revision-1"
    module.__dict__["_latest_quality_evidence"] = lambda phase: None
    module.__dict__["_changed_files"] = lambda: []
    records = module.build_records(
        phase="ZEKAM-P08",
        tasks=("ZEKAM-P08-T01",),
        pending=("ZEKAM-P08-T02",),
        next_safe_action="reacquire-work",
        now=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
    )
    for name, schema_name in (
        ("checkpoint", "checkpoint.schema.json"),
        ("continuity_packet", "continuity-packet.schema.json"),
    ):
        schema = json.loads((root / "schemas" / schema_name).read_text(encoding="utf-8"))
        document = records[name]
        assert set(schema["required"]) <= set(document)
        assert set(document) <= set(schema["properties"])
        digest_field = "checkpoint_digest" if name == "checkpoint" else "packet_digest"
        assert re.fullmatch(r"sha256:[a-f0-9]{64}", document[digest_field])
    assert all(
        isinstance(item, dict) for item in records["continuity_packet"]["authoritative_refs"]
    )
