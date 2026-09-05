"""Historical Windows Claude probe and platform-independent direct Zekam hooks.

Direct CLI delivery below does not claim Claude emitted or installed these hooks.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from zekam.application.client_hook_bootstrap import (
    apply_client_hook_bootstrap,
    plan_client_hook_bootstrap,
)
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(
    os.name != "nt", reason="Historical Windows Claude 2.1.224 runtime/settings probe"
)
def test_historical_windows_claude_version_and_isolated_settings(tmp_path: Path) -> None:
    executable = shutil.which("claude")
    if executable is None:
        pytest.skip("Claude Code kurulu degil")
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=False, timeout=15
    )
    assert version.returncode == 0
    assert version.stdout.strip() == "2.1.224 (Claude Code)"

    user_home = tmp_path / "user"
    user_home.mkdir()
    apply_client_hook_bootstrap(
        plan_client_hook_bootstrap(user_home=user_home, python_executable=Path(sys.executable))
    )
    settings = json.loads((user_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {
        "SessionStart",
        "PreCompact",
        "PostCompact",
        "Stop",
        "SessionEnd",
    }
    isolated_probe = subprocess.run(
        [
            executable,
            "--settings",
            str(user_home / ".claude" / "settings.json"),
            "--setting-sources",
            "user",
            "--version",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(user_home / ".claude")},
    )
    assert isolated_probe.returncode == 0
    assert isolated_probe.stdout.strip() == "2.1.224 (Claude Code)"


def test_direct_zekam_cli_hook_delivery_preserves_isolated_spool_and_privacy(
    tmp_path: Path,
) -> None:
    """Deliver five explicit fixtures to Zekam, without invoking the Claude binary."""
    zekam_home = tmp_path / "zekam-home"
    environment = dict(os.environ)
    environment["ZEKAM_HOME"] = str(zekam_home)
    source_root = Path(__file__).resolve().parents[2] / "src"
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root)
        if not existing_pythonpath
        else str(source_root) + os.pathsep + existing_pythonpath
    )
    session_id = "00000000-0000-8000-8000-000000000001"
    fixtures = (
        ("SessionStart", {"source": "startup", "permission_mode": "default"}),
        ("PreCompact", {"trigger": "auto"}),
        ("PostCompact", {"trigger": "auto"}),
        ("Stop", {"stop_hook_active": False, "permission_mode": "default"}),
        ("SessionEnd", {"reason": "other"}),
    )
    for event, extra in fixtures:
        payload = json.dumps(
            {
                "session_id": session_id,
                "hook_event_name": event,
                "transcript_path": "C:/must-not-persist.jsonl",
                "prompt": "must-not-persist",
                **extra,
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "zekam.interfaces.cli.client",
                "hook",
                "--client",
                "claude-code",
                "--client-version",
                "2.1.224",
            ],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env=environment,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout) == {}

    pending = ClientLifecycleSpool(zekam_home, client_id="claude-code").pending(limit=10)
    assert [item.external_event_type for item in pending] == [item[0] for item in fixtures]
    rendered = json.dumps([item.as_dict() for item in pending])
    assert "must-not-persist" not in rendered
