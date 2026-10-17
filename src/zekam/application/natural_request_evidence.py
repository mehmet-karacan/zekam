"""Real readback adapters that turn claims into verified natural-request signals.

``natural_request_scope`` only evaluates signals; this module is where a claim is
checked against authority. ``read_work_project`` reads the operational Work
record and its project back from the existing SQLite operational store
(``home/state/operational.db``) without committing or mutating anything.
``verify_session_file`` re-reads an exact file inside the registered project
source root (via ``project_rag_runtime.resolve_project_source``), compares its
byte-level SHA-256, and returns metadata only (project slug, relative path,
digest, size) — never the file content.  Both fail closed: missing or archived
Work, missing or drifted source binding, escaping, symlinked or reparse-backed
paths, secret configuration files, binary content and stale digests all
refuse the signal.
"""

from __future__ import annotations

import codecs
import hashlib
import hmac
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.application.project_rag_runtime import resolve_project_source
from zekam.domain.canonical import parse_digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.identifiers import validate_slug
from zekam.domain.work import OPEN_STATES, WorkState
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

SCHEMA_WORK_PROJECT = "zekam-natural-request-work-project-evidence/v1"
SCHEMA_SESSION_FILE = "zekam-natural-request-session-file-evidence/v1"

#: A session-file readback must stay bounded; larger files are not evidence.
MAX_SESSION_FILE_BYTES = 256_000
_CHUNK_SIZE = 64 * 1024
_DIGEST_PREFIX = "sha256:"
_SECRET_SUFFIXES = (".properties",)
_BINARY_SUFFIXES = frozenset({".bin", ".exe", ".dll", ".pdf", ".zip", ".docx"})


@dataclass(frozen=True, slots=True)
class WorkProjectEvidence:
    """Operational Work record plus its resolved project, read back verbatim."""

    work_id: str
    work_state: str
    work_revision: int
    project_id: str
    project_slug: str
    project_revision: int

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": SCHEMA_WORK_PROJECT,
            "work_id": self.work_id,
            "work_state": self.work_state,
            "work_revision": self.work_revision,
            "project_id": self.project_id,
            "project_slug": self.project_slug,
            "project_revision": self.project_revision,
            "verified": True,
            "read_only": True,
            "grants_authority": False,
        }
        return body


@dataclass(frozen=True, slots=True)
class SessionFileEvidence:
    """Metadata of a verified session file; the content is never returned."""

    project_slug: str
    relative_path: str
    digest: str
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": SCHEMA_SESSION_FILE,
            "project_slug": self.project_slug,
            "relative_path": self.relative_path,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "verified": True,
            "read_only": True,
            "grants_authority": False,
            "content_included": False,
        }
        return body


def _required_bounded_text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValidationFailed(f"{label} bounded non-empty metin olmali")
    if len(value.encode("utf-8")) > limit:
        raise ValidationFailed(f"{label} bounded sinirini asiyor")
    return value


def read_work_project(home: Path, work_id: str) -> WorkProjectEvidence:
    """Read an open Work and its active project without committing."""

    _required_bounded_text(work_id, "Work kimligi", 128)
    try:
        store = SQLiteOperationalStore(Path(home) / "state" / "operational.db")
        with store.read_only_unit_of_work() as uow:
            try:
                work = uow.get_work(work_id)
            except ValidationFailed as exc:
                raise PolicyViolation("work-readback-missing") from exc
            try:
                project = uow.resolve_project(work.project_id, include_archived=True)
            except ValidationFailed as exc:
                raise PolicyViolation("work-project-unresolvable") from exc
    except (sqlite3.OperationalError, ConfigurationError) as exc:
        raise PolicyViolation("work-readback-unavailable") from exc
    if project.status != "active":
        raise PolicyViolation("work-project-archived")
    try:
        state = WorkState(work.state)
    except ValueError as exc:
        raise PolicyViolation("work-state-unknown") from exc
    if state not in OPEN_STATES:
        raise PolicyViolation("work-not-open")
    return WorkProjectEvidence(
        work_id=str(work.id),
        work_state=str(work.state),
        work_revision=int(work.revision),
        project_id=str(project.id),
        project_slug=str(project.slug),
        project_revision=int(project.revision),
    )


def _expected_digest(value: object) -> str:
    if not isinstance(value, str) or not value.startswith(_DIGEST_PREFIX):
        raise ValidationFailed("Beklenen digest sha256: onekli olmali")
    parse_digest(value)
    return value


def verify_session_file(
    home: Path, project_slug: str, relative_path: str, expected_sha256: str
) -> SessionFileEvidence:
    """Verify one file inside the registered source root; metadata only.

    The source root is resolved from the device-local private binding (never
    guessed from the slug).  The relative path must stay inside that exact
    root: absolute paths, traversal, symlinked or reparse-backed components,
    secret configuration files, binary content, missing targets and size or
    digest drift are all refused.  Only slug/path/digest/size metadata is
    returned; the content never leaves this function.
    """

    slug = validate_slug(project_slug)
    expected = _expected_digest(expected_sha256)
    root = resolve_project_source(Path(home), slug)
    relative = _required_bounded_text(relative_path, "Session dosya yolu", 1024)
    path = _safe_source_path(root, relative)
    if _is_secret_filename(path.name):
        raise PolicyViolation("session-file-secret-config")
    if path.suffix.casefold() in _BINARY_SUFFIXES:
        raise PolicyViolation("session-file-binary-type")
    if stat.S_ISLNK(path.lstat().st_mode) or _is_reparse(path.lstat()):
        raise PolicyViolation("session-file-symlink")
    actual, size = _stream_sha256(path)
    if not hmac.compare_digest(actual, expected):
        raise PolicyViolation("session-file-digest-mismatch")
    return SessionFileEvidence(
        project_slug=slug,
        relative_path=path.relative_to(root).as_posix(),
        digest=actual,
        size_bytes=size,
    )


def _is_reparse(info: os.stat_result) -> bool:
    return bool(
        int(getattr(info, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def _is_secret_filename(name: str) -> bool:
    lowered = name.casefold()
    return lowered == ".env" or lowered.startswith(".env.") or lowered.endswith(_SECRET_SUFFIXES)


def _safe_source_path(root: Path, relative: str) -> Path:
    if "\\" in relative or relative.startswith("/") or ":" in relative:
        raise ValidationFailed("session-file-path-absolute")
    if re.search(r"[\x00-\x1f]", relative):
        raise ValidationFailed("session-file-path-control-char")
    parts = relative.split("/")
    if not parts or len(parts) > 32 or any(not part for part in parts):
        raise ValidationFailed("session-file-path-traversal")
    current = root
    for index, part in enumerate(parts):
        if part in {".", ".."} or part.endswith((".", " ")):
            raise ValidationFailed("session-file-path-traversal")
        candidate = current / part
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise ValidationFailed("session-file-missing") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise PolicyViolation("session-file-symlink")
        if index == len(parts) - 1 and info.st_nlink != 1:
            raise PolicyViolation("session-file-hardlink")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValidationFailed("session-file-parent-not-directory")
        current = candidate
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise ValidationFailed("session-file-missing") from exc
    if resolved == root or not resolved.is_relative_to(root):
        raise PolicyViolation("session-file-path-escape")
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise ValidationFailed("session-file-not-regular")
    return resolved


def _stream_sha256(path: Path) -> tuple[str, int]:
    before = path.lstat()
    if before.st_nlink != 1:
        raise PolicyViolation("session-file-hardlink")
    if before.st_size > MAX_SESSION_FILE_BYTES:
        raise ValidationFailed("session-file-oversized")
    hasher = hashlib.sha256()
    size = 0
    first = True
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    with path.open("rb", buffering=0) as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise PolicyViolation("session-file-readback-race")
        if opened.st_nlink != 1:
            raise PolicyViolation("session-file-hardlink")
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            size += len(chunk)
            if size > MAX_SESSION_FILE_BYTES:
                raise ValidationFailed("session-file-oversized")
            if any(byte < 32 and byte not in (9, 10, 12, 13) for byte in chunk) or (
                first and chunk.startswith((b"%PDF", b"PK\x03\x04"))
            ):
                raise ValidationFailed("session-file-binary")
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                raise ValidationFailed("session-file-binary") from exc
            first = False
            hasher.update(chunk)
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ValidationFailed("session-file-binary") from exc
        finished = os.fstat(stream.fileno())
    after = path.lstat()
    if finished.st_nlink != 1 or after.st_nlink != 1:
        raise PolicyViolation("session-file-hardlink")
    if (
        any(
            value != expected
            for value, expected in (
                (finished.st_size, before.st_size),
                (finished.st_mtime_ns, before.st_mtime_ns),
                (after.st_dev, before.st_dev),
                (after.st_ino, before.st_ino),
                (after.st_mtime_ns, before.st_mtime_ns),
            )
        )
        or size != before.st_size
    ):
        raise PolicyViolation("session-file-readback-race")
    return _DIGEST_PREFIX + hasher.hexdigest(), size
