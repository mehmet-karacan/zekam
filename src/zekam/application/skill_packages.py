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
from zekam.domain.client_integration import ClientIntegrationId, ClientIntegrationPolicy
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.skill_package import SkillPackage, SkillPackageMetadata


def _real_directory(path: Path) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return not bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def discover_skill_metadata(root: Path) -> SkillPackageMetadata:
    """Metadata-only discovery scan.

    Reuses the full canonical validation/digest pipeline so package identity and
    secret scanning stay exact, but returns only the content-free metadata view.
    Instruction/reference/script/asset content is not exposed; callers that need
    an instruction payload must request an explicit load on the full package.
    """
    if not root.is_absolute() or not _real_directory(root):
        raise PolicyViolation("Skill discovery exact real absolute directory required")
    package = SkillPackage.read_directory(root)
    return package.metadata_view


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


def projected_artifact_digest(target: Path) -> str:
    """Public bounded ownership readback used by integration reconciliation."""

    return _projected_artifact_digest(target)


@dataclass(frozen=True, slots=True)
class SkillProjectionPlan:
    project_root: Path
    package: SkillPackage
    policy: ClientIntegrationPolicy
    quarantine_root: Path | None
    targets: tuple[dict[str, object], ...]

    @property
    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-skill-client-projection-plan/v2",
            "source_root_identity_digest": digest(
                os.path.normcase(str(self.project_root.resolve(strict=True)))
            ),
            "quarantine_root_identity_digest": (
                None
                if self.quarantine_root is None
                else digest(os.path.normcase(str(self.quarantine_root.resolve(strict=False))))
            ),
            "integration_policy": self.policy.body(),
            "integration_policy_digest": self.policy.policy_digest,
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


def _target_specs(package: SkillPackage) -> tuple[tuple[Path, tuple[str, ...], str], ...]:
    return (
        (
            Path(".opencode") / "skills" / package.name,
            (ClientIntegrationId.OPENCODE.value,),
            ClientIntegrationId.OPENCODE.value,
        ),
        (
            Path(".agents") / "skills" / package.name,
            (ClientIntegrationId.CODEX.value,),
            ClientIntegrationId.CODEX.value,
        ),
        (
            Path(".claude") / "skills" / package.name,
            (ClientIntegrationId.CLAUDE_CODE.value,),
            ClientIntegrationId.CLAUDE_CODE.value,
        ),
    )


def _quarantine_relative(client: str, package: SkillPackage, artifact_digest: str) -> str:
    return (
        Path(client)
        / package.name
        / artifact_digest.removeprefix("sha256:")
    ).as_posix()


def _assert_safe_quarantine(project_root: Path, quarantine_root: Path) -> None:
    if not quarantine_root.is_absolute():
        raise PolicyViolation("Skill projection quarantine exact absolute root required")
    resolved = quarantine_root.resolve(strict=False)
    if resolved.exists() and not _real_directory(resolved):
        raise PolicyViolation("Skill projection quarantine symlink/junction rejected")
    for name in (".opencode", ".agents", ".claude"):
        discovery = (project_root / name).resolve(strict=False)
        if (
            resolved == discovery
            or resolved.is_relative_to(discovery)
            or discovery.is_relative_to(resolved)
        ):
            raise PolicyViolation("Skill projection quarantine discovery tree disinda olmali")


def build_projection_plan(
    project_root: Path,
    package: SkillPackage,
    *,
    policy: ClientIntegrationPolicy | None = None,
    quarantine_root: Path | None = None,
) -> SkillProjectionPlan:
    if not project_root.is_absolute() or not _real_directory(project_root):
        raise PolicyViolation("Skill projection exact real project root required")
    policy = ClientIntegrationPolicy() if policy is None else policy
    if quarantine_root is not None:
        _assert_safe_quarantine(project_root, quarantine_root)
    projections = package.safe_projection_files()
    artifact_digest = digest(
        [
            {"path": path, "digest": digest_of_bytes(payload), "size": len(payload)}
            for path, payload in sorted(projections.items())
        ]
    )
    targets: list[dict[str, object]] = []
    for relative, clients, client in _target_specs(package):
        support = "instruction-distribution-only"
        enabled = policy.enabled(client)
        target = project_root / relative
        _assert_contained(project_root, target)
        ownership = target / ".zekam-managed.json"
        state = "new" if enabled else "absent"
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
                    schema = document.get("schema")
                    observed_clients = tuple(document.get("clients", ()))
                    accepted_clients = {clients}
                    if client == ClientIntegrationId.CODEX.value:
                        accepted_clients.add(("codex", "opencode"))
                    if (
                        schema not in {
                            "zekam-managed-skill-projection/v1",
                            "zekam-managed-skill-projection/v2",
                        }
                        or document.get("relative_path") != relative.as_posix()
                        or observed_clients not in accepted_clients
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
                            elif not enabled:
                                state = (
                                    "managed-disable"
                                    if quarantine_root is not None
                                    else "cleanup-blocked-no-quarantine"
                                )
                            elif actual_digest == artifact_digest:
                                state = (
                                    "current"
                                    if observed_package_digest == package.package_digest
                                    and observed_semantic_digest == package.semantic_digest
                                    and schema == "zekam-managed-skill-projection/v2"
                                    and observed_clients == clients
                                    else "managed-metadata-invalid"
                                )
                                if state == "managed-metadata-invalid" and (
                                    observed_package_digest == package.package_digest
                                    and observed_semantic_digest == package.semantic_digest
                                ):
                                    state = "managed-update"
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
                "client": client,
                "enabled": enabled,
                "support_level": support,
                "artifact_digest": artifact_digest,
                "observed_artifact_digest": observed_digest,
                "observed_package_digest": observed_package_digest,
                "observed_semantic_digest": observed_semantic_digest,
                "state": state,
                "quarantine_relative_path": (
                    None
                    if observed_digest is None
                    else _quarantine_relative(client, package, observed_digest)
                ),
                "reload": (
                    "live-watch-or-restart-if-directory-was-missing"
                    if client == ClientIntegrationId.OPENCODE.value
                    else "automatic-or-restart-if-not-visible"
                ),
            }
        )
    return SkillProjectionPlan(
        project_root.resolve(),
        package,
        policy,
        None if quarantine_root is None else quarantine_root.resolve(strict=False),
        tuple(targets),
    )


def _ownership_body(
    plan: SkillProjectionPlan, target_info: dict[str, object]
) -> dict[str, Any]:
    clients = target_info["clients"]
    if not isinstance(clients, (list, tuple)):
        raise PolicyViolation("Skill projection client list invalid")
    return {
        "schema": "zekam-managed-skill-projection/v2",
        "package_digest": plan.package.package_digest,
        "semantic_digest": plan.package.semantic_digest,
        "artifact_digest": target_info["artifact_digest"],
        "relative_path": target_info["relative_path"],
        "clients": [str(client) for client in clients],
        "support_level": target_info["support_level"],
        "integration_policy_digest": plan.policy.policy_digest,
        "grants_authority": False,
    }


def _quarantine_path(plan: SkillProjectionPlan, target_info: dict[str, object]) -> Path:
    if plan.quarantine_root is None:
        raise PolicyViolation("Skill projection cleanup requires quarantine root")
    relative = target_info.get("quarantine_relative_path")
    if not isinstance(relative, str):
        raise PolicyViolation("Skill projection quarantine identity missing")
    target = plan.quarantine_root / Path(relative)
    root = plan.quarantine_root.resolve(strict=False)
    try:
        target.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise PolicyViolation("Skill projection quarantine path escapes root") from exc
    return target


def _terminal_projection_matches(plan: SkillProjectionPlan) -> bool:
    for target_info in plan.targets:
        target = plan.project_root / str(target_info["relative_path"])
        if bool(target_info["enabled"]):
            ownership = target / ".zekam-managed.json"
            if not _real_directory(target) or not ownership.is_file() or ownership.is_symlink():
                return False
            try:
                document = json.loads(ownership.read_text(encoding="utf-8"))
                actual = _projected_artifact_digest(target)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, PolicyViolation):
                return False
            if document != _ownership_body(plan, target_info):
                return False
            if actual != target_info["artifact_digest"]:
                return False
            continue
        if target.exists() or target.is_symlink():
            return False
        if target_info["state"] == "managed-disable":
            try:
                quarantine = _quarantine_path(plan, target_info)
                actual = _projected_artifact_digest(quarantine)
            except (OSError, PolicyViolation):
                return False
            if actual != target_info["observed_artifact_digest"]:
                return False
    return True


def apply_projection_plan(
    plan: SkillProjectionPlan, *, authorized_plan_digest: str
) -> dict[str, object]:
    if plan.plan_digest != authorized_plan_digest:
        raise PolicyViolation("Skill projection exact plan authorization required")
    if _terminal_projection_matches(plan):
        return projection_receipt(plan, authorized_plan_digest=authorized_plan_digest)
    fresh = build_projection_plan(
        plan.project_root,
        plan.package,
        policy=plan.policy,
        quarantine_root=plan.quarantine_root,
    )
    if fresh.targets != plan.targets or fresh.plan_digest != plan.plan_digest:
        raise PolicyViolation("Skill projection target changed after authorization")
    if any(
        item["state"]
        not in {"new", "current", "managed-update", "absent", "managed-disable"}
        for item in plan.targets
    ):
        raise PolicyViolation("Skill projection refuses unmanaged or drifted target")
    if any(item["state"] == "managed-disable" for item in plan.targets):
        if plan.quarantine_root is None:
            raise PolicyViolation("Skill projection cleanup requires quarantine root")
        _assert_safe_quarantine(plan.project_root, plan.quarantine_root)
    projected = plan.package.safe_projection_files()
    staged: list[tuple[Path, Path, Path | None, str, str | None, str]] = []
    committed: list[tuple[Path, Path | None, str]] = []
    quarantined: list[tuple[Path, Path, str]] = []
    transaction = uuid4().hex
    try:
        for target_info in plan.targets:
            if not bool(target_info["enabled"]):
                continue
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
            ownership_body = _ownership_body(plan, target_info)
            ownership = stage / ".zekam-managed.json"
            ownership.write_text(
                canonical_json(ownership_body), encoding="utf-8", newline="\n"
            )
            expected_artifact = str(target_info["artifact_digest"])
            if _projected_artifact_digest(stage) != expected_artifact:
                raise PolicyViolation("Skill projection staged artifact digest drift")

        # Recheck all live targets after staging and immediately before the first swap.
        current = build_projection_plan(
            plan.project_root,
            plan.package,
            policy=plan.policy,
            quarantine_root=plan.quarantine_root,
        )
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

        for target_info in plan.targets:
            if target_info["state"] != "managed-disable":
                continue
            target = plan.project_root / str(target_info["relative_path"])
            quarantine = _quarantine_path(plan, target_info)
            if quarantine.exists() or quarantine.is_symlink():
                raise PolicyViolation("Skill projection quarantine collision")
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            if not _real_directory(quarantine.parent):
                raise PolicyViolation("Skill projection quarantine parent identity invalid")
            observed = target_info["observed_artifact_digest"]
            if not isinstance(observed, str) or _projected_artifact_digest(target) != observed:
                raise PolicyViolation("Skill projection cleanup target changed before quarantine")
            os.replace(target, quarantine)
            quarantined.append((target, quarantine, observed))
            if _projected_artifact_digest(quarantine) != observed:
                raise PolicyViolation("Skill projection quarantine readback drift")

        if not _terminal_projection_matches(plan):
            raise PolicyViolation("Skill projection terminal readback failed")
        for _target, backup, _committed_digest in committed:
            if backup is not None:
                with suppress(OSError):
                    shutil.rmtree(backup)
    except BaseException as error:
        rollback_failed = False
        for target, quarantine, observed in reversed(quarantined):
            try:
                if target.exists() or target.is_symlink():
                    raise OSError("disabled target recreated before rollback")
                if _projected_artifact_digest(quarantine) != observed:
                    raise OSError("quarantine changed before rollback")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(quarantine, target)
            except OSError:
                rollback_failed = True
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
    if not plan.targets or not _terminal_projection_matches(plan):
        raise PolicyViolation("Skill projection receipt requires current artifact readback")
    receipts: list[dict[str, object]] = []
    for target_info in plan.targets:
        if bool(target_info["enabled"]):
            ownership_body = _ownership_body(plan, target_info)
            receipts.append(
                ownership_body
                | {
                    "receipt_digest": digest(ownership_body),
                    "state": "written-and-read-back",
                }
            )
        else:
            cleanup_body: dict[str, object] = {
                "schema": "zekam-managed-skill-deprojection-receipt/v1",
                "relative_path": target_info["relative_path"],
                "client": target_info["client"],
                "state": (
                    "quarantined-and-read-back"
                    if target_info["state"] == "managed-disable"
                    else "absent-and-read-back"
                ),
                "observed_artifact_digest": target_info["observed_artifact_digest"],
                "quarantine_relative_path": target_info["quarantine_relative_path"],
                "integration_policy_digest": plan.policy.policy_digest,
                "grants_authority": False,
            }
            receipts.append(cleanup_body | {"receipt_digest": digest(cleanup_body)})
    body: dict[str, object] = {
        "schema": "zekam-skill-client-projection-receipt/v2",
        "plan_digest": authorized_plan_digest,
        "package_digest": plan.package.package_digest,
        "integration_policy_digest": plan.policy.policy_digest,
        "targets": receipts,
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    return body | {"receipt_digest": digest(body)}
