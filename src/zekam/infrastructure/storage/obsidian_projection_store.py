"""Secure immutable-generation store for Obsidian projections."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes, parse_digest
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed
from zekam.domain.markdown_projection import (
    OBSIDIAN_RENDERER_PROFILE,
    ObsidianProfile,
    ObsidianProjectionBundle,
)

_SAFE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_REPARSE_POINT = 0x400
_EXCLUSION_REASONS = {
    "classification-prohibited",
    "classification-excluded",
    "record-oversized",
    "secret-pattern",
    "pii-email",
    "absolute-path",
    "connection-string",
    "raw-content-marker",
}


def _unsafe(path: Path) -> bool:
    try:
        stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        getattr(stat, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _safe_segment(value: str, field: str) -> None:
    if not _SAFE_SEGMENT.fullmatch(value):
        raise ValidationFailed(f"{field} guvenli logical segment olmali")


def _safe_relative(value: str) -> None:
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or ".." in posix.parts
    ):
        raise PolicyViolation("Obsidian manifest path portable olmali")


def _regular(path: Path) -> None:
    if _unsafe(path) or not path.is_file():
        raise PolicyViolation("Obsidian projection regular, symlink olmayan file ister")


@dataclass(frozen=True, slots=True)
class StagedObsidianProjection:
    bundle: ObsidianProjectionBundle
    staging_root: Path
    generation: str


@dataclass(frozen=True, slots=True)
class PublishedObsidianProjection:
    project_id: UUID
    store_identity_digest: str
    generation: str
    projection_digest: str
    manifest_digest: str
    receipt_digest: str
    current_ref: str


@dataclass(frozen=True, slots=True)
class LocalObsidianProjectionStore:
    root: Path

    @property
    def identity_digest(self) -> str:
        normalized = str(self.root.resolve(strict=False)).replace("\\", "/")
        if os.name == "nt":
            normalized = normalized.casefold()
        return digest({"kind": "local-obsidian-store", "root": normalized})

    def _profile_root(
        self,
        realm_slug: str,
        project_id: UUID,
        profile: ObsidianProfile,
        *,
        create: bool,
    ) -> Path:
        _safe_segment(realm_slug, "Obsidian realm")
        if not isinstance(project_id, UUID):
            raise ValidationFailed("Obsidian store exact project UUID ister")
        project_segment = str(project_id)
        _safe_segment(project_segment, "Obsidian project")
        _safe_segment(profile.value, "Obsidian profile")
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        elif not self.root.exists():
            raise NotFound("Obsidian store root bulunamadi")
        if _unsafe(self.root) or not self.root.is_dir():
            raise PolicyViolation("Obsidian store root guvenli directory olmali")
        current = self.root
        for segment in (realm_slug, project_segment, profile.value):
            candidate = current / segment
            if create and not candidate.exists():
                candidate.mkdir()
            elif not candidate.exists():
                raise NotFound("Obsidian realm/project/profile bulunamadi")
            current = candidate
            if _unsafe(current) or not current.is_dir():
                raise PolicyViolation(
                    "Obsidian realm/project/profile symlink veya reparse olamaz"
                )
        return current

    @staticmethod
    def _write_file(root: Path, relative_path: str, payload: bytes) -> None:
        _safe_relative(relative_path)
        target = root.joinpath(*PurePosixPath(relative_path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if _unsafe(target.parent):
            raise PolicyViolation("Obsidian staging parent symlink olamaz")
        with target.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    def stage(self, bundle: ObsidianProjectionBundle) -> StagedObsidianProjection:
        bundle.__post_init__()
        profile_root = self._profile_root(
            bundle.realm_slug, bundle.project_id, bundle.profile, create=True
        )
        generation = parse_digest(bundle.projection_digest)
        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=profile_root))
        try:
            for item in bundle.files:
                self._write_file(staging, item.relative_path, item.payload)
            self._write_file(staging, "_META/manifest.json", bundle.manifest_bytes())
            self._write_file(
                staging,
                "_META/projection-receipt.json",
                bundle.receipt_bytes(),
            )
            self._verify_generation(
                staging,
                expected_realm_slug=bundle.realm_slug,
                expected_project_id=bundle.project_id,
                expected_profile=bundle.profile,
                expected_projection_digest=bundle.projection_digest,
                expected_manifest_digest=bundle.manifest_digest,
                expected_receipt_digest=bundle.receipt_digest,
            )
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return StagedObsidianProjection(bundle, staging, generation)

    def publish(self, staged: StagedObsidianProjection) -> PublishedObsidianProjection:
        staged.bundle.__post_init__()
        expected_generation = parse_digest(staged.bundle.projection_digest)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", staged.generation)
            or staged.generation != expected_generation
        ):
            raise PolicyViolation("Obsidian staged generation exact projection digest olmali")
        profile_root = self._profile_root(
            staged.bundle.realm_slug,
            staged.bundle.project_id,
            staged.bundle.profile,
            create=True,
        )
        if (
            staged.staging_root.parent != profile_root
            or _unsafe(staged.staging_root)
            or not staged.staging_root.is_dir()
        ):
            raise PolicyViolation("Obsidian staging exact profile rootuna bagli olmali")
        self._verify_generation(
            staged.staging_root,
            expected_realm_slug=staged.bundle.realm_slug,
            expected_project_id=staged.bundle.project_id,
            expected_profile=staged.bundle.profile,
            expected_projection_digest=staged.bundle.projection_digest,
            expected_manifest_digest=staged.bundle.manifest_digest,
            expected_receipt_digest=staged.bundle.receipt_digest,
        )
        generations = profile_root / "generations"
        generations.mkdir(exist_ok=True)
        if _unsafe(generations):
            raise PolicyViolation("Obsidian generations symlink olamaz")
        destination = generations / staged.generation
        if destination.exists():
            try:
                verified = self._verify_generation(
                    destination,
                    expected_realm_slug=staged.bundle.realm_slug,
                    expected_project_id=staged.bundle.project_id,
                    expected_profile=staged.bundle.profile,
                    expected_projection_digest=staged.bundle.projection_digest,
                    expected_manifest_digest=staged.bundle.manifest_digest,
                    expected_receipt_digest=staged.bundle.receipt_digest,
                )
            except Exception:
                shutil.rmtree(staged.staging_root, ignore_errors=True)
                raise
            if verified["manifest_digest"] != staged.bundle.manifest_digest:
                shutil.rmtree(staged.staging_root, ignore_errors=True)
                raise PolicyViolation("Obsidian immutable generation digest drift")
            shutil.rmtree(staged.staging_root)
        else:
            try:
                staged.staging_root.replace(destination)
            except Exception:
                shutil.rmtree(staged.staging_root, ignore_errors=True)
                raise
        self._verify_generation(
            destination,
            expected_realm_slug=staged.bundle.realm_slug,
            expected_project_id=staged.bundle.project_id,
            expected_profile=staged.bundle.profile,
            expected_projection_digest=staged.bundle.projection_digest,
            expected_manifest_digest=staged.bundle.manifest_digest,
            expected_receipt_digest=staged.bundle.receipt_digest,
        )
        pointer = {
            "schema": "zekam-obsidian-current/v2",
            "realm": staged.bundle.realm_slug,
            "project_id": str(staged.bundle.project_id),
            "profile": staged.bundle.profile.value,
            "store_identity_digest": self.identity_digest,
            "generation": staged.generation,
            "projection_digest": staged.bundle.projection_digest,
            "manifest_digest": staged.bundle.manifest_digest,
            "receipt_digest": staged.bundle.receipt_digest,
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".CURRENT-", suffix=".json", dir=profile_root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_bytes(pointer))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(profile_root / "CURRENT.json")
        finally:
            temporary.unlink(missing_ok=True)
        return PublishedObsidianProjection(
            project_id=staged.bundle.project_id,
            store_identity_digest=self.identity_digest,
            generation=staged.generation,
            projection_digest=staged.bundle.projection_digest,
            manifest_digest=staged.bundle.manifest_digest,
            receipt_digest=staged.bundle.receipt_digest,
            current_ref=(
                f"obsidian:{parse_digest(self.identity_digest)}:"
                f"{staged.bundle.realm_slug}:{staged.bundle.project_id}:"
                f"{staged.bundle.profile.value}:"
                f"{staged.generation}"
            ),
        )

    def verify_current(
        self,
        realm_slug: str,
        project_id: UUID,
        profile: ObsidianProfile,
        *,
        expected_projection_digest: str,
        expected_manifest_digest: str,
        expected_receipt_digest: str,
    ) -> dict[str, Any]:
        parse_digest(expected_projection_digest)
        parse_digest(expected_manifest_digest)
        parse_digest(expected_receipt_digest)
        profile_root = self._profile_root(
            realm_slug, project_id, profile, create=False
        )
        pointer_path = profile_root / "CURRENT.json"
        if not pointer_path.exists():
            raise NotFound("Obsidian CURRENT pointer bulunamadi")
        _regular(pointer_path)
        if pointer_path.stat().st_size > 16 * 1024:
            raise PolicyViolation("Obsidian CURRENT pointer bounded disi")
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationFailed("Obsidian CURRENT pointer okunamadi") from exc
        expected_keys = {
            "schema",
            "realm",
            "project_id",
            "profile",
            "store_identity_digest",
            "generation",
            "projection_digest",
            "manifest_digest",
            "receipt_digest",
        }
        if not isinstance(pointer, dict) or set(pointer) != expected_keys:
            raise ValidationFailed("Obsidian CURRENT pointer exact schema ister")
        if pointer["schema"] != "zekam-obsidian-current/v2":
            raise ValidationFailed("Obsidian CURRENT schema gecersiz")
        if pointer["realm"] != realm_slug or pointer["profile"] != profile.value:
            raise PolicyViolation("Obsidian CURRENT realm/profile binding drift")
        if pointer["project_id"] != str(project_id):
            raise PolicyViolation("Obsidian CURRENT project binding drift")
        if pointer["store_identity_digest"] != self.identity_digest:
            raise PolicyViolation("Obsidian CURRENT store binding drift")
        generation = str(pointer["generation"])
        if not re.fullmatch(r"[0-9a-f]{64}", generation):
            raise ValidationFailed("Obsidian CURRENT generation digest olmali")
        projection_digest = str(pointer["projection_digest"])
        if generation != parse_digest(projection_digest):
            raise PolicyViolation("Obsidian CURRENT generation/projection digest drift")
        parse_digest(str(pointer["manifest_digest"]))
        parse_digest(str(pointer["receipt_digest"]))
        if projection_digest != expected_projection_digest:
            raise PolicyViolation("Obsidian CURRENT stale projection gosteriyor")
        generation_root = profile_root / "generations" / generation
        result = self._verify_generation(
            generation_root,
            expected_realm_slug=realm_slug,
            expected_project_id=project_id,
            expected_profile=profile,
            expected_projection_digest=projection_digest,
            expected_manifest_digest=expected_manifest_digest,
            expected_receipt_digest=expected_receipt_digest,
        )
        if (
            result["manifest_digest"] != str(pointer["manifest_digest"])
            or result["receipt_digest"] != str(pointer["receipt_digest"])
        ):
            raise PolicyViolation("Obsidian CURRENT manifest/receipt binding drift")
        return {
            "schema": "zekam-obsidian-verification/v1",
            "realm": realm_slug,
            "project_id": str(project_id),
            "profile": profile.value,
            "store_identity_digest": self.identity_digest,
            "current_ref": (
                f"obsidian:{parse_digest(self.identity_digest)}:{realm_slug}:"
                f"{project_id}:{profile.value}:{generation}"
            ),
            "generation": generation,
            "projection_digest": projection_digest,
            "manifest_digest": result["manifest_digest"],
            "receipt_digest": result["receipt_digest"],
            "file_count": result["file_count"],
            "status": "passed",
            "grants_authority": False,
        }

    @staticmethod
    def _verify_generation(
        root: Path,
        *,
        expected_realm_slug: str,
        expected_project_id: UUID,
        expected_profile: ObsidianProfile,
        expected_projection_digest: str,
        expected_manifest_digest: str,
        expected_receipt_digest: str,
    ) -> dict[str, Any]:
        if _unsafe(root) or not root.is_dir():
            raise PolicyViolation("Obsidian generation guvenli directory olmali")
        manifest_path = root / "_META" / "manifest.json"
        receipt_path = root / "_META" / "projection-receipt.json"
        _regular(manifest_path)
        _regular(receipt_path)
        if manifest_path.stat().st_size > 8 * 1024 * 1024:
            raise PolicyViolation("Obsidian manifest bounded disi")
        if receipt_path.stat().st_size > 1024 * 1024:
            raise PolicyViolation("Obsidian receipt bounded disi")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationFailed("Obsidian manifest/receipt okunamadi") from exc
        if not isinstance(manifest, dict) or not isinstance(receipt, dict):
            raise ValidationFailed("Obsidian manifest/receipt object olmali")
        manifest_digest = str(manifest.pop("manifest_digest", ""))
        receipt_digest = str(receipt.pop("receipt_digest", ""))
        parse_digest(manifest_digest)
        parse_digest(receipt_digest)
        manifest_keys = {
            "schema",
            "realm",
            "project_id",
            "profile",
            "source_snapshot_digest",
            "policy_digest",
            "projection_digest",
            "renderer_profile",
            "generated_at",
            "files",
            "privacy_scan_digest",
            "link_check_digest",
            "exclusions",
            "grants_authority",
        }
        receipt_keys = {
            "schema",
            "realm",
            "project_id",
            "profile",
            "source_snapshot_digest",
            "manifest_digest",
            "file_count",
            "privacy_scan_digest",
            "link_check_digest",
            "projection_digest",
            "status",
            "generated_at",
            "grants_authority",
        }
        if set(manifest) != manifest_keys or set(receipt) != receipt_keys:
            raise ValidationFailed("Obsidian manifest/receipt exact schema ister")
        if digest(manifest) != manifest_digest or digest(receipt) != receipt_digest:
            raise PolicyViolation("Obsidian manifest/receipt digest drift")
        if (
            manifest_digest != expected_manifest_digest
            or receipt_digest != expected_receipt_digest
        ):
            raise PolicyViolation("Obsidian live manifest/receipt binding drift")
        for key in (
            "source_snapshot_digest",
            "policy_digest",
            "projection_digest",
            "privacy_scan_digest",
            "link_check_digest",
        ):
            parse_digest(str(manifest[key]))
        expected_projection = digest(
            {
                "schema": "zekam-obsidian-projection/v1",
                "realm_slug": manifest["realm"],
                "project_id": manifest["project_id"],
                "profile": manifest["profile"],
                "source_snapshot_digest": manifest["source_snapshot_digest"],
                "policy_digest": manifest["policy_digest"],
                "renderer_profile": OBSIDIAN_RENDERER_PROFILE,
            }
        )
        if (
            manifest["schema"] != "zekam-obsidian-manifest/v1"
            or manifest["realm"] != expected_realm_slug
            or manifest["project_id"] != str(expected_project_id)
            or manifest["profile"] != expected_profile.value
            or manifest["renderer_profile"] != OBSIDIAN_RENDERER_PROFILE
            or manifest["grants_authority"] is not False
            or expected_projection != expected_projection_digest
            or receipt["schema"] != "zekam-obsidian-projection-receipt/v1"
            or receipt["realm"] != manifest["realm"]
            or receipt["project_id"] != manifest["project_id"]
            or receipt["profile"] != manifest["profile"]
            or receipt["source_snapshot_digest"] != manifest["source_snapshot_digest"]
            or receipt["privacy_scan_digest"] != manifest["privacy_scan_digest"]
            or receipt["link_check_digest"] != manifest["link_check_digest"]
            or receipt["generated_at"] != manifest["generated_at"]
            or str(manifest.get("projection_digest")) != expected_projection_digest
            or str(receipt.get("projection_digest")) != expected_projection_digest
            or str(receipt.get("manifest_digest")) != manifest_digest
            or receipt.get("status") != "completed"
            or receipt.get("grants_authority") is not False
        ):
            raise PolicyViolation("Obsidian projection receipt provenance drift")
        files = manifest.get("files")
        if not isinstance(files, list) or len(files) > 4096:
            raise ValidationFailed("Obsidian manifest bounded files array ister")
        exclusions = manifest.get("exclusions")
        if not isinstance(exclusions, list) or len(exclusions) > 1000:
            raise ValidationFailed("Obsidian manifest bounded exclusions array ister")
        for exclusion in exclusions:
            if (
                not isinstance(exclusion, dict)
                or set(exclusion) != {"record_digest", "reason_code"}
                or exclusion["reason_code"] not in _EXCLUSION_REASONS
            ):
                raise ValidationFailed("Obsidian manifest exclusion exact schema ister")
            parse_digest(str(exclusion["record_digest"]))
        if receipt["file_count"] != len(files):
            raise PolicyViolation("Obsidian projection receipt file count drift")
        actual_paths: set[str] = set()
        for candidate in root.rglob("*"):
            if _unsafe(candidate):
                raise PolicyViolation("Obsidian generation symlink/reparse tasiyamaz")
            if candidate.is_file():
                actual_paths.add(candidate.relative_to(root).as_posix())
        total_payload_size = 0
        for row in files:
            if not isinstance(row, dict) or set(row) != {
                "relative_path",
                "content_digest",
                "media_type",
            }:
                raise ValidationFailed("Obsidian manifest file exact schema ister")
            relative = str(row["relative_path"])
            _safe_relative(relative)
            parse_digest(str(row["content_digest"]))
            if row["media_type"] not in {
                "text/markdown; charset=utf-8",
                "application/json",
                "text/plain; charset=utf-8",
            }:
                raise ValidationFailed("Obsidian manifest media type allowlist disinda")
            target = root.joinpath(*PurePosixPath(relative).parts)
            _regular(target)
            if target.stat().st_size > 1024 * 1024:
                raise PolicyViolation("Obsidian projection file bounded disi")
            total_payload_size += target.stat().st_size
            if digest_of_bytes(target.read_bytes()) != str(row["content_digest"]):
                raise PolicyViolation("Obsidian projection file digest drift")
        if total_payload_size > 64 * 1024 * 1024:
            raise PolicyViolation("Obsidian generation toplam payload bounded disi")
        expected_paths = {
            str(row["relative_path"]) for row in files if isinstance(row, dict)
        } | {"_META/manifest.json", "_META/projection-receipt.json"}
        if len(expected_paths) != len(files) + 2 or actual_paths != expected_paths:
            raise PolicyViolation("Obsidian generation unmanifested/missing file tasiyor")
        return {
            "manifest_digest": manifest_digest,
            "receipt_digest": receipt_digest,
            "file_count": len(files),
        }
