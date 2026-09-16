"""Provider-free CLI integration migration, conflict and rollback gates."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import zekam.application.client_integrations as client_integrations
from zekam.application.client_hook_bootstrap import (
    apply_client_hook_bootstrap,
    plan_client_hook_bootstrap,
)
from zekam.application.client_integrations import (
    apply_rollback_plan,
    apply_sync_plan,
    build_rollback_plan,
    build_sync_plan,
    integration_mutation_resource,
    integration_status,
)
from zekam.application.composition import ApplicationContext, build_context
from zekam.application.opencode_agent_bootstrap import (
    apply_opencode_agent_bootstrap,
    plan_opencode_agent_bootstrap,
)
from zekam.application.skill_packages import (
    apply_projection_plan,
    build_projection_plan,
    projected_artifact_digest,
)
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.client_integration import ClientIntegrationPolicy
from zekam.domain.errors import PolicyViolation
from zekam.domain.personal_skill import PersonalSkillRevision, SkillScopeKind
from zekam.domain.skill_package import SkillPackage

pytestmark = pytest.mark.integration


def _context(tmp_path: Path) -> ApplicationContext:
    home = tmp_path / "zekam-home"
    home.mkdir()
    return build_context(home=home, environ={})


def _native_home(tmp_path: Path) -> Path:
    home = tmp_path / "native-user"
    home.mkdir()
    return home


def _skill() -> SkillPackage:
    return SkillPackage.parse(
        "zekam-deneme",
        {
            "SKILL.md": (
                b"---\nname: zekam-deneme\n"
                b"description: Managed projection acceptance fixture.\n---\n"
                b"Fail closed.\n"
            )
        },
    )


def _trust_package(
    context: ApplicationContext,
    package: SkillPackage,
    project: Path,
) -> None:
    database = context.home / "state" / "learning.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    revision = PersonalSkillRevision(
        skill_id=package.name,
        name=package.name,
        description=package.description,
        version=1,
        scope_kind=SkillScopeKind.PROJECT,
        scope_ref="project",
        package_digest=package.package_digest,
        author_ref="test-builder",
        trigger_terms=("test",),
    )
    assert "semantic_digest" not in revision.body()
    assert "projection_artifact_digest" not in revision.body()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "create table skill_revision_v2("
            "revision_digest text not null,skill_id text not null,name text not null,"
            "package_digest text not null,body_json text not null)"
        )
        connection.execute(
            "insert into skill_revision_v2 values(?,?,?,?,?)",
            (
                revision.revision_digest,
                revision.skill_id,
                revision.name,
                revision.package_digest,
                json.dumps(
                    revision.body(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
    artifact_digest = digest(
        [
            {"path": path, "digest": digest_of_bytes(payload), "size": len(payload)}
            for path, payload in sorted(package.safe_projection_files().items())
        ]
    )
    targets = [
        {
            "relative_path": f".agents/skills/{package.name}",
            "clients": ["codex", "opencode"],
            "support_level": "instruction-distribution-only",
            "artifact_digest": artifact_digest,
            "observed_artifact_digest": None,
            "observed_package_digest": None,
            "observed_semantic_digest": None,
            "state": "new",
            "reload": "automatic-or-restart-if-not-visible",
        },
        {
            "relative_path": f".claude/skills/{package.name}",
            "clients": ["claude-code"],
            "support_level": "instruction-distribution-only",
            "artifact_digest": artifact_digest,
            "observed_artifact_digest": None,
            "observed_package_digest": None,
            "observed_semantic_digest": None,
            "state": "new",
            "reload": "live-watch-or-restart-if-directory-was-missing",
        },
    ]
    plan_body = {
        "schema": "zekam-skill-client-projection-plan/v1",
        "source_root_identity_digest": digest(os.path.normcase(str(project.resolve()))),
        "package_digest": package.package_digest,
        "semantic_digest": package.semantic_digest,
        "skill_name": package.name,
        "targets": targets,
        "declared_allowed_tools_removed": package.declared_allowed_tools is not None,
        "provider_calls": 0,
        "network_calls": 0,
        "apply": False,
        "grants_authority": False,
    }
    plan = plan_body | {"plan_digest": digest(plan_body)}
    active_revision = {
        "revision_digest": revision.revision_digest,
        "skill_id": revision.skill_id,
        "scope_kind": "project",
        "scope_ref": "project",
        "activation_event_digest": digest("activation"),
    }
    current_plan = json.loads(
        canonical_json(
            build_projection_plan(
                project,
                package,
                policy=ClientIntegrationPolicy(
                    opencode=True,
                    codex=True,
                    claude_code=True,
                ),
            ).as_dict()
        )
    )
    operational = context.settings.database.sqlite_path(context.home)
    operational.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(operational) as connection:
        connection.execute(
            "create table local_job("
            "id text not null,payload_json text not null,state text not null,"
            "terminal_evidence_digest text not null)"
        )
        connection.execute(
            "create table local_effect_claim("
            "id text not null,job_id text not null,operation text not null,"
            "effect_digest text not null)"
        )
        connection.execute(
            "create table local_effect_receipt("
            "claim_id text not null,status text not null,evidence_digest text not null)"
        )
        for index, export_plan in enumerate((plan, current_plan), start=1):
            effect = {
                "schema": "zekam-skill-export-effect/v1",
                "plan": export_plan,
                "active_revision": active_revision,
            }
            terminal = client_integrations._export_receipt_digest(export_plan)
            payload = canonical_json({"operation": "skill.export-v1", "effect": effect})
            job_id = f"export-job-{index}"
            claim_id = f"export-claim-{index}"
            connection.execute(
                "insert into local_job values(?,?,?,?)",
                (job_id, payload, "completed", terminal),
            )
            connection.execute(
                "insert into local_effect_claim values(?,?,?,?)",
                (claim_id, job_id, "skill.export-v1", digest(effect)),
            )
            connection.execute(
                "insert into local_effect_receipt values(?,?,?)",
                (claim_id, "completed", terminal),
            )


def test_user_opencode_disable_quarantines_exact_artifacts_and_rolls_back(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    native = _native_home(tmp_path)
    executable = tmp_path / "opencode.exe"
    executable.write_bytes(b"stub")
    config = native / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"plugin": ["third-party.js"], "provider": {"safe": {"timeout": 30}}}),
        encoding="utf-8",
    )
    bootstrap = plan_opencode_agent_bootstrap(
        executable=executable,
        user_home=native,
    )
    apply_opencode_agent_bootstrap(bootstrap, authorized_plan_digest=bootstrap.plan_digest)

    plan = build_sync_plan(
        context,
        scope="user",
        native_user_root=native,
        disable=("opencode",),
    )
    before = config.read_bytes()
    assert plan.conflicts == ()
    assert any(item["operation"] == "detach-opencode-config" for item in plan.operations)
    receipt = apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert receipt["provider_calls"] == 0
    assert receipt["network_calls"] == 0
    assert not (native / ".config" / "opencode" / "plugins" / "zekam-lifecycle.js").exists()
    stored = json.loads(config.read_text(encoding="utf-8"))
    assert stored["plugin"] == ["third-party.js"]
    assert stored["provider"] == {"safe": {"timeout": 30}}
    assert "default_agent" not in stored
    assert build_context(home=context.home, environ={}).settings.cli.integrations.opencode is False
    assert apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest) == receipt

    rollback = build_rollback_plan(
        build_context(home=context.home, environ={}),
        receipt_id=str(receipt["receipt_id"]),
        native_user_root=native,
    )
    rolled_back = apply_rollback_plan(
        rollback,
        authorized_plan_digest=rollback.plan_digest,
    )
    assert rolled_back["state"] == "rolled-back-and-read-back"
    assert config.read_bytes() == before
    assert (native / ".config" / "opencode" / "plugins" / "zekam-lifecycle.js").is_file()
    assert build_context(home=context.home, environ={}).settings.cli.integrations.opencode is True
    replay = build_rollback_plan(
        build_context(home=context.home, environ={}),
        receipt_id=str(receipt["receipt_id"]),
        native_user_root=native,
    )
    assert apply_rollback_plan(replay, authorized_plan_digest=replay.plan_digest) == rolled_back


def test_project_legacy_shared_projection_requires_replacement_then_quarantines(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    native = _native_home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    package = _skill()
    _trust_package(context, package, project)
    initial = build_projection_plan(
        project,
        package,
        policy=ClientIntegrationPolicy(opencode=True, codex=True, claude_code=True),
    )
    apply_projection_plan(initial, authorized_plan_digest=initial.plan_digest)
    marker = project / ".agents" / "skills" / package.name / ".zekam-managed.json"
    legacy = json.loads(marker.read_text(encoding="utf-8"))
    legacy["schema"] = "zekam-managed-skill-projection/v1"
    legacy["clients"] = ["codex", "opencode"]
    legacy.pop("integration_policy_digest", None)
    marker.write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")

    plan = build_sync_plan(
        context,
        scope="project",
        native_user_root=native,
        project_root=project,
    )
    assert plan.conflicts == ()
    assert {item["client"] for item in plan.operations} == {"codex", "claude-code"}
    receipt = apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert (project / ".opencode" / "skills" / package.name).is_dir()
    assert not (project / ".agents" / "skills" / package.name).exists()
    assert not (project / ".claude" / "skills" / package.name).exists()

    rollback = build_rollback_plan(
        context,
        receipt_id=str(receipt["receipt_id"]),
        native_user_root=native,
        project_root=project,
    )
    apply_rollback_plan(rollback, authorized_plan_digest=rollback.plan_digest)
    assert (project / ".agents" / "skills" / package.name).is_dir()
    assert (project / ".claude" / "skills" / package.name).is_dir()


def test_drifted_native_artifact_is_conflict_and_dry_run_is_read_only(tmp_path: Path) -> None:
    context = _context(tmp_path)
    native = _native_home(tmp_path)
    agent = native / ".config" / "opencode" / "agents" / "zekam-coordinator.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("user-owned content\n", encoding="utf-8")
    before = agent.read_bytes()

    status = integration_status(context, native_user_root=native)
    opencode = next(item for item in status["clients"] if item["client"] == "opencode")
    assert opencode["effective_state"] == "blocked-conflict"
    plan = build_sync_plan(
        context,
        scope="user",
        native_user_root=native,
        disable=("opencode",),
    )
    assert plan.conflicts
    assert agent.read_bytes() == before
    assert not (context.home / "quarantine").exists()
    with pytest.raises(PolicyViolation, match="conflict"):
        apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    assert agent.read_bytes() == before
    assert not (context.home / "quarantine").exists()


def test_self_consistent_marker_without_canonical_package_is_not_owned(tmp_path: Path) -> None:
    context = _context(tmp_path)
    native = _native_home(tmp_path)
    project = tmp_path / "project"
    skill = project / ".agents" / "skills" / "user-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("user content\n", encoding="utf-8")
    marker = {
        "schema": "zekam-managed-skill-projection/v2",
        "package_digest": digest("not-canonical-package"),
        "semantic_digest": digest("not-canonical-semantic"),
        "artifact_digest": projected_artifact_digest(skill),
        "relative_path": ".agents/skills/user-skill",
        "clients": ["codex"],
        "support_level": "instruction-distribution-only",
        "integration_policy_digest": ClientIntegrationPolicy().policy_digest,
        "grants_authority": False,
    }
    (skill / ".zekam-managed.json").write_text(json.dumps(marker), encoding="utf-8")

    plan = build_sync_plan(
        context,
        scope="project",
        native_user_root=native,
        project_root=project,
    )

    assert plan.operations == ()
    assert plan.conflicts == (
        {
            "relative_path": ".agents/skills/user-skill",
            "reason": "canonical-package-ownership-proof-missing",
        },
    )
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "user content\n"

    user_plan = build_sync_plan(
        context,
        scope="user",
        native_user_root=native,
        registered_projects=(("project-one", project),),
    )
    assert user_plan.pending_project_cleanup == (
        {
            "project_ref": "project-one",
            "project_root_identity_digest": digest(os.path.normcase(str(project.resolve()))),
            "managed_cleanup_count": 0,
            "conflict_count": 1,
            "requires_separate_project_plan": True,
        },
    )


def test_real_package_digest_reused_by_arbitrary_marker_is_not_owned(tmp_path: Path) -> None:
    context = _context(tmp_path)
    native = _native_home(tmp_path)
    package = _skill()
    project = tmp_path / "project"
    project.mkdir()
    _trust_package(context, package, project)
    skill = project / ".agents" / "skills" / package.name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("arbitrary user content\n", encoding="utf-8")
    marker = {
        "schema": "zekam-managed-skill-projection/v2",
        "package_digest": package.package_digest,
        "semantic_digest": package.semantic_digest,
        "artifact_digest": projected_artifact_digest(skill),
        "relative_path": f".agents/skills/{package.name}",
        "clients": ["codex"],
        "support_level": "instruction-distribution-only",
        "integration_policy_digest": ClientIntegrationPolicy().policy_digest,
        "grants_authority": False,
    }
    (skill / ".zekam-managed.json").write_text(json.dumps(marker), encoding="utf-8")

    plan = build_sync_plan(
        context,
        scope="project",
        native_user_root=native,
        project_root=project,
    )

    assert plan.operations == ()
    assert plan.conflicts == (
        {
            "relative_path": f".agents/skills/{package.name}",
            "reason": "canonical-package-projection-binding-mismatch",
        },
    )


def test_project_cleanup_and_skill_export_use_the_same_single_writer_resource(
    tmp_path: Path,
) -> None:
    native = _native_home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    cleanup_resource = integration_mutation_resource(
        scope="project",
        native_user_root=native,
        project_root=project,
    )
    export_resource = integration_mutation_resource(
        scope="project",
        native_user_root=native,
        project_root=project,
    )

    assert cleanup_resource == export_resource
    assert cleanup_resource == "client-integrations:project:" + digest(
        os.path.normcase(str(project.resolve()))
    )


def test_disabled_codex_and_claude_exact_hook_groups_are_detached_and_restored(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    native = _native_home(tmp_path)
    python_executable = tmp_path / "python.exe"
    python_executable.write_bytes(b"stub")
    policy = ClientIntegrationPolicy(opencode=True, codex=True, claude_code=True)
    hooks = plan_client_hook_bootstrap(
        user_home=native,
        python_executable=python_executable,
        policy=policy,
    )
    apply_client_hook_bootstrap(hooks)
    codex_path = native / ".codex" / "hooks.json"
    claude_path = native / ".claude" / "settings.json"
    codex_document = json.loads(codex_path.read_text(encoding="utf-8"))
    codex_document["user-setting"] = "preserved"
    codex_path.write_text(json.dumps(codex_document), encoding="utf-8")
    before = {codex_path: codex_path.read_bytes(), claude_path: claude_path.read_bytes()}

    plan = build_sync_plan(
        context,
        scope="user",
        native_user_root=native,
    )
    assert plan.conflicts == ()
    assert {
        item["client"]
        for item in plan.operations
        if item["operation"] == "detach-client-hook-config"
    } == {"codex", "claude-code"}
    receipt = apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)
    cleaned = json.loads(codex_path.read_text(encoding="utf-8"))
    assert cleaned["user-setting"] == "preserved"
    assert all(cleaned["hooks"][event] == [] for event in cleaned["hooks"])

    rollback = build_rollback_plan(
        context,
        receipt_id=str(receipt["receipt_id"]),
        native_user_root=native,
    )
    apply_rollback_plan(rollback, authorized_plan_digest=rollback.plan_digest)
    assert codex_path.read_bytes() == before[codex_path]
    assert claude_path.read_bytes() == before[claude_path]


def test_codex_opt_in_creates_only_enabled_managed_files_then_detaches_them(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    native = _native_home(tmp_path)
    agents = native / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text("# Kullanici kurali\n", encoding="utf-8")

    enable = build_sync_plan(
        context,
        scope="user",
        native_user_root=native,
        enable=("codex",),
    )
    assert enable.conflicts == ()
    assert any(
        row["operation"] == "write-client-hook-config" and row["client"] == "codex"
        for row in enable.operations
    )
    assert not any(row.get("client") == "claude-code" for row in enable.operations)
    apply_sync_plan(enable, authorized_plan_digest=enable.plan_digest)
    assert "zekam-managed-client-instructions/v1:start" in agents.read_text(encoding="utf-8")
    assert (native / ".codex" / "hooks.json").is_file()
    assert not (native / ".claude").exists()

    disable = build_sync_plan(
        build_context(home=context.home, environ={}),
        scope="user",
        native_user_root=native,
        disable=("codex",),
    )
    assert disable.conflicts == ()
    assert {row["operation"] for row in disable.operations if row.get("client") == "codex"} == {
        "detach-client-hook-config",
        "detach-client-instruction-config",
    }
    apply_sync_plan(disable, authorized_plan_digest=disable.plan_digest)
    assert agents.read_text(encoding="utf-8") == "# Kullanici kurali\n"
    hooks = json.loads((native / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert all(groups == [] for groups in hooks["hooks"].values())


def test_user_sync_compensates_native_writes_when_policy_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    native = _native_home(tmp_path)
    agents = native / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    original = b"# Kullanici kurali\n"
    agents.write_bytes(original)
    plan = build_sync_plan(
        context,
        scope="user",
        native_user_root=native,
        enable=("codex",),
    )
    real_atomic_write = client_integrations._atomic_write

    def fail_policy_publish(path: Path, payload: bytes) -> None:
        if path == context.home / "config.yaml":
            raise OSError("injected policy publish failure")
        real_atomic_write(path, payload)

    monkeypatch.setattr(client_integrations, "_atomic_write", fail_policy_publish)
    with pytest.raises(OSError, match="injected"):
        apply_sync_plan(plan, authorized_plan_digest=plan.plan_digest)

    assert agents.read_bytes() == original
    assert not (native / ".codex" / "hooks.json").exists()
    assert not (native / ".config" / "opencode" / "AGENTS.md").exists()
    assert not (context.home / "config.yaml").exists()
