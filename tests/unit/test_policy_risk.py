"""Risk siniflandirmasi, policy belgesi ve capability kayitlari."""

from __future__ import annotations

from uuid import uuid4

import pytest

from zekam.domain.errors import ValidationFailed
from zekam.domain.policy import (
    RISK_ORDER,
    Capability,
    CapabilityKind,
    PolicyDocument,
    PolicyRule,
    RiskLevel,
    classify_risk,
    default_policy_rules,
    is_auto_approved,
    max_risk,
)
from zekam.domain.security import DataClassification
from zekam.domain.work import EffectKind

pytestmark = pytest.mark.unit

REALM = uuid4()


# -- risk ---------------------------------------------------------------------------


def test_read_only_work_is_risk_none() -> None:
    assessment = classify_risk(effects=(EffectKind.NONE,))
    assert assessment.level is RiskLevel.NONE
    assert not assessment.requires_authorization
    assert not assessment.requires_independent_verifier


def test_file_write_is_at_least_medium() -> None:
    assessment = classify_risk(effects=(EffectKind.FILE_WRITE,))
    assert assessment.level is RiskLevel.MEDIUM
    assert assessment.requires_authorization


def test_database_write_is_high_and_needs_verifier() -> None:
    assessment = classify_risk(effects=(EffectKind.DATABASE_WRITE,))
    assert assessment.level is RiskLevel.HIGH
    assert assessment.requires_independent_verifier


def test_push_is_high() -> None:
    assert classify_risk(effects=(EffectKind.GIT_PUSH,)).level is RiskLevel.HIGH


def test_destructive_work_is_critical() -> None:
    assessment = classify_risk(effects=(EffectKind.FILE_WRITE,), destructive=True)
    assert assessment.level is RiskLevel.CRITICAL
    assert assessment.irreversible


def test_irreversible_work_is_at_least_high() -> None:
    assessment = classify_risk(effects=(EffectKind.FILE_WRITE,), reversible=False)
    assert assessment.level is RiskLevel.HIGH
    assert assessment.irreversible


def test_secret_data_makes_it_critical() -> None:
    assessment = classify_risk(
        effects=(EffectKind.NONE,), data_classifications=(DataClassification.SECRET,)
    )
    assert assessment.level is RiskLevel.CRITICAL


def test_local_only_data_is_high() -> None:
    assessment = classify_risk(
        effects=(EffectKind.NONE,), data_classifications=(DataClassification.LOCAL_ONLY,)
    )
    assert assessment.level is RiskLevel.HIGH


def test_large_blast_radius_raises_risk() -> None:
    small = classify_risk(effects=(EffectKind.FILE_WRITE,), resource_count=2)
    large = classify_risk(effects=(EffectKind.FILE_WRITE,), resource_count=50)
    assert RISK_ORDER[large.level] > RISK_ORDER[small.level]


def test_risk_never_goes_below_effect_baseline() -> None:
    # Cok dusuk hassasiyetli veri bile DB yazimini dusuremez.
    assessment = classify_risk(
        effects=(EffectKind.DATABASE_WRITE,),
        data_classifications=(DataClassification.PUBLIC,),
    )
    assert assessment.level is RiskLevel.HIGH


def test_factors_explain_the_level() -> None:
    assessment = classify_risk(effects=(EffectKind.GIT_PUSH,), destructive=True)
    assert any(factor.startswith("effect:git-push") for factor in assessment.factors)
    assert "destructive=critical" in assessment.factors


def test_max_risk_of_empty_sequence_is_none() -> None:
    assert max_risk([]) is RiskLevel.NONE


# -- otomatik onay --------------------------------------------------------------------


@pytest.mark.parametrize("action", ["status", "list", "history", "doctor", "plan", "dry-run"])
def test_read_only_actions_are_auto_approved(action: str) -> None:
    assert is_auto_approved(action)


def test_unknown_action_is_not_auto_approved() -> None:
    assert not is_auto_approved("mutate-everything")


def test_read_action_with_effect_is_not_auto_approved() -> None:
    assert not is_auto_approved("status", effects=(EffectKind.FILE_WRITE,))


# -- policy ---------------------------------------------------------------------------


def _policy(rules: tuple[PolicyRule, ...] | None = None) -> PolicyDocument:
    return PolicyDocument.create(
        realm_id=REALM, name="varsayilan", revision=1, rules=rules or default_policy_rules()
    )


def test_default_policy_denies_network_and_push() -> None:
    document = _policy()
    network = document.rule_for(EffectKind.NETWORK_CALL)
    push = document.rule_for(EffectKind.GIT_PUSH)
    assert network is not None and not network.allow
    assert push is not None and not push.allow


def test_default_policy_allows_read_only() -> None:
    rule = _policy().rule_for(EffectKind.NONE)
    assert rule is not None
    assert rule.allow


def test_default_policy_flags_are_deny_by_default() -> None:
    document = _policy()
    assert document.network_default_deny
    assert document.push_default_deny


def test_policy_digest_is_deterministic() -> None:
    assert _policy().policy_digest == _policy().policy_digest


def test_policy_digest_changes_with_rules() -> None:
    changed = default_policy_rules()[:-1]
    assert _policy(changed).policy_digest != _policy().policy_digest


def test_duplicate_rule_names_are_rejected() -> None:
    rule = PolicyRule(name="ayni", effect_kinds=(EffectKind.NONE,), allow=True)
    with pytest.raises(ValidationFailed):
        _policy((rule, rule))


def test_blank_rule_name_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        PolicyRule(name="  ", effect_kinds=(), allow=True)


def test_rule_without_effect_kinds_applies_to_all() -> None:
    rule = PolicyRule(name="hepsi", effect_kinds=(), allow=True)
    assert rule.applies_to(EffectKind.GIT_PUSH)
    assert rule.applies_to(EffectKind.NONE)


# -- capability -------------------------------------------------------------------------


def test_capability_digest_is_deterministic() -> None:
    first = Capability.create(
        realm_id=REALM, name="source.read", revision=1, kind=CapabilityKind.READ
    )
    same = Capability.create(
        realm_id=REALM, name="source.read", revision=1, kind=CapabilityKind.READ
    )
    assert first.capability_digest == same.capability_digest


def test_capability_digest_changes_with_definition() -> None:
    base = Capability.create(realm_id=REALM, name="net", revision=1, kind=CapabilityKind.NETWORK)
    other = Capability.create(
        realm_id=REALM,
        name="net",
        revision=1,
        kind=CapabilityKind.NETWORK,
        definition={"hosts": ["api.example.com"]},
    )
    assert base.capability_digest != other.capability_digest


def test_capability_requires_name_and_revision() -> None:
    with pytest.raises(ValidationFailed):
        Capability.create(realm_id=REALM, name="  ", revision=1, kind=CapabilityKind.READ)
    with pytest.raises(ValidationFailed):
        Capability.create(realm_id=REALM, name="x", revision=0, kind=CapabilityKind.READ)


def test_capability_record_carries_no_authority_field() -> None:
    document = Capability.create(
        realm_id=REALM, name="provider.call", revision=1, kind=CapabilityKind.PROVIDER
    ).as_dict()
    assert "authority" not in document
    assert "allowed" not in document
