from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from scripts import ci_pytest
from scripts.ci_pytest import GitHubFailureAnnotations

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Report:
    failed: bool
    nodeid: str
    when: str
    location: tuple[str, int, str]
    longrepr: str = "ZEKAM_DATABASE_PASSWORD=cok-gizli"


def _reporter() -> GitHubFailureAnnotations:
    return GitHubFailureAnnotations(output_fd=sys.stdout.fileno())


def test_failure_annotation_only_exposes_sanitized_nodeid_and_phase(
    capfd: pytest.CaptureFixture[str],
) -> None:
    _reporter().pytest_runtest_logreport(
        _Report(
            failed=True,
            nodeid="tests/unit/test_ornek.py::test_satir[param\nsecret]",
            when="call",
            location=("tests/unit/test_ornek.py", 11, "test_satir"),
        )
    )

    output = capfd.readouterr().out
    assert output == (
        "::error file=tests/unit/test_ornek.py,line=12,title=pytest failure::"
        "tests/unit/test_ornek.py::test_satir[param secret] [call]\n"
    )
    assert "cok-gizli" not in output


def test_success_report_does_not_emit_annotation(capfd: pytest.CaptureFixture[str]) -> None:
    _reporter().pytest_runtest_logreport(
        _Report(
            failed=False,
            nodeid="tests/unit/test_ornek.py::test_ok",
            when="call",
            location=("tests/unit/test_ornek.py", 1, "test_ok"),
        )
    )

    assert capfd.readouterr().out == ""


def test_collection_failure_annotation_has_no_exception_payload(
    capfd: pytest.CaptureFixture[str],
) -> None:
    _reporter().pytest_collectreport(
        _Report(
            failed=True,
            nodeid="tests/unit/test_bozuk.py",
            when="collect",
            location=("tests/unit/test_bozuk.py", 0, ""),
        )
    )

    output = capfd.readouterr().out
    assert output == "::error title=pytest failure::tests/unit/test_bozuk.py [collect]\n"
    assert "cok-gizli" not in output


def test_main_propagates_pytest_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_main(args: list[str], *, plugins: list[object]) -> int:
        observed["args"] = args
        observed["plugins"] = plugins
        return 7

    monkeypatch.setattr(ci_pytest.pytest, "main", fake_main)

    assert ci_pytest.main(["-m", "not postgres"]) == 7
    assert observed["args"] == ["-m", "not postgres"]
    assert isinstance(observed["plugins"][0], GitHubFailureAnnotations)  # type: ignore[index]


def test_annotation_property_escapes_metadata_injection(
    capfd: pytest.CaptureFixture[str],
) -> None:
    _reporter().pytest_runtest_logreport(
        _Report(
            failed=True,
            nodeid="tests/unit/test_spoof.py::test_fail",
            when="call",
            location=("C:\\repo\\x.py,line=1,title=spoof%0Ainjected", 8, "test_fail"),
        )
    )

    output = capfd.readouterr().out
    assert output.startswith(
        "::error file=C%3A\\repo\\x.py%2Cline=1%2Ctitle=spoof%250Ainjected,line=9,"
    )
    assert output.count("title=") == 2
    assert ",title=spoof" not in output


def test_wrapper_annotation_bypasses_nested_pytest_capture(tmp_path: Path) -> None:
    failing_test = tmp_path / "test_nested_failure.py"
    failing_test.write_text("def test_nested_failure():\n    assert False\n", encoding="utf-8")
    wrapper = Path(ci_pytest.__file__).resolve()

    result = subprocess.run(
        [sys.executable, str(wrapper), str(failing_test), "-q"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "::error file=" in result.stdout
    assert "test_nested_failure.py::test_nested_failure [call]" in result.stdout
