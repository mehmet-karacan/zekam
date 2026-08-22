"""Work Graph alan modeli.

Work Graph, "is nedir ve hangi durumdadir" sorusunun tek yetkili kaynagidir.
Orchestration run/queue yalnizca bir yurutme denemesini temsil eder; Work Item'in
durumunu belirlemez.

Kurallar:

- Durum gecisleri kapali bir kume ile tanimlidir; tanimsiz gecis reddedilir.
- `completed` durumu acceptance evidence olmadan yazilamaz.
- Her degisiklik yeni bir revision ve gorunur bir olay uretir.
- Intent, Decision ve Task Plan append-only'dir.
- Plan yetki vermez; `grants_authority` her zaman `false`'tur.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7

WORK_ENTITY_TYPE = "work.item"


class WorkType(StrEnum):
    """Is kaydinin turu."""

    REQUEST = "request"
    DEFECT = "defect"
    TASK = "task"
    SUBTASK = "subtask"
    DECISION = "decision"
    RESEARCH = "research"
    IDEA = "idea"
    MAINTENANCE = "maintenance"


class WorkState(StrEnum):
    """Is kaydinin durumu."""

    PROPOSED = "proposed"
    READY = "ready"
    ACTIVE = "active"
    BLOCKED = "blocked"
    VERIFICATION = "verification"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


#: Terminal durumlar. Bunlardan cikis yalnizca acik reopen ile olur.
TERMINAL_STATES: frozenset[WorkState] = frozenset(
    {WorkState.COMPLETED, WorkState.CANCELLED, WorkState.ARCHIVED}
)

#: Aktif calisma sayilan durumlar.
OPEN_STATES: frozenset[WorkState] = frozenset(
    {
        WorkState.PROPOSED,
        WorkState.READY,
        WorkState.ACTIVE,
        WorkState.BLOCKED,
        WorkState.VERIFICATION,
    }
)

#: Izinli durum gecisleri. Tanimsiz gecis reddedilir.
ALLOWED_TRANSITIONS: dict[WorkState, frozenset[WorkState]] = {
    WorkState.PROPOSED: frozenset({WorkState.READY, WorkState.CANCELLED, WorkState.ARCHIVED}),
    WorkState.READY: frozenset(
        {WorkState.ACTIVE, WorkState.BLOCKED, WorkState.PROPOSED, WorkState.CANCELLED}
    ),
    WorkState.ACTIVE: frozenset(
        {WorkState.VERIFICATION, WorkState.BLOCKED, WorkState.READY, WorkState.CANCELLED}
    ),
    WorkState.BLOCKED: frozenset({WorkState.READY, WorkState.ACTIVE, WorkState.CANCELLED}),
    WorkState.VERIFICATION: frozenset(
        {WorkState.COMPLETED, WorkState.ACTIVE, WorkState.BLOCKED, WorkState.CANCELLED}
    ),
    # Terminal durumlardan cikis acik reopen/arsivleme ile sinirlidir.
    WorkState.COMPLETED: frozenset({WorkState.ACTIVE, WorkState.ARCHIVED}),
    WorkState.CANCELLED: frozenset({WorkState.PROPOSED, WorkState.ARCHIVED}),
    WorkState.ARCHIVED: frozenset(),
}


class RelationKind(StrEnum):
    """Is kayitlari arasindaki iliski turu."""

    DEPENDS_ON = "depends-on"
    BLOCKS = "blocks"
    PARENT_OF = "parent-of"
    DUPLICATES = "duplicates"
    RELATES_TO = "relates-to"
    SUPERSEDES = "supersedes"


#: Dongu olusturmasi yasak, yon tasiyan iliskiler.
ACYCLIC_KINDS: frozenset[RelationKind] = frozenset(
    {RelationKind.DEPENDS_ON, RelationKind.PARENT_OF}
)


def can_transition(current: WorkState, target: WorkState) -> bool:
    """Gecisin izinli olup olmadigini soyler."""
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: WorkState, target: WorkState) -> None:
    """Izinsiz gecisi reddeder."""
    if current == target:
        raise ValidationFailed(f"Durum zaten {current.value}")
    if not can_transition(current, target):
        raise PolicyViolation(f"Izinsiz durum gecisi: {current.value} -> {target.value}")


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    """Tek bir kabul kriteri."""

    text: str
    verified: bool = False

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValidationFailed("Kabul kriteri bos olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "verified": self.verified}


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Kanit referansi. Ham icerik degil, dogrulanabilir isaret tasir."""

    kind: str
    reference: str
    digest_value: str | None = None

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.reference.strip():
            raise ValidationFailed("Kanit turu ve referansi bos olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reference": self.reference, "digest": self.digest_value}


@dataclass(frozen=True, slots=True)
class WorkItem:
    """Kanonik is kaydi."""

    id: UUID
    realm_id: UUID
    project_id: UUID
    type: WorkType
    state: WorkState
    title: str
    summary: str = ""
    external_number: str | None = None
    revision: int = 1
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    acceptance_evidence: tuple[EvidenceRef, ...] = ()
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    updated_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValidationFailed("Is basligi bos olamaz")
        if self.revision < 1:
            raise ValidationFailed("Revision 1'den kucuk olamaz")
        if self.state is WorkState.COMPLETED and not self.acceptance_evidence:
            raise PolicyViolation("Acceptance evidence olmadan completed olamaz")

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        project_id: UUID,
        type: WorkType,
        title: str,
        summary: str = "",
        external_number: str | None = None,
        acceptance_criteria: tuple[AcceptanceCriterion, ...] = (),
        now: dt.datetime | None = None,
    ) -> WorkItem:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm_id,
            project_id=project_id,
            type=type,
            state=WorkState.PROPOSED,
            title=title.strip(),
            summary=summary,
            external_number=external_number,
            acceptance_criteria=acceptance_criteria,
            created_at=moment,
            updated_at=moment,
        )

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def body(self) -> dict[str, Any]:
        """Digest hesaplanan govde."""
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "external_number": self.external_number,
            "type": self.type.value,
            "state": self.state.value,
            "title": self.title,
            "summary": self.summary,
            "revision": self.revision,
            "acceptance_criteria": [item.as_dict() for item in self.acceptance_criteria],
            "acceptance_evidence": [item.as_dict() for item in self.acceptance_evidence],
        }

    @property
    def record_digest(self) -> str:
        return digest(self.body())

    def with_state(
        self,
        target: WorkState,
        *,
        evidence: tuple[EvidenceRef, ...] = (),
        now: dt.datetime | None = None,
    ) -> WorkItem:
        """Yeni durumdaki kopyayi uretir ve revision'i artirir."""
        assert_transition(self.state, target)
        merged_evidence = self.acceptance_evidence + evidence
        if target is WorkState.COMPLETED and not merged_evidence:
            raise PolicyViolation("Acceptance evidence olmadan completed olamaz")
        return WorkItem(
            id=self.id,
            realm_id=self.realm_id,
            project_id=self.project_id,
            type=self.type,
            state=target,
            title=self.title,
            summary=self.summary,
            external_number=self.external_number,
            revision=self.revision + 1,
            acceptance_criteria=self.acceptance_criteria,
            acceptance_evidence=merged_evidence,
            created_at=self.created_at,
            updated_at=now or dt.datetime.now(dt.UTC),
        )

    def with_details(
        self,
        *,
        title: str | None = None,
        summary: str | None = None,
        acceptance_criteria: tuple[AcceptanceCriterion, ...] | None = None,
        now: dt.datetime | None = None,
    ) -> WorkItem:
        """Icerik guncellemesi; durum degismez, revision artar."""
        return WorkItem(
            id=self.id,
            realm_id=self.realm_id,
            project_id=self.project_id,
            type=self.type,
            state=self.state,
            title=(title or self.title).strip(),
            summary=self.summary if summary is None else summary,
            external_number=self.external_number,
            revision=self.revision + 1,
            acceptance_criteria=(
                self.acceptance_criteria if acceptance_criteria is None else acceptance_criteria
            ),
            acceptance_evidence=self.acceptance_evidence,
            created_at=self.created_at,
            updated_at=now or dt.datetime.now(dt.UTC),
        )

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {
            "record_digest": self.record_digest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class WorkRelation:
    """Iki is kaydi arasindaki iliski."""

    id: UUID
    realm_id: UUID
    project_id: UUID
    source_id: UUID
    target_id: UUID
    kind: RelationKind
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if self.source_id == self.target_id:
            raise ValidationFailed("Bir is kaydi kendisiyle iliskilendirilemez")

    @classmethod
    def create(
        cls,
        *,
        source: WorkItem,
        target: WorkItem,
        kind: RelationKind,
        now: dt.datetime | None = None,
    ) -> WorkRelation:
        if source.project_id != target.project_id:
            raise PolicyViolation("Cross-project iliski reddedildi")
        if source.realm_id != target.realm_id:
            raise PolicyViolation("Cross-realm iliski reddedildi")
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=source.realm_id,
            project_id=source.project_id,
            source_id=source.id,
            target_id=target.id,
            kind=kind,
            created_at=moment,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "source_id": str(self.source_id),
            "target_id": str(self.target_id),
            "kind": self.kind.value,
        }


@dataclass(frozen=True, slots=True)
class Intent:
    """Isin amaci, kapsam disi maddeleri, beklenen sonuclari ve kisitlari."""

    id: UUID
    realm_id: UUID
    work_item_id: UUID
    revision: int
    goal: str
    non_goals: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValidationFailed("Intent hedefi bos olamaz")
        if self.revision < 1:
            raise ValidationFailed("Revision 1'den kucuk olamaz")

    @classmethod
    def create(
        cls,
        *,
        work_item: WorkItem,
        goal: str,
        revision: int,
        non_goals: tuple[str, ...] = (),
        outcomes: tuple[str, ...] = (),
        constraints: tuple[str, ...] = (),
        now: dt.datetime | None = None,
    ) -> Intent:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=work_item.realm_id,
            work_item_id=work_item.id,
            revision=revision,
            goal=goal.strip(),
            non_goals=non_goals,
            outcomes=outcomes,
            constraints=constraints,
            created_at=moment,
        )

    def body(self) -> dict[str, Any]:
        return {
            "work_item_id": str(self.work_item_id),
            "revision": self.revision,
            "goal": self.goal,
            "non_goals": list(self.non_goals),
            "outcomes": list(self.outcomes),
            "constraints": list(self.constraints),
        }

    @property
    def intent_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"id": str(self.id), "intent_digest": self.intent_digest}


@dataclass(frozen=True, slots=True)
class DecisionOption:
    """Degerlendirilen bir secenek."""

    name: str
    summary: str = ""
    rejected_because: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValidationFailed("Secenek adi bos olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "rejected_because": self.rejected_because,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """Karar kaydi: soru, secenekler, kriterler, gerekce ve kanit."""

    id: UUID
    realm_id: UUID
    work_item_id: UUID
    revision: int
    question: str
    chosen_option: str
    rationale: str
    alternatives: tuple[DecisionOption, ...] = ()
    criteria: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    decided_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        for label, value in (
            ("Karar sorusu", self.question),
            ("Secilen secenek", self.chosen_option),
            ("Gerekce", self.rationale),
        ):
            if not value.strip():
                raise ValidationFailed(f"{label} bos olamaz")
        if self.revision < 1:
            raise ValidationFailed("Revision 1'den kucuk olamaz")

    @classmethod
    def create(
        cls,
        *,
        work_item: WorkItem,
        revision: int,
        question: str,
        chosen_option: str,
        rationale: str,
        alternatives: tuple[DecisionOption, ...] = (),
        criteria: tuple[str, ...] = (),
        evidence: tuple[EvidenceRef, ...] = (),
        now: dt.datetime | None = None,
    ) -> Decision:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=work_item.realm_id,
            work_item_id=work_item.id,
            revision=revision,
            question=question.strip(),
            chosen_option=chosen_option.strip(),
            rationale=rationale.strip(),
            alternatives=alternatives,
            criteria=criteria,
            evidence=evidence,
            decided_at=moment,
        )

    def body(self) -> dict[str, Any]:
        return {
            "work_item_id": str(self.work_item_id),
            "revision": self.revision,
            "question": self.question,
            "chosen_option": self.chosen_option,
            "rationale": self.rationale,
            "alternatives": [item.as_dict() for item in self.alternatives],
            "criteria": list(self.criteria),
            "evidence": [item.as_dict() for item in self.evidence],
        }

    @property
    def decision_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"id": str(self.id), "decision_digest": self.decision_digest}


class EffectKind(StrEnum):
    """Bir adimin dis dunyada yapacagi etkinin turu."""

    NONE = "none"
    FILE_WRITE = "file-write"
    DATABASE_WRITE = "database-write"
    NETWORK_CALL = "network-call"
    PROVIDER_CALL = "provider-call"
    GIT_COMMIT = "git-commit"
    GIT_PUSH = "git-push"
    PROCESS_RUN = "process-run"


#: Exact authorization gerektiren etki turleri.
AUTHORIZED_EFFECTS: frozenset[EffectKind] = frozenset(
    {
        EffectKind.FILE_WRITE,
        EffectKind.DATABASE_WRITE,
        EffectKind.NETWORK_CALL,
        EffectKind.PROVIDER_CALL,
        EffectKind.GIT_COMMIT,
        EffectKind.GIT_PUSH,
        EffectKind.PROCESS_RUN,
    }
)


@dataclass(frozen=True, slots=True)
class PlanStep:
    """Plandaki tek bir adim."""

    step_id: str
    title: str
    effect: EffectKind
    logical_resources: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    risk: str = "low"

    def __post_init__(self) -> None:
        if not self.step_id.strip():
            raise ValidationFailed("Adim kimligi bos olamaz")
        if not self.title.strip():
            raise ValidationFailed("Adim basligi bos olamaz")
        if self.step_id in self.depends_on:
            raise ValidationFailed("Adim kendisine bagimli olamaz")

    @property
    def requires_authorization(self) -> bool:
        return self.effect in AUTHORIZED_EFFECTS

    def body(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "effect": self.effect.value,
            "logical_resources": sorted(self.logical_resources),
            "depends_on": sorted(self.depends_on),
            "risk": self.risk,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"requires_authorization": self.requires_authorization}


def assert_acyclic_steps(steps: tuple[PlanStep, ...]) -> tuple[str, ...]:
    """Adim grafinin acyclic oldugunu dogrular ve topolojik sirayi dondurur."""
    identifiers = [step.step_id for step in steps]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationFailed("Adim kimlikleri tekil olmali")
    known = set(identifiers)
    dependencies = {step.step_id: set(step.depends_on) for step in steps}
    for step_id, requirements in dependencies.items():
        unknown = requirements - known
        if unknown:
            raise ValidationFailed(f"{step_id} tanimsiz adima bagimli: {sorted(unknown)}")

    ordered: list[str] = []
    pending = dict(dependencies)
    while pending:
        ready = sorted(step_id for step_id, needs in pending.items() if not needs)
        if not ready:
            raise ValidationFailed("Plan adimlarinda dongu var")
        for step_id in ready:
            ordered.append(step_id)
            del pending[step_id]
        for needs in pending.values():
            needs.difference_update(ready)
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """Exact adim plani. Plan yetki vermez."""

    id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    revision: int
    source_revision: str
    policy_digest: str
    steps: tuple[PlanStep, ...]
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValidationFailed("Plan en az bir adim icermeli")
        if self.revision < 1:
            raise ValidationFailed("Revision 1'den kucuk olamaz")
        if not self.source_revision.strip():
            raise ValidationFailed("Plan source revision'a baglanmali")
        assert_acyclic_steps(self.steps)

    @classmethod
    def create(
        cls,
        *,
        work_item: WorkItem,
        revision: int,
        source_revision: str,
        policy_digest: str,
        steps: tuple[PlanStep, ...],
        now: dt.datetime | None = None,
    ) -> TaskPlan:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=work_item.realm_id,
            project_id=work_item.project_id,
            work_item_id=work_item.id,
            revision=revision,
            source_revision=source_revision,
            policy_digest=policy_digest,
            steps=steps,
            created_at=moment,
        )

    @property
    def execution_order(self) -> tuple[str, ...]:
        return assert_acyclic_steps(self.steps)

    @property
    def effect_digest(self) -> str:
        """Yalnizca dis dunyaya dokunacak adimlarin kanonik digest'i."""
        effects = [
            {
                "step_id": step.step_id,
                "effect": step.effect.value,
                "logical_resources": sorted(step.logical_resources),
            }
            for step in sorted(self.steps, key=lambda item: item.step_id)
            if step.effect is not EffectKind.NONE
        ]
        return digest(effects)

    @property
    def requires_authorization(self) -> bool:
        return any(step.requires_authorization for step in self.steps)

    @property
    def writable_resources(self) -> tuple[str, ...]:
        resources: set[str] = set()
        for step in self.steps:
            if step.requires_authorization:
                resources.update(step.logical_resources)
        return tuple(sorted(resources))

    def body(self) -> dict[str, Any]:
        return {
            "work_item_id": str(self.work_item_id),
            "project_id": str(self.project_id),
            "revision": self.revision,
            "source_revision": self.source_revision,
            "policy_digest": self.policy_digest,
            "steps": [step.body() for step in self.steps],
            "effect_digest": self.effect_digest,
            "grants_authority": False,
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.body())

    def has_drifted_from(self, *, source_revision: str, policy_digest: str) -> bool:
        """Plan uretildigi kosullardan sapmis mi?"""
        return self.source_revision != source_revision or self.policy_digest != policy_digest

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {
            "id": str(self.id),
            "plan_digest": self.plan_digest,
            "execution_order": list(self.execution_order),
            "requires_authorization": self.requires_authorization,
        }
