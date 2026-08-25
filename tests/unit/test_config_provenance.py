from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.config_provenance import (
    ConfigLayer,
    ManagedFieldRequirement,
    ManagedRequirementMode,
    PermissionProfileRevision,
    compile_config_provenance,
)
from zekam.domain.errors import PolicyViolation

pytestmark = pytest.mark.unit


def test_field_origin_candidates_disabled_reason_and_digest_are_deterministic() -> None:
    layers = (
        ConfigLayer("core-default", 10, {"runtime": {"log_level": "INFO", "network": False}}),
        ConfigLayer("user-config", 20, {"runtime": {"log_level": "DEBUG"}}),
        ConfigLayer("environment", 30, {"runtime": {"log_level": "WARNING"}}),
    )
    graph = compile_config_provenance(layers)
    replay = compile_config_provenance(tuple(reversed(layers)))
    field = graph.explain("runtime.log_level")
    assert field.origin == "environment" and field.value == "WARNING"
    assert tuple((item.layer, item.disabled_reason) for item in field.candidates) == (
        ("core-default", "higher-precedence-layer"),
        ("user-config", "higher-precedence-layer"),
        ("environment", None),
    )
    assert graph.effective_document["runtime"]["network"] is False
    assert replay.graph_digest == graph.graph_digest
    assert graph.grants_authority is False


def test_managed_deny_and_exact_requirement_cannot_be_relaxed_by_session() -> None:
    deny = ManagedFieldRequirement("runtime.network", ManagedRequirementMode.DENY)
    exact = ManagedFieldRequirement(
        "runtime.sandbox", ManagedRequirementMode.EXACT, digest("strict")
    )
    managed = ConfigLayer(
        "managed-policy",
        20,
        {"runtime": {"network": False, "sandbox": "strict"}},
        managed=True,
        requirements=(deny, exact),
    )
    safe = compile_config_provenance(
        (
            ConfigLayer("core-default", 10, {"runtime": {"network": False}}),
            managed,
            ConfigLayer("session", 30, {"runtime": {"network": False, "sandbox": "strict"}}),
        )
    )
    assert safe.explain("runtime.network").managed_requirement == deny
    with pytest.raises(PolicyViolation, match="Managed deny"):
        compile_config_provenance(
            (managed, ConfigLayer("session", 30, {"runtime": {"network": True}}))
        )
    with pytest.raises(PolicyViolation, match="Managed exact"):
        compile_config_provenance(
            (managed, ConfigLayer("session", 30, {"runtime": {"sandbox": "relaxed"}}))
        )


@pytest.mark.parametrize("mode", [ManagedRequirementMode.DENY, ManagedRequirementMode.EXACT])
def test_missing_managed_requirement_field_fails_closed(mode: ManagedRequirementMode) -> None:
    requirement = ManagedFieldRequirement(
        "runtime.required",
        mode,
        digest("strict") if mode is ManagedRequirementMode.EXACT else None,
    )
    with pytest.raises(PolicyViolation, match="requirement alani eksik"):
        compile_config_provenance(
            (
                ConfigLayer(
                    "managed-policy",
                    20,
                    {},
                    managed=True,
                    requirements=(requirement,),
                ),
            )
        )


def test_named_permission_profile_is_revisioned_authority_free_and_managed_deny_is_sticky() -> None:
    profile = PermissionProfileRevision.create(
        realm_id=uuid4(),
        name="workspace-write-no-network",
        revision=2,
        allowed_capabilities=("process.run", "filesystem.read", "filesystem.write"),
        denied_capabilities=("network.access",),
        managed=True,
        created_at=dt.datetime.now(dt.UTC),
    )
    assert profile.resolve_session(("filesystem.read", "process.run")) == (
        "filesystem.read",
        "process.run",
    )
    assert profile.grants_authority is False
    with pytest.raises(PolicyViolation, match="managed deny"):
        profile.resolve_session(("network.access",))
    with pytest.raises(PolicyViolation, match="capability scope"):
        profile.resolve_session(("database.admin",))


def test_builtin_permission_catalog_identity_and_digest_are_stable() -> None:
    from zekam.domain.config_provenance import builtin_permission_profiles

    first = builtin_permission_profiles()
    second = builtin_permission_profiles()
    assert tuple((item.id, item.profile_digest) for item in first) == tuple(
        (item.id, item.profile_digest) for item in second
    )


def test_raw_permission_booleans_compile_to_named_revision_without_loss() -> None:
    profile = PermissionProfileRevision.from_flags(
        realm_id=uuid4(),
        name="named-from-flags",
        revision=1,
        permission_flags={
            "filesystem.read": True,
            "filesystem.write": False,
            "network.access": False,
            "process.run": True,
        },
        managed=True,
        created_at=dt.datetime.now(dt.UTC),
    )
    assert profile.allowed_capabilities == ("filesystem.read", "process.run")
    assert profile.denied_capabilities == ("filesystem.write", "network.access")
    assert profile.name == "named-from-flags" and profile.revision == 1
