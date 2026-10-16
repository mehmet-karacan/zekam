"""Shell'siz, bounded process runner.

`shell=True` hicbir kosulda kullanilmaz: komut daima argv listesi olarak verilir.
Ortam degiskenleri allowlist ile aktarilir; cagiran surecin ortami oldugu gibi
devredilmez. Timeout zorunludur, cikti bayt siniri uygulanir ve ham cikti
kanonik kayda degil yalnizca digest'e donusur.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.sandbox import ProcessResult, ProcessSpec
from zekam.infrastructure.process.bounded_stream import (
    BoundedReader,
    finish_pipes,
    hard_kill_tree,
    start_process_tree,
    wait_for,
)

#: Cagiran ortamdan devralinmasina izin verilen degiskenler.
INHERITED_ENV_NAMES = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL")

_EMPTY_DIGEST = digest_of_bytes(b"")
MAX_STDIN_BYTES = 262_144


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    """Calistirma sonucu ve kirpilmis ham cikti.

    Ham cikti yalnizca cagiran katmanda gecici olarak kullanilir; kanonik
    kayitlara `result` icindeki digest yazilir.
    """

    result: ProcessResult
    stdout: bytes
    stderr: bytes


def build_env(spec: ProcessSpec, *, inherit: bool = True) -> dict[str, str]:
    """Allowlist temelli ortam kurar; cagiran ortami oldugu gibi devretmez."""

    environment: dict[str, str] = {}
    if inherit:
        for name in INHERITED_ENV_NAMES:
            value = os.environ.get(name)
            if value is not None:
                environment[name] = value
    environment.update(dict(spec.env))
    return environment


def run(
    spec: ProcessSpec,
    *,
    cwd: Path,
    inherit_env: bool = True,
    cancel: threading.Event | None = None,
    stdin: bytes | None = None,
) -> ProcessOutput:
    """Komutu sandbox calisma dizininde calistirir.

    Calisma dizini var olmalidir; aksi halde islem sessizce cagiran surecin
    dizininde calisirdi.
    """

    if not cwd.is_dir():
        raise PolicyViolation("sandbox calisma dizini bulunamadi")
    if stdin is not None and (not isinstance(stdin, bytes) or len(stdin) > MAX_STDIN_BYTES):
        raise ValidationFailed("stdin bounded bytes olmali")
    actual_input_digest = None if stdin is None else digest_of_bytes(stdin)
    if spec.stdin_digest != actual_input_digest:
        raise PolicyViolation("stdin spec digest bagina uymuyor")

    started = time.monotonic()
    if cancel is not None and cancel.is_set():
        raise PolicyViolation("komut iptal edildi")
    tree = start_process_tree(
        spec.argv, cwd, env=build_env(spec, inherit=inherit_env), stderr=subprocess.PIPE
    )
    process = tree.process
    assert process.stdout is not None and process.stderr is not None and process.stdin is not None
    limit = spec.max_output_bytes
    stdout_reader, stderr_reader = (
        BoundedReader(process.stdout, limit),
        BoundedReader(process.stderr, limit),
    )
    readers: list[threading.Thread] = []
    writer: threading.Thread | None = None
    input_complete = threading.Event()
    input_failed = threading.Event()

    def write_input() -> None:
        # Use the raw fd: closing a BufferedWriter concurrently with its blocked
        # write can wait for its lock and defeat the caller's timeout. Only this
        # thread writes, and the caller closes stdin after process-tree cleanup.
        assert stdin is not None and process.stdin is not None
        try:
            pending = memoryview(stdin)
            while pending:
                written = os.write(process.stdin.fileno(), pending[:65536])
                if written <= 0:
                    raise OSError("stdin short write")
                pending = pending[written:]
            input_complete.set()
        except (OSError, ValueError):
            input_failed.set()
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                input_failed.set()

    try:
        for reader in (stdout_reader, stderr_reader):
            thread = threading.Thread(target=reader.read, daemon=True)
            thread.start()
            readers.append(thread)
        if stdin is None:
            process.stdin.close()
        else:
            writer = threading.Thread(target=write_input, daemon=True)
            writer.start()
        outcome = wait_for(
            process,
            started + spec.timeout_seconds,
            stdout_reader.overflow,
            stderr_reader.overflow,
            cancel=cancel,
        )
        if outcome != "exited":
            hard_kill_tree(tree)
        finish_pipes(tree, tuple(readers))
        if writer is not None:
            writer.join(timeout=1)
            if writer.is_alive():
                raise PolicyViolation("stdin writer terminal olmadi")
        if outcome == "cancelled" or (cancel is not None and cancel.is_set()):
            raise PolicyViolation("komut iptal edildi; gec sonuc reddedildi")
    except BaseException:
        try:
            hard_kill_tree(tree)
        finally:
            finish_pipes(tree, tuple(readers))
            if writer is not None:
                writer.join(timeout=1)
        raise

    duration_ms = int((time.monotonic() - started) * 1000)
    timed_out = outcome == "deadline"
    truncated = stdout_reader.overflow.is_set() or stderr_reader.overflow.is_set()
    stdout, stderr = bytes(stdout_reader.buffer[:limit]), bytes(stderr_reader.buffer[:limit])
    exit_code = 124 if timed_out else process.returncode
    if exit_code is None:
        raise PolicyViolation("komut terminal status olmadan sonuc donduremez")
    if (
        stdin is not None
        and not timed_out
        and not truncated
        and (input_failed.is_set() or not input_complete.is_set())
    ):
        raise PolicyViolation("stdin tam teslim edilmedi; sonuc reddedildi")

    result = ProcessResult(
        spec_digest=spec.spec_digest,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout_digest=digest_of_bytes(stdout) if stdout else _EMPTY_DIGEST,
        stderr_digest=digest_of_bytes(stderr) if stderr else _EMPTY_DIGEST,
        truncated=truncated,
        timed_out=timed_out,
    )
    return ProcessOutput(result=result, stdout=stdout, stderr=stderr)
