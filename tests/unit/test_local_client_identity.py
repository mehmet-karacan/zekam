"""Read-only artifact identity boundaries with real Akilli Kasa source bytes."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import socket
import struct
import subprocess
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from zekam.application import local_client_identity as app
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.infrastructure import local_client_identity as module

AKILLI_SOURCE = Path("/Users/mkaracan/Projeler/akilli-kasa/src/akilli_kasa/api/saglik.py")
NOW = dt.datetime(2026, 9, 2, 20, tzinfo=dt.UTC)


@pytest.fixture
def inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    if not AKILLI_SOURCE.is_file():
        pytest.skip("Read-only real Akilli Kasa source unavailable")
    body = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 8, 0, 0)
    body += AKILLI_SOURCE.read_bytes()
    native = tmp_path / "codex"
    native.write_bytes(body)
    native.chmod(0o755)
    pin = app.MacNativeArtifactPin("codex", "0.151.0", hashlib.sha256(body).hexdigest())
    monkeypatch.setattr(app, "MAC_NATIVE_ARTIFACT_PINS", (pin,))
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    return {"native": native, "body": body, "pin": pin}


def _inspect(fixture: dict[str, Any]) -> app.NativeClientObservation:
    return module.inspect_macos_client("codex", fixture["native"], NOW)


def test_readonly_inventory_never_runs_process_network_or_writes(
    inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = inventory["native"].read_bytes()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Read-only inventory attempted process, network, or filesystem mutation")

    for method in ("write_bytes", "write_text", "mkdir", "chmod", "rename", "unlink"):
        monkeypatch.setattr(Path, method, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    observation = _inspect(inventory)
    body = observation.body()
    assert body["inventory_observed"] is True
    assert body["version_source"] == "exact-native-artifact-pin"
    assert body["runtime_version_probe"] == "not-run"
    for name in (
        "runtime_version_observed",
        "wire_contract_reviewed",
        "lifecycle_proven",
        "model_capability_proven",
        "grants_authority",
        "hooks_activated",
        "live_user_config_written",
        "native_path_recorded",
    ):
        assert body[name] is False
    assert body["external_provider_calls"] == body["subprocess_calls"] == 0
    expected = body.pop("evidence_digest")
    assert digest(body) == expected
    assert str(inventory["native"].parent) not in json.dumps(body)
    body["lifecycle_proven"] = True
    assert observation.body()["lifecycle_proven"] is False
    assert inventory["native"].read_bytes() == before
    with pytest.raises(FrozenInstanceError):
        observation.observed_at = NOW  # type: ignore[misc]
    with pytest.raises(TypeError):
        replace(observation, grants_authority=True)  # type: ignore[call-arg]


def test_additive_pins_leave_historical_windows_contracts_unchanged() -> None:
    from zekam.infrastructure.clients.claude_lifecycle import CLAUDE_REVIEWED_VERSION
    from zekam.infrastructure.clients.codex_lifecycle import (
        CODEX_REVIEWED_VERSION,
        CODEX_REVIEWED_WINDOWS_SHA256,
    )

    assert CLAUDE_REVIEWED_VERSION == "2.1.224"
    assert CODEX_REVIEWED_VERSION == "0.150.1"
    assert CODEX_REVIEWED_WINDOWS_SHA256 == (
        "cbd657ddfe151d1a6ebad660beffdbd3265dc5aff4b3a6095124d3e2f0156f2f"
    )
    assert [(pin.client_id, pin.version) for pin in app.MAC_NATIVE_ARTIFACT_PINS] == [
        ("codex", "0.151.0"),
        ("claude-code", "2.1.252"),
        ("opencode", "1.18.16"),
    ]


@pytest.mark.parametrize("client", [None, True, [], "", "Codex", "unknown"])
def test_unknown_or_wrong_type_client_is_not_discovery_fallback(client: Any) -> None:
    with pytest.raises(ValidationFailed):
        app.mac_native_pin(client)


@pytest.mark.parametrize("time", [None, "2026-09-02", True, dt.datetime(2026, 9, 2)])
def test_observation_requires_typed_time(inventory: dict[str, Any], time: Any) -> None:
    with pytest.raises(ValidationFailed):
        module.inspect_macos_client("codex", inventory["native"], time)


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_id", None),
        ("version", True),
        ("version", "0.151"),
        ("version", " 0.151.0"),
        ("native_sha256", "A" * 64),
        ("native_sha256", "a" * 63),
    ],
)
def test_pin_fields_are_strict(field: str, value: Any) -> None:
    with pytest.raises(ValidationFailed):
        replace(app.MAC_NATIVE_ARTIFACT_PINS[0], **{field: value})


def test_arbitrary_well_formed_pin_is_not_admitted() -> None:
    custom = replace(app.MAC_NATIVE_ARTIFACT_PINS[0], native_sha256="a" * 64)
    with pytest.raises(ValidationFailed):
        app.NativeClientObservation(custom, NOW)


@pytest.mark.parametrize("field,value", [("system", "Linux"), ("machine", "x86_64")])
def test_host_drift_rejected(
    inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    monkeypatch.setattr(platform, field, lambda: value)
    with pytest.raises(ConfigurationError):
        _inspect(inventory)


@pytest.mark.parametrize("path", [None, True, "codex", Path("codex"), Path("/tmp/a/../codex")])
def test_path_type_and_traversal_rejected(inventory: dict[str, Any], path: Any) -> None:
    with pytest.raises(ValidationFailed):
        module.inspect_macos_client("codex", path, NOW)


@pytest.mark.parametrize(
    "mutation",
    [
        "hash",
        "cpu",
        "magic",
        "kind",
        "commands",
        "truncated",
        "mode",
        "fifo",
        "missing",
    ],
)
def test_invalid_native_cannot_be_observed(inventory: dict[str, Any], mutation: str) -> None:
    native = inventory["native"]
    body = bytearray(inventory["body"])
    offset = {"cpu": 4, "magic": 0, "kind": 12, "commands": 16}.get(mutation)
    if offset is not None:
        body[offset : offset + 4] = b"\0" * 4
    elif mutation == "hash":
        body[-1] ^= 1
    elif mutation == "truncated":
        body = body[:16]
    elif mutation == "mode":
        native.chmod(0o600)
    native.write_bytes(body)
    if mutation in {"fifo", "missing"}:
        inventory["native"] = native.with_name(mutation)
        if mutation == "fifo":
            os.mkfifo(inventory["native"])
    with pytest.raises(ConfigurationError):
        _inspect(inventory)


def test_native_size_bound(inventory: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "MAX_NATIVE_BYTES", 32)
    with pytest.raises(ConfigurationError):
        _inspect(inventory)


def _launcher(inventory: dict[str, Any]) -> tuple[Path, Path]:
    root = inventory["native"].parent / "node_modules/@openai/codex"
    launcher = root / "bin/codex.js"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("throw new Error('launcher must never execute')")
    (root / "package.json").write_text(json.dumps({"name": "@openai/codex", "version": "0.151.0"}))
    package = root / "node_modules/@openai/codex-darwin-arm64"
    native = package / "vendor/aarch64-apple-darwin/bin/codex"
    native.parent.mkdir(parents=True)
    native.write_bytes(inventory["body"])
    native.chmod(0o755)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@openai/codex",
                "version": "0.151.0-darwin-arm64",
            }
        )
    )
    alias = root.parent.parent.parent / "entrypoint"
    alias.symlink_to(launcher)
    inventory["native"] = alias
    return launcher, root / "package.json"


def test_exact_codex_package_layout_inspects_native_not_launcher(inventory: dict[str, Any]) -> None:
    launcher, _metadata = _launcher(inventory)
    observation = _inspect(inventory)
    assert observation.pin.native_sha256 == hashlib.sha256(inventory["body"]).hexdigest()
    assert observation.pin.native_sha256 != hashlib.sha256(launcher.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "body",
    [
        '{"name":"@openai/codex","version":"0.151.0","version":"0.151.0"}',
        '{"name":"different","version":"0.151.0"}',
        '{"name":"@openai/codex","version":"0.150.1"}',
        "null",
        "[]",
        "{",
        "x" * (module.MAX_PACKAGE_BYTES + 1),
    ],
)
def test_bad_package_metadata_rejected(inventory: dict[str, Any], body: str) -> None:
    _launcher_path, metadata = _launcher(inventory)
    metadata.write_text(body)
    with pytest.raises(ConfigurationError):
        _inspect(inventory)


def test_unknown_javascript_layout_is_not_a_native_fallback(inventory: dict[str, Any]) -> None:
    javascript = inventory["native"].with_suffix(".js")
    javascript.write_text("not a reviewed package layout")
    inventory["native"] = javascript
    with pytest.raises(ConfigurationError):
        _inspect(inventory)


@pytest.mark.parametrize("target", ["native", "package", "alias"])
def test_identity_drift_during_observation_is_rejected(
    inventory: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    metadata: Path | None = None
    if target in {"package", "alias"}:
        _launcher_path, metadata = _launcher(inventory)
    original = module._native_identity
    fired = False

    def capture(path: Path) -> Any:
        nonlocal fired
        result = original(path)
        if not fired:
            fired = True
            if target == "package":
                assert metadata is not None
                metadata.write_bytes(metadata.read_bytes() + b" ")
            elif target == "alias":
                alias = inventory["native"]
                alias.unlink()
                alias.symlink_to(path)
            else:
                path.write_bytes(inventory["body"] + b"changed")
        return result

    monkeypatch.setattr(module, "_native_identity", capture)
    with pytest.raises(ConfigurationError):
        _inspect(inventory)


def test_package_native_symlink_is_rejected(inventory: dict[str, Any]) -> None:
    _launcher_path, _metadata = _launcher(inventory)
    native, _ = module._resolve(inventory["native"], inventory["pin"])
    moved = native.with_name("original-native")
    native.rename(moved)
    native.symlink_to(moved)
    with pytest.raises(ConfigurationError):
        _inspect(inventory)
