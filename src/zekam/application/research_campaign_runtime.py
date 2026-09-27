"""Bounded engineering radar campaign plan/run/status/report vertical slice.

A campaign is a composition of existing bounded research runs.  Discover builds
an immutable public inventory and pinned source manifest; Analyse fans out into
narrow sub-research questions through the existing ResearchService contract.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from zekam.application.operational_store import OperationalStore
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.radar_candidate import (
    EvidenceLevel,
    RadarCandidateDecision,
    RadarCandidateKind,
    RadarGapCard,
    RadarPatternCard,
    SourceProvenance,
    decide_automatic,
)
from zekam.domain.research import ResearchBudget, ResearchQuestion, SourceKind, SourcePolicy
from zekam.infrastructure.github_radar_adapter import (
    GitHubInventory,
    GitHubRadarAdapter,
    PinnedCommit,
    RepositoryRecord,
)
from zekam.infrastructure.radar_campaign_store import RadarCampaignRepository
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

PLAN_SCHEMA = "zekam-radar-campaign-plan/v1"
STATUS_SCHEMA = "zekam-radar-campaign-status/v1"
REPORT_SCHEMA = "zekam-radar-campaign-report/v1"
RESULT_SCHEMA = "zekam-radar-campaign-result/v1"

_DEFAULT_OWNERS = ("anthropics", "openai", "google-gemini")
_PREFERRED_REPO_NAMES = frozenset(
    {
        "openai-agents-python",
        "codex",
        "claude-agent-sdk-python",
        "claude-quickstarts",
        "symphony",
        "skills",
        "gemini-cli",
        "gemini-fullstack-langgraph-quickstart",
    }
)


class GitHubRadarPort(Protocol):
    def discover_organization(self, owner: str, *, max_repos: int = 10_000) -> GitHubInventory: ...

    def pin_commit(self, record: RepositoryRecord, branch: str | None = None) -> PinnedCommit: ...

    def fetch_blob(
        self, pin: PinnedCommit, path: str, *, max_bytes: int = 2 * 1024 * 1024
    ) -> Any: ...


class ResearchAdapterPort(Protocol):
    def execute(self, package: dict[str, Any]) -> Any: ...


class AnalyseDispatcherPort(Protocol):
    def dispatch(
        self,
        question: ResearchQuestion,
        snapshots: tuple[Any, ...],
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class CampaignState:
    campaign_id: str
    project_id: str
    project_slug: str
    stage: str
    state: str
    plan_digest: str
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


@dataclass(frozen=True, slots=True)
class CampaignPlan:
    body: dict[str, Any]

    @property
    def campaign_id(self) -> str:
        return str(self.body["campaign_id"])

    @property
    def project_id(self) -> str:
        return str(self.body["project_id"])

    @property
    def project_slug(self) -> str:
        return str(self.body["project_slug"])

    @property
    def stage(self) -> str:
        return str(self.body["stage"])

    @property
    def plan_digest(self) -> str:
        return str(self.body["plan_digest"])

    @property
    def idempotency_key(self) -> str:
        return str(self.body["idempotency_key"])

    @property
    def inventory_digest(self) -> str | None:
        value = self.body.get("inventory_digest")
        return None if value is None else str(value)

    @property
    def pinned_manifest_digest(self) -> str | None:
        value = self.body.get("pinned_manifest_digest")
        return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class CoverageGap:
    gap_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class CampaignStatus:
    campaign_id: str
    stage: str
    state: str
    completed_work: tuple[str, ...]
    pending_work: tuple[str, ...]
    coverage_gaps: tuple[CoverageGap, ...]
    consumed_budget: dict[str, int]
    reserved_budget: dict[str, int]
    next_safe_action: str


@dataclass(frozen=True, slots=True)
class CampaignReport:
    campaign_id: str
    stage: str
    status: str
    inventory_summary: dict[str, Any]
    selected_repos: tuple[dict[str, Any], ...]
    source_manifest_summary: dict[str, Any]
    sub_research_runs: tuple[dict[str, Any], ...]
    coverage_gaps: tuple[CoverageGap, ...]
    budget: dict[str, dict[str, int]]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _timestamp(moment: dt.datetime | None = None) -> str:
    return (moment or _now()).isoformat().replace("+00:00", "Z")


def _default_discover_limits() -> dict[str, int]:
    return {
        "repos_max": 10_000,
        "selected_max": 12,
        "paths_max": 192,
        "duration_seconds": 600,
    }


def _default_analyse_limits() -> dict[str, int]:
    return {
        "repos_max": 12,
        "selected_max": 12,
        "paths_max": 192,
        "duration_seconds": 7_200,
    }


def _default_discover_budget() -> dict[str, int]:
    return {
        "requests": 100,
        "bytes": 32 * 1024 * 1024,
        "calls": 0,
        "tokens": 0,
    }


def _default_analyse_budget() -> dict[str, int]:
    return {
        "requests": 500,
        "bytes": 64 * 1024 * 1024,
        "calls": 24,
        "tokens": 288_000,
    }


def build_radar_plan(
    store: OperationalStore,
    home: Path,
    *,
    project_ref: str,
    stage: str,
    inventory_digest: str | None = None,
    owners: tuple[str, ...] | None = None,
) -> CampaignPlan:
    """Build a deterministic, provider-free radar campaign plan.

    Discover plans are local and require no prior inventory.  Analyse plans need
    a verified inventory digest; otherwise they raise a policy violation that
    the caller should surface as ``needs-discovery``/``stale``.
    """

    if stage not in {"discover", "analyse"}:
        raise ValidationFailed("Radar stage 'discover' veya 'analyse' olmali")
    with store.unit_of_work() as uow:
        project = uow.resolve_project(project_ref)
        uow.commit()

    selected_owners = owners if owners is not None else _DEFAULT_OWNERS
    if not selected_owners:
        raise ValidationFailed("Radar plan en az bir owner ister")

    if stage == "analyse":
        if inventory_digest is None:
            raise PolicyViolation("Analyse plan needs-discovery: inventory_digest gerekiyor")
        repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
        inventory = repo.get_inventory_by_digest(inventory_digest)
        if inventory is None:
            raise PolicyViolation("Analyse plan stale: inventory_digest bulunamadi")

    limits = (
        _default_discover_limits() if stage == "discover" else _default_analyse_limits()
    )
    budget = (
        _default_discover_budget() if stage == "discover" else _default_analyse_budget()
    )

    intent_digest = digest(
        {
            "operation": "research.radar",
            "project_id": project.id,
            "owners": sorted(selected_owners),
            "inventory_digest": inventory_digest,
            "source_revision": "git-head",
        }
    )
    campaign_id = f"radar-campaign:{intent_digest[7:]}"
    stable: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "operation": "research.radar",
        "campaign_id": campaign_id,
        "project_id": project.id,
        "project_slug": project.slug,
        "stage": stage,
        "owners": sorted(selected_owners),
        "budget": budget,
        "limits": limits,
        "inventory_digest": inventory_digest,
        "pinned_manifest_digest": None,
        "requires_public_source_read_authorization": True,
        "requires_agent_run_authorization": stage == "analyse",
        "provider_calls_performed": 0,
        "grants_authority": False,
    }
    plan_digest = digest(stable)
    body = stable | {
        "plan_digest": plan_digest,
        "idempotency_key": f"radar:{plan_digest}:{stage}",
        "dry_run": True,
    }
    return CampaignPlan(body)


def _radar_cache_dir(home: Path) -> Path:
    cache = home / "radar" / "github-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _select_repos(
    inventories: tuple[GitHubInventory, ...], max_selected: int
) -> tuple[RepositoryRecord, ...]:
    all_repos: list[RepositoryRecord] = []
    seen: set[int] = set()
    # Prefer Ek B repositories, then first-come.
    for inventory in inventories:
        for repo in inventory.repositories:
            if repo.repository_id in seen:
                continue
            seen.add(repo.repository_id)
            all_repos.append(repo)
    preferred = [r for r in all_repos if r.name in _PREFERRED_REPO_NAMES]
    remaining = [r for r in all_repos if r.name not in _PREFERRED_REPO_NAMES]
    selected = preferred + remaining
    return tuple(selected[:max_selected])


def _build_sub_questions(
    project_slug: str,
    manifest: tuple[dict[str, Any], ...],
    max_questions: int,
) -> tuple[dict[str, Any], ...]:
    # One question per pinned repo, bounded by max_questions.
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for entry in manifest:
        key = f"{entry.get('owner')}/{entry.get('name')}"
        by_repo.setdefault(key, []).append(entry)
    questions: list[dict[str, Any]] = []
    for idx, (repo_key, entries) in enumerate(by_repo.items()):
        if idx >= max_questions:
            break
        selected_paths = sorted({e["path"] for e in entries})[:5]
        paths = ", ".join(selected_paths)
        questions.append(
            {
                "question_id": f"radar-sub:{digest(repo_key)[7:]}",
                "question": (
                    f"{repo_key} deposundaki {paths} iceriklerinde Zekam icin"
                    " uygulanabilir mühendislik pattern'leri var mi?"
                ),
                "repo_key": repo_key,
                "source_count": 1,
                "slice_count": len(selected_paths),
                "path_overflow": len(entries) > 5,
            }
        )
    return tuple(questions)


def _build_checkpoint(
    repo: RadarCampaignRepository,
    campaign_id: str,
    sequence: int,
    next_safe_action: str,
) -> dict[str, Any]:
    status_doc = repo.campaign_status_document(campaign_id)
    payload = {
        "schema": "zekam-radar-campaign-checkpoint/v1",
        "campaign_id": campaign_id,
        "sequence": sequence,
        "completed_work": status_doc["completed_work"],
        "pending_work": status_doc["pending_work"],
        "coverage_gaps": status_doc["coverage_gaps"],
        "consumed_budget": status_doc["consumed_budget"],
        "reserved_budget": status_doc["reserved_budget"],
        "next_safe_action": next_safe_action,
    }
    repo.save_checkpoint(campaign_id, sequence, payload)
    return payload


def run_radar_campaign(
    store: OperationalStore,
    home: Path,
    plan: CampaignPlan,
    *,
    authorized_plan_digest: str,
    authorize_public_source_read: bool,
    authorize_agent_run: bool,
    github_adapter: GitHubRadarPort | None = None,
    research_adapter: ResearchAdapterPort | None = None,
    analyse_dispatcher: AnalyseDispatcherPort | None = None,
) -> dict[str, Any]:
    """Execute one digest-bound radar campaign stage behind a durable effect claim."""

    if authorized_plan_digest != plan.plan_digest:
        raise PolicyViolation("Radar run exact plan digest ister")
    if not authorize_public_source_read:
        raise PolicyViolation("Radar run public source read authorization ister")
    if plan.stage == "analyse" and not authorize_agent_run:
        raise PolicyViolation("Radar analyse agent run authorization ister")

    db_path = home / "state" / "operational.db"
    radar_db_path = home / "state" / "radar-campaigns.db"
    repo = RadarCampaignRepository(radar_db_path)
    runtime = SQLiteLocalRuntimeStore(db_path, existing_only=True)

    payload = dict(plan.body)
    payload["dry_run"] = False
    job, created = runtime.enqueue(
        idempotency_key=plan.idempotency_key,
        payload=payload,
        max_attempts=1,
    )
    if not created:
        campaign = repo.get_campaign_by_idempotency_key(plan.idempotency_key)
        if campaign is None:
            raise PolicyViolation("Radar replay campaign kaydi bulunamadi")
        status_doc = repo.campaign_status_document(campaign.campaign_id)
        status_doc["replayed"] = True
        status_doc["job_id"] = job.id
        return status_doc

    owner_token = os.urandom(32).hex()
    work = runtime.claim_next(
        owner_id=f"radar-{os.getpid()}",
        owner_pid=os.getpid(),
        owner_token=owner_token,
        lease_seconds=min(plan.body["limits"]["duration_seconds"], 3600),
        resources=(f"radar:{plan.plan_digest}",),
        supported_operations=("research.radar",),
        job_id=job.id,
    )
    if work is None:
        raise PolicyViolation("Radar job claim edilemedi")

    effect = {
        "operation": "research.radar",
        "plan_digest": plan.plan_digest,
        "project_id": plan.project_id,
        "stage": plan.stage,
        "authorized_public_source_read": authorize_public_source_read,
        "authorized_agent_run": authorize_agent_run,
    }
    claim, claim_created = runtime.claim_effect(
        work,
        operation="research.radar",
        effect_digest=digest(effect),
        idempotency_key=f"radar:{plan.plan_digest}:effect",
    )
    if not claim_created:
        runtime.finish(work, state="recovery-required")
        raise PolicyViolation("Radar effect claim replay; silent redispatch yasak")

    campaign = repo.create_campaign(
        project_id=plan.project_id,
        campaign_id=plan.campaign_id,
        stage=plan.stage,
        state="running",
        plan_digest=plan.plan_digest,
        plan=plan.body,
        idempotency_key=plan.idempotency_key,
    )
    repo.update_campaign_state(campaign.campaign_id, "running", effect_claim_id=claim.id)
    repo.record_stage(campaign.id, plan.stage, "running", next_safe_action="execute")

    checkpoint_sequence = 0
    try:
        if plan.stage == "discover":
            _run_discover(
                home,
                repo,
                campaign.id,
                plan,
                github_adapter=github_adapter,
            )
        elif plan.stage == "analyse":
            _run_analyse(
                repo,
                campaign.id,
                plan,
                analyse_dispatcher=analyse_dispatcher,
            )
        else:
            raise ValidationFailed("Bilinmeyen radar stage")

        repo.update_campaign_state(campaign.campaign_id, "completed")
        repo.record_stage(
            campaign.id,
            plan.stage,
            "completed",
            evidence_digest=digest({"campaign_id": campaign.campaign_id, "stage": plan.stage}),
            next_safe_action="report",
        )
        checkpoint_sequence += 1
        _build_checkpoint(repo, campaign.id, checkpoint_sequence, "report")
        terminal_evidence = digest({"campaign_id": campaign.campaign_id, "state": "completed"})
        receipt = runtime.record_receipt(
            claim, status="completed", evidence_digest=terminal_evidence
        )
        runtime.finish(work, state="completed", evidence_digest=terminal_evidence)
    except Exception as exc:
        failure_evidence = digest(
            {
                "operation": "research.radar",
                "campaign_id": campaign.campaign_id,
                "status": "failed",
                "error_type": type(exc).__name__,
            }
        )
        repo.update_campaign_state(campaign.campaign_id, "failed")
        repo.record_stage(
            campaign.id,
            plan.stage,
            "failed",
            evidence_digest=failure_evidence,
            next_safe_action="review",
        )
        checkpoint_sequence += 1
        _build_checkpoint(repo, campaign.id, checkpoint_sequence, "review")
        try:
            runtime.record_receipt(claim, status="failed", evidence_digest=failure_evidence)
            runtime.finish(work, state="failed", evidence_digest=failure_evidence)
        except Exception:
            pass
        raise

    status_doc = repo.campaign_status_document(campaign.campaign_id)
    body: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "job_id": job.id,
        "campaign_id": campaign.campaign_id,
        "plan_digest": plan.plan_digest,
        "stage": plan.stage,
        "state": "completed",
        "checkpoint": status_doc["latest_checkpoint"],
        "receipt": {
            "claim_id": claim.id,
            "receipt_id": receipt.id,
            "status": receipt.status,
            "evidence_digest": receipt.evidence_digest,
        },
        "replayed": False,
        "grants_authority": False,
    }
    return body | {"result_digest": digest(body)}


def _run_discover(
    home: Path,
    repo: RadarCampaignRepository,
    campaign_id: str,
    plan: CampaignPlan,
    *,
    github_adapter: GitHubRadarPort | None = None,
) -> None:
    cache_dir = _radar_cache_dir(home)
    adapter = github_adapter or GitHubRadarAdapter(cache_dir=cache_dir)
    owners = tuple(plan.body["owners"])
    limits = plan.body["limits"]
    budget = plan.body["budget"]

    # Reserve the whole discover budget up front.
    for category, limit in budget.items():
        if category in {"requests", "bytes"}:
            repo.reserve_budget(campaign_id, category, limit, limit)

    inventories: list[GitHubInventory] = []
    for owner in owners:
        inventory = adapter.discover_organization(owner, max_repos=limits["repos_max"])
        inventories.append(inventory)
        repo.save_inventory(
            campaign_id,
            owner,
            inventory.as_dict() if hasattr(inventory, "as_dict") else _inventory_to_dict(inventory),
            state=inventory.state.value,
        )
        repo.consume_budget(campaign_id, "requests", inventory.total_requests)
        repo.consume_budget(campaign_id, "bytes", inventory.total_response_bytes)

    selected = _select_repos(tuple(inventories), limits["selected_max"])
    pins: list[dict[str, Any]] = []
    for repo_record in selected:
        pin = adapter.pin_commit(repo_record)
        pins.append(
            {
                "repository_id": pin.repository_id,
                "owner": pin.owner,
                "name": pin.name,
                "branch": pin.branch,
                "commit_sha": pin.commit_sha,
            }
        )
        # Fetch a bounded README manifest entry for each selected repo.
        blob = adapter.fetch_blob(pin, "README.md")
        repo.save_pinned_commits(campaign_id, (pins[-1],))
        repo.save_source_manifest(
            campaign_id,
            repo.list_pinned_commits(campaign_id)[-1]["id"],
            (
                {
                    "path": "README.md",
                    "blob_sha": blob.blob_sha,
                    "raw_content_digest": blob.raw_content_digest,
                    "complete": blob.complete,
                    "omission_reason": blob.omission_reason,
                },
            ),
        )


def _run_analyse(
    repo: RadarCampaignRepository,
    campaign_id: str,
    plan: CampaignPlan,
    *,
    analyse_dispatcher: AnalyseDispatcherPort | None,
) -> None:
    inventory_digest = plan.inventory_digest
    if inventory_digest is None:
        raise PolicyViolation("Analyse discovery inventory_digest gerekiyor")
    inventory_row = repo.get_inventory_by_digest(inventory_digest)
    if inventory_row is None:
        raise PolicyViolation("Analyse stale inventory_digest")

    source_campaign_id = inventory_row.campaign_id
    pins = repo.list_pinned_commits(source_campaign_id)
    manifest = repo.list_source_manifest(source_campaign_id)
    if not pins:
        repo.record_coverage_gap(campaign_id, "missing-pins", "Pinned commit bulunamadi")
        return

    budget = plan.body["budget"]
    for category, limit in budget.items():
        if limit > 0:
            repo.reserve_budget(campaign_id, category, limit, limit)

    # Build snapshots from manifest.
    snapshots: list[Any] = []
    for entry in manifest:
        pin = next((p for p in pins if p["id"] == entry["pin_id"]), None)
        if pin is None:
            continue
        snapshots.append(
            {
                "snapshot_id": f"{pin['owner']}/{pin['name']}:{entry['path']}",
                "kind": "repository",
                "locator": f"{pin['owner']}/{pin['name']}/{entry['path']}",
                "content_digest": entry.get("raw_content_digest") or digest(""),
                "revision": pin["commit_sha"],
                "repository_id": pin["repository_id"],
                "owner": pin["owner"],
                "name": pin["name"],
                "path": entry["path"],
            }
        )

    max_questions = min(len(pins), 24)
    sub_questions = _build_sub_questions(
        plan.project_slug,
        tuple(
            {
                "owner": p["owner"],
                "name": p["name"],
                "path": "README.md",
            }
            for p in pins
        ),
        max_questions,
    )

    if analyse_dispatcher is None:
        repo.record_coverage_gap(
            campaign_id,
            "no-dispatcher",
            "Analyse dispatcher bagli degil; canli agent calistirma yetkili degil",
        )
        return

    for idx, sub_question in enumerate(sub_questions):
        if idx >= budget["calls"]:
            repo.record_coverage_gap(
                campaign_id,
                f"call-limit-{idx}",
                "Agent call limitine ulasildi",
            )
            break

        runs = repo.list_sub_research_runs(campaign_id)
        question_digest = digest(sub_question)
        if _is_no_progress(runs, question_digest):
            repo.record_coverage_gap(
                campaign_id,
                f"no-progress-{sub_question['question_id']}",
                "Uc ardisik tamamlanmis alt kosu ayni sonucu tekrarliyor",
            )
            break

        question = _build_research_question(plan, sub_question)
        repo_key = sub_question["repo_key"]
        repo_snapshots = tuple(
            s for s in snapshots if s["snapshot_id"].startswith(f"{repo_key}:")
        )
        run = repo.save_sub_research_run(campaign_id, sub_question, "running")
        repo.consume_budget(campaign_id, "calls", 1)
        try:
            result = analyse_dispatcher.dispatch(question, repo_snapshots)
            _transform_and_save_analyse(
                repo, campaign_id, sub_question, repo_snapshots, result
            )
            measurements = _extract_measurements(result)
            result_payload: dict[str, Any] = {
                "result": repr(result)[:1000],
                "estimated_tokens": _estimate_tokens(result),
            }
            if measurements["tokens"] is not None:
                repo.consume_budget(campaign_id, "tokens", measurements["tokens"])
                result_payload["measured_tokens"] = measurements["tokens"]
            else:
                result_payload["measured_tokens"] = None
                result_payload["missing_measurement_reason"] = measurements.get(
                    "missing_reason"
                )
            repo.update_sub_research_run(
                run.id,
                "completed",
                result=result_payload,
            )
        except Exception as exc:
            repo.update_sub_research_run(
                run.id,
                "failed",
                result={"error": str(exc)},
            )


def _transform_and_save_analyse(
    repo: RadarCampaignRepository,
    campaign_id: str,
    sub_question: dict[str, Any],
    repo_snapshots: tuple[Any, ...],
    result: Any,
) -> None:
    """Convert one sub-research result to pattern/gap cards and a decision.

    Code/schema/security proposals are always ``rejected-risk`` because they
    must cross the existing improvement/evaluation/rollout boundary before any
    mutation.
    """

    is_success = getattr(result, "outcome", "") == "success"
    snapshot = repo_snapshots[0] if repo_snapshots else {}
    source = SourceProvenance(
        repository_id=int(snapshot.get("repository_id", 0) or 0),
        owner=str(snapshot.get("owner", "unknown")),
        name=str(snapshot.get("name", "unknown")),
        commit_sha=str(snapshot.get("revision", "")),
        path=str(snapshot.get("path") or "README.md"),
        range="1-1",
        evidence_digest=snapshot.get("content_digest") or digest(""),
    )
    repo_key = sub_question["repo_key"]
    if is_success:
        card: RadarPatternCard | RadarGapCard = RadarPatternCard(
            card_id=f"radar-pattern:{sub_question['question_id']}",
            campaign_id=campaign_id,
            kind=RadarCandidateKind.PATTERN,
            problem=f"{repo_key} kaynakli pattern",
            source=source,
            observed_behavior="successful analyse result produced findings",
            inference_or_assumption="source-reviewed; runtime not measured",
            test_evidence_level=EvidenceLevel.SOURCE_REVIEWED,
            license_reuse_constraint="upstream license must be reviewed before reuse",
            cost_dependency_limit="unknown until local evaluation",
            not_applicable_conditions="requires exact local problem and measurement",
        )
        repo.save_pattern_card(campaign_id, card)
        problem_text = card.problem
    else:
        card = RadarGapCard(
            card_id=f"radar-gap:{sub_question['question_id']}",
            campaign_id=campaign_id,
            kind=RadarCandidateKind.GAP,
            current_baseline="Zekam main",
            source_binding=repo_key,
            production_call_path="not mapped",
            test_measurement_evidence="no local measurement",
            covered_areas="upstream source reviewed",
            uncovered_areas="local baseline comparison not measured",
            result=RadarCandidateDecision.GAP_DEMONSTRATED,
        )
        repo.save_gap_card(campaign_id, card)
        problem_text = f"gap: {repo_key}"

    selection = decide_automatic(
        card_id=card.card_id,
        campaign_id=campaign_id,
        problem=problem_text,
        local_evidence="Zekam baseline not measured",
        upstream_evidence="source-reviewed" if is_success else "metadata-only",
        smallest_actionable_solution="evaluate against local fixture",
        affected_logical_resources=("local-index",),
        expected_benefit="unknown without measurement",
        risk="medium",
        maintenance_burden="unknown",
        dependencies="requires local evaluation fixture",
        acceptance_test="existing tests pass with change",
        rollback="revert patch",
        is_code_or_schema_change=True,
        has_duplicate=False,
    )
    repo.save_candidate_decision(campaign_id, selection)


def _build_research_question(plan: CampaignPlan, sub_question: dict[str, Any]) -> ResearchQuestion:
    intent_digest = digest(
        {
            "operation": "research.radar.sub",
            "campaign_id": plan.campaign_id,
            "question_id": sub_question["question_id"],
        }
    )
    return ResearchQuestion(
        question_id=sub_question["question_id"],
        question=sub_question["question"],
        project_ref=plan.project_slug,
        work_ref=plan.campaign_id,
        intent_digest=intent_digest,
        source_revision="git-head",
        policy=SourcePolicy(
            allowed_kinds=frozenset({SourceKind.REPOSITORY, SourceKind.HTTPS}),
            allowed_hosts=frozenset({"github.com", "raw.githubusercontent.com"}),
            project_scope=plan.project_slug,
            allow_row_data=False,
        ),
        budget=ResearchBudget(
            max_tokens=12_000,
            max_cost_units=3,
            max_seconds=600,
            max_rounds=1,
        ),
        created_at=_now(),
    )


def _extract_measurements(result: Any) -> dict[str, Any]:
    """Read actual telemetry from a sub-research result if available.

    Missing measurements are returned as ``None`` with a reason; they are never
    silently replaced by character counts or reported as measured usage.
    """

    if not hasattr(result, "as_dict"):
        return {
            "tokens": None,
            "provider_requests": None,
            "latency_ms": None,
            "cost_units": None,
            "missing_reason": "Dispatcher result has no telemetry interface",
        }
    data = result.as_dict()
    missing_reason = data.get("missing_measurement_reason")
    return {
        "tokens": data.get("measured_tokens"),
        "provider_requests": data.get("measured_provider_requests"),
        "latency_ms": data.get("measured_latency_ms"),
        "cost_units": data.get("measured_cost_units"),
        "missing_reason": missing_reason,
    }


def _estimate_tokens(result: Any) -> int | None:
    # No actual telemetry; estimate from serialized size for information only.
    # This value is never consumed as measured usage.
    try:
        text = canonical_json({"result": repr(result)})
    except Exception:
        text = repr(result)
    return max(1, len(text.encode("utf-8")) // 4)


def _is_no_progress(runs: tuple[Any, ...], question_digest: str) -> bool:
    """Detect three consecutive completed runs with identical results."""

    relevant = [r for r in runs if r.question_digest == question_digest]
    if len(relevant) < 3:
        return False
    last_three = relevant[-3:]
    if any(r.state != "completed" for r in last_three):
        return False
    digests = {r.result_digest for r in last_three}
    return len(digests) == 1 and digests != {None}


def _inventory_to_dict(inventory: GitHubInventory) -> dict[str, Any]:
    return {
        "owner": inventory.owner,
        "repositories": [r.as_dict() for r in inventory.repositories],
        "state": inventory.state.value,
        "pages_fetched": inventory.pages_fetched,
        "total_requests": inventory.total_requests,
        "total_response_bytes": inventory.total_response_bytes,
        "receipts": [r.as_dict() for r in inventory.receipts],
        "next_safe_action": inventory.next_safe_action,
        "observed_at": inventory.observed_at,
    }
