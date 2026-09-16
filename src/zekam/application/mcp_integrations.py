"""Canonical MCP registry planning and native client projection."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import tomllib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.application.composition import ApplicationContext
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.client_integration import ClientIntegrationId
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.mcp_integration import (
    McpServerRegistration,
    McpTransport,
    validate_registry_entries,
)

_REGISTRY_SCHEMA = "zekam-mcp-registry/v1"
_PLAN_SCHEMA = "zekam-mcp-sync-plan/v1"
_RECEIPT_SCHEMA = "zekam-mcp-sync-receipt/v1"
_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_CODEX_START = "# zekam-managed-mcp:start "
_CODEX_END = "# zekam-managed-mcp:end "
_UNSET = object()
_CLIENT_PATHS = {
    ClientIntegrationId.OPENCODE: Path(".config") / "opencode" / "opencode.json",
    ClientIntegrationId.CODEX: Path(".codex") / "config.toml",
    ClientIntegrationId.CLAUDE_CODE: Path(".claude.json"),
}
_EXECUTABLES = {
    ClientIntegrationId.OPENCODE: "opencode",
    ClientIntegrationId.CODEX: "codex",
    ClientIntegrationId.CLAUDE_CODE: "claude",
}


def _file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise PolicyViolation("MCP native config regular file olmali")
    raw = path.read_bytes()
    if len(raw) > _MAX_CONFIG_BYTES:
        raise PolicyViolation("MCP native config bounded boyutu asti")
    return digest_of_bytes(raw)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _registry_body(entries: tuple[McpServerRegistration, ...]) -> dict[str, Any]:
    ordered = validate_registry_entries(entries)
    return {
        "schema": _REGISTRY_SCHEMA,
        "servers": [item.as_dict() for item in ordered],
        "grants_authority": False,
    }


def _registry_digest(entries: tuple[McpServerRegistration, ...]) -> str:
    return digest(_registry_body(entries))


def _receipt_path(context: ApplicationContext, plan_digest: str) -> Path:
    return (
        context.home
        / "state"
        / "manifests"
        / "mcp-integrations"
        / f"{plan_digest.removeprefix('sha256:')}.json"
    )


def _load_receipt(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("MCP receipt okunamadi") from exc
    if not isinstance(document, dict):
        raise PolicyViolation("MCP receipt mapping olmali")
    stored = document.get("receipt_digest")
    body = {key: value for key, value in document.items() if key != "receipt_digest"}
    if stored != digest(body) or document.get("schema") != _RECEIPT_SCHEMA:
        raise PolicyViolation("MCP receipt digest drift")
    return document


def _current_registry(
    context: ApplicationContext,
) -> tuple[tuple[McpServerRegistration, ...], dict[str, Any] | None]:
    database = context.settings.database.sqlite_path(context.home)
    if not database.is_file() or database.is_symlink():
        raise PolicyViolation("MCP registry operational store unavailable")
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "select payload_json,terminal_evidence_digest,updated_at,id from local_job "
                "where state='completed' order by updated_at desc,id desc limit 1000"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PolicyViolation("MCP registry operational read failed") from exc
    for raw_payload, terminal, _updated_at, _job_id in rows:
        try:
            payload = json.loads(str(raw_payload))
            effect = payload["effect"]
            plan = effect["plan"]
            if payload.get("operation") != "mcp.sync-v1":
                continue
            if plan.get("schema") != _PLAN_SCHEMA:
                continue
            plan_body = {key: value for key, value in plan.items() if key != "plan_digest"}
            if plan.get("plan_digest") != digest(plan_body):
                continue
            receipt = _load_receipt(_receipt_path(context, str(plan["plan_digest"])))
            if receipt.get("receipt_digest") != terminal:
                continue
            after = plan.get("after_registry")
            if not isinstance(after, dict) or after.get("schema") != _REGISTRY_SCHEMA:
                continue
            entries = tuple(
                McpServerRegistration.from_mapping(item) for item in after.get("servers", ())
            )
            if _registry_body(entries) != after:
                continue
            return validate_registry_entries(entries), receipt
        except (KeyError, TypeError, ValueError, ConfigurationError, PolicyViolation):
            continue
    return (), None


def _enabled(context: ApplicationContext, client: ClientIntegrationId) -> bool:
    return context.settings.cli.integrations.enabled(client)


def _executable_present(client: ClientIntegrationId) -> bool:
    return shutil.which(_EXECUTABLES[client]) is not None


def _opencode_uses_v2() -> bool:
    executable = shutil.which(_EXECUTABLES[ClientIntegrationId.OPENCODE])
    if executable is None:
        return False
    try:
        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.search(r"(?<!\d)(\d+)\.", result.stdout + "\n" + result.stderr)
    return match is not None and int(match.group(1)) >= 2


def _read_json(path: Path, source: bytes | object | None = _UNSET) -> dict[str, Any]:
    if source is _UNSET and not path.exists():
        return {}
    try:
        raw = path.read_bytes() if source is _UNSET else source
        if raw is None:
            return {}
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("MCP JSON native config okunamadi") from exc
    if not isinstance(document, dict):
        raise PolicyViolation("MCP JSON native config mapping olmali")
    return document


def _opencode_entry(registration: McpServerRegistration, *, v2: bool) -> dict[str, Any]:
    if registration.transport is McpTransport.STDIO:
        body: dict[str, Any] = {
            "type": "local",
            "command": list(registration.command),
            "environment": {name: f"{{env:{name}}}" for name in registration.env_vars},
        }
    else:
        body = {"type": "remote", "url": registration.url}
        if registration.bearer_token_env_var:
            body["oauth"] = False
            body["headers"] = {
                "Authorization": f"Bearer {{env:{registration.bearer_token_env_var}}}"
            }
    if v2:
        if not registration.enabled:
            body["disabled"] = True
    else:
        body["enabled"] = registration.enabled
    return body


def _render_opencode(
    path: Path,
    registration: McpServerRegistration,
    *,
    owned_digest: str | None,
    adopt: bool,
    source: bytes | object | None = _UNSET,
) -> bytes:
    document = _read_json(path, source)
    mcp = document.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise PolicyViolation("OpenCode MCP config mapping olmali")
    v2 = isinstance(mcp.get("servers"), dict) or (not mcp and _opencode_uses_v2())
    if v2 and "servers" not in mcp:
        if mcp:
            raise PolicyViolation("OpenCode v2 MCP config legacy girdiler iceriyor")
        mcp["servers"] = {}
    servers = mcp["servers"] if v2 else mcp
    current = servers.get(registration.name)
    if current is not None:
        current_digest = digest(current)
        if owned_digest is None and not adopt:
            raise PolicyViolation("OpenCode MCP entry user-owned; --adopt gerekir")
        if owned_digest is not None and current_digest != owned_digest:
            raise PolicyViolation("OpenCode MCP entry user drift")
    servers[registration.name] = _opencode_entry(registration, v2=v2)
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _codex_block(registration: McpServerRegistration) -> str:
    quoted_name = _toml_string(registration.name)
    lines = [f"{_CODEX_START}{registration.name}", f"[mcp_servers.{quoted_name}]"]
    if registration.transport is McpTransport.STDIO:
        lines.append(f"command = {_toml_string(registration.command[0])}")
        lines.append(
            "args = [" + ", ".join(_toml_string(item) for item in registration.command[1:]) + "]"
        )
        if registration.env_vars:
            lines.append(
                "env_vars = ["
                + ", ".join(_toml_string(item) for item in registration.env_vars)
                + "]"
            )
    else:
        lines.append(f"url = {_toml_string(str(registration.url))}")
        if registration.bearer_token_env_var:
            lines.append(
                "bearer_token_env_var = " + _toml_string(registration.bearer_token_env_var)
            )
    lines.append(f"enabled = {'true' if registration.enabled else 'false'}")
    lines.append(f"{_CODEX_END}{registration.name}")
    return "\n".join(lines) + "\n"


def _remove_codex_block(text: str, name: str) -> tuple[str, str | None]:
    pattern = re.compile(
        rf"(?ms)^\# zekam-managed-mcp:start {re.escape(name)}\r?\n.*?"
        rf"^\# zekam-managed-mcp:end {re.escape(name)}\r?\n?"
    )
    matches = tuple(pattern.finditer(text))
    count = len(matches)
    if count > 1:
        raise PolicyViolation("Codex MCP duplicate managed block")
    if not matches:
        return text, None
    match = matches[0]
    return text[: match.start()] + text[match.end() :], match.group(0)


def _render_codex(
    path: Path,
    registration: McpServerRegistration,
    *,
    owned_digest: str | None,
    adopt: bool,
    source: bytes | object | None = _UNSET,
) -> bytes:
    if source is _UNSET:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    else:
        text = "" if source is None else source.decode("utf-8")
    if len(text.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise PolicyViolation("Codex config bounded boyutu asti")
    without, managed_block = _remove_codex_block(text, registration.name)
    if managed_block is not None and owned_digest is None:
        raise PolicyViolation("Codex MCP ownership receipt missing")
    if managed_block is not None and digest({"managed_block": managed_block}) != owned_digest:
        raise PolicyViolation("Codex MCP entry user drift")
    try:
        parsed = tomllib.loads(without)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyViolation("Codex config TOML gecersiz") from exc
    existing = parsed.get("mcp_servers", {})
    if not isinstance(existing, dict):
        raise PolicyViolation("Codex mcp_servers mapping olmali")
    if registration.name in existing and not adopt:
        raise PolicyViolation("Codex MCP entry user-owned; --adopt gerekir")
    result = without.rstrip() + ("\n\n" if without.strip() else "") + _codex_block(registration)
    try:
        tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyViolation("Codex managed MCP TOML gecersiz") from exc
    return result.encode("utf-8")


def _claude_entry(registration: McpServerRegistration) -> dict[str, Any]:
    if registration.transport is McpTransport.STDIO:
        return {
            "type": "stdio",
            "command": registration.command[0],
            "args": list(registration.command[1:]),
        }
    body: dict[str, Any] = {"type": "http", "url": registration.url}
    if registration.bearer_token_env_var:
        body["headers"] = {"Authorization": f"Bearer ${{{registration.bearer_token_env_var}}}"}
    return body


def _render_claude(
    path: Path,
    registration: McpServerRegistration,
    *,
    owned_digest: str | None,
    adopt: bool,
    source: bytes | object | None = _UNSET,
) -> bytes:
    document = _read_json(path, source)
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise PolicyViolation("Claude mcpServers mapping olmali")
    current = servers.get(registration.name)
    if current is not None:
        current_digest = digest(current)
        if owned_digest is None and not adopt:
            raise PolicyViolation("Claude MCP entry user-owned; --adopt gerekir")
        if owned_digest is not None and current_digest != owned_digest:
            raise PolicyViolation("Claude MCP entry user drift")
    servers[registration.name] = _claude_entry(registration)
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _owned_entry_digest(
    receipt: dict[str, Any] | None, client: ClientIntegrationId, name: str
) -> str | None:
    if receipt is None:
        return None
    for target in receipt.get("targets", ()):
        if target.get("client") == client.value and target.get("server") == name:
            return target.get("entry_digest")
    return None


def _render(
    client: ClientIntegrationId,
    path: Path,
    registration: McpServerRegistration,
    *,
    owned_digest: str | None,
    adopt: bool,
    source: bytes | object | None = _UNSET,
) -> bytes:
    if client is ClientIntegrationId.OPENCODE:
        return _render_opencode(
            path,
            registration,
            owned_digest=owned_digest,
            adopt=adopt,
            source=source,
        )
    if client is ClientIntegrationId.CODEX:
        return _render_codex(
            path,
            registration,
            owned_digest=owned_digest,
            adopt=adopt,
            source=source,
        )
    return _render_claude(
        path,
        registration,
        owned_digest=owned_digest,
        adopt=adopt,
        source=source,
    )


def _render_remove(
    client: ClientIntegrationId,
    path: Path,
    registration: McpServerRegistration,
    *,
    owned_digest: str | None,
    source: bytes | object | None = _UNSET,
) -> bytes:
    if owned_digest is None:
        raise PolicyViolation("MCP remove ownership receipt ister")
    if client is ClientIntegrationId.OPENCODE:
        document = _read_json(path, source)
        mcp = document.get("mcp")
        if not isinstance(mcp, dict):
            raise PolicyViolation("OpenCode MCP config mapping olmali")
        servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else mcp
        current = servers.get(registration.name)
        if current is None or digest(current) != owned_digest:
            raise PolicyViolation("OpenCode MCP remove user drift")
        del servers[registration.name]
        return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if client is ClientIntegrationId.CODEX:
        if source is _UNSET:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
        else:
            text = "" if source is None else source.decode("utf-8")
        result, managed_block = _remove_codex_block(text, registration.name)
        if managed_block is None:
            raise PolicyViolation("Codex MCP managed block bulunamadi")
        if digest({"managed_block": managed_block}) != owned_digest:
            raise PolicyViolation("Codex MCP remove user drift")
        try:
            tomllib.loads(result)
        except tomllib.TOMLDecodeError as exc:
            raise PolicyViolation("Codex MCP remove TOML gecersiz") from exc
        return result.encode("utf-8")
    document = _read_json(path, source)
    servers = document.get("mcpServers")
    if not isinstance(servers, dict):
        raise PolicyViolation("Claude mcpServers mapping olmali")
    current = servers.get(registration.name)
    if current is None or digest(current) != owned_digest:
        raise PolicyViolation("Claude MCP remove user drift")
    del servers[registration.name]
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _entry_digest(
    client: ClientIntegrationId, registration: McpServerRegistration, payload: bytes
) -> str:
    if client is ClientIntegrationId.OPENCODE:
        document = json.loads(payload)
        mcp = document["mcp"]
        servers = mcp["servers"] if isinstance(mcp.get("servers"), dict) else mcp
        return digest(servers[registration.name])
    if client is ClientIntegrationId.CODEX:
        return digest({"managed_block": _codex_block(registration)})
    document = json.loads(payload)
    return digest(document["mcpServers"][registration.name])


@dataclass(frozen=True, slots=True)
class McpSyncPlan:
    context: ApplicationContext
    native_user_root: Path
    action: str
    before: tuple[McpServerRegistration, ...]
    after: tuple[McpServerRegistration, ...]
    targets: tuple[dict[str, Any], ...]
    adopt: bool = False
    source_receipt_digest: str | None = None

    def body(self) -> dict[str, Any]:
        return {
            "schema": _PLAN_SCHEMA,
            "action": self.action,
            "before_registry_digest": _registry_digest(self.before),
            "before_registry": _registry_body(self.before),
            "after_registry": _registry_body(self.after),
            "native_user_root_identity_digest": digest(
                {"native_user_root": os.path.normcase(str(self.native_user_root.resolve()))}
            ),
            "targets": list(self.targets),
            "adopt": self.adopt,
            "source_receipt_digest": self.source_receipt_digest,
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"plan_digest": self.plan_digest, "apply": False}


def build_register_plan(
    context: ApplicationContext,
    registration: McpServerRegistration,
    *,
    native_user_root: Path,
    adopt: bool = False,
) -> McpSyncPlan:
    root = native_user_root.resolve(strict=True)
    before, receipt = _current_registry(context)
    mapping = {item.name.casefold(): item for item in before}
    previous_registration = mapping.get(registration.name.casefold())
    mapping[registration.name.casefold()] = registration
    after = validate_registry_entries(tuple(mapping.values()))
    targets: list[dict[str, Any]] = []
    removed_clients = (
        ()
        if previous_registration is None
        else tuple(
            client for client in previous_registration.clients if client not in registration.clients
        )
    )
    for client in removed_clients:
        owned = _owned_entry_digest(receipt, client, registration.name)
        if owned is None:
            targets.append(
                {
                    "client": client.value,
                    "server": registration.name,
                    "state": "absent-or-unmanaged",
                }
            )
            continue
        relative = _CLIENT_PATHS[client]
        path = root / relative
        payload = _render_remove(
            client,
            path,
            previous_registration,
            owned_digest=owned,
        )
        targets.append(
            {
                "client": client.value,
                "server": registration.name,
                "state": "write",
                "mode": "remove",
                "relative_path": relative.as_posix(),
                "before_digest": _file_digest(path),
                "after_digest": digest_of_bytes(payload),
                "before_entry_digest": owned,
                "entry_digest": None,
            }
        )
    for client in registration.clients:
        relative = _CLIENT_PATHS[client]
        if not _enabled(context, client):
            targets.append(
                {"client": client.value, "server": registration.name, "state": "disabled-policy"}
            )
            continue
        if not _executable_present(client):
            targets.append(
                {
                    "client": client.value,
                    "server": registration.name,
                    "state": "pending-not-installed",
                }
            )
            continue
        path = root / relative
        owned = _owned_entry_digest(receipt, client, registration.name)
        payload = _render(client, path, registration, owned_digest=owned, adopt=adopt)
        targets.append(
            {
                "client": client.value,
                "server": registration.name,
                "state": "write",
                "relative_path": relative.as_posix(),
                "before_digest": _file_digest(path),
                "after_digest": digest_of_bytes(payload),
                "before_entry_digest": owned,
                "entry_digest": _entry_digest(client, registration, payload),
            }
        )
    return McpSyncPlan(context, root, "register", before, after, tuple(targets), adopt)


def build_sync_plan(context: ApplicationContext, *, native_user_root: Path) -> McpSyncPlan:
    before, _receipt = _current_registry(context)
    if not before:
        raise ValidationFailed("MCP registry bos; once server kaydedin")
    # Re-registering each entry builds the final native projection while keeping one registry head.
    # The common case is intentionally bounded to 128 entries.
    targets: list[dict[str, Any]] = []
    root = native_user_root.resolve(strict=True)
    current_receipt = _current_registry(context)[1]
    virtual: dict[ClientIntegrationId, bytes | None] = {}
    for client, relative in _CLIENT_PATHS.items():
        path = root / relative
        virtual[client] = path.read_bytes() if path.exists() else None
    for registration in before:
        for client in registration.clients:
            if not _enabled(context, client):
                targets.append(
                    {
                        "client": client.value,
                        "server": registration.name,
                        "state": "disabled-policy",
                    }
                )
                continue
            if not _executable_present(client):
                targets.append(
                    {
                        "client": client.value,
                        "server": registration.name,
                        "state": "pending-not-installed",
                    }
                )
                continue
            relative = _CLIENT_PATHS[client]
            path = root / relative
            owned = _owned_entry_digest(current_receipt, client, registration.name)
            payload = _render(
                client,
                path,
                registration,
                owned_digest=owned,
                adopt=False,
                source=virtual[client],
            )
            before_payload = virtual[client]
            targets.append(
                {
                    "client": client.value,
                    "server": registration.name,
                    "state": "write",
                    "relative_path": relative.as_posix(),
                    "before_digest": (
                        None if before_payload is None else digest_of_bytes(before_payload)
                    ),
                    "after_digest": digest_of_bytes(payload),
                    "before_entry_digest": owned,
                    "entry_digest": _entry_digest(client, registration, payload),
                }
            )
            virtual[client] = payload
    return McpSyncPlan(context, root, "sync", before, before, tuple(targets), False)


def build_remove_plan(
    context: ApplicationContext,
    name: str,
    *,
    native_user_root: Path,
) -> McpSyncPlan:
    before, receipt = _current_registry(context)
    matches = [item for item in before if item.name == name]
    if len(matches) != 1:
        raise ValidationFailed("MCP registry server bulunamadi")
    registration = matches[0]
    after = tuple(item for item in before if item.name != name)
    root = native_user_root.resolve(strict=True)
    targets: list[dict[str, Any]] = []
    for client in registration.clients:
        owned = _owned_entry_digest(receipt, client, registration.name)
        if owned is None:
            targets.append(
                {
                    "client": client.value,
                    "server": registration.name,
                    "state": "absent-or-unmanaged",
                }
            )
            continue
        relative = _CLIENT_PATHS[client]
        path = root / relative
        payload = _render_remove(client, path, registration, owned_digest=owned)
        targets.append(
            {
                "client": client.value,
                "server": registration.name,
                "state": "write",
                "mode": "remove",
                "relative_path": relative.as_posix(),
                "before_digest": _file_digest(path),
                "after_digest": digest_of_bytes(payload),
                "before_entry_digest": owned,
                "entry_digest": None,
            }
        )
    return McpSyncPlan(context, root, "remove", before, after, tuple(targets), False)


def _registration(plan: McpSyncPlan, name: str) -> McpServerRegistration:
    matches = [item for item in plan.after if item.name == name]
    if not matches:
        matches = [item for item in plan.before if item.name == name]
    if len(matches) != 1:
        raise PolicyViolation("MCP plan registration binding missing")
    return matches[0]


def apply_sync_plan(plan: McpSyncPlan, *, authorized_plan_digest: str) -> dict[str, Any]:
    if authorized_plan_digest != plan.plan_digest:
        raise PolicyViolation("MCP exact plan digest required")
    current, previous_receipt = _current_registry(plan.context)
    if _registry_digest(current) != _registry_digest(plan.before):
        raise PolicyViolation("MCP registry changed before apply")
    receipt_path = _receipt_path(plan.context, plan.plan_digest)
    if receipt_path.is_file():
        return sync_receipt(plan, authorized_plan_digest=authorized_plan_digest)
    written: list[tuple[Path, bytes | None]] = []
    updated_keys = {(str(item.get("client")), str(item.get("server"))) for item in plan.targets}
    target_receipts: list[dict[str, Any]] = [
        dict(item)
        for item in (() if previous_receipt is None else previous_receipt.get("targets", ()))
        if (str(item.get("client")), str(item.get("server"))) not in updated_keys
    ]
    try:
        for target in plan.targets:
            if target["state"] != "write":
                target_receipts.append(dict(target))
                continue
            client = ClientIntegrationId(target["client"])
            path = plan.native_user_root / str(target["relative_path"])
            if _file_digest(path) != target["before_digest"]:
                raise PolicyViolation("MCP native config changed before apply")
            previous = path.read_bytes() if path.exists() else None
            registration = _registration(plan, str(target["server"]))
            if target.get("mode") == "remove":
                payload = _render_remove(
                    client,
                    path,
                    registration,
                    owned_digest=target.get("before_entry_digest"),
                )
            else:
                payload = _render(
                    client,
                    path,
                    registration,
                    owned_digest=target.get("before_entry_digest"),
                    adopt=plan.adopt,
                )
            if digest_of_bytes(payload) != target["after_digest"]:
                raise PolicyViolation("MCP native config render drift")
            _atomic_write(path, payload)
            written.append((path, previous))
            if _file_digest(path) != target["after_digest"]:
                raise PolicyViolation("MCP native config readback drift")
            target_receipts.append(dict(target) | {"state": "written-and-read-back"})
    except BaseException as error:
        compensation_failed = False
        for path, previous in reversed(written):
            try:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, previous)
            except OSError:
                compensation_failed = True
        if compensation_failed:
            raise PolicyViolation("MCP sync recovery-required") from error
        raise
    body = {
        "schema": _RECEIPT_SCHEMA,
        "plan_digest": plan.plan_digest,
        "registry_digest": _registry_digest(plan.after),
        "targets": target_receipts,
        "state": "completed",
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    receipt = body | {"receipt_digest": digest(body)}
    _atomic_write(receipt_path, canonical_json(receipt).encode("utf-8"))
    return receipt


def sync_receipt(plan: McpSyncPlan, *, authorized_plan_digest: str) -> dict[str, Any]:
    if authorized_plan_digest != plan.plan_digest:
        raise PolicyViolation("MCP exact receipt plan digest required")
    receipt = _load_receipt(_receipt_path(plan.context, plan.plan_digest))
    if receipt.get("plan_digest") != plan.plan_digest:
        raise PolicyViolation("MCP receipt plan drift")
    for target in receipt.get("targets", ()):
        if target.get("state") != "written-and-read-back":
            continue
        path = plan.native_user_root / str(target["relative_path"])
        if _file_digest(path) != target["after_digest"]:
            raise PolicyViolation("MCP receipt terminal native drift")
    return receipt


def registry_status(context: ApplicationContext, *, native_user_root: Path) -> dict[str, Any]:
    entries, receipt = _current_registry(context)
    clients = []
    for client in ClientIntegrationId:
        clients.append(
            {
                "client": client.value,
                "enabled": _enabled(context, client),
                "installed": _executable_present(client),
            }
        )
    return {
        "schema": "zekam-mcp-registry-status/v1",
        "registry_digest": _registry_digest(entries),
        "servers": [item.as_dict() for item in entries],
        "clients": clients,
        "last_receipt_digest": None if receipt is None else receipt["receipt_digest"],
        "native_user_root_identity_digest": digest(
            {"native_user_root": os.path.normcase(str(native_user_root.resolve(strict=True)))}
        ),
        "grants_authority": False,
    }


def _source_plan_by_receipt(
    context: ApplicationContext, receipt_digest: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    database = context.settings.database.sqlite_path(context.home)
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "select payload_json,terminal_evidence_digest from local_job "
                "where state='completed' order by updated_at desc,id desc limit 1000"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PolicyViolation("MCP rollback operational read failed") from exc
    for raw_payload, terminal in rows:
        if terminal != receipt_digest:
            continue
        try:
            payload = json.loads(str(raw_payload))
            if payload.get("operation") != "mcp.sync-v1":
                continue
            plan = payload["effect"]["plan"]
            receipt = _load_receipt(_receipt_path(context, str(plan["plan_digest"])))
            if receipt.get("receipt_digest") == receipt_digest:
                return plan, receipt
        except (KeyError, TypeError, ValueError, PolicyViolation):
            continue
    raise ValidationFailed("MCP rollback source receipt bulunamadi")


def build_rollback_plan(
    context: ApplicationContext,
    *,
    receipt_digest: str,
    native_user_root: Path,
) -> McpSyncPlan:
    current, current_receipt = _current_registry(context)
    if current_receipt is None or current_receipt.get("receipt_digest") != receipt_digest:
        raise PolicyViolation("MCP rollback yalniz latest receipt icin desteklenir")
    source_plan, _source_receipt = _source_plan_by_receipt(context, receipt_digest)
    raw_before = source_plan.get("before_registry")
    if raw_before is None:
        empty: tuple[McpServerRegistration, ...] = ()
        if source_plan.get("before_registry_digest") != _registry_digest(empty):
            raise PolicyViolation("MCP legacy rollback before registry unavailable")
        previous = empty
    else:
        if not isinstance(raw_before, dict) or raw_before.get("schema") != _REGISTRY_SCHEMA:
            raise PolicyViolation("MCP rollback before registry invalid")
        previous = validate_registry_entries(
            tuple(
                McpServerRegistration.from_mapping(item) for item in raw_before.get("servers", ())
            )
        )
        if _registry_body(previous) != raw_before:
            raise PolicyViolation("MCP rollback before registry drift")
    root = native_user_root.resolve(strict=True)
    targets: list[dict[str, Any]] = []
    previous_by_name = {item.name: item for item in previous}
    current_by_name = {item.name: item for item in current}
    virtual: dict[ClientIntegrationId, bytes | None] = {}
    for client, relative in _CLIENT_PATHS.items():
        path = root / relative
        virtual[client] = path.read_bytes() if path.exists() else None
    raw_targets = source_plan.get("targets", ())
    if not isinstance(raw_targets, list):
        raise PolicyViolation("MCP rollback source targets invalid")
    for source in reversed(raw_targets):
        if not isinstance(source, dict) or source.get("state") != "write":
            continue
        client = ClientIntegrationId(str(source["client"]))
        name = str(source["server"])
        relative = _CLIENT_PATHS[client]
        path = root / relative
        before_payload = virtual[client]
        owned = _owned_entry_digest(current_receipt, client, name)
        previous_registration = previous_by_name.get(name)
        if previous_registration is not None and client in previous_registration.clients:
            payload = _render(
                client,
                path,
                previous_registration,
                owned_digest=owned,
                adopt=False,
                source=before_payload,
            )
            mode = "render"
            entry_digest = _entry_digest(client, previous_registration, payload)
        else:
            current_registration = current_by_name.get(name)
            if current_registration is None:
                raise PolicyViolation("MCP rollback current registration missing")
            payload = _render_remove(
                client,
                path,
                current_registration,
                owned_digest=owned,
                source=before_payload,
            )
            mode = "remove"
            entry_digest = None
        targets.append(
            {
                "client": client.value,
                "server": name,
                "state": "write",
                "mode": mode,
                "relative_path": relative.as_posix(),
                "before_digest": (
                    None if before_payload is None else digest_of_bytes(before_payload)
                ),
                "after_digest": digest_of_bytes(payload),
                "before_entry_digest": owned,
                "entry_digest": entry_digest,
            }
        )
        virtual[client] = payload
    return McpSyncPlan(
        context,
        root,
        "rollback",
        current,
        previous,
        tuple(targets),
        False,
        receipt_digest,
    )


def mcp_mutation_resource(native_user_root: Path) -> str:
    return "mcp-integrations:user:" + digest(
        {"native_user_root": os.path.normcase(str(native_user_root.resolve(strict=True)))}
    )
