"""Bounded source architecture inventory and deterministic local-only exports.

Declarations describe source, never operational Work state or accepted capability.
No application module is imported to discover commands, schema or tests.
"""

from __future__ import annotations

import ast
import html
import io
import json
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

from zekam.application.active_task_contract import ActiveTaskContract
from zekam.application.bounded_file import read_bounded_bytes
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_file_security import path_or_parent_is_link_or_reparse

MAX_FILES = 20_000
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_EXPORT_BYTES = 32 * 1024 * 1024
_WP = re.compile(r"^- \[([ xX])\] \*\*(WP-\d{2}\.\d{2})\*\*\s*[—-]\s*(.+)$")
_REQ = re.compile(r"^### (ZKM-REQ-\d{3})\s*[—-]\s*(.+)$")


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or len(result.stdout) > 4 * 1024 * 1024:
        raise ValidationFailed("Architecture source Git inventory unavailable/bounded")
    return result.stdout


def _selected(path: str) -> bool:
    return (
        path in {"AKTIF_GOREV.md", "pyproject.toml"}
        or path.startswith(("src/zekam/", "tests/"))
        and path.endswith(".py")
        or path.startswith(("mimari/", "docs/adr/"))
        and path.endswith(".md")
        or path.startswith("schemas/")
        and path.endswith(".json")
    )


def _task_records(raw: bytes) -> dict:
    text = raw.decode("utf-8", errors="strict")
    task = ActiveTaskContract.from_bytes(raw)
    requirements, capabilities, packages, decisions = [], [], [], []
    for number, line in enumerate(text.splitlines(), 1):
        # The preserved old request ledger repeats CAP/REQ IDs and has no current
        # authority. Never merge those historical declarations into this catalog.
        if re.match(r"^## Ek [A-Z]\b", line):
            break
        if match := _REQ.fullmatch(line):
            requirements.append({"id": match[1], "title": match[2], "line": number})
        elif match := _WP.fullmatch(line):
            packages.append(
                {
                    "id": match[2],
                    "title": match[3],
                    "line": number,
                    "document_checked": match[1].lower() == "x",
                    "acceptance_verified": False,
                }
            )
        elif line.startswith("| CAP-"):
            cells = [value.strip() for value in line.strip("|").split("|")]
            if len(cells) != 5 or re.fullmatch(r"CAP-\d{2}", cells[0]) is None:
                raise ValidationFailed("Architecture CAP source row invalid")
            capabilities.append(
                {
                    "id": cells[0],
                    "title": cells[1],
                    "declared_status": cells[2].strip("`"),
                    "work_packages": re.findall(r"WP-\d{2}", cells[3]),
                    "acceptance_criterion": cells[4],
                    "line": number,
                    "acceptance_verified": False,
                }
            )
        elif line.startswith("| D-"):
            cells = [value.strip() for value in line.strip("|").split("|")]
            if len(cells) != 3 or re.fullmatch(r"D-\d{2}", cells[0]) is None:
                raise ValidationFailed("Architecture decision source row invalid")
            decisions.append(
                {
                    "id": cells[0],
                    "title": cells[1],
                    "required_evidence": cells[2],
                    "line": number,
                    "resolution_verified": False,
                }
            )
    for records in (requirements, capabilities, packages):
        if not records or len({item["id"] for item in records}) != len(records):
            raise ValidationFailed("Architecture task declarations empty/duplicate")
    if len({item["id"] for item in decisions}) != len(decisions):
        raise ValidationFailed("Architecture decision declarations duplicate")
    parents = {item["id"].split(".")[0] for item in packages}
    if any(
        not item["work_packages"] or set(item["work_packages"]) - parents for item in capabilities
    ):
        raise ValidationFailed("Architecture CAP references unknown Work Package")
    return {
        "task_id": task.task_id,
        "source_digest": digest_of_bytes(raw),
        "requirements": requirements,
        "capabilities": capabilities,
        "work_packages": packages,
        "decision_gates": decisions,
    }


def _python_records(path: str, raw: bytes) -> dict:
    try:
        tree = ast.parse(raw, filename=path)
    except (SyntaxError, UnicodeError, ValueError) as exc:
        raise ValidationFailed("Architecture Python declaration parse failed") from exc
    interfaces, tables, tests, migrations = [], [], [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if path.startswith("tests/") and node.name.startswith("test_"):
                tests.append(
                    {"name": node.name, "line": node.lineno, "run": None, "evidence": None}
                )
            if path.startswith("src/zekam/interfaces/"):
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call) or not isinstance(
                        decorator.func, ast.Attribute
                    ):
                        continue
                    kind = decorator.func.attr
                    if kind not in {"command", "get", "post", "put", "patch", "delete"}:
                        continue
                    label = node.name
                    if (
                        decorator.args
                        and isinstance(decorator.args[0], ast.Constant)
                        and isinstance(decorator.args[0].value, str)
                    ):
                        label = decorator.args[0].value
                    interfaces.append(
                        {"kind": kind, "name": label, "function": node.name, "line": node.lineno}
                    )
        elif (
            path.startswith("src/zekam/infrastructure/sqlite/")
            and isinstance(node, ast.Assign | ast.AnnAssign)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) in {str, int}
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and re.fullmatch(
                    r"(?:[A-Z_]*SCHEMA_VERSION|MIGRATION_NAME|VERSION)", target.id
                ):
                    migrations.append(
                        {
                            "name": target.id,
                            "value": node.value.value,
                            "line": node.lineno,
                            "basis": "literal-source-declaration",
                        }
                    )
        elif (
            path.startswith("src/zekam/infrastructure/sqlite/")
            and isinstance(node, ast.Constant)
            and isinstance(node.value, str)
        ):
            for match in re.finditer(
                r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?([A-Za-z_][A-Za-z0-9_]*)",
                node.value,
                re.I,
            ):
                tables.append(
                    {"name": match[1], "line": node.lineno, "basis": "literal-source-declaration"}
                )
    return {"interfaces": interfaces, "tables": tables, "migrations": migrations, "tests": tests}


def build_architecture_catalog(root: Path) -> dict:
    if path_or_parent_is_link_or_reparse(root) or not root.is_dir():
        raise PolicyViolation("Architecture source requires a real checkout root")
    root = root.resolve(strict=True)
    if Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        raise PolicyViolation("Architecture requires the actual source checkout root")
    head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    if re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", head) is None:
        raise ValidationFailed("Architecture full source revision required")
    names = _git(root, "ls-files", "--cached", "-z").decode("utf-8", errors="strict").split("\0")
    names = sorted({name for name in names if name and _selected(name)})
    if not 1 <= len(names) <= MAX_FILES:
        raise ValidationFailed("Architecture source file budget exceeded")
    sources, total, task, exclusions = [], 0, None, set()
    for name in names:
        portable = PurePosixPath(name)
        if (
            portable.is_absolute()
            or re.fullmatch(r"[A-Za-z0-9_./-]+", name) is None
            or any(part in {"", ".", ".."} for part in portable.parts)
        ):
            raise PolicyViolation("Architecture source path not portable")
        path = root / portable
        if path_or_parent_is_link_or_reparse(path) or not path.is_file():
            raise PolicyViolation("Architecture tracked source missing or linked")
        raw = read_bounded_bytes(path, max_bytes=MAX_FILE_BYTES)
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise ValidationFailed("Architecture total source byte budget exceeded")
        item = {
            "id": f"source-{len(sources) + 1:05d}",
            "path": name,
            "digest": digest_of_bytes(raw),
            "bytes": len(raw),
        }
        if name.endswith(".py"):
            item.update(_python_records(name, raw))
        if name == "AKTIF_GOREV.md":
            task = _task_records(raw)
        elif name == "pyproject.toml":
            project = tomllib.loads(raw.decode("utf-8", errors="strict"))
            policy = (
                project.get("tool", {})
                .get("hatch", {})
                .get("build", {})
                .get("targets", {})
                .get("wheel", {})
                .get("exclude", [])
            )
            if not isinstance(policy, list) or any(not isinstance(value, str) for value in policy):
                raise ValidationFailed("Architecture wheel exclusion source invalid")
            exclusions = {value.removeprefix("/") for value in policy}
        sources.append(item)
    if task is None:
        raise ValidationFailed("Architecture tracked task authority missing")
    for item in sources:
        item["distribution_declaration"] = (
            "archive-only-wheel-exclusion"
            if item["path"] in exclusions
            else "source-inventory-not-wheel-verification"
        )
    retained = sorted(
        {
            name
            for name in _git(root, "ls-files", "--cached", "-z")
            .decode("utf-8", errors="strict")
            .split("\0")
            if name and _selected(name)
        }
    )
    if retained != names:
        raise PolicyViolation("Architecture tracked inventory changed during capture")
    for item in sources:
        path = root / item["path"]
        if (
            path_or_parent_is_link_or_reparse(path)
            or not path.is_file()
            or digest_of_bytes(read_bounded_bytes(path, max_bytes=MAX_FILE_BYTES)) != item["digest"]
        ):
            raise PolicyViolation("Architecture source content changed during capture")
    if _git(root, "rev-parse", "HEAD").decode().strip() != head:
        raise PolicyViolation("Architecture source revision changed during inventory")
    body = {
        "schema": "zekam-source-architecture/v1",
        "source_revision": head,
        "snapshot_basis": "tracked-working-tree-file-bytes",
        "scope": [
            "task",
            "python-source",
            "python-tests",
            "public-schemas",
            "architecture-documents",
            "decision-documents",
            "pyproject",
        ],
        "task": task,
        "sources": sources,
        "limitations": [
            "source-declarations-only",
            "not-runtime-schema-or-interface-discovery",
            "test-sources-not-test-results",
            "no-operational-state",
            "no-publication",
        ],
        "acceptance_verified": False,
        "grants_authority": False,
    }
    return body | {"catalog_digest": digest(body)}


def render_architecture_bundle(catalog: dict) -> dict[str, bytes]:
    body = {key: value for key, value in catalog.items() if key != "catalog_digest"}
    if (
        catalog.get("schema") != "zekam-source-architecture/v1"
        or digest(body) != catalog.get("catalog_digest")
        or catalog.get("acceptance_verified") is not False
        or catalog.get("grants_authority") is not False
    ):
        raise ValidationFailed("Architecture canonical catalog digest/policy invalid")
    # All views consume this one catalog. No separate capability/status source.
    lines = [
        "# Zekam source architecture",
        "",
        f"Catalog: `{catalog['catalog_digest']}`",
        "",
        "Source declarations only. Tests NOT RUN; acceptance unverified.",
        "",
        "[Canonical input](catalog.json) | [Diagram](architecture.mmd)",
        "",
    ]
    sections = []
    for key in ("requirements", "capabilities", "work_packages", "decision_gates"):
        lines.extend([f"## {key}", ""])
        for item in catalog["task"][key]:
            label = item["title"].replace("\n", " ")
            lines.append(f"- {item['id']}: {label}")
            sections.append(f'<p id="{item["id"]}">{html.escape(item["id"] + ": " + label)}</p>')
        lines.append("")
    source_rows = []
    for item in catalog["sources"]:
        lines.extend([f"## {item['id']}", "", f"`{item['path']}` — `{item['digest']}`", ""])
        details = {"distribution_declaration": item["distribution_declaration"]} | {
            key: item[key]
            for key in ("interfaces", "tables", "migrations", "tests")
            if item.get(key)
        }
        if details:
            lines.extend(
                ["```json", json.dumps(details, ensure_ascii=False, sort_keys=True), "```", ""]
            )
        source_rows.append(
            f'<section id="{item["id"]}"><h2>{html.escape(item["path"])}</h2><code>{item["digest"]}</code><pre>{html.escape(json.dumps(details, ensure_ascii=False, sort_keys=True))}</pre></section>'
        )
    diagram = (
        f"%% Catalog: {catalog['catalog_digest']}\nflowchart LR\n"
        + "\n".join(
            f"  {wp.replace('-', '_')} --> {cap['id'].replace('-', '_')}"
            for cap in catalog["task"]["capabilities"]
            for wp in cap["work_packages"]
        )
        + "\n"
    )
    page = (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Zekam source architecture</title><body><h1>Zekam source architecture</h1><p>Source declarations only. Tests NOT RUN; acceptance unverified.</p>'
        + f'<p>{catalog["catalog_digest"]}</p><a href="catalog.json">Canonical input</a> | <a href="architecture.mmd">Diagram</a>'
        + "".join(sections + source_rows)
        + "</body></html>"
    )
    files = {
        "catalog.json": canonical_bytes(catalog) + b"\n",
        "architecture.md": ("\n".join(lines) + "\n").encode("utf-8"),
        "architecture.html": page.encode("utf-8"),
        "architecture.mmd": diagram.encode("utf-8"),
    }
    manifest = {
        "schema": "zekam-architecture-export-manifest/v1",
        "catalog_digest": catalog["catalog_digest"],
        "files": {name: digest_of_bytes(raw) for name, raw in sorted(files.items())},
        "published": False,
        "grants_authority": False,
    }
    files["manifest.json"] = canonical_bytes(manifest) + b"\n"
    if sum(len(raw) for raw in files.values()) > MAX_EXPORT_BYTES:
        raise ValidationFailed("Architecture export byte budget exceeded")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            bundle.writestr(info, raw)
    return files | {"architecture.zip": archive.getvalue()}


def export_architecture(root: Path, destination: Path) -> dict:
    if path_or_parent_is_link_or_reparse(root):
        raise PolicyViolation("Architecture source root cannot be linked")
    root = root.resolve(strict=True)
    if not destination.is_absolute() or path_or_parent_is_link_or_reparse(destination):
        raise PolicyViolation(
            "Architecture export destination must be an explicit real absolute path"
        )
    destination = destination.resolve()
    if (
        destination.is_relative_to(root)
        or root.is_relative_to(destination)
        or destination.exists()
        or not destination.parent.is_dir()
    ):
        raise PolicyViolation(
            "Architecture export requires a new directory outside the source checkout"
        )
    catalog = build_architecture_catalog(root)
    files = render_architecture_bundle(catalog)
    destination.mkdir(exist_ok=False)
    created = []
    try:
        for name, raw in files.items():
            target = destination / name
            with target.open("xb") as stream:
                created.append(target)
                stream.write(raw)
    except OSError:
        for target in reversed(created):
            target.unlink()
        destination.rmdir()
        raise
    return {
        "catalog_digest": catalog["catalog_digest"],
        "files": {name: digest_of_bytes(raw) for name, raw in sorted(files.items())},
        "published": False,
        "grants_authority": False,
    }
