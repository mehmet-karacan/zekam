"""Idempotent managed lifecycle hook configuration for real CLI clients."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.domain.errors import ConfigurationError

_EVENTS = ("SessionStart", "PreCompact", "PostCompact", "Stop", "SessionEnd")
_REPARSE_POINT = 0x400
_COMMAND_MARKER = "-m zekam.interfaces.cli.client hook --client "
_LEGACY_COMMAND_PREFIX = "python " + _COMMAND_MARKER
_VERSIONS = {"codex": "0.150.1", "claude-code": "2.1.224"}


@dataclass(frozen=True, slots=True)
class ClientHookFilePlan:
    client_id: str
    path: Path
    document: dict[str, Any]
    original_text: str | None
    action: str


@dataclass(frozen=True, slots=True)
class ClientHookBootstrapPlan:
    files: tuple[ClientHookFilePlan, ...]


def _unsafe(path: Path) -> bool:
    try:
        stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & _REPARSE_POINT)


def _assert_safe_home(user_home: Path) -> None:
    if not user_home.is_absolute() or not user_home.is_dir() or _unsafe(user_home):
        raise ConfigurationError("Hook bootstrap guvenli absolute mevcut user home ister")


def _assert_safe_path(user_home: Path, path: Path) -> None:
    if user_home not in path.parents:
        raise ConfigurationError("Hook bootstrap hedefi user home altinda olmali")
    current = user_home
    for segment in path.relative_to(user_home).parts[:-1]:
        current /= segment
        if current.exists() and (_unsafe(current) or not current.is_dir()):
            raise ConfigurationError("Hook bootstrap parent symlink/reparse olamaz")
    if path.exists() and (_unsafe(path) or not path.is_file()):
        raise ConfigurationError("Hook bootstrap hedefi regular file olmali")


def _load(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, None
    text = path.read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("Hook config strict JSON olarak okunamadi") from exc
    if not isinstance(document, dict):
        raise ConfigurationError("Hook config JSON object olmali")
    return document, text


def _command(client_id: str, python_executable: Path) -> str:
    return shlex.join(
        (
            str(python_executable),
            "-m",
            "zekam.interfaces.cli.client",
            "hook",
            "--client",
            client_id,
            "--client-version",
            _VERSIONS[client_id],
        )
    )


def _managed_hook(group: Any, client_id: str) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 1 or not isinstance(hooks[0], dict):
        return False
    command = hooks[0].get("command")
    return isinstance(command, str) and f"{_COMMAND_MARKER}{client_id} " in command


def _mentions_managed(group: Any, client_id: str) -> bool:
    try:
        rendered = json.dumps(group, ensure_ascii=True)
    except (TypeError, ValueError):
        return False
    return _LEGACY_COMMAND_PREFIX + client_id in rendered or _COMMAND_MARKER + client_id in rendered


def _group(client_id: str, python_executable: Path, event: str) -> dict[str, Any]:
    argv = (
        str(python_executable),
        "-m",
        "zekam.interfaces.cli.client",
        "hook",
        "--client",
        client_id,
        "--client-version",
        _VERSIONS[client_id],
    )
    hook: dict[str, Any] = {
        "type": "command",
        "command": _command(client_id, python_executable),
        "timeout": 3 if event == "SessionEnd" else 10,
    }
    if client_id == "codex":
        hook["commandWindows"] = subprocess.list2cmdline(argv)
    return {"matcher": "", "hooks": [hook]}


def _updated_document(
    source: dict[str, Any], *, client_id: str, python_executable: Path
) -> dict[str, Any]:
    document = dict(source)
    existing_hooks = document.get("hooks", {})
    if not isinstance(existing_hooks, dict):
        raise ConfigurationError("Hook config hooks object olmali")
    updated_hooks = dict(existing_hooks)
    for event in _EVENTS:
        groups = existing_hooks.get(event, [])
        if not isinstance(groups, list):
            raise ConfigurationError(f"Hook config {event} list olmali")
        managed = [index for index, group in enumerate(groups) if _managed_hook(group, client_id)]
        broken = [
            index
            for index, group in enumerate(groups)
            if _mentions_managed(group, client_id) and index not in managed
        ]
        if broken or len(managed) > 1:
            raise ConfigurationError(f"Hook config {event} managed entry bozuk veya duplicate")
        replacement = _group(client_id, python_executable, event)
        rendered = list(groups)
        if managed:
            rendered[managed[0]] = replacement
        else:
            rendered.append(replacement)
        updated_hooks[event] = rendered
    document["hooks"] = updated_hooks
    return document


def plan_client_hook_bootstrap(
    *, user_home: Path, python_executable: Path
) -> ClientHookBootstrapPlan:
    """Plan managed Codex and Claude hook entries without writing files."""

    _assert_safe_home(user_home)
    if not python_executable.is_absolute() or not python_executable.is_file():
        raise ConfigurationError("Hook bootstrap exact Python executable ister")
    targets = (
        ("codex", user_home / ".codex" / "hooks.json"),
        ("claude-code", user_home / ".claude" / "settings.json"),
    )
    files: list[ClientHookFilePlan] = []
    for client_id, path in targets:
        _assert_safe_path(user_home, path)
        source, original = _load(path)
        document = _updated_document(
            source, client_id=client_id, python_executable=python_executable
        )
        rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        action = (
            "unchanged" if original == rendered else ("create" if original is None else "update")
        )
        files.append(ClientHookFilePlan(client_id, path, document, original, action))
    return ClientHookBootstrapPlan(tuple(files))


def apply_client_hook_bootstrap(plan: ClientHookBootstrapPlan) -> None:
    """Apply a fresh hook plan with atomic replacement and stale rejection."""

    for item in plan.files:
        user_home = item.path.parents[1]
        _assert_safe_home(user_home)
        _assert_safe_path(user_home, item.path)
        current = item.path.read_text(encoding="utf-8") if item.path.exists() else None
        if current != item.original_text:
            raise ConfigurationError("Hook bootstrap plani dosya drift nedeniyle stale")
        if item.action == "unchanged":
            continue
        item.path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_path(user_home, item.path)
        rendered = json.dumps(item.document, ensure_ascii=False, indent=2) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{item.path.name}.", dir=item.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(item.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
