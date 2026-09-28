"""Scheduler-bound evolution handlers.

This package contains read-only, default-disabled handlers that the local
scheduler may invoke.  They never grant authority and never perform network or
provider effects unless an exact, current capability/authorization is supplied.
"""

from __future__ import annotations

from zekam.application.scheduler.radar_proposal_handler import (
    RadarProposalHandler,
    RadarProposalHandlerResult,
)

__all__ = ("RadarProposalHandler", "RadarProposalHandlerResult")
