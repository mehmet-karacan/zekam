"""Generate or check the exact-version protocol SDK resources."""

from __future__ import annotations

import argparse
from pathlib import Path

from zekam.protocol.generation import generate_protocol_artifacts

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "src" / "zekam" / "protocol"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    return 0 if generate_protocol_artifacts(args.output, check=args.check) else 1


if __name__ == "__main__":
    raise SystemExit(main())
