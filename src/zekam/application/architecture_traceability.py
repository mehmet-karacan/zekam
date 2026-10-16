"""Explicit source links only; never infer execution or acceptance from prose."""

from __future__ import annotations

import re

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed


def task_package_scopes(text: str) -> list[dict]:
    scopes, current = [], None
    for line_number, line in enumerate(text.splitlines(), 1):
        if re.match(r"^## Ek [A-Z]\b", line):
            break
        match = re.fullmatch(r"### (WP-\d{2})\s*[—-]\s*(.+)", line)
        if match:
            current = {
                "id": match[1],
                "line": line_number,
                "requirements": [],
                "capabilities": [],
                "declared_test_refs": [],
                "scope_references": [],
            }
            scopes.append(current)
        elif line.startswith("## "):
            current = None
        if current is None:
            continue
        if line.startswith("**Girdi/kapsam:**"):
            for prefix, width, target in (("REQ", 3, "requirements"), ("CAP", 2, "capabilities")):
                pattern = rf"\b{prefix}-(\d{{{width}}}(?:/\d{{{width}}})*)"
                for group in re.findall(pattern, line):
                    for number in group.split("/"):
                        identity = f"{'ZKM-' if prefix == 'REQ' else ''}{prefix}-{number}"
                        if identity not in current[target]:
                            current[target].append(identity)
                        reference = {"id": identity, "authority_line": line_number}
                        if reference not in current["scope_references"]:
                            current["scope_references"].append(reference)
        for path in re.findall(r"\btests/[A-Za-z0-9_./-]+\.py\b", line):
            if any(part in {"", ".", ".."} for part in path.split("/")):
                raise ValidationFailed("Architecture declared test path invalid")
            record = {"path": path, "authority_line": line_number}
            if record not in current["declared_test_refs"]:
                current["declared_test_refs"].append(record)
    if len({scope["id"] for scope in scopes}) != len(scopes):
        raise ValidationFailed("Architecture duplicate package scope")
    return scopes


def build_source_traceability(task: dict, sources: list[dict]) -> dict:
    requirements = {item["id"] for item in task["requirements"]}
    capabilities = {item["id"] for item in task["capabilities"]}
    files = {item["path"]: item for item in sources}
    scopes, edges = [], []
    for scope in task["package_scopes"]:
        if set(scope["requirements"]) - requirements or set(scope["capabilities"]) - capabilities:
            raise ValidationFailed("Architecture scope references unknown requirement/capability")
        links = []
        for declaration in scope["declared_test_refs"]:
            source = files.get(declaration["path"])
            tests = source.get("tests", []) if source is not None else []
            links.append(
                declaration
                | {
                    "source_id": source["id"] if source is not None else None,
                    "source_digest": source["digest"] if source is not None else None,
                    "state": "test-source-found"
                    if tests
                    else "source-without-test-declarations"
                    if source is not None
                    else "declared-test-path-missing",
                    "tests": [{"name": item["name"], "line": item["line"]} for item in tests],
                    "run": None,
                    "evidence": None,
                    "execution_commit": None,
                }
            )
        scopes.append(scope | {"test_links": links})
        for reference in scope["scope_references"]:
            edges.append(
                {
                    "from": reference["id"],
                    "to": scope["id"],
                    "basis": "task-explicit-input-scope",
                    "authority_line": reference["authority_line"],
                }
            )
    mapped = {identity for scope in scopes for identity in scope["requirements"]}
    body = {
        "schema": "zekam-source-traceability/v1",
        "basis": "task-explicit-scope-and-test-path-declarations",
        "authority_digest": task["source_digest"],
        "edges": edges,
        "package_scopes": scopes,
        "requirements_without_explicit_scope": sorted(requirements - mapped),
        "packages_without_declared_tests": sorted(
            scope["id"] for scope in scopes if not scope["declared_test_refs"]
        ),
        "missing_test_paths": sorted(
            {
                link["path"]
                for scope in scopes
                for link in scope["test_links"]
                if link["source_id"] is None
            }
        ),
        "execution_evidence_attached": False,
        "release_chain_complete": False,
        "acceptance_verified": False,
        "grants_authority": False,
    }
    return body | {"traceability_digest": digest(body)}
