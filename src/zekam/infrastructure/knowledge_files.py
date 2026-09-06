"""Symlink-safe, atomic filesystem adapter for knowledge projections and CAS."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from zekam.application.knowledge_file_plane import (
    PROJECT_PROJECTION_SCHEMA,
    ArtifactPutPlan,
    KnowledgeClassification,
    KnowledgeNoteManifest,
    ProjectProjection,
    assert_public_safe_projection,
    note_content_digest,
    validate_generated_note,
    validate_portable_relative,
)
from zekam.application.operational_store import ArtifactRefRecord, KnowledgeNoteRecord
from zekam.domain.errors import (
    ConcurrencyConflict,
    LayoutError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_file,
)


@dataclass(frozen=True, slots=True, order=True)
class KnowledgeFileIssue:
    kind: str
    portable_ref: str


_AUDIT_NOTE_ROOTS = ("global", "projeler", "inbox", "archive")
_AUDIT_LIMIT = 10_000


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(
        int(getattr(info, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


class KnowledgeFileStore:
    """Store source Markdown and sole-copy CAS bytes; DB rows remain manifests only."""

    def __init__(self, home: Path) -> None:
        if not home.is_absolute() or not home.is_dir() or home.is_symlink():
            raise LayoutError("Knowledge home absolute mevcut regular directory olmali")
        self.home = home.resolve()
        identity = self.home.stat(follow_symlinks=False)
        self._home_identity = (identity.st_dev, identity.st_ino)
        self._archive_lock = threading.Lock()

    def _open_home(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.home, flags)
        identity = os.fstat(descriptor)
        if (identity.st_dev, identity.st_ino) != self._home_identity:
            os.close(descriptor)
            raise LayoutError("Knowledge home identity drift")
        return descriptor

    @contextmanager
    def _parent_handle(self, relative: str, *, create: bool) -> Iterator[tuple[int, str, str]]:
        portable = validate_portable_relative(relative)
        parts = PurePosixPath(portable).parts
        descriptor = self._open_home()
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for part in parts[:-1]:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    raise
                except OSError as exc:
                    raise LayoutError(
                        "Knowledge parent symlink veya race-safe directory disi"
                    ) from exc
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1], portable
        finally:
            os.close(descriptor)

    def _read_optional(self, relative: str, *, max_bytes: int) -> bytes | None:
        if os.name == "nt":
            path, _ = self._windows_leaf(relative, create_parent=False)
            if path is None:
                return None
            if not private_regular(path):
                raise LayoutError("Knowledge leaf private regular file olmali")
            if path.stat().st_size > max_bytes:
                raise LayoutError("Knowledge leaf bounded size sinirini asiyor")
            payload = path.read_bytes()
            if len(payload) > max_bytes:
                raise LayoutError("Knowledge leaf bounded size sinirini asiyor")
            return payload
        # A substituted FIFO must reach the regular-file check instead of waiting
        # indefinitely for a writer. Regular-file read semantics are unchanged.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            with self._parent_handle(relative, create=False) as (parent, name, _):
                try:
                    descriptor = os.open(name, flags, dir_fd=parent)
                except FileNotFoundError:
                    return None
                except OSError as exc:
                    raise LayoutError("Knowledge leaf symlink/regular-file disi") from exc
                try:
                    metadata = os.fstat(descriptor)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise LayoutError("Knowledge leaf regular file olmali")
                    if metadata.st_size > max_bytes:
                        raise LayoutError("Knowledge leaf bounded size sinirini asiyor")
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > max_bytes:
                            raise LayoutError("Knowledge leaf bounded size sinirini asiyor")
                    return b"".join(chunks)
                finally:
                    os.close(descriptor)
        except FileNotFoundError:
            return None

    def read_note(
        self, manifest: KnowledgeNoteManifest, *, relative_ref: str | None = None
    ) -> bytes:
        """Read one manifested note through the race/reparse-safe file boundary."""

        relative = validate_portable_relative(relative_ref or manifest.portable_ref)
        if manifest.state == "archived":
            expected = f"archive/{manifest.owner_scope.replace(':', '/')}/"
            if not relative.startswith(expected):
                raise LayoutError("Archived knowledge note owner archive root disinda")
        if manifest.state != "archived" and relative != manifest.portable_ref:
            raise LayoutError("Active knowledge note portable ref drift")
        payload = self._read_optional(relative, max_bytes=2 * 1024 * 1024)
        if payload is None:
            raise LayoutError("Knowledge note bulunamadi")
        if note_content_digest(payload) != manifest.content_digest:
            raise ConcurrencyConflict("Knowledge note content digest drift")
        return payload

    def _unlink(self, relative: str) -> None:
        if os.name == "nt":
            for attempt in range(8):
                path, _ = self._windows_leaf(relative, create_parent=False)
                if path is None:
                    return
                try:
                    path.unlink()
                    return
                except FileNotFoundError:
                    return
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(min(0.005 * (2**attempt), 0.05))
            return
        with self._parent_handle(relative, create=False) as (parent, name, _):
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                return
            os.fsync(parent)

    def _atomic_write(self, relative: str, payload: bytes, *, replace_existing: bool) -> Path:
        if os.name == "nt":
            return self._atomic_write_windows(relative, payload, replace_existing=replace_existing)
        with self._parent_handle(relative, create=True) as (parent, name, portable):
            stage = f".{name}.stage-{secrets.token_hex(12)}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(stage, flags, 0o600, dir_fd=parent)
            try:
                view = memoryview(payload)
                written = 0
                while written < len(view):
                    count = os.write(descriptor, view[written:])
                    if count <= 0:
                        raise OSError("Knowledge stage short write")
                    written += count
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                if not replace_existing:
                    try:
                        os.link(
                            stage,
                            name,
                            src_dir_fd=parent,
                            dst_dir_fd=parent,
                            follow_symlinks=False,
                        )
                    except FileExistsError as exc:
                        raise ConcurrencyConflict("Knowledge target zaten var") from exc
                else:
                    os.replace(stage, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
                if self._read_optional(portable, max_bytes=max(1, len(payload))) != payload:
                    raise LayoutError("Knowledge publish path identity drift")
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                with suppress(FileNotFoundError):
                    os.unlink(stage, dir_fd=parent)
            return self.home / portable

    def _windows_leaf(
        self, relative: str, *, create_parent: bool
    ) -> tuple[Path | None, tuple[int, int]]:
        portable = validate_portable_relative(relative)
        parts = PurePosixPath(portable).parts
        if not private_directory(self.home):
            raise LayoutError("Knowledge home reparse veya ACL drift")
        home_identity = self.home.lstat()
        if (home_identity.st_dev, home_identity.st_ino) != self._home_identity:
            raise LayoutError("Knowledge home identity drift")
        parent = self.home
        for part in parts[:-1]:
            parent /= part
            if not parent.exists():
                if not create_parent:
                    return None, (0, 0)
                with suppress(FileExistsError):
                    parent.mkdir()
            if not private_directory(parent):
                raise LayoutError("Knowledge parent symlink/reparse veya ACL drift")
        identity = parent.lstat()
        target = parent / parts[-1]
        if target.exists() or target.is_symlink():
            info = target.lstat()
            reparse = bool(
                int(getattr(info, "st_file_attributes", 0))
                & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            )
            if not stat.S_ISREG(info.st_mode) or target.is_symlink() or reparse:
                raise LayoutError("Knowledge leaf symlink/reparse veya regular-file disi")
        elif not create_parent:
            return None, (identity.st_dev, identity.st_ino)
        return target, (identity.st_dev, identity.st_ino)

    def _atomic_write_windows(
        self, relative: str, payload: bytes, *, replace_existing: bool
    ) -> Path:
        target, parent_identity = self._windows_leaf(relative, create_parent=True)
        assert target is not None
        stage = target.with_name(f".{target.name}.stage-{secrets.token_hex(12)}")
        current_parent = target.parent.lstat()
        if (
            current_parent.st_dev,
            current_parent.st_ino,
        ) != parent_identity or not private_directory(target.parent):
            raise LayoutError("Knowledge parent identity drift")
        descriptor = os.open(
            stage,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise OSError("Knowledge stage short write")
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            restrict_private_file(stage)
            current_parent = target.parent.lstat()
            if (
                current_parent.st_dev,
                current_parent.st_ino,
            ) != parent_identity or not private_directory(target.parent):
                raise LayoutError("Knowledge parent identity drift")
            if replace_existing:
                os.replace(stage, target)
            else:
                try:
                    os.link(stage, target, follow_symlinks=False)
                except FileExistsError as exc:
                    raise ConcurrencyConflict("Knowledge target zaten var") from exc
                stage.unlink()
            if self._read_optional(relative, max_bytes=max(1, len(payload))) != payload:
                raise LayoutError("Knowledge publish path identity drift")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            stage.unlink(missing_ok=True)
        return target

    def put_artifact(self, plan: ArtifactPutPlan, payload: bytes) -> Path:
        actual = ArtifactPutPlan.create(
            payload,
            media_type=plan.media_type,
            classification=plan.classification,
        )
        if actual != plan:
            raise ConcurrencyConflict("Artifact put plan payload drift")
        existing = self._read_optional(plan.relative_path, max_bytes=64 * 1024 * 1024)
        if existing is not None:
            if hashlib.sha256(existing).hexdigest() != plan.digest.removeprefix("sha256:"):
                raise ConcurrencyConflict("CAS target digest drift")
            return self.home / plan.relative_path
        try:
            return self._atomic_write(plan.relative_path, payload, replace_existing=False)
        except ConcurrencyConflict:
            existing = self._read_optional(plan.relative_path, max_bytes=64 * 1024 * 1024)
            if existing is None or hashlib.sha256(existing).hexdigest() != (
                plan.digest.removeprefix("sha256:")
            ):
                raise
            return self.home / plan.relative_path

    def publish_project_projection(self, projection: ProjectProjection) -> Path:
        payload = projection.render()
        assert_public_safe_projection(
            payload, relative_path=f"projeler/{projection.slug}/PROJECT.yaml"
        )
        relative = f"projeler/{projection.slug}/PROJECT.yaml"
        existing_payload = self._read_optional(relative, max_bytes=2 * 1024 * 1024)
        replace_existing = False
        if existing_payload is not None:
            try:
                existing = yaml.safe_load(existing_payload.decode("utf-8"))
            except (UnicodeDecodeError, yaml.YAMLError) as exc:
                raise PolicyViolation("Mevcut PROJECT.yaml user/bozuk; overwrite edilemez") from exc
            if (
                not isinstance(existing, dict)
                or existing.get("schema") != PROJECT_PROJECTION_SCHEMA
                or existing.get("project_id") != projection.project_id
            ):
                raise PolicyViolation("Mevcut PROJECT.yaml authority binding drift")
            if existing_payload == payload:
                return self.home / relative
            replace_existing = True
        return self._atomic_write(relative, payload, replace_existing=replace_existing)

    def create_note(self, manifest: KnowledgeNoteManifest, payload: bytes) -> Path:
        if manifest.classification is KnowledgeClassification.SECRET:
            raise PolicyViolation("Secret note normal file plane yerine secret backend ister")
        if note_content_digest(payload) != manifest.content_digest:
            raise ConcurrencyConflict("Knowledge note content digest drift")
        if manifest.classification is KnowledgeClassification.PUBLIC:
            assert_public_safe_projection(payload, relative_path=manifest.portable_ref)
        if manifest.authorship == "generated":
            metadata = validate_generated_note(payload)
            if (
                metadata["owner_scope"] != manifest.owner_scope
                or metadata["project_slug"] != manifest.project_slug
                or metadata["note_kind"] != manifest.note_kind
                or metadata["classification"] != manifest.classification.value
            ):
                raise ConcurrencyConflict("Generated note manifest metadata drift")
        existing = self._read_optional(manifest.portable_ref, max_bytes=2 * 1024 * 1024)
        if existing is not None:
            if existing == payload:
                return self.home / manifest.portable_ref
            raise PolicyViolation("Knowledge note overwrite yasak; correction/revision gerekli")
        try:
            return self._atomic_write(manifest.portable_ref, payload, replace_existing=False)
        except ConcurrencyConflict:
            existing = self._read_optional(manifest.portable_ref, max_bytes=2 * 1024 * 1024)
            if existing != payload:
                raise
            return self.home / manifest.portable_ref

    def write_private_binding(self, relative: str, payload: bytes) -> Path:
        """Atomically write a private project binding through the safe home boundary."""

        portable = validate_portable_relative(relative)
        parts = PurePosixPath(portable).parts
        if (
            len(parts) != 4
            or parts[0] != "projeler"
            or parts[2] != "baglantilar"
            or not parts[3].endswith(".json")
        ):
            raise LayoutError("Private binding project baglantilar JSON ref ister")
        existing = self._read_optional(portable, max_bytes=2 * 1024 * 1024)
        if existing == payload:
            return self.home / portable
        return self._atomic_write(portable, payload, replace_existing=existing is not None)

    def read_private_binding(self, relative: str) -> dict[str, object]:
        """Read one bounded project binding through the same safe path boundary."""

        portable = validate_portable_relative(relative)
        parts = PurePosixPath(portable).parts
        if (
            len(parts) != 4
            or parts[0] != "projeler"
            or parts[2] != "baglantilar"
            or not parts[3].endswith(".json")
        ):
            raise LayoutError("Private binding project baglantilar JSON ref ister")
        payload = self._read_optional(portable, max_bytes=2 * 1024 * 1024)
        if payload is None:
            raise LayoutError("Private binding bulunamadi")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("Private binding strict JSON olmali") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("Private binding JSON object olmali")
        return value

    def archive_note(self, manifest: KnowledgeNoteManifest) -> str:
        with self._archive_lock:
            return self._archive_note_locked(manifest)

    def _archive_note_locked(self, manifest: KnowledgeNoteManifest) -> str:
        if manifest.state not in {"inbox", "active"}:
            raise PolicyViolation("Yalniz inbox/active note archive edilebilir")
        source_name = PurePosixPath(manifest.portable_ref).name
        scope_parts = manifest.owner_scope.replace(":", "/")
        target_ref = f"archive/{scope_parts}/{manifest.content_digest[7:]}-{source_name}"
        target_payload = self._read_optional(target_ref, max_bytes=2 * 1024 * 1024)
        if target_payload is not None:
            if note_content_digest(target_payload) != manifest.content_digest:
                raise ConcurrencyConflict("Archive target content digest drift")
            source_payload = self._read_optional(manifest.portable_ref, max_bytes=2 * 1024 * 1024)
            if source_payload is not None:
                if note_content_digest(source_payload) != manifest.content_digest:
                    raise ConcurrencyConflict("Archive duplicate source content digest drift")
                self._unlink(manifest.portable_ref)
            return target_ref
        payload = self._read_optional(manifest.portable_ref, max_bytes=2 * 1024 * 1024)
        if payload is None:
            target_payload = self._read_optional(target_ref, max_bytes=2 * 1024 * 1024)
            if target_payload is not None and note_content_digest(target_payload) == (
                manifest.content_digest
            ):
                return target_ref
            raise LayoutError("Archive source regular note olmali")
        if note_content_digest(payload) != manifest.content_digest:
            raise ConcurrencyConflict("Archive source content digest drift")
        try:
            self._atomic_write(target_ref, payload, replace_existing=False)
        except ConcurrencyConflict:
            target_payload = self._read_optional(target_ref, max_bytes=2 * 1024 * 1024)
            if target_payload is None or note_content_digest(target_payload) != (
                manifest.content_digest
            ):
                raise
        self._unlink(manifest.portable_ref)
        return target_ref

    def audit(
        self,
        *,
        notes: tuple[KnowledgeNoteRecord, ...],
        artifacts: tuple[ArtifactRefRecord, ...],
    ) -> tuple[KnowledgeFileIssue, ...]:
        """Report ambiguous, missing, corrupt or privacy-unsafe file-plane state."""

        issues: set[KnowledgeFileIssue] = set()
        expected_notes: set[str] = set()
        for note in notes:
            if not note.materialized:
                issues.add(KnowledgeFileIssue("pending-note-materialization", note.portable_ref))
            try:
                manifest = KnowledgeNoteManifest(
                    owner_scope=note.owner_scope,
                    note_kind=note.note_kind,
                    authorship=note.authorship,
                    classification=KnowledgeClassification(note.classification),
                    portable_ref=note.portable_ref,
                    content_digest=note.content_digest,
                    project_slug=note.project_slug,
                    state=note.state,
                )
                relative = (
                    validate_portable_relative(note.archived_ref, "Knowledge archive ref")
                    if note.state == "archived"
                    else manifest.portable_ref
                )
                if note.state == "archived" and not relative.startswith("archive/"):
                    raise PolicyViolation("Archived note archive root disinda")
            except (TypeError, ValueError, ValidationFailed, PolicyViolation):
                issues.add(KnowledgeFileIssue("invalid-note-manifest", note.portable_ref))
                continue
            if relative in expected_notes:
                issues.add(KnowledgeFileIssue("duplicate-note-ref", relative))
                continue
            expected_notes.add(relative)
            try:
                payload = self._read_optional(relative, max_bytes=2 * 1024 * 1024)
                if payload is None:
                    issues.add(KnowledgeFileIssue("missing-note", relative))
                    continue
                actual_digest = note_content_digest(payload)
            except (OSError, ValidationFailed, LayoutError):
                issues.add(KnowledgeFileIssue("unreadable-note", relative))
                continue
            if actual_digest != note.content_digest:
                issues.add(KnowledgeFileIssue("corrupt-note", relative))
            if manifest.classification is KnowledgeClassification.SECRET:
                issues.add(KnowledgeFileIssue("secret-in-normal-file-plane", relative))
            elif manifest.classification is KnowledgeClassification.PUBLIC:
                try:
                    assert_public_safe_projection(payload, relative_path=relative)
                except (PolicyViolation, ValidationFailed):
                    issues.add(KnowledgeFileIssue("public-projection-unsafe", relative))

        observed = 0
        for root_name in _AUDIT_NOTE_ROOTS:
            root = self.home / root_name
            if _is_link_or_reparse(root):
                issues.add(KnowledgeFileIssue("unsafe-note-root", root_name))
                continue
            if not root.is_dir():
                continue
            for directory, directory_names, file_names in os.walk(root, followlinks=False):
                parent = Path(directory)
                for name in tuple(directory_names):
                    target = parent / name
                    if _is_link_or_reparse(target):
                        relative = target.relative_to(self.home).as_posix()
                        issues.add(KnowledgeFileIssue("unsafe-note-path", relative))
                        directory_names.remove(name)
                for name in file_names:
                    if not name.lower().endswith(".md"):
                        continue
                    observed += 1
                    if observed > _AUDIT_LIMIT:
                        issues.add(KnowledgeFileIssue("audit-limit-exceeded", root_name))
                        break
                    target = parent / name
                    relative = target.relative_to(self.home).as_posix()
                    if _is_link_or_reparse(target):
                        issues.add(KnowledgeFileIssue("unsafe-note-path", relative))
                    elif relative not in expected_notes:
                        issues.add(KnowledgeFileIssue("unmanifested-note", relative))

        expected_artifacts: set[str] = set()
        for artifact in artifacts:
            hexadecimal = artifact.digest.removeprefix("sha256:")
            relative = f"artifacts/sha256/{hexadecimal[:2]}/{hexadecimal}"
            if relative in expected_artifacts:
                issues.add(KnowledgeFileIssue("duplicate-artifact-ref", relative))
                continue
            expected_artifacts.add(relative)
            if artifact.classification == KnowledgeClassification.SECRET.value:
                issues.add(KnowledgeFileIssue("secret-in-normal-cas", relative))
            try:
                payload = self._read_optional(relative, max_bytes=64 * 1024 * 1024)
                if payload is None:
                    issues.add(KnowledgeFileIssue("missing-cas-object", relative))
                    continue
            except (OSError, LayoutError):
                issues.add(KnowledgeFileIssue("unreadable-cas-object", relative))
                continue
            if (
                len(payload) != artifact.size_bytes
                or hashlib.sha256(payload).hexdigest() != hexadecimal
            ):
                issues.add(KnowledgeFileIssue("corrupt-cas-object", relative))
            if artifact.classification == KnowledgeClassification.PUBLIC.value:
                try:
                    assert_public_safe_projection(payload, relative_path=relative)
                except (PolicyViolation, ValidationFailed):
                    issues.add(KnowledgeFileIssue("public-cas-unsafe", relative))

        cas_root = self.home / "artifacts" / "sha256"
        if _is_link_or_reparse(cas_root):
            issues.add(KnowledgeFileIssue("unsafe-cas-root", "artifacts/sha256"))
        elif cas_root.is_dir():
            for target in cas_root.rglob("*"):
                relative = target.relative_to(self.home).as_posix()
                depth = len(target.relative_to(cas_root).parts)
                if _is_link_or_reparse(target) or (target.is_dir() and depth != 1):
                    issues.add(KnowledgeFileIssue("unsafe-cas-path", relative))
                elif target.is_file() and relative not in expected_artifacts:
                    issues.add(KnowledgeFileIssue("orphan-cas-object", relative))

        return tuple(sorted(issues))
