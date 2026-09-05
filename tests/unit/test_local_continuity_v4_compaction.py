from __future__ import annotations

import hashlib
import inspect
import pickle
import sqlite3
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from zekam.application import local_continuity_v4_compaction as contract
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_v4_compaction import (
    PreCompactionFailure,
    PreCompactionResult,
    PreparedPreCompactionPlan,
    ResolvedPreCompactionBinding,
    SealedPreCompactionDeadline,
    VerifiedAckDecision,
    checkpoint_ready,
    issue_ack_decision,
    recovery_required,
    rejected,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure import macos_precompaction_supervisor as supervisor
from zekam.infrastructure.clients import codex_macos_0151_precompaction_client as client
from zekam.infrastructure.sqlite import local_continuity_v4_compaction as writer
from zekam.infrastructure.sqlite.local_continuity_v4_compaction import _OwnedImmediateTransaction


def _test_generation(monkeypatch: pytest.MonkeyPatch) -> supervisor._DarwinGenerationOwner:
    listener = supervisor._DarwinListenerObservation(
        "/private/tmp/zekam-precompact-unit.sock", 7, 501, 0o600, 1, 2, 1, 1
    )
    job = supervisor._DarwinJobObservation(
        1,
        b"\0" * 16,
        supervisor.JOB_LABEL,
        supervisor.LISTENER_KEY,
        101,
        501,
        "service-start",
        digest("service-artifact"),
        digest("protocol"),
        listener,
    )
    adapter = object.__new__(supervisor._DarwinAuthorityAdapter)
    monkeypatch.setattr(supervisor._DarwinAuthorityAdapter, "observe_current", lambda _self: job)
    owner = object.__new__(supervisor._DarwinGenerationOwner)
    object.__setattr__(owner, "_adapter", adapter)
    object.__setattr__(owner, "_job", job)
    object.__setattr__(owner, "_digest", digest("test-generation"))
    seal = digest("test-generation-seal")
    object.__setattr__(owner, "_seal", seal)
    monkeypatch.setitem(supervisor._GENERATIONS, seal, owner)
    monkeypatch.setitem(supervisor._GENERATION_PARITY, seal, supervisor._generation_bytes(owner))
    return owner


@pytest.mark.parametrize(
    ("category", "expected_sha"),
    (
        (
            PreCompactionFailure.VALIDATION,
            "4f457656b5dfc945f1e6b4833972769b2d8dd9e61618250f35a508b699ecf10a",
        ),
        (
            PreCompactionFailure.PENDING_WORK,
            "7da104c915c9941c5d5f2c11eb62709bf90acc4918cba5c981b913de952d73f4",
        ),
        (
            PreCompactionFailure.UNPERSISTED_DELTA,
            "e56dd61ea0bd60e4463a2a8f6120b6dd85b28cd7ccc81da2a288cd6d3db03d52",
        ),
        (
            PreCompactionFailure.SOURCE_DRIFT,
            "3e0b9f4a0facf15c7ad972883e740b69b94689094005841b6b3dc4568b40ac36",
        ),
        (
            PreCompactionFailure.PROCESS_DRIFT,
            "be2f21f1fc18b648c1f988641403bcdb1404778bef960e93d0f24a07ca29dc9b",
        ),
        (
            PreCompactionFailure.STORAGE_UNAVAILABLE,
            "3b01faabf2a42ad37043f33139185f7861b9d44f074ad96955ff7e49ffefc249",
        ),
        (
            PreCompactionFailure.RECOVERY_REQUIRED,
            "25bcb1e379b1feaf8c9053f1290f94610e7d9f0f16174c09f7b02dda3ef3ec59",
        ),
        (
            PreCompactionFailure.DEADLINE,
            "b3976fb81a91cce9150352d3c8a40cdbca166ea7420afc7e8407bbf32646c7bb",
        ),
    ),
)
def test_failure_vectors_are_exact_and_never_authoritative(
    category: PreCompactionFailure, expected_sha: str
) -> None:
    for value in (rejected(category), recovery_required(category)):
        assert type(value) is PreCompactionResult
        assert hashlib.sha256(value.stdout).hexdigest() == expected_sha
        assert value.durable_reopen_verified is False
        assert value.native_ack_observed is False
        assert value.grants_authority is False


def test_direct_constructor_and_forgery_have_no_test_issuer() -> None:
    for sealed in (
        SealedPreCompactionDeadline,
        PreparedPreCompactionPlan,
        VerifiedAckDecision,
        PreCompactionResult,
    ):
        with pytest.raises(PolicyViolation):
            sealed()
        with pytest.raises(TypeError):
            type("Bad", (sealed,), {})
    assert not hasattr(contract, "_issue_test_deadline")
    assert not hasattr(contract, "issue_precompaction_deadline")
    with pytest.raises(PolicyViolation):
        checkpoint_ready(object(), replay=False)
    with pytest.raises(PolicyViolation):
        issue_ack_decision({})


def test_caller_data_cannot_enter_private_success_factories() -> None:
    forged_generation = object()
    with pytest.raises(PolicyViolation):
        contract._issue_deadline(forged_generation, lambda: 1)
    with pytest.raises(PolicyViolation):
        contract._issue_plan(forged_generation, {})
    with pytest.raises(PolicyViolation):
        contract._issue_ack_decision(forged_generation, {})
    with pytest.raises((PolicyViolation, ValidationFailed)):
        contract._checkpoint_ready(forged_generation, object(), replay=False)  # type: ignore[arg-type]


def test_failure_result_clone_mutation_deepcopy_pickle_and_replace_are_rejected() -> None:
    original = rejected(PreCompactionFailure.VALIDATION)
    clone = object.__new__(PreCompactionResult)
    for name in PreCompactionResult.__dataclass_fields__:
        object.__setattr__(clone, name, getattr(original, name))
    with pytest.raises(PolicyViolation):
        clone.__post_init__()
    object.__setattr__(original, "failure_category", "DEADLINE")
    with pytest.raises(PolicyViolation):
        _ = original.stdout
    fresh = rejected(PreCompactionFailure.VALIDATION)
    for clone_operation in (deepcopy, pickle.dumps, replace):
        with pytest.raises((TypeError, PolicyViolation)):
            clone_operation(fresh)


def test_fixed_transaction_owner_rolls_back_and_hides_raw_connection() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("create table evidence(value text)")
    owner = _OwnedImmediateTransaction(db)
    owner.planned("sha256:" + "a" * 64)
    owner.applying()
    db.execute("insert into evidence values('partial')")
    with pytest.raises(PolicyViolation):
        _ = owner.db
    assert db.execute("select count(*) from evidence").fetchone()[0] == 0
    assert owner.state == "rolled-back"
    db.close()


def test_hook_does_not_swallow_process_control_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(_raw: bytes) -> dict[str, object]:
        raise SystemExit(73)

    monkeypatch.setattr(client, "_strict_json", interrupt)
    with pytest.raises(SystemExit, match="73"):
        client.production_precompaction_hook(b"{}")


def test_authority_modules_have_no_structural_or_test_success_boundary() -> None:
    app_source = inspect.getsource(contract)
    writer_source = inspect.getsource(writer)
    assert "_issue_test_deadline" not in app_source
    assert "Protocol" not in writer_source and "typing import Any" not in writer_source
    assert "select * from" not in inspect.getsource(
        writer.SQLiteDormantV4PreCompactionWriter._selected_census
    )
    assert "apply_precompaction_graph" in inspect.getsource(_OwnedImmediateTransaction)


def test_client_failure_path_uses_exact_preallocated_bytes() -> None:
    malformed = (b"{}", b'{"x":1,"x":2}', b"\xff", b"x" * (client.MAX_FRAME_BYTES + 1))
    for raw in malformed:
        assert client.production_precompaction_hook(raw) == client.VALIDATION_FAILURE_STDOUT
    assert client.DARWIN_LAUNCHD_CAPABILITY_OBSERVED is False
    assert client.PRODUCTION_GENERATION_ISSUED is False


def test_resolved_binding_recomputes_exact_digest_and_state() -> None:
    binding = ContinuityBinding(
        "018f0000-0000-7000-8000-000000000201",
        "external-session",
        "018f0000-0000-7000-8000-000000000202",
        "018f0000-0000-7000-8000-000000000203",
        "codex",
        "macbook",
        "018f0000-0000-7000-8000-000000000204",
        digest("task"),
        digest("plan"),
        digest("policy"),
    )
    values = {
        "binding_digest": binding.binding_digest,
        "attachment_id": "018f0000-0000-7000-8000-000000000205",
        "head_revision_digest": digest("head"),
        "head_state": "hydrated",
        "active_manifest_digest": digest("manifest"),
        "active_hydration_receipt_digest": digest("receipt"),
    }
    resolved = ResolvedPreCompactionBinding(
        binding,
        values["attachment_id"],
        values["head_revision_digest"],
        values["head_state"],
        values["active_manifest_digest"],
        values["active_hydration_receipt_digest"],
        digest({"schema": "zekam-precompact-existing-binding-resolution/v1", **values}),
    )
    assert resolved.binding is binding
    with pytest.raises(PolicyViolation, match="state"):
        replace(resolved, head_state="closed")
    with pytest.raises(PolicyViolation, match="digest"):
        replace(resolved, resolution_digest=digest("forged"))
    with pytest.raises(ValidationFailed, match="resolved binding"):
        replace(resolved, binding=object())  # type: ignore[arg-type]


def test_deadline_issuance_and_clock_boundaries_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _test_generation(monkeypatch)
    for start in (None, -1):

        def invalid_clock(value: Any = start) -> Any:
            return value

        with pytest.raises(ValidationFailed, match="clock"):
            contract._issue_deadline(generation, invalid_clock)

    values = iter((10, None))

    def drifting_clock() -> Any:
        return next(values)

    deadline = contract._issue_deadline(generation, drifting_clock)
    with pytest.raises(PolicyViolation, match="clock"):
        deadline.remaining_ns()
    for reserve in (None, -1):
        valid = contract._issue_deadline(generation, lambda: 10)
        with pytest.raises(ValidationFailed, match="reserve"):
            valid.remaining_seconds(reserve_ms=reserve)  # type: ignore[arg-type]


def test_private_factories_validate_shapes_after_real_generation_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _test_generation(monkeypatch)
    with pytest.raises(ValidationFailed, match="plan fields"):
        contract._issue_plan(generation, {})
    with pytest.raises(ValidationFailed, match="decision body"):
        contract._issue_ack_decision(generation, {})
    for factory in (rejected, recovery_required):
        with pytest.raises(ValidationFailed, match="failure category"):
            factory("VALIDATION")  # type: ignore[arg-type]
