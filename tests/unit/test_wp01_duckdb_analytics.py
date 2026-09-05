from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.suites.wp01_duckdb_analytics import _write_raw_events, run


@pytest.mark.parametrize("count", [0, -1, True])
def test_raw_event_writer_rejects_invalid_count(tmp_path: Path, count: int) -> None:
    with pytest.raises(ValueError):
        _write_raw_events(tmp_path / "events.jsonl", count)


def test_analytics_run_rejects_existing_root_before_import(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir()
    with pytest.raises(FileExistsError):
        run(root=root, row_count=1)
