"""Veri sahiplik siniflari.

`mimari/DIZIN_YERLESIMI_VE_VERI_SAHIPLIK.md` icindeki tabloyu koda baglar. Her kalici
konumun tek bir sahiplik sinifi vardir; ayni identity iki konumda authority olamaz.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OwnershipClass(StrEnum):
    """Kalici verinin sahiplik sinifi."""

    CORE = "core"
    USER_DATA = "user-data"
    RUNTIME = "runtime"
    DERIVED = "derived"
    ARTIFACT = "artifact"
    LOCAL = "local"
    SECRET = "secret"


class BackupPolicy(StrEnum):
    """Yedekleme davranisi."""

    SOURCE_CONTROL = "source-control"
    REQUIRED = "required"
    CONTROLLED = "controlled"
    REGENERATE = "regenerate"
    POLICY_BASED = "policy-based"
    MACHINE_LOCAL = "machine-local"
    SECRET_STORE = "secret-store"


@dataclass(frozen=True, slots=True)
class OwnershipRule:
    """Bir sahiplik sinifinin yedekleme ve Git davranisi."""

    ownership: OwnershipClass
    backup: BackupPolicy
    git_tracked: bool
    portable: bool


OWNERSHIP_RULES: dict[OwnershipClass, OwnershipRule] = {
    OwnershipClass.CORE: OwnershipRule(
        OwnershipClass.CORE, BackupPolicy.SOURCE_CONTROL, git_tracked=True, portable=True
    ),
    OwnershipClass.USER_DATA: OwnershipRule(
        OwnershipClass.USER_DATA, BackupPolicy.REQUIRED, git_tracked=False, portable=True
    ),
    OwnershipClass.RUNTIME: OwnershipRule(
        OwnershipClass.RUNTIME, BackupPolicy.CONTROLLED, git_tracked=False, portable=False
    ),
    OwnershipClass.DERIVED: OwnershipRule(
        OwnershipClass.DERIVED, BackupPolicy.REGENERATE, git_tracked=False, portable=False
    ),
    OwnershipClass.ARTIFACT: OwnershipRule(
        OwnershipClass.ARTIFACT, BackupPolicy.POLICY_BASED, git_tracked=False, portable=True
    ),
    OwnershipClass.LOCAL: OwnershipRule(
        OwnershipClass.LOCAL, BackupPolicy.MACHINE_LOCAL, git_tracked=False, portable=False
    ),
    OwnershipClass.SECRET: OwnershipRule(
        OwnershipClass.SECRET, BackupPolicy.SECRET_STORE, git_tracked=False, portable=False
    ),
}


def rule_for(ownership: OwnershipClass) -> OwnershipRule:
    """Sahiplik sinifinin kuralini dondurur."""
    return OWNERSHIP_RULES[ownership]
