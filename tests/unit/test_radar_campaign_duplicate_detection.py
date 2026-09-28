"""A36: Cross-campaign duplicate detection links candidates to prior provenance."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zekam.application.home import HomeLayout
from zekam.application.operational_store import OperationalStore
from zekam.application.research_campaign_runtime import (
    _transform_and_save_analyse,
    build_radar_plan,
    run_radar_campaign,
)
from zekam.domain.canonical import digest
from zekam.domain.radar_candidate import RadarCandidateDecision, RadarCandidateSelection
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


class _FakeGitHubAdapter:
    """Offline adapter that returns one repo per owner."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._request_count = 0

    def discover_organization(self, owner: str, *, max_repos: int = 10_000) -> GitHubInventory:
        self.calls.append(("discover", owner))
        self._request_count += 1
        repo_id = 1000 + hash(owner) % 1000
        return GitHubInventory(
            owner=owner,
            repositories=(
                RepositoryRecord(
                    repository_id=repo_id,
                    owner=owner,
                    name="codex" if owner == "openai" else "gemini-cli",
                    full_name=f"{owner}/primary",
                    default_branch="main",
                    visibility="public",
                    fork=False,
                    archived=False,
                    disabled=False,
                ),
            ),
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


def _run_discover(
    home: Path,
    store: OperationalStore,
    adapter: _FakeGitHubAdapter,
    owners: tuple[str, ...],
) -> str:
    plan = build_radar_plan(
        store,
        home,
        project_ref="demo",
        stage="discover",
        owners=owners,
    )
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
    return str(result["campaign_id"])


def _run_analyse(
    home: Path,
    store: OperationalStore,
    adapter: _FakeGitHubAdapter,
    inventory_digest: str,
    owners: tuple[str, ...],
) -> str:
    plan = build_radar_plan(
        store,
        home,
        project_ref="demo",
        stage="analyse",
        inventory_digest=inventory_digest,
        owners=owners,
    )
    result = run_radar_campaign(
        store,
        home,
        plan,
        authorized_plan_digest=plan.plan_digest,
        authorize_public_source_read=True,
        authorize_agent_run=True,
        github_adapter=adapter,
        analyse_dispatcher=FakeAnalyseDispatcher(),
    )
    assert result["state"] == "completed"
    return str(result["campaign_id"])


def test_duplicate_selection_found_across_campaigns(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A36: a candidate seen in an earlier campaign is marked DUPLICATE with provenance."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    for campaign_id in ("camp-first", "camp-second"):
        repo.create_campaign(
            project_id="p1",
            campaign_id=campaign_id,
            stage="analyse",
            state="running",
            plan_digest=digest({"campaign": campaign_id}),
            plan={"campaign": campaign_id},
            idempotency_key=f"ik-{campaign_id}",
        )

    sub_question = {"question_id": "q-1", "repo_key": "openai/codex"}
    snapshots = (
        {
            "snapshot_id": "openai/codex:README.md",
            "repository_id": 1,
            "owner": "openai",
            "name": "codex",
            "revision": "abc123" * 6,
            "path": "README.md",
            "content_digest": digest("readme"),
        },
    )
    result = SimpleNamespace(outcome="success")

    _transform_and_save_analyse(repo, "camp-first", sub_question, snapshots, result)
    first_decisions = repo.list_candidate_decisions("camp-first")
    assert len(first_decisions) == 1
    first_selection_id = first_decisions[0].selection_id

    _transform_and_save_analyse(repo, "camp-second", sub_question, snapshots, result)
    second_decisions = repo.list_candidate_decisions("camp-second")
    assert len(second_decisions) == 1
    assert second_decisions[0].decision == "duplicate"
    assert first_selection_id in second_decisions[0].problem
    assert "camp-first" in second_decisions[0].problem
    assert second_decisions[0].existing_decision == "rejected-risk"
    assert second_decisions[0].existing_campaign_id == "camp-first"


def test_different_selection_digest_is_not_duplicate(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A36: a candidate with a different selection digest is not marked duplicate."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    for campaign_id in ("camp-first", "camp-other"):
        repo.create_campaign(
            project_id="p1",
            campaign_id=campaign_id,
            stage="analyse",
            state="running",
            plan_digest=digest({"campaign": campaign_id}),
            plan={"campaign": campaign_id},
            idempotency_key=f"ik-{campaign_id}",
        )

    sub_question = {"question_id": "q-1", "repo_key": "openai/codex"}
    snapshots = (
        {
            "snapshot_id": "openai/codex:README.md",
            "repository_id": 1,
            "owner": "openai",
            "name": "codex",
            "revision": "abc123" * 6,
            "path": "README.md",
            "content_digest": digest("readme"),
        },
    )
    result = SimpleNamespace(outcome="success")

    _transform_and_save_analyse(repo, "camp-first", sub_question, snapshots, result)
    first_selection_digest = repo.list_candidate_decisions("camp-first")[0].selection_digest

    other_question = {"question_id": "q-2", "repo_key": "google-gemini/gemini-cli"}
    other_snapshots = (
        {
            "snapshot_id": "google-gemini/gemini-cli:README.md",
            "repository_id": 2,
            "owner": "google-gemini",
            "name": "gemini-cli",
            "revision": "def456" * 6,
            "path": "README.md",
            "content_digest": digest("other readme"),
        },
    )
    _transform_and_save_analyse(repo, "camp-other", other_question, other_snapshots, result)
    other_decisions = repo.list_candidate_decisions("camp-other")
    assert not any(d.selection_digest == first_selection_digest for d in other_decisions)
    assert not any(d.decision == "duplicate" for d in other_decisions)


def test_find_duplicate_selection_digest_returns_most_recent(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A36: when multiple prior decisions share a digest, the newest is returned."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")

    for idx, campaign_id in enumerate(("camp-older", "camp-newer")):
        repo.create_campaign(
            project_id="p1",
            campaign_id=campaign_id,
            stage="analyse",
            state="running",
            plan_digest=digest({"idx": idx}),
            plan={"idx": idx},
            idempotency_key=f"ik-{campaign_id}",
        )

    base = RadarCandidateSelection(
        selection_id="sel-shared",
        campaign_id="camp-older",
        card_id="card-1",
        decision=RadarCandidateDecision.REJECTED_RISK,
        problem="shared",
        local_evidence="e",
        upstream_evidence="e",
        smallest_actionable_solution="s",
        affected_logical_resources=("local-index",),
        expected_benefit="b",
        risk="low",
        maintenance_burden="m",
        dependencies="d",
        acceptance_test="a",
        rollback="r",
    )
    shared_digest = base.selection_digest

    repo.save_candidate_decision("camp-older", base)
    repo.save_candidate_decision(
        "camp-newer",
        RadarCandidateSelection(
            selection_id="sel-shared",
            campaign_id="camp-newer",
            card_id="card-1",
            decision=RadarCandidateDecision.REJECTED_RISK,
            problem="shared",
            local_evidence="e",
            upstream_evidence="e",
            smallest_actionable_solution="s",
            affected_logical_resources=("local-index",),
            expected_benefit="b",
            risk="low",
            maintenance_burden="m",
            dependencies="d",
            acceptance_test="a",
            rollback="r",
        ),
    )

    duplicate = repo.find_duplicate_selection_digest(shared_digest)
    assert duplicate is not None
    newer_campaign = repo.get_campaign("camp-newer")
    assert duplicate.campaign_id == newer_campaign.id
