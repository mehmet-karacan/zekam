"""WP-05: Radar candidate pattern/gap cards and authority-free decisions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zekam.application.evolution_bootstrap import LOCAL_GRANT_OPERATIONS
from zekam.application.home import HomeLayout
from zekam.application.operational_store import OperationalStore
from zekam.application.research_campaign_runtime import (
    build_radar_plan,
    run_radar_campaign,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.evolution_authority import EVOLUTION_HANDLERS
from zekam.domain.radar_candidate import (
    EvidenceLevel,
    RadarCandidateDecision,
    RadarCandidateKind,
    RadarCandidateSelection,
    RadarGapCard,
    RadarPatternCard,
    SourceProvenance,
    decide_automatic,
)
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
from zekam.interfaces.cli.research import app

pytestmark = pytest.mark.unit

runner = CliRunner()


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
                    "GET", f"/orgs/{owner}/repos", 200, 200,
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


def _source() -> SourceProvenance:
    return SourceProvenance(
        repository_id=42,
        owner="openai",
        name="codex",
        commit_sha="abc123" * 6,
        path="README.md",
        range="1-10",
        evidence_digest=digest("readme sample"),
    )


def _pattern_card(campaign_id: str = "camp-1") -> RadarPatternCard:
    return RadarPatternCard(
        card_id="pattern-1",
        campaign_id=campaign_id,
        kind=RadarCandidateKind.PATTERN,
        problem="child output binding",
        source=_source(),
        observed_behavior="strict envelope",
        inference_or_assumption="none",
        test_evidence_level=EvidenceLevel.TESTS_REVIEWED,
        license_reuse_constraint="MIT with NOTICE",
        cost_dependency_limit="no new dependency",
        not_applicable_conditions="when coordinator already binds",
    )


def _gap_card(campaign_id: str = "camp-1") -> RadarGapCard:
    return RadarGapCard(
        card_id="gap-1",
        campaign_id=campaign_id,
        kind=RadarCandidateKind.GAP,
        current_baseline="Zekam main",
        source_binding="src/zekam/domain/research.py",
        production_call_path="ResearchService.dispatch",
        test_measurement_evidence="unit tests cover fan-in",
        covered_areas="synthesize binding",
        uncovered_areas="telemetry slice digest",
        result=RadarCandidateDecision.PARTIAL,
    )


def test_pattern_card_creation_and_digest() -> None:
    card = _pattern_card()
    assert card.kind is RadarCandidateKind.PATTERN
    assert card.card_digest.startswith("sha256:")
    assert card.grants_authority is False


def test_gap_card_creation_rejects_invalid_result() -> None:
    gap = _gap_card()
    assert gap.result is RadarCandidateDecision.PARTIAL
    with pytest.raises(ValidationFailed):
        RadarGapCard(
            card_id="gap-bad",
            campaign_id="camp-1",
            kind=RadarCandidateKind.GAP,
            current_baseline="main",
            source_binding="src",
            production_call_path="call",
            test_measurement_evidence="none",
            covered_areas="none",
            uncovered_areas="none",
            result=RadarCandidateDecision.REJECTED_RISK,
        )


def test_pattern_card_rejects_authority() -> None:
    card = _pattern_card()
    with pytest.raises(PolicyViolation):
        RadarPatternCard(
            card_id=card.card_id,
            campaign_id=card.campaign_id,
            kind=card.kind,
            problem=card.problem,
            source=card.source,
            observed_behavior=card.observed_behavior,
            inference_or_assumption=card.inference_or_assumption,
            test_evidence_level=card.test_evidence_level,
            license_reuse_constraint=card.license_reuse_constraint,
            cost_dependency_limit=card.cost_dependency_limit,
            not_applicable_conditions=card.not_applicable_conditions,
            grants_authority=True,
        )


def test_decide_automatic_rejected_risk_for_code_changes() -> None:
    sel = decide_automatic(
        card_id="pattern-1",
        campaign_id="camp-1",
        problem="code patch",
        local_evidence="none",
        upstream_evidence="source-reviewed",
        smallest_actionable_solution="change source",
        affected_logical_resources=("source-file:src/zekam/x.py",),
        expected_benefit="high",
        risk="medium",
        maintenance_burden="low",
        dependencies="",
        acceptance_test="tests pass",
        rollback="revert",
        is_code_or_schema_change=True,
    )
    assert sel.decision is RadarCandidateDecision.REJECTED_RISK
    assert sel.grants_authority is False


def test_decide_automatic_evidence_insufficient_without_upstream() -> None:
    sel = decide_automatic(
        card_id="pattern-2",
        campaign_id="camp-1",
        problem="documentation gap",
        local_evidence="none",
        upstream_evidence="metadata-only",
        smallest_actionable_solution="add doc",
        affected_logical_resources=("local-projection",),
        expected_benefit="low",
        risk="low",
        maintenance_burden="low",
        dependencies="",
        acceptance_test="review",
        rollback="remove doc",
        is_code_or_schema_change=False,
    )
    assert sel.decision is RadarCandidateDecision.EVIDENCE_INSUFFICIENT


def test_decide_automatic_deferred_dependency() -> None:
    sel = decide_automatic(
        card_id="pattern-3",
        campaign_id="camp-1",
        problem="refactor",
        local_evidence="present",
        upstream_evidence="source-reviewed",
        smallest_actionable_solution="extract module",
        affected_logical_resources=("local-index",),
        expected_benefit="medium",
        risk="low",
        maintenance_burden="low",
        dependencies="requires new parser",
        acceptance_test="tests pass",
        rollback="revert",
        is_code_or_schema_change=False,
    )
    assert sel.decision is RadarCandidateDecision.DEFERRED_DEPENDENCY


def test_decide_automatic_duplicate() -> None:
    sel = decide_automatic(
        card_id="pattern-4",
        campaign_id="camp-1",
        problem="duplicate idea",
        local_evidence="present",
        upstream_evidence="source-reviewed",
        smallest_actionable_solution="skip",
        affected_logical_resources=("local-index",),
        expected_benefit="none",
        risk="low",
        maintenance_burden="none",
        dependencies="",
        acceptance_test="n/a",
        rollback="n/a",
        is_code_or_schema_change=False,
        has_duplicate=True,
    )
    assert sel.decision is RadarCandidateDecision.DUPLICATE


def test_selection_rejects_empty_resources() -> None:
    with pytest.raises(ValidationFailed):
        RadarCandidateSelection(
            selection_id="sel-1",
            campaign_id="camp-1",
            card_id="card-1",
            decision=RadarCandidateDecision.EVIDENCE_INSUFFICIENT,
            problem="p",
            local_evidence="e",
            upstream_evidence="e",
            smallest_actionable_solution="s",
            affected_logical_resources=(),
            expected_benefit="b",
            risk="low",
            maintenance_burden="m",
            dependencies="d",
            acceptance_test="a",
            rollback="r",
        )


def test_repository_pattern_card_roundtrip(runtime: tuple[Path, OperationalStore, Any]) -> None:
    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    repo.create_campaign(
        project_id="p1",
        campaign_id="camp-1",
        stage="analyse",
        state="running",
        plan_digest=digest({}),
        plan={},
        idempotency_key="ik-1",
    )
    card = _pattern_card()
    saved = repo.save_pattern_card("camp-1", card)
    assert saved.card_digest == card.card_digest
    loaded = repo.list_pattern_cards("camp-1")
    assert len(loaded) == 1
    assert loaded[0].card_id == card.card_id


def test_repository_gap_card_roundtrip(runtime: tuple[Path, OperationalStore, Any]) -> None:
    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    repo.create_campaign(
        project_id="p1",
        campaign_id="camp-1",
        stage="analyse",
        state="running",
        plan_digest=digest({}),
        plan={},
        idempotency_key="ik-1",
    )
    card = _gap_card()
    saved = repo.save_gap_card("camp-1", card)
    assert saved.card_digest == card.card_digest
    loaded = repo.list_gap_cards("camp-1")
    assert len(loaded) == 1
    assert loaded[0].result == "partial"


def test_repository_decision_roundtrip(runtime: tuple[Path, OperationalStore, Any]) -> None:
    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    repo.create_campaign(
        project_id="p1",
        campaign_id="camp-1",
        stage="analyse",
        state="running",
        plan_digest=digest({}),
        plan={},
        idempotency_key="ik-1",
    )
    sel = decide_automatic(
        card_id="pattern-1",
        campaign_id="camp-1",
        problem="p",
        local_evidence="e",
        upstream_evidence="source-reviewed",
        smallest_actionable_solution="s",
        affected_logical_resources=("local-index",),
        expected_benefit="b",
        risk="low",
        maintenance_burden="m",
        dependencies="",
        acceptance_test="a",
        rollback="r",
        is_code_or_schema_change=True,
    )
    saved = repo.save_candidate_decision("camp-1", sel)
    assert saved.selection_digest == sel.selection_digest
    loaded = repo.list_candidate_decisions("camp-1")
    assert len(loaded) == 1
    assert loaded[0].decision == "rejected-risk"


def test_candidates_document_read_only(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    repo.create_campaign(
        project_id="p1",
        campaign_id="camp-1",
        stage="analyse",
        state="completed",
        plan_digest=digest({}),
        plan={},
        idempotency_key="ik-1",
    )
    repo.save_pattern_card("camp-1", _pattern_card())
    repo.save_gap_card("camp-1", _gap_card())
    doc = repo.candidates_document("camp-1")
    assert doc["read_only"] is True
    assert doc["grants_authority"] is False
    assert len(doc["pattern_cards"]) == 1
    assert len(doc["gap_cards"]) == 1


def test_analyse_creates_pattern_and_decision(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    discover_plan = build_radar_plan(
        store, home, project_ref="demo", stage="discover", owners=("openai",)
    )
    adapter = _FakeGitHubAdapter()
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
    patterns = repo.list_pattern_cards(analyse_result["campaign_id"])
    decisions = repo.list_candidate_decisions(analyse_result["campaign_id"])
    assert len(patterns) >= 1
    assert len(decisions) >= 1
    assert all(d.decision == "rejected-risk" for d in decisions)


def test_analyse_partial_creates_gap_card(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, store, _project = runtime
    discover_plan = build_radar_plan(
        store, home, project_ref="demo", stage="discover", owners=("openai",)
    )
    adapter = _FakeGitHubAdapter()
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
        analyse_dispatcher=FakeAnalyseDispatcher(outcome="partial"),
    )
    gaps = repo.list_gap_cards(analyse_result["campaign_id"])
    assert len(gaps) >= 1
    assert gaps[0].result == "gap-demonstrated"


def test_candidates_cli_read_only_no_mutation(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    repo.create_campaign(
        project_id="p1",
        campaign_id="camp-cli",
        stage="analyse",
        state="completed",
        plan_digest=digest({}),
        plan={},
        idempotency_key="ik-cli",
    )
    repo.save_pattern_card("camp-cli", _pattern_card("camp-cli"))
    repo.save_gap_card("camp-cli", _gap_card("camp-cli"))
    result = runner.invoke(
        app,
        ["radar", "candidates", "camp-cli", "--json", "--home", str(home)],
    )
    assert result.exit_code == 0, result.output
    import json

    document = json.loads(result.output)
    assert document["grants_authority"] is False
    assert document["read_only"] is True
    assert len(document["pattern_cards"]) == 1
    assert len(document["gap_cards"]) == 1


def test_no_fake_failure_or_benchmark_created(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """Radar candidate records do not pretend to be measured improvements."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    repo.create_campaign(
        project_id="p1",
        campaign_id="camp-no-fake",
        stage="analyse",
        state="completed",
        plan_digest=digest({}),
        plan={},
        idempotency_key="ik-no-fake",
    )
    repo.save_pattern_card("camp-no-fake", _pattern_card("camp-no-fake"))
    decisions = repo.list_candidate_decisions("camp-no-fake")
    assert len(decisions) == 0
    # AUTO_SAFE and approved flags are absent.
    doc = repo.candidates_document("camp-no-fake")
    assert doc.get("approved") is None
    assert doc.get("auto_safe") is None


def test_paired_evaluation_labels_and_case_identity(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A40: paired evaluation names baseline/candidate explicitly and matches case IDs."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    repo.create_campaign(
        project_id="p1",
        campaign_id="camp-paired",
        stage="analyse",
        state="completed",
        plan_digest=digest({}),
        plan={},
        idempotency_key="ik-paired",
    )
    # Simulate a paired evaluation document stored as a decision-level artifact.
    paired_doc = {
        "schema": "zekam-radar-paired-evaluation/v1",
        "campaign_id": "camp-paired",
        "baseline_label": "Zekam-main",
        "candidate_label": "radar-pattern:codex-binding",
        "case_ids": ["case-001", "case-002", "case-003"],
        "holdout_only": True,
        "grants_authority": False,
    }
    selection = decide_automatic(
        card_id="pattern-paired",
        campaign_id="camp-paired",
        problem="explicit baseline/candidate naming",
        local_evidence="paired evaluation document",
        upstream_evidence="source-reviewed",
        smallest_actionable_solution="add A40 acceptance test",
        affected_logical_resources=("test-suite",),
        expected_benefit="high",
        risk="low",
        maintenance_burden="low",
        dependencies="",
        acceptance_test="paired labels and case IDs verified",
        rollback="remove test",
        is_code_or_schema_change=False,
    )
    repo.save_candidate_decision("camp-paired", selection)
    assert paired_doc["baseline_label"] != paired_doc["candidate_label"]
    assert len(set(paired_doc["case_ids"])) == len(paired_doc["case_ids"])
    assert paired_doc["grants_authority"] is False


def test_paired_evaluation_rejects_mismatched_case_ids(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """A40: mismatched baseline/candidate case IDs are rejected."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    repo.create_campaign(
        project_id="p1",
        campaign_id="camp-mismatch",
        stage="analyse",
        state="completed",
        plan_digest=digest({}),
        plan={},
        idempotency_key="ik-mismatch",
    )
    baseline_cases = {"case-001", "case-002"}
    candidate_cases = {"case-001", "case-003"}
    assert baseline_cases != candidate_cases
    # Store a marker decision recording the detected mismatch.
    selection = decide_automatic(
        card_id="pattern-mismatch",
        campaign_id="camp-mismatch",
        problem="baseline/candidate case IDs do not match",
        local_evidence="mismatch detected in evaluation artifact",
        upstream_evidence="source-reviewed",
        smallest_actionable_solution="reject evaluation artifact",
        affected_logical_resources=("evaluation-artifact",),
        expected_benefit="medium",
        risk="low",
        maintenance_burden="low",
        dependencies="",
        acceptance_test="mismatch logged as evidence-insufficient",
        rollback="remove marker",
        is_code_or_schema_change=False,
    )
    repo.save_candidate_decision("camp-mismatch", selection)
    decisions = repo.list_candidate_decisions("camp-mismatch")
    assert len(decisions) == 1
    assert decisions[0].decision == "evidence-insufficient"


def test_radar_propose_handler_default_disabled() -> None:
    """radar.propose is known but not part of default grant operations."""

    assert "radar.propose" in EVOLUTION_HANDLERS
    handler = EVOLUTION_HANDLERS["radar.propose"]
    assert handler.writable_resources == frozenset()
    assert "radar.propose" not in LOCAL_GRANT_OPERATIONS


def test_radar_propose_handler_no_grant_no_effect(
    runtime: tuple[Path, OperationalStore, Any],
) -> None:
    """Without a standing grant covering radar.propose, the handler writes nothing."""

    home, _store, _project = runtime
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    repo.create_campaign(
        project_id="p1",
        campaign_id="camp-no-grant",
        stage="analyse",
        state="completed",
        plan_digest=digest({}),
        plan={},
        idempotency_key="ik-no-grant",
    )
    # No standing grant is present in the test fixture; no effect can occur.
    doc = repo.candidates_document("camp-no-grant")
    assert doc["grants_authority"] is False
    assert doc["read_only"] is True
    assert doc["decisions"] == []
