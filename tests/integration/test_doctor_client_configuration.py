"""Client config -> composition -> doctor entegrasyonu."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.composition import build_context, build_doctor_checks
from zekam.application.config import CONFIG_SCHEMA, USER_CONFIG_FILE
from zekam.application.diagnostics import CheckStatus

pytestmark = pytest.mark.integration


def test_registered_local_client_reaches_doctor(tmp_path: Path) -> None:
    executable = tmp_path / "opencode.exe"
    executable.write_bytes(b"MZ")
    (tmp_path / USER_CONFIG_FILE).write_text(
        f"schema: {CONFIG_SCHEMA}\nclients:\n  - name: opencode\n    executable: '{executable}'\n",
        encoding="utf-8",
    )

    context = build_context(home=tmp_path, environ={})
    clients_check = next(
        check for check in build_doctor_checks(context) if check.check_id == "runtime.clients"
    )

    result = clients_check.run()

    assert result.status is CheckStatus.PASSED
    assert result.evidence == {
        "configured": 1,
        "clients": ["opencode"],
        "missing": [],
    }
