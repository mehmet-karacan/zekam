from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import UUID

import pytest

from zekam.application.memory_continuity import (
    ContinuityReceiptKind,
    HydrationPreparation,
    MemoryContinuityService,
)
from zekam.application.recovery_reconciliation import (
    RECOVERY_SCHEMA,
    RecoveryReconciliationPlan,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_capability_runtime import (
    MAX_PROVIDER_CALLS,
    CapabilityRuntimeApprovalManifest,
    CapabilityRuntimeCallOutcome,
    CapabilityRuntimeCallStatus,
    CapabilityRuntimeContinuityState,
    CapabilityRuntimeDerivation,
    CapabilityRuntimeEpisodeOutcome,
    CapabilityRuntimeEpisodeStatus,
    CapabilityRuntimeOutcome,
    CapabilityRuntimeSkippedSlot,
    CapabilityRuntimeSlot,
    CapabilityRuntimeStatus,
    CapabilityRuntimeTurnCheckpoint,
)
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
)
from zekam.domain.session_continuity import (
    CompactionReceipt,
    CompactionStatus,
    ContextOmissionReference,
    DigestReference,
    HydrationInventoryEntry,
    HydrationInventorySnapshot,
    SessionHydrationReceipt,
    TruthClass,
)
from zekam.domain.session_continuity import DataClassification as ContinuityClassification

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 20))


def _manifest() -> CapabilityRuntimeApprovalManifest:
    return CapabilityRuntimeApprovalManifest(
        IDS[0],
        IDS[1],
        IDS[2],
        IDS[3],
        "git:revision",
        tuple(f"model-{index}" for index in range(7)),
        tuple(digest(f"task-{index}") for index in range(3)),
        digest("approval"),
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"source_revision": " "}, "source revision"),
        ({"source_revision": "https://source"}, "source revision"),
        ({"model_ids": ("same",) * 7}, "unique model"),
        ({"model_ids": ("", *(f"m-{i}" for i in range(6)))}, "model kimligi"),
        ({"task_digests": (digest("same"),) * 3}, "unique task"),
        ({"episode_count": 20}, "21x8/168"),
        ({"max_retries": 1}, "21x8/168"),
    ],
)
def test_capability_manifest_exact_contract(changes: dict[str, object], message: str) -> None:
    manifest = _manifest()
    assert manifest.manifest_digest == _manifest().manifest_digest
    with pytest.raises(ValidationFailed, match=message):
        replace(manifest, **changes)  # type: ignore[arg-type]


def _slot() -> CapabilityRuntimeSlot:
    template = {"model": "backend", "messages": [{"role": "user", "content": "fixture"}]}
    return CapabilityRuntimeSlot(
        "model-0",
        digest("task"),
        1,
        1,
        IDS[4],
        "provider",
        "backend",
        "endpoint/resource",
        "call/resource",
        digest("endpoint"),
        "provider-contract-call-1",
        "call-1",
        digest("fixture"),
        digest("fixture-identity"),
        128,
        template,
        digest(template),
        digest("derivation"),
        digest("seed"),
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"model_id": ""}, "model kimligi"),
        ({"provider_ref": ""}, "provider/endpoint/operation"),
        ({"turn_number": 0}, "turn 1..8"),
        ({"turn_number": 9}, "turn 1..8"),
        ({"ordinal": 0}, "ordinal 1..168"),
        ({"ordinal": 169}, "ordinal 1..168"),
        ({"max_output_tokens": 0}, "output token cap"),
        ({"max_output_tokens": 16385}, "output token cap"),
        ({"backend_model": "drift"}, "model/template mismatch"),
        ({"request_template_digest": digest("drift")}, "template digest mismatch"),
    ],
)
def test_capability_slot_bounds_and_digest(changes: dict[str, object], message: str) -> None:
    slot = _slot()
    assert slot.slot_digest == _slot().slot_digest
    with pytest.raises(ValidationFailed, match=message):
        replace(slot, **changes)  # type: ignore[arg-type]


def test_capability_continuity_derivation_and_checkpoint_guards() -> None:
    state = CapabilityRuntimeContinuityState(
        {"facts": [], "open_questions": [], "risks": [], "next_action": "continue"},
        digest("state"),
        digest("prior"),
        digest("attestation"),
        IDS[5],
        digest("event"),
    )
    with pytest.raises(ValidationFailed, match="exact typed keys"):
        replace(state, continuity_state={"facts": []})
    derivation = CapabilityRuntimeDerivation(
        {"model": "backend"},
        digest("request"),
        digest("authorization"),
        digest("effect"),
        "provider-contract-call-1",
        "provider-contract:call-1",
    )
    with pytest.raises(ValidationFailed, match="effect action"):
        replace(derivation, effect_action="provider-call")
    with pytest.raises(ValidationFailed, match="claim operation"):
        replace(derivation, claim_operation="provider-call")
    checkpoint = CapabilityRuntimeTurnCheckpoint(
        IDS[6], (1, 2), (3, 4), digest("result"), digest("cp")
    )
    with pytest.raises(ValidationFailed, match="partition disjoint"):
        replace(checkpoint, pending_turns=(2, 3))


def _call_outcome(status: CapabilityRuntimeCallStatus) -> CapabilityRuntimeCallOutcome:
    return CapabilityRuntimeCallOutcome(
        status,
        IDS[7],
        IDS[8],
        None if status is CapabilityRuntimeCallStatus.RECOVERY_REQUIRED else IDS[9],
        digest("result") if status is CapabilityRuntimeCallStatus.COMPLETED else None,
        None if status is CapabilityRuntimeCallStatus.COMPLETED else "adapter-failure",
        digest("evidence"),
        NOW,
    )


def test_capability_call_outcome_terminal_matrix() -> None:
    for status in CapabilityRuntimeCallStatus:
        _call_outcome(status)
    with pytest.raises(ValidationFailed, match="timezone"):
        replace(
            _call_outcome(CapabilityRuntimeCallStatus.COMPLETED),
            completed_at=NOW.replace(tzinfo=None),
        )
    with pytest.raises(ValidationFailed, match="receipt tasiyamaz"):
        replace(_call_outcome(CapabilityRuntimeCallStatus.RECOVERY_REQUIRED), receipt_id=IDS[9])
    with pytest.raises(ValidationFailed, match="Terminal provider call receipt"):
        replace(_call_outcome(CapabilityRuntimeCallStatus.FAILED), receipt_id=None)
    with pytest.raises(ValidationFailed, match="exact result"):
        replace(_call_outcome(CapabilityRuntimeCallStatus.COMPLETED), failure_category="failure")
    with pytest.raises(ValidationFailed, match="failure category"):
        replace(_call_outcome(CapabilityRuntimeCallStatus.FAILED), failure_category=None)


def test_capability_skipped_and_episode_terminal_matrices() -> None:
    CapabilityRuntimeSkippedSlot(IDS[10], "model-contract-failure", digest("skip"))
    with pytest.raises(ValidationFailed, match="model-contract reason"):
        CapabilityRuntimeSkippedSlot(IDS[10], "timeout", digest("skip"))
    successful = CapabilityRuntimeEpisodeOutcome(
        "model",
        digest("task"),
        IDS[11],
        CapabilityRuntimeEpisodeStatus.SUCCESSFUL,
        8,
        8,
        None,
        None,
        digest("episode"),
        NOW,
    )
    failed = CapabilityRuntimeEpisodeOutcome(
        "model",
        digest("task"),
        IDS[11],
        CapabilityRuntimeEpisodeStatus.MODEL_CONTRACT_FAILED,
        3,
        3,
        3,
        "model-contract-failure",
        digest("episode"),
        NOW,
    )
    recovery = CapabilityRuntimeEpisodeOutcome(
        "model",
        digest("task"),
        IDS[11],
        CapabilityRuntimeEpisodeStatus.RECOVERY_REQUIRED,
        2,
        1,
        2,
        "runtime-crash",
        digest("episode"),
        NOW,
    )
    assert successful.successful_calls == 8 and failed.failure_turn == 3 and recovery.reason_code
    for changes, message in (
        ({"model_id": ""}, "model kimligi"),
        ({"completed_at": NOW.replace(tzinfo=None)}, "terminal timezone"),
        ({"attempted_calls": 9}, "call sayilari"),
        ({"attempted_calls": 7, "successful_calls": 7}, "exact sekiz call"),
        ({"failure_turn": 2}, "terminal episode kaniti"),
    ):
        base = successful if "failure_turn" not in changes else failed
        with pytest.raises(ValidationFailed, match=message):
            replace(base, **changes)
    with pytest.raises(ValidationFailed, match="Recovery episode reason"):
        replace(recovery, reason_code=None)


def test_capability_aggregate_completed_partial_and_recovery_contracts() -> None:
    calls = tuple(digest(f"call-{index}") for index in range(MAX_PROVIDER_CALLS))
    completed = CapabilityRuntimeOutcome(
        CapabilityRuntimeStatus.COMPLETED, MAX_PROVIDER_CALLS, 0, calls, digest("all"), NOW
    )
    partial = CapabilityRuntimeOutcome(
        CapabilityRuntimeStatus.PARTIAL,
        1,
        0,
        calls[:1],
        digest("partial"),
        NOW,
        successful_episode_count=0,
    )
    recovery = replace(partial, status=CapabilityRuntimeStatus.RECOVERY_REQUIRED)
    assert completed.score_eligible and not completed.routing_eligible
    assert not partial.score_eligible and not recovery.score_eligible
    for changes, message in (
        ({"completed_at": NOW.replace(tzinfo=None)}, "aggregate timezone"),
        ({"actual_retries": 1}, "retry"),
        ({"actual_provider_calls": MAX_PROVIDER_CALLS + 1}, "call budget"),
        ({"call_evidence_digests": ()}, "evidence/call"),
        (
            {
                "call_evidence_digests": (digest("same"), digest("same")),
                "actual_provider_calls": 2,
            },
            "unique",
        ),
        ({"successful_episode_count": 20}, "21x8 partition"),
    ):
        with pytest.raises(ValidationFailed, match=message):
            replace(completed, **changes)
    with pytest.raises(ValidationFailed, match="Partial capability runtime"):
        replace(completed, status=CapabilityRuntimeStatus.PARTIAL)


def _compaction_receipt() -> CompactionReceipt:
    return CompactionReceipt(
        IDS[0],
        IDS[1],
        IDS[2],
        IDS[3],
        IDS[4],
        "session/compact",
        "codex",
        digest("pre"),
        digest("draft"),
        "outbox/compact",
        digest("outbox"),
        None,
        None,
        None,
        None,
        None,
        CompactionStatus.PREPARED,
        NOW,
        None,
    )


@dataclass
class _Snapshot:
    ready_for_mutation: bool
    hydration_receipt_digest: str | None = digest("hydration")
    hydration_fresh: bool = True
    hydration_complete: bool = True
    open_gaps: tuple[object, ...] = ()


class _Repository:
    def __init__(self, snapshot: _Snapshot) -> None:
        self.snapshot = snapshot

    def read_session_snapshot(self, **_kwargs: object) -> _Snapshot:
        return self.snapshot


def _service(snapshot: _Snapshot) -> MemoryContinuityService:
    return MemoryContinuityService(_Repository(snapshot), object())  # type: ignore[arg-type]


def test_memory_continuity_public_plan_and_admission_contracts() -> None:
    receipt = _compaction_receipt()
    service = _service(_Snapshot(True))
    plan = service.prepare_compaction(
        receipt,
        idempotency_key="compact:one",
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
        context_digest=digest("context"),
    )
    assert plan.kind is ContinuityReceiptKind.COMPACTION
    plan.assert_integrity()
    snapshot = service.assert_mutating_admission(
        project_id=IDS[2],
        work_item_id=IDS[3],
        run_id=IDS[4],
        session_id="session/compact",
        client_id="codex",
    )
    assert snapshot.ready_for_mutation
    with pytest.raises(PolicyViolation, match="plan body drift"):
        replace(plan, receipt_digest=digest("drift")).assert_integrity()
    with pytest.raises(PolicyViolation, match="plan digest mismatch"):
        replace(plan, plan_digest=digest("drift")).assert_integrity()
    with pytest.raises(ValidationFailed, match="idempotency key"):
        service.prepare_compaction(
            receipt,
            idempotency_key=" ",
            source_digest=digest("source"),
            policy_digest=digest("policy"),
            migration_digest=digest("migration"),
            context_digest=digest("context"),
        )


@pytest.mark.parametrize(
    "snapshot",
    [
        _Snapshot(False, hydration_receipt_digest=None),
        _Snapshot(False, hydration_fresh=False),
        _Snapshot(False, hydration_complete=False),
        _Snapshot(False, open_gaps=(object(),)),
    ],
)
def test_memory_continuity_admission_rejects_each_gap(snapshot: _Snapshot) -> None:
    with pytest.raises(PolicyViolation, match="mutating admission"):
        _service(snapshot).assert_mutating_admission(
            project_id=IDS[2],
            work_item_id=IDS[3],
            run_id=IDS[4],
            session_id="session/compact",
            client_id="codex",
        )


def _recovery_document() -> dict[str, Any]:
    return {
        "schema": RECOVERY_SCHEMA,
        "project_id": str(IDS[0]),
        "work_item_id": str(IDS[1]),
        "task_plan_id": str(IDS[2]),
        "task_plan_digest": digest("task-plan"),
        "old_completion": {
            "job_id": str(IDS[3]),
            "attempt_id": str(IDS[4]),
            "claim_id": str(IDS[5]),
            "fencing_token": 1,
            "claim_digest": digest("claim"),
            "effect_digest": digest("effect"),
            "authorization_digest": digest("authorization"),
            "result_digest": digest("result"),
        },
        "checkpoint": {
            "checkpoint_id": "checkpoint/one",
            "source_revision": "git/revision",
            "plan_steps": ["one", "two"],
            "completed_steps": ["one"],
            "pending_steps": ["two"],
            "step_results": {"one": digest("one")},
            "context_manifest_digest": digest("manifest"),
            "journal_head_digest": digest("journal"),
            "next_safe_action": "run/two",
            "created_at": NOW.isoformat(),
        },
        "evidence_refs": [
            {"kind": "receipt", "ref": "receipt/one", "digest": digest("receipt"), "revision": 1}
        ],
        "outcome": "completed",
    }


def test_recovery_plan_public_parse_round_trip_and_defaults() -> None:
    document = _recovery_document()
    plan = RecoveryReconciliationPlan.from_dict(document)
    assert plan.as_dict()["dry_run"] is True
    assert plan.effect_request.resources == (plan.resource,)
    assert plan.adapter_digest == digest(
        {"adapter": "recovery-reconciliation/v1", "plan": plan.plan_digest}
    )
    without_outcome = dict(document)
    without_outcome.pop("outcome")
    assert RecoveryReconciliationPlan.from_dict(without_outcome).outcome == "completed"
    failed = dict(document, outcome="failed-no-effect")
    assert RecoveryReconciliationPlan.from_dict(failed).outcome == "failed-no-effect"


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda d: d.update(schema="other"), "schema"),
        (lambda d: d.update(extra=True), "alanlari exact"),
        (lambda d: d.update(project_id="not-uuid"), "UUID"),
        (lambda d: d.update(old_completion=[]), "old_completion object"),
        (lambda d: d.update(checkpoint=[]), "checkpoint object"),
        (lambda d: d["checkpoint"].update(step_results=[]), "step_results object"),
        (lambda d: d.update(evidence_refs={}), "evidence_refs array"),
        (lambda d: d.update(evidence_refs=["bad"]), "evidence_refs\\[0\\] object"),
        (lambda d: d.update(evidence_refs=[{"kind": "receipt"}]), "alanlari gecersiz"),
        (lambda d: d["checkpoint"].update(created_at="not-time"), "ISO-8601"),
        (lambda d: d["checkpoint"].update(created_at="2026-09-04T12:00:00"), "timezone"),
        (lambda d: d.update(outcome="unknown"), "outcome"),
    ],
)
def test_recovery_plan_rejects_malformed_documents(mutator: Any, message: str) -> None:
    document = _recovery_document()
    mutator(document)
    with pytest.raises(ValidationFailed, match=message):
        RecoveryReconciliationPlan.from_dict(document)


def test_recovery_plan_rejects_identity_drift_and_empty_evidence() -> None:
    plan = RecoveryReconciliationPlan.from_dict(_recovery_document())
    with pytest.raises(ValidationFailed, match="en az bir"):
        replace(plan, evidence_refs=())
    with pytest.raises(ValidationFailed, match="project kimligi"):
        replace(plan, project_id=IDS[9])
    with pytest.raises(ValidationFailed, match="work kimligi"):
        replace(plan, work_item_id=IDS[9])
    with pytest.raises(ValidationFailed, match="plan kimligi"):
        replace(plan, task_plan_id=IDS[9])


def _inventory(
    *, entries: tuple[HydrationInventoryEntry, ...] | None = None
) -> HydrationInventorySnapshot:
    return HydrationInventorySnapshot(
        IDS[0],
        IDS[1],
        IDS[2],
        IDS[3],
        "session/hydrate",
        "codex",
        "plan/current",
        "checkpoint/current",
        digest("source"),
        digest("policy"),
        digest("migration"),
        digest("context"),
        entries
        or (
            HydrationInventoryEntry(
                "context/required",
                digest("required"),
                3,
                TruthClass.REPO_FACT,
                ContinuityClassification.INTERNAL,
                True,
                "source/required",
                "git/revision",
            ),
            HydrationInventoryEntry(
                "context/optional",
                digest("optional"),
                2,
                TruthClass.REPO_FACT,
                ContinuityClassification.PUBLIC,
                False,
                "source/optional",
                "git/revision",
            ),
            HydrationInventoryEntry(
                "context/large",
                digest("large"),
                10,
                TruthClass.REPO_FACT,
                ContinuityClassification.INTERNAL,
                False,
                "source/large",
                "git/revision",
            ),
            HydrationInventoryEntry(
                "context/secret",
                digest("secret"),
                1,
                TruthClass.REPO_FACT,
                ContinuityClassification.RESTRICTED,
                False,
                "source/secret",
                "git/revision",
            ),
        ),
        (),
        (DigestReference("projection/active-work", digest("projection"), TruthClass.REPO_FACT),),
        digest("hydration-event"),
    )


def _hydration_request(
    inventory: HydrationInventorySnapshot, **changes: object
) -> HydrationPreparation:
    values: dict[str, object] = {
        "receipt_id": IDS[4],
        "realm_id": inventory.realm_id,
        "project_id": inventory.project_id,
        "work_item_id": inventory.work_item_id,
        "run_id": inventory.run_id,
        "session_id": inventory.session_id,
        "client_id": inventory.client_id,
        "token_budget": 6,
        "idempotency_key": "hydrate:one",
        "created_at": NOW,
    }
    values.update(changes)
    return HydrationPreparation(**values)  # type: ignore[arg-type]


class _Transaction:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


class _HydrationRepository(_Repository):
    def __init__(self, inventory: HydrationInventorySnapshot) -> None:
        super().__init__(_Snapshot(True))
        self.inventory = inventory
        self.connection = self
        self.stored: set[str] = set()

    def transaction(self) -> _Transaction:
        return _Transaction()

    def read_hydration_inventory(self, **_kwargs: object) -> HydrationInventorySnapshot:
        return self.inventory

    def store_hydration_receipt(self, receipt: object, *, idempotency_key: str) -> bool:
        del receipt
        created = idempotency_key not in self.stored
        self.stored.add(idempotency_key)
        return created


class _Authorizations:
    def __init__(self) -> None:
        self.authorization: Authorization | None = None

    def get(self, _authorization_id: UUID) -> Authorization:
        assert self.authorization is not None
        return self.authorization

    def consume(self, *_args: object, **_kwargs: object) -> object:
        return type("Consumed", (), {"consumed": True})()


def test_memory_hydration_inventory_selection_apply_and_replay() -> None:
    inventory = _inventory()
    repository = _HydrationRepository(inventory)
    authorizations = _Authorizations()
    service = MemoryContinuityService(cast(Any, repository), authorizations)
    plan = service.prepare_from_inventory(_hydration_request(inventory), inventory)
    receipt = cast(SessionHydrationReceipt, plan.receipt)
    assert [item.ref for item in receipt.required_selections] == ["context/required"]
    assert [item.ref for item in receipt.optional_selections] == ["context/optional"]
    assert {item.reason_code for item in receipt.omissions} == {
        "classification-excluded",
        "token-budget",
    }
    authorization = Authorization.issue(
        realm_id=inventory.realm_id,
        actor_id=IDS[8],
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(plan.resource,),
            allowed_effects=("database-write",),
        ),
        risk="medium",
        lifetime=dt.timedelta(minutes=5),
        work_item_id=inventory.work_item_id,
        now=NOW,
    )
    authorizations.authorization = authorization
    first = service.apply(plan, authorization_id=authorization.id, now=NOW)
    replay = service.apply(plan, authorization_id=authorization.id, now=NOW)
    assert first.created and not replay.created
    assert first.result_digest == first.result_digest


def test_memory_hydration_rejects_budget_identity_stale_and_required_policy() -> None:
    inventory = _inventory()
    service = MemoryContinuityService(cast(Any, _HydrationRepository(inventory)), _Authorizations())
    with pytest.raises(ValidationFailed, match="token budget"):
        service.prepare_from_inventory(_hydration_request(inventory, token_budget=0), inventory)
    with pytest.raises(PolicyViolation, match="identity binding drift"):
        service.prepare_from_inventory(_hydration_request(inventory, project_id=IDS[9]), inventory)
    with pytest.raises(PolicyViolation, match="stale"):
        service.prepare_from_inventory(
            _hydration_request(inventory, source_digest=digest("old-source")), inventory
        )
    required_secret = replace(
        inventory.entries[0], classification=ContinuityClassification.RESTRICTED
    )
    forbidden = _inventory(entries=(required_secret,))
    with pytest.raises(PolicyViolation, match="classification policy"):
        service.prepare_from_inventory(_hydration_request(forbidden), forbidden)
    with pytest.raises(PolicyViolation, match="token budget"):
        service.prepare_from_inventory(_hydration_request(inventory, token_budget=2), inventory)
    omitted = replace(
        inventory,
        known_omissions=(ContextOmissionReference("context/omitted", "missing", True),),
    )
    with pytest.raises(PolicyViolation, match="omission fail-closed"):
        service.prepare_from_inventory(_hydration_request(omitted), omitted)


def test_memory_hydration_apply_rejects_inventory_and_authorization_drift() -> None:
    inventory = _inventory()
    repository = _HydrationRepository(inventory)
    authorizations = _Authorizations()
    service = MemoryContinuityService(cast(Any, repository), authorizations)
    plan = service.prepare_from_inventory(_hydration_request(inventory), inventory)
    authorizations.authorization = Authorization.issue(
        realm_id=inventory.realm_id,
        actor_id=IDS[8],
        plan_digest=digest("wrong-plan"),
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(plan.resource,), allowed_effects=("database-write",)
        ),
        risk="medium",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        service.apply(
            plan,
            authorization_id=authorizations.authorization.id,
            now=NOW.replace(tzinfo=None),
        )
    with pytest.raises(Exception, match="authorization binding"):
        service.apply(plan, authorization_id=authorizations.authorization.id, now=NOW)
    repository.inventory = replace(inventory, source_digest=digest("new-source"))
    with pytest.raises(PolicyViolation, match="replan required"):
        service.apply(plan, authorization_id=authorizations.authorization.id, now=NOW)


def test_recovery_parser_rejects_nested_exact_keys_and_bad_evidence_revision() -> None:
    for section, extra in (("old_completion", "extra"), ("checkpoint", "extra")):
        document = _recovery_document()
        document[section][extra] = True
        with pytest.raises(ValidationFailed, match="alanlari exact"):
            RecoveryReconciliationPlan.from_dict(document)
    document = _recovery_document()
    document["evidence_refs"][0]["revision"] = 0
    with pytest.raises(ValidationFailed, match="revision pozitif"):
        RecoveryReconciliationPlan.from_dict(document)
    document = _recovery_document()
    document["old_completion"]["fencing_token"] = 0
    with pytest.raises(ValidationFailed, match="fencing token"):
        RecoveryReconciliationPlan.from_dict(document)
