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
_MAX_ANNOTATIONS = 100
_MAX_PAYLOAD_BYTES = 64 * 1024
_SUMMARY_RESERVE_BYTES = 256
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
        self._commands: list[bytes] = []
        self._payload_bytes = 0
        self._omitted = 0

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
        encoded = command.encode("utf-8", errors="replace")
        if (
            len(self._commands) >= _MAX_ANNOTATIONS
            or self._payload_bytes + len(encoded) > _MAX_PAYLOAD_BYTES - _SUMMARY_RESERVE_BYTES
        ):
            self._omitted += 1
            return
        self._commands.append(encoded)
        self._payload_bytes += len(encoded)

    def flush(self) -> None:
        """Pytest capture tamamen kapandiktan sonra runner pipe'ina yaz."""

        if not self._commands and not self._omitted:
            return
        payload = b"".join(self._commands)
        if self._omitted:
            payload += (
                f"::warning title=pytest failures omitted::{self._omitted} "
                "additional failures omitted by bounded reporter\n"
            ).encode("ascii")
        self._commands.clear()
        self._payload_bytes = 0
        self._omitted = 0
        remaining = memoryview(payload)
        while remaining:
            written = os.write(self._output_fd, remaining)
            if written <= 0:
                raise RuntimeError("CI annotation pipe write failed")
            remaining = remaining[written:]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    reporter = GitHubFailureAnnotations()
    exit_code = int(pytest.main(args, plugins=[reporter]))
    reporter.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
