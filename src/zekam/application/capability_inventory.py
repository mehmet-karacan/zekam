"""Reviewed, model-independent inventory of the current Zekam product surface."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from zekam.domain.canonical import digest

CAPABILITY_SCHEMA = "zekam-capability-inventory/v1"
CAPABILITY_READINESS_SCHEMA = "zekam-capability-readiness/v1"
CapabilityStatus = Literal["ready", "partial", "scaffold"]
CapabilityReadinessStatus = Literal["ready", "partial", "blocked"]
CapabilityEvidenceState = Literal["observed", "unobserved", "partial", "missing"]


@dataclass(frozen=True, slots=True)
class Capability:
    capability_id: str
    title: str
    status: CapabilityStatus
    surfaces: tuple[str, ...]
    agent_roles: tuple[str, ...]
    verified_by: tuple[str, ...]
    gap: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "title": self.title,
            "status": self.status,
            "surfaces": list(self.surfaces),
            "agent_roles": list(self.agent_roles),
            "verified_by": list(self.verified_by),
            "gap": self.gap,
        }


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "native-cli-workspace-entry",
        "Thin project-local entry for OpenCode, Codex, Claude Code and Gemini CLI "
        "without global installation",
        "partial",
        (
            "AGENTS.md",
            "CLAUDE.md (@AGENTS.md)",
            "GEMINI.md (@AGENTS.md)",
            "integration status/sync (legacy global cleanup plan)",
            "git publication trust-plan/trust-enroll/plan/status/import-evidence/import-approval/revoke/apply",
        ),
        (),
        (
            "tests/integration/test_native_cli_migration.py",
            "tests/unit/test_config.py",
            "tests/unit/test_opencode_agent_bootstrap.py",
            "tests/unit/test_git_publication.py",
            "tests/unit/test_git_publication_trust.py",
            "tests/unit/test_git_publication_wire.py",
        ),
        "Source implementation and fixture-home migration are tested; real per-client "
        "acceptance (A01-A21, A31-A32) has not been run, the real user-home cleanup plan "
        "awaits separate exact authorization, and no CLI/model is qualified.",
    ),
    Capability(
        "native-helper-path",
        "Client-independent unit-test measurement and local continuity for native sessions",
        "partial",
        (
            "test measure",
            "test evidence",
            "continuity native start/checkpoint/close/resume",
        ),
        (),
        (
            "tests/integration/test_native_unit_test_helper.py",
            "tests/integration/test_native_unit_test_cli.py",
            "tests/integration/test_native_continuity.py",
        ),
        "Measurement requires the explicit --authorize --build flags; target-met never closes "
        "a Work Item. Checkpoint narrative fields are client-declared and stay unverified.",
    ),
    Capability(
        "project-rag",
        "Project-scoped hybrid RAG and verified citations",
        "ready",
        ("ask", "project index", "project query", "project citation", "project status"),
        ("zekam-coordinator", "zekam-researcher"),
        ("tests/e2e/test_cli_project_rag.py", "tests/unit/test_project_rag_runtime.py"),
    ),
    Capability(
        "intent-project-routing",
        "Intent, project family, repository role and Jira routing",
        "ready",
        ("route families", "route preview", "route explain", "jira resolve"),
        ("zekam-coordinator", "zekam-router"),
        ("tests/e2e/test_cli_request_route.py", "tests/unit/test_request_routing.py"),
    ),
    Capability(
        "opencode-continuity",
        "Durable OpenCode lifecycle, semantic checkpoint and automatic resume context",
        "ready",
        (
            "resume",
            "opencode install",
            "opencode event",
            "opencode pre-compact",
            "opencode resume",
        ),
        ("zekam-coordinator",),
        (
            "tests/e2e/test_cli_opencode_windows.py",
            "tests/e2e/test_opencode_lifecycle_plugin_runtime.py",
        ),
    ),
    Capability(
        "operational-work-graph",
        "Local project and work authority",
        "partial",
        (
            "project add",
            "project list",
            "project inventory",
            "work create",
            "work list",
            "work show",
            "work history",
            "work dependencies",
            "work relate",
            "work transition",
            "work run-create",
            "work run-show",
            "work run-transition",
            "work run-step",
            "work run-step-transition",
            "work run-steps",
            "work checkpoint",
            "work checkpoints",
            "work close",
            "work resume",
        ),
        ("zekam-coordinator", "zekam-builder", "zekam-verifier"),
        (
            "tests/unit/test_operational_store.py",
            "tests/unit/test_work_dependencies.py",
            "tests/unit/test_natural_work_read.py",
            "tests/e2e/test_cli_package_acceptance.py",
        ),
        "Lease/fencing ve native clientlar arasi devam kabulleri ayri live kanit ister.",
    ),
    Capability(
        "markdown-knowledge",
        "Durable Markdown knowledge lifecycle",
        "ready",
        (
            "knowledge scan",
            "knowledge inspect",
            "knowledge ingest",
            "knowledge list",
            "knowledge show",
            "knowledge search",
            "knowledge create",
            "knowledge update",
            "knowledge archive",
            "knowledge restore",
            "knowledge mutation-status",
        ),
        ("zekam-memory-curator",),
        (
            "tests/e2e/test_cli_local_core.py",
            "tests/unit/test_markdown_knowledge.py",
            "tests/e2e/test_cli_research_and_knowledge.py",
        ),
    ),
    Capability(
        "odi11g-lineage",
        "ODI 11g export preflight and project binding",
        "partial",
        ("project odi-preflight", "project odi-bind"),
        ("zekam-memory-curator", "zekam-researcher"),
        ("tests/e2e/test_cli_odi11g_export.py",),
        "Object-aware sanitizer and exact lineage graph must be validated on real GPU/SKY "
        "exports before embedding.",
    ),
    Capability(
        "research",
        "Project-scoped research with OpenCode events and a native host bridge hook",
        "partial",
        ("research run", "research status", "research report"),
        ("zekam-research-runner", "zekam-researcher", "zekam-verifier"),
        (
            "tests/unit/test_research.py",
            "tests/unit/test_research_runtime.py",
            "tests/unit/test_native_research.py",
            "tests/e2e/test_cli_research_and_knowledge.py",
        ),
    ),
    Capability(
        "ideas",
        "Idea generation, review and promotion",
        "partial",
        (
            "idea propose",
            "idea review",
            "idea work-plan",
            "idea work-create",
            "knowledge note_kind=idea",
        ),
        ("zekam-memory-curator",),
        ("tests/unit/test_intake.py", "tests/unit/test_idea_proposals.py"),
        "Proposal/review is read-only and secret-free; idea work-create now provides the "
        "explicit scope/approval effect, while execution run/receipt linkage remains open.",
    ),
    Capability(
        "reports",
        "Analytics reports and machine-readable reporting",
        "partial",
        ("scheduler report", "scheduler rebuild"),
        ("zekam-verifier",),
        ("tests/e2e/test_cli_local_worker_scheduler.py",),
        "Scheduler report/rebuild surfaces exist for machine-readable reporting.",
    ),
    Capability(
        "model-benchmark",
        "Model health, routing evidence and benchmark laboratory",
        "partial",
        (
            "model benchmark",
            "model decide",
            "model health",
            "model portable-inspect",
            "model campaign plan",
            "model campaign run",
            "model campaign status",
            "model campaign report",
        ),
        ("zekam-router", "zekam-verifier"),
        (
            "tests/unit/test_model_capability_benchmark.py",
            "tests/e2e/test_cli_native_benchmark_campaign.py",
        ),
        "The provider-free native pipeline campaign is ready on Windows; portable catalog "
        "import, real provider execution, baselines and release gates remain gated.",
    ),
    Capability(
        "unit-test-engineering",
        "Evidence-bound unit-test generation, measurement and durable continuation",
        "partial",
        (
            "test plan",
            "test run",
            "test status",
            "test report",
            "test pause/resume/cancel",
            "unit-test-engineering skill lifecycle",
        ),
        ("zekam-coordinator", "zekam-builder", "zekam-verifier"),
        (
            "tests/unit/test_unit_test_engineering_domain.py",
            "tests/unit/test_unit_test_cli.py",
            "tests/unit/test_unit_test_ledger_sqlite.py",
            "tests/integration/test_real_maven_unit_test_fixture.py",
        ),
        "Automatic OpenCode batch (test run) is an explicit opt-in path and is separate "
        "from the native helper (test measure/evidence). Live model qualification remains "
        "explicitly gated; Maven fixture execution is verified on Windows.",
    ),
    Capability(
        "semantic-memory",
        "Memory candidates, hygiene, promotion and retrieval",
        "partial",
        (
            "memory status",
            "memory inspect",
            "memory search",
            "memory candidates",
            "memory hygiene",
            "memory review",
            "memory promote",
        ),
        ("zekam-memory-curator",),
        (
            "tests/integration/test_memory_cli.py",
            "tests/unit/test_memory_continuity_contracts.py",
            "tests/unit/test_memory_continuity_orchestrator.py",
            "tests/e2e/test_cross_harness_memory_continuity.py",
        ),
        "Review/promote claim ve receipt ile explicit effect'tir; memory-sourced recipe "
        "activation ve native two-cycle acceptance ayri kanit ister.",
    ),
    Capability(
        "jira",
        "Deterministic Jira issue resolution",
        "partial",
        ("jira resolve",),
        ("zekam-coordinator", "zekam-researcher"),
        ("tests/e2e/test_cli_jira.py",),
        "Issue fetch and evidence persistence still depend on the OpenCode Jira MCP path.",
    ),
    Capability(
        "backup-recovery",
        "Local backup, restore and recovery diagnostics",
        "ready",
        ("backup create", "backup verify", "backup restore", "local-runtime recover"),
        ("zekam-verifier",),
        ("tests/e2e/test_cli_backup.py", "tests/unit/test_backup_manifest.py"),
    ),
    Capability(
        "autonomous-evolution",
        "Measured local learning and bounded autonomous rollout",
        "ready",
        (
            "evolve plan",
            "evolve status",
            "evolve candidates",
            "evolve report",
            "evolve pause",
            "evolve resume",
            "evolve disable",
            "evolve run-once",
            "evolve enable",
            "evolve bootstrap-plan",
            "evolve supervisor-plan",
            "evolve supervisor-status",
        ),
        ("zekam-coordinator", "zekam-builder", "zekam-verifier"),
        (
            "tests/integration/test_authorized_rollout_runtime.py",
            "tests/unit/test_evolution_rollout.py",
            "tests/unit/test_windows_task_scheduler.py",
        ),
        "Runtime authority and native two-cycle acceptance are reported dynamically by "
        "evolve status; capability readiness does not itself grant effects.",
    ),
)


def capability_inventory(
    *, root: Path | None = None, settings: Any | None = None
) -> dict[str, Any]:
    readiness = capability_readiness(root=root, settings=settings)
    readiness_by_id = {item["capability_id"]: item for item in readiness["capabilities"]}
    items: list[dict[str, Any]] = []
    for capability in CAPABILITIES:
        item = capability.as_dict()
        dynamic = readiness_by_id[capability.capability_id]
        # Static inventory status remains the maturity classification. Runtime
        # readiness is deliberately nested and repeated in explicit fields so
        # callers cannot mistake core health for CAP readiness.
        item.update(
            {
                "readiness": dynamic,
                "readiness_status": dynamic["status"],
                "evidence_state": dynamic["evidence_state"],
                "permission_gap": dynamic["permission_gap"],
                "installation_gap": dynamic["installation_gap"],
            }
        )
        items.append(item)
    counts = {
        status: sum(item["status"] == status for item in items)
        for status in ("ready", "partial", "scaffold")
    }
    body = {
        "schema": CAPABILITY_SCHEMA,
        "capabilities": items,
        "counts": counts,
        "read_only": True,
        "grants_authority": False,
        "readiness": readiness,
        "readiness_counts": readiness["counts"],
    }
    return body | {"inventory_digest": digest(body)}


_DEFAULT_ALLOWED_PERMISSIONS = frozenset({"filesystem.read", "filesystem.write", "process.run"})
_CAPABILITY_PERMISSION_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "native-cli-workspace-entry": ("filesystem.read",),
    "native-helper-path": ("filesystem.read",),
    "project-rag": ("filesystem.read",),
    "intent-project-routing": ("filesystem.read",),
    "opencode-continuity": ("filesystem.read", "process.run"),
    "operational-work-graph": ("filesystem.read", "filesystem.write"),
    "markdown-knowledge": ("filesystem.read", "filesystem.write"),
    "odi11g-lineage": ("filesystem.read", "filesystem.write"),
    "research": ("filesystem.read", "network.access"),
    "ideas": ("filesystem.read", "filesystem.write"),
    "reports": ("filesystem.read",),
    "model-benchmark": ("filesystem.read", "process.run", "network.access"),
    "unit-test-engineering": ("filesystem.read", "filesystem.write", "process.run"),
    "semantic-memory": ("filesystem.read", "filesystem.write"),
    "jira": ("filesystem.read", "network.access"),
    "backup-recovery": ("filesystem.read", "filesystem.write"),
    "autonomous-evolution": ("filesystem.read", "filesystem.write", "process.run"),
}
_CAPABILITY_CLIENT_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "opencode-continuity": ("opencode",),
    "research": ("opencode",),
    "unit-test-engineering": ("opencode",),
}
_CLIENT_COMMANDS = {
    "opencode": "opencode",
    "codex": "codex",
    "claude-code": "claude",
    "gemini": "gemini",
}


@dataclass(frozen=True, slots=True)
class CapabilityReadiness:
    """Provider-free, read-only readiness observation for one capability."""

    capability_id: str
    status: CapabilityReadinessStatus
    evidence_state: CapabilityEvidenceState
    permission_gap: tuple[str, ...]
    installation_gap: tuple[str, ...]
    acceptance_gap: tuple[str, ...]
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "status": self.status,
            "evidence_state": self.evidence_state,
            # Keep the vocabulary used by capability profile consumers while
            # making the absence of a fresh acceptance observation explicit.
            "evidence_level": self.evidence_state,
            "permission_gap": list(self.permission_gap),
            "installation_gap": list(self.installation_gap),
            "acceptance_gap": list(self.acceptance_gap),
            "gaps": {
                "permission": list(self.permission_gap),
                "installation": list(self.installation_gap),
                "acceptance": list(self.acceptance_gap),
            },
            "evidence": self.evidence,
            "provider_calls": 0,
            "network_calls": 0,
            "read_only": True,
            "grants_authority": False,
        }


def _capability_root(root: Path | None) -> Path:
    if root is not None:
        return root.resolve()
    # Lazy import keeps the static inventory usable from packaging/bootstrap
    # code without introducing a configuration import cycle.
    from zekam.application.config import core_root

    return core_root().resolve()


def _reference_path(root: Path, reference: str) -> Path | None:
    path = Path(reference)
    if path.is_absolute() or ".." in path.parts:
        return None
    return root / path


def _allowed_permissions(settings: Any | None) -> tuple[str, ...]:
    profile = None if settings is None else getattr(settings, "permission_profile", None)
    allowed = None if profile is None else getattr(profile, "allowed_capabilities", None)
    if allowed is None:
        return tuple(sorted(_DEFAULT_ALLOWED_PERMISSIONS))
    return tuple(sorted(str(value) for value in allowed))


def _installed_clients(settings: Any | None) -> dict[str, bool]:
    configured: dict[str, bool] = {}
    if settings is not None:
        for client in getattr(settings, "clients", ()):
            name = str(getattr(client, "name", "")).casefold()
            if name:
                configured[name] = bool(getattr(client, "executable_present", False))
    return {
        name: configured.get(name, False) or shutil.which(command) is not None
        for name, command in _CLIENT_COMMANDS.items()
    }


def _capability_readiness(
    capability: Capability,
    *,
    root: Path,
    allowed_permissions: tuple[str, ...],
    installed_clients: dict[str, bool],
    permission_profile_observed: bool,
) -> CapabilityReadiness:
    references = tuple(capability.verified_by)
    present = tuple(
        reference
        for reference in references
        if (path := _reference_path(root, reference)) is not None and path.is_file()
    )
    missing = tuple(reference for reference in references if reference not in present)
    if not present:
        evidence_state: CapabilityEvidenceState = "missing"
    elif missing:
        evidence_state = "partial"
    else:
        # Source/test declarations are not a test receipt or a live acceptance.
        evidence_state = "unobserved"

    required_permissions = _CAPABILITY_PERMISSION_REQUIREMENTS.get(
        capability.capability_id, ("filesystem.read",)
    )
    permission_gap = tuple(
        permission for permission in required_permissions if permission not in allowed_permissions
    )
    installation_gap = tuple(
        f"executable:{client}"
        for client in _CAPABILITY_CLIENT_REQUIREMENTS.get(capability.capability_id, ())
        if not installed_clients.get(client, False)
    )
    acceptance_gap = (
        (capability.gap or "Static inventory is partial; current acceptance is unobserved",)
        if capability.status != "ready"
        else ("Current acceptance receipt is unobserved",)
    )
    evidence = {
        "declared_references": list(references),
        "present_references": list(present),
        "missing_references": list(missing),
        "acceptance_observed": False,
        # Absolute local paths are not exposed in a report that may be printed
        # or persisted as operational evidence.
        "source_root": "configured-core-root",
        "permission_profile_observed": permission_profile_observed,
    }
    if evidence_state == "missing" or capability.status == "scaffold":
        status: CapabilityReadinessStatus = "blocked"
    elif evidence_state != "observed" or permission_gap or installation_gap or acceptance_gap:
        status = "partial"
    else:
        status = "ready"
    return CapabilityReadiness(
        capability_id=capability.capability_id,
        status=status,
        evidence_state=evidence_state,
        permission_gap=permission_gap,
        installation_gap=installation_gap,
        acceptance_gap=acceptance_gap,
        evidence=evidence,
    )


def capability_readiness(
    *, root: Path | None = None, settings: Any | None = None
) -> dict[str, Any]:
    """Build a provider-free local readiness report.

    This function reads source/test references, local executable discovery and
    the resolved permission profile only. It never calls a model/provider,
    writes state, grants authority, or treats core doctor health as CAP proof.
    """

    resolved_root = _capability_root(root)
    allowed_permissions = _allowed_permissions(settings)
    installed_clients = _installed_clients(settings)
    items = [
        _capability_readiness(
            capability,
            root=resolved_root,
            allowed_permissions=allowed_permissions,
            installed_clients=installed_clients,
            permission_profile_observed=settings is not None,
        ).as_dict()
        for capability in CAPABILITIES
    ]
    counts = {
        status: sum(item["status"] == status for item in items)
        for status in ("ready", "partial", "blocked")
    }
    status: CapabilityReadinessStatus
    if counts["blocked"]:
        status = "blocked"
    elif counts["partial"]:
        status = "partial"
    else:
        status = "ready"
    body = {
        "schema": CAPABILITY_READINESS_SCHEMA,
        "status": status,
        "all_ready": status == "ready",
        "capabilities": items,
        "counts": counts,
        "core_health_independent": True,
        "provider_calls": 0,
        "network_calls": 0,
        "read_only": True,
        "grants_authority": False,
    }
    return body | {"readiness_digest": digest(body)}


def compact_capability_summary() -> dict[str, list[str]]:
    return {
        status: [item.capability_id for item in CAPABILITIES if item.status == status]
        for status in ("ready", "partial", "scaffold")
    }
