"""Historical Windows native pins/version probes and content-free adapter contracts.

The separate mandatory Mac artifact inventory is not runtime or lifecycle proof.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from zekam.domain.canonical import digest
from zekam.domain.clients import ClientKind
from zekam.infrastructure.clients.adapters import (
    SubprocessClientAdapter,
    claude_code_adapter,
    codex_adapter,
    opencode_adapter,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.name != "nt",
        reason="Historical Windows native pins; Mac native artifact inventory is a separate gate",
    ),
]

_VERSION = re.compile(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])")
_SESSION_ID = "0198f2ad-3d10-7a11-b515-4c5c1733f7c1"
_NOW = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.UTC)


def _native_executable(command: str, environment_key: str) -> Path:
    configured = os.environ.get(environment_key)
    found = configured or shutil.which(command)
    if found is None:
        pytest.skip(f"{command} runtime bulunamadi")
    candidate = Path(found)
    if candidate.suffix.casefold() in {".bat", ".cmd", ".ps1"}:
        native_candidates = {
            "codex": (
                candidate.parent
                / "node_modules"
                / "@openai"
                / "codex"
                / "node_modules"
                / "@openai"
                / "codex-win32-x64"
                / "vendor"
                / "x86_64-pc-windows-msvc"
                / "bin"
                / "codex.exe"
            ),
            "opencode": (
                candidate.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
            ),
        }
        candidate = native_candidates.get(command, candidate)
    if not candidate.is_file():
        pytest.fail(f"{command} native executable bulunamadi: {candidate}")
    return candidate.resolve()


def _provider_free_environment(tmp_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    sensitive_markers = ("API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN", "SECRET", "PASSWORD")
    for key in tuple(environment):
        if any(marker in key.upper() for marker in sensitive_markers):
            environment.pop(key, None)
    deny_proxy = "http://127.0.0.1:9"
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment[key] = deny_proxy
    environment.update(
        {
            "CI": "1",
            "NO_COLOR": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_BUG_COMMAND": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        }
    )
    return environment


@pytest.mark.parametrize(
    (
        "command",
        "environment_key",
        "client_id",
        "client_kind",
        "expected_version",
        "expected_sha256",
        "factory",
        "lifecycle_contract_verified",
    ),
    (
        (
            "claude",
            "CLAUDE_EXECUTABLE",
            "claude-code",
            ClientKind.CLAUDE_CODE,
            "2.1.224",
            "879f0d7e7eee606095051c0c00772fc1de41778f34835a9de43ea8e1caad9afb",
            claude_code_adapter,
            False,
        ),
        (
            "codex",
            "CODEX_EXECUTABLE",
            "codex",
            ClientKind.CODEX,
            "0.150.1",
            "cbd657ddfe151d1a6ebad660beffdbd3265dc5aff4b3a6095124d3e2f0156f2f",
            codex_adapter,
            True,
        ),
        (
            "opencode",
            "OPENCODE_EXECUTABLE",
            "opencode",
            ClientKind.OPENCODE,
            "1.18.23",
            "f831518278ded5090c41cc532b16ab80629e980f710a0b46d1e5b605808bb1d9",
            opencode_adapter,
            True,
        ),
    ),
)
def test_historical_windows_binary_and_content_free_adapter_contract(
    tmp_path: Path,
    command: str,
    environment_key: str,
    client_id: str,
    client_kind: ClientKind,
    expected_version: str,
    expected_sha256: str,
    factory: Callable[..., SubprocessClientAdapter],
    lifecycle_contract_verified: bool,
) -> None:
    executable = _native_executable(command, environment_key)
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == expected_sha256

    version_result = subprocess.run(
        [str(executable), "--version"],
        cwd=tmp_path,
        env=_provider_free_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    output = "\n".join(
        item.strip() for item in (version_result.stdout, version_result.stderr) if item.strip()
    )
    assert version_result.returncode == 0, output
    assert len(output.encode("utf-8")) <= 1024
    observed_versions = frozenset(_VERSION.findall(output))
    assert observed_versions, output
    assert expected_version in observed_versions, output

    adapter = factory(
        str(executable),
        version=expected_version,
        lifecycle_contract_verified=lifecycle_contract_verified,
    )
    lifecycle = adapter.lifecycle_event(
        session_id=_SESSION_ID,
        sequence=1,
        previous_digest=None,
        event_type="session_start",
        payload_digest=digest({"client_id": client_id, "version": expected_version}),
        occurred_at=_NOW,
    )
    document = lifecycle.as_dict()
    assert adapter.descriptor.client_id == client_id
    assert adapter.descriptor.kind is client_kind
    assert adapter.descriptor.version == expected_version
    assert adapter.descriptor.supports("lifecycle-events-v2") is lifecycle_contract_verified
    assert document["transcript_included"] is False
    assert document["grants_authority"] is False
