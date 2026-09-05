# mypy: disable-error-code="assignment,arg-type"
from __future__ import annotations

import datetime as dt
import math
from dataclasses import replace as dc_replace
from typing import Any
from uuid import UUID

import pytest

from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
    ContextSelection,
    EvidenceReference,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.execution_topology import (
    ExecutionTopologyDecision,
    ExecutionTopologyPattern,
    GraphExecutionReceipt,
    GraphNodeMode,
    GraphNodeReceipt,
    GraphNodeTerminalState,
    GraphTerminalState,
    LoopSuitabilityAssessment,
    MeasurementSourceTier,
    TournamentBudget,
    TournamentCandidateAssignment,
    TournamentPlan,
)
from zekam.domain.hook_runtime import (
    CompiledHookEntry,
    CompiledHookSet,
    HookConfigurationSnapshot,
    HookEventType,
    HookExecutionMode,
    HookFailurePolicy,
    HookLoadState,
    HookRuntimeRevision,
    HookSpecRevision,
    validate_payload,
)
from zekam.domain.knowledge import (
    Artifact,
    CodeSymbol,
    ContentUnit,
    DatabaseObject,
    IngestionJob,
    IngestionStage,
    Locator,
    NormalizedDocument,
    ScanLimits,
    SourceFormat,
    SourceVersion,
    UnitKind,
    VersionState,
    assert_safe_relative,
    is_denied,
)
from zekam.domain.loop_change_set import (
    LoopChangeBaseline,
    LoopOwnedChangeSet,
    LoopPatchApplyCheck,
    LoopRollbackPlan,
    LoopRollbackReceipt,
    LoopSourceEntry,
    SourceEntryKind,
)
from zekam.domain.loop_policy import (
    LoopAdmission,
    LoopAttemptOutcome,
    LoopAttemptRequest,
    LoopDeltaKind,
    LoopEffectClass,
    LoopPolicy,
    LoopTerminalState,
    LoopValidation,
)
from zekam.domain.optimization import (
    MeasurementEvidence,
    MetricAggregation,
    MetricDirection,
    MetricProgressResult,
    MetricRole,
    MetricSpec,
    OptimizationObjective,
    ProgressState,
    ProgressVector,
    ValidatorAsset,
    ValidatorAssetManifest,
    ValidatorAssetRole,
    evaluate_progress,
)
from zekam.domain.session_continuity import (
    CloseStatus,
    CompactionReceipt,
    CompactionStatus,
    ContextOmissionReference,
    ContextSelectionReference,
    DataClassification,
    DigestReference,
    FreshnessDimension,
    ProjectionGenerationReceipt,
    SessionCloseReceipt,
    SessionLifecycleEvent,
    TruthClass,
    TypedMetadata,
)
from zekam.domain.tool_registry import (
    CompiledToolSet,
    ModelToolPayloadBinding,
    ToolDispatchBinding,
    ToolDispatchPlan,
    ToolDispatchWave,
    ToolExecutionPermit,
    ToolExposure,
    ToolRuntimeRevision,
    ToolSetEntry,
    ToolSpecRevision,
    assert_tool_dispatch_binding,
    tool_entry_map,
)

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
NAIVE = NOW.replace(tzinfo=None)
LATER = NOW + dt.timedelta(hours=1)
U = tuple(UUID(int=value) for value in range(1, 20))
D = digest("wp16-domain-contract-matrix")


def _assert_rejected(factory: Any, variants: tuple[dict[str, Any], ...]) -> None:
    for changes in variants:
        with pytest.raises((ValidationFailed, PolicyViolation, ValueError, TypeError)):
            factory(**changes)


def _replace(instance: Any, **changes: Any) -> Any:
    return dc_replace(instance, **changes)


def _hook_spec(**changes: Any) -> HookSpecRevision:
    values: dict[str, Any] = {
        "realm_id": U[0],
        "hook_id": "audit.turn",
        "revision": 1,
        "event_type": HookEventType.TURN_START,
        "required": True,
        "source_layer": "managed",
        "timeout_ms": 100,
        "execution_mode": HookExecutionMode.INTERNAL,
        "input_schema": {"type": "object", "additionalProperties": False},
        "output_schema": {"type": "object", "additionalProperties": False},
        "permission_profile_name": "read-only",
        "permission_profile_digest": D,
        "failure_policy": HookFailurePolicy.ABORT,
        "created_at": NOW,
    }
    values.update(changes)
    return HookSpecRevision.create(**values)


def _hook_runtime(spec: HookSpecRevision, **changes: Any) -> HookRuntimeRevision:
    values: dict[str, Any] = {
        "realm_id": spec.realm_id,
        "hook_id": spec.hook_id,
        "hook_revision": spec.revision,
        "adapter_ref": "adapter/audit",
        "adapter_digest": D,
        "permission_capabilities": ("read", "read"),
        "load_state": HookLoadState.READY,
        "captured_at": NOW,
        "expires_at": LATER,
    }
    values.update(changes)
    return HookRuntimeRevision.create(**values)


def test_hook_spec_schema_integrity_and_fail_closed_matrix() -> None:
    spec = _hook_spec()
    spec.assert_integrity()
    assert spec.input_schema_digest == digest(spec.input_schema)
    validate_payload(spec.input_schema, {}, "payload")
    with pytest.raises(ValidationFailed, match="typed schema"):
        validate_payload({"type": "integer"}, "wrong", "payload")
    _assert_rejected(
        _hook_spec,
        (
            {"hook_id": " "},
            {"source_layer": "x" * 256},
            {"revision": 0},
            {"timeout_ms": 0},
            {"timeout_ms": 300_001},
            {"created_at": NAIVE},
            {"failure_policy": HookFailurePolicy.WARN},
            {"permission_profile_digest": "bad"},
            {"input_schema": {"type": "not-a-json-schema-type"}},
        ),
    )
    for forged in (
        _replace(spec, input_schema_digest=digest("forged")),
        _replace(spec, output_schema_digest=digest("forged")),
        _replace(spec, hook_digest=digest("forged")),
    ):
        with pytest.raises(PolicyViolation):
            forged.assert_integrity()


def test_hook_runtime_snapshot_and_compilation_guards() -> None:
    spec = _hook_spec()
    runtime = _hook_runtime(spec)
    assert runtime.permission_capabilities == ("read",)
    runtime.assert_integrity()
    _assert_rejected(
        lambda **kw: _hook_runtime(spec, **kw),
        (
            {"hook_revision": 0},
            {"adapter_ref": ""},
            {"adapter_digest": "bad"},
            {"captured_at": NAIVE},
            {"expires_at": NAIVE},
            {"expires_at": NOW},
        ),
    )
    with pytest.raises(PolicyViolation, match="digest mismatch"):
        _replace(runtime, adapter_ref="adapter/forged").assert_integrity()
    snapshot = HookConfigurationSnapshot.create(
        generation=1,
        hooks=(spec,),
        unavailable_optional=("z", "z"),
        required_load_errors=("failed", "failed"),
    )
    assert snapshot.unavailable_optional == ("z",)
    with pytest.raises(PolicyViolation, match="session baslangici"):
        snapshot.assert_session_startable()
    with pytest.raises(ValidationFailed, match="generation"):
        HookConfigurationSnapshot.create(generation=0, hooks=())
    with pytest.raises(ValidationFailed, match="duplicate"):
        HookConfigurationSnapshot.create(generation=1, hooks=(spec, spec))
    enabled = CompiledHookEntry(1, spec, runtime, None)
    with pytest.raises(ValidationFailed, match="ordinal"):
        _replace(enabled, ordinal=0)
    with pytest.raises(PolicyViolation, match="exact binding"):
        _replace(enabled, runtime=_replace(runtime, realm_id=U[1]))
    with pytest.raises(ValidationFailed, match="tutarsiz"):
        _replace(enabled, disabled_reason="impossible")
    compiled = CompiledHookSet.create(
        realm_id=U[0],
        generation=1,
        config_effective_digest=D,
        entries=(enabled,),
        required_load_errors=("required",),
    )
    with pytest.raises(PolicyViolation, match="session baslangici"):
        compiled.assert_session_startable()
    with pytest.raises(ValidationFailed, match="ordinal"):
        CompiledHookSet.create(
            realm_id=U[0],
            generation=1,
            config_effective_digest=D,
            entries=(_replace(enabled, ordinal=2),),
        )
    with pytest.raises(PolicyViolation, match="cross-realm"):
        CompiledHookSet.create(
            realm_id=U[1], generation=1, config_effective_digest=D, entries=(enabled,)
        )


def _tool_bundle() -> tuple[
    ToolSpecRevision, ToolRuntimeRevision, CompiledToolSet, ToolDispatchBinding
]:
    spec = ToolSpecRevision.create(
        realm_id=U[0],
        tool_id="repo.search",
        revision=1,
        name="Search",
        description="Search repo",
        input_schema_digest=D,
        output_schema_digest=D,
        created_at=NOW,
    )
    runtime = ToolRuntimeRevision.create(
        realm_id=U[0],
        tool_id=spec.tool_id,
        revision=1,
        adapter_ref="local/search",
        executable_revision="v1",
        executable_digest=D,
        permission_capabilities=("read", "read"),
        parallel_supported=True,
        captured_at=NOW,
        expires_at=LATER,
    )
    entry = ToolSetEntry(
        spec.tool_id, 1, ToolExposure.DIRECT, spec.spec_digest, runtime.runtime_digest
    )
    compiled = CompiledToolSet.create(
        realm_id=U[0],
        role="researcher",
        permission_profile_digest=D,
        entries=(entry,),
        created_at=NOW,
    )
    binding = ToolDispatchBinding(
        U[1],
        D,
        compiled.tool_set_digest,
        spec.tool_id,
        1,
        spec.spec_digest,
        runtime.runtime_digest,
        D,
    )
    return spec, runtime, compiled, binding


def test_tool_spec_runtime_set_dispatch_and_permit_guards() -> None:
    spec, runtime, compiled, binding = _tool_bundle()
    spec.assert_digest()
    runtime.assert_digest()
    compiled.assert_digest()
    assert tool_entry_map(compiled)[spec.tool_id].revision == 1
    assert_tool_dispatch_binding(binding, compiled, spec, runtime, now=NOW)
    for forged, method in (
        (_replace(spec, name="forged"), "assert_digest"),
        (_replace(runtime, adapter_ref="forged"), "assert_digest"),
        (_replace(compiled, role="forged"), "assert_digest"),
    ):
        with pytest.raises(PolicyViolation):
            getattr(forged, method)()
    for changed in (
        {"now": NAIVE},
        {"binding": _replace(binding, tool_set_digest=D)},
        {"binding": _replace(binding, revision=2)},
        {"spec": _replace(spec, spec_digest=D)},
        {"runtime": _replace(runtime, runtime_digest=D)},
        {"now": LATER},
    ):
        args: dict[str, Any] = {
            "binding": binding,
            "compiled": compiled,
            "spec": spec,
            "runtime": runtime,
            "now": NOW,
        }
        args.update(changed)
        with pytest.raises(PolicyViolation):
            assert_tool_dispatch_binding(**args)
    forged_permit = ToolExecutionPermit(
        U[1],
        D,
        compiled.tool_set_digest,
        spec.tool_id,
        1,
        spec.spec_digest,
        runtime.runtime_digest,
        D,
        object(),
    )
    with pytest.raises(PolicyViolation, match="kanonik gate"):
        forged_permit.assert_for(binding)


def test_tool_construction_wave_plan_and_payload_boundaries() -> None:
    spec, runtime, compiled, _binding = _tool_bundle()
    _assert_rejected(
        lambda **kw: ToolSpecRevision.create(
            realm_id=U[0],
            tool_id=kw.get("tool_id", "x"),
            revision=kw.get("revision", 1),
            name=kw.get("name", "name"),
            description=kw.get("description", "desc"),
            input_schema_digest=kw.get("input_schema_digest", D),
            output_schema_digest=D,
            created_at=kw.get("created_at", NOW),
        ),
        (
            {"tool_id": ""},
            {"revision": 0},
            {"name": "x" * 256},
            {"input_schema_digest": "bad"},
            {"created_at": NAIVE},
        ),
    )
    _assert_rejected(
        lambda **kw: ToolRuntimeRevision.create(
            realm_id=U[0],
            tool_id="x",
            revision=kw.get("revision", 1),
            adapter_ref="adapter",
            executable_revision="v1",
            executable_digest=D,
            permission_capabilities=(),
            parallel_supported=False,
            captured_at=kw.get("captured_at", NOW),
            expires_at=kw.get("expires_at", LATER),
        ),
        ({"revision": 0}, {"captured_at": NAIVE}, {"expires_at": NAIVE}, {"expires_at": NOW}),
    )
    entry = compiled.entries[0]
    with pytest.raises(ValidationFailed):
        _replace(entry, revision=0)
    binding = ToolDispatchBinding(
        U[2],
        D,
        compiled.tool_set_digest,
        entry.tool_id,
        1,
        entry.spec_digest,
        entry.runtime_digest,
        D,
    )
    wave = ToolDispatchWave(1, (binding,))
    with pytest.raises(ValidationFailed):
        _replace(wave, ordinal=0)
    with pytest.raises(ValidationFailed):
        _replace(wave, bindings=())
    with pytest.raises(ValidationFailed):
        _replace(wave, bindings=(binding, binding))
    plan = ToolDispatchPlan.create(
        turn_execution_snapshot_digest=D,
        tool_set_digest=compiled.tool_set_digest,
        waves=(wave,),
        max_parallelism=1,
    )
    assert len(plan.waves) == 1
    for changed in ({"max_parallelism": 0}, {"waves": (_replace(wave, ordinal=2),)}):
        with pytest.raises(ValidationFailed):
            ToolDispatchPlan.create(
                **{
                    "turn_execution_snapshot_digest": D,
                    "tool_set_digest": compiled.tool_set_digest,
                    "waves": (wave,),
                    "max_parallelism": 1,
                    **changed,
                }
            )
    payload = compiled.compile_model_payload().serialize_request({"prompt": "x"})
    payload.binding.assert_valid()
    with pytest.raises(PolicyViolation, match="duplicate"):
        _replace(payload.binding, ordered_tool_ids=(spec.tool_id, spec.tool_id)).assert_valid()
    with pytest.raises(PolicyViolation, match="kanonik serializer"):
        _replace(payload.binding, _token=object()).assert_valid()
    with pytest.raises(PolicyViolation, match="muhru"):
        _replace(payload.binding, request_payload_digest=D).assert_valid()
    with pytest.raises(PolicyViolation, match="onceden"):
        compiled.compile_model_payload().serialize_request({"tools": []})
    with pytest.raises(PolicyViolation, match="bos"):
        compiled.compile_model_payload().serialize_request({}, tools_field=" ")
    with pytest.raises(PolicyViolation, match="mutation"):
        _replace(payload, payload={"prompt": "tampered"}).assert_unchanged()
    invalid_binding = ModelToolPayloadBinding(D, False, (), D, D, object(), b"bad")
    with pytest.raises(PolicyViolation):
        invalid_binding.assert_valid()
    assert runtime.permission_capabilities == ("read",)


def _baseline() -> LoopChangeBaseline:
    before = LoopSourceEntry("src/a.py", SourceEntryKind.FILE, digest("before"))
    return LoopChangeBaseline(U[0], "rev-1", D, D, ("src/a.py",), (before,), (), NOW)


def test_loop_change_source_baseline_and_change_set_guards() -> None:
    missing = LoopSourceEntry("src/new.py", SourceEntryKind.MISSING, None)
    file_entry = LoopSourceEntry("src/a.py", SourceEntryKind.FILE, D)
    with pytest.raises(ValidationFailed):
        _replace(missing, content_digest=D)
    with pytest.raises(ValidationFailed):
        _replace(file_entry, content_digest=None)
    baseline = _baseline()
    assert baseline.baseline_digest.startswith("sha256:")
    for changed in (
        {"grants_authority": True},
        {"source_revision": " "},
        {"captured_at": NAIVE},
        {"tree_digest": "bad"},
        {"allowed_paths": ()},
        {"allowed_paths": ("b", "a")},
        {"allowed_entries": ()},
        {"protected_dirty_entries": (LoopSourceEntry("src/a.py", SourceEntryKind.FILE, D),)},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _replace(baseline, **changed)
    after = LoopSourceEntry("src/a.py", SourceEntryKind.FILE, digest("after"))
    changeset = LoopOwnedChangeSet.create(
        baseline=baseline,
        changed_resources=("src/a.py",),
        before_entries=baseline.allowed_entries,
        after_entries=(after,),
        forward_patch_digest=D,
        inverse_patch_digest=D,
        created_at=NOW,
    )
    assert changeset.change_set_digest.startswith("sha256:")
    for changed in (
        {"grants_authority": True},
        {"source_revision": ""},
        {"created_at": NAIVE},
        {"changed_resources": ()},
        {"before_entries": ()},
        {"after_entries": ()},
        {"after_entries": changeset.before_entries},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _replace(changeset, **changed)
    with pytest.raises(PolicyViolation, match="allowed path"):
        LoopOwnedChangeSet.create(
            baseline=baseline,
            changed_resources=("src/outside.py",),
            before_entries=(LoopSourceEntry("src/outside.py", SourceEntryKind.MISSING, None),),
            after_entries=(LoopSourceEntry("src/outside.py", SourceEntryKind.FILE, D),),
            forward_patch_digest=D,
            inverse_patch_digest=D,
            created_at=NOW,
        )
    with pytest.raises(PolicyViolation, match="exact baseline"):
        LoopOwnedChangeSet.create(
            baseline=baseline,
            changed_resources=("src/a.py",),
            before_entries=(file_entry,),
            after_entries=(after,),
            forward_patch_digest=D,
            inverse_patch_digest=D,
            created_at=NOW,
        )


def test_loop_rollback_plan_apply_check_and_receipt_guards() -> None:
    changeset = LoopOwnedChangeSet.create(
        baseline=_baseline(),
        changed_resources=("src/a.py",),
        before_entries=_baseline().allowed_entries,
        after_entries=(LoopSourceEntry("src/a.py", SourceEntryKind.FILE, digest("after")),),
        forward_patch_digest=D,
        inverse_patch_digest=D,
        created_at=NOW,
    )
    plan = LoopRollbackPlan(
        changeset.change_set_digest, U[0], "rev-1", changeset.changed_resources, D, D, "failed", NOW
    )
    assert plan.plan_digest.startswith("sha256:")
    for changed in (
        {"grants_authority": True},
        {"source_revision": ""},
        {"reason_code": ""},
        {"prepared_at": NAIVE},
        {"changed_resources": ("b", "a")},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _replace(plan, **changed)
    check = LoopPatchApplyCheck(plan.plan_digest, D, "rev-1", plan.changed_resources, D, True, NOW)
    assert check.check_digest.startswith("sha256:")
    for changed in (
        {"source_revision": ""},
        {"checked_at": NAIVE},
        {"changed_resources": ("b", "a")},
    ):
        with pytest.raises(ValidationFailed):
            _replace(check, **changed)
    receipt = LoopRollbackReceipt(
        plan.plan_digest,
        changeset.change_set_digest,
        check.check_digest,
        D,
        plan.changed_resources,
        D,
        NOW,
    )
    assert receipt.receipt_digest.startswith("sha256:")
    for changed in (
        {"status": "partial"},
        {"grants_authority": True},
        {"applied_at": NAIVE},
        {"changed_resources": ("b", "a")},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _replace(receipt, **changed)


def _assessment(**changes: Any) -> LoopSuitabilityAssessment:
    values: dict[str, Any] = {
        "measurement_available": True,
        "measurement_source_tier": MeasurementSourceTier.DETERMINISTIC_EXTERNAL,
        "measurement_estimated_cost_micros": 10,
        "action_estimated_cost_micros": 20,
        "reversible": True,
        "idempotent_or_receipt_bound": True,
        "creative_diversity_goal": False,
        "human_judgment_required": False,
        "distinct_deliverable_count": 1,
        "dependency_edge_count": 0,
        "expected_coordination_cost_micros": 2,
        "recommended_pattern": ExecutionTopologyPattern.BOUNDED_LOOP,
        "reason_codes": ("measured",),
    }
    values.update(changes)
    return LoopSuitabilityAssessment.create(**values)


def test_topology_assessment_decision_and_digest_guards() -> None:
    assessment = _assessment()
    assert assessment.measurement_to_action_ratio is not None
    assert _assessment(action_estimated_cost_micros=0).measurement_to_action_ratio is None
    assert _assessment(measurement_estimated_cost_micros=None).measurement_to_action_ratio is None
    for changed in (
        {"measurement_estimated_cost_micros": -1},
        {"reason_codes": ()},
        {"reason_codes": ("",)},
    ):
        with pytest.raises(ValidationFailed):
            _assessment(**changed)
    with pytest.raises(PolicyViolation, match="digest mismatch"):
        _replace(assessment, measurement_available=False)
    values: dict[str, Any] = {
        "pattern": ExecutionTopologyPattern.BOUNDED_LOOP,
        "objective_digest": D,
        "plan_digest": D,
        "node_modes": (("step", GraphNodeMode.DIRECT),),
        "parallelism_ceiling": 1,
        "estimated_calls": 1,
        "estimated_tokens": 10,
        "estimated_cost_micros": 5,
        "estimated_coordination_overhead_micros": 0,
        "required_human_gates": (),
        "reason_codes": ("bounded",),
    }
    decision = ExecutionTopologyDecision.create(**values)
    assert decision.computed_digest == decision.decision_digest
    for changed in (
        {"grants_authority": True},
        {"parallelism_ceiling": -1},
        {"node_modes": (("", GraphNodeMode.DIRECT),)},
        {"node_modes": (("x", GraphNodeMode.DIRECT), ("x", GraphNodeMode.BOUNDED_LOOP))},
        {"reason_codes": ()},
        {"required_human_gates": ("approval",)},
        {"pattern": ExecutionTopologyPattern.QUEUE_HUMAN_REVIEW},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            ExecutionTopologyDecision.create(**(values | changed))
    with pytest.raises(PolicyViolation, match="digest mismatch"):
        _replace(decision, estimated_calls=2)


def _node(step: str = "a") -> GraphNodeReceipt:
    return GraphNodeReceipt(
        step,
        GraphNodeMode.DIRECT,
        NOW,
        NOW,
        NOW + dt.timedelta(seconds=1),
        0,
        0,
        1,
        1,
        1,
        1,
        D,
        GraphNodeTerminalState.COMPLETED,
    )


def test_graph_receipt_order_usage_and_integrity_guards() -> None:
    node = _node()
    assert node.duration_millis == 1000
    for changed in (
        {"step_id": ""},
        {"queued_at": NAIVE},
        {"ended_at": NOW},
        {"dependency_wait_millis": -1},
        {"result_digest": "bad"},
    ):
        with pytest.raises((ValidationFailed, ValueError)):
            _replace(node, **changed)
    values: dict[str, Any] = {
        "graph_root_id": U[0],
        "plan_digest": D,
        "node_receipts": (node,),
        "critical_path": ("a",),
        "max_observed_concurrency": 1,
        "parallel_overlap_duration_millis": 0,
        "parallel_efficiency_ppm": 0,
        "coordination_input_tokens": 1,
        "coordination_output_tokens": 1,
        "coordination_cost_micros": 1,
        "coordination_message_count": 1,
        "fan_in_result_digest": D,
        "terminal_state": GraphTerminalState.COMPLETED,
        "topology_feedback": ("ok",),
    }
    receipt = GraphExecutionReceipt.create(**values)
    assert receipt.receipt_digest == receipt.computed_digest
    for changed in (
        {"node_receipts": ()},
        {"node_receipts": (node, node)},
        {"critical_path": ()},
        {"critical_path": ("missing",)},
        {"max_observed_concurrency": -1},
        {"parallel_efficiency_ppm": -1},
        {"parallel_efficiency_ppm": 1_000_001},
    ):
        with pytest.raises(ValidationFailed):
            GraphExecutionReceipt.create(**(values | changed))
    with pytest.raises(PolicyViolation, match="digest mismatch"):
        _replace(receipt, terminal_state=GraphTerminalState.FAILED)


def _tournament_values() -> dict[str, Any]:
    assignments = (
        TournamentCandidateAssignment(U[0], "m1", "worker-1", 10, 10),
        TournamentCandidateAssignment(U[1], "m2", "worker-2", 10, 10),
    )
    return {
        "candidate_assignments": assignments,
        "shared_objective_digest": D,
        "candidate_context_digest": D,
        "selector_assignment_id": U[2],
        "selector_model_id": "selector",
        "selector_execution_identity": "judge",
        "selector_spec_digest": D,
        "human_final_gate": True,
        "budget": TournamentBudget(2, 20, 20, LATER),
    }


def test_tournament_isolation_identity_and_budget_guards() -> None:
    values = _tournament_values()
    plan = TournamentPlan.create(**values)
    assert plan.candidate_count == 2
    with pytest.raises(ValidationFailed):
        TournamentBudget(0, 1, 1, LATER)
    with pytest.raises(ValidationFailed):
        TournamentBudget(1, 1, 1, NAIVE)
    with pytest.raises(ValidationFailed):
        TournamentCandidateAssignment(U[3], "", "worker", 1, 1)
    with pytest.raises(ValidationFailed):
        TournamentCandidateAssignment(U[3], "m", "worker", 0, 1)
    assignments = values["candidate_assignments"]
    assert isinstance(assignments, tuple)
    variants = (
        {"grants_authority": True},
        {"candidate_isolation": False},
        {"candidate_assignments": assignments[:1]},
        {
            "candidate_assignments": (
                assignments[0],
                _replace(assignments[1], assignment_id=assignments[0].assignment_id),
            )
        },
        {
            "candidate_assignments": (
                assignments[0],
                _replace(assignments[1], execution_identity="worker-1"),
            )
        },
        {"selector_assignment_id": assignments[0].assignment_id},
        {"selector_execution_identity": "worker-1"},
        {"selector_model_id": "m1"},
        {"budget": TournamentBudget(1, 20, 20, LATER)},
        {"budget": TournamentBudget(2, 19, 20, LATER)},
        {"budget": TournamentBudget(2, 20, 19, LATER)},
    )
    for changed in variants:
        with pytest.raises((ValidationFailed, PolicyViolation)):
            TournamentPlan.create(**(values | changed))
    with pytest.raises(PolicyViolation, match="digest mismatch"):
        _replace(plan, human_final_gate=False)


def test_context_reference_candidate_selection_security_and_rejection() -> None:
    evidence = EvidenceReference("test", "tests/report", D, 1)
    assert evidence.as_dict()["revision"] == 1
    for values in (
        ("unknown", "ref", D, None),
        ("test", "../secret", D, None),
        ("test", "/abs", D, None),
        ("test", "ref", "bad", None),
        ("test", "ref", D, 0),
    ):
        with pytest.raises((ValidationFailed, PolicyViolation, ValueError)):
            EvidenceReference(*values)
    candidate = ContextCandidate(
        "candidate", AuthorityLevel.VERIFIED, NOW, "rev-1", D, 10, evidence_refs=(evidence,)
    )
    assert candidate.score(NOW)[0] == int(AuthorityLevel.VERIFIED)
    assert candidate.rejection(NOW, AuthorityLevel.OBSERVED) is None
    for changed in (
        {"candidate_id": "private-key"},
        {"source_revision": ""},
        {"token_count": 0},
        {"observed_at": NAIVE},
        {"valid_until": NAIVE},
        {"valid_until": NOW},
        {"kind": "wrong"},
        {"source_ref": "../escape"},
        {"identity_refs": ("a", "a")},
        {"canonical_revision_id": "bad"},
        {"tokenizer_profile_digest": "bad"},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation, ValueError)):
            _replace(candidate, **changed)
    assert _replace(candidate, superseded=True).rejection(NOW, AuthorityLevel.OBSERVED) is not None
    assert (
        _replace(candidate, authority=AuthorityLevel.UNTRUSTED).rejection(
            NOW, AuthorityLevel.OBSERVED
        )
        is not None
    )
    assert (
        _replace(candidate, observed_at=LATER).rejection(NOW, AuthorityLevel.OBSERVED) is not None
    )
    selection = ContextSelection("candidate", D, 10, (1,), "selected")
    for changed in (
        {"kind": "wrong"},
        {"source_ref": "../x"},
        {"authority": 99},
        {"reason_codes": ("x", "x")},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _replace(selection, **changed)
    assert candidate.kind is ContextCandidateKind.GENERAL


def test_session_low_level_reference_and_metadata_security() -> None:
    ref = DigestReference("checkpoint/current", D, TruthClass.REPO_FACT)
    assert ref.as_dict()["truth_class"] == TruthClass.REPO_FACT.value
    for changed in (
        {"ref": ""},
        {"ref": " ../x"},
        {"ref": "/abs"},
        {"ref": "a\\b"},
        {"ref": "x\x01"},
        {"digest_value": "bad"},
        {"truth_class": "wrong"},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation, ValueError)):
            _replace(ref, **changed)
    metadata = TypedMetadata("repo.fact", "facts/current", D, TruthClass.REPO_FACT)
    assert metadata.as_dict()["name"] == "repo.fact"
    for changed in (
        {"name": "UPPER"},
        {"name": "password"},
        {"value_ref": "../x"},
        {"value_digest": "bad"},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation, ValueError)):
            _replace(metadata, **changed)
    selected = ContextSelectionReference("context/a", D, 3, TruthClass.REPO_FACT)
    with pytest.raises(ValidationFailed):
        _replace(selected, token_count=0)
    omitted = ContextOmissionReference("context/b", "budget")
    with pytest.raises(ValidationFailed):
        _replace(omitted, reason_code="BAD REASON")
    fresh = FreshnessDimension("source", D, D, True)
    with pytest.raises(ValidationFailed):
        _replace(fresh, current=False)


def _lifecycle(**changes: Any) -> SessionLifecycleEvent:
    values: dict[str, Any] = {
        "realm_id": U[0],
        "project_id": U[1],
        "work_item_id": U[2],
        "run_id": U[3],
        "session_id": "session/1",
        "client_id": "client/1",
        "event_id": U[4],
        "event_type": "turn.start",
        "sequence": 1,
        "previous_digest": None,
        "origin": "local",
        "causation_id": "cause/1",
        "correlation_id": "corr/1",
        "recursion_depth": 0,
        "source_revision": "rev/1",
        "plan_ref": "plan/1",
        "checkpoint_ref": None,
        "context_ref": None,
        "payload_digest": D,
        "metadata": (),
        "classification": DataClassification.INTERNAL,
        "occurred_at": NOW,
        "ingested_at": NOW,
    }
    values.update(changes)
    return SessionLifecycleEvent(**values)


def test_session_lifecycle_chain_bounds_and_raw_content_guards() -> None:
    event = _lifecycle()
    assert event.event_digest.startswith("sha256:")
    for changed in (
        {"event_type": "prompt_body"},
        {"sequence": 2},
        {"sequence": 1, "previous_digest": D},
        {"sequence": 2, "previous_digest": "bad"},
        {"recursion_depth": 17},
        {"metadata": (TypedMetadata("repo.fact", "a", D, TruthClass.REPO_FACT),) * 2},
        {"occurred_at": NAIVE},
        {"ingested_at": NAIVE},
        {"ingested_at": NOW - dt.timedelta(seconds=1)},
        {"contains_prompt": True},
        {"contains_response": True},
        {"contains_transcript": True},
        {"grants_authority": True},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation, ValueError)):
            _lifecycle(**changed)


def _close(**changes: Any) -> SessionCloseReceipt:
    ref = DigestReference("checkpoint/current", D, TruthClass.REPO_FACT)
    values: dict[str, Any] = {
        "receipt_id": U[0],
        "realm_id": U[1],
        "project_id": U[2],
        "work_item_id": U[3],
        "run_id": U[4],
        "session_id": "session/1",
        "client_id": "client/1",
        "job_id": U[5],
        "attempt_id": U[6],
        "envelope_digest": D,
        "fencing_token": 1,
        "completed_steps": (),
        "changed_artifacts": (),
        "verified_outcomes": (),
        "pending_steps": (),
        "next_safe_action": None,
        "human_decisions": (),
        "discovered_constraints": (),
        "failure_recovery_refs": (),
        "candidate_lessons": (),
        "candidate_skills": (),
        "checkpoint_ref": ref,
        "journal_head": _replace(ref, ref="journal/head"),
        "source_digest": D,
        "policy_digest": D,
        "migration_digest": D,
        "context_digest": D,
        "status": CloseStatus.CLOSED,
        "closed_at": NOW,
    }
    values.update(changes)
    return SessionCloseReceipt(**values)


def test_session_close_terminal_integrity_matrix() -> None:
    receipt = _close()
    assert receipt.document()["receipt_digest"] == receipt.receipt_digest
    action = DigestReference("action/retry", D, TruthClass.USER_DECISION)
    recovery = DigestReference("recovery/log", D, TruthClass.REPO_FACT)
    for changed in (
        {"fencing_token": 0},
        {"completed_steps": (recovery, recovery)},
        {"pending_steps": (recovery,)},
        {"next_safe_action": action},
        {"status": CloseStatus.DEGRADED},
        {"status": CloseStatus.RECOVERY_REQUIRED, "next_safe_action": action},
        {"closed_at": NAIVE},
        {"grants_authority": True},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _close(**changed)
    degraded = _close(status=CloseStatus.DEGRADED, next_safe_action=action)
    assert degraded.status is CloseStatus.DEGRADED
    recovered = _close(
        status=CloseStatus.RECOVERY_REQUIRED,
        next_safe_action=action,
        failure_recovery_refs=(recovery,),
    )
    assert recovered.failure_recovery_refs == (recovery,)


def _compaction(**changes: Any) -> CompactionReceipt:
    values: dict[str, Any] = {
        "receipt_id": U[0],
        "realm_id": U[1],
        "project_id": U[2],
        "work_item_id": U[3],
        "run_id": U[4],
        "session_id": "session/1",
        "client_id": "client/1",
        "pre_compaction_event_digest": D,
        "checkpoint_draft_digest": D,
        "outbox_ref": "outbox/1",
        "outbox_payload_digest": D,
        "worker_result_digest": None,
        "checkpoint_ref": None,
        "checkpoint_digest": None,
        "post_compaction_event_digest": None,
        "rehydration_receipt_digest": None,
        "status": CompactionStatus.PREPARED,
        "created_at": NOW,
        "completed_at": None,
    }
    values.update(changes)
    return CompactionReceipt(**values)


def test_compaction_and_projection_terminal_security_matrix() -> None:
    assert _compaction().document()["status"] == CompactionStatus.PREPARED.value
    with pytest.raises(ValidationFailed, match="terminal zincir"):
        _compaction(status=CompactionStatus.COMPLETED)
    with pytest.raises(ValidationFailed, match="completed_at"):
        _compaction(completed_at=LATER)
    complete = _compaction(
        status=CompactionStatus.COMPLETED,
        worker_result_digest=D,
        checkpoint_ref="checkpoint/1",
        checkpoint_digest=D,
        post_compaction_event_digest=D,
        rehydration_receipt_digest=D,
        completed_at=LATER,
    )
    assert complete.receipt_digest.startswith("sha256:")
    for changed in (
        {"completed_at": NAIVE},
        {"completed_at": NOW - dt.timedelta(seconds=1)},
        {"grants_authority": True},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _replace(complete, **changed)
    projection = ProjectionGenerationReceipt(
        U[0], U[1], U[2], U[3], "source/a", D, "projection/a", D, "v1", NOW
    )
    assert projection.document()["classification"] == "public"
    for changed in (
        {"generated_at": NAIVE},
        {"classification": DataClassification.CONFIDENTIAL},
        {"public_filtered": False},
        {"grants_authority": True},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _replace(projection, **changed)


def test_knowledge_path_artifact_and_locator_security_matrix() -> None:
    assert assert_safe_relative("docs/a.md") == "docs/a.md"
    assert is_denied("nested/.env") and is_denied("key.pem")
    assert not is_denied("docs/public.md")
    for path in ("", "/etc/passwd", "../secret", "a\\b"):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            assert_safe_relative(path)
    artifact = Artifact("artifact-1", D, 10, "text/markdown", "docs/a.md", NOW)
    for changed in (
        {"content_digest": "bad"},
        {"byte_size": -1},
        {"media_type": ""},
        {"original_name": "../x"},
        {"original_name": ".env"},
        {"stored_at": NAIVE},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation, ValueError)):
            _replace(artifact, **changed)
    locator = Locator(page=1, bbox=(0.0, 0.0, 1.0, 1.0), line_start=1, line_end=2)
    for changed in (
        {"page": 0},
        {"bbox": (0.0, 0.0, 1.0)},
        {"bbox": (0.0, 0.0, -1.0, 1.0)},
        {"page": None, "bbox": (0.0, 0.0, 1.0, 1.0)},
        {"line_start": None, "line_end": 2},
        {"line_start": 3, "line_end": 2},
        {"relative_path": "../x"},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _replace(locator, **changed)


def _document() -> NormalizedDocument:
    locator = Locator(line_start=1, line_end=1, relative_path="docs/a.md")
    unit = ContentUnit("unit-1", UnitKind.PARAGRAPH, "hello", locator, 1)
    return NormalizedDocument("doc-1", D, SourceFormat.MARKDOWN, (unit,), "parser/local", "v1", {})


def test_knowledge_unit_document_job_version_and_limits_matrix() -> None:
    document = _document()
    unit = document.units[0]
    for changed in (
        {"text": ""},
        {"order": -1},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"kind": UnitKind.OCR_BLOCK, "confidence": None},
    ):
        with pytest.raises((ValidationFailed, ValueError)):
            _replace(unit, **changed)
    for changed in (
        {"artifact_digest": "bad"},
        {"units": ()},
        {"units": (unit, unit)},
        {"parser_version": ""},
        {"parser_profile": 3},
    ):
        with pytest.raises((ValidationFailed, ValueError, TypeError)):
            _replace(document, **changed)
    job = IngestionJob("job-1", "source-1", D, "idem-1")
    assert job.next_stage is IngestionStage.VALIDATED
    advanced = job.advance(IngestionStage.VALIDATED)
    assert advanced.next_stage is IngestionStage.STORED
    with pytest.raises(ValidationFailed):
        job.advance(IngestionStage.STORED)
    with pytest.raises(ValidationFailed):
        job.fail("")
    failed = job.fail("parse-failed")
    assert failed.failure == "parse-failed"
    with pytest.raises(PolicyViolation):
        failed.advance(IngestionStage.VALIDATED)
    version = SourceVersion("version-1", "source-1", 1, D, D, VersionState.PENDING, NOW)
    with pytest.raises(PolicyViolation):
        version.activate(job)
    complete_job = IngestionJob("job-1", "source-1", D, "idem-1", tuple(IngestionStage))
    active = version.activate(complete_job)
    assert active.state is VersionState.ACTIVE
    with pytest.raises(PolicyViolation):
        version.supersede("version-2")
    assert active.supersede("version-2").state is VersionState.SUPERSEDED
    for args in ((0, 1, 1), (1, 0, 1), (1, 1, 0)):
        with pytest.raises(ValidationFailed):
            ScanLimits(*args)


def test_knowledge_database_and_code_symbol_reject_row_data_and_unsafe_names() -> None:
    obj = DatabaseObject("public", "orders", "table", "rev-1", ("id", "total"))
    assert obj.qualified_name == "public.orders"
    for changed in (
        {"schema_name": ""},
        {"object_name": ""},
        {"object_kind": ""},
        {"object_name": "passwords"},
        {"row_data_included": True},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation, TypeError)):
            _replace(obj, **changed)
    symbol = CodeSymbol("run", "function", "src/a.py", 1, 2, "rev-1")
    for changed in (
        {"relative_path": "../x"},
        {"name": ""},
        {"line_start": 0},
        {"line_end": 0},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation, ValueError)):
            _replace(symbol, **changed)


def _metric(**changes: Any) -> MetricSpec:
    values: dict[str, Any] = {
        "metric_id": "accuracy",
        "name": "Accuracy",
        "unit": "ratio",
        "direction": MetricDirection.MAXIMIZE,
        "role": MetricRole.PRIMARY,
        "source_kind": "external",
        "target_value": 0.9,
        "min_value": None,
        "max_value": None,
        "minimum_meaningful_delta": 0.01,
        "regression_tolerance": 0.0,
        "aggregation": MetricAggregation.LATEST,
    }
    values.update(changes)
    return MetricSpec(**values)


def test_optimization_asset_metric_and_objective_invariants() -> None:
    asset = ValidatorAsset("validator/test", "tests/test.py", D, ValidatorAssetRole.TEST)
    for changed in ({"asset_id": ""}, {"logical_ref": ""}, {"content_digest": "bad"}):
        with pytest.raises((ValidationFailed, ValueError)):
            _replace(asset, **changed)
    manifest = ValidatorAssetManifest(U[0], U[1], D, "source", U[2], U[3], (asset,), NOW)
    assert manifest.manifest_digest.startswith("sha256:")
    for changed in (
        {"grants_authority": True},
        {"source_revision": ""},
        {"builder_assignment_id": U[2], "verifier_assignment_id": U[2]},
        {"assets": ()},
        {"assets": (asset, asset)},
        {"created_at": NAIVE},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _replace(manifest, **changed)
    metric = _metric()
    for changed in (
        {"metric_id": ""},
        {"target_value": math.nan},
        {"minimum_meaningful_delta": -1.0},
        {"target_value": None},
        {"min_value": 0.0},
        {
            "direction": MetricDirection.RANGE,
            "target_value": None,
            "min_value": 2.0,
            "max_value": 1.0,
        },
    ):
        with pytest.raises(ValidationFailed):
            _metric(**changed)
    objective = OptimizationObjective(
        U[0],
        U[1],
        U[2],
        U[3],
        U[4],
        "step",
        "artifact",
        D,
        D,
        manifest.manifest_digest,
        (metric,),
        2,
        100,
        100,
        LATER,
        "reversible",
        NOW,
    )
    assert objective.objective_digest.startswith("sha256:")
    for changed in (
        {"grants_authority": True},
        {"step_id": ""},
        {"max_attempts": 0},
        {"max_attempts": 101},
        {"max_tokens": 0},
        {"created_at": NAIVE},
        {"deadline": NOW},
        {"metric_specs": ()},
        {"metric_specs": (metric, metric)},
        {"metric_specs": (_replace(metric, role=MetricRole.HARD_GUARD),)},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _replace(objective, **changed)


def _evidence(
    metric_id: str, value: float, revision: str = "rev-1", *, self_report: bool = False
) -> MeasurementEvidence:
    return MeasurementEvidence(
        metric_id,
        value,
        f"evidence/{metric_id}/{value}",
        digest((metric_id, value, revision)),
        revision,
        NOW,
        "producer",
        "verifier",
        self_report,
    )


def test_optimization_measurement_progress_invalid_and_directional_paths() -> None:
    metric = _metric()
    evidence = _evidence("accuracy", 0.5)
    for changed in (
        {"metric_id": ""},
        {"value": math.inf},
        {"evidence_digest": "bad"},
        {"measured_at": NAIVE},
        {"verifier_identity": "producer"},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation, ValueError)):
            _replace(evidence, **changed)
    improved = evaluate_progress(
        (metric,),
        (_evidence("accuracy", 0.4),),
        (_evidence("accuracy", 0.5),),
        (_evidence("accuracy", 0.7),),
        cost_micros=100,
    )
    assert improved.progress_state is ProgressState.IMPROVED
    assert improved.value_per_cost is not None
    invalid = evaluate_progress((metric,), (), (), (), cost_micros=0)
    assert invalid.progress_state is ProgressState.INVALID
    duplicate = evaluate_progress((metric,), (evidence, evidence), (evidence,), (evidence,))
    assert duplicate.progress_state is ProgressState.INVALID
    with pytest.raises(ValidationFailed):
        evaluate_progress((), (), (), ())
    with pytest.raises(ValidationFailed):
        evaluate_progress((metric,), (evidence,), (evidence,), (evidence,), cost_micros=-1)
    result = MetricProgressResult("accuracy", MetricRole.PRIMARY, 1.0, True, False, False)
    good = ProgressVector(
        (("accuracy", 0.0),),
        (("accuracy", 0.0),),
        (("accuracy", 1.0),),
        (("accuracy", 1.0),),
        (result,),
        (D,),
        1.0,
        ProgressState.IMPROVED,
    )
    assert good.progress_digest.startswith("sha256:")
    variants = (
        {"current_values": (("b", 1.0), ("a", 2.0))},
        {"current_values": (("accuracy", math.nan),)},
        {"metric_results": (_replace(result, favorable_delta=2.0),)},
        {"value_per_cost": math.inf},
        {"evidence_digests": (D, D)},
        {"progress_state": ProgressState.INVALID},
        {"invalid_reasons": ("bad",)},
        {"evidence_digests": ()},
    )
    for changed in variants:
        with pytest.raises(ValidationFailed):
            _replace(good, **changed)


def _policy(**changes: Any) -> LoopPolicy:
    values: dict[str, Any] = {
        "id": U[0],
        "realm_id": U[1],
        "project_id": U[2],
        "work_item_id": U[3],
        "plan_id": U[4],
        "step_id": "step",
        "assignment_id": U[5],
        "context_manifest_id": U[6],
        "validator_assignment_id": U[7],
        "max_attempts": 3,
        "max_tokens": 1000,
        "max_cost_micros": 1000,
        "deadline": LATER,
        "validator_spec_digest": D,
        "required_delta": tuple(sorted((LoopDeltaKind.NEW_EVIDENCE,), key=str)),
        "forbidden_effects": tuple(sorted((LoopEffectClass.DEPLOY,), key=str)),
        "terminal_states": tuple(sorted(LoopTerminalState, key=str)),
        "source_revision": "rev-1",
        "context_manifest_digest": D,
        "plan_digest": D,
        "policy_revision_digest": D,
        "canonical_effect_kind": "none",
        "created_at": NOW,
    }
    values.update(changes)
    return LoopPolicy(**values)


def test_loop_policy_budget_canonical_terminal_and_measured_bounds() -> None:
    policy = _policy()
    assert policy.policy_digest.startswith("sha256:")
    for changed in (
        {"grants_authority": True},
        {"max_attempts": 0},
        {"max_attempts": 101},
        {"max_tokens": 0},
        {"created_at": NAIVE},
        {"deadline": NOW},
        {"step_id": ""},
        {"canonical_effect_kind": "shell"},
        {"required_delta": ()},
        {"required_delta": (LoopDeltaKind.NEW_EVIDENCE,) * 2},
        {"forbidden_effects": (LoopEffectClass.DEPLOY,) * 2},
        {"terminal_states": (LoopTerminalState.PASSED,)},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _policy(**changed)
    measured = {
        "objective_id": U[8],
        "stable_objective_digest": D,
        "measurement_plan_digest": D,
        "validator_manifest_id": U[9],
        "validator_asset_manifest_digest": D,
        "metric_specs_digest": D,
        "stall_limit": 2,
        "diagnostic_patience": 1,
        "progress_token_budget": 64,
        "minimum_value_per_cost": 0.0,
    }
    assert _policy(**measured).body()["measured_v2"] is not None
    for changed in (
        {"objective_id": U[8]},
        {**measured, "stall_limit": 4},
        {**measured, "diagnostic_patience": 3},
        {**measured, "progress_token_budget": 63},
        {**measured, "minimum_value_per_cost": math.nan},
    ):
        with pytest.raises(ValidationFailed):
            _policy(**changed)


def _attempt(**changes: Any) -> LoopAttemptRequest:
    values: dict[str, Any] = {
        "loop_id": U[0],
        "prompt_digest": D,
        "context_digest": D,
        "action_digest": D,
        "source_revision": "rev-1",
        "plan_digest": D,
        "policy_revision_digest": D,
        "validator_spec_digest": D,
        "reserved_input_tokens": 1,
        "reserved_output_tokens": 0,
        "reserved_cost_micros": 0,
    }
    values.update(changes)
    return LoopAttemptRequest(**values)


def test_loop_attempt_admission_validation_terminal_and_usage_guards() -> None:
    request = _attempt()
    assert request.semantic_request_digest.startswith("sha256:")
    assert request.delta_digest == digest([])
    for changed in (
        {"source_revision": ""},
        {"reserved_input_tokens": -1},
        {"reserved_input_tokens": 0},
        {"delta_evidence_ids": (U[1], U[1])},
        {"attempt_ordinal": 0},
        {"objective_digest": "bad"},
        {"objective_digest": D},
        {"attempt_ordinal": 2},
    ):
        with pytest.raises((ValidationFailed, ValueError)):
            _attempt(**changed)
    admitted = LoopAdmission(True, U[0], U[1], 1, None, "admitted", D)
    assert admitted.admitted
    for changed in (
        {"grants_authority": True},
        {"decision_digest": "bad"},
        {"attempt_id": None},
        {"terminal_state": LoopTerminalState.PASSED},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation, ValueError)):
            _replace(admitted, **changed)
    rejected = LoopAdmission(False, U[0], None, None, LoopTerminalState.BLOCKED, "blocked", D)
    with pytest.raises(ValidationFailed):
        _replace(rejected, terminal_state=None)
    validation = LoopValidation(LoopAttemptOutcome.PASSED, D, 1, 1, 1, U[2], U[3])
    assert validation.terminal_state is LoopTerminalState.PASSED
    assert (
        LoopValidation(LoopAttemptOutcome.RETRYABLE_FAILURE, D, 0, 0, 0, U[2], U[3]).terminal_state
        is None
    )
    with pytest.raises(ValidationFailed):
        _replace(validation, actual_cost_micros=-1)
    with pytest.raises(ValidationFailed, match="guard"):
        _replace(validation, producer_self_report=True)
    measured = _replace(
        validation,
        metric_evidence_refs=("evidence/a",),
        metric_vector_digest=D,
        progress_state=ProgressState.IMPROVED,
        progress_decision_digest=D,
        progress_packet_digest=D,
    )
    assert measured.measured_progress
    assert not _replace(measured, producer_self_report=True).measured_progress
    assert not _replace(measured, hard_guard_regressed=True).measured_progress
    for changed in (
        {"metric_vector_digest": None},
        {"metric_evidence_refs": ("b", "a")},
        {"metric_evidence_refs": ("",)},
    ):
        with pytest.raises(ValidationFailed):
            _replace(measured, **changed)
