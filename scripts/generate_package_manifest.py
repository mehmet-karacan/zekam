"""Generate or check the deterministic shipped package manifest v3."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from zekam.application.package_acceptance import build_package_manifest

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "zekam" / "PACKAGE_RELEASE_MANIFEST.json"


def rendered() -> str:
    return (
        json.dumps(
            build_package_manifest(ROOT / "src" / "zekam").body(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    body = rendered()
    if args.check:
        return 0 if TARGET.is_file() and TARGET.read_text(encoding="utf-8") == body else 1
    TARGET.write_text(body, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
