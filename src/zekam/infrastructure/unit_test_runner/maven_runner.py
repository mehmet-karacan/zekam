"""Maven calistirici: explicit argv/cwd, filtreli ortam, build kilidi, surec agaci temizligi.

- ``shell=True`` yoktur; argv ``MavenPlan``'dan gelir ve ``.cmd`` kopru karakter kumesi
  ile sinirlidir. Launcher/wrapper/root drift'i calistirma aninda tekrar dogrulanir.
- Ortam allowlist + secret-ad filtresiyle kurulur; ``MAVEN_OPTS``/``JAVA_TOOL_OPTIONS``
  gibi arguman/agent enjekte eden degiskenler dusurulur. Cikti redakte edilir; ham cikti
  kanonik kayda girmez (yalniz digest + redakte kuyruk).
- Proje/reactor basina tek build kilidi (cross-process dosya kilidi) ``target/`` yarisini
  engeller; kilit proje kokune dosya yazmaz (``lock_dir`` cagirandan gelir).
- Calistirmadan once onceki kosunun JaCoCo exec (``append`` ile birikir) ve Surefire
  raporlari karantinaya (rename) alinir; boylece eski veri yeni kosuya karisamaz.
- Timeout/cancel/output-overflow: Windows Job Object / POSIX process group ile alt surec
  agaci birlikte sonlandirilir.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from zekam.application.unit_test_measurement import RunStatus
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.process.bounded_stream import (
    BoundedReader,
    ProcessTree,
    finish_pipes,
    hard_kill_tree,
    start_process_tree,
)
from zekam.infrastructure.unit_test_runner.maven_plan import (
    SAFE_ARG,
    ExecutionAuthorization,
    MavenPlan,
    check_authorization,
    verify_launcher,
)
from zekam.infrastructure.unit_test_runner.path_safety import resolve_inside

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

ALLOWED_ENV: Final = frozenset(
    {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
        "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
        "JAVA_HOME", "MAVEN_HOME", "M2_HOME", "LANG", "LC_ALL", "OS",
    }
)  # fmt: skip
#: Arguman/agent enjekte edebilen degiskenler: her zaman dusurulur ve adlari kaydedilir.
RISKY_ENV: Final = frozenset(
    {"MAVEN_OPTS", "MAVEN_ARGS", "MAVEN_CONFIG", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
     "JDK_JAVA_OPTIONS", "CLASSPATH", "JAVA_OPTS"}
)  # fmt: skip
SECRET_NAME: Final = re.compile(
    r"(?i)(secret|token|passw|credential|api[-_]?key|private|auth|cookie|session|bearer|signing)"
)
_SECRET_ASSIGNMENT: Final = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[-_]?key)[A-Za-z0-9_.-]*)"
    r"(\s*[=:]\s*)(\S+)"
)
_PLUGIN_LINE: Final = re.compile(
    r"---\s+([A-Za-z0-9_.-]+):([A-Za-z0-9_.+-]+):([A-Za-z0-9_.:-]+)\s+\("
)
_CMD_PATH_FORBIDDEN: Final = re.compile(r'[&|<>^%"!\r\n]')
MAX_OUTPUT_BYTES: Final = 4 * 1024 * 1024
TAIL_CHARS: Final = 4000
_MTIME_TOLERANCE_NS: Final = 2_000_000_000


@dataclass(frozen=True, slots=True)
class FilteredEnvironment:
    values: dict[str, str]
    dropped_risky: tuple[str, ...]
    secret_values: tuple[str, ...]


def filter_environment(source: Mapping[str, str]) -> FilteredEnvironment:
    """Allowlist + secret-ad filtresi; dusurulen riskli adlar kaydedilir, degerleri degil."""

    values: dict[str, str] = {}
    risky: list[str] = []
    secrets: list[str] = []
    for name, value in source.items():
        upper = name.upper()
        if SECRET_NAME.search(name):
            if len(value) >= 6:
                secrets.append(value)
            continue
        if upper in RISKY_ENV:
            risky.append(name)
            continue
        if upper in ALLOWED_ENV:
            values[name] = value
    return FilteredEnvironment(values, tuple(sorted(risky)), tuple(secrets))


def redact(text: str, secret_values: tuple[str, ...]) -> str:
    for secret in sorted(set(secret_values), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


def extract_plugin_executions(output: str) -> tuple[tuple[str, str, str], ...]:
    """Console'daki plugin kimligi satirlari; YALNIZ arac/plugin kaydi, test sonucu degil."""

    found: dict[tuple[str, str, str], None] = {}
    for match in _PLUGIN_LINE.finditer(output):
        found[(match.group(1), match.group(2), match.group(3))] = None
    return tuple(found)


class BuildLockBusy(PolicyViolation):
    """Ayni proje/reactor build kaynagi baska kosuda."""

    code = "build-lock-busy"


@contextlib.contextmanager
def build_lock(lock_dir: Path, project_root: Path, *, wait_seconds: float = 0.0) -> Iterator[None]:
    """Proje/reactor kokune bagli cross-process kilit (kilit dosyasi ``lock_dir``'de)."""

    key = os.path.normcase(str(project_root.resolve(strict=True)))
    name = "maven-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / name, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    try:
        while True:
            try:
                if sys.platform == "win32":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise BuildLockBusy("Build kaynagi baska bir kosuda (serializasyon)") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- rapor konumlari


def _target_dir(module: str) -> str:
    return f"{module}/target" if module else "target"


def surefire_dir(module: str) -> str:
    return f"{_target_dir(module)}/surefire-reports"


def jacoco_exec_path(module: str) -> str:
    return f"{_target_dir(module)}/jacoco.exec"


def jacoco_xml_path(module: str) -> str:
    return f"{_target_dir(module)}/site/jacoco/jacoco.xml"


def _integration_artifacts(module: str) -> tuple[str, ...]:
    base = _target_dir(module)
    return (f"{base}/failsafe-reports", f"{base}/jacoco-it.exec", f"{base}/site/jacoco-it")


def quarantine_stale_outputs(root: Path, modules: tuple[str, ...], run_id: str) -> tuple[str, ...]:
    """Onceki kosunun exec/rapor ciktilarini ``*.stale-<run>`` adina tasir (silmez)."""

    moved: list[str] = []
    suffix = f".stale-{hashlib.sha256(run_id.encode('utf-8')).hexdigest()[:12]}"
    for module in modules:
        for relative in (
            surefire_dir(module),
            jacoco_exec_path(module),
            jacoco_xml_path(module),
            *_integration_artifacts(module),
        ):
            path = resolve_inside(root, relative) if (root / relative).exists() else None
            if path is None:
                continue
            destination = path.with_name(path.name + suffix)
            counter = 0
            while destination.exists():
                counter += 1
                destination = path.with_name(f"{path.name}{suffix}-{counter}")
            try:
                path.rename(destination)
            except OSError as exc:
                raise PolicyViolation("Eski rapor karantinaya alinamadi; kosu reddedildi") from exc
            moved.append(relative)
    return tuple(moved)


@dataclass(frozen=True, slots=True)
class CollectedFile:
    relative: str
    digest: str
    data: bytes


@dataclass(frozen=True, slots=True)
class CollectedOutputs:
    surefire: dict[str, tuple[CollectedFile, ...]]
    jacoco_xml: dict[str, CollectedFile]
    exec_digests: dict[str, str]
    integration_changed: bool
    stale: tuple[str, ...]


def _fresh(path: Path, start_ns: int) -> bool:
    return path.stat().st_mtime_ns >= start_ns - _MTIME_TOLERANCE_NS


def _read_collected(
    root: Path, relative: str, max_bytes: int, start_ns: int, stale: list[str]
) -> CollectedFile | None:
    path = resolve_inside(root, relative)
    if not path.is_file():
        return None
    if not _fresh(path, start_ns):
        stale.append(relative)
        return None
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValidationFailed("Rapor boyut sinirini asiyor")
    return CollectedFile(relative, digest_of_bytes(data), data)


def collect_outputs(
    root: Path, modules: tuple[str, ...], start_ns: int, *, max_report_bytes: int = 64 * 1024 * 1024
) -> CollectedOutputs:
    """Kosudan sonra YALNIZ yeni olusan rapor/exec dosyalarini toplar (mtime yalniz ek kontrol)."""

    surefire: dict[str, tuple[CollectedFile, ...]] = {}
    jacoco: dict[str, CollectedFile] = {}
    execs: dict[str, str] = {}
    stale: list[str] = []
    integration = False
    for module in modules:
        directory = resolve_inside(root, surefire_dir(module))
        files: list[CollectedFile] = []
        if directory.is_dir():
            for entry in sorted(directory.glob("TEST-*.xml")):
                relative = f"{surefire_dir(module)}/{entry.name}"
                item = _read_collected(root, relative, 16 * 1024 * 1024, start_ns, stale)
                if item is not None:
                    files.append(item)
        surefire[module] = tuple(files)
        xml = _read_collected(root, jacoco_xml_path(module), max_report_bytes, start_ns, stale)
        if xml is not None:
            jacoco[module] = xml
        exec_path = resolve_inside(root, jacoco_exec_path(module))
        if exec_path.is_file():
            if _fresh(exec_path, start_ns):
                execs[module] = digest_of_bytes(exec_path.read_bytes())
            else:
                stale.append(jacoco_exec_path(module))
        integration = integration or any(
            (root / artifact).exists() for artifact in _integration_artifacts(module)
        )
    return CollectedOutputs(surefire, jacoco, execs, integration, tuple(stale))


def class_digests(root: Path, module: str, class_names: tuple[str, ...]) -> dict[str, str]:
    """``target/classes`` altindaki sinif ve inner class dosyalarinin digest'leri."""

    base = resolve_inside(root, f"{_target_dir(module)}/classes")
    result: dict[str, str] = {}
    for name in class_names:
        package, _, simple = name.rpartition("/")
        directory = base / package if package else base
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.name == f"{simple}.class" or (
                entry.name.startswith(simple + "$") and entry.suffix == ".class"
            ):
                key = f"{module}:{package + '/' if package else ''}{entry.name}"
                result[key] = digest_of_bytes(entry.read_bytes())
    return result


# ---------------------------------------------------------------- calistirma


@dataclass(frozen=True, slots=True)
class MavenRunResult:
    status: RunStatus
    exit_code: int | None
    duration_ms: int
    start_ns: int
    output_digest: str
    output_bytes: int
    output_tail: str
    plugin_executions: tuple[tuple[str, str, str], ...]
    dropped_risky_env: tuple[str, ...]
    quarantined: tuple[str, ...]
    tree_terminated: bool


def _validate_invocation(plan: MavenPlan) -> Path:
    root = Path(plan.project_root)
    if root.resolve(strict=True) != root:
        raise PolicyViolation("Proje koku plandakiyle ayni gercek yol degil (root drift)")
    verify_launcher(plan.launcher)
    for arg in plan.args:
        if SAFE_ARG.fullmatch(arg) is None:
            raise PolicyViolation("Arguman guvenli karakter kumesi disinda")
    if plan.launcher.bridge_required and _CMD_PATH_FORBIDDEN.search(plan.launcher.path):
        raise PolicyViolation("Launcher yolu cmd metakarakteri tasiyor")
    for module in plan.modules:
        resolve_inside(root, module)
    return root


def run_maven(
    plan: MavenPlan,
    authorization: ExecutionAuthorization | None,
    *,
    lock_dir: Path,
    run_id: str,
    timeout_seconds: float,
    cancel: threading.Event | None = None,
    environ: Mapping[str, str] | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    lock_wait_seconds: float = 0.0,
    quarantine: bool = True,
) -> MavenRunResult:
    """Plani yetkiyle, kilit altinda calistirir. Gercek exit code/sure/cikti digest'i doner."""

    if timeout_seconds <= 0:
        raise ValidationFailed("timeout pozitif olmali")
    check_authorization(plan, authorization)
    root = _validate_invocation(plan)
    filtered = filter_environment(os.environ if environ is None else environ)
    with build_lock(lock_dir, root, wait_seconds=lock_wait_seconds):
        verify_launcher(plan.launcher)  # kilit beklerken drift olmasin
        moved = quarantine_stale_outputs(root, plan.modules or ("",), run_id) if quarantine else ()
        start_ns = time.time_ns()
        started = time.monotonic()
        try:
            tree = start_process_tree(
                plan.argv, root, env=filtered.values, stderr=subprocess.STDOUT
            )
        except PolicyViolation:
            return MavenRunResult(
                RunStatus.LAUNCH_FAILED, None, 0, start_ns, digest_of_bytes(b""), 0, "", (),
                filtered.dropped_risky, moved, True,
            )  # fmt: skip
        status, exit_code, data, terminated = _supervise(
            tree, started + timeout_seconds, cancel, max_output_bytes
        )
    text = data.decode("utf-8", errors="replace")
    tail = redact(text[-TAIL_CHARS:], filtered.secret_values)
    plugins = extract_plugin_executions(redact(text, filtered.secret_values))
    return MavenRunResult(
        status,
        exit_code,
        int((time.monotonic() - started) * 1000),
        start_ns,
        digest_of_bytes(data),
        len(data),
        tail,
        plugins,
        filtered.dropped_risky,
        moved,
        terminated,
    )


def _supervise(
    tree: ProcessTree, deadline: float, cancel: threading.Event | None, limit: int
) -> tuple[RunStatus, int | None, bytes, bool]:
    process = tree.process
    assert process.stdout is not None
    if process.stdin is not None:
        process.stdin.close()
    reader = BoundedReader(process.stdout, limit)
    thread = threading.Thread(target=reader.read, daemon=True)
    thread.start()
    status = RunStatus.COMPLETED
    while process.poll() is None:
        if reader.overflow.is_set():
            status = RunStatus.OUTPUT_OVERFLOW
        elif cancel is not None and cancel.is_set():
            status = RunStatus.CANCELLED
        elif time.monotonic() >= deadline:
            status = RunStatus.TIMED_OUT
        if status is not RunStatus.COMPLETED:
            hard_kill_tree(tree)
            break
        time.sleep(0.01)
    finish_pipes(tree, (thread,))
    exit_code = process.returncode if status is RunStatus.COMPLETED else None
    return status, exit_code, bytes(reader.buffer[:limit]), process.poll() is not None
