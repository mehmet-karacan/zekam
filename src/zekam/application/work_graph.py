"""Work Graph uygulama servisi.

Bu servis Work Item, iliski, Intent, Decision ve Task Plan islemlerini tek yerden
yurutur ve her degisiklikte:

1. head satirini optimistic concurrency ile gunceller,
2. tam durumu `core.revision` hash zincirine ekler,
3. gorunur bir olayi `core.event` kaydina yazar.

Boylece "Markdown'da tamamlandi yaziyor" iddiasi ile kanonik kayit arasindaki fark
her zaman gorunur olur.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed
from zekam.domain.realm import Realm
from zekam.domain.work import (
    WORK_ENTITY_TYPE,
    AcceptanceCriterion,
    Decision,
    DecisionOption,
    EvidenceRef,
    Intent,
    PlanStep,
    RelationKind,
    TaskPlan,
    WorkItem,
    WorkRelation,
    WorkState,
    WorkType,
)
from zekam.infrastructure.postgres.core_repository import EventStore, RevisionStore
from zekam.infrastructure.postgres.work_repository import (
    DecisionRepository,
    IntentRepository,
    TaskPlanRepository,
    WorkItemRepository,
    WorkQueryRepository,
    WorkRelationRepository,
)


@dataclass(frozen=True, slots=True)
class WorkSnapshot:
    """Bir isin tam gorunumu."""

    item: WorkItem
    intent: Intent | None
    decision: Decision | None
    plan: TaskPlan | None
    outgoing: tuple[WorkRelation, ...]
    incoming: tuple[WorkRelation, ...]
    unmet_dependencies: tuple[UUID, ...]
    blockers: tuple[UUID, ...]

    @property
    def is_actionable(self) -> bool:
        """Bagimliligi karsilanmis ve bloklanmamis mi?"""
        return not self.unmet_dependencies and not self.blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "work_item": self.item.as_dict(),
            "intent": None if self.intent is None else self.intent.as_dict(),
            "decision": None if self.decision is None else self.decision.as_dict(),
            "plan": None if self.plan is None else self.plan.as_dict(),
            "relations": {
                "outgoing": [item.as_dict() for item in self.outgoing],
                "incoming": [item.as_dict() for item in self.incoming],
            },
            "unmet_dependencies": [str(item) for item in self.unmet_dependencies],
            "blockers": [str(item) for item in self.blockers],
            "is_actionable": self.is_actionable,
        }


@dataclass(frozen=True, slots=True)
class WorkGraphService:
    """Work Graph islemleri."""

    connection: Any
    realm: Realm
    actor_id: UUID | None = None

    # -- depolar ----------------------------------------------------------------

    @property
    def items(self) -> WorkItemRepository:
        return WorkItemRepository(self.connection, self.realm.id)

    @property
    def relations(self) -> WorkRelationRepository:
        return WorkRelationRepository(self.connection, self.realm.id)

    @property
    def intents(self) -> IntentRepository:
        return IntentRepository(self.connection, self.realm.id)

    @property
    def decisions(self) -> DecisionRepository:
        return DecisionRepository(self.connection, self.realm.id)

    @property
    def plans(self) -> TaskPlanRepository:
        return TaskPlanRepository(self.connection, self.realm.id)

    @property
    def queries(self) -> WorkQueryRepository:
        return WorkQueryRepository(self.connection, self.realm.id)

    @property
    def revisions(self) -> RevisionStore:
        return RevisionStore(self.connection, self.realm.id)

    @property
    def events(self) -> EventStore:
        return EventStore(self.connection, self.realm.id)

    # -- Work Item ---------------------------------------------------------------

    def create_item(
        self,
        *,
        project_id: UUID,
        type: WorkType,
        title: str,
        summary: str = "",
        external_number: str | None = None,
        acceptance_criteria: tuple[AcceptanceCriterion, ...] = (),
        reason: str = "is kaydi olusturuldu",
        now: dt.datetime | None = None,
    ) -> WorkItem:
        """Yeni is kaydi olusturur ve revision/olay zincirini baslatir."""
        moment = now or dt.datetime.now(dt.UTC)
        item = WorkItem.create(
            realm_id=self.realm.id,
            project_id=project_id,
            type=type,
            title=title,
            summary=summary,
            external_number=external_number,
            acceptance_criteria=acceptance_criteria,
            now=moment,
        )
        self.items.add(item)
        self._record(item, reason=reason, event_type="work.created", now=moment)
        return item

    def transition(
        self,
        work_item_id: UUID,
        target: WorkState,
        *,
        evidence: tuple[EvidenceRef, ...] = (),
        reason: str | None = None,
        now: dt.datetime | None = None,
    ) -> WorkItem:
        """Durumu degistirir. Izinsiz gecis ve kanitsiz `completed` reddedilir."""
        moment = now or dt.datetime.now(dt.UTC)
        current = self.items.get(work_item_id)
        if target is WorkState.ACTIVE:
            self._assert_actionable(current)
        updated = current.with_state(target, evidence=evidence, now=moment)
        self.items.replace(updated, expected_revision=current.revision)
        self._record(
            updated,
            reason=reason or f"durum {current.state.value} -> {target.value}",
            event_type=f"work.state.{target.value}",
            now=moment,
        )
        return updated

    def update_details(
        self,
        work_item_id: UUID,
        *,
        title: str | None = None,
        summary: str | None = None,
        acceptance_criteria: tuple[AcceptanceCriterion, ...] | None = None,
        reason: str = "icerik guncellendi",
        now: dt.datetime | None = None,
    ) -> WorkItem:
        """Baslik, ozet veya kabul kriterlerini gunceller."""
        moment = now or dt.datetime.now(dt.UTC)
        current = self.items.get(work_item_id)
        updated = current.with_details(
            title=title, summary=summary, acceptance_criteria=acceptance_criteria, now=moment
        )
        self.items.replace(updated, expected_revision=current.revision)
        self._record(updated, reason=reason, event_type="work.updated", now=moment)
        return updated

    def _assert_actionable(self, item: WorkItem) -> None:
        unmet = self.relations.unmet_dependencies(item.id)
        blockers = self.relations.blockers(item.id)
        if unmet or blockers:
            raise PolicyViolation(
                "Bagimlilik veya blocker cozulmeden is aktif edilemez: "
                f"{len(unmet)} bagimlilik, {len(blockers)} blocker"
            )

    def _record(self, item: WorkItem, *, reason: str, event_type: str, now: dt.datetime) -> None:
        revision = self.revisions.append(
            entity_type=WORK_ENTITY_TYPE,
            entity_id=item.id,
            payload=item.body(),
            reason=reason,
            actor_id=self.actor_id,
            now=now,
        )
        self.events.append(
            event_type=event_type,
            entity_type=WORK_ENTITY_TYPE,
            entity_id=item.id,
            payload={"state": item.state.value, "revision": item.revision},
            revision_id=revision.id,
            actor_id=self.actor_id,
            occurred_at=now,
        )

    # -- iliskiler ----------------------------------------------------------------

    def relate(
        self,
        *,
        source_id: UUID,
        target_id: UUID,
        kind: RelationKind,
        now: dt.datetime | None = None,
    ) -> WorkRelation:
        """Iki is kaydini iliskilendirir. Dongu ve cross-project reddedilir."""
        source = self.items.get(source_id)
        target = self.items.get(target_id)
        relation = WorkRelation.create(source=source, target=target, kind=kind, now=now)
        return self.relations.add(relation)

    def unrelate(self, relation_id: UUID) -> bool:
        return self.relations.remove(relation_id)

    # -- Intent / Decision / Plan --------------------------------------------------

    def set_intent(
        self,
        work_item_id: UUID,
        *,
        goal: str,
        non_goals: tuple[str, ...] = (),
        outcomes: tuple[str, ...] = (),
        constraints: tuple[str, ...] = (),
        now: dt.datetime | None = None,
    ) -> Intent:
        """Yeni Intent revision'i ekler."""
        moment = now or dt.datetime.now(dt.UTC)
        item = self.items.get(work_item_id)
        intent = Intent.create(
            work_item=item,
            goal=goal,
            revision=self.intents.next_revision(work_item_id),
            non_goals=non_goals,
            outcomes=outcomes,
            constraints=constraints,
            now=moment,
        )
        self.intents.append(intent)
        self.events.append(
            event_type="work.intent.recorded",
            entity_type=WORK_ENTITY_TYPE,
            entity_id=work_item_id,
            payload={"revision": intent.revision, "intent_digest": intent.intent_digest},
            actor_id=self.actor_id,
            occurred_at=moment,
        )
        return intent

    def record_decision(
        self,
        work_item_id: UUID,
        *,
        question: str,
        chosen_option: str,
        rationale: str,
        alternatives: tuple[DecisionOption, ...] = (),
        criteria: tuple[str, ...] = (),
        evidence: tuple[EvidenceRef, ...] = (),
        now: dt.datetime | None = None,
    ) -> Decision:
        """Yeni karar revision'i ekler."""
        moment = now or dt.datetime.now(dt.UTC)
        item = self.items.get(work_item_id)
        decision = Decision.create(
            work_item=item,
            revision=self.decisions.next_revision(work_item_id),
            question=question,
            chosen_option=chosen_option,
            rationale=rationale,
            alternatives=alternatives,
            criteria=criteria,
            evidence=evidence,
            now=moment,
        )
        self.decisions.append(decision)
        self.events.append(
            event_type="work.decision.recorded",
            entity_type=WORK_ENTITY_TYPE,
            entity_id=work_item_id,
            payload={"revision": decision.revision, "decision_digest": decision.decision_digest},
            actor_id=self.actor_id,
            occurred_at=moment,
        )
        return decision

    def create_plan(
        self,
        work_item_id: UUID,
        *,
        source_revision: str,
        policy_digest: str,
        steps: tuple[PlanStep, ...],
        now: dt.datetime | None = None,
    ) -> TaskPlan:
        """Yeni plan revision'i ekler. Plan yetki vermez."""
        moment = now or dt.datetime.now(dt.UTC)
        item = self.items.get(work_item_id)
        plan = TaskPlan.create(
            work_item=item,
            revision=self.plans.next_revision(work_item_id),
            source_revision=source_revision,
            policy_digest=policy_digest,
            steps=steps,
            now=moment,
        )
        self.plans.append(plan)
        self.events.append(
            event_type="work.plan.created",
            entity_type=WORK_ENTITY_TYPE,
            entity_id=work_item_id,
            payload={
                "revision": plan.revision,
                "plan_digest": plan.plan_digest,
                "effect_digest": plan.effect_digest,
                "requires_authorization": plan.requires_authorization,
            },
            actor_id=self.actor_id,
            occurred_at=moment,
        )
        return plan

    def assert_plan_is_current(
        self, work_item_id: UUID, *, source_revision: str, policy_digest: str
    ) -> TaskPlan:
        """Planin uretildigi kosullardan sapmadigini dogrular."""
        plan = self.plans.current(work_item_id)
        if plan is None:
            raise NotFound("Is kaydinin plani yok")
        if plan.has_drifted_from(source_revision=source_revision, policy_digest=policy_digest):
            raise PolicyViolation("Plan stale: source veya policy degismis, yeni revision gerekir")
        return plan

    # -- sorgular --------------------------------------------------------------------

    def snapshot(self, work_item_id: UUID) -> WorkSnapshot:
        """Isin tam gorunumunu kanonik kayittan uretir."""
        item = self.items.get(work_item_id)
        return WorkSnapshot(
            item=item,
            intent=self.intents.current(work_item_id),
            decision=self.decisions.current(work_item_id),
            plan=self.plans.current(work_item_id),
            outgoing=self.relations.outgoing(work_item_id),
            incoming=self.relations.incoming(work_item_id),
            unmet_dependencies=self.relations.unmet_dependencies(work_item_id),
            blockers=self.relations.blockers(work_item_id),
        )

    def history(self, work_item_id: UUID) -> tuple[dict[str, Any], ...]:
        """Isin degisim tarihcesini hash zincirinden okur."""
        records = self.revisions.history(entity_type=WORK_ENTITY_TYPE, entity_id=work_item_id)
        return tuple(
            {
                "revision": record.revision,
                "state": record.payload.get("state"),
                "reason": record.reason,
                "payload_digest": record.payload_digest,
                "previous_digest": record.previous_digest,
                "recorded_at": record.recorded_at,
            }
            for record in records
        )

    def verify_history(self, work_item_id: UUID) -> bool:
        """Tarihce zincirinin kopuk olmadigini bagimsiz dogrular."""
        return self.revisions.verify_chain(entity_type=WORK_ENTITY_TYPE, entity_id=work_item_id)

    def find_exact(self, *, project_id: UUID, external_number: str) -> WorkItem:
        """Talep/defect numarasini exact arar; benzerlik kullanmaz."""
        if not external_number.strip():
            raise ValidationFailed("Numara bos olamaz")
        found = self.items.find_by_external_number(project_id, external_number.strip())
        if found is None:
            raise NotFound(f"{external_number} numarali kayit bulunamadi")
        return found

    def today(self, *, limit: int = 50) -> tuple[WorkItem, ...]:
        return self.queries.today(limit=limit)

    def next_actionable(self, project_id: UUID | None = None) -> WorkItem | None:
        return self.queries.next_actionable(project_id)

    def where_did_we_stop(self, *, limit: int = 10) -> dict[str, Any]:
        """ "Nerede kaldik?" sorusunu kanonik kayitlardan yanitlar."""
        activity = self.queries.recent_activity(limit=limit)
        open_items = self.items.list_open(limit=limit)
        blocked = self.queries.blocked_with_reasons()
        candidate = self.next_actionable()
        return {
            "recent_activity": list(activity),
            "open_items": [item.as_dict() for item in open_items],
            "blocked": [
                {"work_item": item.as_dict(), "blockers": [str(value) for value in reasons]}
                for item, reasons in blocked
            ],
            "next_actionable": None if candidate is None else candidate.as_dict(),
            "next_safe_action": _next_safe_action(candidate, open_items, blocked),
            "source": "work-graph",
        }


def _next_safe_action(
    candidate: WorkItem | None,
    open_items: Sequence[WorkItem],
    blocked: Sequence[tuple[WorkItem, tuple[UUID, ...]]],
) -> str:
    if candidate is not None:
        return f"{candidate.title} isini surdur ({candidate.state.value})"
    if blocked:
        return f"{len(blocked)} bloklu is var; once blocker raporunu inceleyin"
    if open_items:
        return "Acik isler var fakat hicbiri hazir degil; bagimliliklari gozden gecirin"
    return "Acik is yok; yeni is kaydi olusturun"
