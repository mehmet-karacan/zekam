from __future__ import annotations

from pathlib import Path

from benchmarks.suites.wp01_operational_fixture import WorkloadSize
from benchmarks.suites.wp01_platform import current_acceptance_platform
from benchmarks.suites.wp01_sqlite_operational import run_sqlite_operational_bakeoff

from zekam.application.technology_bakeoff import canonical_json_digest


def test_sqlite_operational_bakeoff_exercises_failure_and_recovery(tmp_path: Path) -> None:
    result = run_sqlite_operational_bakeoff(
        root=tmp_path / "Windows kabul ölçümü" / "run",
        size=WorkloadSize(
            project_rows=40,
            work_rows=40,
            event_rows=200,
            producer_rows=80,
        ),
    )

    assert result["local_pass"] is True
    assert result["row_counts"] == {
        "project": 40,
        "work_item": 40,
        "work_event": 280,
        "idempotency_claim": 1,
    }
    assert result["probes"]["network_attempts"] == 0
    assert result["probes"]["uncommitted_process_kill"]["passed"] is True
    assert result["probes"]["snapshot_process_kill"]["passed"] is True
    assert result["probes"]["disk_full"]["passed"] is True
    assert result["probes"]["read_only_directory"]["passed"] is True
    assert result["probes"]["schema_drift"]["passed"] is True
    assert result["probes"]["corruption"]["passed"] is True
    assert result["probes"]["backup_restore"]["passed"] is True
    current_platform = current_acceptance_platform()
    assert result["executed_platforms"] == [current_platform]
    assert result["hard_gates"]["windows_x64"] is (current_platform == "windows-x64")
    if current_platform == "windows-x64":
        assert result["runtime"]["concurrency_profile"] in {
            "multi-connection-wal",
            "single-writer-rollback-journal",
        }
        assert result["probes"]["windows_file_lock"]["passed"] is True
        assert result["probes"]["atomic_replace"]["passed"] is True
    artifact_digest = result.pop("artifact_digest")
    assert artifact_digest == canonical_json_digest(result)
