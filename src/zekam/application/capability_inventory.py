"""Reviewed, model-independent inventory of the current Zekam product surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from zekam.domain.canonical import digest

CAPABILITY_SCHEMA = "zekam-capability-inventory/v1"
CapabilityStatus = Literal["ready", "partial", "scaffold"]


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
        ("project add", "project list", "work create", "work list", "work resume"),
        ("zekam-coordinator", "zekam-builder", "zekam-verifier"),
        ("tests/unit/test_operational_store.py", "tests/e2e/test_cli_package_acceptance.py"),
        "Public work transition/history/checkpoint commands are not complete.",
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
        "Project-scoped, citation-verified OpenCode research",
        "ready",
        ("research run", "research status", "research report"),
        ("zekam-research-runner", "zekam-researcher", "zekam-verifier"),
        (
            "tests/unit/test_research.py",
            "tests/unit/test_research_runtime.py",
            "tests/e2e/test_cli_research_and_knowledge.py",
        ),
    ),
    Capability(
        "ideas",
        "Idea generation, review and promotion",
        "scaffold",
        (),
        ("zekam-memory-curator",),
        ("tests/unit/test_intake.py",),
        "Idea classification and storage roots exist; generation/review/save surfaces do not.",
    ),
    Capability(
        "reports-observatory",
        "Analytics reports and live read-only observatory",
        "partial",
        ("scheduler report", "scheduler rebuild", "ui serve"),
        ("zekam-verifier",),
        ("tests/e2e/test_ui_live_observatory.py",),
        "Research report body show/refresh is not connected to the observatory.",
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
        "semantic-memory",
        "Memory candidates, hygiene, promotion and retrieval",
        "partial",
        (),
        ("zekam-memory-curator",),
        (
            "tests/unit/test_memory_continuity_contracts.py",
            "tests/unit/test_memory_continuity_orchestrator.py",
            "tests/e2e/test_cross_harness_memory_continuity.py",
        ),
        "Current CLI has no memory inspect/search/review/promote/status surface.",
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
)


def capability_inventory() -> dict[str, Any]:
    items = [item.as_dict() for item in CAPABILITIES]
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
    }
    return body | {"inventory_digest": digest(body)}


def compact_capability_summary() -> dict[str, list[str]]:
    return {
        status: [item.capability_id for item in CAPABILITIES if item.status == status]
        for status in ("ready", "partial", "scaffold")
    }
