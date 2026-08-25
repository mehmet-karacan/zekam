"""Generate or verify the tracked App Server v1 JSON Schema bundle."""

from __future__ import annotations

import argparse
from pathlib import Path

from zekam.protocol.generation import render_protocol_artifacts

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "schemas" / "app-server-protocol-v1.schema.json"


def rendered_schema() -> str:
    return render_protocol_artifacts()["schema-bundle.json"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = rendered_schema()
    if args.check:
        return 0 if TARGET.is_file() and TARGET.read_text(encoding="utf-8") == rendered else 1
    TARGET.write_text(rendered, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
