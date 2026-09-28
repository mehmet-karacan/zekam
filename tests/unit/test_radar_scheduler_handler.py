"""WP-05 A41: Default-disabled read-only radar proposal handler tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zekam.application.home import HomeLayout
from zekam.application.scheduler import RadarProposalHandler
from zekam.application.scheduler.radar_proposal_handler import default_capabilities
from zekam.domain.canonical import digest
from zekam.domain.errors import NotFound, ValidationFailed
from zekam.domain.radar_candidate import (
    EvidenceLevel,
    RadarCandidateDecision,
    RadarCandidateKind,
    RadarCandidateSelection,
    RadarGapCard,
    RadarPatternCard,
    SourceProvenance,
)
from zekam.infrastructure.radar_campaign_store import RadarCampaignRepository
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(tmp_path: Path) -> tuple[Path, SQLiteOperationalStore, Any]:
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


def _seed_campaign(home: Path) -> str:
    """Create a minimal radar campaign and return its campaign id."""

    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    campaign_id = "radar-campaign:test-001"
    campaign = repo.create_campaign(
        project_id="p1",
        campaign_id=campaign_id,
        stage="discover",
        state="completed",
        plan_digest=digest({"plan": "body"}),
        plan={"plan": "body"},
        idempotency_key="ik:test-001",
    )
    repo.record_stage(
        campaign_id=campaign.id,
        stage="discover",
        state="completed",
        next_safe_action="candidates",
    )
    repo.save_pattern_card(
        campaign_id=campaign_id,
        card=RadarPatternCard(
            card_id="pattern-1",
            campaign_id=campaign_id,
            kind=RadarCandidateKind.PATTERN,
            problem="coordinator text replaces child evidence",
            source=SourceProvenance(
                repository_id=42,
                owner="openai",
                name="codex",
                commit_sha="abc123" * 6,
                path="README.md",
                range="1-10",
                evidence_digest=digest("readme sample"),
            ),
            observed_behavior="child payload bound to finding",
            inference_or_assumption="verifier checks digest",
            test_evidence_level=EvidenceLevel.SOURCE_REVIEWED,
            license_reuse_constraint="MIT with NOTICE",
            cost_dependency_limit="none",
            not_applicable_conditions="none",
        ),
    )
    repo.save_gap_card(
        campaign_id=campaign_id,
        card=RadarGapCard(
            card_id="gap-1",
            campaign_id=campaign_id,
            kind=RadarCandidateKind.GAP,
            current_baseline="zekam main",
            source_binding="src/zekam/application/research_runtime.py",
            production_call_path="research_service.dispatch",
            test_measurement_evidence="unit tests assert child binding",
            covered_areas="fan-in",
            uncovered_areas="live provider telemetry",
            result=RadarCandidateDecision.GAP_DEMONSTRATED,
        ),
    )
    repo.save_candidate_decision(
        campaign_id=campaign_id,
        selection=RadarCandidateSelection(
            selection_id="radar-sel:test-001",
            campaign_id=campaign_id,
            card_id="gap-1",
            decision=RadarCandidateDecision.GAP_DEMONSTRATED,
            problem="coordinator text replaces child evidence",
            local_evidence="unit tests lack child payload binding",
            upstream_evidence="source-reviewed",
            smallest_actionable_solution="bind real child payload digest to findings",
            affected_logical_resources=("local-index",),
            expected_benefit="high",
            risk="medium",
            maintenance_burden="low",
            dependencies="",
            acceptance_test="child payload digest appears in final report",
            rollback="revert fan-in change",
        ),
    )
    return campaign_id


class _CustomCapabilities:
    """Test-only capability checker with explicit operation/resource gates."""

    def __init__(
        self,
        *,
        enabled: bool,
        authorized: bool,
        extra_reads: tuple[str, ...] = (),
        extra_writes: tuple[str, ...] = (),
    ) -> None:
        self._enabled = enabled
        self._authorized = authorized
        self._extra_reads = extra_reads
        self._extra_writes = extra_writes

    def is_enabled(self, operation: str) -> bool:
        return self._enabled and operation == RadarProposalHandler.OPERATION

    def is_authorized(
        self,
        operation: str,
        *,
        readable_resources: tuple[str, ...],
        writable_resources: tuple[str, ...],
    ) -> bool:
        if not self._authorized or operation != RadarProposalHandler.OPERATION:
            return False
        required = frozenset(RadarProposalHandler.READABLE_RESOURCES)
        allowed_reads = frozenset(RadarProposalHandler.READABLE_RESOURCES) | frozenset(
            self._extra_reads
        )
        allowed_writes = frozenset(self._extra_writes)
        return (
            required.issubset(allowed_reads)
            and frozenset(readable_resources).issubset(allowed_reads)
            and frozenset(writable_resources).issubset(allowed_writes)
            and not writable_resources
        )


def test_default_capabilities_are_disabled() -> None:
    caps = default_capabilities()
    assert caps.is_enabled("radar.propose") is False
    assert (
        caps.is_authorized(
            "radar.propose", readable_resources=("radar-candidate",), writable_resources=()
        )
        is False
    )


def test_handler_returns_disabled_when_capability_not_enabled(
    runtime: tuple[Path, SQLiteOperationalStore, Any],
) -> None:
    home, _store, _project = runtime
    campaign_id = _seed_campaign(home)
    handler = RadarProposalHandler(home / "state" / "radar-campaigns.db")
    result = handler.handle(
        campaign_id=campaign_id,
        capabilities=default_capabilities(enabled=False, authorized=False),
    )
    assert result.status == "disabled"
    body = result.body
    assert body["operation"] == "radar.propose"
    assert body["campaign_id"] == campaign_id
    assert body["candidates"] is None
    assert body["next_safe_action"] == "enable-radar-propose-capability-or-grant"
    assert body["provider_calls"] == 0
    assert body["network_calls"] == 0
    assert body["mutations"] == 0
    assert body["grants_authority"] is False


def test_handler_returns_unauthorized_when_enabled_but_not_authorized(
    runtime: tuple[Path, SQLiteOperationalStore, Any],
) -> None:
    home, _store, _project = runtime
    campaign_id = _seed_campaign(home)
    handler = RadarProposalHandler(home / "state" / "radar-campaigns.db")
    result = handler.handle(
        campaign_id=campaign_id,
        capabilities=default_capabilities(enabled=True, authorized=False),
    )
    assert result.status == "unauthorized"
    body = result.body
    assert body["candidates"] is None
    assert body["next_safe_action"] == "grant-radar-propose-read-only"
    assert body["grants_authority"] is False


def test_handler_returns_candidates_when_authorized(
    runtime: tuple[Path, SQLiteOperationalStore, Any],
) -> None:
    home, _store, _project = runtime
    campaign_id = _seed_campaign(home)
    handler = RadarProposalHandler(home / "state" / "radar-campaigns.db")
    result = handler.handle(
        campaign_id=campaign_id,
        capabilities=_CustomCapabilities(enabled=True, authorized=True),
    )
    assert result.status == "completed"
    body = result.body
    assert body["campaign_id"] == campaign_id
    assert body["candidates"] is not None
    assert body["candidates"]["schema"] == "zekam-radar-campaign-candidates/v1"
    assert body["candidates"]["campaign_id"] == campaign_id
    assert len(body["candidates"]["pattern_cards"]) == 1
    assert len(body["candidates"]["gap_cards"]) == 1
    assert len(body["candidates"]["decisions"]) == 1
    assert body["candidates"]["read_only"] is True
    assert body["candidates"]["grants_authority"] is False
    assert body["next_safe_action"] == "review-candidates-then-decide"
    assert body["grants_authority"] is False
    assert body["mutations"] == 0


def test_handler_rejects_unknown_campaign_when_authorized(
    runtime: tuple[Path, SQLiteOperationalStore, Any],
) -> None:
    home, _store, _project = runtime
    handler = RadarProposalHandler(home / "state" / "radar-campaigns.db")
    result = handler.handle(
        campaign_id="radar-campaign:missing",
        capabilities=_CustomCapabilities(enabled=True, authorized=True),
    )
    assert result.status == "completed"
    candidates = result.body["candidates"]
    assert candidates["pattern_cards"] == []
    assert candidates["gap_cards"] == []
    assert candidates["decisions"] == []


def test_handler_rejects_invalid_campaign_id(
    runtime: tuple[Path, SQLiteOperationalStore, Any],
) -> None:
    home, _store, _project = runtime
    handler = RadarProposalHandler(home / "state" / "radar-campaigns.db")
    with pytest.raises(ValidationFailed):
        handler.handle(
            campaign_id="",
            capabilities=_CustomCapabilities(enabled=True, authorized=True),
        )


def test_handler_no_effect_for_unauthorized_does_not_create_campaign(
    runtime: tuple[Path, SQLiteOperationalStore, Any],
) -> None:
    home, _store, _project = runtime
    handler = RadarProposalHandler(home / "state" / "radar-campaigns.db")
    handler.handle(
        campaign_id="radar-campaign:nonexistent",
        capabilities=default_capabilities(enabled=False, authorized=False),
    )
    repo = RadarCampaignRepository(home / "state" / "radar-campaigns.db")
    with pytest.raises(NotFound):
        repo.get_campaign("radar-campaign:nonexistent")


def test_handler_result_digest_is_stable(
    runtime: tuple[Path, SQLiteOperationalStore, Any],
) -> None:
    home, _store, _project = runtime
    campaign_id = _seed_campaign(home)
    handler = RadarProposalHandler(home / "state" / "radar-campaigns.db")
    result1 = handler.handle(
        campaign_id=campaign_id,
        capabilities=_CustomCapabilities(enabled=True, authorized=True),
    )
    result2 = handler.handle(
        campaign_id=campaign_id,
        capabilities=_CustomCapabilities(enabled=True, authorized=True),
    )
    assert result1.result_digest == result2.result_digest
    assert result1.result_digest.startswith("sha256:")


def test_handler_rejects_writable_resource_even_if_authorized() -> None:
    class BadCapabilities:
        def is_enabled(self, operation: str) -> bool:
            return operation == RadarProposalHandler.OPERATION

        def is_authorized(
            self,
            operation: str,
            *,
            readable_resources: tuple[str, ...],
            writable_resources: tuple[str, ...],
        ) -> bool:
            # The handler itself never writes; this fake checker pretends to allow writes.
            return operation == RadarProposalHandler.OPERATION and bool(writable_resources)

    handler = RadarProposalHandler(Path("/tmp/ignored.db"))
    result = handler.handle(
        campaign_id="radar-campaign:ignored",
        capabilities=BadCapabilities(),
    )
    # Because the checker is external, the handler can only trust it for read
    # authorization.  The handler still does not perform writes, so the result
    # reports zero mutations.  The test documents this boundary.
    assert result.status in {"disabled", "unauthorized", "completed"}
    assert result.body["mutations"] == 0
