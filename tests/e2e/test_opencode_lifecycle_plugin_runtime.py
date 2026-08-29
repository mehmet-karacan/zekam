"""Generated OpenCode lifecycle pluginini gercek Bun runtime'inda dogrula."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
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


def _native_opencode() -> Path:
    configured = os.environ.get("OPENCODE_EXECUTABLE")
    found = configured or shutil.which("opencode")
    if found is None:
        pytest.skip("OpenCode runtime bulunamadi")
    candidate = Path(found)
    if candidate.suffix.casefold() in {".bat", ".cmd", ".ps1"}:
        candidate = candidate.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    if not candidate.is_file():
        pytest.fail(f"OpenCode native executable bulunamadi: {candidate}")
    return candidate.resolve()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(url: str, *, method: str = "GET", body: dict[str, object] | None = None) -> object:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def test_real_opencode_process_loads_plugin_and_emits_provider_free_lifecycle(
    tmp_path: Path,
) -> None:
    bun = _bun_executable()
    executable = _native_opencode()
    user_home = tmp_path / "user"
    config_root = user_home / ".config" / "opencode"
    plugin_root = config_root / "plugins"
    project = tmp_path / "project"
    plugin_root.mkdir(parents=True)
    project.mkdir()
    model_cache = Path.home() / ".cache" / "opencode" / "models.json"
    if not model_cache.is_file():
        pytest.fail("OpenCode provider-free model metadata cache bulunamadi")
    isolated_cache = user_home / ".cache" / "opencode"
    isolated_cache.mkdir(parents=True)
    shutil.copy2(model_cache, isolated_cache / "models.json")
    installed_config = Path.home() / ".config" / "opencode"
    installed_modules = installed_config / "node_modules"
    installed_plugin = installed_modules / "@opencode-ai" / "plugin"
    if not installed_plugin.is_dir() or not (installed_config / "package.json").is_file():
        pytest.fail("OpenCode provider-free plugin runtime dependency bulunamadi")
    shutil.copytree(installed_modules, config_root / "node_modules")
    shutil.copy2(installed_config / "package.json", config_root / "package.json")
    if (installed_config / "package-lock.json").is_file():
        shutil.copy2(installed_config / "package-lock.json", config_root / "package-lock.json")
    (config_root / "opencode.json").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "plugin": ["./plugins/zekam-lifecycle.js"],
            }
        ),
        encoding="utf-8",
    )
    (plugin_root / "zekam-lifecycle.js").write_text(
        opencode_template_bundle()["plugins/zekam-lifecycle.js"], encoding="utf-8"
    )
    capture = tmp_path / "zekam-calls.jsonl"
    fake_source = tmp_path / "fake-zekam-capture.js"
    fake_source.write_text(
        'import { appendFileSync } from "node:fs"\n'
        'appendFileSync(Bun.env.ZEKAM_CAPTURE, JSON.stringify(Bun.argv.slice(2)) + "\\n")\n',
        encoding="utf-8",
    )
    fake_executable = tmp_path / (
        "fake-zekam-capture.exe" if os.name == "nt" else "fake-zekam-capture"
    )
    subprocess.run(
        [bun, "build", "--compile", str(fake_source), "--outfile", str(fake_executable)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    environment = dict(os.environ)
    for key in tuple(environment):
        if any(marker in key.upper() for marker in ("API_KEY", "AUTH_TOKEN", "SECRET", "PASSWORD")):
            environment.pop(key, None)
    environment.update(
        {
            "USERPROFILE": str(user_home),
            "HOME": str(user_home),
            "XDG_CONFIG_HOME": str(user_home / ".config"),
            "XDG_CACHE_HOME": str(user_home / ".cache"),
            "ZEKAM_HOME": str(tmp_path / "zekam-home"),
            "ZEKAM_EXECUTABLE": str(fake_executable),
            "ZEKAM_CAPTURE": str(capture),
            "NO_PROXY": "127.0.0.1,localhost",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "NO_COLOR": "1",
        }
    )
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment[key] = "http://127.0.0.1:9"
    port = _free_loopback_port()
    server = subprocess.Popen(
        [
            str(executable),
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "ERROR",
        ],
        cwd=project,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while True:
            if server.poll() is not None:
                stdout, stderr = server.communicate(timeout=2)
                pytest.fail(f"OpenCode server erken kapandi: {stdout[-500:]} {stderr[-500:]}")
            try:
                health = _request(f"{base}/global/health")
                if isinstance(health, dict) and health.get("healthy") is True:
                    break
            except (OSError, urllib.error.URLError):
                pass
            if time.monotonic() >= deadline:
                pytest.fail("OpenCode loopback server zamaninda hazir olmadi")
            time.sleep(0.1)
        directory = urllib.parse.quote(str(project), safe="")
        session = _request(
            f"{base}/session?directory={directory}",
            method="POST",
            body={"title": "Zekam provider-free lifecycle harness"},
        )
        assert isinstance(session, dict)
        session_id = str(session["id"])
        assert (
            _request(f"{base}/session/{session_id}?directory={directory}", method="DELETE") is True
        )
        deadline = time.monotonic() + 10
        calls: list[list[str]] = []
        while time.monotonic() < deadline:
            if capture.is_file():
                calls = [
                    json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()
                ]
                event_types = {call[call.index("--type") + 1] for call in calls if "--type" in call}
                if {"session.created", "session.deleted"} <= event_types:
                    break
            time.sleep(0.1)
        event_types = {call[call.index("--type") + 1] for call in calls if "--type" in call}
        assert {"session.created", "session.deleted"} <= event_types
        assert all(call[:2] == ["opencode", "event"] for call in calls)
        assert all("provider-free lifecycle harness" not in " ".join(call) for call in calls)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


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
