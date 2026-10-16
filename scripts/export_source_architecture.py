#!/usr/bin/env python3
"""Export source declarations locally; never run tests or publish externally."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from zekam.application.architecture_export import export_architecture  # noqa: E402
from zekam.domain.canonical import canonical_json  # noqa: E402
from zekam.domain.errors import ZekamError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", required=True, type=Path, help="New absolute directory outside checkout"
    )
    args = parser.parse_args()
    try:
        report = export_architecture(ROOT, args.output)
    except (ZekamError, OSError, ValueError, UnicodeError) as exc:
        print(f"Architecture export failed: {type(exc).__name__}", file=sys.stderr)
        return 70
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
