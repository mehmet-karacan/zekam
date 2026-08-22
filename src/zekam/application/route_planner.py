"""Route planner: hangi adimlar simdi ve nasil calisir?

Sabit global maksimum yoktur. Her run icin paralellik su degerlerin en kucugudur
(`harness/ORKESTRASYON_DAG_QUEUE_LEASE_FENCING.md`):

```text
min(ready_independent_steps,
    available_worker_slots,
    quota_safe_slots,
    token_budget_slots,
    cost_budget_slots,
    provider_rate_slots,
    policy_concurrency_limit)
```

Sonuc 1 olabilir; bu da gecerli bir karardir. Iki adim ancak yazilabilir kaynak
kesisimi yoksa paralel calisabilir.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from zekam.domain.errors import ValidationFailed
from zekam.domain.resources import ResourceRequest, conflicts, parse_requests
from zekam.domain.runtime import RouteKind
from zekam.domain.work import PlanStep, TaskPlan, assert_acyclic_steps


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    """Bir run icin kullanilabilir kapasite ve butce."""

    worker_slots: int = 1
    quota_safe_slots: int = 1
    token_budget_slots: int = 1
    cost_budget_slots: int = 1
    provider_rate_slots: int = 1
    policy_concurrency_limit: int = 1

    def __post_init__(self) -> None:
        for label, value in (
            ("worker_slots", self.worker_slots),
            ("quota_safe_slots", self.quota_safe_slots),
            ("token_budget_slots", self.token_budget_slots),
            ("cost_budget_slots", self.cost_budget_slots),
            ("provider_rate_slots", self.provider_rate_slots),
            ("policy_concurrency_limit", self.policy_concurrency_limit),
        ):
            if value < 0:
                raise ValidationFailed(f"{label} negatif olamaz")

    @property
    def ceiling(self) -> int:
        """Butun sinirlarin en kucugu."""
        return min(
            self.worker_slots,
            self.quota_safe_slots,
            self.token_budget_slots,
            self.cost_budget_slots,
            self.provider_rate_slots,
            self.policy_concurrency_limit,
        )

    def limiting_factor(self) -> str:
        """Paralelligi sinirlayan ilk etken."""
        candidates = (
            ("worker_slots", self.worker_slots),
            ("quota_safe_slots", self.quota_safe_slots),
            ("token_budget_slots", self.token_budget_slots),
            ("cost_budget_slots", self.cost_budget_slots),
            ("provider_rate_slots", self.provider_rate_slots),
            ("policy_concurrency_limit", self.policy_concurrency_limit),
        )
        return min(candidates, key=lambda item: item[1])[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_slots": self.worker_slots,
            "quota_safe_slots": self.quota_safe_slots,
            "token_budget_slots": self.token_budget_slots,
            "cost_budget_slots": self.cost_budget_slots,
            "provider_rate_slots": self.provider_rate_slots,
            "policy_concurrency_limit": self.policy_concurrency_limit,
            "ceiling": self.ceiling,
        }


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Bir sonraki calistirma dalgasinin karari."""

    kind: RouteKind
    steps: tuple[str, ...]
    parallelism: int
    reason: str
    limiting_factor: str | None = None
    blocked_steps: tuple[str, ...] = ()
    conflicts: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "steps": list(self.steps),
            "parallelism": self.parallelism,
            "reason": self.reason,
            "limiting_factor": self.limiting_factor,
            "blocked_steps": list(self.blocked_steps),
            "conflicts": [list(pair) for pair in self.conflicts],
        }


@dataclass(frozen=True, slots=True)
class StepState:
    """Bir adimin calistirma durumu."""

    step_id: str
    completed: bool = False
    failed: bool = False
    recovery_required: bool = False


def _step_resources(step: PlanStep) -> tuple[ResourceRequest, ...]:
    """Adimin ilan ettigi kilit istekleri.

    Etki uretmeyen adimlar okuma, digerleri yazma ister.
    """
    from zekam.domain.work import EffectKind

    if step.effect is EffectKind.NONE:
        return parse_requests(read=step.logical_resources)
    return parse_requests(write=step.logical_resources)


@dataclass(frozen=True, slots=True)
class RoutePlanner:
    """Plan adimlarindan calistirma dalgasi hesaplar."""

    budget: ExecutionBudget = field(default_factory=ExecutionBudget)

    def ready_steps(self, plan: TaskPlan, states: Sequence[StepState]) -> tuple[PlanStep, ...]:
        """Butun bagimliliklari terminal-success olan adimlar."""
        by_id = {state.step_id: state for state in states}
        completed = {state.step_id for state in states if state.completed}
        pending: list[PlanStep] = []
        for step in plan.steps:
            state = by_id.get(step.step_id)
            if state is not None and (state.completed or state.failed):
                continue
            if set(step.depends_on) <= completed:
                pending.append(step)
        return tuple(sorted(pending, key=lambda step: step.step_id))

    def decide(
        self,
        plan: TaskPlan,
        states: Sequence[StepState] = (),
        *,
        agentic: bool = True,
    ) -> RouteDecision:
        """Bir sonraki dalgayi ve calistirma bicimini secer."""
        assert_acyclic_steps(plan.steps)

        recovery = [state.step_id for state in states if state.recovery_required]
        if recovery:
            return RouteDecision(
                kind=RouteKind.RECOVERY,
                steps=tuple(sorted(recovery)),
                parallelism=0,
                reason="recovery-required-step-var",
                blocked_steps=tuple(sorted(recovery)),
            )

        ready = self.ready_steps(plan, states)
        if not ready:
            remaining = [
                step.step_id
                for step in plan.steps
                if step.step_id not in {state.step_id for state in states if state.completed}
            ]
            if not remaining:
                return RouteDecision(
                    kind=RouteKind.DIRECT,
                    steps=(),
                    parallelism=0,
                    reason="butun-adimlar-tamam",
                )
            return RouteDecision(
                kind=RouteKind.BLOCKED,
                steps=(),
                parallelism=0,
                reason="hazir-adim-yok",
                blocked_steps=tuple(sorted(remaining)),
            )

        if len(ready) == 1:
            single = ready[0]
            kind = (
                RouteKind.DIRECT
                if not agentic and single.effect.value == "none"
                else RouteKind.SINGLE
            )
            return RouteDecision(
                kind=kind,
                steps=(single.step_id,),
                parallelism=1,
                reason="tek-hazir-adim",
                limiting_factor=None,
            )

        independent, found_conflicts = self._independent_subset(ready)
        ceiling = self.budget.ceiling
        selected = independent[: max(ceiling, 1)]

        if len(selected) <= 1 or ceiling <= 1:
            return RouteDecision(
                kind=RouteKind.SEQUENTIAL,
                steps=tuple(step.step_id for step in ready),
                parallelism=1,
                reason=("kaynak-catismasi" if found_conflicts else f"butce-siniri-{ceiling}"),
                limiting_factor=self.budget.limiting_factor(),
                conflicts=found_conflicts,
            )

        return RouteDecision(
            kind=RouteKind.PARALLEL,
            steps=tuple(step.step_id for step in selected),
            parallelism=len(selected),
            reason="bagimsiz-adimlar-paralel",
            limiting_factor=self.budget.limiting_factor(),
            conflicts=found_conflicts,
        )

    def _independent_subset(
        self, ready: Sequence[PlanStep]
    ) -> tuple[tuple[PlanStep, ...], tuple[tuple[str, str], ...]]:
        """Yazilabilir kaynak kesisimi olmayan en buyuk kararli alt kumeyi secer."""
        chosen: list[PlanStep] = []
        chosen_requests: list[tuple[str, ResourceRequest]] = []
        found: list[tuple[str, str]] = []

        for step in ready:
            requests = _step_resources(step)
            clash = next(
                (
                    (step.step_id, other_id)
                    for other_id, existing in chosen_requests
                    for request in requests
                    if conflicts(request, existing)
                ),
                None,
            )
            if clash is not None:
                found.append(clash)
                continue
            chosen.append(step)
            chosen_requests.extend((step.step_id, request) for request in requests)
        return tuple(chosen), tuple(found)


def declared_resources(plan: TaskPlan) -> tuple[ResourceRequest, ...]:
    """Planin ilan ettigi butun kilit istekleri."""
    requests: list[ResourceRequest] = []
    for step in plan.steps:
        requests.extend(_step_resources(step))
    return tuple(requests)
