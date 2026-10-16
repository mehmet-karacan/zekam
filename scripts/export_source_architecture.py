#!/usr/bin/env python3
"""Export source declarations locally; never run tests or publish externally."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from zekam.application.architecture_export import (  # noqa: E402
    export_architecture,
    verify_architecture_export,
)
from zekam.domain.canonical import canonical_json  # noqa: E402
from zekam.domain.errors import ZekamError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path, help="New absolute directory outside checkout")
    action.add_argument(
        "--verify",
        type=Path,
        help="Existing absolute export directory to compare with current source",
    )
    args = parser.parse_args()
    try:
        report = (
            verify_architecture_export(ROOT, args.verify)
            if args.verify is not None
            else export_architecture(ROOT, args.output)
        )
    except (ZekamError, OSError, ValueError, UnicodeError) as exc:
        print(f"Architecture export failed: {type(exc).__name__}", file=sys.stderr)
        return 70
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
