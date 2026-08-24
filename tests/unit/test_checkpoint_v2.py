from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import UUID

import pytest

from zekam.domain.canonical import digest
from zekam.domain.checkpoint_v2 import (
    CheckpointV2,
    NextSafeActionV2,
    OpenEffect,
    OpenEffectState,
    RecoveryDirectiveV2,
    Resumability,
    SandboxBindingV2,
    SandboxDisposition,
    StaleDigestBindings,
    StepResultV2,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.work import EffectKind

NOW = dt.datetime(2026, 8, 24, 12, 0, tzinfo=dt.UTC)
D = digest("binding")


def uid(value: int) -> UUID:
    return UUID(int=value)


def bindings() -> StaleDigestBindings:
    return StaleDigestBindings(
        routing_context_snapshot_id=uid(40),
        source_revision="3ef6c55",
        policy_digest=D,
        capability_profile_digest=D,
        dependency_snapshot_digest=D,
        migration_head_digest=D,
        model_route_decision_digest=D,
        context_manifest_digest=D,
        context_packet_digest=D,
        architecture_digest=D,
        rules_digest=D,
        test_suite_digest=D,
        model_inventory_digest=D,
        journal_head_digest=D,
    )


def step_result(
    step_id: str,
    result_digest: str,
    effect_kind: EffectKind,
    *,
    receipt_refs: tuple[UUID, ...] = (),
    verification_refs: tuple[UUID, ...] = (),
    verification_required: bool = False,
) -> StepResultV2:
    return StepResultV2(
        step_id,
        result_digest,
        effect_kind,
        uid(7),
        uid(8),
        uid(9),
        uid(32),
        D,
        receipt_refs,
        verification_refs,
        verification_required,
    )


def checkpoint(**changes: object) -> CheckpointV2:
    values: dict[str, object] = {
        "checkpoint_id": uid(1),
        "checkpoint_key": "checkpoint:work-1",
        "revision": 1,
        "previous_checkpoint_id": None,
        "previous_checkpoint_digest": None,
        "realm_id": uid(2),
        "project_id": uid(3),
        "work_item_id": uid(4),
        "intent_digest": D,
        "plan_id": uid(5),
        "plan_digest": D,
        "step_id": "build",
        "run_id": uid(6),
        "job_id": uid(7),
        "attempt_id": uid(8),
        "assignment_id": uid(9),
        "execution_envelope_id": uid(32),
        "execution_envelope_digest": D,
        "route_decision_id": uid(10),
        "context_manifest_id": uid(11),
        "context_packet_id": uid(12),
        "bindings": bindings(),
        "plan_steps": ("research", "build", "verify"),
        "completed_steps": ("research",),
        "pending_steps": ("build", "verify"),
        "step_results": (step_result("research", digest("result"), EffectKind.NONE),),
        "open_effects": (
            OpenEffect(uid(20), digest("effect"), OpenEffectState.STARTED_NO_TERMINAL_RECEIPT),
        ),
        "logical_read_resources": ("path:project-1:src/input.py",),
        "logical_write_resources": ("path:project-1:src/output.py",),
        "sandbox": SandboxBindingV2(SandboxDisposition.NOT_APPLICABLE),
        "tokens_used": 100,
        "cost_micros_used": 200,
        "attempts_used": 1,
        "deadline": NOW + dt.timedelta(hours=1),
        "rollback_or_recovery": (
            RecoveryDirectiveV2("reconcile", "terminal receipt bekleniyor", (D,)),
        ),
        "resumability": Resumability.RECONCILIATION_REQUIRED,
        "next_safe_action": NextSafeActionV2("dispatch", "build", "siradaki adim"),
        "created_at": NOW,
        "test_and_eval_digests": (digest("test"),),
        "observed_lease_id": uid(21),
        "observed_fencing_token": 3,
    }
    values.update(changes)
    return CheckpointV2(**values)  # type: ignore[arg-type]


def test_checkpoint_v2_has_exact_identity_and_authority_free_body() -> None:
    item = checkpoint()
    assert item.body()["schema"] == "zekam-checkpoint/v2"
    assert item.body()["identity"]["run_id"] == str(uid(6))
    assert item.body()["grants_authority"] is False
    assert item.body()["carries_active_lease"] is False
    assert item.checkpoint_digest.startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("completed_steps", ("research", "build")),
        ("pending_steps", ("verify",)),
        ("plan_steps", ("research", "build", "build")),
        ("step_results", ()),
    ],
)
def test_exact_partition_and_results_are_required(field: str, value: object) -> None:
    with pytest.raises(ValidationFailed):
        checkpoint(**{field: value})


def test_effect_and_verification_require_exact_evidence_refs() -> None:
    with pytest.raises(PolicyViolation, match="terminal receipt"):
        step_result("build", D, EffectKind.FILE_WRITE)
    with pytest.raises(PolicyViolation, match="verification"):
        step_result("verify", D, EffectKind.NONE, verification_required=True)
    result = step_result(
        "build",
        D,
        EffectKind.FILE_WRITE,
        receipt_refs=(uid(30),),
        verification_refs=(uid(31),),
        verification_required=True,
    )
    assert result.receipt_refs == (uid(30),)


def test_revision_chain_is_explicit() -> None:
    with pytest.raises(ValidationFailed, match="Ilk checkpoint"):
        checkpoint(previous_checkpoint_id=uid(30), previous_checkpoint_digest=D)
    with pytest.raises(ValidationFailed, match="previous"):
        checkpoint(revision=2)
    item = checkpoint(revision=2, previous_checkpoint_id=uid(30), previous_checkpoint_digest=D)
    assert item.revision == 2


def test_sandbox_disposition_is_exact() -> None:
    with pytest.raises(ValidationFailed, match="Not-applicable"):
        SandboxBindingV2(SandboxDisposition.NOT_APPLICABLE, sandbox_id="sandbox:1")
    with pytest.raises(ValidationFailed, match="Dirty sandbox"):
        SandboxBindingV2(SandboxDisposition.DIRTY, "sandbox:1", "rev")
    dirty = SandboxBindingV2(SandboxDisposition.DIRTY, "sandbox:1", "rev", D, D)
    assert dirty.body()["dirty_state_digest"] == D


@pytest.mark.parametrize(
    "unsafe",
    (
        "C:\\Users\\person\\repo",
        "/home/person/repo",
        "../escape",
        "password=hunter2",
        "raw_prompt=ignore",
        "sk-sensitive",
    ),
)
def test_portable_fields_reject_paths_secrets_and_raw_prompts(unsafe: str) -> None:
    with pytest.raises(PolicyViolation):
        NextSafeActionV2("dispatch", "build", unsafe)


def test_logical_resources_must_be_portable_sorted_and_disjoint() -> None:
    with pytest.raises(ValidationFailed):
        checkpoint(logical_read_resources=("path:project-1:z", "path:project-1:a"))
    with pytest.raises(ValidationFailed, match="read ve write"):
        checkpoint(
            logical_read_resources=("path:project-1:src/same.py",),
            logical_write_resources=("path:project-1:src/same.py",),
        )
    with pytest.raises(ValidationFailed):
        checkpoint(logical_read_resources=("path:project-1:C:\\secret",))


def test_authority_lease_and_approval_cannot_be_inherited() -> None:
    for field in ("grants_authority", "carries_active_lease", "approval_inherited"):
        with pytest.raises(PolicyViolation):
            checkpoint(**{field: True})


def test_budget_and_lease_observation_are_validated() -> None:
    with pytest.raises(ValidationFailed, match="negatif"):
        checkpoint(tokens_used=-1)
    with pytest.raises(ValidationFailed, match="pozitif"):
        checkpoint(observed_fencing_token=0)
    with pytest.raises(ValidationFailed, match="zorunludur"):
        checkpoint(observed_lease_id=None)


def test_open_effect_claims_are_unique() -> None:
    effect = OpenEffect(uid(20), D, OpenEffectState.UNKNOWN)
    with pytest.raises(ValidationFailed, match="Open effect"):
        checkpoint(open_effects=(effect, effect))


def test_digest_covers_all_semantic_measurements_and_time() -> None:
    original = checkpoint()
    variants = (
        replace(original, tokens_used=original.tokens_used + 1),
        replace(original, cost_micros_used=original.cost_micros_used + 1),
        replace(original, attempts_used=original.attempts_used + 1),
        replace(original, created_at=original.created_at + dt.timedelta(seconds=1)),
        replace(
            original,
            open_effects=(OpenEffect(uid(22), digest("other"), OpenEffectState.UNKNOWN),),
        ),
        replace(original, bindings=replace(original.bindings, policy_digest=digest("policy-2"))),
    )
    assert len({original.checkpoint_digest, *(item.checkpoint_digest for item in variants)}) == 7


def test_next_action_must_target_pending_step() -> None:
    with pytest.raises(ValidationFailed, match="pending"):
        checkpoint(next_safe_action=NextSafeActionV2("dispatch", "research", "already done"))


def test_terminal_checkpoint_has_no_next_action() -> None:
    item = checkpoint(
        completed_steps=("research", "build", "verify"),
        pending_steps=(),
        step_results=(
            step_result("research", digest("research"), EffectKind.NONE),
            step_result("build", digest("build"), EffectKind.NONE),
            step_result("verify", digest("verify"), EffectKind.NONE),
        ),
        open_effects=(),
        resumability=Resumability.SAFE_CONTINUE,
        next_safe_action=None,
    )
    assert item.next_safe_action is None

    with pytest.raises(ValidationFailed, match="Pending checkpoint"):
        checkpoint(next_safe_action=None)
