"""Provider-free CLI integration inventory, sync and rollback orchestration."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from zekam.application.active_task_contract import ActiveTaskContract
from zekam.application.client_hook_bootstrap import (
    plan_client_hook_bootstrap,
    remove_managed_hook_entries,
)
from zekam.application.client_instruction_bootstrap import (
    plan_client_instruction_bootstrap,
    remove_managed_instruction_section,
)
from zekam.application.composition import ApplicationContext
from zekam.application.config import USER_CONFIG_FILE, load_config_document
from zekam.application.opencode_agent_bootstrap import DEFAULT_AGENT, opencode_template_bundle
from zekam.application.skill_packages import projected_artifact_digest
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes, parse_digest
from zekam.domain.client_integration import (
    ClientIntegrationId,
    ClientIntegrationPolicy,
    ClientIntegrationState,
)
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed

_MAX_SKILLS_PER_ROOT = 256
_SKILL_ROOTS = {
    ClientIntegrationId.OPENCODE: Path(".opencode") / "skills",
    ClientIntegrationId.CODEX: Path(".agents") / "skills",
    ClientIntegrationId.CLAUDE_CODE: Path(".claude") / "skills",
}
_USER_SKILL_ROOTS = {
    ClientIntegrationId.OPENCODE: Path(".config") / "opencode" / "skills",
    ClientIntegrationId.CODEX: Path(".agents") / "skills",
    ClientIntegrationId.CLAUDE_CODE: Path(".claude") / "skills",
}
_OPENCODE_CONFIG = Path(".config") / "opencode" / "opencode.json"
_OPENCODE_PLUGIN_REF = "./plugins/zekam-lifecycle.js"
_KNOWN_CLIENTS = {
    ClientIntegrationId.OPENCODE: ("opencode",),
    ClientIntegrationId.CODEX: ("codex",),
    ClientIntegrationId.CLAUDE_CODE: ("claude-code",),
}
_EXECUTABLE_COMMANDS = {
    ClientIntegrationId.OPENCODE: "opencode",
    ClientIntegrationId.CODEX: "codex",
    ClientIntegrationId.CLAUDE_CODE: "claude",
}
_SECRET_ASSIGNMENT = re.compile(
    rb"(?i)(?:password|passwd|secret|token|api[-_ ]?key|apikey|private[-_ ]?key)"
    rb"[\"'\s]*[:=]"
)


def _contains_secret_assignment(path: Path) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and bool(_SECRET_ASSIGNMENT.search(path.read_bytes()))
    )


@dataclass(frozen=True, slots=True)
class _TrustedPackageProjection:
    package_digest: str
    revision_digest: str
    name: str
    semantic_digest: str
    artifact_digest: str
    relative_path: str
    clients: tuple[str, ...]
    marker_schema: str
    source_root_identity_digest: str


def _export_receipt_digest(plan: dict[str, Any]) -> str:
    schema = plan.get("schema")
    plan_body = {key: value for key, value in plan.items() if key != "plan_digest"}
    plan_digest = plan.get("plan_digest")
    if schema not in {
        "zekam-skill-client-projection-plan/v1",
        "zekam-skill-client-projection-plan/v2",
    } or plan_digest != digest(plan_body):
        raise ValidationFailed("Skill export persisted plan identity invalid")
    package_digest = str(plan.get("package_digest"))
    semantic_digest = str(plan.get("semantic_digest"))
    parse_digest(package_digest)
    parse_digest(semantic_digest)
    targets = plan.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 3:
        raise ValidationFailed("Skill export persisted targets invalid")
    receipts: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            raise ValidationFailed("Skill export persisted target invalid")
        relative_path = target.get("relative_path")
        clients = target.get("clients")
        support = target.get("support_level")
        artifact_digest = str(target.get("artifact_digest"))
        if (
            not isinstance(relative_path, str)
            or not isinstance(clients, list)
            or not clients
            or any(not isinstance(client, str) for client in clients)
            or support != "instruction-distribution-only"
        ):
            raise ValidationFailed("Skill export persisted target metadata invalid")
        parse_digest(artifact_digest)
        enabled = True if schema.endswith("/v1") else target.get("enabled") is True
        if enabled:
            ownership: dict[str, Any] = {
                "schema": (
                    "zekam-managed-skill-projection/v1"
                    if schema.endswith("/v1")
                    else "zekam-managed-skill-projection/v2"
                ),
                "package_digest": package_digest,
                "semantic_digest": semantic_digest,
                "artifact_digest": artifact_digest,
                "relative_path": relative_path,
                "clients": clients,
                "support_level": support,
                "grants_authority": False,
            }
            if schema.endswith("/v2"):
                ownership["integration_policy_digest"] = plan.get(
                    "integration_policy_digest"
                )
            receipts.append(
                ownership
                | {
                    "receipt_digest": digest(ownership),
                    "state": "written-and-read-back",
                }
            )
            continue
        cleanup: dict[str, Any] = {
            "schema": "zekam-managed-skill-deprojection-receipt/v1",
            "relative_path": relative_path,
            "client": target.get("client"),
            "state": (
                "quarantined-and-read-back"
                if target.get("state") == "managed-disable"
                else "absent-and-read-back"
            ),
            "observed_artifact_digest": target.get("observed_artifact_digest"),
            "quarantine_relative_path": target.get("quarantine_relative_path"),
            "integration_policy_digest": plan.get("integration_policy_digest"),
            "grants_authority": False,
        }
        receipts.append(cleanup | {"receipt_digest": digest(cleanup)})
    receipt: dict[str, Any] = {
        "schema": (
            "zekam-skill-client-projection-receipt/v1"
            if schema.endswith("/v1")
            else "zekam-skill-client-projection-receipt/v2"
        ),
        "plan_digest": plan_digest,
        "package_digest": package_digest,
        "targets": receipts,
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    if schema.endswith("/v2"):
        receipt["integration_policy_digest"] = plan.get("integration_policy_digest")
    return digest(receipt)


def _trusted_package_projections(
    context: ApplicationContext,
) -> tuple[_TrustedPackageProjection, ...]:
    """Join immutable revisions to completed exact export receipt chains."""

    path = context.home / "state" / "learning.db"
    if not path.is_file() or path.is_symlink():
        return ()
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "select revision_digest,skill_id,name,package_digest,body_json "
                "from skill_revision_v2 order by revision_digest"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return ()
    revisions: dict[str, tuple[str, str, str]] = {}
    for row in rows:
        try:
            revision_digest = str(row[0])
            skill_id = str(row[1])
            name = str(row[2])
            package_digest = str(row[3])
            body = json.loads(str(row[4]))
            if not isinstance(body, dict):
                continue
            for value in (revision_digest, package_digest):
                parse_digest(value)
            if (
                digest(body) != revision_digest
                or body.get("schema") != "zekam-personal-skill-revision/v2"
                or body.get("skill_id") != skill_id
                or body.get("name") != name
                or body.get("package_digest") != package_digest
            ):
                continue
        except (json.JSONDecodeError, ValidationFailed):
            continue
        revisions[revision_digest] = (package_digest, skill_id, name)
    operational = context.settings.database.sqlite_path(context.home)
    if not operational.is_file() or operational.is_symlink():
        return ()
    try:
        connection = sqlite3.connect(f"file:{operational.as_posix()}?mode=ro", uri=True)
        try:
            exports = connection.execute(
                "select j.payload_json,j.terminal_evidence_digest,c.effect_digest,"
                "r.status,r.evidence_digest from local_job j "
                "join local_effect_claim c on c.job_id=j.id "
                "join local_effect_receipt r on r.claim_id=c.id "
                "where j.state='completed' and c.operation='skill.export-v1' "
                "order by j.id limit 1001"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return ()
    if len(exports) > 1000:
        return ()
    trusted: set[_TrustedPackageProjection] = set()
    for row in exports:
        try:
            raw_payload = str(row[0])
            terminal_evidence = str(row[1])
            effect_digest = str(row[2])
            if row[3] != "completed" or str(row[4]) != terminal_evidence:
                continue
            payload = json.loads(raw_payload)
            if not isinstance(payload, dict) or canonical_json(payload) != raw_payload:
                continue
            effect = payload.get("effect")
            if payload.get("operation") != "skill.export-v1" or not isinstance(effect, dict):
                continue
            if (
                effect.get("schema") != "zekam-skill-export-effect/v1"
                or digest(effect) != effect_digest
            ):
                continue
            plan = effect.get("plan")
            active = effect.get("active_revision")
            if not isinstance(plan, dict) or not isinstance(active, dict):
                continue
            package_digest = str(plan.get("package_digest"))
            semantic_digest = str(plan.get("semantic_digest"))
            source_root_identity_digest = str(plan.get("source_root_identity_digest"))
            revision_digest = str(active.get("revision_digest"))
            revision = revisions.get(revision_digest)
            skill_name = plan.get("skill_name")
            if (
                revision is None
                or revision[0] != package_digest
                or revision[1] != active.get("skill_id")
                or revision[2] != skill_name
                or _export_receipt_digest(plan) != terminal_evidence
            ):
                continue
            parse_digest(semantic_digest)
            parse_digest(source_root_identity_digest)
            marker_schema = (
                "zekam-managed-skill-projection/v1"
                if plan.get("schema") == "zekam-skill-client-projection-plan/v1"
                else "zekam-managed-skill-projection/v2"
            )
            targets = plan.get("targets")
            if not isinstance(targets, list):
                continue
            for target in targets:
                if not isinstance(target, dict):
                    continue
                if plan.get("schema") != "zekam-skill-client-projection-plan/v1" and (
                    target.get("enabled") is not True
                ):
                    continue
                clients = target.get("clients")
                relative_path = target.get("relative_path")
                artifact_digest = str(target.get("artifact_digest"))
                if (
                    not isinstance(clients, list)
                    or not clients
                    or any(not isinstance(client, str) for client in clients)
                    or not isinstance(relative_path, str)
                ):
                    continue
                parse_digest(artifact_digest)
                trusted.add(
                    _TrustedPackageProjection(
                        package_digest,
                        revision_digest,
                        str(skill_name),
                        semantic_digest,
                        artifact_digest,
                        relative_path,
                        tuple(clients),
                        marker_schema,
                        source_root_identity_digest,
                    )
                )
        except (json.JSONDecodeError, ValidationFailed):
            continue
    return tuple(
        sorted(
            trusted,
            key=lambda item: (
                item.package_digest,
                item.relative_path,
                item.clients,
                item.revision_digest,
                item.source_root_identity_digest,
            ),
        )
    )


def _executable_inventory(context: ApplicationContext) -> tuple[dict[str, Any], ...]:
    registered = {item.name: item for item in context.settings.clients}
    rows: list[dict[str, Any]] = []
    for client in ClientIntegrationId:
        configured = registered.get(client.value)
        configured_path = None if configured is None else configured.executable
        discovered = shutil.which(_EXECUTABLE_COMMANDS[client])
        detected_path = None if discovered is None else Path(discovered).resolve(strict=False)
        effective = (
            configured_path
            if configured_path is not None and configured_path.is_file()
            else detected_path
        )
        present = effective is not None and effective.is_file()
        rows.append(
            {
                "client": client.value,
                "configured": configured is not None,
                "configured_executable_present": bool(
                    configured_path is not None and configured_path.is_file()
                ),
                "executable_present": present,
                "path_identity_digest": (
                    None if effective is None else digest(os.path.normcase(str(effective)))
                ),
            }
        )
    return tuple(rows)


def _real_directory(path: Path) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return not bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _root_identity(path: Path, *, must_exist: bool) -> str:
    resolved = path.resolve(strict=must_exist)
    if must_exist and not _real_directory(resolved):
        raise PolicyViolation("CLI integration exact real root required")
    return digest(os.path.normcase(str(resolved)))


def integration_mutation_resource(
    *,
    scope: str,
    native_user_root: Path,
    project_root: Path | None = None,
) -> str:
    """Return the shared single-writer key for every integration mutation surface."""

    if scope == "user" and project_root is None:
        return "client-integrations:native:" + _root_identity(
            native_user_root, must_exist=True
        )
    if scope == "project" and project_root is not None:
        return "client-integrations:project:" + _root_identity(
            project_root, must_exist=True
        )
    raise ValidationFailed("CLI integration mutation resource scope/root mismatch")


def _source_head(core: Path, *, packaged_fallback: str) -> str:
    try:
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=core,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return packaged_fallback
    value = result.stdout.strip()
    if result.returncode or len(value) != 40:
        return packaged_fallback
    return value


def _file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise PolicyViolation("CLI integration config regular file olmali")
    return digest_of_bytes(path.read_bytes())


def _artifact_digest(path: Path) -> str:
    if path.is_dir() and not path.is_symlink():
        return projected_artifact_digest(path)
    value = _file_digest(path)
    if value is None:
        raise PolicyViolation("CLI integration managed artifact missing")
    return value


def _render_opencode_cleanup(config_path: Path, operation: dict[str, Any]) -> bytes:
    if _file_digest(config_path) != operation.get("before_digest"):
        raise PolicyViolation("OpenCode config changed before integration cleanup")
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("OpenCode config cleanup JSON invalid") from exc
    if not isinstance(document, dict):
        raise PolicyViolation("OpenCode config cleanup object required")
    if operation.get("remove_plugin_reference"):
        plugins = document.get("plugin")
        if not isinstance(plugins, list) or any(not isinstance(item, str) for item in plugins):
            raise PolicyViolation("OpenCode plugin config cleanup shape drift")
        if plugins.count(_OPENCODE_PLUGIN_REF) != 1:
            raise PolicyViolation("OpenCode managed plugin reference drift")
        remaining = [item for item in plugins if item != _OPENCODE_PLUGIN_REF]
        if remaining:
            document["plugin"] = remaining
        else:
            document.pop("plugin")
    if operation.get("remove_default_agent"):
        if document.get("default_agent") != DEFAULT_AGENT:
            raise PolicyViolation("OpenCode managed default agent drift")
        document.pop("default_agent")
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _render_hook_cleanup(config_path: Path, operation: dict[str, Any]) -> bytes:
    if _file_digest(config_path) != operation.get("before_digest"):
        raise PolicyViolation("Client hook config changed before cleanup")
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("Client hook cleanup JSON invalid") from exc
    if not isinstance(document, dict):
        raise PolicyViolation("Client hook cleanup object required")
    try:
        updated, removed = remove_managed_hook_entries(
            document,
            client_id=str(operation["client"]),
        )
    except ConfigurationError as exc:
        raise PolicyViolation("Client hook cleanup ownership drift") from exc
    if removed != operation.get("managed_group_count"):
        raise PolicyViolation("Client hook cleanup managed group drift")
    return (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _render_instruction_cleanup(config_path: Path, operation: dict[str, Any]) -> bytes:
    if _file_digest(config_path) != operation.get("before_digest"):
        raise PolicyViolation("Client instruction config changed before cleanup")
    try:
        updated, removed = remove_managed_instruction_section(
            config_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ConfigurationError) as exc:
        raise PolicyViolation("Client instruction cleanup ownership drift") from exc
    if not removed:
        raise PolicyViolation("Client instruction cleanup managed section missing")
    return updated.encode("utf-8")


def _assert_safe_integration_quarantine(home: Path, native_user_root: Path) -> None:
    quarantine = (home / "quarantine" / "client-integrations").resolve(strict=False)
    discoveries = (
        native_user_root / ".config" / "opencode",
        native_user_root / ".agents",
        native_user_root / ".claude",
    )
    for discovery in discoveries:
        resolved = discovery.resolve(strict=False)
        if (
            quarantine == resolved
            or quarantine.is_relative_to(resolved)
            or resolved.is_relative_to(quarantine)
        ):
            raise PolicyViolation("CLI integration quarantine discovery tree disinda olmali")


def _assert_quarantine_destination(root: Path, destination: Path) -> None:
    resolved_root = root.resolve(strict=True)
    if not _real_directory(resolved_root):
        raise PolicyViolation("CLI integration quarantine root identity invalid")
    try:
        destination.resolve(strict=False).relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PolicyViolation("CLI integration quarantine path escape") from exc
    current = destination.parent
    while current != resolved_root:
        if not _real_directory(current):
            raise PolicyViolation("CLI integration quarantine parent identity invalid")
        current = current.parent


def _managed_skill(
    project_root: Path,
    client: ClientIntegrationId,
    directory: Path,
    *,
    trusted_package_projections: tuple[_TrustedPackageProjection, ...],
) -> tuple[dict[str, Any] | None, str | None]:
    marker = directory / ".zekam-managed.json"
    if not marker.exists():
        return None, None
    if not _real_directory(directory) or not marker.is_file() or marker.is_symlink():
        return None, "managed-marker-or-directory-identity-invalid"
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
        actual = projected_artifact_digest(directory)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, PolicyViolation):
        return None, "managed-artifact-unreadable-or-drifted"
    allowed_clients: set[tuple[str, ...]] = {_KNOWN_CLIENTS[client]}
    if client is ClientIntegrationId.CODEX:
        allowed_clients.add(("codex", "opencode"))
    relative = directory.relative_to(project_root).as_posix()
    if (
        not isinstance(document, dict)
        or document.get("schema")
        not in {"zekam-managed-skill-projection/v1", "zekam-managed-skill-projection/v2"}
        or document.get("relative_path") != relative
        or tuple(document.get("clients", ())) not in allowed_clients
        or document.get("support_level") != "instruction-distribution-only"
        or document.get("grants_authority") is not False
        or document.get("artifact_digest") != actual
    ):
        return None, "managed-artifact-metadata-drift"
    try:
        package_digest = str(document.get("package_digest"))
        parse_digest(package_digest)
        parse_digest(str(document.get("semantic_digest")))
    except ValidationFailed:
        return None, "managed-artifact-package-digest-invalid"
    exact = tuple(
        trusted
        for trusted in trusted_package_projections
        if trusted.package_digest == package_digest
        and trusted.name == directory.name
        and trusted.semantic_digest == document.get("semantic_digest")
        and trusted.artifact_digest == actual
        and trusted.relative_path == relative
        and trusted.clients == tuple(document.get("clients", ()))
        and trusted.marker_schema == document.get("schema")
        and trusted.source_root_identity_digest
        == _root_identity(project_root, must_exist=True)
    )
    if not any(trusted.package_digest == package_digest for trusted in trusted_package_projections):
        return None, "canonical-package-ownership-proof-missing"
    if len(exact) != 1:
        return None, "canonical-package-projection-binding-mismatch"
    return document | {"actual_artifact_digest": actual}, None


def _skill_inventory(
    root_path: Path,
    skill_roots: dict[ClientIntegrationId, Path],
    *,
    trusted_package_projections: tuple[_TrustedPackageProjection, ...],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for client, relative_root in skill_roots.items():
        root = root_path / relative_root
        if not root.exists():
            continue
        if not _real_directory(root):
            rows.append(
                {
                    "client": client.value,
                    "relative_path": relative_root.as_posix(),
                    "conflict": "skill-root-symlink-junction-or-special",
                }
            )
            continue
        children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        if len(children) > _MAX_SKILLS_PER_ROOT:
            raise PolicyViolation("CLI integration skill inventory bound exceeded")
        for directory in children:
            document, conflict = _managed_skill(
                root_path,
                client,
                directory,
                trusted_package_projections=trusted_package_projections,
            )
            if document is None and conflict is None:
                continue
            rows.append(
                {
                    "client": client.value,
                    "relative_path": directory.relative_to(root_path).as_posix(),
                    "artifact_type": "skill-projection",
                    "artifact_digest": (
                        None if document is None else document["actual_artifact_digest"]
                    ),
                    "package_digest": None if document is None else document["package_digest"],
                    "marker_clients": (
                        [] if document is None else list(document.get("clients", ()))
                    ),
                    "conflict": conflict,
                }
            )
    return tuple(rows)


def _project_inventory(
    project_root: Path,
    *,
    trusted_package_projections: tuple[_TrustedPackageProjection, ...],
) -> tuple[dict[str, Any], ...]:
    return _skill_inventory(
        project_root,
        _SKILL_ROOTS,
        trusted_package_projections=trusted_package_projections,
    )


def _user_opencode_inventory(native_user_root: Path) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    bundle = opencode_template_bundle()
    agents = native_user_root / ".config" / "opencode" / "agents"
    exact_agents: set[str] = set()
    if agents.exists():
        if not _real_directory(agents):
            rows.append(
                {
                    "client": "opencode",
                    "artifact_type": "agent-root",
                    "relative_path": agents.relative_to(native_user_root).as_posix(),
                    "artifact_digest": None,
                    "conflict": "agent-root-symlink-junction-or-special",
                }
            )
        else:
            candidates = sorted(agents.glob("zekam-*.md"), key=lambda item: item.name.casefold())
            if len(candidates) > 64:
                raise PolicyViolation("CLI integration OpenCode agent inventory bound exceeded")
            for candidate in candidates:
                relative = candidate.relative_to(native_user_root).as_posix()
                expected = bundle.get(f"agents/{candidate.name}")
                if not candidate.is_file() or candidate.is_symlink():
                    agent_conflict: str | None = "agent-file-identity-invalid"
                    actual = None
                else:
                    payload = candidate.read_bytes()
                    actual = digest_of_bytes(payload)
                    agent_conflict = (
                        None
                        if expected is not None and payload == expected.encode()
                        else "agent-content-unowned-or-drifted"
                    )
                if agent_conflict is None:
                    exact_agents.add(candidate.name)
                rows.append(
                    {
                        "client": "opencode",
                        "artifact_type": "agent",
                        "relative_path": relative,
                        "artifact_digest": actual,
                        "conflict": agent_conflict,
                    }
                )
    plugin = native_user_root / ".config" / "opencode" / "plugins" / "zekam-lifecycle.js"
    plugin_exact = False
    if plugin.exists() or plugin.is_symlink():
        relative = plugin.relative_to(native_user_root).as_posix()
        if not plugin.is_file() or plugin.is_symlink():
            actual = None
            plugin_conflict: str | None = "plugin-file-identity-invalid"
        else:
            payload = plugin.read_bytes()
            actual = digest_of_bytes(payload)
            plugin_exact = payload == bundle["plugins/zekam-lifecycle.js"].encode()
            plugin_conflict = None if plugin_exact else "plugin-content-unowned-or-drifted"
        rows.append(
            {
                "client": "opencode",
                "artifact_type": "plugin",
                "relative_path": relative,
                "artifact_digest": actual,
                "conflict": plugin_conflict,
            }
        )
    config = native_user_root / _OPENCODE_CONFIG
    if config.exists() or config.is_symlink():
        relative = config.relative_to(native_user_root).as_posix()
        if not config.is_file() or config.is_symlink():
            rows.append(
                {
                    "client": "opencode",
                    "artifact_type": "config-reference",
                    "relative_path": relative,
                    "artifact_digest": None,
                    "conflict": "config-file-identity-invalid",
                }
            )
        else:
            try:
                document = json.loads(config.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                document = None
            if not isinstance(document, dict):
                rows.append(
                    {
                        "client": "opencode",
                        "artifact_type": "config-reference",
                        "relative_path": relative,
                        "artifact_digest": _file_digest(config),
                        "conflict": "config-json-invalid",
                    }
                )
            else:
                plugins = document.get("plugin", [])
                plugin_ref = isinstance(plugins, list) and _OPENCODE_PLUGIN_REF in plugins
                default_ref = document.get("default_agent") == DEFAULT_AGENT
                if plugin_ref or default_ref:
                    config_conflict: str | None = None
                    if _contains_secret_assignment(config):
                        config_conflict = "secret-bearing-native-config-preserved"
                    elif plugin_ref and not plugin_exact:
                        config_conflict = "plugin-reference-without-exact-managed-plugin"
                    if default_ref and "zekam-coordinator.md" not in exact_agents:
                        config_conflict = "default-agent-without-exact-managed-agent"
                    rows.append(
                        {
                            "client": "opencode",
                            "artifact_type": "config-reference",
                            "relative_path": relative,
                            "artifact_digest": _file_digest(config),
                            "remove_plugin_reference": plugin_ref,
                            "remove_default_agent": default_ref,
                            "conflict": config_conflict,
                        }
                    )
    return tuple(rows)


def _user_hook_inventory(native_user_root: Path) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    targets = (
        ("codex", Path(".codex") / "hooks.json"),
        ("claude-code", Path(".claude") / "settings.json"),
    )
    for client, relative in targets:
        path = native_user_root / relative
        if not path.exists() and not path.is_symlink():
            continue
        conflict: str | None = None
        removed = 0
        if not path.is_file() or path.is_symlink():
            conflict = "hook-config-file-identity-invalid"
        else:
            try:
                raw = path.read_text(encoding="utf-8")
                document = json.loads(raw)
                if not isinstance(document, dict):
                    raise ValueError("object required")
                _updated, removed = remove_managed_hook_entries(
                    document,
                    client_id=client,
                )
                mentions = (
                    "zekam.interfaces.cli.client" in raw and '"--client"' in raw and client in raw
                )
                if mentions and removed == 0:
                    conflict = "hook-command-unowned-or-drifted"
                if _contains_secret_assignment(path):
                    conflict = "secret-bearing-native-config-preserved"
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                conflict = "hook-config-json-invalid"
            except ConfigurationError:
                conflict = "hook-command-unowned-or-drifted"
        if removed or conflict is not None:
            rows.append(
                {
                    "client": client,
                    "artifact_type": "hook-config",
                    "relative_path": relative.as_posix(),
                    "artifact_digest": _file_digest(path) if path.is_file() else None,
                    "managed_group_count": removed,
                    "conflict": conflict,
                }
            )
    return tuple(rows)


def _user_instruction_inventory(native_user_root: Path) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    targets = (
        ("opencode", Path(".config") / "opencode" / "AGENTS.md"),
        ("codex", Path(".codex") / "AGENTS.md"),
        ("claude-code", Path(".claude") / "CLAUDE.md"),
    )
    for client, relative in targets:
        path = native_user_root / relative
        if not path.exists() and not path.is_symlink():
            continue
        conflict: str | None = None
        managed = False
        if not path.is_file() or path.is_symlink():
            conflict = "instruction-file-identity-invalid"
        else:
            try:
                _updated, managed = remove_managed_instruction_section(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, ConfigurationError):
                conflict = "instruction-section-unowned-or-drifted"
            if _contains_secret_assignment(path):
                conflict = "secret-bearing-native-config-preserved"
        if managed or conflict is not None:
            rows.append(
                {
                    "client": client,
                    "artifact_type": "instruction-config",
                    "relative_path": relative.as_posix(),
                    "artifact_digest": _file_digest(path) if path.is_file() else None,
                    "conflict": conflict,
                }
            )
    return tuple(rows)


def _user_inventory(
    native_user_root: Path,
    *,
    trusted_package_projections: tuple[_TrustedPackageProjection, ...],
) -> tuple[dict[str, Any], ...]:
    return (
        _skill_inventory(
            native_user_root,
            _USER_SKILL_ROOTS,
            trusted_package_projections=trusted_package_projections,
        )
        + _user_opencode_inventory(native_user_root)
        + _user_hook_inventory(native_user_root)
        + _user_instruction_inventory(native_user_root)
    )


def integration_status(
    context: ApplicationContext,
    *,
    native_user_root: Path | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    native_user_root = Path.home() if native_user_root is None else native_user_root
    if not native_user_root.is_absolute() or not _real_directory(native_user_root):
        raise PolicyViolation("CLI integration exact native user root required")
    _assert_safe_integration_quarantine(context.home, native_user_root)
    trusted_packages = _trusted_package_projections(context)
    user_inventory = _user_inventory(
        native_user_root,
        trusted_package_projections=trusted_packages,
    )
    project_inventory = (
        ()
        if project_root is None
        else _project_inventory(
            project_root,
            trusted_package_projections=trusted_packages,
        )
    )
    inventory = user_inventory + project_inventory
    executable_rows = _executable_inventory(context)
    executable = {str(item["client"]): bool(item["executable_present"]) for item in executable_rows}
    capabilities = {
        ClientIntegrationId.OPENCODE: (
            "instruction-projection",
            "agent-bootstrap",
            "lifecycle-events-v2",
        ),
        ClientIntegrationId.CODEX: ("instruction-projection", "reviewed-lifecycle-hook"),
        ClientIntegrationId.CLAUDE_CODE: (
            "instruction-projection",
            "reviewed-lifecycle-hook",
        ),
    }
    states: list[ClientIntegrationState] = []
    for client in ClientIntegrationId:
        rows = tuple(row for row in inventory if row["client"] == client.value)
        managed = any(row["conflict"] is None for row in rows)
        conflicts = tuple(
            f"{row['relative_path']}:{row['conflict']}"
            for row in rows
            if row["conflict"] is not None
        )
        enabled = context.settings.cli.integrations.enabled(client)
        states.append(
            ClientIntegrationState(
                client=client,
                enabled=enabled,
                supported_capabilities=capabilities[client],
                executable_present=bool(executable.get(client.value, False)),
                managed_artifacts_present=managed,
                cleanup_required=not enabled and managed,
                conflicts=conflicts,
            )
        )
    body: dict[str, Any] = {
        "schema": "zekam-cli-integration-status/v1",
        "policy": context.settings.cli.integrations.body(),
        "policy_digest": context.settings.cli.integrations.policy_digest,
        "config_effective_digest": (
            None
            if context.settings.config_provenance is None
            else context.settings.config_provenance.effective_digest
        ),
        "clients": [item.as_dict() for item in states],
        "managed_inventory": list(inventory),
        "executable_inventory": list(executable_rows),
        "native_user_root_identity_digest": _root_identity(native_user_root, must_exist=True),
        "user_scanned": True,
        "project_scanned": project_root is not None,
        "read_only": True,
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    return body | {"status_digest": digest(body)}


@dataclass(frozen=True, slots=True)
class IntegrationSyncPlan:
    context: ApplicationContext
    scope: str
    native_user_root: Path
    project_root: Path | None
    before_policy: ClientIntegrationPolicy
    after_policy: ClientIntegrationPolicy
    config_before_digest: str | None
    config_after_bytes: bytes | None
    operations: tuple[dict[str, Any], ...]
    conflicts: tuple[dict[str, str], ...]
    native_write_payloads: Mapping[str, bytes]
    pending_project_cleanup: tuple[dict[str, Any], ...]
    registered_projects: tuple[tuple[str, Path], ...]

    @property
    def body(self) -> dict[str, Any]:
        active = ActiveTaskContract.load(self.context.core_path / "AKTIF_GOREV.md")
        provenance = self.context.settings.config_provenance
        if provenance is None:
            raise ValidationFailed("CLI integration sync config provenance ister")
        return {
            "schema": "zekam-cli-integration-sync-plan/v1",
            "task_id": active.task_id,
            "task_digest": active.source_digest,
            "source_head": _source_head(
                self.context.core_path,
                packaged_fallback=active.baseline_head,
            ),
            "package_source_manifest_digest": _file_digest(
                Path(__file__).resolve().parents[1] / "PACKAGE_RELEASE_MANIFEST.json"
            ),
            "config_effective_digest": provenance.effective_digest,
            "config_graph_digest": provenance.graph_digest,
            "scope": self.scope,
            "native_user_root_identity_digest": _root_identity(
                self.native_user_root, must_exist=True
            ),
            "project_root_identity_digest": (
                None
                if self.project_root is None
                else _root_identity(self.project_root, must_exist=True)
            ),
            "before_policy": self.before_policy.body(),
            "after_policy": self.after_policy.body(),
            "executables": list(_executable_inventory(self.context)),
            "config_before_digest": self.config_before_digest,
            "config_after_digest": (
                None
                if self.config_after_bytes is None
                else digest_of_bytes(self.config_after_bytes)
            ),
            "operations": list(self.operations),
            "conflicts": list(self.conflicts),
            "pending_project_cleanup": list(self.pending_project_cleanup),
            "apply": False,
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.body)

    def as_dict(self) -> dict[str, Any]:
        return self.body | {"plan_digest": self.plan_digest}


def _render_user_config(context: ApplicationContext, policy: ClientIntegrationPolicy) -> bytes:
    path = context.home / USER_CONFIG_FILE
    document = load_config_document(path)
    document["schema"] = "zekam-config/v1"
    cli = document.get("cli")
    if cli is None:
        cli = {}
        document["cli"] = cli
    if not isinstance(cli, dict):
        raise ValidationFailed("CLI integration user config shape drift")
    cli["integrations"] = policy.body()
    return yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).encode("utf-8")


def build_sync_plan(
    context: ApplicationContext,
    *,
    scope: str,
    native_user_root: Path,
    project_root: Path | None = None,
    enable: tuple[str, ...] = (),
    disable: tuple[str, ...] = (),
    registered_projects: tuple[tuple[str, Path], ...] = (),
) -> IntegrationSyncPlan:
    if scope not in {"user", "project"}:
        raise ValidationFailed("CLI integration scope user veya project olmali")
    if not native_user_root.is_absolute() or not _real_directory(native_user_root):
        raise PolicyViolation("CLI integration exact native user root required")
    _assert_safe_integration_quarantine(context.home, native_user_root)
    before = context.settings.cli.integrations
    if scope == "project" and (enable or disable):
        raise PolicyViolation("Project scope kalici user integration tercihini degistiremez")
    after = before.with_change(enable=enable, disable=disable) if scope == "user" else before
    config_path = context.home / USER_CONFIG_FILE
    config_before = _file_digest(config_path)
    config_after = (
        _render_user_config(context, after) if scope == "user" and before != after else None
    )
    operations: list[dict[str, Any]] = []
    conflicts: list[dict[str, str]] = []
    native_write_payloads: dict[str, bytes] = {}
    pending_project_cleanup: list[dict[str, Any]] = []
    if scope == "user" and before != after:
        operations.append(
            {
                "operation": "update-user-policy",
                "relative_path": USER_CONFIG_FILE,
                "before_digest": config_before,
                "after_digest": digest_of_bytes(config_after or b""),
            }
        )
    if scope == "user":
        instruction_plan = plan_client_instruction_bootstrap(
            user_home=native_user_root,
            integration_policy=after,
        )
        for instruction in instruction_plan.files:
            if instruction.action == "unchanged":
                continue
            relative = instruction.path.relative_to(native_user_root).as_posix()
            if _contains_secret_assignment(instruction.path):
                conflicts.append(
                    {
                        "relative_path": relative,
                        "reason": "secret-bearing-native-config-preserved",
                    }
                )
                continue
            payload = instruction.content.encode("utf-8")
            operations.append(
                {
                    "operation": "write-client-instruction-config",
                    "artifact_type": "instruction-config",
                    "client": instruction.client_id,
                    "relative_path": relative,
                    "before_digest": _file_digest(instruction.path),
                    "after_digest": digest_of_bytes(payload),
                    "ownership_proof": "reviewed-managed-section-render",
                }
            )
            native_write_payloads[relative] = payload
        hook_plan = plan_client_hook_bootstrap(
            user_home=native_user_root,
            python_executable=Path(sys.executable).resolve(strict=True),
            policy=after,
        )
        for hook in hook_plan.files:
            if hook.action == "unchanged":
                continue
            relative = hook.path.relative_to(native_user_root).as_posix()
            if _contains_secret_assignment(hook.path):
                conflicts.append(
                    {
                        "relative_path": relative,
                        "reason": "secret-bearing-native-config-preserved",
                    }
                )
                continue
            payload = (json.dumps(hook.document, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            )
            operations.append(
                {
                    "operation": "write-client-hook-config",
                    "artifact_type": "hook-config",
                    "client": hook.client_id,
                    "relative_path": relative,
                    "before_digest": _file_digest(hook.path),
                    "after_digest": digest_of_bytes(payload),
                    "ownership_proof": "reviewed-managed-hook-render",
                }
            )
            native_write_payloads[relative] = payload
        for row in _user_inventory(
            native_user_root,
            trusted_package_projections=_trusted_package_projections(context),
        ):
            client = ClientIntegrationId(str(row["client"]))
            if after.enabled(client):
                continue
            if row["conflict"] is not None:
                conflicts.append(
                    {"relative_path": str(row["relative_path"]), "reason": str(row["conflict"])}
                )
                continue
            artifact = row.get("artifact_digest")
            if not isinstance(artifact, str):
                conflicts.append(
                    {
                        "relative_path": str(row["relative_path"]),
                        "reason": "managed-artifact-digest-missing",
                    }
                )
                continue
            if row["artifact_type"] == "config-reference":
                operation: dict[str, Any] = {
                    "operation": "detach-opencode-config",
                    "client": client.value,
                    "relative_path": str(row["relative_path"]),
                    "before_digest": artifact,
                    "remove_plugin_reference": bool(row.get("remove_plugin_reference")),
                    "remove_default_agent": bool(row.get("remove_default_agent")),
                    "ownership_proof": "exact-linked-managed-artifact-and-reference",
                }
                rendered = _render_opencode_cleanup(
                    native_user_root / str(row["relative_path"]), operation
                )
                operation["after_digest"] = digest_of_bytes(rendered)
                operations.append(operation)
                continue
            if row["artifact_type"] == "hook-config":
                hook_operation: dict[str, Any] = {
                    "operation": "detach-client-hook-config",
                    "artifact_type": "hook-config",
                    "client": client.value,
                    "relative_path": str(row["relative_path"]),
                    "before_digest": artifact,
                    "managed_group_count": int(row["managed_group_count"]),
                    "ownership_proof": "exact-reviewed-hook-group-and-config-digest",
                }
                rendered = _render_hook_cleanup(
                    native_user_root / str(row["relative_path"]), hook_operation
                )
                hook_operation["after_digest"] = digest_of_bytes(rendered)
                operations.append(hook_operation)
                continue
            if row["artifact_type"] == "instruction-config":
                instruction_operation: dict[str, Any] = {
                    "operation": "detach-client-instruction-config",
                    "artifact_type": "instruction-config",
                    "client": client.value,
                    "relative_path": str(row["relative_path"]),
                    "before_digest": artifact,
                    "ownership_proof": "exact-reviewed-managed-instruction-section",
                }
                rendered = _render_instruction_cleanup(
                    native_user_root / str(row["relative_path"]), instruction_operation
                )
                instruction_operation["after_digest"] = digest_of_bytes(rendered)
                operations.append(instruction_operation)
                continue
            operations.append(
                {
                    "operation": "quarantine-managed-native-artifact",
                    "artifact_type": str(row["artifact_type"]),
                    "client": client.value,
                    "relative_path": str(row["relative_path"]),
                    "before_digest": artifact,
                    "ownership_proof": "exact-template-or-managed-marker-plus-artifact-digest",
                    "quarantine_relative_path": (
                        Path("native")
                        / _root_identity(native_user_root, must_exist=True).removeprefix("sha256:")
                        / client.value
                        / str(row["artifact_type"])
                        / Path(str(row["relative_path"])).name
                        / artifact.removeprefix("sha256:")
                    ).as_posix(),
                }
            )
    if scope == "project":
        if (
            project_root is None
            or not project_root.is_absolute()
            or not _real_directory(project_root)
        ):
            raise PolicyViolation("Project integration sync exact real project root ister")
        inventory = _project_inventory(
            project_root,
            trusted_package_projections=_trusted_package_projections(context),
        )
        by_path = {str(row["relative_path"]): row for row in inventory}
        for row in inventory:
            if row["conflict"] is not None:
                conflicts.append(
                    {"relative_path": str(row["relative_path"]), "reason": str(row["conflict"])}
                )
                continue
            client = ClientIntegrationId(str(row["client"]))
            if after.enabled(client):
                continue
            if client is ClientIntegrationId.CODEX and "opencode" in row["marker_clients"]:
                replacement = (
                    Path(".opencode") / "skills" / Path(str(row["relative_path"])).name
                ).as_posix()
                candidate = by_path.get(replacement)
                if after.opencode and (
                    candidate is None
                    or candidate["conflict"] is not None
                    or candidate["artifact_digest"] != row["artifact_digest"]
                ):
                    conflicts.append(
                        {
                            "relative_path": str(row["relative_path"]),
                            "reason": "opencode-specific-replacement-required",
                        }
                    )
                    continue
            artifact = str(row["artifact_digest"])
            operations.append(
                {
                    "operation": "quarantine-managed-skill",
                    "client": client.value,
                    "relative_path": str(row["relative_path"]),
                    "before_digest": artifact,
                    "package_digest": row.get("package_digest"),
                    "marker_clients": list(row.get("marker_clients", ())),
                    "ownership_proof": "managed-marker-plus-artifact-digest",
                    "quarantine_relative_path": (
                        Path("projects")
                        / _root_identity(project_root, must_exist=True).removeprefix("sha256:")
                        / client.value
                        / Path(str(row["relative_path"])).name
                        / artifact.removeprefix("sha256:")
                    ).as_posix(),
                }
            )
    if scope == "user":
        trusted = _trusted_package_projections(context)
        for project_ref, registered_root in sorted(registered_projects, key=lambda item: item[0]):
            if not registered_root.is_absolute() or not _real_directory(registered_root):
                continue
            rows = _project_inventory(
                registered_root,
                trusted_package_projections=trusted,
            )
            relevant = [
                row
                for row in rows
                if row["conflict"] is not None
                or not after.enabled(ClientIntegrationId(str(row["client"])))
            ]
            if not relevant:
                continue
            pending_project_cleanup.append(
                {
                    "project_ref": project_ref,
                    "project_root_identity_digest": _root_identity(
                        registered_root, must_exist=True
                    ),
                    "managed_cleanup_count": sum(1 for row in relevant if row["conflict"] is None),
                    "conflict_count": sum(1 for row in relevant if row["conflict"] is not None),
                    "requires_separate_project_plan": True,
                }
            )
    return IntegrationSyncPlan(
        context=context,
        scope=scope,
        native_user_root=native_user_root.resolve(strict=True),
        project_root=None if project_root is None else project_root.resolve(strict=True),
        before_policy=before,
        after_policy=after,
        config_before_digest=config_before,
        config_after_bytes=config_after,
        operations=tuple(operations),
        conflicts=tuple(conflicts),
        native_write_payloads=native_write_payloads,
        pending_project_cleanup=tuple(pending_project_cleanup),
        registered_projects=registered_projects,
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_sync_plan(plan: IntegrationSyncPlan, *, authorized_plan_digest: str) -> dict[str, Any]:
    if authorized_plan_digest != plan.plan_digest:
        raise PolicyViolation("CLI integration exact plan digest required")
    manifest_path = (
        plan.context.home
        / "state"
        / "manifests"
        / "client-integrations"
        / f"{plan.plan_digest.removeprefix('sha256:')}.json"
    )
    if manifest_path.is_file() and not manifest_path.is_symlink():
        return sync_receipt(plan, authorized_plan_digest=authorized_plan_digest)
    if plan.conflicts:
        raise PolicyViolation("CLI integration sync unresolved ownership conflict")
    fresh_context = plan.context
    fresh = build_sync_plan(
        fresh_context,
        scope=plan.scope,
        native_user_root=plan.native_user_root,
        project_root=plan.project_root,
        enable=tuple(
            key
            for key, value in plan.after_policy.body().items()
            if value and not plan.before_policy.body()[key]
        ),
        disable=tuple(
            key
            for key, value in plan.after_policy.body().items()
            if not value and plan.before_policy.body()[key]
        ),
        registered_projects=plan.registered_projects,
    )
    if fresh.plan_digest != plan.plan_digest:
        raise PolicyViolation("CLI integration plan source changed before apply")
    quarantine_root = plan.context.home / "quarantine" / "client-integrations"
    moved: list[tuple[Path, Path, str]] = []
    config_path = plan.context.home / USER_CONFIG_FILE
    config_backup: Path | None = None
    native_config_backups: list[tuple[Path, Path | None]] = []
    receipt_key = plan.plan_digest.removeprefix("sha256:")
    try:
        for operation in plan.operations:
            if operation["operation"] not in {
                "detach-opencode-config",
                "detach-client-hook-config",
                "detach-client-instruction-config",
                "write-client-hook-config",
                "write-client-instruction-config",
            }:
                continue
            native_config = plan.native_user_root / str(operation["relative_path"])
            if operation["operation"] == "detach-opencode-config":
                rendered = _render_opencode_cleanup(native_config, operation)
            elif operation["operation"] == "detach-client-hook-config":
                rendered = _render_hook_cleanup(native_config, operation)
            elif operation["operation"] == "detach-client-instruction-config":
                rendered = _render_instruction_cleanup(native_config, operation)
            else:
                try:
                    rendered = plan.native_write_payloads[str(operation["relative_path"])]
                except KeyError as exc:
                    raise PolicyViolation("Native client config write payload missing") from exc
                if _file_digest(native_config) != operation["before_digest"]:
                    raise PolicyViolation("Native client config changed before write")
            if digest_of_bytes(rendered) != operation["after_digest"]:
                raise PolicyViolation("Native client config cleanup plan drift")
            native_config_backup: Path | None = None
            if native_config.exists():
                backup_root = quarantine_root / "native-config"
                backup_root.mkdir(parents=True, exist_ok=True)
                suffix = digest(str(operation["relative_path"])).removeprefix("sha256:")[:16]
                native_config_backup = backup_root / f"{receipt_key}-{suffix}.json"
                _assert_quarantine_destination(quarantine_root, native_config_backup)
                if native_config_backup.exists() or native_config_backup.is_symlink():
                    raise PolicyViolation("Native client config cleanup backup collision")
                _atomic_write(native_config_backup, native_config.read_bytes())
            native_config_backups.append((native_config, native_config_backup))
            _atomic_write(native_config, rendered)
            if _file_digest(native_config) != operation["after_digest"]:
                raise PolicyViolation("Native client config cleanup readback drift")
        for operation in plan.operations:
            if operation["operation"] not in {
                "quarantine-managed-skill",
                "quarantine-managed-native-artifact",
            }:
                continue
            if operation["operation"] == "quarantine-managed-skill":
                if plan.project_root is None:
                    raise PolicyViolation("CLI integration project operation root missing")
                source = plan.project_root / str(operation["relative_path"])
            else:
                source = plan.native_user_root / str(operation["relative_path"])
            destination = quarantine_root / str(operation["quarantine_relative_path"])
            if destination.exists() or destination.is_symlink():
                raise PolicyViolation("CLI integration quarantine collision")
            if _artifact_digest(source) != operation["before_digest"]:
                raise PolicyViolation("CLI integration managed artifact changed before apply")
            destination.parent.mkdir(parents=True, exist_ok=True)
            _assert_quarantine_destination(quarantine_root, destination)
            os.replace(source, destination)
            moved.append((source, destination, str(operation["before_digest"])))
            if _artifact_digest(destination) != operation["before_digest"]:
                raise PolicyViolation("CLI integration quarantine readback drift")
        if plan.config_after_bytes is not None and plan.before_policy != plan.after_policy:
            if _file_digest(config_path) != plan.config_before_digest:
                raise PolicyViolation("CLI integration user config changed before apply")
            backup_root = quarantine_root / "config"
            backup_root.mkdir(parents=True, exist_ok=True)
            if config_path.exists():
                config_backup = backup_root / f"{receipt_key}.yaml"
                _assert_quarantine_destination(quarantine_root, config_backup)
                if config_backup.exists():
                    raise PolicyViolation("CLI integration config backup collision")
                _atomic_write(config_backup, config_path.read_bytes())
            _atomic_write(config_path, plan.config_after_bytes)
            if _file_digest(config_path) != digest_of_bytes(plan.config_after_bytes):
                raise PolicyViolation("CLI integration user config readback drift")
    except BaseException as error:
        rollback_failed = False
        if config_backup is not None and config_backup.exists():
            try:
                _atomic_write(config_path, config_backup.read_bytes())
            except OSError:
                rollback_failed = True
        for native_config, native_config_backup in reversed(native_config_backups):
            try:
                if native_config_backup is None:
                    native_config.unlink(missing_ok=True)
                else:
                    _atomic_write(native_config, native_config_backup.read_bytes())
            except OSError:
                rollback_failed = True
        for source, destination, expected in reversed(moved):
            try:
                if source.exists() or _artifact_digest(destination) != expected:
                    raise OSError("integration rollback target drift")
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)
            except (OSError, PolicyViolation):
                rollback_failed = True
        if rollback_failed:
            raise PolicyViolation("CLI integration recovery-required; backup retained") from error
        raise
    receipt_body: dict[str, Any] = {
        "schema": "zekam-cli-integration-sync-receipt/v1",
        "receipt_id": receipt_key,
        "plan_digest": plan.plan_digest,
        "scope": plan.scope,
        "native_user_root_identity_digest": _root_identity(plan.native_user_root, must_exist=True),
        "project_root_identity_digest": (
            None
            if plan.project_root is None
            else _root_identity(plan.project_root, must_exist=True)
        ),
        "before_policy": plan.before_policy.body(),
        "after_policy": plan.after_policy.body(),
        "config_before_digest": plan.config_before_digest,
        "config_after_digest": (
            None if plan.config_after_bytes is None else digest_of_bytes(plan.config_after_bytes)
        ),
        "config_backup_ref": (
            None
            if config_backup is None
            else config_backup.relative_to(plan.context.home).as_posix()
        ),
        "native_config_backups": [
            {
                "relative_path": source.relative_to(plan.native_user_root).as_posix(),
                "backup_ref": (
                    None if backup is None else backup.relative_to(plan.context.home).as_posix()
                ),
                "before_digest": _file_digest(backup) if backup is not None else None,
            }
            for source, backup in native_config_backups
        ],
        "operations": list(plan.operations),
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    receipt = receipt_body | {"receipt_digest": digest(receipt_body)}
    _atomic_write(manifest_path, canonical_json(receipt).encode("utf-8"))
    return receipt


def sync_receipt(plan: IntegrationSyncPlan, *, authorized_plan_digest: str) -> dict[str, Any]:
    """Read and verify an immutable integration receipt plus its terminal state."""

    if authorized_plan_digest != plan.plan_digest:
        raise PolicyViolation("CLI integration exact receipt plan digest required")
    return sync_receipt_by_digest(
        plan.context,
        authorized_plan_digest=authorized_plan_digest,
        native_user_root=plan.native_user_root,
        project_root=plan.project_root,
    )


def sync_receipt_by_digest(
    context: ApplicationContext,
    *,
    authorized_plan_digest: str,
    native_user_root: Path,
    project_root: Path | None,
) -> dict[str, Any]:
    """Replay a completed receipt after policy state has already changed."""

    parse_digest(authorized_plan_digest)
    path = (
        context.home
        / "state"
        / "manifests"
        / "client-integrations"
        / f"{authorized_plan_digest.removeprefix('sha256:')}.json"
    )
    if not path.is_file() or path.is_symlink():
        raise PolicyViolation("CLI integration receipt missing")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("CLI integration receipt unreadable") from exc
    if not isinstance(document, dict):
        raise PolicyViolation("CLI integration receipt invalid")
    receipt_digest = document.pop("receipt_digest", None)
    if receipt_digest != digest(document) or document.get("plan_digest") != authorized_plan_digest:
        raise PolicyViolation("CLI integration receipt digest drift")
    document["receipt_digest"] = receipt_digest
    if document.get("native_user_root_identity_digest") != _root_identity(
        native_user_root, must_exist=True
    ):
        raise PolicyViolation("CLI integration receipt native root drift")
    expected_project = (
        None if project_root is None else _root_identity(project_root, must_exist=True)
    )
    if document.get("project_root_identity_digest") != expected_project:
        raise PolicyViolation("CLI integration receipt project root drift")
    expected_config = document.get("config_after_digest")
    if (
        expected_config is not None
        and _file_digest(context.home / USER_CONFIG_FILE) != expected_config
    ):
        raise PolicyViolation("CLI integration receipt config readback drift")
    quarantine_root = context.home / "quarantine" / "client-integrations"
    operations = document.get("operations")
    if not isinstance(operations, list):
        raise PolicyViolation("CLI integration receipt operations invalid")
    for operation in operations:
        if not isinstance(operation, dict):
            raise PolicyViolation("CLI integration receipt operation invalid")
        if operation["operation"] in {
            "detach-opencode-config",
            "detach-client-hook-config",
            "detach-client-instruction-config",
            "write-client-hook-config",
            "write-client-instruction-config",
        }:
            native_config = native_user_root / str(operation["relative_path"])
            if _file_digest(native_config) != operation["after_digest"]:
                raise PolicyViolation("CLI integration receipt native config drift")
            continue
        if operation["operation"] not in {
            "quarantine-managed-skill",
            "quarantine-managed-native-artifact",
        }:
            continue
        if operation["operation"] == "quarantine-managed-skill":
            if project_root is None:
                raise PolicyViolation("CLI integration receipt project root missing")
            source = project_root / str(operation["relative_path"])
        else:
            source = native_user_root / str(operation["relative_path"])
        destination = quarantine_root / str(operation["quarantine_relative_path"])
        if source.exists() or source.is_symlink():
            raise PolicyViolation("CLI integration receipt disabled artifact still active")
        if _artifact_digest(destination) != operation["before_digest"]:
            raise PolicyViolation("CLI integration receipt quarantine readback drift")
    return document


@dataclass(frozen=True, slots=True)
class IntegrationRollbackPlan:
    context: ApplicationContext
    receipt_id: str
    native_user_root: Path
    project_root: Path | None
    receipt: dict[str, Any]
    config_current_digest: str | None

    @property
    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-cli-integration-rollback-plan/v1",
            "receipt_id": self.receipt_id,
            "receipt_digest": self.receipt["receipt_digest"],
            "native_user_root_identity_digest": _root_identity(
                self.native_user_root, must_exist=True
            ),
            "project_root_identity_digest": (
                None
                if self.project_root is None
                else _root_identity(self.project_root, must_exist=True)
            ),
            "operations": list(reversed(self.receipt["operations"])),
            "config_current_digest": self.config_current_digest,
            "config_restore_digest": self.receipt["config_before_digest"],
            "apply": False,
            "provider_calls": 0,
            "network_calls": 0,
            "grants_authority": False,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.body)

    def as_dict(self) -> dict[str, Any]:
        return self.body | {"plan_digest": self.plan_digest}


def build_rollback_plan(
    context: ApplicationContext,
    *,
    receipt_id: str,
    native_user_root: Path,
    project_root: Path | None = None,
) -> IntegrationRollbackPlan:
    if not receipt_id or any(character not in "0123456789abcdef" for character in receipt_id):
        raise ValidationFailed("CLI integration rollback receipt id gecersiz")
    path = context.home / "state" / "manifests" / "client-integrations" / f"{receipt_id}.json"
    if not path.is_file() or path.is_symlink():
        raise ValidationFailed("CLI integration rollback receipt bulunamadi")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("CLI integration rollback receipt okunamadi") from exc
    if not isinstance(receipt, dict):
        raise ValidationFailed("CLI integration rollback receipt gecersiz")
    stored_digest = receipt.pop("receipt_digest", None)
    if stored_digest != digest(receipt) or receipt.get("receipt_id") != receipt_id:
        raise PolicyViolation("CLI integration rollback receipt digest drift")
    receipt["receipt_digest"] = stored_digest
    rollback_path = (
        context.home / "state" / "manifests" / "client-integration-rollbacks" / f"{receipt_id}.json"
    )
    completed: dict[str, Any] | None = None
    if rollback_path.is_file() and not rollback_path.is_symlink():
        try:
            loaded = json.loads(rollback_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PolicyViolation("CLI integration rollback receipt unreadable") from exc
        if not isinstance(loaded, dict):
            raise PolicyViolation("CLI integration rollback receipt invalid")
        completed_digest = loaded.pop("receipt_digest", None)
        if completed_digest != digest(loaded) or loaded.get("source_receipt_id") != receipt_id:
            raise PolicyViolation("CLI integration rollback receipt digest drift")
        loaded["receipt_digest"] = completed_digest
        completed = loaded
    if receipt.get("scope") == "project" and project_root is None:
        raise PolicyViolation("Project integration rollback exact project root ister")
    if receipt.get("scope") == "user" and project_root is not None:
        raise PolicyViolation("User integration rollback project root kabul etmez")
    if completed is None:
        expected_after = receipt.get("config_after_digest")
        if (
            expected_after is not None
            and _file_digest(context.home / USER_CONFIG_FILE) != expected_after
        ):
            raise PolicyViolation("CLI integration rollback user config drift")
        quarantine_root = context.home / "quarantine" / "client-integrations"
        for operation in receipt.get("operations", []):
            if operation.get("operation") in {
                "detach-opencode-config",
                "detach-client-hook-config",
                "detach-client-instruction-config",
                "write-client-hook-config",
                "write-client-instruction-config",
            }:
                native_config = native_user_root / str(operation["relative_path"])
                if _file_digest(native_config) != operation["after_digest"]:
                    raise PolicyViolation("CLI integration rollback native config drift")
                continue
            if operation.get("operation") not in {
                "quarantine-managed-skill",
                "quarantine-managed-native-artifact",
            }:
                continue
            if operation.get("operation") == "quarantine-managed-skill":
                assert project_root is not None
                source = project_root / str(operation["relative_path"])
            else:
                source = native_user_root / str(operation["relative_path"])
            destination = quarantine_root / str(operation["quarantine_relative_path"])
            if source.exists() or source.is_symlink():
                raise PolicyViolation("CLI integration rollback source user drift")
            if _artifact_digest(destination) != operation["before_digest"]:
                raise PolicyViolation("CLI integration rollback quarantine drift")
        config_current_digest = _file_digest(context.home / USER_CONFIG_FILE)
    else:
        config_current_digest = completed.get("config_current_digest")
    return IntegrationRollbackPlan(
        context=context,
        receipt_id=receipt_id,
        native_user_root=native_user_root.resolve(strict=True),
        project_root=None if project_root is None else project_root.resolve(strict=True),
        receipt=receipt,
        config_current_digest=config_current_digest,
    )


def apply_rollback_plan(
    plan: IntegrationRollbackPlan, *, authorized_plan_digest: str
) -> dict[str, Any]:
    if authorized_plan_digest != plan.plan_digest:
        raise PolicyViolation("CLI integration exact rollback plan digest required")
    terminal_path = (
        plan.context.home
        / "state"
        / "manifests"
        / "client-integration-rollbacks"
        / f"{plan.receipt_id}.json"
    )
    if terminal_path.is_file() and not terminal_path.is_symlink():
        return rollback_receipt(plan, authorized_plan_digest=authorized_plan_digest)
    fresh = build_rollback_plan(
        plan.context,
        receipt_id=plan.receipt_id,
        native_user_root=plan.native_user_root,
        project_root=plan.project_root,
    )
    if fresh.plan_digest != plan.plan_digest:
        raise PolicyViolation("CLI integration rollback target changed before apply")
    quarantine_root = plan.context.home / "quarantine" / "client-integrations"
    restored: list[tuple[Path, Path, str]] = []
    restored_config_after: bytes | None = None
    restored_native_configs_after: list[tuple[Path, bytes]] = []
    try:
        for operation in reversed(plan.receipt.get("operations", [])):
            if operation.get("operation") not in {
                "quarantine-managed-skill",
                "quarantine-managed-native-artifact",
            }:
                continue
            if operation.get("operation") == "quarantine-managed-skill":
                if plan.project_root is None:
                    raise PolicyViolation("CLI integration rollback project root missing")
                source = plan.project_root / str(operation["relative_path"])
            else:
                source = plan.native_user_root / str(operation["relative_path"])
            quarantine = quarantine_root / str(operation["quarantine_relative_path"])
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(quarantine, source)
            restored.append((source, quarantine, str(operation["before_digest"])))
            if _artifact_digest(source) != operation["before_digest"]:
                raise PolicyViolation("CLI integration rollback artifact readback drift")
        native_backups = plan.receipt.get("native_config_backups", [])
        if not isinstance(native_backups, list):
            raise PolicyViolation("CLI integration rollback native backup list invalid")
        for backup_row in native_backups:
            if not isinstance(backup_row, dict):
                raise PolicyViolation("CLI integration rollback native backup invalid")
            relative = str(backup_row.get("relative_path"))
            native_config = plan.native_user_root / relative
            detach = next(
                (
                    item
                    for item in plan.receipt.get("operations", [])
                    if item.get("operation")
                    in {
                        "detach-opencode-config",
                        "detach-client-hook-config",
                        "detach-client-instruction-config",
                        "write-client-hook-config",
                        "write-client-instruction-config",
                    }
                    and item.get("relative_path") == relative
                ),
                None,
            )
            if detach is None or _file_digest(native_config) != detach["after_digest"]:
                raise PolicyViolation("CLI integration rollback native config source drift")
            raw_backup_ref = backup_row.get("backup_ref")
            native_backup = (
                None if raw_backup_ref is None else plan.context.home / str(raw_backup_ref)
            )
            if native_backup is None:
                if detach["before_digest"] is not None:
                    raise PolicyViolation("CLI integration rollback native config backup missing")
            elif _file_digest(native_backup) != detach["before_digest"]:
                raise PolicyViolation("CLI integration rollback native config backup drift")
            restored_native_configs_after.append((native_config, native_config.read_bytes()))
            if native_backup is None:
                native_config.unlink()
            else:
                _atomic_write(native_config, native_backup.read_bytes())
            if _file_digest(native_config) != detach["before_digest"]:
                raise PolicyViolation("CLI integration rollback native config readback drift")
        backup_ref = plan.receipt.get("config_backup_ref")
        expected_before = plan.receipt.get("config_before_digest")
        if plan.receipt.get("config_after_digest") is not None:
            config_path = plan.context.home / USER_CONFIG_FILE
            restored_config_after = config_path.read_bytes()
            if backup_ref is None:
                if expected_before is not None:
                    raise PolicyViolation("CLI integration rollback config backup missing")
                config_path.unlink()
            else:
                backup = plan.context.home / str(backup_ref)
                if _file_digest(backup) != expected_before:
                    raise PolicyViolation("CLI integration rollback config backup drift")
                _atomic_write(config_path, backup.read_bytes())
            if _file_digest(config_path) != expected_before:
                raise PolicyViolation("CLI integration rollback config readback drift")
    except BaseException as error:
        failed = False
        for native_config, native_after in reversed(restored_native_configs_after):
            try:
                _atomic_write(native_config, native_after)
            except OSError:
                failed = True
        if restored_config_after is not None:
            try:
                _atomic_write(plan.context.home / USER_CONFIG_FILE, restored_config_after)
            except OSError:
                failed = True
        for source, quarantine, expected in reversed(restored):
            try:
                if _artifact_digest(source) != expected or quarantine.exists():
                    raise OSError("rollback compensation drift")
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, quarantine)
            except (OSError, PolicyViolation):
                failed = True
        if failed:
            raise PolicyViolation("CLI integration rollback recovery-required") from error
        raise
    body: dict[str, Any] = {
        "schema": "zekam-cli-integration-rollback-receipt/v1",
        "source_receipt_id": plan.receipt_id,
        "plan_digest": plan.plan_digest,
        "config_current_digest": plan.config_current_digest,
        "state": "rolled-back-and-read-back",
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
    receipt = body | {"receipt_digest": digest(body)}
    _atomic_write(terminal_path, canonical_json(receipt).encode("utf-8"))
    return receipt


def rollback_receipt(
    plan: IntegrationRollbackPlan, *, authorized_plan_digest: str
) -> dict[str, Any]:
    """Verify a persisted rollback receipt and the restored terminal state."""

    if authorized_plan_digest != plan.plan_digest:
        raise PolicyViolation("CLI integration exact rollback receipt digest required")
    path = (
        plan.context.home
        / "state"
        / "manifests"
        / "client-integration-rollbacks"
        / f"{plan.receipt_id}.json"
    )
    if not path.is_file() or path.is_symlink():
        raise PolicyViolation("CLI integration rollback receipt missing")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("CLI integration rollback receipt unreadable") from exc
    if not isinstance(document, dict):
        raise PolicyViolation("CLI integration rollback receipt invalid")
    stored = document.pop("receipt_digest", None)
    if stored != digest(document) or document.get("plan_digest") != plan.plan_digest:
        raise PolicyViolation("CLI integration rollback receipt digest drift")
    document["receipt_digest"] = stored
    expected_config = plan.receipt.get("config_before_digest")
    if (
        plan.receipt.get("config_after_digest") is not None
        and _file_digest(plan.context.home / USER_CONFIG_FILE) != expected_config
    ):
        raise PolicyViolation("CLI integration rollback config terminal drift")
    quarantine_root = plan.context.home / "quarantine" / "client-integrations"
    for operation in plan.receipt.get("operations", []):
        if operation.get("operation") in {
            "detach-opencode-config",
            "detach-client-hook-config",
            "detach-client-instruction-config",
            "write-client-hook-config",
            "write-client-instruction-config",
        }:
            native_config = plan.native_user_root / str(operation["relative_path"])
            if _file_digest(native_config) != operation["before_digest"]:
                raise PolicyViolation("CLI integration rollback native config terminal drift")
            continue
        if operation.get("operation") not in {
            "quarantine-managed-skill",
            "quarantine-managed-native-artifact",
        }:
            continue
        if operation.get("operation") == "quarantine-managed-skill":
            if plan.project_root is None:
                raise PolicyViolation("CLI integration rollback project root missing")
            source = plan.project_root / str(operation["relative_path"])
        else:
            source = plan.native_user_root / str(operation["relative_path"])
        quarantine = quarantine_root / str(operation["quarantine_relative_path"])
        if _artifact_digest(source) != operation["before_digest"]:
            raise PolicyViolation("CLI integration rollback artifact terminal drift")
        if quarantine.exists() or quarantine.is_symlink():
            raise PolicyViolation("CLI integration rollback quarantine still active")
    return document
