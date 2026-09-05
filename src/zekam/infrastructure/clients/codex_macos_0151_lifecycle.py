"""Dormant, exact Codex 0.151 macOS lifecycle boundary.

This module parses content-free structural hook input and renders bounded hook
output.  It does not install hooks, allocate continuity authority, or write any
runtime or project state.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import json
import os
import platform
import pwd
import re
import stat
import sys
import weakref
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, final
from uuid import UUID, uuid5

from zekam.application.context_ranking import count_context_tokens
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_v4_ingress import (
    MAX_ADDITIONAL_CONTEXT_UTF8_BYTES,
    MAX_SESSION_START_SUCCESS_STDOUT_UTF8_BYTES,
    ManagedInvocationSnapshot,
    ManagedProcessSnapshot,
)
from zekam.application.local_hook_command_contract import (
    NATIVE_DOUBLE_EXEC_TOPOLOGY,
    ReviewedHookCommand,
)
from zekam.application.secret_detection import SECRET_RULES, scan_text
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

if TYPE_CHECKING:
    from zekam.application.local_continuity_v4_compaction import (
        SealedPreCompactionDeadline,
    )

CODEX_MACOS_0151_VERSION = "0.151.0"
CODEX_MACOS_0151_NATIVE_SHA256 = "98491713ffb196061003ee148636e743997cc31d76144ba7c53462269896891d"
CODEX_MACOS_0151_CONTRACT_SCHEMA = "zekam-codex-macos-0151-command-hook/v1"

_MAX_INPUT_BYTES = 65_536
_MAX_TRANSCRIPT_BYTES = 4_096
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/-]{1,512}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_EVENT_MAPPING = {
    "SessionStart": "SESSION_START",
    "PreCompact": "PRE_COMPACTION",
    "PostCompact": "POST_COMPACTION",
}
_PERMISSION_MODES = {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}
_START_SOURCES = {"startup", "resume", "clear", "compact"}
_TRIGGERS = {"manual", "auto"}
_BASE_KEYS = {"session_id", "transcript_path", "cwd", "hook_event_name"}
_NAMESPACE = UUID("018f0000-0000-7000-8000-000000000151")
_SHELL_PATH = Path("/bin/zsh")
_ARTIFACT_PINS: Final = (
    ("native", 231_563_824, CODEX_MACOS_0151_NATIVE_SHA256),
    ("shell", 1_357_312, "3fbc7a357f2cc9ee90b975f76c27744c19a051a5922fe59c5c8a3ac7a981ffc5"),
    ("launcher", 34_640, "30b2a6bf9706029b609327ca5db722cd458239df96efc87e1f8e37beed49db2e"),
    ("runtime", 33_568, "0467c7061b8f4b4e08cfe72b80da9fb3928cb11d0fbc81f51b571922c377eabb"),
)
_TRUSTED_MANAGERS: weakref.WeakSet[TrustedCodex0151ProcessManager]


def _artifact_paths() -> tuple[Path, Path, Path, Path]:
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    native = (
        home
        / ".local/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64"
        / "vendor/aarch64-apple-darwin/bin/codex"
    )
    launcher = Path(getattr(sys, "_base_executable", sys.executable))
    runtime = launcher.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
    return native, _SHELL_PATH, launcher, runtime


# Read-only compatibility names for diagnostics and pinned-binary acceptance tests.
_NATIVE_PATH, _, _LAUNCHER_PATH, _RUNTIME_PATH = _artifact_paths()


class LiveProcessVerificationError(PolicyViolation):
    """Bounded transient diagnosis; codes are never canonical or persisted."""

    def __init__(self, codes: tuple[str, ...]) -> None:
        allowed = {
            "native-not-live",
            "native-pid",
            "native-uid",
            "native-start-token",
            "native-artifact",
            "native-parent",
            "hook-not-live",
            "hook-pid",
            "hook-uid",
            "hook-start-token",
            "hook-parent",
            "shell-artifact",
            "python-launcher-artifact",
            "python-runtime-artifact",
            "reviewed-command",
            "hook-set",
            "ancestry-policy",
            "exec-preserved-tuple",
        }
        if (
            type(codes) is not tuple
            or not 1 <= len(codes) <= 18
            or tuple(sorted(codes)) != codes
            or len(set(codes)) != len(codes)
            or any(type(code) is not str or code not in allowed for code in codes)
            or sum(len(code.encode("utf-8")) for code in codes) > 1024
        ):
            raise ValidationFailed("Codex 0.151 bounded process drift codes required")
        self.codes = codes
        super().__init__("Codex 0.151 live manager verification failed")


def _utc_second() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(timespec="seconds")


def _raw_file_digest(path: Path, *, deadline: SealedPreCompactionDeadline | None = None) -> str:
    if deadline is not None:
        from zekam.application.local_continuity_v4_compaction import (
            SealedPreCompactionDeadline,
        )

        if type(deadline) is not SealedPreCompactionDeadline:
            raise ValidationFailed("Codex 0.151 exact PreCompact deadline required")
    try:
        if deadline is not None:
            deadline.require_current()
        before = path.stat(follow_symlinks=False)
        if not path.is_file() or before.st_size < 1 or before.st_size > 536_870_912:
            raise OSError
        value = hashlib.sha256()
        with path.open("rb", buffering=0) as source:
            while chunk := source.read(1_048_576):
                if deadline is not None:
                    deadline.require_current()
                value.update(chunk)
        if deadline is not None:
            deadline.require_current()
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise PolicyViolation("Codex 0.151 reviewed artifact unavailable") from exc
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise PolicyViolation("Codex 0.151 reviewed artifact changed during capture")
    return f"sha256:{value.hexdigest()}"


@final
class _PinnedArtifactSet:
    """Four exact, hash-once artifact descriptors retained by a service generation."""

    __slots__ = ("_closed", "_descriptors", "_identities", "_paths", "_values")

    def __init__(self, paths: tuple[Path, Path, Path, Path] | None = None) -> None:
        selected = _artifact_paths() if paths is None else paths
        if type(selected) is not tuple or len(selected) != 4:
            raise ValidationFailed("Codex 0.151 exact four artifact paths required")
        descriptors: list[int] = []
        identities: list[tuple[int, int, int, int, int, int]] = []
        values: list[str] = []
        try:
            for path, (_label, expected_size, expected_digest) in zip(
                selected, _ARTIFACT_PINS, strict=True
            ):
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                descriptors.append(descriptor)
                before = os.fstat(descriptor)
                current = path.stat(follow_symlinks=False)
                identity = (
                    before.st_dev,
                    before.st_ino,
                    before.st_uid,
                    before.st_mode,
                    before.st_nlink,
                    before.st_size,
                )
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid not in {0, os.geteuid()}
                    or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    or before.st_nlink != 1
                    or before.st_size != expected_size
                    or identity
                    != (
                        current.st_dev,
                        current.st_ino,
                        current.st_uid,
                        current.st_mode,
                        current.st_nlink,
                        current.st_size,
                    )
                ):
                    raise PolicyViolation("Codex 0.151 reviewed artifact identity rejected")
                value = hashlib.sha256()
                offset = 0
                while offset < expected_size:
                    chunk = os.pread(descriptor, min(1_048_576, expected_size - offset), offset)
                    if not chunk:
                        raise PolicyViolation("Codex 0.151 reviewed artifact short read")
                    value.update(chunk)
                    offset += len(chunk)
                after = os.fstat(descriptor)
                after_identity = (
                    after.st_dev,
                    after.st_ino,
                    after.st_uid,
                    after.st_mode,
                    after.st_nlink,
                    after.st_size,
                )
                if value.hexdigest() != expected_digest or after_identity != identity:
                    raise PolicyViolation("Codex 0.151 reviewed artifact digest rejected")
                identities.append(identity)
                values.append(f"sha256:{expected_digest}")
        except BaseException:
            for descriptor in descriptors:
                with suppress(OSError):
                    os.close(descriptor)
            raise
        self._paths, self._descriptors = selected, tuple(descriptors)
        self._identities, self._values, self._closed = tuple(identities), tuple(values), False

    def recheck(self) -> tuple[str, str, str, str]:
        if self._closed:
            raise PolicyViolation("Codex 0.151 reviewed artifact set closed")
        for path, descriptor, expected in zip(
            self._paths, self._descriptors, self._identities, strict=True
        ):
            held = os.fstat(descriptor)
            current = path.stat(follow_symlinks=False)
            observed = (
                held.st_dev,
                held.st_ino,
                held.st_uid,
                held.st_mode,
                held.st_nlink,
                held.st_size,
            )
            path_observed = (
                current.st_dev,
                current.st_ino,
                current.st_uid,
                current.st_mode,
                current.st_nlink,
                current.st_size,
            )
            if observed != expected or path_observed != expected:
                raise LiveProcessVerificationError(("native-artifact",))
        return (self._values[0], self._values[1], self._values[2], self._values[3])

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            for descriptor in self._descriptors:
                os.close(descriptor)


def _process_row(pid: int, *, timeout: float = 3.0) -> tuple[int, int, str, Path]:
    if type(pid) is not int or pid < 1 or type(timeout) not in {int, float} or timeout <= 0:
        raise LiveProcessVerificationError(("native-pid",))
    if platform.system() != "Darwin" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise LiveProcessVerificationError(("native-not-live",))

    class BsdInfo(ctypes.Structure):
        _fields_ = [
            ("flags", ctypes.c_uint32),
            ("status", ctypes.c_uint32),
            ("xstatus", ctypes.c_uint32),
            ("pid", ctypes.c_uint32),
            ("ppid", ctypes.c_uint32),
            ("uid", ctypes.c_uint32),
            ("gid", ctypes.c_uint32),
            ("ruid", ctypes.c_uint32),
            ("rgid", ctypes.c_uint32),
            ("svuid", ctypes.c_uint32),
            ("svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("comm", ctypes.c_char * 16),
            ("name", ctypes.c_char * 32),
            ("nfiles", ctypes.c_uint32),
            ("pgid", ctypes.c_uint32),
            ("jobc", ctypes.c_uint32),
            ("tdev", ctypes.c_uint32),
            ("tpgid", ctypes.c_uint32),
            ("nice", ctypes.c_int32),
            ("start_sec", ctypes.c_uint64),
            ("start_usec", ctypes.c_uint64),
        ]

    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        info = BsdInfo()
        lib.proc_pidinfo.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        lib.proc_pidinfo.restype = ctypes.c_int
        if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(
            info
        ):
            raise OSError
        buffer = ctypes.create_string_buffer(4096)
        lib.proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        lib.proc_pidpath.restype = ctypes.c_int
        size = lib.proc_pidpath(pid, buffer, len(buffer))
        if not 1 <= size < len(buffer):
            raise OSError
        executable = Path(buffer.raw[:size].decode("utf-8")).resolve(strict=True)
        parent, uid = int(info.ppid), int(info.uid)
        start = f"{int(info.start_sec)}:{int(info.start_usec)}"
    except (UnicodeDecodeError, ValueError, OSError) as exc:
        raise LiveProcessVerificationError(("native-not-live",)) from exc
    if parent < 0 or uid < 0 or not start or len(start.encode("utf-8")) > 512:
        raise LiveProcessVerificationError(("native-not-live",))
    return parent, uid, start, executable


@final
class TrustedCodex0151ProcessManager:
    """Fixed macOS process/artifact verifier; it accepts no caller evidence DTO."""

    __slots__ = ("__weakref__", "_artifact_pins", "_peer_identity")

    def __init__(self, artifact_pins: _PinnedArtifactSet | None = None) -> None:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise PolicyViolation("Codex 0.151 trusted manager requires Darwin arm64")
        if artifact_pins is not None and type(artifact_pins) is not _PinnedArtifactSet:
            raise ValidationFailed("Codex 0.151 exact artifact pins required")
        self._artifact_pins = artifact_pins
        self._peer_identity: tuple[int, int, str, str, str] | None = None
        _TRUSTED_MANAGERS.add(self)

    def _hook_row(self, deadline: SealedPreCompactionDeadline | None) -> tuple[int, int, str, Path]:
        pid = os.getpid() if self._peer_identity is None else self._peer_identity[0]
        row = (
            _process_row(pid)
            if deadline is None
            else _process_row(pid, timeout=deadline.remaining_seconds())
        )
        if self._peer_identity is not None:
            _parent, uid, start, executable = row
            peer_pid, peer_uid, peer_start, peer_artifact, _audit = self._peer_identity
            if (
                pid != peer_pid
                or uid != peer_uid
                or start != peer_start
                or _raw_file_digest(executable, deadline=deadline) != peer_artifact
            ):
                raise LiveProcessVerificationError(("hook-not-live",))
        return row

    @staticmethod
    def recovery_time() -> str:
        return _utc_second()

    def _artifacts(
        self,
        deadline: SealedPreCompactionDeadline | None = None,
    ) -> tuple[str, str, str, str]:
        if self._artifact_pins is not None:
            if deadline is not None:
                deadline.require_current()
            return self._artifact_pins.recheck()
        native_path, shell_path, launcher_path, runtime_path = _artifact_paths()

        def read(path: Path) -> str:
            return (
                _raw_file_digest(path)
                if deadline is None
                else _raw_file_digest(path, deadline=deadline)
            )

        native = read(native_path)
        if native != f"sha256:{CODEX_MACOS_0151_NATIVE_SHA256}":
            raise LiveProcessVerificationError(("native-artifact",))
        return (
            native,
            read(shell_path),
            read(launcher_path),
            read(runtime_path),
        )

    @staticmethod
    def _attachment(binding: ContinuityBinding) -> str:
        return str(uuid5(_NAMESPACE, f"attachment|{binding.session_id}|{binding.binding_digest}"))

    def capture_process(
        self,
        binding: ContinuityBinding,
        *,
        deadline: SealedPreCompactionDeadline | None = None,
    ) -> ManagedProcessSnapshot:
        if not _trusted_process_owner(self):
            raise PolicyViolation("Codex 0.151 unsealed process manager")
        if type(binding) is not ContinuityBinding:
            raise ValidationFailed("Codex 0.151 exact binding required")
        parent_pid = os.getppid() if self._peer_identity is None else self._hook_row(deadline)[0]
        process_row = (
            _process_row(parent_pid)
            if deadline is None
            else _process_row(parent_pid, timeout=deadline.remaining_seconds())
        )
        _grandparent, native_uid, native_start, executable = process_row
        native_path, shell_path, launcher_path, runtime_path = _artifact_paths()
        if executable != native_path.resolve(strict=True) or native_uid != os.geteuid():
            raise LiveProcessVerificationError(("native-parent",))
        native, shell, launcher, runtime = self._artifacts(deadline)
        captured = _utc_second()
        attachment = self._attachment(binding)
        client = digest(
            {
                "client": "codex",
                "version": CODEX_MACOS_0151_VERSION,
                "platform": "darwin-arm64",
                "native_artifact_digest": native,
            }
        )
        hook_set = digest(("SessionStart", "PreCompact", "PostCompact"))
        ancestry = digest(_TOPOLOGY if (_TOPOLOGY := NATIVE_DOUBLE_EXEC_TOPOLOGY) else "")
        commands = tuple(
            ReviewedHookCommand(
                attachment,
                event,
                NATIVE_DOUBLE_EXEC_TOPOLOGY,
                client,
                hook_set,
                shell,
                launcher,
                runtime,
                digest(
                    {
                        "event": event,
                        "argv": (str(shell_path), str(launcher_path), str(runtime_path)),
                    }
                ),
                digest("workspace-write-no-network"),
                captured,
            )
            for event in ("SessionStart", "PreCompact", "PostCompact")
        )
        process = object.__new__(ManagedProcessSnapshot)
        object.__setattr__(process, "attachment_id", attachment)
        object.__setattr__(process, "captured_at", captured)
        object.__setattr__(process, "native_pid", parent_pid)
        object.__setattr__(process, "native_uid", native_uid)
        object.__setattr__(process, "native_start_token", native_start)
        object.__setattr__(process, "native_artifact_digest", native)
        object.__setattr__(process, "client_contract_digest", client)
        object.__setattr__(process, "hook_set_digest", hook_set)
        object.__setattr__(process, "ancestry_policy_digest", ancestry)
        object.__setattr__(process, "reviewed_commands", commands)
        process.__post_init__()
        return process

    @staticmethod
    def _generation(process: ManagedProcessSnapshot) -> str:
        managed_body = {
            "ancestry_policy_digest": process.ancestry_policy_digest,
            "attachment_id": process.attachment_id,
            "created_at": process.captured_at,
            "hook_set_digest": process.hook_set_digest,
            "native_artifact_digest": process.native_artifact_digest,
            "native_pid": process.native_pid,
            "native_start_token": process.native_start_token,
            "native_uid": process.native_uid,
            "predecessor_process_generation_digest": None,
            "transition_kind": "initial-attach",
        }
        return digest(
            {
                "ancestry_policy_digest": process.ancestry_policy_digest,
                "attachment_id": process.attachment_id,
                "created_at": process.captured_at,
                "generation": 1,
                "hook_set_digest": process.hook_set_digest,
                "managed_launch_receipt_digest": digest(managed_body),
                "native_artifact_digest": process.native_artifact_digest,
                "native_pid": process.native_pid,
                "native_start_token": process.native_start_token,
                "native_uid": process.native_uid,
                "previous_process_generation_digest": None,
            }
        )

    def capture_invocation(
        self,
        binding: ContinuityBinding,
        observation: dict[str, object],
        spool_digest: str,
        observed_at: str,
        expected_process_generation_digest: str,
        expected_generation_created_at: str,
        expected_managed_receipt_digest: str,
        expected_launch_command: ReviewedHookCommand,
        expected_ancestry_policy_digest: str,
    ) -> ManagedInvocationSnapshot:
        return self._capture_invocation(
            binding,
            observation,
            spool_digest,
            observed_at,
            expected_process_generation_digest,
            expected_generation_created_at,
            expected_managed_receipt_digest,
            expected_launch_command,
            expected_ancestry_policy_digest,
            event_type="SessionStart",
            deadline=None,
        )

    def capture_precompaction_invocation(
        self,
        binding: ContinuityBinding,
        observation: dict[str, object],
        spool_digest: str,
        observed_at: str,
        expected_process_generation_digest: str,
        expected_generation_created_at: str,
        expected_managed_receipt_digest: str,
        expected_launch_command: ReviewedHookCommand,
        expected_ancestry_policy_digest: str,
        deadline: SealedPreCompactionDeadline,
        *,
        expected_generation_number: int = 1,
        expected_previous_generation_digest: str | None = None,
        expected_transition_kind: str = "initial-attach",
    ) -> ManagedInvocationSnapshot:
        from zekam.application.local_continuity_v4_compaction import (
            SealedPreCompactionDeadline,
        )

        if type(deadline) is not SealedPreCompactionDeadline:
            raise ValidationFailed("Codex 0.151 exact PreCompact deadline required")
        deadline.remaining_seconds()
        result = self._capture_invocation(
            binding,
            observation,
            spool_digest,
            observed_at,
            expected_process_generation_digest,
            expected_generation_created_at,
            expected_managed_receipt_digest,
            expected_launch_command,
            expected_ancestry_policy_digest,
            event_type="PreCompact",
            deadline=deadline,
            expected_generation_number=expected_generation_number,
            expected_previous_generation_digest=expected_previous_generation_digest,
            expected_transition_kind=expected_transition_kind,
        )
        deadline.remaining_seconds()
        return result

    def _capture_invocation(
        self,
        binding: ContinuityBinding,
        observation: dict[str, object],
        spool_digest: str,
        observed_at: str,
        expected_process_generation_digest: str,
        expected_generation_created_at: str,
        expected_managed_receipt_digest: str,
        expected_launch_command: ReviewedHookCommand,
        expected_ancestry_policy_digest: str,
        *,
        event_type: str,
        deadline: SealedPreCompactionDeadline | None,
        expected_generation_number: int = 1,
        expected_previous_generation_digest: str | None = None,
        expected_transition_kind: str = "initial-attach",
    ) -> ManagedInvocationSnapshot:
        if not _trusted_process_owner(self):
            raise PolicyViolation("Codex 0.151 unsealed process manager")
        if event_type not in {"SessionStart", "PreCompact"}:
            raise ValidationFailed("Codex 0.151 invocation event invalid")
        if deadline is not None:
            deadline.remaining_seconds()
        process = self.capture_process(binding, deadline=deadline)
        process_row = self._hook_row(deadline)
        _parent, hook_uid, hook_start, runtime_path = process_row
        native, shell, launcher, runtime = self._artifacts(deadline)
        if runtime_path != _artifact_paths()[3]:
            raise LiveProcessVerificationError(("python-runtime-artifact",))
        delivery_body = {
            "schema": "zekam-codex-0151-delivery/v1",
            "session_id": binding.external_session_id,
            "external_event_type": event_type,
            "wire_digest": observation["wire_digest"],
        }
        if event_type == "PreCompact":
            delivery_body.update(turn_id=observation["turn_id"], trigger=observation["trigger"])
        delivery = digest(delivery_body)
        reviewed = {item.external_event_type: item for item in process.reviewed_commands}[
            event_type
        ]
        if type(expected_launch_command) is not ReviewedHookCommand:
            raise ValidationFailed("Codex 0.151 exact reviewed command required")
        expected_launch_command.__post_init__()
        rebuilt_command = ReviewedHookCommand(
            attachment_id=process.attachment_id,
            external_event_type=event_type,
            topology=NATIVE_DOUBLE_EXEC_TOPOLOGY,
            client_contract_digest=process.client_contract_digest,
            hook_set_digest=process.hook_set_digest,
            shell_artifact_digest=shell,
            python_launcher_artifact_digest=launcher,
            python_runtime_artifact_digest=runtime,
            argv_recipe_digest=expected_launch_command.argv_recipe_digest,
            sandbox_profile_digest=expected_launch_command.sandbox_profile_digest,
            created_at=expected_launch_command.created_at,
        )
        current_command_scope = (
            reviewed.external_event_type,
            reviewed.topology,
            reviewed.client_contract_digest,
            reviewed.hook_set_digest,
            reviewed.shell_artifact_digest,
            reviewed.python_launcher_artifact_digest,
            reviewed.python_runtime_artifact_digest,
            reviewed.argv_recipe_digest,
            reviewed.sandbox_profile_digest,
        )
        expected_command_scope = (
            rebuilt_command.external_event_type,
            rebuilt_command.topology,
            rebuilt_command.client_contract_digest,
            rebuilt_command.hook_set_digest,
            rebuilt_command.shell_artifact_digest,
            rebuilt_command.python_launcher_artifact_digest,
            rebuilt_command.python_runtime_artifact_digest,
            rebuilt_command.argv_recipe_digest,
            rebuilt_command.sandbox_profile_digest,
        )
        if (
            process.ancestry_policy_digest != expected_ancestry_policy_digest
            or rebuilt_command.command_digest != expected_launch_command.command_digest
            or current_command_scope != expected_command_scope
        ):
            raise LiveProcessVerificationError(("ancestry-policy", "hook-set", "reviewed-command"))
        if (
            type(expected_generation_number) is not int
            or not 1 <= expected_generation_number <= 64
            or (
                expected_generation_number == 1
                and (
                    expected_previous_generation_digest is not None
                    or expected_transition_kind != "initial-attach"
                )
            )
            or (
                expected_generation_number > 1
                and (
                    type(expected_previous_generation_digest) is not str
                    or expected_transition_kind not in {"orderly-reattach", "recovery-reattach"}
                )
            )
        ):
            raise ValidationFailed("Codex 0.151 expected generation relation invalid")
        rebuilt_managed_receipt = digest(
            {
                "ancestry_policy_digest": process.ancestry_policy_digest,
                "attachment_id": process.attachment_id,
                "created_at": expected_generation_created_at,
                "hook_set_digest": process.hook_set_digest,
                "native_artifact_digest": process.native_artifact_digest,
                "native_pid": process.native_pid,
                "native_start_token": process.native_start_token,
                "native_uid": process.native_uid,
                "predecessor_process_generation_digest": expected_previous_generation_digest,
                "transition_kind": expected_transition_kind,
            }
        )
        if rebuilt_managed_receipt != expected_managed_receipt_digest:
            raise LiveProcessVerificationError(("exec-preserved-tuple",))
        rebuilt_generation = digest(
            {
                "ancestry_policy_digest": process.ancestry_policy_digest,
                "attachment_id": process.attachment_id,
                "created_at": expected_generation_created_at,
                "generation": expected_generation_number,
                "hook_set_digest": process.hook_set_digest,
                "managed_launch_receipt_digest": rebuilt_managed_receipt,
                "native_artifact_digest": process.native_artifact_digest,
                "native_pid": process.native_pid,
                "native_start_token": process.native_start_token,
                "native_uid": process.native_uid,
                "previous_process_generation_digest": expected_previous_generation_digest,
            }
        )
        if rebuilt_generation != expected_process_generation_digest:
            raise LiveProcessVerificationError(("exec-preserved-tuple",))
        invocation = object.__new__(ManagedInvocationSnapshot)
        object.__setattr__(invocation, "delivery_id", delivery)
        object.__setattr__(invocation, "observed_at", observed_at)
        object.__setattr__(invocation, "process_generation_digest", rebuilt_generation)
        object.__setattr__(invocation, "ancestry_policy_digest", process.ancestry_policy_digest)
        object.__setattr__(invocation, "native_pid", process.native_pid)
        object.__setattr__(invocation, "native_uid", process.native_uid)
        object.__setattr__(invocation, "native_start_token", process.native_start_token)
        object.__setattr__(invocation, "native_artifact_digest", native)
        object.__setattr__(
            invocation,
            "hook_pid",
            os.getpid() if self._peer_identity is None else self._peer_identity[0],
        )
        object.__setattr__(invocation, "hook_uid", hook_uid)
        object.__setattr__(invocation, "hook_start_token", hook_start)
        object.__setattr__(invocation, "shell_artifact_digest", shell)
        object.__setattr__(invocation, "python_launcher_artifact_digest", launcher)
        object.__setattr__(invocation, "python_runtime_artifact_digest", runtime)
        object.__setattr__(invocation, "launch_command_digest", rebuilt_command.command_digest)
        object.__setattr__(invocation, "observation_digest", digest(observation))
        object.__setattr__(invocation, "spool_digest", spool_digest)
        invocation.__post_init__()
        if deadline is not None:
            deadline.remaining_seconds()
        return invocation

    def assert_invocation_bounded(
        self, snapshot: ManagedInvocationSnapshot, deadline: SealedPreCompactionDeadline
    ) -> None:
        from zekam.application.local_continuity_v4_compaction import (
            SealedPreCompactionDeadline,
        )

        if type(deadline) is not SealedPreCompactionDeadline:
            raise ValidationFailed("Codex 0.151 exact PreCompact deadline required")
        deadline.remaining_seconds()
        self.assert_invocation(snapshot, deadline=deadline)
        deadline.remaining_seconds()

    def assert_process(self, snapshot: ManagedProcessSnapshot) -> None:
        if not _trusted_process_owner(self):
            raise PolicyViolation("Codex 0.151 unsealed process manager")
        if type(snapshot) is not ManagedProcessSnapshot:
            raise ValidationFailed("Codex 0.151 exact managed process receipt required")
        current = self.capture_process_from_snapshot(snapshot)
        if current != snapshot:
            raise LiveProcessVerificationError(("native-start-token",))

    def capture_process_from_snapshot(
        self,
        snapshot: ManagedProcessSnapshot,
        *,
        deadline: SealedPreCompactionDeadline | None = None,
    ) -> ManagedProcessSnapshot:
        if not _trusted_process_owner(self):
            raise PolicyViolation("Codex 0.151 unsealed process manager")
        process_row = (
            _process_row(snapshot.native_pid)
            if deadline is None
            else _process_row(snapshot.native_pid, timeout=deadline.remaining_seconds())
        )
        _parent, uid, start, executable = process_row
        native, shell, launcher, runtime = self._artifacts(deadline)
        if executable != _artifact_paths()[0].resolve(strict=True):
            raise LiveProcessVerificationError(("native-artifact",))
        client = digest(
            {
                "client": "codex",
                "version": CODEX_MACOS_0151_VERSION,
                "platform": "darwin-arm64",
                "native_artifact_digest": native,
            }
        )
        hook_set = digest(("SessionStart", "PreCompact", "PostCompact"))
        ancestry = digest(NATIVE_DOUBLE_EXEC_TOPOLOGY)
        commands = tuple(
            ReviewedHookCommand(
                snapshot.attachment_id,
                command.external_event_type,
                NATIVE_DOUBLE_EXEC_TOPOLOGY,
                client,
                hook_set,
                shell,
                launcher,
                runtime,
                command.argv_recipe_digest,
                command.sandbox_profile_digest,
                command.created_at,
            )
            for command in snapshot.reviewed_commands
        )
        current = object.__new__(ManagedProcessSnapshot)
        object.__setattr__(current, "attachment_id", snapshot.attachment_id)
        object.__setattr__(current, "captured_at", snapshot.captured_at)
        object.__setattr__(current, "native_pid", snapshot.native_pid)
        object.__setattr__(current, "native_uid", uid)
        object.__setattr__(current, "native_start_token", start)
        object.__setattr__(current, "native_artifact_digest", native)
        object.__setattr__(current, "client_contract_digest", client)
        object.__setattr__(current, "hook_set_digest", hook_set)
        object.__setattr__(current, "ancestry_policy_digest", ancestry)
        object.__setattr__(current, "reviewed_commands", commands)
        current.__post_init__()
        if self._generation(current) != self._generation(snapshot):
            raise LiveProcessVerificationError(("ancestry-policy",))
        return current

    def assert_invocation(
        self,
        snapshot: ManagedInvocationSnapshot,
        *,
        deadline: SealedPreCompactionDeadline | None = None,
    ) -> None:
        if not _trusted_process_owner(self):
            raise PolicyViolation("Codex 0.151 unsealed process manager")
        if type(snapshot) is not ManagedInvocationSnapshot:
            raise ValidationFailed("Codex 0.151 exact invocation receipt required")
        native_row = (
            _process_row(snapshot.native_pid)
            if deadline is None
            else _process_row(snapshot.native_pid, timeout=deadline.remaining_seconds())
        )
        _native_parent, native_uid, native_start, native_executable = native_row
        hook_row = (
            _process_row(snapshot.hook_pid)
            if deadline is None
            else _process_row(snapshot.hook_pid, timeout=deadline.remaining_seconds())
        )
        hook_parent, hook_uid, hook_start, hook_executable = hook_row
        native, shell, launcher, runtime = self._artifacts(deadline)
        native_path, _shell_path, _launcher_path, runtime_path = _artifact_paths()
        codes: list[str] = []
        if (
            native_executable != native_path.resolve(strict=True)
            or native != snapshot.native_artifact_digest
        ):
            codes.append("native-artifact")
        if native_uid != snapshot.native_uid:
            codes.append("native-uid")
        if native_start != snapshot.native_start_token:
            codes.append("native-start-token")
        if hook_parent != snapshot.native_pid:
            codes.append("hook-parent")
        if hook_uid != snapshot.hook_uid:
            codes.append("hook-uid")
        if hook_start != snapshot.hook_start_token:
            codes.append("hook-start-token")
        if hook_executable != runtime_path or runtime != snapshot.python_runtime_artifact_digest:
            codes.append("python-runtime-artifact")
        if shell != snapshot.shell_artifact_digest:
            codes.append("shell-artifact")
        if launcher != snapshot.python_launcher_artifact_digest:
            codes.append("python-launcher-artifact")
        if snapshot.ancestry_policy_digest != digest(NATIVE_DOUBLE_EXEC_TOPOLOGY):
            codes.append("ancestry-policy")
        if codes:
            raise LiveProcessVerificationError(tuple(sorted(set(codes))))


_TRUSTED_MANAGERS = weakref.WeakSet()


def _issue_peer_bound_process_manager(
    artifact_pins: _PinnedArtifactSet,
    peer_identity: tuple[int, int, str, str, str],
) -> TrustedCodex0151ProcessManager:
    """Bind the service-side verifier to one OS-observed live hook process."""
    if type(artifact_pins) is not _PinnedArtifactSet or (
        type(peer_identity) is not tuple
        or len(peer_identity) != 5
        or type(peer_identity[0]) is not int
        or type(peer_identity[1]) is not int
        or any(type(value) is not str for value in peer_identity[2:])
    ):
        raise ValidationFailed("Codex 0.151 exact peer-bound manager inputs required")
    pid, uid, start, artifact, audit = peer_identity
    for value in (artifact, audit):
        if _DIGEST.fullmatch(value) is None:
            raise ValidationFailed("Codex 0.151 exact peer digest required")
    _parent, live_uid, live_start, executable = _process_row(pid, timeout=1.0)
    if (
        uid != live_uid
        or start != live_start
        or executable != _artifact_paths()[3]
        or _raw_file_digest(executable) != artifact
    ):
        raise LiveProcessVerificationError(("hook-not-live",))
    manager = TrustedCodex0151ProcessManager(artifact_pins)
    manager._peer_identity = peer_identity
    return manager


def _trusted_process_owner(value: object) -> bool:
    return type(value) is TrustedCodex0151ProcessManager and value in _TRUSTED_MANAGERS


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    if type(value) is not str:
        raise ValidationFailed(f"Codex 0.151 {label} exact string required")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationFailed(f"Codex 0.151 {label} UTF-8 required") from exc
    if not 1 <= len(encoded) <= maximum or any(
        ord(char) < 32 or ord(char) == 0x7F or 0x80 <= ord(char) <= 0x9F for char in value
    ):
        raise ValidationFailed(f"Codex 0.151 {label} outside bounded text contract")
    return value


def _strict_document(payload: object) -> dict[str, object]:
    if type(payload) is not bytes or not 1 <= len(payload) <= _MAX_INPUT_BYTES:
        raise ValidationFailed("Codex 0.151 exact bounded bytes required")

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        if len(values) > 64:
            raise ValidationFailed("Codex 0.151 object member bound exceeded")
        for key, value in values:
            if type(key) is not str or key in result:
                raise ValidationFailed("Codex 0.151 duplicate/nontext JSON key")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValidationFailed("Codex 0.151 nonfinite JSON constant")

    try:
        document = json.loads(
            payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationFailed("Codex 0.151 strict UTF-8 JSON required") from exc
    if type(document) is not dict:
        raise ValidationFailed("Codex 0.151 one JSON object required")

    def validate(value: object, depth: int) -> None:
        if depth > 12:
            raise ValidationFailed("Codex 0.151 JSON depth exceeded")
        if type(value) is dict:
            if len(value) > 64:
                raise ValidationFailed("Codex 0.151 object member bound exceeded")
            for key, child in value.items():
                _text(key, "JSON key", maximum=512)
                validate(child, depth + 1)
        elif type(value) is list:
            if len(value) > 128:
                raise ValidationFailed("Codex 0.151 array bound exceeded")
            for child in value:
                validate(child, depth + 1)
        elif value is not None and type(value) not in {str, int, float, bool}:
            raise ValidationFailed("Codex 0.151 unsupported JSON value")
        elif type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValidationFailed("Codex 0.151 JSON UTF-8 value required") from exc

    validate(document, 1)
    return document


@final
@dataclass(frozen=True, slots=True)
class CodexMacOS0151Event:
    external_session_id: str
    event_type: str
    source: str | None
    turn_id: str | None
    trigger: str | None
    permission_mode: str | None
    wire_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.external_session_id) is not str
            or _SESSION.fullmatch(self.external_session_id) is None
        ):
            raise ValidationFailed("Codex 0.151 external session id invalid")
        if type(self.event_type) is not str or self.event_type not in _EVENT_MAPPING:
            raise PolicyViolation("Codex 0.151 lifecycle event outside reviewed contract")
        expected = (
            self.event_type == "SessionStart",
            self.source is not None,
            self.turn_id is None,
            self.trigger is None,
        )
        if expected != (True, True, True, True) and self.event_type == "SessionStart":
            raise ValidationFailed("Codex 0.151 SessionStart field relation invalid")
        if self.event_type != "SessionStart":
            if self.source is not None or self.turn_id is None or self.trigger not in _TRIGGERS:
                raise ValidationFailed("Codex 0.151 compaction field relation invalid")
            _text(self.turn_id, "turn id")
        if self.permission_mode is not None and self.permission_mode not in _PERMISSION_MODES:
            raise ValidationFailed("Codex 0.151 permission mode invalid")
        if type(self.wire_digest) is not str:
            raise ValidationFailed("Codex 0.151 wire digest string required")

    @property
    def internal_event_type(self) -> str:
        return _EVENT_MAPPING[self.event_type]

    def observation_body(self) -> dict[str, object]:
        return {
            "schema": CODEX_MACOS_0151_CONTRACT_SCHEMA,
            "client_id": "codex",
            "client_kind": "codex",
            "client_version": CODEX_MACOS_0151_VERSION,
            "session_id": self.external_session_id,
            "external_event_type": self.event_type,
            "internal_event_type": self.internal_event_type,
            "turn_id": self.turn_id,
            "source": self.source,
            "trigger": self.trigger,
            "reason": None,
            "stop_hook_active": False,
            "permission_mode": self.permission_mode,
            "wire_digest": self.wire_digest,
            "contains_prompt": False,
            "contains_response": False,
            "contains_transcript": False,
            "grants_authority": False,
        }


def parse_codex_macos_0151(
    payload: bytes,
    *,
    expected_root: Path,
    client_version: str = CODEX_MACOS_0151_VERSION,
) -> CodexMacOS0151Event:
    if not isinstance(expected_root, Path) or not expected_root.is_absolute():
        raise ValidationFailed("Codex 0.151 exact absolute source root required")
    if type(client_version) is not str or client_version != CODEX_MACOS_0151_VERSION:
        raise PolicyViolation("Codex client version is not reviewed macOS 0.151")
    system = platform.system()
    architecture = platform.machine()
    if type(system) is not str or system != "Darwin" or architecture != "arm64":
        raise PolicyViolation("Codex 0.151 adapter requires reviewed Darwin arm64")
    body = _strict_document(payload)
    event_type = body.get("hook_event_name")
    if type(event_type) is not str:
        raise ValidationFailed("Codex 0.151 hook event string required")
    if event_type not in _EVENT_MAPPING:
        raise PolicyViolation("Codex 0.151 lifecycle event outside reviewed contract")
    allowed = _BASE_KEYS | (
        {"source", "model", "permission_mode"}
        if event_type == "SessionStart"
        else {"turn_id", "trigger", "model"}
    )
    if set(body) != allowed - ({"model"} if "model" not in body else set()) - (
        {"permission_mode"}
        if event_type == "SessionStart" and "permission_mode" not in body
        else set()
    ):
        raise ValidationFailed("Codex 0.151 exact event key set required")
    session_id = body.get("session_id")
    if type(session_id) is not str or _SESSION.fullmatch(session_id) is None:
        raise ValidationFailed("Codex 0.151 external session id invalid")
    if "transcript_path" not in body:
        raise ValidationFailed("Codex 0.151 transcript_path key required")
    transcript = body["transcript_path"]
    if transcript is not None:
        transcript = _text(transcript, "transcript_path", maximum=_MAX_TRANSCRIPT_BYTES)
    cwd = _text(body.get("cwd"), "cwd", maximum=4096)
    if not Path(cwd).is_absolute():
        raise ValidationFailed("Codex 0.151 cwd must be absolute")
    try:
        actual_root = Path(cwd).resolve(strict=True)
        bound_root = expected_root.resolve(strict=True)
    except OSError as exc:
        raise PolicyViolation("Codex 0.151 source root unavailable") from exc
    if actual_root != bound_root:
        raise PolicyViolation("Codex 0.151 cwd does not match bound source root")
    model = body.get("model")
    if model is not None:
        _text(model, "model")
    permission = body.get("permission_mode")
    if permission is not None:
        permission = _text(permission, "permission mode")
        if permission not in _PERMISSION_MODES:
            raise ValidationFailed("Codex 0.151 permission mode invalid")
    source = body.get("source")
    turn_id = body.get("turn_id")
    trigger = body.get("trigger")
    if event_type == "SessionStart":
        if type(source) is not str:
            raise ValidationFailed("Codex 0.151 SessionStart source required")
        if source not in _START_SOURCES:
            raise ValidationFailed("Codex 0.151 SessionStart source invalid")
        if source != "startup":
            raise PolicyViolation("Codex 0.151 slice accepts startup only")
        turn_id = trigger = None
    else:
        turn_id = _text(turn_id, "turn id")
        if type(trigger) is not str or trigger not in _TRIGGERS:
            raise ValidationFailed("Codex 0.151 compaction trigger invalid")
        source = permission = None
    wire = {
        "session_id": session_id,
        "hook_event_name": event_type,
        "turn_id": turn_id,
        "source": source,
        "trigger": trigger,
        "reason": None,
        "stop_hook_active": False,
        "permission_mode": permission,
    }
    return CodexMacOS0151Event(
        session_id,
        event_type,
        source,
        turn_id,
        trigger,
        permission,
        digest(wire),
    )


def success_output(additional_context: str) -> bytes:
    if type(additional_context) is not str:
        raise ValidationFailed("Codex 0.151 exact additional context string required")
    try:
        encoded = additional_context.encode("utf-8")
        document = json.loads(additional_context)
    except (UnicodeEncodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationFailed("Codex 0.151 canonical additional context required") from exc
    expected_keys = {
        "schema",
        "manifest_digest",
        "source_snapshot_id",
        "source_revision",
        "fragments",
        "provider_called",
        "model_summary",
        "grants_authority",
    }
    fragments = document.get("fragments") if type(document) is dict else None
    if (
        type(document) is not dict
        or set(document) != expected_keys
        or document.get("schema") != "zekam-codex-session-start-context/v1"
        or any(
            document.get(key) is not False
            for key in ("provider_called", "model_summary", "grants_authority")
        )
        or type(document.get("manifest_digest")) is not str
        or _DIGEST.fullmatch(document["manifest_digest"]) is None
        or type(document.get("source_snapshot_id")) is not str
        or _UUID.fullmatch(document["source_snapshot_id"]) is None
        or type(document.get("source_revision")) is not str
        or _GIT_OBJECT.fullmatch(document["source_revision"]) is None
        or type(fragments) is not list
        or not 1 <= len(fragments) <= 16
        or canonical_json(document) != additional_context
        or not 1 <= len(encoded) <= MAX_ADDITIONAL_CONTEXT_UTF8_BYTES
    ):
        raise ValidationFailed("Codex 0.151 additional context outside exact bounds")
    identifiers: set[str] = set()
    for fragment in fragments:
        if (
            type(fragment) is not dict
            or set(fragment)
            != {"candidate_id", "kind", "source_ref", "content_digest", "token_count", "text"}
            or type(fragment.get("candidate_id")) is not str
            or _TOKEN.fullmatch(fragment["candidate_id"]) is None
            or fragment["candidate_id"] in identifiers
            or type(fragment.get("kind")) is not str
            or _TOKEN.fullmatch(fragment["kind"]) is None
            or type(fragment.get("source_ref")) is not str
            or _TOKEN.fullmatch(fragment["source_ref"]) is None
            or fragment["source_ref"].startswith("/")
            or "\\" in fragment["source_ref"]
            or any(part in {"", ".", ".."} for part in fragment["source_ref"].split("/"))
            or type(fragment.get("content_digest")) is not str
            or _DIGEST.fullmatch(fragment["content_digest"]) is None
            or type(fragment.get("token_count")) is not int
            or not 1 <= fragment["token_count"] <= 131_072
            or type(fragment.get("text")) is not str
            or digest(fragment["text"]) != fragment["content_digest"]
            or count_context_tokens(fragment["text"]) != fragment["token_count"]
        ):
            raise ValidationFailed("Codex 0.151 additional context fragment invalid")
        identifiers.add(fragment["candidate_id"])
    if scan_text(additional_context, relative_path="continuity/session-start", rules=SECRET_RULES):
        raise PolicyViolation("Codex 0.151 additional context secret rejected")
    body = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        }
    }
    output = canonical_json(body).encode("utf-8") + b"\n"
    if not 1 <= len(output) <= MAX_SESSION_START_SUCCESS_STDOUT_UTF8_BYTES:
        raise ValidationFailed("Codex 0.151 success stdout outside exact byte bound")
    return output


def handled_failure_output(*, recovery_required: bool) -> bytes:
    if type(recovery_required) is not bool:
        raise ValidationFailed("Codex 0.151 exact recovery flag required")
    reason = (
        "ZEKAM_SESSION_START_RECOVERY_REQUIRED"
        if recovery_required
        else "ZEKAM_SESSION_START_REJECTED"
    )
    return canonical_json({"continue": False, "stopReason": reason}).encode("utf-8") + b"\n"
