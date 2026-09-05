"""Pure Gate-A record boundaries; no local source authority is provisioned here."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from zekam.application.local_continuity_source_authority import (
    BACKUP_RESTORE_READY,
    FileIdentity,
    LocalBindingRevision,
    PortableSourcePlanRecord,
    strict_json,
)
from zekam.application.local_continuity_source_plan import (
    CapturedSourceFile,
    ContinuitySourcePlan,
    ContinuitySourceRecipe,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ValidationFailed

PROJECT = "22222222-2222-4222-8222-222222222222"
BINDING = "33333333-3333-4333-8333-333333333333"
SNAPSHOT = "44444444-4444-4444-8444-444444444444"
REALM = "55555555-5555-4555-8555-555555555555"
LOCAL = "11111111-1111-4111-8111-111111111111"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_D = "sha256:" + "d" * 64
DIGEST_F = "sha256:" + "f" * 64


@pytest.fixture
def plan() -> ContinuitySourcePlan:
    recipe = ContinuitySourceRecipe(PROJECT, REALM, BINDING, ("AGENTS.md",), DIGEST_A, DIGEST_D)
    return ContinuitySourcePlan(
        recipe,
        "0" * 40,
        (CapturedSourceFile("AGENTS.md", DIGEST_F, 9),),
        ((".git/info/exclude", None),),
        DIGEST_B,
    )


def test_portable_record_reconstructs_exact_current_types_and_digests(
    plan: ContinuitySourcePlan,
) -> None:
    record = PortableSourcePlanRecord(SNAPSHOT, plan)
    restored = PortableSourcePlanRecord.from_bytes(record.bytes())
    assert type(restored.plan) is ContinuitySourcePlan
    assert type(restored.plan.recipe.allowed_paths) is tuple
    assert type(restored.plan.ignore_digests) is tuple
    assert type(restored.plan.ignore_digests[0]) is tuple
    assert restored == record
    assert restored.body()["plan_content_digest"] == digest(plan.body())
    assert str(record.plan.recipe.project_id) in record.bytes().decode()
    assert ("approval_inherited", False) in record.body().items()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":1}',
        b'{"a":NaN}',
        b'{"a":1} ',
        b'{"a":1.0}',
        b'{"a":"\\ud800"}',
        b"\xff",
    ],
)
def test_strict_json_rejects_duplicate_nonfinite_noncanonical_and_invalid(raw: bytes) -> None:
    with pytest.raises(ValidationFailed):
        strict_json(raw, maximum=32768)


def test_portable_record_rejects_self_consistent_wrapper_field_drift(
    plan: ContinuitySourcePlan,
) -> None:
    body = PortableSourcePlanRecord(SNAPSHOT, plan).body()
    body["plan_content_digest"] = DIGEST_A
    with pytest.raises(ValidationFailed):
        PortableSourcePlanRecord.from_bytes(canonical_json(body).encode())


def test_portable_record_rejects_recipe_constant_and_nested_field_drift(
    plan: ContinuitySourcePlan,
) -> None:
    body = PortableSourcePlanRecord(SNAPSHOT, plan).body()
    body["plan_body"]["recipe"]["max_file_bytes"] = 1
    with pytest.raises(ValidationFailed):
        PortableSourcePlanRecord.from_bytes(canonical_json(body).encode())


def test_portable_plan_exact_byte_cap(plan: ContinuitySourcePlan) -> None:
    raw = PortableSourcePlanRecord(SNAPSHOT, plan).bytes()
    assert strict_json(raw, maximum=len(raw))
    with pytest.raises(ValidationFailed):
        strict_json(raw + b" ", maximum=len(raw))


def test_local_revision_golden_shape_and_predecessor_rules() -> None:
    operational = FileIdentity(1, 2, 501, 20, 0o100600, 1, 123_000_000_000)
    root = FileIdentity(3, 4, 501, 20, 0o40700, 2, 456_000_000_000)
    revision = LocalBindingRevision(
        "device-a",
        LOCAL,
        operational,
        DIGEST_A,
        PROJECT,
        BINDING,
        "/tmp/zekam-source",
        root,
        DIGEST_F,
        None,
        1,
        "2026-09-04T00:00:00.000000Z",
    )
    assert revision.body()["grants_authority"] is False
    assert revision.body()["approval_inherited"] is False
    assert revision.body()["root"]["path"] == "/tmp/zekam-source"
    assert revision.revision_digest.startswith("sha256:")
    with pytest.raises(ValidationFailed):
        replace(revision, generation=2).body()


def test_backup_restore_readiness_is_honestly_false() -> None:
    assert BACKUP_RESTORE_READY is False


def test_raw_parser_rejects_depth_members_array_and_integer_bounds() -> None:
    values: list[object] = [[[[[[[[[0]]]]]]]], list(range(33)), 2**63]
    for value in values:
        raw = json.dumps({"value": value}, separators=(",", ":")).encode()
        with pytest.raises(ValidationFailed):
            strict_json(raw, maximum=32768)
    raw = canonical_json({f"k{i}": i for i in range(65)}).encode()
    with pytest.raises(ValidationFailed):
        strict_json(raw, maximum=32768)
