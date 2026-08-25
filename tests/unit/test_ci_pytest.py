from __future__ import annotations

from dataclasses import dataclass

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


def test_failure_annotation_only_exposes_sanitized_nodeid_and_phase(
    capsys: pytest.CaptureFixture[str],
) -> None:
    GitHubFailureAnnotations().pytest_runtest_logreport(
        _Report(
            failed=True,
            nodeid="tests/unit/test_ornek.py::test_satir[param\nsecret]",
            when="call",
            location=("tests/unit/test_ornek.py", 11, "test_satir"),
        )
    )

    output = capsys.readouterr().out
    assert output == (
        "::error file=tests/unit/test_ornek.py,line=12,title=pytest failure::"
        "tests/unit/test_ornek.py::test_satir[param secret] [call]\n"
    )
    assert "cok-gizli" not in output


def test_success_report_does_not_emit_annotation(capsys: pytest.CaptureFixture[str]) -> None:
    GitHubFailureAnnotations().pytest_runtest_logreport(
        _Report(
            failed=False,
            nodeid="tests/unit/test_ornek.py::test_ok",
            when="call",
            location=("tests/unit/test_ornek.py", 1, "test_ok"),
        )
    )

    assert capsys.readouterr().out == ""


def test_collection_failure_annotation_has_no_exception_payload(
    capsys: pytest.CaptureFixture[str],
) -> None:
    GitHubFailureAnnotations().pytest_collectreport(
        _Report(
            failed=True,
            nodeid="tests/unit/test_bozuk.py",
            when="collect",
            location=("tests/unit/test_bozuk.py", 0, ""),
        )
    )

    output = capsys.readouterr().out
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
    capsys: pytest.CaptureFixture[str],
) -> None:
    GitHubFailureAnnotations().pytest_runtest_logreport(
        _Report(
            failed=True,
            nodeid="tests/unit/test_spoof.py::test_fail",
            when="call",
            location=("C:\\repo\\x.py,line=1,title=spoof%0Ainjected", 8, "test_fail"),
        )
    )

    output = capsys.readouterr().out
    assert output.startswith(
        "::error file=C%3A\\repo\\x.py%2Cline=1%2Ctitle=spoof%250Ainjected,line=9,"
    )
    assert output.count("title=") == 2
    assert ",title=spoof" not in output
