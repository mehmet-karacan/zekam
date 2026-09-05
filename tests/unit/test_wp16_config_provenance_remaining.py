from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.config_provenance import (
    ConfigFieldCandidate,
    ConfigFieldDecision,
    ConfigLayer,
    ConfigProvenanceGraph,
    ManagedFieldRequirement,
    ManagedRequirementMode,
    PermissionProfileRevision,
    compile_config_provenance,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "a" * 64
NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)


def _candidate(
    *, layer: str = "base", value: object = "value", selected: bool = True
) -> ConfigFieldCandidate:
    return ConfigFieldCandidate(layer, digest(value), selected, None if selected else "disabled")


def _decision(**changes: Any) -> ConfigFieldDecision:
    values: dict[str, Any] = {
        "field_path": "runtime.mode",
        "origin": "base",
        "value": "value",
        "value_digest": digest("value"),
        "candidates": (_candidate(),),
    }
    values.update(changes)
    return ConfigFieldDecision(**values)


def _graph() -> ConfigProvenanceGraph:
    return compile_config_provenance((ConfigLayer("base", 1, {"runtime": {"mode": "value"}}),))


def _profile(**changes: Any) -> PermissionProfileRevision:
    values: dict[str, Any] = {
        "realm_id": uuid4(),
        "name": "restricted",
        "revision": 1,
        "allowed_capabilities": ("filesystem.read",),
        "denied_capabilities": ("network.access",),
        "managed": True,
        "created_at": NOW,
    }
    values.update(changes)
    return PermissionProfileRevision.create(**values)


def test_managed_requirement_rejects_blank_missing_invalid_and_forbidden_digest() -> None:
    with pytest.raises(ValidationFailed):
        ManagedFieldRequirement(" ", ManagedRequirementMode.DENY)
    with pytest.raises(ValidationFailed):
        ManagedFieldRequirement("runtime.mode", ManagedRequirementMode.EXACT)
    with pytest.raises(ValidationFailed):
        ManagedFieldRequirement("runtime.mode", ManagedRequirementMode.EXACT, "bad")
    with pytest.raises(ValidationFailed):
        ManagedFieldRequirement("runtime.mode", ManagedRequirementMode.DENY, DIGEST)


def test_config_layer_rejects_invalid_identity_authority_and_duplicate_requirement() -> None:
    requirement = ManagedFieldRequirement("runtime.mode", ManagedRequirementMode.DENY)
    with pytest.raises(ValidationFailed):
        ConfigLayer(" ", 1, {})
    with pytest.raises(ValidationFailed):
        ConfigLayer("base", -1, {})
    with pytest.raises(PolicyViolation):
        ConfigLayer("base", 1, {}, requirements=(requirement,))
    with pytest.raises(ValidationFailed):
        ConfigLayer("base", 1, {}, managed=True, requirements=(requirement, requirement))


def test_field_candidate_rejects_invalid_digest_and_selection_reason_drift() -> None:
    with pytest.raises(ValidationFailed):
        ConfigFieldCandidate("base", "bad", True, None)
    with pytest.raises(ValidationFailed):
        ConfigFieldCandidate("base", DIGEST, True, "disabled")
    with pytest.raises(ValidationFailed):
        ConfigFieldCandidate("base", DIGEST, False, None)


def test_field_decision_rejects_identity_value_and_selected_origin_drift() -> None:
    with pytest.raises(ValidationFailed):
        _decision(field_path=" ")
    with pytest.raises(ValidationFailed):
        _decision(origin=" ")
    with pytest.raises(ValidationFailed):
        _decision(value_digest="bad")
    with pytest.raises(PolicyViolation):
        _decision(value_digest=digest("different"))
    with pytest.raises(ValidationFailed):
        _decision(candidates=(_candidate(selected=False),))
    with pytest.raises(ValidationFailed):
        _decision(origin="other")
    with pytest.raises(ValidationFailed):
        _decision(candidates=(_candidate(), _candidate(layer="other")))


def test_graph_rejects_stack_order_authority_and_digest_drift() -> None:
    graph = _graph()
    second = replace(graph.fields[0], field_path="aaa")
    with pytest.raises(ValidationFailed):
        replace(graph, layer_stack=())
    with pytest.raises(ValidationFailed):
        replace(graph, layer_stack=("base", "base"))
    with pytest.raises(ValidationFailed):
        replace(graph, fields=(graph.fields[0], second))
    with pytest.raises(PolicyViolation):
        replace(graph, grants_authority=True)
    with pytest.raises(PolicyViolation):
        replace(graph, effective_document={"runtime": {"mode": "forged"}})
    with pytest.raises(ValidationFailed):
        replace(graph, graph_digest="bad")
    with pytest.raises(PolicyViolation):
        replace(graph, layer_stack=("forged",))
    with pytest.raises(ValidationFailed):
        graph.explain("missing")


def test_compile_rejects_empty_and_duplicate_precedence() -> None:
    with pytest.raises(ValidationFailed):
        compile_config_provenance(())
    with pytest.raises(ValidationFailed):
        compile_config_provenance((ConfigLayer("a", 1, {}), ConfigLayer("b", 1, {})))


@pytest.mark.parametrize(
    "changes",
    [
        {"name": " "},
        {"revision": 0},
        {"created_at": NOW.replace(tzinfo=None)},
        {"allowed_capabilities": ("z", "a")},
        {"allowed_capabilities": ("a", "a")},
        {"denied_capabilities": ("z", "a")},
        {"denied_capabilities": ("a", "a")},
        {"allowed_capabilities": ("same",), "denied_capabilities": ("same",)},
        {"allowed_capabilities": (" ",)},
        {"denied_capabilities": (" ",)},
        {"grants_authority": True},
    ],
)
def test_permission_profile_rejects_invalid_identity_sets_and_authority(
    changes: dict[str, Any],
) -> None:
    profile = _profile()
    with pytest.raises((PolicyViolation, ValidationFailed)):
        replace(profile, **changes)


def test_permission_profile_rejects_invalid_and_mismatched_supplied_digest() -> None:
    profile = _profile()
    with pytest.raises(ValidationFailed):
        replace(profile, profile_digest="bad")
    with pytest.raises(PolicyViolation):
        replace(profile, name="forged")


def test_permission_flags_require_exact_boolean_contract() -> None:
    base: dict[str, Any] = {
        "realm_id": uuid4(),
        "name": "flags",
        "revision": 1,
        "managed": True,
        "created_at": NOW,
    }
    with pytest.raises(ValidationFailed):
        PermissionProfileRevision.from_flags(permission_flags={}, **base)
    invalid_flags: dict[str, Any] = {
        "filesystem.read": 1,
        "filesystem.write": False,
        "network.access": False,
        "process.run": True,
    }
    with pytest.raises(ValidationFailed):
        PermissionProfileRevision.from_flags(
            permission_flags=invalid_flags,
            **base,
        )
