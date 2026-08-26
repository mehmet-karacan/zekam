"""Generated OpenCode lifecycle pluginini gercek Bun runtime'inda dogrula."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zekam.application.opencode_agent_bootstrap import opencode_template_bundle

pytestmark = pytest.mark.e2e


def _bun_executable() -> str:
    configured = os.environ.get("BUN_EXECUTABLE")
    executable = configured or shutil.which("bun")
    if executable is None:
        pytest.skip("Bun runtime bulunamadi")
    return executable


def _write_runtime_files(root: Path) -> tuple[Path, Path]:
    plugin_path = root / "zekam-lifecycle.js"
    plugin_path.write_text(
        opencode_template_bundle()["plugins/zekam-lifecycle.js"], encoding="utf-8"
    )
    package_root = root / "node_modules" / "@opencode-ai" / "plugin"
    package_root.mkdir(parents=True)
    (package_root / "package.json").write_text(
        json.dumps({"name": "@opencode-ai/plugin", "type": "module", "exports": "./index.js"}),
        encoding="utf-8",
    )
    (package_root / "index.js").write_text(
        """export const tool = (value) => value
tool.schema = { string: () => ({ max: () => ({}) }) }
""",
        encoding="utf-8",
    )
    fake_source = root / "fake-zekam.js"
    fake_source.write_text(
        """const code = Number(Bun.env.FAKE_ZEKAM_EXIT_CODE ?? "0")
process.exit(Number.isInteger(code) ? code : 1)
""",
        encoding="utf-8",
    )
    fake_executable = root / ("fake-zekam.exe" if os.name == "nt" else "fake-zekam")
    subprocess.run(
        [
            _bun_executable(),
            "build",
            "--compile",
            str(fake_source),
            "--outfile",
            str(fake_executable),
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    runner = root / "runner.js"
    runner.write_text(
        """import { ZekamLifecycle } from "./zekam-lifecycle.js"

const count = Number(Bun.env.EMIT_COUNT ?? "1")
const prefix = Bun.env.SESSION_PREFIX ?? "runtime"
const plugin = await ZekamLifecycle({ directory: process.cwd() })
const results = await Promise.allSettled(
  Array.from({ length: count }, (_, index) => plugin.event({
    event: { type: "session.created", properties: { id: `${prefix}-${index}` } },
  })),
)
if (results.some((result) => result.status === "rejected")) process.exit(2)
if (Bun.env.RUN_PRECOMPACT === "1") {
  try {
    await plugin["experimental.session.compacting"]({ sessionID: `${prefix}-compact` })
    process.exit(3)
  } catch {
    process.exit(17)
  }
}
""",
        encoding="utf-8",
    )
    return runner, fake_executable


def _environment(home: Path, fake_executable: Path, **extra: str) -> dict[str, str]:
    return {
        **os.environ,
        "ZEKAM_HOME": str(home),
        "ZEKAM_EXECUTABLE": str(fake_executable),
        **extra,
    }


def _spool(home: Path) -> Path:
    return home / "global" / "runtime" / "opencode-plugin-spool"


def _run(
    bun: str,
    runner: Path,
    *,
    home: Path,
    fake_executable: Path,
    **extra: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bun, str(runner)],
        cwd=runner.parent,
        env=_environment(home, fake_executable, **extra),
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


def test_single_flight_drains_concurrent_events(tmp_path: Path) -> None:
    bun = _bun_executable()
    runner, fake_executable = _write_runtime_files(tmp_path)
    home = tmp_path / "home"

    result = _run(
        bun,
        runner,
        home=home,
        fake_executable=fake_executable,
        EMIT_COUNT="100",
    )

    assert result.returncode == 0, result.stderr
    spool = _spool(home)
    assert list(spool.glob("*.json")) == []
    assert list(spool.glob(".drain.candidate.*")) == []
    assert not (spool / ".drain.lock").exists()


def test_two_bun_processes_share_one_windows_safe_lock(tmp_path: Path) -> None:
    bun = _bun_executable()
    runner, fake_executable = _write_runtime_files(tmp_path)
    home = tmp_path / "home"
    environment_one = _environment(home, fake_executable, EMIT_COUNT="50", SESSION_PREFIX="one")
    environment_two = _environment(home, fake_executable, EMIT_COUNT="50", SESSION_PREFIX="two")

    first = subprocess.Popen(
        [bun, str(runner)],
        cwd=runner.parent,
        env=environment_one,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        [bun, str(runner)],
        cwd=runner.parent,
        env=environment_two,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_output, first_error = first.communicate(timeout=90)
    second_output, second_error = second.communicate(timeout=90)

    assert first.returncode == 0, first_output + first_error
    assert second.returncode == 0, second_output + second_error
    spool = _spool(home)
    assert list(spool.glob("*.json")) == []
    assert list(spool.glob(".drain.candidate.*")) == []
    assert not (spool / ".drain.lock").exists()


def test_retry_is_durable_and_precompact_remains_fail_closed(tmp_path: Path) -> None:
    bun = _bun_executable()
    runner, fake_executable = _write_runtime_files(tmp_path)
    home = tmp_path / "home"

    deferred = _run(
        bun,
        runner,
        home=home,
        fake_executable=fake_executable,
        FAKE_ZEKAM_EXIT_CODE="1",
    )
    queued = list(_spool(home).glob("*.json"))
    assert deferred.returncode == 0, deferred.stderr
    assert len(queued) == 1
    assert json.loads(queued[0].read_text(encoding="utf-8"))["attempts"] == 1

    fail_closed_home = tmp_path / "precompact-home"
    fail_closed = _run(
        bun,
        runner,
        home=fail_closed_home,
        fake_executable=fake_executable,
        FAKE_ZEKAM_EXIT_CODE="1",
        RUN_PRECOMPACT="1",
    )
    assert fail_closed.returncode == 17


def test_live_owner_is_preserved_and_dead_owner_is_quarantined(tmp_path: Path) -> None:
    bun = _bun_executable()
    runner, fake_executable = _write_runtime_files(tmp_path)
    live_home = tmp_path / "live-home"
    live_lock = _spool(live_home) / ".drain.lock"
    live_lock.mkdir(parents=True)
    now = datetime.now(UTC)
    live_owner = {
        "schema": "zekam-opencode-drain-owner/v2",
        "pid": os.getpid(),
        "device": "runtime-test",
        "ownerToken": "live-owner-token-1234",
        "startedAt": now.isoformat(),
        "expiresAt": (now - timedelta(minutes=1)).isoformat(),
    }
    (live_lock / "owner.json").write_text(json.dumps(live_owner), encoding="utf-8")

    contended = _run(bun, runner, home=live_home, fake_executable=fake_executable)
    assert contended.returncode == 0, contended.stderr
    assert json.loads((live_lock / "owner.json").read_text(encoding="utf-8")) == live_owner
    assert len(list(_spool(live_home).glob("*.json"))) == 1
    assert list(_spool(live_home).glob(".drain.candidate.*")) == []

    dead_home = tmp_path / "dead-home"
    dead_lock = _spool(dead_home) / ".drain.lock"
    dead_lock.mkdir(parents=True)
    dead_owner = {
        **live_owner,
        "pid": 2_147_483_647,
        "ownerToken": "dead-owner-token-1234",
        "expiresAt": (now - timedelta(minutes=1)).isoformat(),
    }
    (dead_lock / "owner.json").write_text(json.dumps(dead_owner), encoding="utf-8")

    recovered = _run(bun, runner, home=dead_home, fake_executable=fake_executable)
    assert recovered.returncode == 0, recovered.stderr
    spool = _spool(dead_home)
    assert list(spool.glob("*.json")) == []
    assert not (spool / ".drain.lock").exists()
    assert len(list((spool / "quarantine").glob(".drain.lock.*"))) == 1
