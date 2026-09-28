"""Default-disabled, read-only radar proposal handler for the scheduler.

WP-05 A41: ``radar.propose`` is registered in ``EVOLUTION_HANDLERS`` but is
intentionally absent from ``LOCAL_GRANT_OPERATIONS``.  The handler therefore
runs only when an explicit, current grant or capability authorizes it.  In all
other cases it returns a no-effect result with a clear next safe action.

The handler never writes to the radar store, never calls providers, and never
mutates project source.  It only reads the candidate document from
``RadarCampaignRepository.candidates_document()`` and emits a bounded report.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.radar_campaign_store import RadarCampaignRepository


class CapabilityChecker(Protocol):
    """Read-only capability probe supplied by the scheduler/runtime."""

    def is_enabled(self, operation: str) -> bool: ...

    def is_authorized(
        self,
        operation: str,
        *,
        readable_resources: tuple[str, ...],
        writable_resources: tuple[str, ...],
    ) -> bool: ...


class RadarProposalHandlerResult:
    """Immutable, digest-bound result of one ``radar.propose`` invocation."""

    def __init__(
        self,
        *,
        status: str,
        operation: str,
        campaign_id: str,
        candidates: dict[str, Any] | None,
        next_safe_action: str,
        reason: str | None,
    ) -> None:
        if status not in {"disabled", "unauthorized", "completed"}:
            raise ValidationFailed("Radar proposal status gecersiz")
        self._status: str = status
        self._body: dict[str, Any] = {
            "schema": "zekam-radar-proposal-handler-result/v1",
            "operation": operation,
            "status": status,
            "campaign_id": campaign_id,
            "candidates": candidates,
            "next_safe_action": next_safe_action,
            "reason": reason,
            "provider_calls": 0,
            "network_calls": 0,
            "mutations": 0,
            "grants_authority": False,
        }
        self._digest: str = digest(self._body)

    def __setattr__(self, name: str, value: object) -> None:
        if name.startswith("_") and name in {"_status", "_body", "_digest"}:
            object.__setattr__(self, name, value)
            return
        raise AttributeError("RadarProposalHandlerResult is immutable")

    @property
    def status(self) -> str:
        return self._status

    @property
    def body(self) -> dict[str, Any]:
        return self._body.copy()

    @property
    def result_digest(self) -> str:
        return self._digest


class RadarProposalHandler:
    """Read-only handler for ``radar.propose`` scheduler invitations.

    The handler is default-disabled.  When enabled and authorized it produces a
    candidate report; otherwise it returns ``disabled``/``unauthorized`` without
    side effects.
    """

    OPERATION = "radar.propose"
    READABLE_RESOURCES = ("radar-candidate", "source-candidate")
    WRITABLE_RESOURCES: tuple[str, ...] = ()

    def __init__(self, radar_db_path: Path) -> None:
        if not isinstance(radar_db_path, Path):
            raise ValidationFailed("RadarProposalHandler Path ister")
        self._radar_db_path = radar_db_path

    def handle(
        self,
        *,
        campaign_id: str,
        capabilities: CapabilityChecker,
    ) -> RadarProposalHandlerResult:
        """Return a read-only proposal result or a no-effect gate result."""

        self._validate_campaign_id(campaign_id)

        if not capabilities.is_enabled(self.OPERATION):
            return RadarProposalHandlerResult(
                status="disabled",
                operation=self.OPERATION,
                campaign_id=campaign_id,
                candidates=None,
                next_safe_action="enable-radar-propose-capability-or-grant",
                reason="radar.propose default-disabled",
            )

        if not capabilities.is_authorized(
            self.OPERATION,
            readable_resources=self.READABLE_RESOURCES,
            writable_resources=self.WRITABLE_RESOURCES,
        ):
            return RadarProposalHandlerResult(
                status="unauthorized",
                operation=self.OPERATION,
                campaign_id=campaign_id,
                candidates=None,
                next_safe_action="grant-radar-propose-read-only",
                reason="radar.propose requires explicit read-only authorization",
            )

        repo = RadarCampaignRepository(self._radar_db_path)
        try:
            candidates = repo.candidates_document(campaign_id)
        except Exception as exc:
            if "Campaign bulunamadi" in str(exc):
                candidates = {
                    "schema": "zekam-radar-campaign-candidates/v1",
                    "campaign_id": campaign_id,
                    "campaign_state": "unknown",
                    "pattern_cards": [],
                    "gap_cards": [],
                    "decisions": [],
                    "read_only": True,
                    "grants_authority": False,
                }
            else:
                raise
        return RadarProposalHandlerResult(
            status="completed",
            operation=self.OPERATION,
            campaign_id=campaign_id,
            candidates=candidates,
            next_safe_action="review-candidates-then-decide",
            reason=None,
        )

    @staticmethod
    def _validate_campaign_id(value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed("campaign_id bos olamaz")
        if len(value) > 256 or "\n" in value or "\x00" in value:
            raise ValidationFailed("campaign_id bounded canonical ref olmali")


class _StaticCapability:
    """Default capability checker used by the scheduler when none is injected."""

    def __init__(self, enabled: bool = False, authorized: bool = False) -> None:
        self._enabled = enabled
        self._authorized = authorized

    def is_enabled(self, operation: str) -> bool:
        return self._enabled and operation == RadarProposalHandler.OPERATION

    def is_authorized(
        self,
        operation: str,
        *,
        readable_resources: tuple[str, ...],
        writable_resources: tuple[str, ...],
    ) -> bool:
        return (
            self._authorized
            and operation == RadarProposalHandler.OPERATION
            and frozenset(readable_resources) >= frozenset(RadarProposalHandler.READABLE_RESOURCES)
            and not writable_resources
        )


def default_capabilities(*, enabled: bool = False, authorized: bool = False) -> CapabilityChecker:
    """Return a default fail-closed capability checker for tests and CLI."""
    return _StaticCapability(enabled=enabled, authorized=authorized)
