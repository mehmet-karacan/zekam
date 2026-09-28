"""WP-06: Radar offline corpus E2E without network or providers.

This end-to-end test exercises the bounded research radar pipeline using only
in-memory / deterministic adapters:

1. Bootstraps a temporary ``ZEKAM_HOME`` and SQLite operational store.
2. Runs a discover campaign with ``FakeGitHubAdapter``.
3. Uses the resulting inventory digest to run an analyse campaign with
   ``FakeAnalyseDispatcher``.
4. Reads candidate, status and report documents (all read-only).
5. Bridges one candidate selection to a real ``ImprovementCandidate`` using
   genuine local evidence digests.
6. Runs ``RadarInternalScanner`` over the actual Zekam ``domain`` package to
   produce at least one internal gap card.
7. Verifies sentinel user files outside the temporary home are untouched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from zekam.application.code_graph import apply_graph_build, graph_store_path, plan_graph_build
from zekam.application.code_graph_python import PythonAstExtractor
from zekam.application.home import HomeLayout
from zekam.application.operational_store import OperationalStore
from zekam.application.radar_evolution_bridge import RadarEvolutionBridge
from zekam.application.radar_internal_scanner import RadarInternalScanner
from zekam.application.research_campaign_runtime import (
    build_radar_plan,
    run_radar_campaign,
)
from zekam.domain.canonical import digest
from zekam.domain.improvement_policy import ImprovementChangeClass
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
from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeGitHubAdapter:
    """Deterministic offline GitHub adapter returning one repo per owner."""

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
                    name="openai-agents-python" if owner == "openai" else "gemini-cli",
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
                    method="GET",
                    url=f"/orgs/{owner}/repos",
                    status_code=200,
                    response_bytes=200,
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
    """Temporary ZEKAM_HOME with bootstrapped operational DB and demo project."""

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


def _sentinel_files(tmp_path: Path) -> tuple[Path, Path]:
    """Create sentinel files outside the temporary ZEKAM_HOME."""

    sentinel_dir = tmp_path / "user-sentinel"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    before = sentinel_dir / "before.txt"
    after = sentinel_dir / "after.txt"
    before.write_text("before", encoding="utf-8")
    after.write_text("after", encoding="utf-8")
    return before, after


def _sentinel_hashes(paths: tuple[Path, ...]) -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_radar_offline_corpus_discover_analyse_bridge_and_scan(
    tmp_path: Path,
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """Full offline radar corpus scenario with real evidence digests."""

    home, store, _project = runtime
    before, after = _sentinel_files(home.parent)
    before_hashes = _sentinel_hashes((before, after))

    # 1. Discover stage
    discover_plan = build_radar_plan(
        store,
        home,
        project_ref="demo",
        stage="discover",
        owners=("openai", "google-gemini"),
    )
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
    assert discover_result["state"] == "completed"

    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    inventories = repo.list_inventories(discover_result["campaign_id"])
    assert len(inventories) >= 1
    inventory_digest = inventories[0].inventory_digest

    # 2. Analyse stage using the discovered inventory digest
    analyse_plan = build_radar_plan(
        store,
        home,
        project_ref="demo",
        stage="analyse",
        inventory_digest=inventory_digest,
        owners=("openai", "google-gemini"),
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
    assert analyse_result["state"] == "completed"
    campaign_id = analyse_result["campaign_id"]

    # 3. Read-only candidate document
    candidates_document = repo.candidates_document(campaign_id)
    assert candidates_document["read_only"] is True
    assert candidates_document["grants_authority"] is False
    assert (
        len(candidates_document.get("pattern_cards", ())) >= 1
        or len(candidates_document.get("gap_cards", ())) >= 1
    )

    # 4. Bridge one candidate selection to a real ImprovementCandidate
    decisions = repo.list_candidate_decisions(campaign_id)
    assert len(decisions) >= 1
    row = decisions[0]
    selection = RadarCandidateSelection(
        selection_id=row.selection_id,
        campaign_id=row.campaign_id,
        card_id=row.card_id,
        decision=RadarCandidateDecision(row.decision),
        problem=row.problem,
        local_evidence=row.local_evidence,
        upstream_evidence=row.upstream_evidence,
        smallest_actionable_solution=row.smallest_actionable_solution,
        affected_logical_resources=tuple(
            json.loads(row.affected_logical_resources_json).get("affected_logical_resources", [])
        ),
        expected_benefit=row.expected_benefit,
        risk=row.risk,
        maintenance_burden=row.maintenance_burden,
        dependencies=row.dependencies,
        acceptance_test=row.acceptance_test,
        rollback=row.rollback,
        existing_decision=row.existing_decision,
        existing_campaign_id=row.existing_campaign_id,
    )
    bridge = RadarEvolutionBridge()
    candidate = bridge.to_improvement_candidate(
        selection,
        local_failure_card_digest=digest({}),
        local_baseline_aggregate_digest=digest({"baseline": True}),
        evaluation_dataset_contract_digest=digest("dataset-contract"),
    )
    assert candidate.change_class is not ImprovementChangeClass.AUTO_SAFE
    assert candidate.change_class in {
        ImprovementChangeClass.REVIEW_REQUIRED,
        ImprovementChangeClass.HUMAN_APPROVAL_REQUIRED,
    }
    assert candidate.failure_card_digest == digest({})
    assert candidate.baseline_aggregate_digest == digest({"baseline": True})

    # 5. Internal scanner over the real Zekam domain package
    source_root = REPO_ROOT / "src" / "zekam" / "domain"
    extractor = PythonAstExtractor()
    plan = plan_graph_build(
        source_root,
        project_id="demo",
        project_slug="demo",
        source_revision="test-rev",
        extractor=extractor,
        created_at="2026-01-01T00:00:00Z",
    )
    graph_db_path = graph_store_path(home, "demo")
    graph_store = SQLiteCodeGraphStore(graph_db_path, create=True, read_only=False)
    try:
        apply_graph_build(graph_store, extractor, source_root, plan)
        scanner = RadarInternalScanner(graph_store)
        internal_cards = scanner.scan_for_gaps("demo")
    finally:
        graph_store.close()
    assert len(internal_cards) >= 1
    assert all(card.kind.value == "gap" for card in internal_cards)

    # 6. Read-only status and report documents
    status_document = repo.campaign_status_document(campaign_id)
    assert status_document["read_only"] is True
    assert status_document["grants_authority"] is False

    report_document = repo.campaign_report_document(campaign_id)
    assert report_document["read_only"] is True
    assert report_document["grants_authority"] is False

    # 7. Sentinel files outside the temporary home must not change
    after_hashes = _sentinel_hashes((before, after))
    assert after_hashes == before_hashes
