"""Protocol-independent bounded subprocess stream and process-tree primitives.

Stdout and stderr are read concurrently with byte limits applied during the
stream, not after the process returns.  Time budgets use a monotonic clock;
after cancel/timeout a graceful shutdown is attempted first, then a bounded
hard stop with process-tree cleanup.

This module does not know about capability IPC or OpenCode event formats;
those protocols live in their own callers.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from zekam.domain.errors import PolicyViolation, ValidationFailed

DEFAULT_CANCELLATION_GRACE_SECONDS: Final = 10.0
_POLL_INTERVAL_SECONDS: Final = 0.01
_CREATE_BREAKAWAY_FROM_JOB: Final = 0x01000000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: Final = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    )


class _IoCounters(ctypes.Structure):
    _fields_ = (
        ("read_operation_count", ctypes.c_uint64),
        ("write_operation_count", ctypes.c_uint64),
        ("other_operation_count", ctypes.c_uint64),
        ("read_transfer_count", ctypes.c_uint64),
        ("write_transfer_count", ctypes.c_uint64),
        ("other_transfer_count", ctypes.c_uint64),
    )


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("basic_limit_information", _JobObjectBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    )


@dataclass(slots=True)
class _WindowsJob:
    handle: int | None

    def close(self) -> None:
        if self.handle is None:
            return
        kernel32 = _windows_kernel32()
        handle, self.handle = self.handle, None
        kernel32.CloseHandle(ctypes.c_void_p(handle))


@dataclass(slots=True)
class ProcessTree:
    process: subprocess.Popen[bytes]
    windows_job: _WindowsJob | None = None


@dataclass(slots=True)
class BoundedReader:
    """Concurrent, bounded reader for one byte stream.

    The buffer stores at most ``limit + 1`` bytes so that overflow can be
    detected reliably without unbounded growth.
    """

    stream: Any
    limit: int
    buffer: bytearray = field(default_factory=bytearray)
    overflow: threading.Event = field(default_factory=threading.Event)

    def read(self) -> None:
        while True:
            try:
                chunk = os.read(self.stream.fileno(), 65_536)
            except OSError:
                return
            if not chunk:
                return
            remaining = self.limit + 1 - len(self.buffer)
            if remaining > 0:
                self.buffer.extend(chunk[:remaining])
            if len(self.buffer) > self.limit or len(chunk) > remaining:
                self.overflow.set()


def start_process_tree(
    argv: tuple[str, ...],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    stderr: int = subprocess.DEVNULL,
    start_new_session: bool = True,
) -> ProcessTree:
    """Start ``argv`` in its own process group or Windows Job Object."""

    if not argv or any(not isinstance(part, str) or not part for part in argv):
        raise ValidationFailed("subprocess argv gecersiz")
    if not cwd.is_dir():
        raise PolicyViolation("subprocess calisma dizini bulunamadi")
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": stderr,
        "env": env if env is not None else worker_env(),
        "shell": False,
    }
    windows_job: _WindowsJob | None = None
    if os.name == "nt":
        windows_job = _create_windows_job()
        kwargs["creationflags"] = (
            int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP"))  # noqa: B009
            | _CREATE_BREAKAWAY_FROM_JOB
        )
    elif start_new_session:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(list(argv), **kwargs)
    except OSError as exc:
        if windows_job is not None:
            windows_job.close()
        raise PolicyViolation("subprocess baslatilamadi") from exc
    if windows_job is not None:
        try:
            _assign_windows_job(windows_job, process)
        except PolicyViolation:
            try:
                process.kill()
                process.wait(timeout=5)
            finally:
                windows_job.close()
            raise
    return ProcessTree(process, windows_job)


def hard_kill_tree(tree: ProcessTree) -> None:
    """Force-terminate the whole process tree."""

    process = tree.process
    if tree.windows_job is not None:
        tree.windows_job.close()
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.kill()
        return
    try:
        process_group = getattr(os, "getpgid")(process.pid)  # noqa: B009
        getattr(os, "killpg")(  # noqa: B009
            process_group,
            getattr(signal, "SIGKILL"),  # noqa: B009
        )
    except OSError:
        if process.poll() is None:
            process.kill()


def finish_pipes(
    tree: ProcessTree,
    reader_threads: tuple[threading.Thread, ...],
    *,
    wait_timeout: float = 5.0,
    reader_join_timeout: float = 5.0,
) -> None:
    """Close stdin/stdout and wait for reader threads without leaking handles."""

    process = tree.process
    try:
        try:
            process.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired:
            hard_kill_tree(tree)
            process.wait(timeout=wait_timeout)
    finally:
        if tree.windows_job is not None:
            tree.windows_job.close()
        if process.stdin is not None:
            process.stdin.close()
        for thread in reader_threads:
            thread.join(timeout=reader_join_timeout)
        if process.stdout is not None:
            process.stdout.close()


def wait_for(
    process: subprocess.Popen[bytes],
    deadline: float,
    *overflows: threading.Event,
) -> str:
    """Wait until the process exits, the deadline passes, or a buffer overflows."""

    while True:
        if any(event.is_set() for event in overflows):
            return "overflow"
        if process.poll() is not None:
            return "exited"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "deadline"
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def worker_env() -> dict[str, str]:
    """Minimal, deterministic environment for child Python workers."""

    allowed = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONPATH")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    source_root = str(Path(__file__).resolve().parents[3])
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root if not existing_pythonpath else source_root + os.pathsep + existing_pythonpath
    )
    environment["PYTHONUTF8"] = "1"
    return environment


def _windows_kernel32() -> Any:
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.IsProcessInJob.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    )
    kernel32.IsProcessInJob.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def _create_windows_job() -> _WindowsJob:
    kernel32 = _windows_kernel32()
    raw_handle = kernel32.CreateJobObjectW(None, None)
    if not raw_handle:
        raise PolicyViolation("Windows Job Object olusturulamadi")
    handle = int(raw_handle)
    information = _JobObjectExtendedLimitInformation()
    information.basic_limit_information.limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        ctypes.c_void_p(handle),
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    if not configured:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise PolicyViolation("Windows Job Object yapilandirilamadi")
    return _WindowsJob(handle)


def _assign_windows_job(job: _WindowsJob, process: subprocess.Popen[bytes]) -> None:
    if job.handle is None:
        raise PolicyViolation("Windows Job Object kapali")
    kernel32 = _windows_kernel32()
    process_handle = ctypes.c_void_p(int(process._handle))  # type: ignore[attr-defined]
    assigned = kernel32.AssignProcessToJobObject(ctypes.c_void_p(job.handle), process_handle)
    if not assigned:
        raise PolicyViolation("child Windows Job Object'a guvenle atanamadi")
    in_exact_job = ctypes.c_int()
    checked = kernel32.IsProcessInJob(
        process_handle, ctypes.c_void_p(job.handle), ctypes.byref(in_exact_job)
    )
    if not checked or not in_exact_job.value:
        raise PolicyViolation("child Windows Job Object binding dogrulanamadi")
