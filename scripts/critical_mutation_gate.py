"""Deterministic mutation-quality gate for critical local validators and policies.

The gate never mutates the checkout.  It copies ``src`` to an isolated temporary
directory, applies one bounded AST mutation, and runs the fixed owning test file
in a fresh subprocess.  Results are canonical JSON suitable for release evidence.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Final, NamedTuple

MIN_SCORE: Final = 0.75
MUTANTS_PER_FILE: Final = 50
TIMEOUT_SECONDS: Final = 30


class Target(NamedTuple):
    source: str
    test: str


TARGETS: Final = (
    Target("src/zekam/domain/model_routing.py", "tests/unit/test_model_routing.py"),
    Target(
        "src/zekam/infrastructure/local_analytics.py",
        "tests/integration/test_local_analytics.py",
    ),
)


class Mutation(NamedTuple):
    family: str
    lineno: int
    col_offset: int
    node_kind: str
    detail: str


_COMPARE_FLIPS: Final = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
}
_MEMBERSHIP_FLIPS: Final = {
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _has_raise(node: ast.If) -> bool:
    return any(isinstance(child, ast.Raise) for child in ast.walk(node))


def _candidates(source: str) -> list[Mutation]:
    tree = ast.parse(source)
    found: list[Mutation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            operator = node.ops[0]
            if type(operator) in _COMPARE_FLIPS:
                found.append(
                    Mutation(
                        "comparison_boundary",
                        node.lineno,
                        node.col_offset,
                        "Compare",
                        type(operator).__name__,
                    )
                )
            elif type(operator) in _MEMBERSHIP_FLIPS:
                found.append(
                    Mutation(
                        "membership_enum",
                        node.lineno,
                        node.col_offset,
                        "Compare",
                        type(operator).__name__,
                    )
                )
        elif isinstance(node, ast.BoolOp):
            found.append(
                Mutation(
                    "boolean_negation",
                    node.lineno,
                    node.col_offset,
                    "BoolOp",
                    type(node.op).__name__,
                )
            )
        elif isinstance(node, ast.If) and node.body and isinstance(node.body[0], ast.Raise):
            found.append(
                Mutation(
                    "guard_raise_removal",
                    node.lineno,
                    node.col_offset,
                    "If",
                    "direct_raise",
                )
            )
        elif isinstance(node, ast.If) and _has_raise(node):
            found.append(
                Mutation(
                    "validation_evidence_omission",
                    node.lineno,
                    node.col_offset,
                    "If",
                    "nested_raise",
                )
            )
        elif (
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is bool
        ):
            found.append(
                Mutation(
                    "success_failure_literal",
                    node.lineno,
                    node.col_offset,
                    "Return",
                    str(node.value.value).lower(),
                )
            )
    return sorted(set(found), key=lambda item: (item.lineno, item.col_offset, item.family))


def _select(candidates: list[Mutation]) -> list[Mutation]:
    by_family: dict[str, list[Mutation]] = {}
    for candidate in candidates:
        by_family.setdefault(candidate.family, []).append(candidate)
    selected: list[Mutation] = []
    # Every available family gets a representative; remaining slots favour
    # concrete predicate mutations over broad guard removal, reducing the
    # expected-equivalent population without consulting test outcomes.
    for family in sorted(by_family):
        selected.append(by_family[family].pop(0))
    priority = (
        "comparison_boundary",
        "membership_enum",
        "boolean_negation",
        "validation_evidence_omission",
        "guard_raise_removal",
        "success_failure_literal",
    )
    for family in priority:
        while by_family.get(family) and len(selected) < MUTANTS_PER_FILE:
            selected.append(by_family[family].pop(0))
    if len(selected) != MUTANTS_PER_FILE:
        raise RuntimeError(f"insufficient deterministic mutations: {len(selected)}")
    return selected


class _Apply(ast.NodeTransformer):
    def __init__(self, mutation: Mutation) -> None:
        self.mutation = mutation
        self.applied = 0

    def _matches(self, node: ast.AST, kind: str) -> bool:
        return (
            type(node).__name__ == kind
            and getattr(node, "lineno", -1) == self.mutation.lineno
            and getattr(node, "col_offset", -1) == self.mutation.col_offset
        )

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if self.applied != 0 or not self._matches(node, "Compare"):
            return node
        operator = node.ops[0]
        replacements = _COMPARE_FLIPS | _MEMBERSHIP_FLIPS
        node.ops[0] = replacements[type(operator)]()
        self.applied += 1
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        if self.applied == 0 and self._matches(node, "BoolOp"):
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            self.applied += 1
        return node

    def visit_If(self, node: ast.If) -> ast.AST:
        self.generic_visit(node)
        if self.applied == 0 and self._matches(node, "If"):
            node.test = ast.Constant(value=False)
            self.applied += 1
        return node

    def visit_Return(self, node: ast.Return) -> ast.AST:
        self.generic_visit(node)
        if self.applied == 0 and self._matches(node, "Return"):
            assert isinstance(node.value, ast.Constant)
            node.value = ast.Constant(value=not node.value.value)
            self.applied += 1
        return node


def _mutated(source: str, mutation: Mutation) -> str:
    tree = ast.parse(source)
    transformer = _Apply(mutation)
    tree = transformer.visit(tree)
    ast.fix_missing_locations(tree)
    if transformer.applied != 1:
        raise RuntimeError(f"mutation {mutation!r} applied {transformer.applied} times")
    return ast.unparse(tree) + "\n"


def _run_test(repo: Path, isolated_src: Path, test: str) -> tuple[int, str, float]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(isolated_src)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-c",
                str(isolated_src.parent / "pytest.ini"),
                str(isolated_src.parent / test),
            ],
            cwd=isolated_src.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
        summary = (completed.stdout + completed.stderr)[-2000:]
        return completed.returncode, summary, time.monotonic() - started
    except subprocess.TimeoutExpired:
        return 124, "mutation test timed out", time.monotonic() - started


def run(repo: Path) -> dict[str, object]:
    before = {target.source: _sha256((repo / target.source).read_bytes()) for target in TARGETS}
    results: list[dict[str, object]] = []
    baselines: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory(prefix="zekam-critical-mutation-") as raw_temp:
        temp = Path(raw_temp)
        isolated_src = temp / "src"
        shutil.copytree(repo / "src", isolated_src)
        shutil.copytree(repo / "tests", temp / "tests")
        (temp / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        for target in TARGETS:
            source_path = repo / target.source
            isolated_path = temp / target.source
            original = source_path.read_text(encoding="utf-8")
            baseline_code, baseline_output, baseline_seconds = _run_test(
                repo, isolated_src, target.test
            )
            baselines[target.source] = {
                "test": target.test,
                "returncode": baseline_code,
                "seconds": round(baseline_seconds, 3),
                "output_tail": baseline_output,
            }
            if baseline_code != 0:
                continue
            for ordinal, mutation in enumerate(_select(_candidates(original)), start=1):
                isolated_path.write_text(_mutated(original, mutation), encoding="utf-8")
                code, output, seconds = _run_test(repo, isolated_src, target.test)
                isolated_path.write_text(original, encoding="utf-8")
                results.append(
                    {
                        "id": f"{Path(target.source).stem}-{ordinal:02d}",
                        "source": target.source,
                        "source_sha256": before[target.source],
                        "test": target.test,
                        "family": mutation.family,
                        "line": mutation.lineno,
                        "column": mutation.col_offset,
                        "detail": mutation.detail,
                        "status": "survived" if code == 0 else "killed",
                        "returncode": code,
                        "seconds": round(seconds, 3),
                        "output_tail": output,
                    }
                )
    after = {target.source: _sha256((repo / target.source).read_bytes()) for target in TARGETS}
    killed = sum(result["status"] == "killed" for result in results)
    survived = sum(result["status"] == "survived" for result in results)
    invalid = len(TARGETS) * MUTANTS_PER_FILE - len(results)
    denominator = killed + survived
    score = killed / denominator if denominator else 0.0
    families = sorted({str(result["family"]) for result in results})
    passed = (
        invalid == 0
        and before == after
        and len(results) >= 100
        and len(families) == 6
        and score >= MIN_SCORE
    )
    return {
        "schema": "zekam.critical-mutation-gate.v1",
        "passed": passed,
        "threshold": MIN_SCORE,
        "score": score,
        "killed": killed,
        "survived": survived,
        "invalid": invalid,
        "mutants": len(results),
        "operator_families": families,
        "production_hashes_unchanged": before == after,
        "baselines": baselines,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = run(args.repo.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"mutants={result['mutants']} killed={result['killed']} "
        f"survived={result['survived']} score={result['score']:.3f}"
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
