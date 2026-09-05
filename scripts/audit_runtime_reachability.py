"""Fail-closed audit for the local-first shipped runtime graph.

The PostgreSQL-era implementations remain source-controlled reference material
until their regression coverage is retired.  They are intentionally absent from
the wheel and must not be reachable from a public Mac runtime composition root.
"""

from __future__ import annotations

import argparse
import ast
import json
import stat
import tomllib
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "zekam-runtime-reachability-audit/v1"

ARCHIVE_ONLY_SOURCE_PATHS = (
    "src/zekam/application/client_lifecycle_composition.py",
    "src/zekam/application/client_lifecycle_continuity.py",
    "src/zekam/application/client_runtime_bootstrap.py",
    "src/zekam/application/diagnostic_trace_composition.py",
    "src/zekam/application/doctor_repair_runtime.py",
    "src/zekam/application/execution.py",
    "src/zekam/application/legacy_repository_provider.py",
    "src/zekam/application/lifecycle_runtime_template_prepare.py",
    "src/zekam/application/lifecycle_template_recovery.py",
    "src/zekam/application/measured_loop_runtime.py",
    "src/zekam/application/measured_loop_worker.py",
    "src/zekam/application/project_integration.py",
    "src/zekam/application/projection_close_runtime.py",
    "src/zekam/application/provider_contract_runner.py",
    "src/zekam/application/recovery_reconciliation.py",
    "src/zekam/application/resume_apply_service.py",
    "src/zekam/application/run_reconciliation.py",
    "src/zekam/application/work_graph.py",
    "src/zekam/application/worker.py",
)

PUBLIC_RUNTIME_ROOTS = (
    "zekam.interfaces.cli.main",
    "zekam.interfaces.cli.worker",
    "zekam.interfaces.cli.scheduler",
    "zekam.interfaces.api.health",
    "zekam.interfaces.api.app_server",
    "zekam.interfaces.api.observatory",
)

FORBIDDEN_RUNTIME_MODULES = frozenset(
    path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    for path in ARCHIVE_ONLY_SOURCE_PATHS
)
FORBIDDEN_WHEEL_PATHS = frozenset(path.removeprefix("src/") for path in ARCHIVE_ONLY_SOURCE_PATHS)


@dataclass(frozen=True, slots=True)
class AuditReport:
    findings: tuple[str, ...]
    reachable_modules: tuple[str, ...]
    wheel_entries: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "passed": self.passed,
            "findings": list(self.findings),
            "public_runtime_roots": list(PUBLIC_RUNTIME_ROOTS),
            "reachable_module_count": len(self.reachable_modules),
            "archive_only_module_count": len(FORBIDDEN_RUNTIME_MODULES),
            "wheel_entry_count": len(self.wheel_entries),
            "legacy_postgresql_data_accessed": False,
            "grants_authority": False,
        }


class _RuntimeImportVisitor(ast.NodeVisitor):
    """Collect imports while ignoring imports guarded only by TYPE_CHECKING."""

    def __init__(self, *, module: str, is_package: bool = False) -> None:
        self.imports: set[str] = set()
        self._package_parts = module.split(".") if is_package else module.split(".")[:-1]

    def visit_If(self, node: ast.If) -> None:
        if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            for branch in node.orelse:
                self.visit(branch)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.update(alias.name for alias in node.names if alias.name.startswith("zekam"))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            parent_count = node.level - 1
            if parent_count > len(self._package_parts):
                return
            prefix = self._package_parts[: len(self._package_parts) - parent_count]
            module = ".".join((*prefix, *module.split(".")))
        if not module.startswith("zekam"):
            return
        self.imports.add(module)
        self.imports.update(f"{module}.{alias.name}" for alias in node.names)


def _module_name(source_root: Path, path: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _source_graph(source_root: Path) -> tuple[dict[str, set[str]], list[str]]:
    findings: list[str] = []
    paths = {
        _module_name(source_root, path): path
        for path in sorted((source_root / "zekam").rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    graph: dict[str, set[str]] = {}
    for module, path in paths.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            findings.append(f"source-parse-failed:{module}:{type(exc).__name__}")
            continue
        visitor = _RuntimeImportVisitor(module=module, is_package=path.name == "__init__.py")
        visitor.visit(tree)
        graph[module] = {imported for imported in visitor.imports if imported in paths}
    return graph, findings


def _reachable(graph: dict[str, set[str]]) -> tuple[str, ...]:
    visited: set[str] = set()
    pending = deque(PUBLIC_RUNTIME_ROOTS)
    while pending:
        module = pending.popleft()
        if module in visited:
            continue
        visited.add(module)
        pending.extend(sorted(graph.get(module, ())))
    return tuple(sorted(visited))


def _configured_exclusions(pyproject: Path) -> tuple[set[str], list[str]]:
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        raw = document["tool"]["hatch"]["build"]["targets"]["wheel"]["exclude"]
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        return set(), [f"wheel-exclusion-config-invalid:{type(exc).__name__}"]
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        return set(), ["wheel-exclusion-config-invalid:type"]
    normalized = {value.removeprefix("/") for value in raw}
    expected = set(ARCHIVE_ONLY_SOURCE_PATHS)
    findings = []
    if normalized != expected:
        missing = sorted(expected - normalized)
        extra = sorted(normalized - expected)
        if missing:
            findings.append(f"wheel-exclusion-missing:{','.join(missing)}")
        if extra:
            findings.append(f"wheel-exclusion-unreviewed:{','.join(extra)}")
    return normalized, findings


def _wheel_inventory(wheel: Path) -> tuple[tuple[str, ...], list[str]]:
    findings: list[str] = []
    try:
        with zipfile.ZipFile(wheel) as archive:
            names: list[str] = []
            folded: set[str] = set()
            for info in archive.infolist():
                if info.is_dir():
                    continue
                raw_name = info.filename
                path = PurePosixPath(raw_name)
                if (
                    not raw_name
                    or "\\" in raw_name
                    or path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                ):
                    findings.append(f"wheel-path-invalid:{raw_name}")
                    continue
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_IFMT(mode) and not stat.S_ISREG(mode):
                    findings.append(f"wheel-entry-not-regular:{raw_name}")
                    continue
                normalized = path.as_posix()
                if normalized.casefold() in folded:
                    findings.append(f"wheel-path-duplicate:{normalized}")
                    continue
                folded.add(normalized.casefold())
                names.append(normalized)
            name_set = set(names)
            shipped_legacy = sorted(name_set & FORBIDDEN_WHEEL_PATHS)
            if shipped_legacy:
                findings.append(f"wheel-ships-archive-only:{','.join(shipped_legacy)}")
            postgres_paths = sorted(
                name
                for name in names
                if name.startswith("zekam/infrastructure/postgres/")
                or name == "zekam/infrastructure/postgres.py"
            )
            if postgres_paths:
                findings.append(f"wheel-ships-postgres-adapter:{','.join(postgres_paths)}")
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                findings.append(f"wheel-metadata-cardinality:{len(metadata_names)}")
            else:
                metadata_body = archive.read(metadata_names[0]).decode("utf-8", errors="replace")
                requirements = [
                    line.casefold()
                    for line in metadata_body.splitlines()
                    if line.casefold().startswith("requires-dist:")
                ]
                if any("psycopg" in line or "pgvector" in line for line in requirements):
                    findings.append("wheel-metadata-postgresql-dependency")
    except (OSError, zipfile.BadZipFile) as exc:
        return (), [f"wheel-unreadable:{type(exc).__name__}"]
    return tuple(sorted(names)), findings


def audit(repository: Path, wheel: Path | None = None) -> AuditReport:
    repository = repository.resolve()
    source_root = repository / "src"
    graph, findings = _source_graph(source_root)
    reachable = _reachable(graph)
    missing_roots = sorted(root for root in PUBLIC_RUNTIME_ROOTS if root not in graph)
    if missing_roots:
        findings.append(f"public-runtime-root-missing:{','.join(missing_roots)}")
    leaked = sorted(set(reachable) & FORBIDDEN_RUNTIME_MODULES)
    if leaked:
        findings.append(f"public-runtime-reaches-archive-only:{','.join(leaked)}")
    for path in ARCHIVE_ONLY_SOURCE_PATHS:
        if not (repository / path).is_file():
            findings.append(f"archive-only-source-missing:{path}")
    _, exclusion_findings = _configured_exclusions(repository / "pyproject.toml")
    findings.extend(exclusion_findings)
    wheel_entries: tuple[str, ...] = ()
    if wheel is not None:
        wheel_entries, wheel_findings = _wheel_inventory(wheel.resolve())
        findings.extend(wheel_findings)
    return AuditReport(
        findings=tuple(sorted(set(findings))),
        reachable_modules=reachable,
        wheel_entries=wheel_entries,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    report = audit(args.repository, args.wheel)
    print(json.dumps(report.as_dict(), ensure_ascii=True, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
