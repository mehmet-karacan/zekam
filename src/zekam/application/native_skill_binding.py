"""Resolve a native skill load to an active, receipt-backed managed package."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from zekam.application.bounded_file import read_bounded_bytes
from zekam.application.composition import build_context
from zekam.application.project_rag_runtime import resolve_project_source
from zekam.application.skill_evidence import _constant, _pairs
from zekam.application.skill_packages import (
    _assert_contained,
    _iter_projected_files,
    projected_artifact_digest,
)
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes, parse_digest
from zekam.domain.client_integration import ClientIntegrationPolicy
from zekam.domain.errors import PolicyViolation
from zekam.domain.personal_skill import SkillScopeKind
from zekam.domain.skill_package import MAX_FILE_BYTES, MAX_FILES, MAX_PACKAGE_BYTES, SkillPackage

if TYPE_CHECKING:
    from zekam.infrastructure.sqlite.skill_lifecycle import SQLiteSkillLifecycle

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def resolve_native_skill_binding(
    lifecycle: SQLiteSkillLifecycle,
    *,
    home: Path,
    project_root: Path,
    policy: ClientIntegrationPolicy,
    allowed_scopes: tuple[tuple[SkillScopeKind, str], ...],
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Read-only resolution. It neither mints Work nor records a verified outcome."""
    if not policy.opencode:
        raise PolicyViolation("Native OpenCode skill binding requires enabled integration")
    context = build_context(home=home)
    if lifecycle.operational_path.resolve(strict=True) != context.settings.database.sqlite_path(
        context.home
    ).resolve(strict=True):
        raise PolicyViolation("Native skill binding home/store identity mismatch")
    if len(allowed_scopes) != 1 or allowed_scopes[0][0] is not SkillScopeKind.PROJECT:
        raise PolicyViolation("Native skill binding requires one exact project scope")
    scope_ref = lifecycle.canonical_project_scope(allowed_scopes[0][1])
    if project_root.resolve(strict=True) != resolve_project_source(home, scope_ref):
        raise PolicyViolation("Native skill binding registered source root mismatch")
    if (
        not isinstance(observation, dict)
        or set(observation) != {"schema", "skill_id", "relative_path", "instruction_digest"}
        or observation["schema"] != "zekam-native-skill-load-query/v1"
    ):
        raise PolicyViolation("Native skill exact observation query required")
    name, relative, observed_body = (
        observation["skill_id"],
        observation["relative_path"],
        observation["instruction_digest"],
    )
    if (
        type(name) is not str
        or len(name) > 64
        or _NAME.fullmatch(name) is None
        or type(relative) is not str
        or type(observed_body) is not str
    ):
        raise PolicyViolation("Native skill bounded identity required")
    parse_digest(observed_body)
    part = PurePosixPath(relative)
    if (
        relative != part.as_posix()
        or part.is_absolute()
        or "\\" in relative
        or len(part.parts) != 4
        or part.parts[0] not in {".agents", ".claude", ".opencode"}
        or part.parts[1:] != ("skills", name, "SKILL.md")
    ):
        raise PolicyViolation("Native skill must name one exact managed project path")
    root = project_root.resolve(strict=True)
    target = root / Path(*part.parts[:-1])
    _assert_contained(root, target / "SKILL.md")
    ownership = target / ".zekam-managed.json"
    raw_ownership = read_bounded_bytes(ownership, max_bytes=16_384)
    meta = json.loads(
        raw_ownership.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
    )
    if (
        not isinstance(meta, dict)
        or set(meta)
        != {
            "schema",
            "package_digest",
            "semantic_digest",
            "artifact_digest",
            "relative_path",
            "clients",
            "support_level",
            "integration_policy_digest",
            "grants_authority",
        }
        or meta["schema"] != "zekam-managed-skill-projection/v2"
        or meta["relative_path"] != "/".join(part.parts[:-1])
        or meta["grants_authority"] is not False
        or meta["support_level"] != "instruction-distribution-only"
        or meta["integration_policy_digest"] != policy.policy_digest
    ):
        raise PolicyViolation("Native skill managed ownership/policy drift")
    owner = {".agents": "codex", ".claude": "claude-code", ".opencode": "opencode"}[part.parts[0]]
    if meta["clients"] != [owner] or not policy.projection_enabled(owner):
        raise PolicyViolation("Native skill projection owner is disabled or ambiguous")
    for key in ("package_digest", "semantic_digest", "artifact_digest"):
        parse_digest(meta[key])
    if projected_artifact_digest(target) != meta["artifact_digest"]:
        raise PolicyViolation("Native skill projected artifact drift")
    files = {}
    total = 0
    for path in _iter_projected_files(target):
        key = path.relative_to(target).as_posix()
        if key == ".zekam-managed.json":
            continue
        payload = read_bounded_bytes(path, max_bytes=MAX_FILE_BYTES)
        total += len(payload)
        files[key] = payload
        if len(files) > MAX_FILES or total > MAX_PACKAGE_BYTES:
            raise PolicyViolation("Native skill package exceeds bounded read budget")
    package = SkillPackage.parse(name, files)
    if (
        package.declared_allowed_tools is not None
        or package.semantic_digest != meta["semantic_digest"]
        or digest_of_bytes(package.instructions.strip().encode("utf-8")) != observed_body
    ):
        raise PolicyViolation("Native effective skill instructions differ from managed package")
    activation = lifecycle.require_active_package(
        meta["package_digest"], allowed_scopes=allowed_scopes
    )
    if activation["scope_ref"] != scope_ref:
        raise PolicyViolation("Native skill binding activation scope mismatch")
    root_digest = digest(os.path.normcase(str(root)))
    # Filter project root/package/policy/activation and target before LIMIT; a
    # foreign project's recent exports must never crowd out this exact source.
    with closing(
        sqlite3.connect(
            f"{lifecycle.operational_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
        )
    ) as db:
        rows = db.execute(
            "select j.id,j.payload_json,j.terminal_evidence_digest from local_job j "
            "where j.state='completed' and json_extract(j.payload_json,'$.operation')='skill.export-v1' "
            "and json_extract(j.payload_json,'$.effect.plan.source_root_identity_digest')=? "
            "and json_extract(j.payload_json,'$.effect.plan.package_digest')=? "
            "and json_extract(j.payload_json,'$.effect.plan.integration_policy_digest')=? "
            "and json_extract(j.payload_json,'$.effect.active_revision.activation_event_digest')=? "
            "and exists(select 1 from json_each(j.payload_json,'$.effect.plan.targets') t "
            "where json_extract(t.value,'$.relative_path')=? and json_extract(t.value,'$.enabled')=1 "
            "and json_extract(t.value,'$.artifact_digest')=?) "
            "order by j.created_at desc,j.id desc limit 1",
            (
                root_digest,
                meta["package_digest"],
                policy.policy_digest,
                activation["activation_event_digest"],
                meta["relative_path"],
                meta["artifact_digest"],
            ),
        ).fetchall()
    if len(rows) != 1 or len(str(rows[0][1]).encode("utf-8")) > 64_000:
        raise PolicyViolation("Native skill terminal export source required")
    job_id, raw, evidence = rows[0]
    effect = json.loads(str(raw), object_pairs_hook=_pairs, parse_constant=_constant)["effect"]
    export_plan = effect["plan"]
    if (
        canonical_json(effect["active_revision"]) != canonical_json(activation)
        or digest({k: v for k, v in export_plan.items() if k != "plan_digest"})
        != export_plan["plan_digest"]
    ):
        raise PolicyViolation("Native skill export plan/active revision binding drift")
    lifecycle._completed_terminal_evidence(
        str(job_id), str(evidence), operation="skill.export-v1", effect_digest=digest(effect)
    )
    if (
        lifecycle.require_active_package(meta["package_digest"], allowed_scopes=allowed_scopes)
        != activation
        or read_bounded_bytes(ownership, max_bytes=16_384) != raw_ownership
        or projected_artifact_digest(target) != meta["artifact_digest"]
    ):
        raise PolicyViolation("Native skill source changed during observation binding")
    body = {
        "schema": "zekam-native-skill-binding/v1",
        "client_id": "opencode",
        "skill_id": name,
        "relative_path": relative,
        "revision_digest": activation["revision_digest"],
        "package_digest": meta["package_digest"],
        "activation_event_digest": activation["activation_event_digest"],
        "instruction_digest": observed_body,
        "export_job_id": str(job_id),
        "export_evidence_digest": str(evidence),
        "invocation_observed": False,
        "quality_verified": False,
        "provider_calls": 0,
        "grants_authority": False,
    }
    return body | {"binding_digest": digest(body)}
