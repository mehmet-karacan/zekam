"""Shared improvement risk classes used by policy and persistence."""

from enum import StrEnum


class ImprovementChangeClass(StrEnum):
    """How much authority an improvement candidate requires."""

    AUTO_SAFE = "AUTO_SAFE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
    PROHIBITED_AUTONOMOUS = "PROHIBITED_AUTONOMOUS"
