"""Fail-closed audit for the supported Windows/Linux test and package scope."""

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

SCHEMA = "zekam-platform-scope-audit/v2"
MAX_FILE_BYTES = 4 * 1024 * 1024
REMOVED_MACOS_SOURCES = frozenset(
    {
        "src/zekam/application/local_continuity_v4_compaction.py",
        "src/zekam/application/local_continuity_v4_ingress.py",
        "src/zekam/infrastructure/clients/codex_macos_0151_lifecycle.py",
        "src/zekam/infrastructure/clients/codex_macos_0151_precompaction_client.py",
        "src/zekam/infrastructure/embedding/infinity_bge.py",
        "src/zekam/infrastructure/local_continuity_v4_composition.py",
        "src/zekam/infrastructure/macos_precompaction_supervisor.py",
        "src/zekam/infrastructure/sqlite/local_continuity_v4_compaction.py",
        "src/zekam/infrastructure/sqlite/local_continuity_v4_ingress.py",
    }
)
FORBIDDEN_TEST_PATH_PARTS = frozenset(
    {"macos", "darwin", "osx", "infinity_bge", "local_bge_provider"}
)
FORBIDDEN_TEST_IMPORTS = (
    "codex_macos_0151",
    "macos_precompaction_supervisor",
    "infrastructure.embedding.infinity_bge",
)
FORBIDDEN_SCOPE_MARKERS = ("macos", "mac os", "darwin", "osx")


def _tracked_paths(repository: Path) -> tuple[Path, ...]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "ls-files", "--", "tests", ".github", "benchmarks/suites"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise RuntimeError(f"tracked scope inventory failed: {type(exc).__name__}") from exc
    paths: list[Path] = []
    for raw in completed.stdout.splitlines():
        if not raw or "\\" in raw or any(part in {"", ".", ".."} for part in Path(raw).parts):
            raise RuntimeError("tracked scope path is not canonical")
        path = repository / Path(raw)
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"tracked scope target is not a regular file: {raw}")
        paths.append(path)
    return tuple(paths)


def _read_bounded(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise RuntimeError(f"scope file exceeds bound: {path.as_posix()}")
    payload = path.read_bytes()
    if len(payload) > MAX_FILE_BYTES:
        raise RuntimeError(f"scope file exceeds bound: {path.as_posix()}")
    return payload.decode("utf-8", errors="strict")


def _wheel_exclusions(repository: Path) -> set[str]:
    try:
        document = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
        values = document["tool"]["hatch"]["build"]["targets"]["wheel"]["exclude"]
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"wheel exclusion policy unreadable: {type(exc).__name__}") from exc
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise RuntimeError("wheel exclusion policy must be a string list")
    return {value.removeprefix("/") for value in values}


def audit(repository: Path) -> dict[str, Any]:
    repository = repository.resolve()
    findings: list[str] = []
    try:
        tracked = _tracked_paths(repository)
        exclusions = _wheel_exclusions(repository)
    except RuntimeError as exc:
        return {
            "schema": SCHEMA,
            "passed": False,
            "findings": [str(exc)],
            "test_file_count": 0,
            "forbidden_test_count": 0,
            "forbidden_scope_file_count": 0,
            "removed_macos_source_count": 0,
            "macos_source_present_count": 0,
            "grants_authority": False,
        }

    macos_source_present_count = 0
    for source in sorted(REMOVED_MACOS_SOURCES):
        if (repository / source).exists():
            macos_source_present_count += 1
            findings.append(f"macos-source-present:{source}")
        if source in exclusions:
            findings.append(f"macos-source-stale-wheel-exclusion:{source}")

    test_paths = tuple(
        path
        for path in tracked
        if "tests" in path.relative_to(repository).parts and path.suffix.casefold() == ".py"
    )
    forbidden_test_count = 0
    forbidden_scope_file_count = 0
    for path in tracked:
        relative = path.relative_to(repository).as_posix()
        parts = path.relative_to(repository).parts
        is_test = "tests" in parts
        is_ci_or_benchmark = ".github" in parts or "benchmarks" in parts
        if not (is_test or is_ci_or_benchmark):
            continue
        # Binary test fixtures are inventory entries, not executable test scope;
        # inspect only Python tests while still scanning all CI/benchmark text.
        if is_test and path.suffix.casefold() != ".py":
            continue
        folded = relative.casefold()
        if is_test and any(part in folded for part in FORBIDDEN_TEST_PATH_PARTS):
            forbidden_test_count += 1
            findings.append(f"macos-test-path:{relative}")
        try:
            body = _read_bounded(path)
        except (OSError, UnicodeError, RuntimeError) as exc:
            findings.append(f"test-read-failed:{relative}:{type(exc).__name__}")
            continue
        body_folded = body.casefold()
        for marker in FORBIDDEN_SCOPE_MARKERS:
            if marker in body_folded:
                forbidden_scope_file_count += 1
                if is_test:
                    forbidden_test_count += 1
                    findings.append(f"macos-test-marker:{relative}:{marker}")
                else:
                    findings.append(f"macos-ci-marker:{relative}:{marker}")
        if is_test:
            for marker in FORBIDDEN_TEST_IMPORTS:
                if marker in body:
                    forbidden_test_count += 1
                    findings.append(f"macos-test-import:{relative}:{marker}")

    return {
        "schema": SCHEMA,
        "passed": not findings,
        "findings": sorted(set(findings)),
        "test_file_count": len(test_paths),
        "forbidden_test_count": forbidden_test_count,
        "forbidden_scope_file_count": forbidden_scope_file_count,
        "removed_macos_source_count": len(REMOVED_MACOS_SOURCES),
        "macos_source_present_count": macos_source_present_count,
        "grants_authority": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    report = audit(parser.parse_args().repository)
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
