#!/usr/bin/env python3
"""Generate/check the immutable 14-family benchmark task resource catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGETS = (ROOT / "benchmarks", ROOT / "src" / "zekam" / "benchmarks")
FAMILIES = (
    "sql-plsql",
    "code-repair",
    "code-review",
    "architecture",
    "rag-retrieval",
    "tool-use",
    "agentic-workflow",
    "long-context",
    "document-analysis",
    "structured-output",
    "safety-policy",
    "embedding-retrieval",
    "reranking",
    "creative-tournament",
)
DIMENSIONS = {
    "sql-plsql": ("correctness", "evidence-citation", "safety"),
    "code-repair": ("correctness", "recovery", "safety"),
    "code-review": ("correctness", "evidence-citation", "safety"),
    "architecture": ("correctness", "evidence-citation", "structured-format"),
    "rag-retrieval": ("correctness", "evidence-citation", "reliability"),
    "tool-use": ("tool-correctness", "safety", "recovery"),
    "agentic-workflow": ("tool-correctness", "recovery", "reliability"),
    "long-context": ("correctness", "evidence-citation", "reliability"),
    "document-analysis": ("correctness", "evidence-citation", "structured-format"),
    "structured-output": ("correctness", "structured-format", "reliability"),
    "safety-policy": ("safety", "correctness", "reliability"),
    "embedding-retrieval": ("correctness", "reliability", "latency"),
    "reranking": ("correctness", "reliability", "latency"),
    "creative-tournament": ("correctness", "human-correction", "reliability"),
}


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _yaml(value: object) -> bytes:
    return yaml.safe_dump(value, allow_unicode=False, sort_keys=False).encode()


def _resources(family: str) -> dict[str, bytes]:
    evaluation_class = "creative" if family == "creative-tournament" else "technical"
    prompt_ref = f"resources/prompts/{family}.txt"
    fixture_ref = f"resources/fixtures/{family}.json"
    hidden_ref = f"resources/hidden-keys/{family}.json"
    grader_ref = f"resources/graders/{family}.yaml"
    prompt = (
        f"Evaluate the {family} fixture. Return only the required structured result.\n".encode()
    )
    fixture = _json(
        {
            "case_id": f"{family}-smoke-v1",
            "family": family,
            "schema": "zekam-benchmark-fixture/v1",
            "synthetic": True,
            "version": 1,
        }
    )
    hidden = _json(
        {
            "expected_marker": f"{family}-accepted",
            "family": family,
            "schema": "zekam-benchmark-hidden-key/v1",
            "version": 1,
        }
    )
    grader = _yaml(
        {
            "schema": "zekam-benchmark-grader/v1",
            "grader_id": f"{family}-grader",
            "version": 1,
            "family": family,
            "evaluation_class": evaluation_class,
            "dimensions": list(DIMENSIONS[family]),
            "hidden_key_model_visible": False,
        }
    )
    blobs = {
        prompt_ref: prompt,
        fixture_ref: fixture,
        hidden_ref: hidden,
        grader_ref: grader,
    }
    task = _yaml(
        {
            "schema": "zekam-benchmark-task/v1",
            "task_id": f"{family}-smoke",
            "version": 1,
            "family": family,
            "evaluation_class": evaluation_class,
            "workload": family,
            "modality": "text",
            "prompt_ref": prompt_ref,
            "fixture_refs": [fixture_ref],
            "hidden_key_ref": hidden_ref,
            "grader_ref": grader_ref,
            "required_tools": [],
            "forbidden_effects": ["network", "filesystem-write"],
            "data_classification": "public",
            "repetitions": 5,
            "timeout_seconds": 120,
            "max_input_tokens": None,
            "max_output_tokens": None,
            "scoring_dimensions": list(DIMENSIONS[family]),
            "pass_thresholds": dict.fromkeys(DIMENSIONS[family], 0.8),
            "resource_digests": {name: _digest(body) for name, body in sorted(blobs.items())},
        }
    )
    blobs[f"suites/{family}/task.yaml"] = task
    return blobs


def expected() -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    for target in TARGETS:
        for family in FAMILIES:
            for relative, payload in _resources(family).items():
                files[target / relative] = payload
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    failures: list[str] = []
    for path, payload in expected().items():
        if args.check:
            if not path.is_file() or path.read_bytes() != payload:
                failures.append(str(path.relative_to(ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    if failures:
        raise SystemExit("benchmark catalog drift: " + ", ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
