from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.suites.wp01_knowledge_bakeoff import (
    IndexRecord,
    _percentile,
    _record_stream,
    run_bakeoff,
)


def _chunk() -> dict[str, object]:
    import base64
    import struct

    vector = [0.0] * 1_024
    vector[0] = 1.0
    return {
        "chunk_id": "c1",
        "project_id": "zekam",
        "source_path": "docs/a.md",
        "source_digest": "sha256:" + "a" * 64,
        "text": "content",
        "vector_b64": base64.b64encode(struct.pack("<1024f", *vector)).decode("ascii"),
    }


def test_record_stream_is_exact_count_and_deterministic() -> None:
    rows = list(_record_stream([_chunk()], 3))
    assert [row.record_id for row in rows] == ["r000000000", "r000000001", "r000000002"]
    assert all(isinstance(row, IndexRecord) for row in rows)


@pytest.mark.parametrize(
    ("values", "quantile", "expected"),
    [([1.0], 0.95, 1.0), ([1.0, 2.0, 3.0, 4.0], 0.50, 2.0), ([1.0, 2.0], 0.95, 2.0)],
)
def test_percentile_uses_bounded_nearest_rank(
    values: list[float], quantile: float, expected: float
) -> None:
    assert _percentile(values, quantile) == expected


@pytest.mark.parametrize("candidate", ["", "qdrant", "LanceDB"])
def test_bakeoff_rejects_unknown_candidate(candidate: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        run_bakeoff(
            candidate=candidate,
            corpus_path=tmp_path / "missing.json",
            root=tmp_path / "run",
            target_count=100,
            rebuild=False,
        )


def test_bakeoff_rejects_small_or_boolean_target(tmp_path: Path) -> None:
    for target in (0, 99, True):
        with pytest.raises(ValueError, match="target_count"):
            run_bakeoff(
                candidate="lancedb",
                corpus_path=tmp_path / "missing.json",
                root=tmp_path / f"run-{target}",
                target_count=target,
                rebuild=False,
            )
