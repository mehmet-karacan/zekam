"""Pytest'i calistir ve GitHub Actions'a yalniz sanitize failure kimligi yaz.

Ham traceback, fixture degeri veya exception mesaji workflow annotation'ina
tasınmaz. Normal pytest ciktisi job logunda kalir; annotation yalniz exact nodeid
ve pytest fazini gorunur yapar.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

_UNSAFE = re.compile(r"[^A-Za-z0-9_./:\[\](), =+-]")
# Script dogrudan calistirildiginda pytest capture baslamadan once runner pipe'ini
# kopyalar. Hook bu descriptor'a yazar; test stdout capture'ini acmaz.
_ANNOTATION_FD = os.dup((sys.__stdout__ or sys.stdout).fileno())


def _safe(value: object, *, limit: int = 500) -> str:
    return _UNSAFE.sub("?", str(value).replace("\r", " ").replace("\n", " "))[:limit]


def _property(value: object, *, limit: int = 500) -> str:
    """GitHub workflow-command property degerini injection'a kapat."""

    text = str(value)[:limit]
    return (
        text.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


class GitHubFailureAnnotations:
    """Failed test/collection kimligini payload siz annotation'a cevirir."""

    def __init__(self, *, output_fd: int = _ANNOTATION_FD) -> None:
        self._output_fd = output_fd

    def pytest_runtest_logreport(self, report: Any) -> None:
        if not report.failed:
            return
        path, line_index, _ = report.location
        self._emit(
            nodeid=report.nodeid,
            phase=report.when,
            path=path,
            line=int(line_index) + 1,
        )

    def pytest_collectreport(self, report: Any) -> None:
        if not report.failed:
            return
        self._emit(nodeid=report.nodeid, phase="collect")

    def _emit(
        self,
        *,
        nodeid: object,
        phase: object,
        path: object | None = None,
        line: int | None = None,
    ) -> None:
        metadata = " "
        if path is not None and line is not None:
            metadata = f" file={_property(path)},line={line},"
        command = (
            f"::error{metadata}title=pytest failure::{_safe(nodeid)} [{_safe(phase, limit=32)}]\n"
        )
        os.write(self._output_fd, command.encode("utf-8", errors="replace"))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    return int(pytest.main(args, plugins=[GitHubFailureAnnotations()]))


if __name__ == "__main__":
    raise SystemExit(main())
