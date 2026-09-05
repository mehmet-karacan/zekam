"""Dormant macOS launchd-owned PreCompact supervisor boundary.

The production adapters are complete but issuance is deliberately disabled.
Synthetic helpers exercise only codecs and state reduction; their values can
never satisfy a production generation check.
"""

from __future__ import annotations

import copy
import ctypes
import datetime as dt
import fcntl
import json
import os
import pickle
import re
import secrets
import socket
import sqlite3
import stat
import struct
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, SupportsIndex, cast, final
from weakref import WeakValueDictionary

if __name__ == "__main__":  # Keep exact class identity for the reviewed ``python -m`` entry.
    sys.modules["zekam.infrastructure.macos_precompaction_supervisor"] = sys.modules[__name__]

from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.codex_macos_0151_lifecycle import (
    _process_row,
    _raw_file_digest,
)
from zekam.infrastructure.clients.codex_macos_0151_precompaction_client import (
    _STDOUT_DIGESTS,
    MAX_FRAME_BYTES,
    PROTOCOL_DIGEST,
    decode_frame,
    encode_frame,
)

JOB_LABEL: Final = "io.zekam.precompaction-supervisor"
LISTENER_KEY: Final = "PreCompactionListener"
DARWIN_LAUNCHD_CAPABILITY_OBSERVED: Final = False
PRODUCTION_GENERATION_ISSUED: Final = False
NATIVE_HOOK_ACTIVATED: Final = False
NATIVE_ACK_OBSERVED: Final = False
SOL_LOCAL: Final = 0
LOCAL_PEERPID: Final = 2
LOCAL_PEERTOKEN: Final = 6
MAX_DARWIN_PID: Final = 2_147_483_647
MAX_DARWIN_UID: Final = 2_147_483_647
_LAUNCHD_SOCKET_API: Final = "launch_activate_socket"
_EXPECTED_AUDIT_TOKEN_BYTES: Final = 32
_EXPECTED_PROC_STRUCT_VERSION: Final = 1
_CANARY_LABEL = re.compile(r"^io\.zekam\.precompaction-canary\.([0-9a-f]{64})$")
_GENERATIONS: WeakValueDictionary[str, _DarwinGenerationOwner] = WeakValueDictionary()
_GENERATION_PARITY: dict[str, bytes] = {}
_CANARY_ACTIVATIONS: dict[str, _CanaryActivation] = {}


def _text(value: object, label: str, maximum: int = 512) -> str:
    if type(value) is not str:
        raise ValidationFailed(f"PreCompact {label} string required")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationFailed(f"PreCompact {label} UTF-8 required") from exc
    if not 1 <= len(raw) <= maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValidationFailed(f"PreCompact {label} outside bound")
    return value


def _exact_int(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationFailed(f"PreCompact {label} coordinate invalid")
    return value


def _listener_observation_from_fd(path: str, fd: int, owner_uid: int) -> _DarwinListenerObservation:
    """Rebuild listener identity from one live descriptor without following links."""
    checked = _text(path, "listener path", 103)
    candidate = os.fsencode(checked)
    if not checked.startswith("/") or b".." in candidate.split(b"/"):
        raise PolicyViolation("PreCompact exact absolute listener path required")
    current = b"/"
    for part in candidate.split(b"/")[1:-1]:
        current = os.path.join(current, part)
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PolicyViolation("PreCompact listener ancestor identity mismatch")
        writable = bool(metadata.st_mode & 0o022)
        root_sticky = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
        if metadata.st_uid not in {0, owner_uid} or (writable and not root_sticky):
            raise PolicyViolation("PreCompact listener ancestor ownership mismatch")
    leaf = os.lstat(candidate)
    opened = os.fstat(fd)
    if (
        stat.S_ISLNK(leaf.st_mode)
        or not stat.S_ISSOCK(leaf.st_mode)
        or not stat.S_ISSOCK(opened.st_mode)
        or leaf.st_uid != owner_uid
    ):
        raise PolicyViolation("PreCompact listener pathname/descriptor mismatch")
    with socket.socket(fileno=os.dup(fd)) as duplicate:
        if duplicate.family != socket.AF_UNIX:
            raise PolicyViolation("PreCompact listener family mismatch")
        socket_type = duplicate.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
        if duplicate.getsockname() != checked:
            raise PolicyViolation("PreCompact listener pathname/descriptor mismatch")
    fd_flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    status_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFD, fd_flags | fcntl.FD_CLOEXEC)
    fcntl.fcntl(fd, fcntl.F_SETFL, status_flags | os.O_NONBLOCK)
    if (
        not fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        or not fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK
    ):
        raise PolicyViolation("PreCompact listener descriptor flags unavailable")
    return _DarwinListenerObservation(
        checked,
        fd,
        owner_uid,
        leaf.st_mode,
        leaf.st_dev,
        leaf.st_ino,
        leaf.st_nlink,
        socket_type,
    )


@final
@dataclass(frozen=True, slots=True)
class _DarwinListenerObservation:
    path: str
    fd: int
    owner_uid: int
    mode: int
    device: int
    inode: int
    nlink: int
    socket_type: int

    def __post_init__(self) -> None:
        path = _text(self.path, "listener path", 103)
        if not path.startswith("/") or "//" in path or "/../" in path:
            raise PolicyViolation("PreCompact exact absolute listener path required")
        _exact_int(self.fd, 0, 2**31 - 1, "listener fd")
        _exact_int(self.owner_uid, 0, MAX_DARWIN_UID, "listener owner")
        _exact_int(self.device, 0, 2**63 - 1, "listener device")
        _exact_int(self.inode, 1, 2**63 - 1, "listener inode")
        if type(self.mode) is not int or stat.S_IMODE(self.mode) != 0o600:
            raise PolicyViolation("PreCompact listener mode mismatch")
        if self.nlink != 1 or type(self.nlink) is not int:
            raise PolicyViolation("PreCompact listener link count mismatch")
        if type(self.socket_type) is not int or self.socket_type != socket.SOCK_STREAM:
            raise PolicyViolation("PreCompact listener type mismatch")


@final
@dataclass(frozen=True, slots=True)
class _DarwinJobObservation:
    struct_version: int
    reserved: bytes
    label: str
    listener_key: str
    service_pid: int
    service_uid: int
    service_start_token: str
    service_artifact_digest: str
    protocol_digest: str
    listener: _DarwinListenerObservation

    @property
    def listener_incarnation(self) -> tuple[int, int]:
        return (self.listener.device, self.listener.inode)

    def __post_init__(self) -> None:
        if self.struct_version != _EXPECTED_PROC_STRUCT_VERSION or self.reserved != b"\0" * 16:
            raise PolicyViolation("PreCompact Darwin structure/version mismatch")
        if (self.label != JOB_LABEL and _CANARY_LABEL.fullmatch(self.label) is None) or (
            self.listener_key != LISTENER_KEY
        ):
            raise PolicyViolation("PreCompact launchd identity mismatch")
        _exact_int(self.service_pid, 1, MAX_DARWIN_PID, "service pid")
        _exact_int(self.service_uid, 0, MAX_DARWIN_UID, "service uid")
        _text(self.service_start_token, "service start")
        for value in (self.service_artifact_digest, self.protocol_digest):
            if type(value) is not str or len(value) != 71 or not value.startswith("sha256:"):
                raise ValidationFailed("PreCompact generation digest invalid")
        if type(self.listener) is not _DarwinListenerObservation:
            raise ValidationFailed("PreCompact exact listener observation required")
        self.listener.__post_init__()
        if self.listener.owner_uid != self.service_uid:
            raise PolicyViolation("PreCompact listener/service owner mismatch")


@final
@dataclass(frozen=True, slots=True)
class _DarwinPeerObservation:
    pid: int
    uid: int
    start_token: str
    audit_token_digest: str
    artifact_digest: str

    def __post_init__(self) -> None:
        _exact_int(self.pid, 1, MAX_DARWIN_PID, "peer pid")
        _exact_int(self.uid, 0, MAX_DARWIN_UID, "peer uid")
        _text(self.start_token, "peer start")
        for value in (self.audit_token_digest, self.artifact_digest):
            if type(value) is not str or len(value) != 71 or not value.startswith("sha256:"):
                raise ValidationFailed("PreCompact peer digest invalid")


@final
class _DarwinAuditTokenParser:
    __slots__ = ()

    @staticmethod
    def parse(raw: bytes) -> tuple[int, int, str]:
        if type(raw) is not bytes or len(raw) != _EXPECTED_AUDIT_TOKEN_BYTES:
            raise ValidationFailed("PreCompact exact LOCAL_PEERTOKEN bytes required")
        words = struct.unpack("=8I", raw)
        uid, pid = words[1], words[5]
        _exact_int(uid, 0, MAX_DARWIN_UID, "audit uid")
        _exact_int(pid, 1, MAX_DARWIN_PID, "audit pid")
        return pid, uid, digest(raw.hex())


def _peer_identity_from_socket(connection: socket.socket) -> tuple[int, int, str, str, str]:
    if type(connection) is not socket.socket or connection.family != socket.AF_UNIX:
        raise ValidationFailed("PreCompact exact local peer socket required")
    peer_pid, token_uid, token_digest = _peer_audit_from_socket(connection)
    _parent, process_uid, start, executable = _process_row(peer_pid, timeout=1.0)
    if process_uid != token_uid:
        raise PolicyViolation("PreCompact peer UID/audit token mismatch")
    return peer_pid, token_uid, start, _raw_file_digest(executable), token_digest


def _peer_audit_from_socket(connection: socket.socket) -> tuple[int, int, str]:
    if type(connection) is not socket.socket or connection.family != socket.AF_UNIX:
        raise ValidationFailed("PreCompact exact local peer socket required")
    try:
        peer_pid = connection.getsockopt(SOL_LOCAL, LOCAL_PEERPID)
        raw = connection.getsockopt(SOL_LOCAL, LOCAL_PEERTOKEN, _EXPECTED_AUDIT_TOKEN_BYTES)
    except OSError as exc:
        raise PolicyViolation("PreCompact peer audit identity unavailable") from exc
    if type(peer_pid) is not int or type(raw) is not bytes:
        raise PolicyViolation("PreCompact peer audit identity invalid")
    token_pid, token_uid, token_digest = _DarwinAuditTokenParser.parse(raw)
    if token_pid != peer_pid:
        raise PolicyViolation("PreCompact peer PID/audit token mismatch")
    return peer_pid, token_uid, token_digest


@final
class _DarwinGenerationOwner:
    __slots__ = ("__weakref__", "_adapter", "_artifacts", "_digest", "_job", "_seal")
    _adapter: _DarwinAuthorityAdapter
    _artifacts: object | None
    _digest: str
    _job: _DarwinJobObservation
    _seal: str

    def __init__(self, *_values: object, **_named: object) -> None:
        raise PolicyViolation("PreCompact generation is OS-adapter owned")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise pickle.PicklingError("PreCompact production generation is not serializable")

    def _recheck(self, stage: str) -> None:
        if stage not in {
            "accept",
            "first-mutation",
            "precommit",
            "read-only-verification",
            "response",
            "deadline",
            "writer-construction",
        }:
            raise ValidationFailed("PreCompact generation stage invalid")
        observed = self._adapter.observe_current()
        if observed != self._job:
            raise PolicyViolation("PreCompact generation drift")
        _generation_digest_if_current(self)

    @property
    def generation_digest(self) -> str:
        return _generation_digest_if_current(self)


def _generation_bytes(owner: _DarwinGenerationOwner) -> bytes:
    return canonical_json(
        {
            "generation_digest": owner._digest,
            "job": {
                "label": owner._job.label,
                "listener_key": owner._job.listener_key,
                "pid": owner._job.service_pid,
                "uid": owner._job.service_uid,
                "start": owner._job.service_start_token,
                "artifact": owner._job.service_artifact_digest,
                "protocol": owner._job.protocol_digest,
                "struct_version": owner._job.struct_version,
                "reserved": owner._job.reserved.hex(),
                "listener_device": owner._job.listener.device,
                "listener_inode": owner._job.listener.inode,
                "listener_fd": owner._job.listener.fd,
                "listener_owner": owner._job.listener.owner_uid,
                "listener_mode": owner._job.listener.mode,
                "listener_nlink": owner._job.listener.nlink,
                "listener_type": owner._job.listener.socket_type,
                "listener_path_digest": digest(owner._job.listener.path),
            },
        }
    ).encode("utf-8")


def _generation_digest_if_current(value: object) -> str:
    if type(value) is not _DarwinGenerationOwner:
        raise PolicyViolation("PreCompact exact production generation required")
    seal = value._seal
    if _GENERATIONS.get(seal) is not value or _GENERATION_PARITY.get(seal) != _generation_bytes(
        value
    ):
        raise PolicyViolation("PreCompact stale production generation")
    value._job.__post_init__()
    return value._digest


@final
class _DarwinAuthorityAdapter:
    """Only production issuer; it takes no caller evidence or synthetic adapter."""

    __slots__ = ("_expected",)

    def __init__(self, *_values: object, **_named: object) -> None:
        raise PolicyViolation("PreCompact Darwin authority is launchd-issued")

    @staticmethod
    def _launch_activate_socket(listener_key: str = LISTENER_KEY) -> tuple[int, ...]:
        key = _text(listener_key, "listener key", 64)
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        if not hasattr(libc, _LAUNCHD_SOCKET_API):
            raise PolicyViolation("PreCompact launchd socket API unavailable")
        function = libc.launch_activate_socket
        function.argtypes = (
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
            ctypes.POINTER(ctypes.c_size_t),
        )
        function.restype = ctypes.c_int
        values = ctypes.POINTER(ctypes.c_int)()
        count = ctypes.c_size_t()
        status = function(key.encode("ascii"), ctypes.byref(values), ctypes.byref(count))
        if status != 0:
            raise OSError(status, "launch_activate_socket failed")
        descriptors: tuple[int, ...] = ()
        try:
            if count.value > 16:
                raise PolicyViolation("PreCompact launchd descriptor count outside bound")
            descriptors = tuple(int(values[index]) for index in range(count.value))
        finally:
            libc.free.argtypes = (ctypes.c_void_p,)
            libc.free.restype = None
            libc.free(values)
        if len(descriptors) != 1 or descriptors[0] < 0:
            for descriptor in descriptors:
                with suppress(OSError):
                    os.close(descriptor)
            raise PolicyViolation("PreCompact launchd exact one listener required")
        return descriptors

    @classmethod
    def acquire(cls) -> _DarwinGenerationOwner:
        if not DARWIN_LAUNCHD_CAPABILITY_OBSERVED or not PRODUCTION_GENERATION_ISSUED:
            raise PolicyViolation("PreCompact launchd capability is not activated")
        cls._launch_activate_socket()
        raise PolicyViolation("PreCompact production generation remains unissued")

    def observe_current(self) -> _DarwinJobObservation:
        expected = getattr(self, "_expected", None)
        if type(expected) is not _DarwinJobObservation:
            raise PolicyViolation("PreCompact live generation observation unavailable")
        listener = _listener_observation_from_fd(
            expected.listener.path, expected.listener.fd, expected.service_uid
        )
        _parent, uid, start, executable = _process_row(os.getpid(), timeout=1.0)
        observed = _DarwinJobObservation(
            _EXPECTED_PROC_STRUCT_VERSION,
            b"\0" * 16,
            expected.label,
            LISTENER_KEY,
            os.getpid(),
            uid,
            start,
            _raw_file_digest(executable),
            PROTOCOL_DIGEST,
            listener,
        )
        if observed != expected:
            raise PolicyViolation("PreCompact canary generation drift")
        return observed


@final
class _CanaryActivation:
    __slots__ = ("__weakref__", "_generation", "_nonce", "_seal")
    _generation: _DarwinGenerationOwner
    _nonce: str
    _seal: str

    def __init__(self, *_values: object, **_named: object) -> None:
        raise PolicyViolation("PreCompact canary is launchd-issued")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise pickle.PicklingError("PreCompact canary is not serializable")


def _issue_canary_activation(nonce: str, label: str, socket_path: str) -> _CanaryActivation:
    nonce = _text(nonce, "canary nonce", 64)
    try:
        decoded = bytes.fromhex(nonce)
    except ValueError as exc:
        raise ValidationFailed("PreCompact exact canary nonce required") from exc
    if (
        len(decoded) != 32
        or nonce != nonce.lower()
        or label != f"io.zekam.precompaction-canary.{nonce}"
    ):
        raise PolicyViolation("PreCompact canary label/nonce mismatch")
    root = Path(socket_path).parent
    root_metadata = root.lstat()
    if (
        not root.is_absolute()
        or not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
        or root_metadata.st_uid != os.geteuid()
    ):
        raise PolicyViolation("PreCompact canary root identity invalid")
    from zekam.infrastructure.clients.codex_macos_0151_lifecycle import _PinnedArtifactSet

    (descriptor,) = _DarwinAuthorityAdapter._launch_activate_socket()
    artifacts: _PinnedArtifactSet | None = None
    try:
        artifacts = _PinnedArtifactSet()
        listener = _listener_observation_from_fd(socket_path, descriptor, os.geteuid())
        _parent, uid, start, executable = _process_row(os.getpid(), timeout=1.0)
        job = _DarwinJobObservation(
            _EXPECTED_PROC_STRUCT_VERSION,
            b"\0" * 16,
            label,
            LISTENER_KEY,
            os.getpid(),
            uid,
            start,
            _raw_file_digest(executable),
            PROTOCOL_DIGEST,
            listener,
        )
        adapter = object.__new__(_DarwinAuthorityAdapter)
        object.__setattr__(adapter, "_expected", job)
        generation = object.__new__(_DarwinGenerationOwner)
        generation_digest = digest(
            {
                "schema": "zekam-precompact-canary-generation/v1",
                "label": label,
                "nonce": nonce,
                "listener_device": listener.device,
                "listener_inode": listener.inode,
                "service_pid": job.service_pid,
                "service_start_token": job.service_start_token,
                "service_uid": job.service_uid,
            }
        )
        seal = secrets.token_hex(32)
        object.__setattr__(generation, "_adapter", adapter)
        object.__setattr__(generation, "_artifacts", artifacts)
        object.__setattr__(generation, "_digest", generation_digest)
        object.__setattr__(generation, "_job", job)
        object.__setattr__(generation, "_seal", seal)
        _GENERATIONS[seal] = generation
        _GENERATION_PARITY[seal] = _generation_bytes(generation)
        activation = object.__new__(_CanaryActivation)
        activation_seal = secrets.token_hex(32)
        object.__setattr__(activation, "_generation", generation)
        object.__setattr__(activation, "_nonce", nonce)
        object.__setattr__(activation, "_seal", activation_seal)
        _CANARY_ACTIVATIONS[activation_seal] = activation
        return activation
    except BaseException:
        if artifacts is not None:
            with suppress(OSError):
                artifacts.close()
        os.close(descriptor)
        raise


def _consume_canary(value: object) -> tuple[_DarwinGenerationOwner, str]:
    if type(value) is not _CanaryActivation:
        raise PolicyViolation("PreCompact exact canary activation required")
    if _CANARY_ACTIVATIONS.pop(value._seal, None) is not value:
        raise PolicyViolation("PreCompact canary activation already consumed")
    if type(value._generation) is not _DarwinGenerationOwner:
        raise PolicyViolation("PreCompact canary generation unavailable")
    _generation_digest_if_current(value._generation)
    return value._generation, value._nonce


def _response_body(
    generation: _DarwinGenerationOwner,
    request: dict[str, object],
    result: object | None,
    decision_body: object | None,
) -> dict[str, object]:
    from zekam.application.local_continuity_v4_compaction import (
        PreCompactionResult,
        VerifiedAckDecision,
    )

    generation._recheck("response")
    job = generation._job
    classification = "STORAGE_UNAVAILABLE"
    fresh = replay = False
    decision_digest = census = None
    if result is not None:
        if type(result) is not PreCompactionResult:
            raise PolicyViolation("PreCompact exact durable result required")
        result.__post_init__()
        if result.status == "checkpoint-ready":
            if (
                type(decision_body) is not VerifiedAckDecision
                or decision_body.generation_digest != generation.generation_digest
                or decision_body.decision_digest != result.ack_decision_digest
            ):
                raise PolicyViolation("PreCompact durable decision body unavailable")
            decision_body.__post_init__()
            decoded_decision = json.loads(decision_body.body_json)
            if (
                decoded_decision.get("checkpoint_digest") != result.checkpoint_digest
                or decoded_decision.get("checkpoint_requested_event_digest")
                != result.checkpoint_requested_event_digest
                or decoded_decision.get("pre_compaction_event_digest")
                != result.pre_compaction_event_digest
                or decoded_decision.get("native_receipt_digest") != result.native_receipt_digest
                or decoded_decision.get("pre_compact_committed_revision_digest")
                != result.attachment_revision_digest
            ):
                raise PolicyViolation("PreCompact durable result/decision mismatch")
            decision_body = decoded_decision
            classification = "checkpoint-ready"
            fresh, replay = not result.replay, result.replay
            decision_digest = result.ack_decision_digest
            census = digest(
                {
                    "schema": "zekam-precompact-verified-census/v1",
                    "decision_digest": decision_digest,
                    "checkpoint_digest": result.checkpoint_digest,
                    "attachment_revision_digest": result.attachment_revision_digest,
                }
            )
        else:
            classification = str(result.failure_category)
            decision_body = None
    stdout_digest = _STDOUT_DIGESTS.get(classification)
    if stdout_digest is None:
        classification = "RECOVERY_REQUIRED"
        stdout_digest = _STDOUT_DIGESTS[classification]
        decision_body = decision_digest = census = None
    return {
        "attempt_nonce": request["attempt_nonce"],
        "classification": classification,
        "decision_body": decision_body,
        "decision_digest": decision_digest,
        "fresh": fresh,
        "protocol_digest": PROTOCOL_DIGEST,
        "replay": replay,
        "request_body_digest": digest(canonical_json(request)),
        "request_key": request["request_key"],
        "schema": "zekam-precompact-local-response/v1",
        "service_pid": job.service_pid,
        "service_start_token": job.service_start_token,
        "service_uid": job.service_uid,
        "stdout_digest": stdout_digest,
        "verified_census_digest": census,
    }


def _resolved_precompaction(
    generation: _DarwinGenerationOwner,
    request: dict[str, object],
    peer: tuple[int, int, str, str, str],
    database: Path,
    home: Path,
) -> tuple[object, object | None]:
    """Resolve an existing hydrated binding and invoke the fixed writer server-side."""
    from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
    from zekam.infrastructure.clients.codex_macos_0151_lifecycle import (
        CodexMacOS0151Event,
        _issue_peer_bound_process_manager,
        _PinnedArtifactSet,
    )
    from zekam.infrastructure.sqlite.local_continuity_v4_compaction import (
        resolve_existing_precompaction_binding,
        resolved_precompaction_writer,
        rollover_existing_precompaction_process,
    )

    if (
        request.get("schema") != "zekam-precompact-local-raw-request/v1"
        or type(database) is not type(Path())
        or type(home) is not type(Path())
        or database != home / "state" / "operational.db"
    ):
        raise PolicyViolation("PreCompact exact server composition required")
    event = CodexMacOS0151Event(
        str(request["external_session_id"]),
        "PreCompact",
        None,
        str(request["turn_id"]),
        str(request["trigger"]),
        None,
        str(request["event_wire_digest"]),
    )
    resolved = resolve_existing_precompaction_binding(
        database, event, cwd=Path(str(request["cwd"]))
    )
    artifacts = generation._artifacts
    if type(artifacts) is not _PinnedArtifactSet:
        raise PolicyViolation("PreCompact reviewed artifact pins unavailable")
    manager = _issue_peer_bound_process_manager(artifacts, peer)
    resolved = rollover_existing_precompaction_process(
        database,
        event,
        Path(str(request["cwd"])),
        resolved,
        manager,
    )
    writer = resolved_precompaction_writer(
        database,
        resolved,
        spool=ClientLifecycleSpool(home, client_id="codex"),
        generation=generation,
    )
    writer.process_manager = manager
    return writer.pre_compaction_with_decision(event)


def _session_plan(path: Path) -> dict[str, object]:
    from zekam.infrastructure.clients.codex_macos_0151_precompaction_client import _strict_json

    info = path.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or not 1 <= info.st_size <= 8192
    ):
        raise PolicyViolation("SessionStart sealed allocation plan identity rejected")
    raw = path.read_bytes()
    plan = _strict_json(raw)
    keys = {
        "device_id",
        "opened_at",
        "plan_digest",
        "policy_digest",
        "project_id",
        "realm_id",
        "run_id",
        "schema",
        "source_paths",
        "source_snapshot_id",
        "task_digest",
        "work_item_id",
    }
    if set(plan) != keys or plan["schema"] != "zekam-session-start-allocation-plan/v1":
        raise ValidationFailed("SessionStart exact allocation plan required")
    if type(plan["source_paths"]) is not list:
        raise ValidationFailed("SessionStart exact source path list required")
    paths = plan["source_paths"]
    if (
        not 1 <= len(paths) <= 8
        or any(type(value) is not str for value in paths)
        or sorted(set(paths)) != paths
    ):
        raise ValidationFailed("SessionStart bounded source paths required")
    for name in keys - {"schema", "source_paths"}:
        _text(plan[name], f"allocation {name}", 512)
    try:
        opened = dt.datetime.fromisoformat(str(plan["opened_at"]))
    except ValueError as exc:
        raise ValidationFailed("SessionStart allocation timestamp invalid") from exc
    if opened.tzinfo is None or opened.microsecond or opened.utcoffset() != dt.timedelta(0):
        raise ValidationFailed("SessionStart whole-second UTC timestamp required")
    return plan


def _allocate_and_hydrate_session(
    generation: _DarwinGenerationOwner,
    request: dict[str, object],
    peer: tuple[int, int, str, str, str],
    database: Path,
    home: Path,
    plan_path: Path,
) -> object:
    """Canary-only exact-once allocator followed by the existing SessionStart ingress."""
    from uuid import UUID, uuid5

    from zekam.application.active_task_contract import AUTHORITY_REF
    from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
    from zekam.application.config import core_root
    from zekam.application.local_continuity import ContinuityBinding
    from zekam.application.local_continuity_source_plan import ContinuitySourceRecipe
    from zekam.application.local_continuity_v4_compaction import _issue_deadline
    from zekam.infrastructure.clients.codex_macos_0151_lifecycle import (
        CodexMacOS0151Event,
        _issue_peer_bound_process_manager,
        _PinnedArtifactSet,
    )
    from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource
    from zekam.infrastructure.local_continuity_v4_composition import (
        _CurrentV4SessionStartContext,
        _DormantV4Environment,
    )
    from zekam.infrastructure.sqlite import operational_schema
    from zekam.infrastructure.sqlite.local_continuity_v4_compaction import _GenerationSource
    from zekam.infrastructure.sqlite.local_continuity_v4_ingress import SQLiteCodexV4Ingress

    if (
        request.get("schema") != "zekam-session-start-local-raw-request/v1"
        or database != home / "state" / "operational.db"
        or plan_path.parent != home
        or Path(str(request["cwd"])).resolve(strict=True)
        != Path("/Users/mkaracan/Projeler/akilli-kasa").resolve(strict=True)
    ):
        raise PolicyViolation("SessionStart exact canary scope required")
    plan = _session_plan(plan_path)
    artifacts = generation._artifacts
    if type(artifacts) is not _PinnedArtifactSet:
        raise PolicyViolation("SessionStart reviewed artifact pins unavailable")
    manager = _issue_peer_bound_process_manager(artifacts, peer)
    native_pid, _hook_uid, _hook_start, _hook_path = _process_row(peer[0], timeout=1.0)
    _native_parent, native_uid, native_start, _native_path = _process_row(native_pid, timeout=1.0)
    session_id = str(
        uuid5(
            UUID("018f0000-0000-7000-8000-000000000153"),
            digest(
                {
                    "schema": "zekam-session-start-allocation-identity/v1",
                    "external_session_id": request["external_session_id"],
                    "hook_pid": peer[0],
                    "hook_start_token": peer[2],
                    "native_pid": native_pid,
                    "native_uid": native_uid,
                    "native_start_token": native_start,
                    "plan_digest": digest(plan),
                }
            ),
        )
    )
    binding = ContinuityBinding(
        session_id,
        str(request["external_session_id"]),
        str(plan["project_id"]),
        str(plan["realm_id"]),
        "codex",
        str(plan["device_id"]),
        str(plan["source_snapshot_id"]),
        str(plan["task_digest"]),
        str(plan["plan_digest"]),
        str(plan["policy_digest"]),
        str(plan["work_item_id"]),
        str(plan["run_id"]),
    )
    deadline = _issue_deadline(generation, time.monotonic_ns)
    gate = _GenerationSource(generation, database)
    try:
        gate.snapshot(binding, deadline)
    finally:
        gate.close()
    db = sqlite3.connect(f"{database.resolve().as_uri()}?mode=rw", uri=True, timeout=0.0)
    db.row_factory = sqlite3.Row
    try:
        db.execute("pragma foreign_keys=on")
        db.execute("pragma busy_timeout=0")
        db.execute("begin immediate")
        if operational_schema._validate_connection(db) != 4:
            raise PolicyViolation("SessionStart corrected v4 required")
        facts = db.execute(
            "select p.id from project p join source_binding sb on sb.project_id=p.id "
            "join source_snapshot ss on ss.source_binding_id=sb.id "
            "join config_revision c on c.active=1 join work_item w on w.project_id=p.id "
            "join run r on r.work_item_id=w.id where p.id=? and p.status='active' "
            "and sb.active=1 and sb.source_kind='git' and ss.id=? and c.task_digest=? "
            "and c.config_digest=? and w.id=? and w.state='ready' and r.id=? "
            "and r.config_revision_id=c.id and r.source_snapshot_id=ss.id "
            "and r.plan_digest=? limit 2",
            (
                binding.project_id,
                binding.source_snapshot_id,
                binding.task_digest,
                binding.policy_digest,
                binding.work_item_id,
                binding.run_id,
                binding.plan_digest,
            ),
        ).fetchall()
        if len(facts) != 1:
            raise PolicyViolation("SessionStart preseeded authority drift")
        rows = db.execute(
            "select b.*,s.status from continuity_session_binding b join session s "
            "on s.id=b.session_id where b.external_session_id=? limit 2",
            (binding.external_session_id,),
        ).fetchall()
        if not rows:
            db.execute(
                "insert into session(id,client_id,device_id,project_id,work_item_id,status,"
                "opened_at) "
                "values(?,?,?,?,?,'open',?)",
                (
                    binding.session_id,
                    "codex",
                    binding.device_id,
                    binding.project_id,
                    binding.work_item_id,
                    plan["opened_at"],
                ),
            )
            db.execute(
                "insert into continuity_session_binding values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    binding.session_id,
                    binding.external_session_id,
                    binding.project_id,
                    binding.realm_id,
                    binding.work_item_id,
                    binding.run_id,
                    "codex",
                    binding.device_id,
                    binding.source_snapshot_id,
                    binding.task_digest,
                    binding.plan_digest,
                    binding.policy_digest,
                    binding.binding_digest,
                    plan["opened_at"],
                ),
            )
        elif len(rows) != 1 or any(
            rows[0][name] != value
            for name, value in {
                "session_id": binding.session_id,
                "binding_digest": binding.binding_digest,
                "status": "open",
            }.items()
        ):
            raise PolicyViolation("SessionStart allocation replay ambiguity")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as read:
        source_row = read.execute(
            "select source_binding_id from source_snapshot where id=?",
            (binding.source_snapshot_id,),
        ).fetchone()
    if source_row is None:
        raise PolicyViolation("SessionStart source binding disappeared")
    recipe = ContinuitySourceRecipe(
        binding.project_id,
        binding.realm_id,
        str(source_row[0]),
        tuple(str(value) for value in cast(list[object], plan["source_paths"])),
        binding.task_digest,
        binding.policy_digest,
    )
    source = BoundedContinuitySource(Path(str(request["cwd"])), recipe)
    environment = _DormantV4Environment(home, core_root(), core_root() / AUTHORITY_REF, database)
    context = _CurrentV4SessionStartContext(
        database, binding, source, recipe.allowed_paths, environment
    )
    ingress = SQLiteCodexV4Ingress(
        database,
        binding,
        process_manager=manager,
        context_port=context,
        spool=ClientLifecycleSpool(home, client_id="codex"),
    )
    event = CodexMacOS0151Event(
        binding.external_session_id,
        "SessionStart",
        str(request["source"]),
        None,
        None,
        None,
        str(request["event_wire_digest"]),
    )
    ingress.attach_process()
    result = ingress.session_start(event)
    if (
        result.recovery_required
        or result.manifest_digest is None
        or result.hydration_receipt_digest is None
        or result.attachment_revision_digest is None
    ):
        raise PolicyViolation("SessionStart hydration did not reach durable graph")
    return result


def _session_response_body(
    generation: _DarwinGenerationOwner, request: dict[str, object], result: object
) -> dict[str, object]:
    from zekam.application.local_continuity_v4_ingress import SessionStartIngressResult

    if type(result) is not SessionStartIngressResult:
        raise PolicyViolation("SessionStart exact durable result required")
    result.__post_init__()
    if any(
        value is None
        for value in (
            result.manifest_digest,
            result.hydration_receipt_digest,
            result.attachment_revision_digest,
        )
    ):
        raise PolicyViolation("SessionStart complete evidence required")
    generation._recheck("response")
    job = generation._job
    return {
        "attempt_nonce": request["attempt_nonce"],
        "attachment_revision_digest": result.attachment_revision_digest,
        "classification": "hydrated",
        "hook_stdout": result.stdout.decode("utf-8"),
        "hook_stdout_digest": digest_of_bytes(result.stdout),
        "hydration_receipt_digest": result.hydration_receipt_digest,
        "manifest_digest": result.manifest_digest,
        "protocol_digest": PROTOCOL_DIGEST,
        "replay": result.replay,
        "request_body_digest": digest(canonical_json(request)),
        "request_key": request["request_key"],
        "schema": "zekam-session-start-local-response/v1",
        "service_pid": job.service_pid,
        "service_start_token": job.service_start_token,
        "service_uid": job.service_uid,
    }


def serve_canary_once(
    activation: object,
    handler: Callable[[dict[str, object]], tuple[object, object | None]] | None = None,
    *,
    timeout_seconds: float = 2.0,
) -> int:
    """Consume one launchd capability and serve exactly one bounded request."""
    if type(timeout_seconds) is not float or not 0.05 <= timeout_seconds <= 8.0:
        raise ValidationFailed("PreCompact canary timeout invalid")
    generation, nonce = _consume_canary(activation)
    descriptor = generation._job.listener.fd
    listener = socket.socket(fileno=os.dup(descriptor))
    connection: socket.socket | None = None
    try:
        generation._recheck("accept")
        listener.settimeout(timeout_seconds)
        connection, _address = listener.accept()
        connection.settimeout(timeout_seconds)
        peer = _peer_identity_from_socket(connection)
        request = decode_frame(_receive_one(connection), response=False)
        if (
            request["attempt_nonce"] != nonce
            or request["client_pid"] != peer[0]
            or request["client_uid"] != peer[1]
            or request["client_start_token"] != peer[2]
        ):
            raise PolicyViolation("PreCompact request/peer identity mismatch")
        result: object | None = None
        decision: object | None = None
        session_response = False
        if handler is not None:
            result, decision = handler(request)
        else:
            database_text = os.environ.get("ZEKAM_PRECOMPACT_CANARY_DATABASE", "")
            home_text = os.environ.get("ZEKAM_PRECOMPACT_CANARY_HOME", "")
            plan_text = os.environ.get("ZEKAM_PRECOMPACT_CANARY_SESSION_PLAN", "")
            if database_text or home_text:
                if request.get("schema") == "zekam-session-start-local-raw-request/v1":
                    if not plan_text:
                        raise PolicyViolation("SessionStart sealed allocation plan unavailable")
                    result = _allocate_and_hydrate_session(
                        generation,
                        request,
                        peer,
                        Path(database_text),
                        Path(home_text),
                        Path(plan_text),
                    )
                    session_response = True
                else:
                    result, decision = _resolved_precompaction(
                        generation, request, peer, Path(database_text), Path(home_text)
                    )
        response_body = (
            _session_response_body(generation, request, result)
            if session_response
            else _response_body(generation, request, result, decision)
        )
        response = encode_frame(response_body, response=True)
        connection.sendall(response)
        connection.shutdown(socket.SHUT_WR)
        time.sleep(0.05)
        return (
            os.EX_OK
            if response_body["classification"] in {"checkpoint-ready", "hydrated"}
            else os.EX_UNAVAILABLE
        )
    finally:
        if connection is not None:
            connection.close()
        listener.close()
        with suppress(OSError):
            os.close(descriptor)
        artifacts = getattr(generation, "_artifacts", None)
        if artifacts is not None:
            with suppress(OSError):
                artifacts.close()
        _GENERATION_PARITY.pop(generation._seal, None)
        _GENERATIONS.pop(generation._seal, None)


def canary_service_entry() -> int:
    nonce = os.environ.get("ZEKAM_PRECOMPACT_CANARY_NONCE", "")
    label = os.environ.get("ZEKAM_PRECOMPACT_CANARY_LABEL", "")
    socket_path = os.environ.get("ZEKAM_PRECOMPACT_CANARY_SOCKET", "")
    try:
        activation = _issue_canary_activation(nonce, label, socket_path)
        result = serve_canary_once(activation)
        _write_canary_status("served-false" if result else "served-ack")
        return result
    except Exception as exc:
        _write_canary_status(f"error-{type(exc).__name__}")
        return os.EX_UNAVAILABLE


def _write_canary_status(code: str) -> None:
    path_text = os.environ.get("ZEKAM_PRECOMPACT_CANARY_STATUS", "")
    if not path_text:
        return
    path = Path(path_text)
    root = path.parent
    try:
        metadata = root.lstat()
        if (
            not path.is_absolute()
            or path.name != "status.json"
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()
        ):
            return
        raw = canonical_json({"schema": "zekam-precompact-canary-status/v1", "status": code})
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(descriptor, raw.encode("ascii") + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return


@final
class _ProductionService:
    __slots__ = ("_generation",)

    def __init__(self, generation: _DarwinGenerationOwner) -> None:
        _generation_digest_if_current(generation)
        self._generation = generation

    def serve_once(self) -> int:
        self._generation._recheck("accept")
        return os.EX_UNAVAILABLE


@final
@dataclass(frozen=True, slots=True)
class LaunchdQuiescence:
    job_absent: bool
    listener_released: bool
    service_exited: bool
    connections_closed: bool
    resource_handles_released: bool
    durable_census: str

    def __post_init__(self) -> None:
        flags = (
            self.job_absent,
            self.listener_released,
            self.service_exited,
            self.connections_closed,
            self.resource_handles_released,
        )
        if tuple(type(value) for value in flags) != (bool,) * 5:
            raise ValidationFailed("PreCompact exact quiescence flags required")
        if self.durable_census not in {"baseline", "complete", "other"}:
            raise ValidationFailed("PreCompact exact durable census required")

    @property
    def permits_next_generation(self) -> bool:
        return all(
            (
                self.job_absent,
                self.listener_released,
                self.service_exited,
                self.connections_closed,
                self.resource_handles_released,
                self.durable_census in {"baseline", "complete"},
            )
        )


@final
@dataclass(frozen=True, slots=True)
class SyntheticSupervisorObservation:
    request_digest: str
    response_digest: str | None
    classification: str
    protocol_verified: bool
    grants_authority: bool = False
    production_generation_issued: bool = False
    native_hook_activated: bool = False
    native_ack_observed: bool = False

    def __post_init__(self) -> None:
        if type(self.protocol_verified) is not bool or any(
            value is not False
            for value in (
                self.grants_authority,
                self.production_generation_issued,
                self.native_hook_activated,
                self.native_ack_observed,
            )
        ):
            raise PolicyViolation("Synthetic PreCompact observation cannot grant authority")
        if self.classification not in {"codec-rejected", "response-verified"}:
            raise ValidationFailed("Synthetic PreCompact classification invalid")

    def __copy__(self) -> NoReturn:
        raise TypeError("Synthetic observations are not copyable")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("Synthetic observations are not copyable")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise pickle.PicklingError("Synthetic observations are not serializable")


@final
@dataclass(frozen=True, slots=True)
class SyntheticDurableOutcome:
    request_key: str
    census_digest: str
    classification: str
    spool_count: int
    graph_count: int
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.classification not in {"fresh-graph", "replay-graph", "fixed-false"}:
            raise ValidationFailed("PreCompact synthetic outcome invalid")
        if (
            type(self.spool_count) is not int
            or type(self.graph_count) is not int
            or self.spool_count not in {0, 1}
            or self.graph_count not in {0, 1}
            or self.grants_authority is not False
        ):
            raise PolicyViolation("PreCompact synthetic outcome cannot grant authority")


class SyntheticCrash(RuntimeError):
    pass


@final
class SyntheticCheckpointModel:
    """Thread-safe crash/replay reducer; intentionally disjoint from production values."""

    __slots__ = ("_graphs", "_lock", "_spool")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spool: set[str] = set()
        self._graphs: set[str] = set()

    def execute(
        self, request_frame: bytes, *, crash_stage: str | None = None
    ) -> SyntheticDurableOutcome:
        if crash_stage not in {None, "before-spool", "after-spool", "after-commit"}:
            raise ValidationFailed("PreCompact synthetic crash stage invalid")
        request = decode_frame(request_frame, response=False)
        key = str(request["request_key"])
        with self._lock:
            if key in self._graphs:
                return self._outcome(key, "replay-graph")
            if crash_stage == "before-spool":
                raise SyntheticCrash("before-spool")
            self._spool.add(key)
            if crash_stage == "after-spool":
                raise SyntheticCrash("after-spool")
            self._graphs.add(key)
            if crash_stage == "after-commit":
                raise SyntheticCrash("after-commit")
            return self._outcome(key, "fresh-graph")

    def _outcome(self, key: str, classification: str) -> SyntheticDurableOutcome:
        census = digest(
            {
                "schema": "zekam-precompact-synthetic-census/v1",
                "request_key": key,
                "spool": key in self._spool,
                "graph": key in self._graphs,
            }
        )
        return SyntheticDurableOutcome(
            key, census, classification, int(key in self._spool), int(key in self._graphs)
        )

    def census(self, request_key: str) -> SyntheticDurableOutcome:
        if type(request_key) is not str:
            raise ValidationFailed("PreCompact synthetic request key required")
        with self._lock:
            kind = "replay-graph" if request_key in self._graphs else "fixed-false"
            return self._outcome(request_key, kind)


def observe_synthetic_exchange(
    request_frame: bytes, response_frame: bytes
) -> SyntheticSupervisorObservation:
    request = decode_frame(request_frame, response=False)
    response = decode_frame(response_frame, response=True)
    request_digest = digest(canonical_json(request))
    if response["attempt_nonce"] != request["attempt_nonce"]:
        raise PolicyViolation("PreCompact response attempt mismatch")
    if response["request_key"] != request["request_key"]:
        raise PolicyViolation("PreCompact response request mismatch")
    if response["request_body_digest"] != request_digest:
        raise PolicyViolation("PreCompact response body binding mismatch")
    if response["classification"] == "checkpoint-ready":
        raise PolicyViolation("Synthetic PreCompact cannot observe production success")
    return SyntheticSupervisorObservation(
        request_digest, digest(canonical_json(response)), "response-verified", True
    )


def _receive_one(connection: socket.socket) -> bytes:
    header = bytearray()
    while len(header) < 4:
        chunk = connection.recv(4 - len(header))
        if not chunk:
            raise ConnectionError("PreCompact synthetic early EOF")
        header.extend(chunk)
    size = struct.unpack(">I", header)[0]
    if not 1 <= size <= MAX_FRAME_BYTES:
        raise ValidationFailed("PreCompact synthetic frame cap exceeded")
    payload = bytearray()
    while len(payload) < size:
        chunk = connection.recv(min(4096, size - len(payload)))
        if not chunk:
            raise ConnectionError("PreCompact synthetic early EOF")
        payload.extend(chunk)
    if connection.recv(1):
        raise ValidationFailed("PreCompact synthetic trailing frame")
    return bytes(header + payload)


def serve_synthetic_once(
    listener: socket.socket, response_frame: bytes, *, timeout_seconds: float = 1.0
) -> SyntheticSupervisorObservation:
    if (
        type(listener) is not socket.socket
        or listener.family != socket.AF_UNIX
        or listener.type & socket.SOCK_STREAM != socket.SOCK_STREAM
        or type(timeout_seconds) is not float
        or not 0.01 <= timeout_seconds <= 2.0
    ):
        raise ValidationFailed("PreCompact exact synthetic listener required")
    listener.settimeout(timeout_seconds)
    connection, _address = listener.accept()
    try:
        connection.settimeout(timeout_seconds)
        request_frame = _receive_one(connection)
        observed = observe_synthetic_exchange(request_frame, response_frame)
        connection.sendall(response_frame)
        connection.shutdown(socket.SHUT_WR)
        return observed
    finally:
        connection.close()


def production_service_entry() -> int:
    """No-argument production closure; dormant until reviewed launchd issuance."""
    if not DARWIN_LAUNCHD_CAPABILITY_OBSERVED or not PRODUCTION_GENERATION_ISSUED:
        return os.EX_UNAVAILABLE
    generation = _DarwinAuthorityAdapter.acquire()
    return _ProductionService(generation).serve_once()


def _production_hook_round_trip(document: dict[str, object]) -> dict[str, object]:
    """Fixed production client closure; unavailable until OS issuance is accepted."""
    if type(document) is not dict:
        raise ValidationFailed("PreCompact exact hook selector required")
    if not DARWIN_LAUNCHD_CAPABILITY_OBSERVED or not PRODUCTION_GENERATION_ISSUED:
        raise PolicyViolation("PreCompact production generation unavailable")
    _DarwinAuthorityAdapter.acquire()
    raise PolicyViolation("PreCompact production client activation unavailable")


def assert_synthetic_cannot_promote(value: object) -> None:
    if type(value) is SyntheticSupervisorObservation:
        value.__post_init__()
    if DARWIN_LAUNCHD_CAPABILITY_OBSERVED or PRODUCTION_GENERATION_ISSUED:
        raise PolicyViolation("Dormant PreCompact production flags changed")
    try:
        copy.copy(value)
    except TypeError:
        return
    raise PolicyViolation("Synthetic PreCompact value is copyable")


def main() -> int:
    if os.environ.get("ZEKAM_PRECOMPACT_CANARY_NONCE"):
        return canary_service_entry()
    return production_service_entry()


if __name__ == "__main__":  # pragma: no cover - dormant executable boundary
    sys.exit(main())
