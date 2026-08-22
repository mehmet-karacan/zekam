"""Shell'siz, bounded process runner.

`shell=True` hicbir kosulda kullanilmaz: komut daima argv listesi olarak verilir.
Ortam degiskenleri allowlist ile aktarilir; cagiran surecin ortami oldugu gibi
devredilmez. Timeout zorunludur, cikti bayt siniri uygulanir ve ham cikti
kanonik kayda degil yalnizca digest'e donusur.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation
from zekam.domain.sandbox import ProcessResult, ProcessSpec

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


def run(spec: ProcessSpec, *, cwd: Path, inherit_env: bool = True) -> ProcessOutput:
    """Komutu sandbox calisma dizininde calistirir.

    Calisma dizini var olmalidir; aksi halde islem sessizce cagiran surecin
    dizininde calisirdi.
    """

    if not cwd.is_dir():
        raise PolicyViolation("sandbox calisma dizini bulunamadi")

    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            list(spec.argv),
            cwd=str(cwd),
            env=build_env(spec, inherit=inherit_env),
            capture_output=True,
            timeout=spec.timeout_seconds,
            check=False,
            shell=False,
        )
        stdout, stderr, exit_code = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as expired:
        timed_out = True
        stdout = expired.stdout or b""
        stderr = expired.stderr or b""
        exit_code = 124
    except OSError as exc:
        raise PolicyViolation("komut calistirilamadi") from exc

    duration_ms = int((time.monotonic() - started) * 1000)
    limit = spec.max_output_bytes
    truncated = len(stdout) > limit or len(stderr) > limit
    stdout, stderr = stdout[:limit], stderr[:limit]

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
