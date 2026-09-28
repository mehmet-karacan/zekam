"""WP-04 bounded runtime limit tests for engineering radar campaigns."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from zekam.application.home import HomeLayout
from zekam.application.operational_store import OperationalStore
from zekam.application.research_campaign_runtime import (
    CampaignPlan,
    _run_analyse,
    _run_discover,
    run_radar_campaign,
)
from zekam.domain.canonical import digest
from zekam.infrastructure.github_radar_adapter import (
    FetchReceipt,
    GitHubInventory,
    InventoryState,
    PinnedCommit,
    RepositoryRecord,
    SourceBlob,
)
from zekam.infrastructure.radar_analyse_dispatcher import (
    AnalyseResult,
    FakeAnalyseDispatcher,
)
from zekam.infrastructure.radar_campaign_store import RadarCampaignRepository
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.unit


class ManyRepoGitHubAdapter:
    """Offline adapter returning a configurable number of identical repos."""

    def __init__(self, repo_count: int) -> None:
        self.repo_count = repo_count
        self.calls: list[tuple[str, str]] = []

    def discover_organization(self, owner: str, *, max_repos: int = 10_000) -> GitHubInventory:
        self.calls.append(("discover", owner))
        repos = [
            RepositoryRecord(
                repository_id=10_000 + idx,
                owner=owner,
                name=f"repo-{idx}",
                full_name=f"{owner}/repo-{idx}",
                default_branch="main",
                visibility="public",
                fork=False,
                archived=False,
                disabled=False,
            )
            for idx in range(min(self.repo_count, max_repos))
        ]
        return GitHubInventory(
            owner=owner,
            repositories=tuple(repos),
            state=InventoryState.COMPLETE,
            pages_fetched=1,
            total_requests=1,
            total_response_bytes=200,
            receipts=(
                FetchReceipt(
                    "GET",
                    f"/orgs/{owner}/repos",
                    200,
                    200,
                    observed_at="2026-01-01T00:00:00Z",
                ),
            ),
            next_safe_action="analyse",
        )

    def pin_commit(self, record: RepositoryRecord, branch: str | None = None) -> PinnedCommit:
        self.calls.append(("pin", f"{record.owner}/{record.name}"))
        return PinnedCommit(
            repository_id=record.repository_id,
            owner=record.owner,
            name=record.name,
            branch=branch or record.default_branch,
            commit_sha="abc123" * 6,
        )

    def fetch_blob(
        self, pin: PinnedCommit, path: str, *, max_bytes: int = 2 * 1024 * 1024
    ) -> SourceBlob:
        self.calls.append(("fetch_blob", f"{pin.owner}/{pin.name}/{path}"))
        body = f"# {pin.name}\n\nSample README.\n".encode()
        return SourceBlob(
            repository_id=pin.repository_id,
            commit_sha=pin.commit_sha,
            path=path,
            blob_sha="blobsha1",
            raw_bytes=body,
            raw_content_digest=digest(body.decode("utf-8")),
            complete=True,
        )


class ConcurrencySpyDispatcher:
    """Dispatcher that blocks until released so concurrency can be measured."""

    def __init__(self) -> None:
        self._release_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.active_count = 0
        self.max_active_count = 0
        self.question_ids: list[str] = []

    def release(self, question_id: str) -> None:
        event = self._release_events.get(question_id)
        if event is not None:
            event.set()

    def release_all(self) -> None:
        for event in self._release_events.values():
            event.set()

    def dispatch(self, question: Any, snapshots: tuple[Any, ...]) -> AnalyseResult:
        with self._lock:
            self.active_count += 1
            self.max_active_count = max(self.max_active_count, self.active_count)
            self.question_ids.append(question.question_id)
        event = threading.Event()
        with self._lock:
            self._release_events[question.question_id] = event
        event.wait(timeout=5.0)
        with self._lock:
            self.active_count -= 1
        return FakeAnalyseDispatcher().dispatch(question, snapshots)


@pytest.fixture
def runtime(tmp_path: Path) -> tuple[Path, OperationalStore, Any]:
    layout = HomeLayout(tmp_path / ".zekam").ensure()
    layout.ensure_project("demo")
    home = layout.root
    database = home / "state" / "operational.db"
    bootstrap(database)
    SQLiteLocalRuntimeStore(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="demo", display_name="Demo")
        uow.commit()
    return home, store, project


def _repo(home: Path) -> RadarCampaignRepository:
    return RadarCampaignRepository(home / "state" / "radar-campaigns.db")


def _make_plan(
    project: Any,
    *,
    stage: str = "discover",
    limits: dict[str, int] | None = None,
    budget: dict[str, int] | None = None,
    inventory_digest: str | None = None,
) -> CampaignPlan:
    limits = limits or {}
    budget = budget or {}
    intent_digest = digest(
        {
            "operation": "research.radar",
            "project_id": project.id,
            "owners": ["openai"],
            "inventory_digest": inventory_digest,
            "source_revision": "git-head",
        }
    )
    campaign_id = f"radar-campaign:{intent_digest[7:]}"
    stable: dict[str, Any] = {
        "schema": "zekam-radar-campaign-plan/v1",
        "operation": "research.radar",
        "campaign_id": campaign_id,
        "project_id": project.id,
        "project_slug": project.slug,
        "stage": stage,
        "owners": ["openai"],
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


def _create_campaign(
    repo: RadarCampaignRepository,
    plan: CampaignPlan,
) -> Any:
    return repo.create_campaign(
        project_id=plan.project_id,
        campaign_id=plan.campaign_id,
        stage=plan.stage,
        state="running",
        plan_digest=plan.plan_digest,
        plan=plan.body,
        idempotency_key=plan.idempotency_key,
    )


def _seed_discovery(
    repo: RadarCampaignRepository,
    project: Any,
    pin_count: int,
) -> tuple[Any, str]:
    """Create a discover campaign, inventory, pins and manifest for analyse tests."""

    discover_plan = _make_plan(
        project,
        stage="discover",
        limits={
            "repos_max": 10_000,
            "selected_max": pin_count,
            "paths_max": 192,
            "duration_seconds": 600,
            "concurrent_sub_runs": 2,
            "invocation_attempts": 72,
        },
        budget={"requests": 100, "bytes": 32 * 1024 * 1024, "calls": 0, "tokens": 0},
    )
    discover_campaign = _create_campaign(repo, discover_plan)
    inventory = {"owner": "openai", "repositories": []}
    inventory_row = repo.save_inventory(
        campaign_id=discover_campaign.id,
        owner="openai",
        inventory=inventory,
        state="complete",
    )
    pins = [
        {
            "repository_id": 1000 + idx,
            "owner": "openai",
            "name": f"repo-{idx}",
            "branch": "main",
            "commit_sha": "abc123" * 6,
        }
        for idx in range(pin_count)
    ]
    manifest_entries = [
        {
            "path": "README.md",
            "blob_sha": "blobsha1",
            "raw_content_digest": digest(f"readme-{idx}"),
            "complete": True,
            "omission_reason": None,
        }
        for idx in range(pin_count)
    ]
    repo.save_pinned_commits(discover_campaign.id, tuple(pins))
    saved_pins = repo.list_pinned_commits(discover_campaign.id)
    for pin, entry in zip(saved_pins, manifest_entries, strict=True):
        repo.save_source_manifest(discover_campaign.id, pin["id"], (entry,))
    return discover_campaign, inventory_row.inventory_digest


def test_discover_enforces_paths_max_and_records_coverage_gap(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """192 path cap: 200 repos yield 192 manifest entries and a gap card."""

    home, _store, project = runtime
    repo = _repo(home)
    plan = _make_plan(
        project,
        stage="discover",
        limits={
            "repos_max": 10_000,
            "selected_max": 200,
            "paths_max": 192,
            "duration_seconds": 600,
            "concurrent_sub_runs": 2,
            "invocation_attempts": 72,
        },
        budget={"requests": 100, "bytes": 32 * 1024 * 1024, "calls": 0, "tokens": 0},
    )
    campaign = _create_campaign(repo, plan)
    adapter = ManyRepoGitHubAdapter(200)

    _run_discover(home, repo, campaign.id, plan, github_adapter=adapter)

    manifest = repo.list_source_manifest(campaign.id)
    assert len(manifest) == 192
    gaps = repo.list_coverage_gaps(campaign.id)
    gap_keys = {g.gap_key for g in gaps}
    assert "discover-paths-overflow" in gap_keys


def test_analyse_concurrency_limited_to_two_sub_runs(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """At most two dispatcher calls run concurrently."""

    home, _store, project = runtime
    repo = _repo(home)
    _discover_campaign, inventory_digest = _seed_discovery(repo, project, pin_count=5)

    plan = _make_plan(
        project,
        stage="analyse",
        limits={
            "repos_max": 12,
            "selected_max": 12,
            "paths_max": 192,
            "duration_seconds": 7_200,
            "concurrent_sub_runs": 2,
            "invocation_attempts": 72,
        },
        budget={
            "requests": 500,
            "bytes": 64 * 1024 * 1024,
            "calls": 24,
            "tokens": 288_000,
        },
        inventory_digest=inventory_digest,
    )
    analyse_campaign = _create_campaign(repo, plan)

    spy = ConcurrencySpyDispatcher()
    # Start _run_analyse in a background thread so we can release calls
    # incrementally from the test thread.
    analyse_thread_exception: list[BaseException] = []

    def _target() -> None:
        try:
            _run_analyse(repo, analyse_campaign.id, plan, analyse_dispatcher=spy)
        except BaseException as exc:
            analyse_thread_exception.append(exc)

    thread = threading.Thread(target=_target)
    thread.start()
    # Wait until at least two calls are active.
    deadline = time.monotonic() + 5.0
    while spy.max_active_count < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    # Immediately release the first two blocked calls.
    for qid in spy.question_ids[:2]:
        spy.release(qid)
    # Wait for the remaining queued calls to start, then release them too.
    deadline = time.monotonic() + 5.0
    while len(spy.question_ids) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    spy.release_all()
    thread.join(timeout=10.0)

    assert not analyse_thread_exception, analyse_thread_exception
    assert spy.max_active_count == 2


def test_analyse_deadline_returns_partial_with_checkpoint(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A zero-second deadline triggers a blocked campaign state and checkpoint."""

    home, store, project = runtime
    repo = _repo(home)
    _discover_campaign, inventory_digest = _seed_discovery(repo, project, pin_count=3)

    plan = _make_plan(
        project,
        stage="analyse",
        limits={
            "repos_max": 12,
            "selected_max": 12,
            "paths_max": 192,
            "duration_seconds": 0,
            "concurrent_sub_runs": 2,
            "invocation_attempts": 72,
        },
        budget={
            "requests": 500,
            "bytes": 64 * 1024 * 1024,
            "calls": 24,
            "tokens": 288_000,
        },
        inventory_digest=inventory_digest,
    )

    result = run_radar_campaign(
        store,
        home,
        plan,
        authorized_plan_digest=plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=True,
        analyse_dispatcher=FakeAnalyseDispatcher(),
    )

    assert result["state"] == "blocked"
    assert result["checkpoint"] is not None
    status = repo.campaign_status_document(result["campaign_id"])
    assert status["state"] == "blocked"
    assert status["latest_checkpoint"] is not None
    gap_keys = {g["gap_key"] for g in status["coverage_gaps"]}
    assert "analyse-partial" in gap_keys


def test_analyse_invocation_attempt_limit_rejects_excess(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """The 25th invocation attempt is rejected and recorded as a coverage gap."""

    home, store, project = runtime
    repo = _repo(home)
    _discover_campaign, inventory_digest = _seed_discovery(repo, project, pin_count=25)

    plan = _make_plan(
        project,
        stage="analyse",
        limits={
            "repos_max": 12,
            "selected_max": 12,
            "paths_max": 192,
            "duration_seconds": 7_200,
            "concurrent_sub_runs": 2,
            "invocation_attempts": 24,
        },
        budget={
            "requests": 500,
            "bytes": 64 * 1024 * 1024,
            "calls": 25,
            "tokens": 288_000,
        },
        inventory_digest=inventory_digest,
    )

    result = run_radar_campaign(
        store,
        home,
        plan,
        authorized_plan_digest=plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=True,
        analyse_dispatcher=FakeAnalyseDispatcher(),
    )

    assert result["state"] == "blocked"
    campaign_row = repo.get_campaign(result["campaign_id"])
    assert campaign_row.invocation_attempts == 24
    status = repo.campaign_status_document(result["campaign_id"])
    assert status["planned_invocations"] == 25
    assert status["started_invocations"] == 24
    assert status["completed_invocations"] == 24
    gap_keys = {g["gap_key"] for g in status["coverage_gaps"]}
    assert any("invocation-limit" in k for k in gap_keys)


def test_status_document_exposes_invocation_counters(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """planned/started/completed and observed_provider_requests appear in status."""

    home, _store, project = runtime
    repo = _repo(home)
    _discover_campaign, inventory_digest = _seed_discovery(repo, project, pin_count=2)

    plan = _make_plan(
        project,
        stage="analyse",
        limits={
            "repos_max": 12,
            "selected_max": 12,
            "paths_max": 192,
            "duration_seconds": 7_200,
            "concurrent_sub_runs": 2,
            "invocation_attempts": 72,
        },
        budget={
            "requests": 500,
            "bytes": 64 * 1024 * 1024,
            "calls": 24,
            "tokens": 288_000,
        },
        inventory_digest=inventory_digest,
    )
    campaign = _create_campaign(repo, plan)

    class TelemetryDispatcher:
        def dispatch(self, question: Any, snapshots: tuple[Any, ...]) -> AnalyseResult:
            result = FakeAnalyseDispatcher().dispatch(question, snapshots)
            # Return a new result with measured provider requests.
            return AnalyseResult(
                question_id=result.question_id,
                outcome=result.outcome,
                findings=result.findings,
                verification=result.verification,
                conflicts=result.conflicts,
                non_success=result.non_success,
                measured_tokens=result.measured_tokens,
                measured_provider_requests=3,
                measured_latency_ms=result.measured_latency_ms,
                measured_cost_units=result.measured_cost_units,
                missing_measurement_reason=result.missing_measurement_reason,
            )

    _run_analyse(repo, campaign.id, plan, analyse_dispatcher=TelemetryDispatcher())

    status = repo.campaign_status_document(campaign.campaign_id)
    assert status["planned_invocations"] == 2
    assert status["started_invocations"] == 2
    assert status["completed_invocations"] == 2
    assert status["observed_provider_requests"] == 6
