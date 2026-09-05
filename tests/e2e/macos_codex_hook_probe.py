"""Test-only, review-before-execution Mac hook observation; never a model oracle.

Preparation is inert. Execution requires the exact externally reviewed plan digest.
The native pathname is checked before/after, NOT atomic against same-UID attackers.
The kernel sandbox remains the host protection boundary even if identity drifts.
"""

from __future__ import annotations

import contextlib
import ctypes
import datetime as dt
import errno
import hashlib
import json
import os
import platform
import pwd
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import sysconfig
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.infrastructure.local_client_identity import inspect_macos_client

EVIDENCE = Path("/Users/mkaracan/zekam-wp08-v3-evidence.16wYdB")
PROJECT = Path("/Users/mkaracan/Projeler/akilli-kasa")
SOURCE = PROJECT / "src/akilli_kasa/api/saglik.py"
ENTRYPOINT = Path("/Users/mkaracan/.local/bin/codex")
NATIVE = Path(
    "/Users/mkaracan/.local/lib/node_modules/@openai/codex/node_modules/"
    "@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"
)
NATIVE_SHA = "98491713ffb196061003ee148636e743997cc31d76144ba7c53462269896891d"
ZSH = Path("/bin/zsh")
ZSH_SHA = "3fbc7a357f2cc9ee90b975f76c27744c19a051a5922fe59c5c8a3ac7a981ffc5"
ZSH_SIZE = 1_357_312
PYTHON_RUNTIME = Path(
    "/opt/homebrew/Cellar/python@3.12/3.12.14/Frameworks/Python.framework/Versions/3.12/"
    "Resources/Python.app/Contents/MacOS/Python"
)
PYTHON_RUNTIME_SHA = "0467c7061b8f4b4e08cfe72b80da9fb3928cb11d0fbc81f51b571922c377eabb"
PYTHON_RUNTIME_SIZE = 33_568
QUESTION = (
    "Akıllı Kasa src/akilli_kasa/api/saglik.py içindeki /saglik uç noktasının "
    "SaglikYaniti dönüş alanları nelerdir?"
)
STOP_MARKER = "ZEKAM_WP08_START_ABORT"
TIMEOUT = 30.0
OUTPUT_CAP = 128 * 1024
DIAGNOSTIC_LINE_CAP = 4096
PROCESS_CENSUS_CAP = 4096
OWNED_SESSION_CAP = 64
CLEANUP_TIMEOUT = 3.0
PATH_ALIAS_WARNING = b"WARNING: proceeding, even though we could not create PATH aliases:"
# These exact text prefixes were observed read-only in the pinned 0.151.0 native.
# Tags describe prefix observations, not causal attribution of an exit status.
DIAGNOSTIC_PREFIXES: tuple[tuple[str, tuple[bytes, ...]], ...] = (
    (
        "cwd-resolution",
        (
            b"failed to determine current directory",
            b"failed to determine working directory",
            b"failed to determine the working directory",
            b"failed to read current working directory",
        ),
    ),
    ("home-canonicalization", (b"failed to canonicalize codex_home ",)),
    ("directory-read", (b"failed to read directory",)),
    (
        "config-loading",
        (
            b"error loading config",
            b"failed to load config",
            b"failed to read config",
            b"failed to parse config",
            b"failed to load managed config",
            b"failed to read project config",
            b"failed to load bootstrap config",
        ),
    ),
)

# Standalone stdlib program: neither imports Zekam nor opens the transcript/source.
# Unrecognized wire fields/values are ignored, never copied or hashed into evidence.
CAPTURE_SCRIPT = r"""import json, os, re, sys

def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result

def check_tree(value, depth=0):
    if depth > 12:
        raise ValueError("depth")
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError("keys")
        for item in value.values():
            check_tree(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 128:
            raise ValueError("list")
        for item in value:
            check_tree(item, depth + 1)

event = sys.argv[1]
reply = ({"continue": False, "stopReason": "ZEKAM_WP08_START_ABORT"}
         if event == "SessionStart" else {})
try:
    raw = sys.stdin.buffer.read(65537)
    if not 1 <= len(raw) <= 65536:
        raise ValueError("size")
    wire = json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
    check_tree(wire)
    if not isinstance(wire, dict) or wire.get("hook_event_name") != event:
        raise ValueError("event")
    sid = wire.get("session_id")
    if not isinstance(sid, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid) is None:
        raise ValueError("session")
    if wire.get("cwd") != "/Users/mkaracan/Projeler/akilli-kasa":
        raise ValueError("scope")
    if "transcript_path" not in wire:
        raise ValueError("transcript-key")
    if (wire["transcript_path"] is not None
            and (not isinstance(wire["transcript_path"], str)
                 or not 1 <= len(wire["transcript_path"]) <= 4096)):
        raise ValueError("transcript-type")
    source = wire.get("source") if event == "SessionStart" else wire.get("reason")
    allowed = ("startup",) if event == "SessionStart" else ("other",)
    if source not in allowed:
        raise ValueError("event-scope")
    known = ("session_id", "transcript_path", "cwd", "hook_event_name", "model",
             "permission_mode", "source", "reason", "turn_id")
    receipt = {"schema": 1, "event": event, "session_id": sid,
               "event_scope": source, "cwd_matches_source_project": True,
               "known_field_types": {key: type(wire[key]).__name__ for key in known if key in wire},
               "startup_abort_requested": event == "SessionStart",
               "raw_wire_persisted": False, "transcript_opened": False,
               "grants_authority": False}
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 4096:
        raise ValueError("receipt-size")
    directory = os.open(sys.argv[2], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(event + ".json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
except Exception:
    # Start remains fail-closed even when the durable evidence write failed.
    print(json.dumps(reply), flush=True)
    sys.exit(2)
print(json.dumps(reply), flush=True)
"""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def identity(path: Path) -> tuple[int, ...]:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("canonical absolute artifact path required")
    for parent in reversed(path.parents):
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ValueError("symlink ancestor rejected")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("regular artifact required")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def read_stable(path: Path, cap: int = 1024 * 1024) -> tuple[tuple[int, ...], bytes]:
    before = identity(path)
    if not 0 < before[3] <= cap:
        raise ValueError("artifact size bound")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != before[:2]:
            raise ValueError("artifact changed before read")
        raw = stream.read(cap + 1)
    if len(raw) != before[3] or identity(path) != before:
        raise ValueError("artifact changed during read")
    return before, raw


def system_zsh_identity() -> tuple[tuple[int, ...], bytes]:
    observed, raw = read_stable(ZSH, 2 * 1024 * 1024)
    info = ZSH.lstat()
    if (
        info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o755
        or info.st_size != ZSH_SIZE
        or sha(raw) != ZSH_SHA
    ):
        raise ValueError("reviewed system zsh identity required")
    return observed, raw


def framework_python_identity() -> tuple[tuple[int, ...], bytes]:
    observed, raw = read_stable(PYTHON_RUNTIME, 64 * 1024)
    info = PYTHON_RUNTIME.lstat()
    if (
        info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o755
        or info.st_size != PYTHON_RUNTIME_SIZE
        or sha(raw) != PYTHON_RUNTIME_SHA
    ):
        raise ValueError("reviewed framework Python identity required")
    return observed, raw


def _new_file(directory: int, name: str, raw: bytes) -> None:
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _directory(path: Path) -> tuple[int, int, int]:
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("physical directory required")
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("private owned directory required")
    return info.st_dev, info.st_ino, info.st_mode


def _profile(root: Path, python: Path, stdlib: Path) -> str:
    def literal(path: Path | str) -> str:
        return "(literal " + json.dumps(str(path)) + ")"

    def subpath(path: Path | str) -> str:
        return "(subpath " + json.dumps(str(path)) + ")"

    actual_home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(strict=True)
    library = python.parent.parent / "Python"
    reads = [
        literal("/"),
        literal(PROJECT),
        literal(PROJECT.parent),
        literal(NATIVE),
        literal(python),
        literal(library),
        literal("/bin/sh"),
        literal(ZSH),
        literal(PYTHON_RUNTIME),
        literal(SOURCE),
        subpath(root),
        subpath(stdlib),
        subpath("/System/Library"),
        subpath("/usr/lib"),
        subpath("/usr/share/locale"),
        subpath("/usr/share/zoneinfo"),
        literal("/dev/null"),
        literal("/dev/urandom"),
        literal("/dev/random"),
    ]
    # No whole-home/project read grant; only the above exact exceptions exist.
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            "(deny mach-lookup)",
            "(deny file-read-data)",
            "(allow file-read-data " + " ".join(reads) + ")",
            "(deny file-read-data " + subpath(stdlib / "site-packages") + ")",
            "(deny file-read-data "
            + " ".join(
                subpath(path)
                for path in (
                    actual_home / ".codex",
                    actual_home / ".claude",
                    actual_home / ".config",
                    actual_home / "Library/Keychains",
                    Path("/Library/Keychains"),
                    Path("/Library/Managed Preferences"),
                    Path("/etc/codex"),
                    Path("/private/etc/codex"),
                )
            )
            + ")",
            "(deny file-write*)",
            "(allow file-write* " + subpath(root / "tmp") + " " + subpath(root / "receipts") + ")",
            "(allow file-write* " + literal(root / "codex/installation_id") + ")",
            "(deny process-exec)",
            "(allow process-exec "
            + " ".join(literal(path) for path in (NATIVE, python, "/bin/sh", ZSH, PYTHON_RUNTIME))
            + ")",
            "",
        ]
    )


def prepare_probe() -> tuple[Path, str]:
    """Create a new external review bundle. No subprocess/native execution."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("exact Mac arm64 probe only")
    parent_identity = _directory(EVIDENCE)
    source_identity, source_raw = read_stable(SOURCE, 65536)
    for required in (b"class SaglikYaniti", b"durum: str", b"uygulama: str", b"surum: str"):
        if required not in source_raw:
            raise ValueError("current health fixture no longer matches question")
    inventory = inspect_macos_client("codex", ENTRYPOINT, dt.datetime.now(dt.UTC)).body()
    if inventory["native_sha256"] != NATIVE_SHA:
        raise ValueError("native pin mismatch")
    zsh_identity, zsh_raw = system_zsh_identity()
    framework_python, framework_python_raw = framework_python_identity()
    python = Path(sys.executable).resolve(strict=True)
    stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    name = "mac-hook-review-" + uuid.uuid4().hex
    root = EVIDENCE / name
    parent = os.open(EVIDENCE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(parent).st_ino != parent_identity[1]:
            raise ValueError("evidence root changed")
        os.mkdir(name, 0o700, dir_fd=parent)
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        for child in ("codex", "tmp", "receipts", "runtime"):
            os.mkdir(child, 0o700, dir_fd=directory)
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "CODEX_HOME": str(root / "codex"),
            "TMPDIR": str(root / "tmp"),
            "XDG_CONFIG_HOME": str(root / "codex"),
            "XDG_CACHE_HOME": str(root / "runtime"),
            "XDG_DATA_HOME": str(root / "runtime"),
            "RUST_LOG": "off",
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
        config = """model = "zekam-hook-no-dispatch"
model_provider = "zekam-no-dispatch"
approval_policy = "never"
sandbox_mode = "read-only"
allow_login_shell = false
cli_auth_credentials_store = "file"
check_for_update_on_startup = false
web_search = "disabled"
[features]
hooks = true
shell_snapshot = false
remote_plugin = false
apps = false
[history]
persistence = "none"
[analytics]
enabled = false
[feedback]
enabled = false
[otel]
exporter = "none"
metrics_exporter = "none"
[model_providers.zekam-no-dispatch]
name = "Provider-free hook observation"
base_url = "http://127.0.0.1:9/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
[projects."/Users/mkaracan/Projeler/akilli-kasa"]
trust_level = "untrusted"
"""
        hooks = {
            "hooks": {
                event: [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "timeout": 3,
                                "command": shlex.join(
                                    [
                                        str(python),
                                        "-I",
                                        "-S",
                                        str(root / "capture.py"),
                                        event,
                                        str(root / "receipts"),
                                    ]
                                ),
                            }
                        ]
                    }
                ]
                for event in ("SessionStart", "SessionEnd")
            }
        }
        argv = [
            "/usr/bin/sandbox-exec",
            "-f",
            str(root / "sandbox.sb"),
            str(NATIVE),
            "exec",
            "--ephemeral",
            "--json",
            "--dangerously-bypass-hook-trust",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            QUESTION,
        ]
        files = {
            "capture.py": CAPTURE_SCRIPT.encode(),
            "sandbox.sb": _profile(root, python, stdlib).encode(),
            "argv.json": canonical(argv),
            "environment.json": canonical(environment),
        }
        for filename, raw in files.items():
            _new_file(directory, filename, raw)
        config_dir = os.open(
            "codex", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
        )
        try:
            _new_file(config_dir, "config.toml", config.encode())
            _new_file(config_dir, "hooks.json", canonical(hooks))
            os.fsync(config_dir)
        finally:
            os.close(config_dir)
        files.update({"codex/config.toml": config.encode(), "codex/hooks.json": canonical(hooks)})
        plan = {
            "schema": 1,
            "scope": "natural-start-end-observation-only",
            "cwd": str(PROJECT),
            "files": {name: sha(raw) for name, raw in files.items()},
            "source_sha256": sha(source_raw),
            "source_identity": source_identity,
            "native_sha256": NATIVE_SHA,
            "native_identity": identity(NATIVE),
            "zsh": {
                "path": str(ZSH),
                "sha256": sha(zsh_raw),
                "identity": zsh_identity,
                "uid": 0,
                "nlink": 1,
                "mode": "0755",
                "size": ZSH_SIZE,
            },
            "framework_python": {
                "path": str(PYTHON_RUNTIME),
                "sha256": sha(framework_python_raw),
                "identity": framework_python,
                "uid": os.geteuid(),
                "nlink": 1,
                "mode": "0755",
                "size": PYTHON_RUNTIME_SIZE,
            },
            "python_identity": identity(python),
            "python": str(python),
            "helper_sha256": sha(read_stable(Path(__file__).resolve())[1]),
            "root_identity": _directory(root),
            "child_identities": {
                child: _directory(root / child) for child in ("codex", "tmp", "receipts", "runtime")
            },
            "source_ref": "src/akilli_kasa/api/saglik.py",
            "timeout_seconds": TIMEOUT,
            "output_cap_bytes": OUTPUT_CAP,
            "expected_natural_receipts": ["SessionStart", "SessionEnd"],
            "required_startup_abort_marker": STOP_MARKER,
            "failure_semantics": {
                "missing_or_invalid_natural_event": "failed-gate",
                "timeout_or_output_cap": "failed-gate-output-unavailable",
                "process_admission_or_cleanup": "failed-gate",
                "identity_or_artifact_drift": "failed-gate",
                "provider_or_network_attempt": "not-success-evidence",
            },
            "process_supervision": {
                "uid_process_census_cap": PROCESS_CENSUS_CAP,
                "owned_session_member_cap": OWNED_SESSION_CAP,
                "cleanup_timeout_seconds": CLEANUP_TIMEOUT,
                "separate_process_group_same_session_cleanup": True,
                "trusted_descendant_setsid_escape_guaranteed": False,
            },
            "runtime_persistence_permitted": False,
            "runtime_persistence_scope": "state-db-and-rollout",
            "native_bootstrap_file_write": "installation-id-only",
            "new_system_execution_scope": "literal-/bin/zsh-read-and-exec-only",
            "new_framework_execution_scope": "literal-framework-python-read-and-exec-only",
            "network_permitted": False,
            "lifecycle_accepted": False,
            "same_uid_execution_atomicity_claimed": False,
        }
        encoded = canonical(plan)
        _new_file(directory, "review-plan.json", encoded)
        os.fsync(directory)
    finally:
        os.close(directory)
    if _directory(EVIDENCE) != parent_identity or read_stable(SOURCE, 65536) != (
        source_identity,
        source_raw,
    ):
        raise ValueError("preparation source/evidence identity drift")
    if system_zsh_identity() != (zsh_identity, zsh_raw):
        raise ValueError("preparation system zsh identity drift")
    if framework_python_identity() != (framework_python, framework_python_raw):
        raise ValueError("preparation framework Python identity drift")
    return root, sha(encoded)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load(path: Path, cap: int = 1024 * 1024) -> Any:
    return json.loads(read_stable(path, cap)[1], object_pairs_hook=_unique)


def _validate(root: Path, reviewed_digest: str, *, pristine: bool = False) -> dict[str, Any]:
    if root.parent != EVIDENCE or not root.name.startswith("mac-hook-review-"):
        raise ValueError("exact external review root required")
    plan_raw = read_stable(root / "review-plan.json")[1]
    if (
        type(reviewed_digest) is not str
        or len(reviewed_digest) != 64
        or sha(plan_raw) != reviewed_digest
    ):
        raise ValueError("externally reviewed exact plan digest required")
    plan: dict[str, Any] = json.loads(plan_raw, object_pairs_hook=_unique)
    if (
        plan.get("expected_natural_receipts") != ["SessionStart", "SessionEnd"]
        or plan.get("required_startup_abort_marker") != STOP_MARKER
        or plan.get("new_system_execution_scope") != "literal-/bin/zsh-read-and-exec-only"
        or plan.get("new_framework_execution_scope")
        != "literal-framework-python-read-and-exec-only"
    ):
        raise ValueError("reviewed hook outcome contract required")
    if list(_directory(root)) != plan["root_identity"]:
        raise ValueError("review root changed")
    for child in ("codex", "tmp", "receipts", "runtime"):
        if list(_directory(root / child)) != plan["child_identities"][child]:
            raise ValueError("review child directory changed")
    if pristine and any((root / "receipts").iterdir()):
        raise ValueError("pristine receipt directory required before native execution")
    if pristine:
        try:
            (root / "codex/installation_id").lstat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError("pristine installation identity required before native execution")
    for relative, expected in plan["files"].items():
        if relative not in {
            "capture.py",
            "sandbox.sb",
            "argv.json",
            "environment.json",
            "codex/config.toml",
            "codex/hooks.json",
        }:
            raise ValueError("unexpected plan artifact")
        if sha(read_stable(root / relative)[1]) != expected:
            raise ValueError("reviewed artifact drift")
    source_identity, source_raw = read_stable(SOURCE, 65536)
    if list(source_identity) != plan["source_identity"] or sha(source_raw) != plan["source_sha256"]:
        raise ValueError("health source drift")
    if (
        list(identity(NATIVE)) != plan["native_identity"]
        or list(identity(Path(plan["python"]))) != plan["python_identity"]
    ):
        raise ValueError("executable metadata drift")
    zsh_identity, zsh_raw = system_zsh_identity()
    if plan.get("zsh") != {
        "path": str(ZSH),
        "sha256": sha(zsh_raw),
        "identity": list(zsh_identity),
        "uid": 0,
        "nlink": 1,
        "mode": "0755",
        "size": ZSH_SIZE,
    }:
        raise ValueError("system zsh identity drift")
    framework_python, framework_python_raw = framework_python_identity()
    if plan.get("framework_python") != {
        "path": str(PYTHON_RUNTIME),
        "sha256": sha(framework_python_raw),
        "identity": list(framework_python),
        "uid": os.geteuid(),
        "nlink": 1,
        "mode": "0755",
        "size": PYTHON_RUNTIME_SIZE,
    }:
        raise ValueError("framework Python identity drift")
    if sha(read_stable(Path(__file__).resolve())[1]) != plan["helper_sha256"]:
        raise ValueError("test helper drift")
    observation = inspect_macos_client("codex", ENTRYPOINT, dt.datetime.now(dt.UTC)).body()
    if observation["native_sha256"] != NATIVE_SHA:
        raise ValueError("native identity drift")
    return plan


def _receipt(path: Path, event: str) -> dict[str, Any]:
    value = _load(path, 4096)
    keys = {
        "schema",
        "event",
        "session_id",
        "event_scope",
        "cwd_matches_source_project",
        "known_field_types",
        "startup_abort_requested",
        "raw_wire_persisted",
        "transcript_opened",
        "grants_authority",
    }
    if type(value) is not dict or set(value) != keys:
        raise ValueError("exact receipt schema required")
    if type(value["schema"]) is not int or value["schema"] != 1 or value["event"] != event:
        raise ValueError("receipt schema or event mismatch")
    sid = value["session_id"]
    if type(sid) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid) is None:
        raise ValueError("receipt session invalid")
    if value["event_scope"] != ("startup" if event == "SessionStart" else "other"):
        raise ValueError("receipt event scope mismatch")
    for key, expected in {
        "cwd_matches_source_project": True,
        "startup_abort_requested": event == "SessionStart",
        "raw_wire_persisted": False,
        "transcript_opened": False,
        "grants_authority": False,
    }.items():
        if value[key] is not expected:
            raise ValueError("receipt boolean contract violated")
    fields = value["known_field_types"]
    required = {
        "session_id",
        "transcript_path",
        "cwd",
        "hook_event_name",
        "source" if event == "SessionStart" else "reason",
    }
    optional = {"model", "permission_mode", "turn_id"}
    if type(fields) is not dict or not required <= fields.keys() <= required | optional:
        raise ValueError("receipt field vocabulary invalid")
    if any(fields[key] != "str" for key in required - {"transcript_path"}) or any(
        item not in ("str", "NoneType") for item in fields.values()
    ):
        raise ValueError("receipt field types invalid")
    return dict(value)


def installation_id_observation(root: Path) -> dict[str, Any]:
    """Audit only the permitted bootstrap file; never echo or hash its ID."""
    _directory(root / "codex")
    path = root / "codex/installation_id"
    before = path.lstat()

    def checked(info: os.stat_result) -> tuple[int, ...]:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o644
            or info.st_size != 36
        ):
            raise ValueError("installation identity metadata invalid")
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    captured = checked(before)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        if checked(os.fstat(stream.fileno())) != captured:
            raise ValueError("installation identity changed before read")
        raw = stream.read(37)
        if checked(os.fstat(stream.fileno())) != captured:
            raise ValueError("installation identity changed during read")
    if checked(path.lstat()) != captured:
        raise ValueError("installation identity changed after read")
    try:
        parsed = uuid.UUID(raw.decode("ascii"))
    except (ValueError, UnicodeError):
        raise ValueError("installation identity is not canonical UUIDv4") from None
    if (
        len(raw) != 36
        or parsed.version != 4
        or parsed.variant != uuid.RFC_4122
        or str(parsed).encode() != raw
    ):
        raise ValueError("installation identity is not canonical UUIDv4")
    return {
        "status": "valid",
        "kind": "canonical-uuid-v4",
        "bytes": 36,
        "mode": "0644",
        "single_link": True,
        "value_or_digest_recorded": False,
    }


class _BsdInfo(ctypes.Structure):
    # Exact macOS SDK sys/proc_info.h proc_bsdinfo, MAXCOMLEN=16.
    _fields_ = (
        [
            (name, ctypes.c_uint32)
            for name in (
                "flags",
                "status",
                "xstatus",
                "pid",
                "ppid",
                "uid",
                "gid",
                "ruid",
                "rgid",
                "svuid",
                "svgid",
                "reserved",
            )
        ]
        + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
        + [
            (name, ctypes.c_uint32)
            for name in (
                "nfiles",
                "pgid",
                "jobc",
                "tdev",
                "tpgid",
            )
        ]
        + [
            ("nice", ctypes.c_int32),
            ("start_sec", ctypes.c_uint64),
            ("start_usec", ctypes.c_uint64),
        ]
    )


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    ppid: int
    pgid: int
    uid: int
    start_sec: int
    start_usec: int
    status: int
    sid: int | None

    def stable(self) -> tuple[int, ...]:
        return (self.pid, self.uid, self.start_sec, self.start_usec)


class _MacProcessTable:
    """Bounded UID-only kernel metadata; never inspect command lines or environments."""

    def __init__(self) -> None:
        if platform.system() != "Darwin" or ctypes.sizeof(_BsdInfo) != 136:
            raise ValueError("reviewed Darwin process layout required")
        if _BsdInfo.start_sec.offset != 120 or _BsdInfo.pgid.offset != 100:
            raise ValueError("reviewed Darwin process offsets required")
        self.uid = os.geteuid()
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.lib.proc_listpids.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        self.lib.proc_listpids.restype = ctypes.c_int
        self.lib.proc_pidinfo.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        self.lib.proc_pidinfo.restype = ctypes.c_int
        self.libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self.libc.waitid.argtypes = (
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        self.libc.waitid.restype = ctypes.c_int

    def pids(self) -> tuple[int, ...]:
        buffer = (ctypes.c_int * PROCESS_CENSUS_CAP)()
        amount = self.lib.proc_listpids(4, self.uid, buffer, ctypes.sizeof(buffer))
        if amount <= 0 or amount >= ctypes.sizeof(buffer) or amount % ctypes.sizeof(ctypes.c_int):
            raise ValueError("owned process census unavailable or capped")
        return tuple(sorted({int(pid) for pid in buffer[: amount // 4] if pid > 0}))

    def read(self, pid: int) -> _ProcessIdentity | None:
        info = _BsdInfo()
        ctypes.set_errno(0)
        amount = self.lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if amount == 0 and ctypes.get_errno() in (errno.ESRCH, errno.ENOENT):
            return None
        if amount != ctypes.sizeof(info):
            raise ValueError("process kernel identity unavailable")
        try:
            sid: int | None = os.getsid(pid)
        except ProcessLookupError:
            if info.status != 5:
                return None
            sid = None  # Darwin can hide an exited, still-unreaped child from getsid.
        return _ProcessIdentity(
            info.pid,
            info.ppid,
            info.pgid,
            info.uid,
            info.start_sec,
            info.start_usec,
            info.status,
            sid,
        )

    def unreaped_child(self, pid: int) -> bool:
        # Darwin libproc stops returning an exited child before its parent reaps it.
        # waitid(WNOWAIT) proves waitability without releasing the PID/SID anchor.
        info = ctypes.create_string_buffer(256)
        ctypes.set_errno(0)
        result = self.libc.waitid(1, pid, info, 0x01 | 0x04 | 0x20)
        if result != 0:
            if ctypes.get_errno() in (errno.ECHILD, errno.ESRCH):
                return False
            raise ValueError("owned child wait identity unavailable")
        return any(info.raw)


class _OwnedSession:
    """Own only the new child session; retain its unreaped leader as the SID anchor."""

    def __init__(self, pid: int, table: _MacProcessTable) -> None:
        self.table = table
        leader = table.read(pid)
        if leader is None and table.unreaped_child(pid):
            leader = _ProcessIdentity(pid, os.getpid(), pid, os.geteuid(), 0, 0, 5, None)
        if (
            leader is None
            or leader.pid <= 1
            or leader.pid == os.getpid()
            or leader.ppid != os.getpid()
            or leader.uid != os.geteuid()
            or leader.pgid != pid
            or leader.sid not in (pid, None)
            or (leader.sid is None and leader.status != 5)
            or os.getsid(0) == pid
        ):
            raise ValueError("owned child session identity required")
        self.leader = leader

    def anchor(self) -> _ProcessIdentity:
        current = self.table.read(self.leader.pid)
        if current is None and self.table.unreaped_child(self.leader.pid):
            return _ProcessIdentity(
                self.leader.pid,
                self.leader.ppid,
                self.leader.pgid,
                self.leader.uid,
                self.leader.start_sec,
                self.leader.start_usec,
                5,
                None,
            )
        if (
            current is None
            or current.stable() != self.leader.stable()
            or current.ppid != os.getpid()
            or current.pgid != self.leader.pid
            or current.sid not in (self.leader.pid, None)
            or (current.sid is None and current.status != 5)
        ):
            raise ValueError("owned session anchor drift")
        return current

    def members(self) -> tuple[_ProcessIdentity, ...]:
        self.anchor()
        result = []
        for pid in self.table.pids():
            current = self.table.read(pid)
            if current is not None and current.sid == self.leader.pid:
                if current.uid != self.leader.uid:
                    raise ValueError("owned session user drift")
                result.append(current)
                if len(result) > OWNED_SESSION_CAP:
                    raise ValueError("owned session member cap")
        self.anchor()
        return tuple(result)

    def signal(self, expected: _ProcessIdentity, value: signal.Signals) -> None:
        self.anchor()
        current = self.table.read(expected.pid)
        if current is None or current.status == 5:
            return
        if (
            current.stable() != expected.stable()
            or current.uid != self.leader.uid
            or current.sid != self.leader.pid
            or current.pgid != expected.pgid
            or current.pid == os.getpid()
        ):
            raise ValueError("owned signal identity drift")
        with contextlib.suppress(ProcessLookupError):
            os.kill(current.pid, value)

    def cleanup(self) -> None:
        deadline = time.monotonic() + CLEANUP_TIMEOUT
        leader = self.anchor()
        if leader.status not in (4, 5):
            # Stop the only process that can create additional hook groups before census.
            self.signal(leader, signal.SIGSTOP)
        previous: tuple[tuple[int, ...], ...] | None = None
        while time.monotonic() < deadline:
            members = self.members()
            for member in members:
                if member.status not in (4, 5):
                    self.signal(member, signal.SIGSTOP)
            current = self.members()
            identities = tuple(member.stable() for member in current)
            if identities == previous and all(member.status in (4, 5) for member in current):
                break
            previous = identities
            time.sleep(0.01)
        else:
            raise ValueError("owned session cleanup did not quiesce")
        # Stopped members cannot create another hook group between census and kill.
        for member in current:
            if member.pid != self.leader.pid:
                self.signal(member, signal.SIGKILL)
        while time.monotonic() < deadline:
            survivors = [
                item for item in self.members() if item.pid != self.leader.pid and item.status != 5
            ]
            if not survivors:
                self.signal(self.anchor(), signal.SIGKILL)
                return
            time.sleep(0.01)
        raise ValueError("owned session cleanup survivors remain")


def _emergency_cleanup(process: subprocess.Popen[bytes], table: _MacProcessTable) -> None:
    """Cleanup the unreaped direct-child session when the richer anchor cannot be built."""
    deadline = time.monotonic() + CLEANUP_TIMEOUT
    failed = False

    def session_members() -> tuple[_ProcessIdentity, ...]:
        result = []
        for pid in table.pids():
            current = table.read(pid)
            if current is not None and current.sid == process.pid:
                if current.uid != os.geteuid():
                    raise ValueError("emergency owned session user drift")
                result.append(current)
                if len(result) > OWNED_SESSION_CAP:
                    raise ValueError("emergency owned session member cap")
        return tuple(result)

    def checked_signal(expected: _ProcessIdentity, value: signal.Signals) -> None:
        current = table.read(expected.pid)
        if current is None or current.status == 5:
            return
        if (
            current.stable() != expected.stable()
            or current.uid != expected.uid
            or current.sid != process.pid
            or current.pgid != expected.pgid
            or current.pid == os.getpid()
        ):
            raise ValueError("emergency signal identity drift")
        with contextlib.suppress(ProcessLookupError):
            os.kill(current.pid, value)

    try:
        # Popen's unreaped direct child cannot be PID-reused, even before rich admission.
        with contextlib.suppress(ProcessLookupError):
            os.kill(process.pid, signal.SIGSTOP)
        previous: tuple[tuple[int, ...], ...] | None = None
        while time.monotonic() < deadline:
            members = session_members()
            for member in members:
                if member.pid != process.pid and member.status not in (4, 5):
                    checked_signal(member, signal.SIGSTOP)
            current = session_members()
            identities = tuple(member.stable() for member in current)
            if identities == previous and all(member.status in (4, 5) for member in current):
                break
            previous = identities
            time.sleep(0.01)
        else:
            raise ValueError("emergency owned session did not quiesce")
        for member in current:
            if member.pid != process.pid:
                checked_signal(member, signal.SIGKILL)
        while time.monotonic() < deadline:
            survivors = [
                item for item in session_members() if item.pid != process.pid and item.status != 5
            ]
            if not survivors:
                break
            time.sleep(0.01)
        else:
            raise ValueError("emergency owned session survivors remain")
    except (ValueError, OSError):
        failed = True
    finally:
        # This exact PID is still our unreaped Popen child, so this fallback cannot hit reuse.
        with contextlib.suppress(ProcessLookupError):
            os.kill(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            failed = True
    if failed:
        raise ValueError("emergency owned session cleanup failed")


def _close_process_streams(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _collect(argv: list[str], environment: dict[str, str]) -> tuple[int, bytes, bytes]:
    table = _MacProcessTable()  # Layout and census admission before creating the child.
    table.pids()
    outputs = [bytearray(), bytearray()]
    deadline = time.monotonic() + TIMEOUT
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            argv,
            cwd=PROJECT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except BaseException:
        selector.close()
        raise
    try:
        with contextlib.suppress(ProcessLookupError):
            os.kill(process.pid, signal.SIGSTOP)
        owned = _OwnedSession(process.pid, table)
    except (ValueError, OSError):
        try:
            _emergency_cleanup(process, table)
        finally:
            selector.close()
            _close_process_streams(process)
        raise ValueError("owned session admission failed") from None
    try:
        os.kill(process.pid, signal.SIGCONT)
        for index, stream in enumerate((process.stdout, process.stderr)):
            assert stream is not None
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, index)
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise ValueError("native hook observation timeout")
            for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    outputs[key.data].extend(chunk)
                    if sum(map(len, outputs)) > OUTPUT_CAP:
                        raise ValueError("native hook observation output cap")
        # Do not poll/wait/reap yet: its PID must remain reserved through session cleanup.
        while owned.anchor().status != 5:
            if time.monotonic() >= deadline:
                raise ValueError("native hook observation timeout")
            time.sleep(0.01)
    finally:
        cleanup_failed = False
        try:
            owned.cleanup()
        except (ValueError, OSError):
            cleanup_failed = True
            with contextlib.suppress(ValueError, OSError, subprocess.SubprocessError):
                _emergency_cleanup(process, table)
        finally:
            # Always terminate the exact unreaped direct child, even if rich cleanup failed.
            with contextlib.suppress(ProcessLookupError):
                os.kill(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                cleanup_failed = True
            selector.close()
            _close_process_streams(process)
        if cleanup_failed:
            raise ValueError("owned session cleanup failed") from None
    return process.returncode, bytes(outputs[0]), bytes(outputs[1])


def diagnostic_lines(stdout: bytes, stderr: bytes, root: Path) -> dict[str, Any]:
    """Finite diagnostic vocabulary only; no raw text, paths or output digests."""
    if type(stdout) is not bytes or type(stderr) is not bytes:
        raise ValueError("diagnostic input must be bytes")
    if len(stdout) + len(stderr) > OUTPUT_CAP:
        raise ValueError("diagnostic byte cap exceeded")
    if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
        raise ValueError("diagnostic root must be canonical absolute Path")
    resources = (
        ("probe-codex-temp", root / "codex/tmp"),
        ("probe-codex-config", root / "codex/config.toml"),
        ("probe-codex-home", root / "codex"),
        ("probe-runtime", root / "runtime"),
        ("source-project-root", PROJECT),
        ("source-project-parent", PROJECT.parent),
        ("source-project-config", PROJECT / ".codex/config.toml"),
        ("live-codex-home", Path("/Users/mkaracan/.codex")),
        ("system-passwd", Path("/private/etc/passwd")),
        ("system-cryptex-os", Path("/System/Volumes/Preboot/Cryptexes/OS")),
    )
    patterns = tuple(
        (
            tag,
            re.compile(
                rb"(?<![A-Za-z0-9_./-])" + re.escape(str(path).encode()) + rb"(?![A-Za-z0-9_./-])"
            ),
        )
        for tag, path in resources
    )
    warning_tags: set[str] = set()
    fatal_tags: set[str] = set()
    warning_resources: set[str] = set()
    fatal_resources: set[str] = set()
    warnings = errors = unclassified = scanned = 0
    path_alias_warning = generic_eperm = False
    lines = stdout.splitlines() + stderr.splitlines()
    for raw in lines[:DIAGNOSTIC_LINE_CAP]:
        scanned += 1
        line = raw.strip()
        lowered = line.lower()
        if line.startswith(b"WARNING:"):
            warnings += 1
            body = lowered[len(b"WARNING:") :].lstrip()
            tags, selected_resources = warning_tags, warning_resources
            if line.startswith(PATH_ALIAS_WARNING):
                path_alias_warning = True
                tags.add("path-alias-creation")
        elif line.startswith(b"Error:") or lowered.startswith(b"error loading config"):
            errors += 1
            body = lowered[len(b"Error:") :].lstrip() if line.startswith(b"Error:") else lowered
            tags, selected_resources = fatal_tags, fatal_resources
            if body.startswith((b"operation not permitted", b"permission denied")):
                generic_eperm = True
                tags.add("generic-permission-denied")
        else:
            unclassified += 1
            continue
        for tag, prefixes in DIAGNOSTIC_PREFIXES:
            if body.startswith(prefixes):
                tags.add(tag)
        for tag, pattern in patterns:
            if pattern.search(line) is not None:
                selected_resources.add(tag)
    return {
        "warning_line_count": warnings,
        "error_line_count": errors,
        "unclassified_line_count": unclassified,
        "lines_scanned": scanned,
        "line_limit_reached": len(lines) > DIAGNOSTIC_LINE_CAP,
        "path_alias_warning_present": path_alias_warning,
        "fatal_generic_eperm_present": generic_eperm,
        "warning_tags": sorted(warning_tags),
        "fatal_tags": sorted(fatal_tags),
        "warning_resource_tags": sorted(warning_resources),
        "fatal_resource_tags": sorted(fatal_resources),
        "raw_content_persisted": False,
    }


def run_reviewed_probe(root: Path, reviewed_digest: str) -> dict[str, Any]:
    """One externally approved invocation; missing natural events is a failed gate."""
    plan = _validate(root, reviewed_digest, pristine=True)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _new_file(directory, "attempt.json", canonical({"reviewed_plan_digest": reviewed_digest}))
        stdout = stderr = b""
        returncode: int | None = None
        output_complete = False
        failure: str | None = None
        try:
            _validate(root, reviewed_digest, pristine=True)
            returncode, stdout, stderr = _collect(
                _load(root / "argv.json"), _load(root / "environment.json")
            )
            output_complete = True
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            failure = {
                "native hook observation timeout": "native-timeout",
                "native hook observation output cap": "native-output-cap",
                "owned session cleanup failed": "owned-session-cleanup-failed",
                "owned session admission failed": "owned-session-admission-failed",
            }.get(str(error), "bounded-native-invocation-failed")
        try:
            _validate(root, reviewed_digest)
        except (ValueError, OSError):
            failure = "post-invocation-identity-drift"
        try:
            installation = installation_id_observation(root)
        except FileNotFoundError:
            installation = {"status": "missing", "value_or_digest_recorded": False}
            failure = failure or "installation-identity-missing"
        except (ValueError, OSError):
            installation = {"status": "invalid", "value_or_digest_recorded": False}
            failure = "installation-identity-invalid"
        receipts: list[dict[str, Any]] = []
        for event in ("SessionStart", "SessionEnd"):
            try:
                receipt = _receipt(root / "receipts" / (event + ".json"), event)
                receipts.append(receipt)
            except (ValueError, OSError, KeyError, TypeError):
                failure = failure or "required-natural-hook-missing-or-invalid"
        if len(receipts) == 2 and receipts[0]["session_id"] != receipts[1]["session_id"]:
            failure = "natural-hook-session-mismatch"
        result = {
            "schema": 1,
            "scope": plan["scope"],
            "reviewed_plan_digest": reviewed_digest,
            "native_sha256": NATIVE_SHA,
            "returncode": returncode,
            "output_capture": "complete" if output_complete else "unavailable",
            "stdout_bytes": len(stdout) if output_complete else None,
            "stderr_bytes": len(stderr) if output_complete else None,
            "natural_events": [receipt["event"] for receipt in receipts],
            "startup_stop_marker_observed": (
                STOP_MARKER.encode() in stdout + stderr if output_complete else None
            ),
            "diagnostic_lines": (
                diagnostic_lines(stdout, stderr, root) if output_complete else None
            ),
            "installation_identity": installation,
            "diagnostic_categories": sorted(
                category
                for category, markers in {
                    "kernel-permission-denied": (b"Operation not permitted", b"Permission denied"),
                    "cli-argument-rejected": (b"unexpected argument", b"unrecognized option"),
                    "local-state-initialization-mentioned": (b"database", b"SQLite", b"sqlite"),
                    "auth-required": (b"not logged in", b"authentication required"),
                }.items()
                if any(marker in stdout + stderr for marker in markers)
            )
            if output_complete
            else None,
            "failure": failure,
            "passed": failure is None,
            "network_permitted": False,
            "real_model_proven": False,
            "full_hydration_proven": False,
            "compaction_proven": False,
            "lifecycle_accepted": False,
            "raw_output_persisted": False,
            "same_uid_execution_atomicity_claimed": False,
        }
        _new_file(directory, "result.json", canonical(result))
        os.fsync(directory)
    finally:
        os.close(directory)
    if not result["passed"]:
        raise AssertionError(
            "Natural Mac hook gate failed; inspect content-free external result.json"
        )
    return result
