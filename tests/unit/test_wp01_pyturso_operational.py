from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.suites.wp01_operational_fixture import WorkloadSize
from benchmarks.suites.wp01_pyturso_operational import run


def test_pyturso_run_rejects_existing_root_before_import(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir()
    with pytest.raises(FileExistsError):
        run(
            root=root,
            size=WorkloadSize(project_rows=1, work_rows=1, event_rows=1, producer_rows=1),
        )


def test_pyturso_run_rejects_invalid_workload_before_import(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run(
            root=tmp_path / "run",
            size=WorkloadSize(project_rows=1, work_rows=2, event_rows=1, producer_rows=1),
        )
