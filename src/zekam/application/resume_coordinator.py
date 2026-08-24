"""Read-only resume preparation over a canonical immutable observation."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from zekam.domain.checkpoint_v2 import Resumability
from zekam.domain.resume import (
    DriftDecision,
    ReconciliationAction,
    ResumeAction,
    ResumeDisposition,
    ResumeObservation,
    ResumePlan,
    StaleDimension,
)

_BINDING_RULES: tuple[tuple[str, DriftDecision], ...] = (
    ("source_revision", DriftDecision.REPLAN),
    ("policy_digest", DriftDecision.REAUTHORIZE),
    ("capability_profile_digest", DriftDecision.RECOMPILE),
    ("dependency_snapshot_digest", DriftDecision.REPLAN),
    ("migration_head_digest", DriftDecision.MANUAL_REVIEW),
    ("model_route_decision_digest", DriftDecision.RECOMPILE),
    ("context_manifest_digest", DriftDecision.RECOMPILE),
    ("context_packet_digest", DriftDecision.RECOMPILE),
    ("architecture_digest", DriftDecision.REPLAN),
    ("rules_digest", DriftDecision.REPLAN),
    ("test_suite_digest", DriftDecision.REPLAN),
    ("model_inventory_digest", DriftDecision.RECOMPILE),
    ("journal_head_digest", DriftDecision.REPLAN),
)


def _binding_value(observation: ResumeObservation, side: str, field: str) -> str:
    bindings = (
        observation.checkpoint_bindings if side == "checkpoint" else observation.current_bindings
    )
    return str(getattr(bindings, field))


def _stale_dimensions(observation: ResumeObservation) -> tuple[StaleDimension, ...]:
    stale: list[StaleDimension] = []
    if (
        observation.plan_id != observation.current_plan_id
        or observation.plan_digest != observation.current_plan_digest
    ):
        stale.append(
            StaleDimension(
                dimension="task_plan",
                checkpoint_value=observation.plan_digest,
                current_value=observation.current_plan_digest,
                decision=DriftDecision.REPLAN,
                reason_code="resume.plan-drift",
            )
        )
    for field, decision in _BINDING_RULES:
        checkpoint = _binding_value(observation, "checkpoint", field)
        current = _binding_value(observation, "current", field)
        if checkpoint != current:
            stale.append(
                StaleDimension(
                    dimension=field,
                    checkpoint_value=checkpoint,
                    current_value=current,
                    decision=decision,
                    reason_code=f"resume.{field.replace('_', '-')}-drift",
                )
            )
    return tuple(sorted(stale, key=lambda item: item.dimension))


def _disposition(
    observation: ResumeObservation,
    stale: tuple[StaleDimension, ...],
) -> tuple[ResumeDisposition, tuple[str, ...]]:
    if observation.legacy_limited:
        return ResumeDisposition.MANUAL_REVIEW, ("resume.legacy-checkpoint-limited",)
    if not observation.checkpoint_integrity:
        return ResumeDisposition.MANUAL_REVIEW, ("resume.checkpoint-integrity-failed",)
    if observation.work_state in {"cancelled", "archived"}:
        return ResumeDisposition.DENIED, ("resume.work-not-active",)
    if observation.open_effects:
        return ResumeDisposition.RECOVERY_REQUIRED, ("resume.unresolved-effect",)
    if observation.resumability is Resumability.MANUAL_REVIEW:
        return ResumeDisposition.MANUAL_REVIEW, ("resume.checkpoint-manual-review",)
    if observation.resumability is Resumability.BLOCKED:
        return ResumeDisposition.WAITING, ("resume.checkpoint-blocked",)
    if observation.work_state == "completed":
        if observation.pending_steps:
            return ResumeDisposition.MANUAL_REVIEW, ("resume.completed-partition-drift",)
        return ResumeDisposition.ALREADY_COMPLETED, ()
    decisions = {item.decision for item in stale}
    if DriftDecision.MANUAL_REVIEW in decisions:
        return ResumeDisposition.MANUAL_REVIEW, ("resume.high-risk-drift",)
    if DriftDecision.DENY in decisions:
        return ResumeDisposition.DENIED, ("resume.policy-denied",)
    if DriftDecision.REPLAN in decisions:
        return ResumeDisposition.SAFE_REPLAN, ("resume.replan-required",)
    if decisions:
        return ResumeDisposition.SAFE_RECOMPILE, ("resume.recompile-required",)
    if not observation.pending_steps:
        return ResumeDisposition.MANUAL_REVIEW, ("resume.nonterminal-empty-partition",)
    return ResumeDisposition.SAFE_CONTINUE, ()


def _actions(
    observation: ResumeObservation,
    disposition: ResumeDisposition,
    reconciliation: tuple[ReconciliationAction, ...],
) -> tuple[ResumeAction, ...]:
    actions: list[ResumeAction] = []
    prior: tuple[str, ...] = ()
    if reconciliation and disposition is not ResumeDisposition.RECOVERY_REQUIRED:
        return ()
    if reconciliation:
        for index, item in enumerate(reconciliation, start=1):
            action_id = f"reconcile-{index:03d}"
            actions.append(
                ResumeAction(
                    action_id=action_id,
                    kind="reconcile-effect",
                    depends_on=prior,
                    resource=f"claim:{item.claim_id}",
                )
            )
            prior = (action_id,)
        return tuple(actions)
    if disposition in {ResumeDisposition.SAFE_RECOMPILE, ResumeDisposition.SAFE_REPLAN}:
        kind = "replan" if disposition is ResumeDisposition.SAFE_REPLAN else "recompile-context"
        actions.append(ResumeAction("refresh-001", kind, (), None))
        prior = ("refresh-001",)
        if disposition is ResumeDisposition.SAFE_REPLAN:
            return tuple(actions)
    if (
        disposition
        in {
            ResumeDisposition.SAFE_CONTINUE,
            ResumeDisposition.SAFE_RECOMPILE,
            ResumeDisposition.SAFE_REPLAN,
        }
        and observation.next_step_id is not None
    ):
        actions.append(ResumeAction("reacquire-001", "reacquire", prior, None))
        actions.append(
            ResumeAction(
                "dispatch-001",
                "dispatch-next-step",
                ("reacquire-001",),
                f"step:{observation.next_step_id}",
            )
        )
    return tuple(actions)


@dataclass(frozen=True, slots=True)
class ResumeCoordinator:
    """Prepare only: repository is a read model and no mutator is reachable here."""

    repository: object

    def prepare(
        self,
        work_item_id: object,
        *,
        client_id: str,
        observed_at: dt.datetime | None = None,
    ) -> ResumePlan:
        observation = self.repository.read_snapshot(  # type: ignore[attr-defined]
            work_item_id,
            client_id=client_id,
            observed_at=observed_at,
        )
        stale = _stale_dimensions(observation)
        disposition, blockers = _disposition(observation, stale)
        reconciliation = (
            tuple(
                ReconciliationAction(
                    claim_id=item.claim_id,
                    effect_digest=item.effect_digest,
                    reason_code=(
                        "resume.failed-effect-reconciliation"
                        if item.state.value == "failed-reconciliation"
                        else "resume.receiptless-or-ambiguous-effect"
                    ),
                )
                for item in sorted(observation.open_effects, key=lambda value: str(value.claim_id))
            )
            if observation.checkpoint_integrity and not observation.legacy_limited
            else ()
        )
        reacquire: tuple[str, ...] = ()
        if disposition in {
            ResumeDisposition.SAFE_CONTINUE,
            ResumeDisposition.SAFE_RECOMPILE,
            ResumeDisposition.SAFE_REPLAN,
        }:
            reacquire = tuple(
                sorted(
                    {
                        "authorization",
                        "lease",
                        *(
                            f"resource-lock:{value}"
                            for value in observation.logical_write_resources
                        ),
                    }
                )
            )
        return ResumePlan(
            realm_id=observation.realm_id,
            project_id=observation.project_id,
            work_item_id=observation.work_item_id,
            checkpoint_id=observation.checkpoint_id,
            checkpoint_digest=observation.checkpoint_digest,
            checkpoint_revision=observation.checkpoint_revision,
            selected_checkpoint_reason=(
                "latest-valid-v2-for-current-work"
                if observation.checkpoint_integrity
                else "ambiguous-or-invalid-v2-head"
            ),
            disposition=disposition,
            stale_dimensions=stale,
            reconciliation_actions=reconciliation,
            reacquire_resources=reacquire,
            next_step_id=(
                observation.next_step_id
                if disposition
                in {
                    ResumeDisposition.SAFE_CONTINUE,
                    ResumeDisposition.SAFE_RECOMPILE,
                    ResumeDisposition.RECOVERY_REQUIRED,
                }
                else None
            ),
            context_recipe=observation.context_recipe,
            required_route_role=observation.required_route_role,
            actions=_actions(observation, disposition, reconciliation),
            blockers=blockers,
            observed_at=observation.observed_at,
        )
