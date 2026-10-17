"""Build a wheel from the reviewed tracked package tree in the real checkout.

No source clone is made. Untracked modules cannot enter the release artifact;
Hatch receives exact force-include mappings from the existing manifest builder.
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

from hatchling.builders.wheel import WheelBuilder

from zekam.application.package_acceptance import (
    _package_source_entries,
    build_package_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


def build(output: Path) -> Path:
    output = output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("Wheel artifact repository disinda olmali")
    package = ROOT / "src" / "zekam"
    manifest = package / "PACKAGE_RELEASE_MANIFEST.json"
    if json.loads(manifest.read_text(encoding="utf-8")) != build_package_manifest(package).body():
        raise ValueError("Package manifest stale; once generate_package_manifest.py calistir")
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    target = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    includes: dict[str, str] = {}
    for entry in _package_source_entries(package, ROOT):
        relative = entry["path"]
        if relative == "__init__.py":
            continue  # The single ordinary include below establishes the src mapping.
        if relative.startswith("_config/"):
            source = ROOT / "config" / relative.removeprefix("_config/")
        elif relative.startswith(("schemas/", "modeller/")):
            source = ROOT / relative
        elif relative == "AKTIF_GOREV.md":
            source = ROOT / relative
        elif relative == "RADAR_RUNBOOK.md":
            source = ROOT / "docs" / relative
        else:
            source = package / relative
        includes[str(source)] = "zekam/" + relative
    includes[str(manifest)] = "zekam/PACKAGE_RELEASE_MANIFEST.json"
    target["only-include"] = ["src/zekam/__init__.py"]
    target["sources"] = {"src": ""}
    target["force-include"] = includes
    output.mkdir(parents=True, exist_ok=True)
    builder = WheelBuilder(str(ROOT), config=config)
    return Path(next(builder.build(directory=str(output), versions=["standard"])))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.output))


if __name__ == "__main__":
    main()
