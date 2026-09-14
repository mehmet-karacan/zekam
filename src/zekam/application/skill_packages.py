"""Managed, digest-bound client projections for instruction-only skill packages."""

from __future__ import annotations

import json
import os
import shutil
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from zekam.domain.canonical import canonical_json, digest, digest_of_bytes, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.skill_package import SkillPackage


def _real_directory(path: Path) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return not bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _assert_contained(root: Path, target: Path) -> None:
    resolved_root = root.resolve(strict=True)
    try:
        target.resolve(strict=False).relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PolicyViolation("Skill projection target escapes bound project root") from exc
    current = target.parent
    while current != resolved_root:
        if current.exists() and not _real_directory(current):
            raise PolicyViolation("Skill projection parent symlink/junction rejected")
        current = current.parent


def _projected_artifact_digest(target: Path) -> str:
    manifest: list[dict[str, object]] = []
    for path in sorted(target.rglob("*")):
        info = path.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if path.is_symlink() or reparse:
            raise PolicyViolation("Managed skill projection symlink/junction rejected")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PolicyViolation("Managed skill projection special file rejected")
        relative = path.relative_to(target).as_posix()
        if relative == ".zekam-managed.json":
            continue
        payload = path.read_bytes()
        manifest.append(
            {"path": relative, "digest": digest_of_bytes(payload), "size": len(payload)}
        )
    return digest(sorted(manifest, key=lambda item: str(item["path"])))


@dataclass(frozen=True, slots=True)
class SkillProjectionPlan:
    project_root: Path
    package: SkillPackage
    targets: tuple[dict[str, object], ...]

    @property
    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-skill-client-projection-plan/v1",
            "source_root_identity_digest": digest(
                os.path.normcase(str(self.project_root.resolve(strict=True)))
            ),
            "package_digest": self.package.package_digest,
            "semantic_digest": self.package.semantic_digest,
            "skill_name": self.package.name,
            "targets": self.targets,
            "declared_allowed_tools_removed": self.package.declared_allowed_tools is not None,
            "provider_calls": 0,
            "network_calls": 0,
            "apply": False,
            "grants_authority": False,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.body)

    def as_dict(self) -> dict[str, object]:
        return self.body | {"plan_digest": self.plan_digest}


def build_projection_plan(project_root: Path, package: SkillPackage) -> SkillProjectionPlan:
    if not project_root.is_absolute() or not _real_directory(project_root):
        raise PolicyViolation("Skill projection exact real project root required")
    projections = package.safe_projection_files()
    artifact_digest = digest(
        [
            {"path": path, "digest": digest_of_bytes(payload), "size": len(payload)}
            for path, payload in sorted(projections.items())
        ]
    )
    targets: list[dict[str, object]] = []
    for relative, clients, support in (
        (
            Path(".agents") / "skills" / package.name,
            ("codex", "opencode"),
            "instruction-distribution-only",
        ),
        (
            Path(".claude") / "skills" / package.name,
            ("claude-code",),
            "instruction-distribution-only",
        ),
    ):
        target = project_root / relative
        _assert_contained(project_root, target)
        ownership = target / ".zekam-managed.json"
        state = "new"
        observed_digest: str | None = None
        observed_package_digest: str | None = None
        observed_semantic_digest: str | None = None
        if target.exists():
            if not _real_directory(target) or not ownership.is_file() or ownership.is_symlink():
                state = "unmanaged-conflict"
            else:
                try:
                    document = json.loads(ownership.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    state = "managed-metadata-invalid"
                else:
                    observed_digest = str(document.get("artifact_digest"))
                    observed_package_digest = str(document.get("package_digest"))
                    observed_semantic_digest = str(document.get("semantic_digest"))
                    if (
                        document.get("schema") != "zekam-managed-skill-projection/v1"
                        or document.get("relative_path") != relative.as_posix()
                        or tuple(document.get("clients", ())) != clients
                        or document.get("support_level") != support
                        or document.get("grants_authority") is not False
                    ):
                        state = "managed-metadata-invalid"
                    else:
                        try:
                            actual_digest = _projected_artifact_digest(target)
                        except (OSError, PolicyViolation):
                            state = "managed-drift"
                        else:
                            if observed_digest != actual_digest:
                                state = "managed-drift"
                            elif actual_digest == artifact_digest:
                                state = (
                                    "current"
                                    if observed_package_digest == package.package_digest
                                    and observed_semantic_digest == package.semantic_digest
                                    else "managed-metadata-invalid"
                                )
                            else:
                                try:
                                    parse_digest(observed_package_digest)
                                    parse_digest(observed_semantic_digest)
                                except (TypeError, ValidationFailed):
                                    state = "managed-metadata-invalid"
                                else:
                                    state = "managed-update"
        targets.append(
            {
                "relative_path": relative.as_posix(),
                "clients": clients,
                "support_level": support,
                "artifact_digest": artifact_digest,
                "observed_artifact_digest": observed_digest,
                "observed_package_digest": observed_package_digest,
                "observed_semantic_digest": observed_semantic_digest,
                "state": state,
                "reload": (
                    "automatic-or-restart-if-not-visible"
                    if "codex" in clients
                    else "live-watch-or-restart-if-directory-was-missing"
                ),
            }
        )
    return SkillProjectionPlan(project_root.resolve(), package, tuple(targets))


def apply_projection_plan(
    plan: SkillProjectionPlan, *, authorized_plan_digest: str
) -> dict[str, object]:
    if plan.plan_digest != authorized_plan_digest:
        raise PolicyViolation("Skill projection exact plan authorization required")
    fresh = build_projection_plan(plan.project_root, plan.package)
    if fresh.targets != plan.targets or fresh.plan_digest != plan.plan_digest:
        raise PolicyViolation("Skill projection target changed after authorization")
    if any(
        item["state"] not in {"new", "current", "managed-update"}
        for item in plan.targets
    ):
        raise PolicyViolation("Skill projection refuses unmanaged or drifted target")
    projected = plan.package.safe_projection_files()
    staged: list[tuple[Path, Path, Path | None, str, str | None, str]] = []
    committed: list[tuple[Path, Path | None, str]] = []
    transaction = uuid4().hex
    try:
        for target_info in plan.targets:
            target = plan.project_root / str(target_info["relative_path"])
            _assert_contained(plan.project_root, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not _real_directory(target.parent):
                raise PolicyViolation("Skill projection parent identity invalid")
            stage = target.with_name(f".{target.name}.zekam-stage-{transaction}")
            backup = (
                None
                if target_info["state"] == "new"
                else target.with_name(f".{target.name}.zekam-backup-{transaction}")
            )
            for private_path in (stage, backup):
                if private_path is not None and (
                    private_path.exists() or private_path.is_symlink()
                ):
                    raise PolicyViolation("Skill projection private path collision")
            stage.mkdir()
            if not _real_directory(stage):
                raise PolicyViolation("Skill projection staging identity invalid")
            observed_artifact = target_info["observed_artifact_digest"]
            staged.append(
                (
                    target,
                    stage,
                    backup,
                    str(target_info["state"]),
                    None if observed_artifact is None else str(observed_artifact),
                    str(target_info["artifact_digest"]),
                )
            )
            for relative, payload in sorted(projected.items()):
                destination = stage / relative
                _assert_contained(plan.project_root, destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
                if digest_of_bytes(destination.read_bytes()) != digest_of_bytes(payload):
                    raise PolicyViolation("Skill projection staged file readback drift")
            ownership_body: dict[str, Any] = {
                "schema": "zekam-managed-skill-projection/v1",
                "package_digest": plan.package.package_digest,
                "semantic_digest": plan.package.semantic_digest,
                "artifact_digest": target_info["artifact_digest"],
                "relative_path": target_info["relative_path"],
                "clients": target_info["clients"],
                "support_level": target_info["support_level"],
                "grants_authority": False,
            }
            ownership = stage / ".zekam-managed.json"
            ownership.write_text(
                canonical_json(ownership_body), encoding="utf-8", newline="\n"
            )
            expected_artifact = str(target_info["artifact_digest"])
            if _projected_artifact_digest(stage) != expected_artifact:
                raise PolicyViolation("Skill projection staged artifact digest drift")

        # Recheck all live targets after staging and immediately before the first swap.
        current = build_projection_plan(plan.project_root, plan.package)
        if current.targets != plan.targets or current.plan_digest != plan.plan_digest:
            raise PolicyViolation("Skill projection target changed during staging")

        for target, stage, backup, state, observed_artifact, expected_artifact in staged:
            if state == "current":
                shutil.rmtree(stage)
                continue
            if backup is not None:
                # Record rollback intent before the first destructive rename. A
                # concurrent user edit can fail readback after the rename and must
                # still be restored to its original target path.
                committed.append((target, backup, expected_artifact))
                os.replace(target, backup)
                if (
                    observed_artifact is None
                    or _projected_artifact_digest(backup) != observed_artifact
                ):
                    raise PolicyViolation("Skill projection target changed during atomic swap")
                os.replace(stage, target)
            else:
                os.replace(stage, target)
                committed.append((target, backup, expected_artifact))
        for _target, backup, _committed_digest in committed:
            if backup is not None:
                with suppress(OSError):
                    shutil.rmtree(backup)
    except BaseException as error:
        rollback_failed = False
        for target, backup, committed_digest in reversed(committed):
            try:
                if backup is None:
                    if target.exists() and _real_directory(target):
                        if _projected_artifact_digest(target) != committed_digest:
                            raise OSError("new projection changed before rollback")
                        shutil.rmtree(target)
                elif backup.exists():
                    if target.exists() and _real_directory(target):
                        if _projected_artifact_digest(target) != committed_digest:
                            raise OSError("managed projection changed before rollback")
                        shutil.rmtree(target)
                    os.replace(backup, target)
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise PolicyViolation(
                "Skill projection rollback incomplete; recoverable backup retained"
            ) from error
        raise
    finally:
        for _target, stage, _backup, _state, _observed, _expected in staged:
            if stage.exists() and _real_directory(stage):
                shutil.rmtree(stage)
    return projection_receipt(plan, authorized_plan_digest=authorized_plan_digest)


def projection_receipt(
    plan: SkillProjectionPlan, *, authorized_plan_digest: str
) -> dict[str, object]:
    """Reconstruct the deterministic receipt after exact current readback."""

    if plan.plan_digest != authorized_plan_digest:
        raise PolicyViolation("Skill projection exact receipt authorization required")
    fresh = build_projection_plan(plan.project_root, plan.package)
    if not fresh.targets or any(item["state"] != "current" for item in fresh.targets):
        raise PolicyViolation("Skill projection receipt requires current artifact readback")
    receipts: list[dict[str, object]] = []
    for target_info in plan.targets:
        ownership_body: dict[str, Any] = {
            "schema": "zekam-managed-skill-projection/v1",
            "package_digest": plan.package.package_digest,
            "semantic_digest": plan.package.semantic_digest,
            "artifact_digest": target_info["artifact_digest"],
            "relative_path": target_info["relative_path"],
            "clients": target_info["clients"],
            "support_level": target_info["support_level"],
            "grants_authority": False,
        }
        receipts.append(
            ownership_body
            | {
                "receipt_digest": digest(ownership_body),
                "state": "written-and-read-back",
            }
        )
    body: dict[str, object] = {
        "schema": "zekam-skill-client-projection-receipt/v1",
        "plan_digest": authorized_plan_digest,
        "package_digest": plan.package.package_digest,
        "targets": receipts,
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    return body | {"receipt_digest": digest(body)}
