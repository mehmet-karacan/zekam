"""WP-04: Bounded engineering radar campaign unit tests."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from zekam.application.home import HomeLayout
from zekam.application.operational_store import OperationalStore
from zekam.application.research_campaign_runtime import (
    CampaignPlan,
    _build_sub_questions,
    _extract_measurements,
    _is_no_progress,
    _strongest_snapshot,
    _transform_and_save_analyse,
    build_radar_plan,
    run_radar_campaign,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.radar_candidate import RadarCandidateDecision, decide_automatic
from zekam.domain.research import ResearchBudget, ResearchQuestion, SourceKind, SourcePolicy
from zekam.infrastructure.github_radar_adapter import (
    FetchReceipt,
    GitHubInventory,
    InventoryState,
    PinnedCommit,
    RepositoryRecord,
    SourceBlob,
)
from zekam.infrastructure.radar_analyse_dispatcher import FakeAnalyseDispatcher
from zekam.infrastructure.radar_campaign_store import RadarCampaignRepository
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.unit


class FakeGitHubAdapter:
    """Offline GitHub radar adapter with deterministic pagination and faults."""

    def __init__(
        self,
        *,
        duplicate_id: bool = False,
        fault_on_page: int | None = None,
        empty: bool = False,
    ) -> None:
        self._duplicate_id = duplicate_id
        self._fault_on_page = fault_on_page
        self._empty = empty
        self.calls: list[tuple[str, str]] = []
        self._request_count = 0

    def discover_organization(self, owner: str, *, max_repos: int = 10_000) -> GitHubInventory:
        self.calls.append(("discover", owner))
        self._request_count += 1
        if self._fault_on_page == 1:
            return GitHubInventory(
                owner=owner,
                repositories=(),
                state=InventoryState.PARTIAL,
                pages_fetched=0,
                total_requests=self._request_count,
                total_response_bytes=0,
                receipts=(),
                next_safe_action="retry",
            )
        if self._empty:
            return GitHubInventory(
                owner=owner,
                repositories=(),
                state=InventoryState.COMPLETE,
                pages_fetched=1,
                total_requests=self._request_count,
                total_response_bytes=2,
                receipts=(
                    FetchReceipt(
                        "GET",
                        f"/orgs/{owner}/repos",
                        200,
                        2,
                        observed_at="2026-01-01T00:00:00Z",
                    ),
                ),
                next_safe_action="analyse",
            )
        repo_id = 1000 + hash(owner) % 1000
        repos = [
            RepositoryRecord(
                repository_id=repo_id,
                owner=owner,
                name="openai-agents-python" if owner == "openai" else "gemini-cli",
                full_name=f"{owner}/primary",
                default_branch="main",
                visibility="public",
                fork=False,
                archived=False,
                disabled=False,
            )
        ]
        if self._duplicate_id:
            repos.append(
                RepositoryRecord(
                    repository_id=repo_id,
                    owner=owner,
                    name="duplicate",
                    full_name=f"{owner}/duplicate",
                    default_branch="main",
                    visibility="public",
                    fork=False,
                    archived=False,
                    disabled=False,
                )
            )
        if self._fault_on_page == 2:
            # Second page would fault; return partial with first page.
            return GitHubInventory(
                owner=owner,
                repositories=tuple(repos),
                state=InventoryState.PARTIAL,
                pages_fetched=1,
                total_requests=self._request_count,
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
                next_safe_action="retry",
            )
        return GitHubInventory(
            owner=owner,
            repositories=tuple(repos),
            state=InventoryState.COMPLETE,
            pages_fetched=1,
            total_requests=self._request_count,
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


def _plan(
    home: Path,
    store: OperationalStore,
    *,
    stage: str = "discover",
    inventory_digest: str | None = None,
    owners: tuple[str, ...] | None = None,
) -> CampaignPlan:
    return build_radar_plan(
        store,
        home,
        project_ref="demo",
        stage=stage,
        inventory_digest=inventory_digest,
        owners=owners if owners is not None else ("openai", "google-gemini"),
    )


def test_plan_discover_is_read_only(runtime: tuple[Path, OperationalStore, Any]) -> None:
    home, store, _project = runtime
    plan = _plan(home, store)
    assert plan.stage == "discover"
    assert plan.body["dry_run"] is True
    assert plan.body["provider_calls_performed"] == 0
    assert plan.body["grants_authority"] is False


def test_plan_analyse_without_inventory_needs_discovery(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    with pytest.raises(PolicyViolation, match="needs-discovery"):
        _plan(home, store, stage="analyse")


def test_plan_analyse_with_stale_inventory_rejected(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    with pytest.raises(PolicyViolation, match="stale"):
        _plan(home, store, stage="analyse", inventory_digest=digest("never-saved"))


def test_run_discover_saves_inventory_and_manifest(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    plan = _plan(home, store)
    adapter = FakeGitHubAdapter()
    result = run_radar_campaign(
        store,
        home,
        plan,
        authorized_plan_digest=plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )
    assert result["state"] == "completed"
    assert result["replayed"] is False
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    campaign_id = result["campaign_id"]
    inventories = repo.list_inventories(campaign_id)
    assert len(inventories) == 2
    pins = repo.list_pinned_commits(campaign_id)
    assert len(pins) == 2
    manifest = repo.list_source_manifest(campaign_id)
    assert len(manifest) == 2
    assert all(m["complete"] for m in manifest)


def test_run_discover_partial_inventory_keeps_state(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    plan = _plan(home, store)
    adapter = FakeGitHubAdapter(fault_on_page=2)
    result = run_radar_campaign(
        store,
        home,
        plan,
        authorized_plan_digest=plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )
    assert result["state"] == "completed"
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    inventories = repo.list_inventories(result["campaign_id"])
    assert any(i.state == "partial" for i in inventories)


def test_run_discover_deduplicates_duplicate_ids(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    plan = _plan(home, store)
    adapter = FakeGitHubAdapter(duplicate_id=True)
    result = run_radar_campaign(
        store,
        home,
        plan,
        authorized_plan_digest=plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    pins = repo.list_pinned_commits(result["campaign_id"])
    names = {p["name"] for p in pins}
    assert "duplicate" not in names


def test_run_discover_fault_returns_partial_not_empty(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    plan = _plan(home, store)
    adapter = FakeGitHubAdapter(fault_on_page=1)
    result = run_radar_campaign(
        store,
        home,
        plan,
        authorized_plan_digest=plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    inventories = repo.list_inventories(result["campaign_id"])
    assert all(i.state == "partial" for i in inventories)


def test_run_rejects_wrong_plan_digest(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    plan = _plan(home, store)
    with pytest.raises(PolicyViolation, match="exact plan digest"):
        run_radar_campaign(
            store,
            home,
            plan,
            authorized_plan_digest=digest("other"),
            authorize_public_source_read=True,
            authorize_agent_run=False,
        )


def test_run_rejects_missing_authorization(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    plan = _plan(home, store)
    with pytest.raises(PolicyViolation, match="public source read"):
        run_radar_campaign(
            store,
            home,
            plan,
            authorized_plan_digest=plan.plan_digest,
            authorize_public_source_read=False,
            authorize_agent_run=False,
        )


def test_run_replay_idempotent(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    plan = _plan(home, store)
    adapter = FakeGitHubAdapter()
    first = run_radar_campaign(
        store,
        home,
        plan,
        authorized_plan_digest=plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )
    adapter.calls.clear()
    second = run_radar_campaign(
        store,
        home,
        plan,
        authorized_plan_digest=plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )
    assert second["replayed"] is True
    assert second["campaign_id"] == first["campaign_id"]
    # No new remote calls on replay.
    assert adapter.calls == []


def test_run_analyse_without_discover_fails(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    with pytest.raises(PolicyViolation, match="needs-discovery"):
        _plan(home, store, stage="analyse")


def test_run_analyse_uses_inventory_digest(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    discover_plan = _plan(home, store)
    adapter = FakeGitHubAdapter()
    discover_result = run_radar_campaign(
        store,
        home,
        discover_plan,
        authorized_plan_digest=discover_plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    inventories = repo.list_inventories(discover_result["campaign_id"])
    inventory_digest = inventories[0].inventory_digest
    analyse_plan = _plan(home, store, stage="analyse", inventory_digest=inventory_digest)
    analyse_result = run_radar_campaign(
        store,
        home,
        analyse_plan,
        authorized_plan_digest=analyse_plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=True,
        github_adapter=adapter,
        analyse_dispatcher=FakeAnalyseDispatcher(),
    )
    assert analyse_result["state"] == "completed"
    runs = repo.list_sub_research_runs(analyse_result["campaign_id"])
    assert len(runs) > 0


def test_budget_reservation_atomic_blocks_overshoot(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    campaign_id = "camp-1"
    repo.create_campaign(
        project_id="p1",
        campaign_id="camp-1",
        stage="discover",
        state="running",
        plan_digest=digest({}),
        plan={},
        idempotency_key="ik1",
    )
    campaign = repo.get_campaign_by_idempotency_key("ik1")
    assert campaign is not None
    campaign_id = campaign.id
    repo.reserve_budget(campaign_id, "calls", 5, 10)
    repo.reserve_budget(campaign_id, "calls", 5, 10)
    with pytest.raises(PolicyViolation, match="limit"):
        repo.reserve_budget(campaign_id, "calls", 1, 10)


def test_analyse_stale_inventory_rejected(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    with pytest.raises(PolicyViolation, match="stale"):
        _plan(home, store, stage="analyse", inventory_digest=digest("never-saved"))


def test_status_report_candidates_read_only(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    plan = _plan(home, store)
    adapter = FakeGitHubAdapter()
    result = run_radar_campaign(
        store,
        home,
        plan,
        authorized_plan_digest=plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    status = repo.campaign_status_document(result["campaign_id"])
    assert status["read_only"] is True
    assert status["grants_authority"] is False
    report = repo.campaign_report_document(result["campaign_id"])
    assert report["read_only"] is True
    assert report["grants_authority"] is False


def test_sub_questions_do_not_pile_sources(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A26: questions stay per-source and slices are bounded to five."""

    manifest = (
        {"owner": "openai", "name": "codex", "path": "a.md"},
        {"owner": "openai", "name": "codex", "path": "b.md"},
        {"owner": "openai", "name": "codex", "path": "c.md"},
        {"owner": "openai", "name": "codex", "path": "d.md"},
        {"owner": "openai", "name": "codex", "path": "e.md"},
        {"owner": "openai", "name": "codex", "path": "f.md"},
        {"owner": "anthropics", "name": "skills", "path": "x.md"},
        {"owner": "google-gemini", "name": "gemini-cli", "path": "y.md"},
        {"owner": "google-gemini", "name": "other", "path": "z.md"},
    )
    questions = _build_sub_questions("demo", manifest, max_questions=10)
    assert len(questions) == 4
    by_key = {q["repo_key"]: q for q in questions}
    assert by_key["openai/codex"]["slice_count"] == 5
    assert by_key["openai/codex"]["path_overflow"] is True
    assert all(q["source_count"] == 1 for q in questions)


def test_analyse_passes_only_relevant_snapshots(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A26: each sub-question receives only its own source snapshot."""

    home, store, _project = runtime
    discover_plan = _plan(home, store)
    adapter = FakeGitHubAdapter()
    discover_result = run_radar_campaign(
        store,
        home,
        discover_plan,
        authorized_plan_digest=discover_plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    inventories = repo.list_inventories(discover_result["campaign_id"])
    inventory_digest = inventories[0].inventory_digest
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class SnapshotSpy:
        def dispatch(
            self,
            question: Any,
            snapshots: tuple[Any, ...],
        ) -> Any:
            calls.append((question.question_id, snapshots))
            return FakeAnalyseDispatcher().dispatch(question, snapshots)

    analyse_plan = _plan(home, store, stage="analyse", inventory_digest=inventory_digest)
    run_radar_campaign(
        store,
        home,
        analyse_plan,
        authorized_plan_digest=analyse_plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=True,
        github_adapter=adapter,
        analyse_dispatcher=SnapshotSpy(),
    )
    assert len(calls) > 0
    for _question_id, snapshots in calls:
        assert len(snapshots) == 1


def test_no_progress_detection(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A27: three identical completed results stop further attempts."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    campaign = repo.create_campaign(
        project_id="p1",
        campaign_id="camp-no-progress",
        stage="analyse",
        state="running",
        plan_digest=digest({}),
        plan={},
        idempotency_key="ik-no-progress",
    )
    campaign_id = campaign.id
    question: dict[str, Any] = {"question_id": "q1", "repo_key": "openai/codex"}
    same_result: dict[str, Any] = {"finding": "same"}
    for _ in range(3):
        run = repo.save_sub_research_run(campaign_id, question, "running")
        repo.update_sub_research_run(run.id, "completed", result=same_result)
    runs = repo.list_sub_research_runs(campaign_id)
    question_digest = digest(question)
    assert _is_no_progress(runs, question_digest) is True
    assert _is_no_progress(runs, digest({"other": True})) is False


def test_missing_telemetry_is_null_with_reason(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A39: missing token/provider/latency measurements are null + reason."""

    question = ResearchQuestion(
        question_id="q1",
        question="ornek",
        project_ref="demo",
        work_ref="w1",
        intent_digest=digest("intent"),
        source_revision="git-head",
        policy=SourcePolicy(
            allowed_kinds=frozenset({SourceKind.REPOSITORY}),
            allowed_hosts=frozenset(),
            project_scope="demo",
            allow_row_data=False,
        ),
        budget=ResearchBudget(
            max_tokens=12_000,
            max_cost_units=3,
            max_seconds=600,
            max_rounds=1,
        ),
        created_at=dt.datetime.now(dt.UTC),
    )
    result = FakeAnalyseDispatcher().dispatch(question, ())
    measurements = _extract_measurements(result)
    assert measurements["tokens"] is None
    assert measurements["provider_requests"] is None
    assert measurements["latency_ms"] is None
    assert measurements["cost_units"] is None
    assert measurements["missing_reason"] is not None


def test_analyse_budget_consumes_measured_tokens(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A39: measured token telemetry is consumed; missing telemetry is not."""

    home, store, _project = runtime
    discover_plan = _plan(home, store)
    adapter = FakeGitHubAdapter()
    discover_result = run_radar_campaign(
        store,
        home,
        discover_plan,
        authorized_plan_digest=discover_plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    inventories = repo.list_inventories(discover_result["campaign_id"])
    inventory_digest = inventories[0].inventory_digest

    # No telemetry: tokens consumed should stay zero.
    analyse_plan = _plan(
        home,
        store,
        stage="analyse",
        inventory_digest=inventory_digest,
        owners=("openai",),
    )
    no_telemetry = run_radar_campaign(
        store,
        home,
        analyse_plan,
        authorized_plan_digest=analyse_plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=True,
        github_adapter=adapter,
        analyse_dispatcher=FakeAnalyseDispatcher(),
    )
    budget = repo.get_budget(no_telemetry["campaign_id"])
    assert budget.get("tokens", {}).get("consumed", -1) == 0

    # With telemetry: tokens should be consumed from the reservation.
    # Use different owners so the second plan gets its own campaign id.
    analyse_plan2 = _plan(
        home,
        store,
        stage="analyse",
        inventory_digest=inventory_digest,
        owners=("google-gemini",),
    )
    with_telemetry = run_radar_campaign(
        store,
        home,
        analyse_plan2,
        authorized_plan_digest=analyse_plan2.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=True,
        github_adapter=adapter,
        analyse_dispatcher=FakeAnalyseDispatcher(measured_tokens=400),
    )
    budget2 = repo.get_budget(with_telemetry["campaign_id"])
    assert budget2.get("tokens", {}).get("consumed", 0) == 800


def _sub_question() -> dict[str, Any]:
    return {
        "question_id": "radar-sub:abc123",
        "question": "ornek pattern sorusu",
        "repo_key": "openai/codex",
        "source_count": 1,
        "slice_count": 1,
        "path_overflow": False,
    }


def _question() -> ResearchQuestion:
    return ResearchQuestion(
        question_id="radar-sub:abc123",
        question="ornek pattern sorusu",
        project_ref="demo",
        work_ref="w1",
        intent_digest=digest("intent"),
        source_revision="git-head",
        policy=SourcePolicy(
            allowed_kinds=frozenset({SourceKind.REPOSITORY}),
            allowed_hosts=frozenset(),
            project_scope="demo",
            allow_row_data=False,
        ),
        budget=ResearchBudget(
            max_tokens=12_000,
            max_cost_units=3,
            max_seconds=600,
            max_rounds=1,
        ),
        created_at=dt.datetime.now(dt.UTC),
    )


def _analyse_result() -> Any:
    return FakeAnalyseDispatcher().dispatch(_question(), ())


def _source_snapshot(*, path: str = "README.md", **markers: Any) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "repository_id": 1001,
        "owner": "openai",
        "name": "codex",
        "revision": "abc123" * 6,
        "path": path,
        "content_digest": digest("content"),
    }
    snap.update(markers)
    return snap


def _make_analyse_campaign(
    repo: RadarCampaignRepository,
    campaign_id: str,
) -> str:
    created = repo.create_campaign(
        project_id="p1",
        campaign_id=campaign_id,
        stage="analyse",
        state="running",
        plan_digest=digest({}),
        plan={},
        idempotency_key=f"ik-{campaign_id}",
    )
    return created.id


def test_integration_a33_domain_decide_automatic_short_circuits() -> None:
    """A33 (domain): already_satisfied/not_applicable flags short-circuit to
    ALREADY_SATISFIED/NOT_APPLICABLE instead of adding a new module."""

    commons: dict[str, Any] = {
        "card_id": "radar-pattern:x",
        "campaign_id": "camp-x",
        "problem": "ornek",
        "local_evidence": "baseline not measured",
        "upstream_evidence": "source-reviewed",
        "smallest_actionable_solution": "evaluate",
        "affected_logical_resources": ("local-index",),
        "expected_benefit": "unknown",
        "risk": "medium",
        "maintenance_burden": "unknown",
        "dependencies": "fixture",
        "acceptance_test": "tests pass",
        "rollback": "revert",
    }
    satisfied = decide_automatic(
        **commons,
        is_code_or_schema_change=True,
        already_satisfied=True,
    )
    assert satisfied.decision is RadarCandidateDecision.ALREADY_SATISFIED
    applicable = decide_automatic(
        **commons,
        is_code_or_schema_change=True,
        not_applicable=True,
    )
    assert applicable.decision is RadarCandidateDecision.NOT_APPLICABLE


def test_integration_a34_metadata_only_rejects_pattern_card(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A34: README/metadata-only evidence raises PolicyViolation and no pattern
    card is produced (no fake success)."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    campaign_id = _make_analyse_campaign(repo, "camp-a34")
    result = _analyse_result()
    with pytest.raises(PolicyViolation, match="source-reviewed promotion"):
        _transform_and_save_analyse(
            repo,
            campaign_id,
            _sub_question(),
            (_source_snapshot(),),
            result,
        )
    assert repo.list_pattern_cards(campaign_id) == ()


def test_integration_a38_source_available_license_rejected(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A38: a source-available license constraint is rejected even when code
    evidence exists."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    campaign_id = _make_analyse_campaign(repo, "camp-a38")
    snapshot = _source_snapshot(path="src/main.py", license_reuse_constraint="source-available")
    result = _analyse_result()
    with pytest.raises(PolicyViolation, match="reuse-approved"):
        _transform_and_save_analyse(
            repo,
            campaign_id,
            _sub_question(),
            (snapshot,),
            result,
        )
    assert repo.list_pattern_cards(campaign_id) == ()


def test_integration_a33_already_satisfied_short_circuits(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A33: an already-satisfied candidate is decided ALREADY_SATISFIED and
    adds no new module/selection that grants mutation."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    campaign_id = _make_analyse_campaign(repo, "camp-a33")
    snapshot = _source_snapshot(path="src/main.py")
    result = _analyse_result()
    _transform_and_save_analyse(
        repo,
        campaign_id,
        _sub_question(),
        (snapshot,),
        result,
        already_satisfied=True,
        not_applicable=False,
    )
    decisions = repo.list_candidate_decisions(campaign_id)
    assert len(decisions) == 1
    assert decisions[0].decision == RadarCandidateDecision.ALREADY_SATISFIED.value


def test_integration_a33_not_applicable_short_circuits(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A33: not-applicable candidates are decided NOT_APPLICABLE."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    campaign_id = _make_analyse_campaign(repo, "camp-a33na")
    snapshot = _source_snapshot(path="src/main.py")
    result = _analyse_result()
    _transform_and_save_analyse(
        repo,
        campaign_id,
        _sub_question(),
        (snapshot,),
        result,
        already_satisfied=False,
        not_applicable=True,
    )
    decisions = repo.list_candidate_decisions(campaign_id)
    assert len(decisions) == 1
    assert decisions[0].decision == RadarCandidateDecision.NOT_APPLICABLE.value


def test_integration_a35_measured_improvement_without_binding_rejected(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A35: a measured-improvement promotion without a real failure/baseline/
    eval binding is refused with PolicyViolation."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    campaign_id = _make_analyse_campaign(repo, "camp-a35")
    snapshot = _source_snapshot(path="src/main.py")
    result = _analyse_result()
    with pytest.raises(PolicyViolation, match="measured improvement"):
        _transform_and_save_analyse(
            repo,
            campaign_id,
            _sub_question(),
            (snapshot,),
            result,
            promote_measured_improvement=True,
        )
    assert repo.list_candidate_decisions(campaign_id) == ()


def test_integration_guards_keep_campaign_result_safe(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """When guards trigger through the real run flow, the campaign/report
    result stays safe: no pattern card, no fake success, run marked failed."""

    home, store, _project = runtime
    discover_plan = _plan(home, store)
    adapter = FakeGitHubAdapter()
    discover_result = run_radar_campaign(
        store,
        home,
        discover_plan,
        authorized_plan_digest=discover_plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=False,
        github_adapter=adapter,
    )
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    inventories = repo.list_inventories(discover_result["campaign_id"])
    inventory_digest = inventories[0].inventory_digest
    analyse_plan = _plan(
        home,
        store,
        stage="analyse",
        inventory_digest=inventory_digest,
        owners=("openai",),
    )
    analyse_result = run_radar_campaign(
        store,
        home,
        analyse_plan,
        authorized_plan_digest=analyse_plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=True,
        github_adapter=adapter,
        analyse_dispatcher=FakeAnalyseDispatcher(),
    )
    campaign_id = analyse_result["campaign_id"]
    # README snapshot -> A34 refuses -> no pattern card, no fake success.
    assert repo.list_pattern_cards(campaign_id) == ()
    runs = repo.list_sub_research_runs(campaign_id)
    assert len(runs) > 0
    assert all(r.state == "failed" for r in runs)
    report = repo.campaign_report_document(campaign_id)
    assert report["read_only"] is True
    assert report["grants_authority"] is False


def test_strongest_snapshot_prefers_real_source_over_readme() -> None:
    readme = _source_snapshot(path="README.md")
    source = _source_snapshot(path="src/core.py")
    tests_file = _source_snapshot(path="tests/core_test.py")
    assert _strongest_snapshot((readme, source))["path"] == "src/core.py"
    assert _strongest_snapshot((source, readme))["path"] == "src/core.py"
    assert _strongest_snapshot((source, tests_file))["path"] == "tests/core_test.py"
    # README-only stays README-only so the promotion guard still refuses it.
    assert _strongest_snapshot((readme,))["path"] == "README.md"
    assert _strongest_snapshot(()) == {}
