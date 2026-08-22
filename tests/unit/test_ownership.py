"""Sahiplik siniflarinin yedekleme ve Git davranisi."""

from __future__ import annotations

import pytest

from zekam.domain.ownership import OWNERSHIP_RULES, BackupPolicy, OwnershipClass, rule_for

pytestmark = pytest.mark.unit


def test_every_ownership_class_has_a_rule() -> None:
    assert set(OWNERSHIP_RULES) == set(OwnershipClass)


def test_only_core_is_git_tracked() -> None:
    tracked = {name for name, rule in OWNERSHIP_RULES.items() if rule.git_tracked}
    assert tracked == {OwnershipClass.CORE}


def test_secret_never_leaves_secret_store() -> None:
    rule = rule_for(OwnershipClass.SECRET)
    assert rule.backup is BackupPolicy.SECRET_STORE
    assert rule.git_tracked is False
    assert rule.portable is False


def test_derived_data_is_regenerated_not_backed_up() -> None:
    assert rule_for(OwnershipClass.DERIVED).backup is BackupPolicy.REGENERATE
