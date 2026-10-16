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
from zekam.domain.errors import PolicyViolation
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
    spec: ProcessSpec, *, cwd: Path, inherit_env: bool = True, cancel: threading.Event | None = None
) -> ProcessOutput:
    """Komutu sandbox calisma dizininde calistirir.

    Calisma dizini var olmalidir; aksi halde islem sessizce cagiran surecin
    dizininde calisirdi.
    """

    if not cwd.is_dir():
        raise PolicyViolation("sandbox calisma dizini bulunamadi")

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
    try:
        for reader in (stdout_reader, stderr_reader):
            thread = threading.Thread(target=reader.read, daemon=True)
            thread.start()
            readers.append(thread)
        process.stdin.close()
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
        if outcome == "cancelled" or (cancel is not None and cancel.is_set()):
            raise PolicyViolation("komut iptal edildi; gec sonuc reddedildi")
    except BaseException:
        try:
            hard_kill_tree(tree)
        finally:
            finish_pipes(tree, tuple(readers))
        raise

    duration_ms = int((time.monotonic() - started) * 1000)
    timed_out = outcome == "deadline"
    truncated = stdout_reader.overflow.is_set() or stderr_reader.overflow.is_set()
    stdout, stderr = bytes(stdout_reader.buffer[:limit]), bytes(stderr_reader.buffer[:limit])
    exit_code = 124 if timed_out else process.returncode
    if exit_code is None:
        raise PolicyViolation("komut terminal status olmadan sonuc donduremez")

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
