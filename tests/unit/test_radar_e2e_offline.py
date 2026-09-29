"""WP-06 A44: End-to-end offline corpus through typed adapter boundary.

A44: discover -> pinned snapshot -> real typed adapter boundary -> campaign ->
gap -> proposal -> report works; sentinel user files outside source root do not
change.

A23: two project/realm inspecting the same upstream keep candidate/gap/receipt
information isolated.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from zekam.application.home import HomeLayout
from zekam.application.operational_store import OperationalStore
from zekam.application.research_campaign_runtime import (
    build_radar_plan,
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
    FakeAnalyseDispatcher,
)
from zekam.infrastructure.radar_campaign_store import RadarCampaignRepository
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.unit

_FIXED_SHA = "a" * 40


def _seed_source_entry(repo: RadarCampaignRepository, campaign_id: str) -> None:
    """Add a real source-level manifest entry next to the README.

    The analyse promotion guard (A34/A38) refuses README-only evidence, so the
    offline corpus must carry a genuine source file for pattern cards to exist.
    """
    body = "def pattern() -> int:\n    return 1\n"
    internal_id = repo.get_campaign(campaign_id).id
    for pin in repo.list_pinned_commits(campaign_id):
        repo.save_source_manifest(
            internal_id,
            pin["id"],
            (
                {
                    "path": "src/core.py",
                    "blob_sha": "b" * 40,
                    "raw_content_digest": digest(body),
                    "complete": True,
                    "omission_reason": None,
                },
            ),
        )


class _DeterministicGitHubAdapter:
    """Typed fake GitHub adapter returning a deterministic offline corpus."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._request_count = 0

    def discover_organization(self, owner: str, *, max_repos: int = 10_000) -> GitHubInventory:
        self.calls.append(("discover", owner))
        self._request_count += 1
        repo_id = 2000 + hash(owner) % 1000
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
            total_response_bytes=300,
            receipts=(
                FetchReceipt(
                    method="GET",
                    url=f"/orgs/{owner}/repos",
                    status_code=200,
                    response_bytes=300,
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
            commit_sha=_FIXED_SHA,
        )

    def fetch_blob(
        self, pin: PinnedCommit, path: str, *, max_bytes: int = 2 * 1024 * 1024
    ) -> SourceBlob:
        self.calls.append(("fetch_blob", f"{pin.owner}/{pin.name}/{path}"))
        body = f"# {pin.name}\n\nSample README for offline corpus.\n".encode()
        return SourceBlob(
            repository_id=pin.repository_id,
            commit_sha=pin.commit_sha,
            path=path,
            blob_sha=_git_blob_sha(body),
            raw_bytes=body,
            raw_content_digest=digest(body.decode("utf-8")),
            complete=True,
        )


def _git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


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


@pytest.fixture
def second_project_runtime(tmp_path: Path) -> tuple[Path, OperationalStore, Any]:
    layout = HomeLayout(tmp_path / ".zekam-other").ensure()
    layout.ensure_project("other")
    home = layout.root
    database = home / "state" / "operational.db"
    bootstrap(database)
    SQLiteLocalRuntimeStore(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="other", display_name="Other")
        uow.commit()
    return home, store, project


def _sentinel_files(tmp_path: Path) -> tuple[Path, Path]:
    """Create sentinel files outside the Zekam home to verify they are untouched."""
    sentinel_dir = tmp_path / "user-sentinel"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    before = sentinel_dir / "before.txt"
    after = sentinel_dir / "after.txt"
    before.write_text("before", encoding="utf-8")
    after.write_text("after", encoding="utf-8")
    return before, after


def _sentinel_hashes(paths: tuple[Path, ...]) -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_e2e_offline_discover_analyse_report(runtime: tuple[Path, OperationalStore, Any]) -> None:
    home, store, _project = runtime
    before, after = _sentinel_files(home.parent)
    before_hashes = _sentinel_hashes((before, after))

    discover_plan = build_radar_plan(
        store, home, project_ref="demo", stage="discover", owners=("openai",)
    )
    adapter = _DeterministicGitHubAdapter()
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
    assert discover_result["replayed"] is False

    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    inventories = repo.list_inventories(discover_result["campaign_id"])
    assert len(inventories) == 1
    inventory_digest = inventories[0].inventory_digest

    pins = repo.list_pinned_commits(discover_result["campaign_id"])
    assert len(pins) == 1
    assert pins[0]["commit_sha"] == _FIXED_SHA

    manifest = repo.list_source_manifest(discover_result["campaign_id"])
    assert len(manifest) == 1
    assert manifest[0]["path"] == "README.md"
    assert manifest[0]["complete"] is True

    _seed_source_entry(repo, discover_result["campaign_id"])
    analyse_plan = build_radar_plan(
        store,
        home,
        project_ref="demo",
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
    assert analyse_result["state"] == "completed"

    patterns = repo.list_pattern_cards(analyse_result["campaign_id"])
    decisions = repo.list_candidate_decisions(analyse_result["campaign_id"])
    assert len(patterns) >= 1
    assert len(decisions) >= 1
    assert all(d.decision == "rejected-risk" for d in decisions)

    report = repo.campaign_report_document(analyse_result["campaign_id"])
    assert report["schema"] == "zekam-radar-campaign-report/v1"
    assert report["read_only"] is True
    assert report["grants_authority"] is False
    # selected_repos are reported from the source campaign; analysis stage
    # does not repin, so the list is empty in the analyse campaign report.
    source_report = repo.campaign_report_document(discover_result["campaign_id"])
    assert len(source_report["selected_repos"]) == 1

    candidates = repo.candidates_document(analyse_result["campaign_id"])
    assert candidates["read_only"] is True
    assert candidates["grants_authority"] is False
    assert len(candidates["pattern_cards"]) >= 1

    # A44: sentinel files outside the source root/home must not change.
    after_hashes = _sentinel_hashes((before, after))
    assert after_hashes == before_hashes


def test_two_projects_same_upstream_keep_isolation(
    runtime: tuple[Path, OperationalStore, Any],
    second_project_runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A23: same upstream inventory must not leak private gap/candidate/receipts."""

    home1, store1, _project1 = runtime
    home2, store2, _project2 = second_project_runtime

    def run_discovery_and_analysis(
        home: Path, store: OperationalStore, project_ref: str
    ) -> tuple[str, RadarCampaignRepository]:
        plan = build_radar_plan(
            store, home, project_ref=project_ref, stage="discover", owners=("openai",)
        )
        adapter = _DeterministicGitHubAdapter()
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
        inventory_digest = repo.list_inventories(result["campaign_id"])[0].inventory_digest
        _seed_source_entry(repo, result["campaign_id"])
        analyse_plan = build_radar_plan(
            store,
            home,
            project_ref=project_ref,
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
        return analyse_result["campaign_id"], repo

    campaign1, repo1 = run_discovery_and_analysis(home1, store1, "demo")
    campaign2, repo2 = run_discovery_and_analysis(home2, store2, "other")

    # A23: campaign IDs differ because the project slug is part of the plan intent.
    assert campaign1 != campaign2

    # A23: private candidate/gap/receipt information must not cross project bounds.
    decisions1 = {d.selection_id for d in repo1.list_candidate_decisions(campaign1)}
    decisions2 = {d.selection_id for d in repo2.list_candidate_decisions(campaign2)}
    assert not decisions1.intersection(decisions2)

    gaps1 = {g.card_id for g in repo1.list_gap_cards(campaign1)}
    gaps2 = {g.card_id for g in repo2.list_gap_cards(campaign2)}
    assert not gaps1.intersection(gaps2)

    # Pattern card IDs are deterministic from the question digest, so identical
    # upstreams produce identical card_ids.  Isolation is enforced by the
    # campaign_id foreign key in separate DB files, not by globally unique IDs.
    patterns1 = repo1.list_pattern_cards(campaign1)
    patterns2 = repo2.list_pattern_cards(campaign2)
    assert len(patterns1) == 1
    assert len(patterns2) == 1
    assert patterns1[0].card_id == patterns2[0].card_id
    assert patterns1[0].id != patterns2[0].id
    assert patterns1[0].campaign_id != patterns2[0].campaign_id

    # Databases are physically separate.
    assert str(home1) != str(home2)
    assert home1 / "state" / "radar-campaigns.db" != home2 / "state" / "radar-campaigns.db"
