"""Generated OpenCode lifecycle pluginini gercek Bun runtime'inda dogrula."""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zekam.application.opencode_agent_bootstrap import opencode_template_bundle
from zekam.domain.errors import ZekamError
from zekam.infrastructure.clients.opencode_executable import resolve_opencode_executable

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
    (root / ".ai").mkdir(exist_ok=True)
    (root / ".ai" / "repository-context.json").write_text(
        json.dumps({"schema": "zekam-repository-context/v2", "project": "zekam"}),
        encoding="utf-8",
    )
    (root / "PROJE_MANIFESTI.yaml").write_text("project:\n  slug: zekam\n", encoding="utf-8")
    fake_source = root / "fake-zekam.js"
    fake_source.write_text(
        """import { appendFileSync } from "node:fs"

if (Bun.env.ZEKAM_CAPTURE) {
  appendFileSync(Bun.env.ZEKAM_CAPTURE, JSON.stringify(Bun.argv.slice(2)) + "\\n")
}
if (Bun.argv.includes("resume")) {
  console.log('ZEKAM_RESUME_PACKET_V1\\n{"schema":"zekam-resume-packet/v1","read_only":true,"grants_authority":false}')
  process.exit(0)
}
if (Bun.argv.includes("native-binding")) {
  const query = JSON.parse(await Bun.stdin.text())
  if (Bun.env.NATIVE_BINDING_MODE === "reject") process.exit(70)
  console.log(JSON.stringify({
    schema: "zekam-native-skill-binding/v1", client_id: "opencode",
    skill_id: query.skill_id, relative_path: query.relative_path,
    instruction_digest: query.instruction_digest,
    revision_digest: "sha256:" + "1".repeat(64), package_digest: "sha256:" + "2".repeat(64),
    export_job_id: "11111111-2222-4333-8444-555555555555",
    grants_authority: false, quality_verified: false, invocation_observed: false, provider_calls: 0,
  }))
  process.exit(0)
}
const code = Number(Bun.env.FAKE_ZEKAM_EXIT_CODE ?? "0")
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
        """import pluginV2, { ZekamLifecycle } from "./zekam-lifecycle.js"
import { join, dirname } from "node:path"

if (Bun.env.RUN_V2_SETUP === "1") {
  const pending = []
  const waiters = []
  const registrations = []
  const callbacks = {}
  const push = (event) => {
    const waiter = waiters.shift()
    if (waiter) waiter(event)
    else pending.push(event)
  }
  const subscribe = () => ({
    async *[Symbol.asyncIterator]() {
      while (true) {
        const event = pending.length > 0
          ? pending.shift()
          : await new Promise((resolve) => waiters.push(resolve))
        if (event === null) return
        yield event
      }
    },
  })
  const skillPath = join(process.cwd(), ".agents", "skills", "native-example", "SKILL.md")
  const skillBody = "Private fixture instruction sentinel."
  await pluginV2.setup({
    location: { directory: process.cwd() },
    event: { subscribe },
    skill: { list: async () => [{ id: "native-example", name: "native-example", path: skillPath, content: skillBody }] },
    session: { hook: async (name, callback) => {
      registrations.push(`session:${name}`)
      callbacks[`session:${name}`] = callback
    } },
    tool: { hook: async (name, callback) => {
      registrations.push(`tool:${name}`)
      callbacks[`tool:${name}`] = callback
    } },
  })
  await Bun.sleep(50)
  const contextEvent = { sessionID: "v2-context", system: [] }
  const compactionEvent = { sessionID: "v2-compaction", context: [] }
  await callbacks["session:context"](contextEvent)
  await callbacks["session:compaction"](compactionEvent)
  await callbacks["tool:execute.before"]({
    sessionID: "v2-tool", callID: "v2-call", tool: "read", input: { filePath: "README.md" },
  })
  const after = {
    sessionID: "v2-tool", callID: "v2-call", tool: "read",
    result: { title: "secret-result-sentinel", output: "private-result-sentinel", metadata: {} },
    error: { name: "private-error-name-sentinel", code: "private-error-code-sentinel", message: "private-error-message-sentinel" },
  }
  if (Bun.env.TOOL_AFTER_STATUS !== "missing") after.status = Bun.env.TOOL_AFTER_STATUS ?? "completed"
  await callbacks["tool:execute.after"](after)
  if (Bun.env.RUN_V2_NATIVE_IDS === "1") {
    for (const [id, file] of [["call-one", "first.md"], ["call-two", "second.md"]]) {
      await callbacks["tool:execute.before"]({sessionID: "parallel", id, tool: "read", input: {filePath: file}})
    }
    for (const id of ["call-one", "call-two"]) {
      await callbacks["tool:execute.after"]({sessionID: "parallel", id, tool: "read", status: "completed"})
    }
  }
  if (Bun.env.RUN_V2_SKILL === "1") {
    const native = { sessionID: "native-skill", id: "native-call-private", messageID: "native-message-private",
      agent: "agent-builder", tool: "skill", input: { id: "native-example" } }
    const modelMode = Bun.env.NATIVE_STEP_MODEL
    if (modelMode) {
      const step = { sessionID: modelMode === "other-session" ? "other-session" : native.sessionID,
        assistantMessageID: modelMode === "other-message" ? "other-message" : native.messageID,
        agent: modelMode === "other-agent" ? "other-agent" : native.agent,
        model: {providerID: "provider", id: "builder", variant: "extra-high"},
        content: "private-step-content-sentinel" }
      if (modelMode === "invalid") step.model.id = "private model with spaces"
      push({ type: "session.step.started", data: step })
      if (modelMode === "conflict") push({ type: "session.step.started", data: {
        ...step, model: {providerID: "provider", id: "different"} } })
      if (modelMode === "model-switch") push({ type: "session.model.selected", data: {
        sessionID: native.sessionID, model: {providerID: "provider", id: "later-default"} } })
      await Bun.sleep(50)
    }
    await callbacks["tool:execute.before"](native)
    const content = `<skill_content name="native-example">\\n# Skill: native-example\\n\\n${skillBody}\\n\\nBase directory for this skill: ${dirname(skillPath)}\\nRelative paths in this skill are relative to this base directory.\\n<skill_files>\\n</skill_files>\\n</skill_content>`
    await callbacks["tool:execute.after"]({ ...native, status: "completed",
      result: {content, output: {name: "native-example", directory: dirname(skillPath),
        output: Bun.env.NATIVE_BINDING_MODE === "renderer-drift" ? "wrong renderer" : content}} })
  }
  push({ type: "session.created", data: { sessionID: "v2-created" } })
  push({ type: "session.deleted", data: { sessionID: "v2-deleted" } })
  await Bun.sleep(250)
  push(null)
  await Bun.sleep(50)
  console.log(JSON.stringify({
    registrations,
    contextSystem: contextEvent.system,
    compactionContext: compactionEvent.context,
  }))
  process.exit(0)
}

const count = Number(Bun.env.EMIT_COUNT ?? "1")
const prefix = Bun.env.SESSION_PREFIX ?? "runtime"
const plugin = await ZekamLifecycle({ directory: process.cwd() })
if (Bun.env.RUN_V1_TOOL_AFTER === "1") {
  const input = { sessionID: "v1-tool", callID: "v1-call", tool: "read" }
  await plugin["tool.execute.before"](input, { args: { filePath: "README.md" } })
  const output = Bun.env.LEGACY_AFTER_OUTPUT === "valid"
    ? { title: "private-title-sentinel", output: "private-output-sentinel", metadata: {} }
    : undefined
  await plugin["tool.execute.after"](input, output)
  process.exit(0)
}
if (Bun.env.EXPECT_INACTIVE === "1") process.exit(Object.keys(plugin).length === 0 ? 0 : 5)
const results = await Promise.allSettled(
  Array.from({ length: count }, (_, index) => plugin.event({
    event: { type: "session.created", properties: { id: `${prefix}-${index}` } },
  })),
)
if (results.some((result) => result.status === "rejected")) process.exit(2)
if (Bun.env.RUN_SYSTEM_TRANSFORM === "1") {
  const output = { system: [] }
  await plugin["experimental.chat.system.transform"](
    { sessionID: `${prefix}-hydrate`, model: {} }, output,
  )
  await plugin["experimental.chat.system.transform"](
    { sessionID: `${prefix}-hydrate`, model: {} }, output,
  )
  console.log(JSON.stringify(output))
}
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


def test_plugin_injects_bounded_resume_packet_once_per_session(tmp_path: Path) -> None:
    runner, fake_executable = _write_runtime_files(tmp_path)
    environment = dict(os.environ)
    environment.update(
        {
            "ZEKAM_EXECUTABLE": str(fake_executable),
            "ZEKAM_HOME": str(tmp_path / "home"),
            "RUN_SYSTEM_TRANSFORM": "1",
        }
    )

    result = subprocess.run(
        [_bun_executable(), str(runner)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout.splitlines()[-1])
    assert len(document["system"]) == 1
    assert document["system"][0].startswith("ZEKAM_RESUME_PACKET_V1")


def test_v2_setup_registers_hooks_and_forwards_lifecycle_events(tmp_path: Path) -> None:
    runner, fake_executable = _write_runtime_files(tmp_path)
    capture = tmp_path / "v2-calls.jsonl"
    result = _run(
        _bun_executable(),
        runner,
        home=tmp_path / "home",
        fake_executable=fake_executable,
        RUN_V2_SETUP="1",
        ZEKAM_CAPTURE=str(capture),
    )

    assert result.returncode == 0, result.stderr
    registration_document = json.loads(result.stdout.splitlines()[-1])
    assert set(registration_document["registrations"]) >= {
        "session:context",
        "session:compaction",
        "tool:execute.before",
        "tool:execute.after",
    }
    assert any(
        isinstance(item, dict) and str(item.get("text", "")).startswith("ZEKAM_RESUME_PACKET_V1")
        for item in registration_document["contextSystem"]
    )
    assert any(
        isinstance(item, dict) and str(item.get("text", "")).startswith("ZEKAM_RESUME_PACKET_V1")
        for item in registration_document["compactionContext"]
    )
    calls = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
    event_types = {call[call.index("--type") + 1] for call in calls if "--type" in call}
    assert {
        "session.created",
        "session.deleted",
        "tool.execute.before",
        "tool.execute.after",
    } <= event_types


@pytest.mark.parametrize(
    "native_status,expected,category",
    [
        ("completed", "completed", None),
        ("error", "failed", "native-tool-error"),
        ("missing", "unknown", "native-tool-status-unknown"),
        ("future-status", "unknown", "native-tool-status-unknown"),
        ("cancelled", "cancelled", "native-tool-cancelled"),
    ],
)
def test_v2_tool_after_uses_native_status_without_error_or_result_text(
    tmp_path: Path,
    native_status: str,
    expected: str,
    category: str | None,
) -> None:
    runner, executable = _write_runtime_files(tmp_path)
    capture = tmp_path / "outcome-calls.jsonl"
    result = _run(
        _bun_executable(),
        runner,
        home=tmp_path / "home",
        fake_executable=executable,
        RUN_V2_SETUP="1",
        ZEKAM_CAPTURE=str(capture),
        TOOL_AFTER_STATUS=native_status,
    )
    assert result.returncode == 0, result.stderr
    raw = capture.read_text(encoding="utf-8")
    calls = [json.loads(line) for line in raw.splitlines()]
    after = [
        call
        for call in calls
        if "--type" in call and call[call.index("--type") + 1] == "tool.execute.after"
    ]
    assert len(after) == 1
    assert after[0][after[0].index("--status") + 1] == expected
    if category is None:
        assert "--error-category" not in after[0]
    else:
        assert after[0][after[0].index("--error-category") + 1] == category
    assert "sentinel" not in raw


@pytest.mark.parametrize("shape,expected", [("valid", "completed"), ("missing", "unknown")])
def test_v1_after_requires_observed_success_result_without_persisting_content(
    tmp_path: Path,
    shape: str,
    expected: str,
) -> None:
    runner, executable = _write_runtime_files(tmp_path)
    capture = tmp_path / "v1-calls.jsonl"
    result = _run(
        _bun_executable(),
        runner,
        home=tmp_path / "home",
        fake_executable=executable,
        RUN_V1_TOOL_AFTER="1",
        LEGACY_AFTER_OUTPUT=shape,
        ZEKAM_CAPTURE=str(capture),
    )
    assert result.returncode == 0, result.stderr
    raw = capture.read_text(encoding="utf-8")
    calls = [json.loads(line) for line in raw.splitlines()]
    after = [
        call
        for call in calls
        if "--type" in call and call[call.index("--type") + 1] == "tool.execute.after"
    ]
    assert len(after) == 1
    assert after[0][after[0].index("--status") + 1] == expected
    assert "sentinel" not in raw


def test_v2_actual_call_ids_keep_overlapping_resources_separate(tmp_path: Path) -> None:
    runner, executable = _write_runtime_files(tmp_path)
    capture = tmp_path / "native-ids.jsonl"
    result = _run(
        _bun_executable(),
        runner,
        home=tmp_path / "home",
        fake_executable=executable,
        RUN_V2_SETUP="1",
        RUN_V2_NATIVE_IDS="1",
        ZEKAM_CAPTURE=str(capture),
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
    observed = [
        call[call.index("--resource") + 1]
        for call in calls
        if "--type" in call
        and call[call.index("--type") + 1] == "tool.execute.after"
        and call[call.index("--session") + 1] == "parallel"
    ]
    assert observed == ["first.md", "second.md"]


@pytest.mark.parametrize("mode", ["bound", "reject", "renderer-drift"])
def test_v2_native_skill_load_attaches_only_resolved_content_free_observation(
    tmp_path: Path,
    mode: str,
) -> None:
    runner, executable = _write_runtime_files(tmp_path)
    capture = tmp_path / "native-skill.jsonl"
    result = _run(
        _bun_executable(),
        runner,
        home=tmp_path / "home",
        fake_executable=executable,
        RUN_V2_SETUP="1",
        RUN_V2_SKILL="1",
        NATIVE_BINDING_MODE=mode,
        ZEKAM_CAPTURE=str(capture),
    )
    assert result.returncode == 0, result.stderr
    raw = capture.read_text(encoding="utf-8")
    calls = [json.loads(line) for line in raw.splitlines()]
    after = [
        call
        for call in calls
        if "--type" in call
        and call[call.index("--type") + 1] == "tool.execute.after"
        and call[call.index("--session") + 1] == "native-skill"
    ]
    assert len(after) == 1
    assert after[0][after[0].index("--status") + 1] == "completed"
    assert "Private fixture instruction" not in raw
    assert "native-call-private" not in raw
    assert "native-message-private" not in raw
    if mode != "bound":
        assert "--task-label" not in after[0]
        return
    assert after[0][after[0].index("--resource") + 1] == ".agents/skills/native-example/SKILL.md"
    label = after[0][after[0].index("--task-label") + 1]
    assert label.startswith("native-skill-load/v1:")
    assert len(label) <= 500
    binding = json.loads(label.removeprefix("native-skill-load/v1:"))
    assert binding == {
        "j": "11111111-2222-4333-8444-555555555555",
        "r": "1" * 64,
        "p": "2" * 64,
        "b": hashlib.sha256(b"Private fixture instruction sentinel.").hexdigest(),
        "c": hashlib.sha256(b"native-call-private").hexdigest(),
        "m": hashlib.sha256(b"native-message-private").hexdigest(),
    }


@pytest.mark.parametrize(
    "mode,known",
    [
        ("exact", True),
        ("model-switch", True),
        ("other-message", False),
        ("other-session", False),
        ("other-agent", False),
        ("conflict", False),
        ("invalid", False),
    ],
)
def test_native_model_identity_comes_only_from_exact_step_message(
    tmp_path: Path, mode: str, known: bool
) -> None:
    runner, executable = _write_runtime_files(tmp_path)
    capture = tmp_path / "native-model.jsonl"
    result = _run(
        _bun_executable(),
        runner,
        home=tmp_path / "home",
        fake_executable=executable,
        RUN_V2_SETUP="1",
        RUN_V2_SKILL="1",
        NATIVE_STEP_MODEL=mode,
        ZEKAM_CAPTURE=str(capture),
    )
    assert result.returncode == 0, result.stderr
    raw = capture.read_text(encoding="utf-8")
    calls = [json.loads(line) for line in raw.splitlines()]
    after = [
        call
        for call in calls
        if "--type" in call
        and call[call.index("--type") + 1] == "tool.execute.after"
        and call[call.index("--session") + 1] == "native-skill"
    ]
    assert len(after) == 1
    label = after[0][after[0].index("--task-label") + 1]
    assert len(label) <= 500
    assert "private-step-content-sentinel" not in raw
    assert "later-default" not in raw
    if not known:
        assert label.startswith("native-skill-load/v1:")
        assert "--model" not in after[0]
        return
    assert label.startswith("native-skill-load/v2:")
    model = after[0][after[0].index("--model") + 1]
    assert model == "provider/builder#extra-high"
    assert (
        json.loads(label.removeprefix("native-skill-load/v2:"))["u"]
        == hashlib.sha256(model.encode()).hexdigest()
    )


def _native_opencode() -> Path:
    configured = os.environ.get("OPENCODE_EXECUTABLE")
    found = configured or shutil.which("opencode")
    if found is None:
        pytest.skip("OpenCode runtime bulunamadi")
    try:
        return resolve_opencode_executable(found)
    except (OSError, ZekamError):
        pytest.skip("OpenCode direct executable unavailable")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


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
    (project / ".ai").mkdir()
    (project / ".ai" / "repository-context.json").write_text(
        json.dumps({"schema": "zekam-repository-context/v2", "project": "zekam"}),
        encoding="utf-8",
    )
    (project / "PROJE_MANIFESTI.yaml").write_text("project:\n  slug: zekam\n", encoding="utf-8")
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
            "OPENCODE_SERVER_PASSWORD": secrets.token_urlsafe(24),
        }
    )
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment[key] = "http://127.0.0.1:9"
    port = _free_loopback_port()
    server_log = tmp_path / "opencode-server.log"
    server_log_handle = server_log.open("w", encoding="utf-8")
    server = subprocess.Popen(
        [
            str(executable),
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        cwd=project,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=server_log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while True:
            if server.poll() is not None:
                server_log_handle.flush()
                pytest.fail(
                    "OpenCode server erken kapandi: "
                    f"{server_log.read_text(encoding='utf-8')[-1000:]}"
                )
            if server_log.is_file() and "server listening on" in server_log.read_text(
                encoding="utf-8"
            ):
                break
            if time.monotonic() >= deadline:
                pytest.fail("OpenCode persistent server zamaninda hazir olmadi")
            time.sleep(0.1)

        result = subprocess.run(
            [
                str(executable),
                "run",
                "--server",
                base,
                "--log-level",
                "error",
                "--format",
                "json",
                "--model",
                "missing/nope",
                "--title",
                "Zekam provider-free lifecycle harness",
                "provider-free lifecycle probe",
            ],
            cwd=project,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=90,
        )
        session_id: str | None = None
        for line in result.stdout.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            candidate = payload.get("sessionID") if isinstance(payload, dict) else None
            if isinstance(candidate, str) and candidate.startswith("ses"):
                session_id = candidate
                break
        assert session_id, result.stderr[-1000:]
        assert result.returncode != 0, result.stdout[-1000:]
        deleted = subprocess.run(
            [str(executable), "session", "delete", "--server", base, session_id],
            cwd=project,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=90,
            check=False,
        )
        assert deleted.returncode == 0, deleted.stderr[-1000:]

        deadline = time.monotonic() + 15
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
        assert {"session.created", "session.deleted"} <= event_types, result.stderr[-1000:]
        assert all(call[:2] == ["opencode", "event"] for call in calls)
        assert all("provider-free lifecycle harness" not in " ".join(call) for call in calls)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        server_log_handle.close()


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


def test_retry_is_durable_and_precompact_never_blocks_but_stays_queued(tmp_path: Path) -> None:
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
    # Gozlem arizasi native compaction'i bloklamaz (runner: throw yok => 3); teslim edilemeyen
    # ACK durable spool'da gorunur ve telafi edilebilir kalir.
    assert fail_closed.returncode == 3
    assert "spool'da teslim bekliyor" in fail_closed.stderr
    spooled = [item.read_text(encoding="utf-8") for item in _spool(fail_closed_home).glob("*.json")]
    assert any("pre-compact" in item for item in spooled)


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


def _inactive_run(tmp_path: Path, workdir: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    runner, fake_executable = _write_runtime_files(tmp_path)
    home = tmp_path / "home-should-stay-absent"
    environment = dict(os.environ)
    environment.update(
        {
            "ZEKAM_EXECUTABLE": str(fake_executable),
            "ZEKAM_HOME": str(home),
            "EXPECT_INACTIVE": "1",
        }
    )
    result = subprocess.run(
        [_bun_executable(), str(runner)],
        cwd=workdir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    return result, home


def test_plugin_is_inert_outside_a_zekam_workspace(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    other = tmp_path_factory.mktemp("not-zekam")

    result, home = _inactive_run(tmp_path, other)

    assert result.returncode == 0, result.stderr
    assert not home.exists()


def test_plugin_is_inert_when_marker_is_not_a_zekam_context(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    other = tmp_path_factory.mktemp("lookalike")
    (other / ".ai").mkdir()
    (other / ".ai" / "repository-context.json").write_text(
        json.dumps({"schema": "other/v1", "project": "zekam-old"}), encoding="utf-8"
    )
    (other / "PROJE_MANIFESTI.yaml").write_text("project: {}\n", encoding="utf-8")

    result, home = _inactive_run(tmp_path, other)

    assert result.returncode == 0, result.stderr
    assert not home.exists()


def test_plugin_is_active_in_a_subdirectory_of_the_workspace(tmp_path: Path) -> None:
    runner, fake_executable = _write_runtime_files(tmp_path)
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    environment = dict(os.environ)
    environment.update(
        {
            "ZEKAM_EXECUTABLE": str(fake_executable),
            "ZEKAM_HOME": str(tmp_path / "home"),
            "RUN_SYSTEM_TRANSFORM": "1",
        }
    )

    result = subprocess.run(
        [_bun_executable(), str(runner)],
        cwd=nested,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["system"]
