"""Work Graph icin PostgreSQL adapterleri.

Head satiri optimistic concurrency ile guncellenir: beklenen revision eslesmezse
guncelleme 0 satir etkiler ve `ConcurrencyConflict` yukselir.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation
from zekam.domain.work import (
    AcceptanceCriterion,
    Decision,
    DecisionOption,
    EffectKind,
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

_WORK_COLUMNS = (
    "id, realm_id, project_id, external_number, type, state, title, summary, revision,"
    " acceptance_criteria, acceptance_evidence, created_at, updated_at"
)


def _work_from_row(row: Sequence[Any]) -> WorkItem:
    return WorkItem(
        id=row[0],
        realm_id=row[1],
        project_id=row[2],
        external_number=row[3],
        type=WorkType(row[4]),
        state=WorkState(row[5]),
        title=row[6],
        summary=row[7],
        revision=row[8],
        acceptance_criteria=tuple(
            AcceptanceCriterion(text=item["text"], verified=bool(item.get("verified", False)))
            for item in row[9] or []
        ),
        acceptance_evidence=tuple(
            EvidenceRef(
                kind=item["kind"], reference=item["reference"], digest_value=item.get("digest")
            )
            for item in row[10] or []
        ),
        created_at=row[11],
        updated_at=row[12],
    )


@dataclass(frozen=True, slots=True)
class WorkItemRepository:
    """Work Item head satirini yonetir."""

    connection: Any
    realm_id: UUID

    def add(self, item: WorkItem) -> WorkItem:
        if item.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm is kaydi reddedildi")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.work_item"
                " (id, realm_id, project_id, external_number, type, state, title, summary,"
                "  revision, acceptance_criteria, acceptance_evidence, record_digest,"
                "  created_at, updated_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)",
                (
                    item.id,
                    item.realm_id,
                    item.project_id,
                    item.external_number,
                    item.type.value,
                    item.state.value,
                    item.title,
                    item.summary,
                    item.revision,
                    canonical_json([entry.as_dict() for entry in item.acceptance_criteria]),
                    canonical_json([entry.as_dict() for entry in item.acceptance_evidence]),
                    item.record_digest,
                    item.created_at,
                    item.updated_at,
                ),
            )
        return item

    def replace(self, item: WorkItem, *, expected_revision: int) -> WorkItem:
        """Head satirini optimistic concurrency ile gunceller."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update work.work_item set"
                " state = %s, title = %s, summary = %s, revision = %s,"
                " acceptance_criteria = %s::jsonb, acceptance_evidence = %s::jsonb,"
                " record_digest = %s, updated_at = %s"
                " where id = %s and revision = %s",
                (
                    item.state.value,
                    item.title,
                    item.summary,
                    item.revision,
                    canonical_json([entry.as_dict() for entry in item.acceptance_criteria]),
                    canonical_json([entry.as_dict() for entry in item.acceptance_evidence]),
                    item.record_digest,
                    item.updated_at,
                    item.id,
                    expected_revision,
                ),
            )
            if cursor.rowcount == 0:
                raise ConcurrencyConflict(
                    f"Beklenen revision {expected_revision} artik gecerli degil"
                )
        return item

    def get(self, work_item_id: UUID) -> WorkItem:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_WORK_COLUMNS} from work.work_item where id = %s", (work_item_id,)
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Is kaydi bulunamadi")
        return _work_from_row(row)

    def get_for_update(self, work_item_id: UUID) -> WorkItem:
        """Lock one exact Work head for a surrounding atomic state transition."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_WORK_COLUMNS} from work.work_item"
                " where realm_id = %s and id = %s for update",
                (self.realm_id, work_item_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Is kaydi bulunamadi")
        return _work_from_row(row)

    def find_by_external_number(self, project_id: UUID, external_number: str) -> WorkItem | None:
        """Exact numara aramasi. Semantic benzerlik numarayi degistiremez."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_WORK_COLUMNS} from work.work_item"
                " where project_id = %s and external_number = %s",
                (project_id, external_number),
            )
            row = cursor.fetchone()
        return None if row is None else _work_from_row(row)

    def list_for_project(
        self,
        project_id: UUID,
        *,
        states: Sequence[WorkState] | None = None,
        types: Sequence[WorkType] | None = None,
        limit: int = 200,
    ) -> tuple[WorkItem, ...]:
        query = f"select {_WORK_COLUMNS} from work.work_item where project_id = %s"
        parameters: list[Any] = [project_id]
        if states:
            query += " and state = any(%s)"
            parameters.append([state.value for state in states])
        if types:
            query += " and type = any(%s)"
            parameters.append([item.value for item in types])
        query += " order by updated_at desc, id desc limit %s"
        parameters.append(limit)
        with self.connection.cursor() as cursor:
            cursor.execute(query, parameters)
            rows = cursor.fetchall()
        return tuple(_work_from_row(row) for row in rows)

    def list_open(self, *, limit: int = 200) -> tuple[WorkItem, ...]:
        """Realm genelinde acik isler."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_WORK_COLUMNS} from work.work_item"
                " where state in ('proposed', 'ready', 'active', 'blocked', 'verification')"
                " order by updated_at desc, id desc limit %s",
                (limit,),
            )
            rows = cursor.fetchall()
        return tuple(_work_from_row(row) for row in rows)


@dataclass(frozen=True, slots=True)
class WorkRelationRepository:
    """Is iliskilerini yonetir."""

    connection: Any
    realm_id: UUID

    def add(self, relation: WorkRelation) -> WorkRelation:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.work_relation"
                " (id, realm_id, project_id, source_id, target_id, kind, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                (
                    relation.id,
                    relation.realm_id,
                    relation.project_id,
                    relation.source_id,
                    relation.target_id,
                    relation.kind.value,
                    relation.created_at,
                ),
            )
        return relation

    def remove(self, relation_id: UUID) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute("delete from work.work_relation where id = %s", (relation_id,))
            return bool(cursor.rowcount)

    def outgoing(self, work_item_id: UUID) -> tuple[WorkRelation, ...]:
        return self._query("source_id = %s", work_item_id)

    def incoming(self, work_item_id: UUID) -> tuple[WorkRelation, ...]:
        return self._query("target_id = %s", work_item_id)

    def _query(self, predicate: str, value: UUID) -> tuple[WorkRelation, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, project_id, source_id, target_id, kind, created_at"
                f" from work.work_relation where {predicate} order by created_at",
                (value,),
            )
            rows = cursor.fetchall()
        return tuple(
            WorkRelation(
                id=row[0],
                realm_id=row[1],
                project_id=row[2],
                source_id=row[3],
                target_id=row[4],
                kind=RelationKind(row[5]),
                created_at=row[6],
            )
            for row in rows
        )

    def unmet_dependencies(self, work_item_id: UUID) -> tuple[UUID, ...]:
        """Tamamlanmamis `depends-on` hedeflerini dondurur."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select w.id from work.work_relation r"
                " join work.work_item w on w.id = r.target_id"
                " where r.source_id = %s and r.kind = 'depends-on'"
                "   and w.state <> 'completed'"
                " order by w.id",
                (work_item_id,),
            )
            return tuple(row[0] for row in cursor.fetchall())

    def blockers(self, work_item_id: UUID) -> tuple[UUID, ...]:
        """Bu isi bloklayan acik isleri dondurur."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select w.id from work.work_relation r"
                " join work.work_item w on w.id = r.source_id"
                " where r.target_id = %s and r.kind = 'blocks'"
                "   and w.state in ('proposed', 'ready', 'active', 'blocked', 'verification')"
                " order by w.id",
                (work_item_id,),
            )
            return tuple(row[0] for row in cursor.fetchall())


@dataclass(frozen=True, slots=True)
class IntentRepository:
    """Intent revision'larini yonetir (append-only)."""

    connection: Any
    realm_id: UUID

    def append(self, intent: Intent) -> Intent:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.intent"
                " (id, realm_id, work_item_id, revision, goal, non_goals, outcomes,"
                "  constraints, intent_digest, created_at)"
                " values (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)",
                (
                    intent.id,
                    intent.realm_id,
                    intent.work_item_id,
                    intent.revision,
                    intent.goal,
                    canonical_json(list(intent.non_goals)),
                    canonical_json(list(intent.outcomes)),
                    canonical_json(list(intent.constraints)),
                    intent.intent_digest,
                    intent.created_at,
                ),
            )
        return intent

    def current(self, work_item_id: UUID) -> Intent | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, work_item_id, revision, goal, non_goals, outcomes,"
                " constraints, created_at from work.intent"
                " where work_item_id = %s order by revision desc limit 1",
                (work_item_id,),
            )
            row = cursor.fetchone()
        return None if row is None else _intent_from_row(row)

    def history(self, work_item_id: UUID) -> tuple[Intent, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, work_item_id, revision, goal, non_goals, outcomes,"
                " constraints, created_at from work.intent"
                " where work_item_id = %s order by revision",
                (work_item_id,),
            )
            rows = cursor.fetchall()
        return tuple(_intent_from_row(row) for row in rows)

    def next_revision(self, work_item_id: UUID) -> int:
        current = self.current(work_item_id)
        return 1 if current is None else current.revision + 1


def _intent_from_row(row: Sequence[Any]) -> Intent:
    return Intent(
        id=row[0],
        realm_id=row[1],
        work_item_id=row[2],
        revision=row[3],
        goal=row[4],
        non_goals=tuple(row[5] or []),
        outcomes=tuple(row[6] or []),
        constraints=tuple(row[7] or []),
        created_at=row[8],
    )


@dataclass(frozen=True, slots=True)
class DecisionRepository:
    """Karar revision'larini yonetir (append-only)."""

    connection: Any
    realm_id: UUID

    def append(self, decision: Decision) -> Decision:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.decision"
                " (id, realm_id, work_item_id, revision, question, chosen_option, alternatives,"
                "  criteria, rationale, evidence, decision_digest, decided_at)"
                " values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s)",
                (
                    decision.id,
                    decision.realm_id,
                    decision.work_item_id,
                    decision.revision,
                    decision.question,
                    decision.chosen_option,
                    canonical_json([item.as_dict() for item in decision.alternatives]),
                    canonical_json(list(decision.criteria)),
                    decision.rationale,
                    canonical_json([item.as_dict() for item in decision.evidence]),
                    decision.decision_digest,
                    decision.decided_at,
                ),
            )
        return decision

    def current(self, work_item_id: UUID) -> Decision | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, work_item_id, revision, question, chosen_option,"
                " alternatives, criteria, rationale, evidence, decided_at from work.decision"
                " where work_item_id = %s order by revision desc limit 1",
                (work_item_id,),
            )
            row = cursor.fetchone()
        return None if row is None else _decision_from_row(row)

    def history(self, work_item_id: UUID) -> tuple[Decision, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, work_item_id, revision, question, chosen_option,"
                " alternatives, criteria, rationale, evidence, decided_at from work.decision"
                " where work_item_id = %s order by revision",
                (work_item_id,),
            )
            rows = cursor.fetchall()
        return tuple(_decision_from_row(row) for row in rows)

    def next_revision(self, work_item_id: UUID) -> int:
        current = self.current(work_item_id)
        return 1 if current is None else current.revision + 1


def _decision_from_row(row: Sequence[Any]) -> Decision:
    return Decision(
        id=row[0],
        realm_id=row[1],
        work_item_id=row[2],
        revision=row[3],
        question=row[4],
        chosen_option=row[5],
        alternatives=tuple(
            DecisionOption(
                name=item["name"],
                summary=item.get("summary", ""),
                rejected_because=item.get("rejected_because"),
            )
            for item in row[6] or []
        ),
        criteria=tuple(row[7] or []),
        rationale=row[8],
        evidence=tuple(
            EvidenceRef(
                kind=item["kind"], reference=item["reference"], digest_value=item.get("digest")
            )
            for item in row[9] or []
        ),
        decided_at=row[10],
    )


@dataclass(frozen=True, slots=True)
class TaskPlanRepository:
    """Task Plan revision'larini yonetir (append-only)."""

    connection: Any
    realm_id: UUID

    def append(self, plan: TaskPlan) -> TaskPlan:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into work.task_plan"
                " (id, realm_id, project_id, work_item_id, revision, source_revision,"
                "  policy_digest, steps, effect_digest, plan_digest, grants_authority, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, false, %s)",
                (
                    plan.id,
                    plan.realm_id,
                    plan.project_id,
                    plan.work_item_id,
                    plan.revision,
                    plan.source_revision,
                    plan.policy_digest,
                    canonical_json([step.body() for step in plan.steps]),
                    plan.effect_digest,
                    plan.plan_digest,
                    plan.created_at,
                ),
            )
        return plan

    def current(self, work_item_id: UUID) -> TaskPlan | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, project_id, work_item_id, revision, source_revision,"
                " policy_digest, steps, created_at from work.task_plan"
                " where work_item_id = %s order by revision desc limit 1",
                (work_item_id,),
            )
            row = cursor.fetchone()
        return None if row is None else _plan_from_row(row)

    def history(self, work_item_id: UUID) -> tuple[TaskPlan, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, project_id, work_item_id, revision, source_revision,"
                " policy_digest, steps, created_at from work.task_plan"
                " where work_item_id = %s order by revision",
                (work_item_id,),
            )
            rows = cursor.fetchall()
        return tuple(_plan_from_row(row) for row in rows)

    def next_revision(self, work_item_id: UUID) -> int:
        current = self.current(work_item_id)
        return 1 if current is None else current.revision + 1

    def find_by_effect_digest(self, effect_digest: str) -> tuple[TaskPlan, ...]:
        """Ayni exact etkiyi tarif eden planlari bulur (idempotency icin)."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, project_id, work_item_id, revision, source_revision,"
                " policy_digest, steps, created_at from work.task_plan"
                " where effect_digest = %s order by created_at",
                (effect_digest,),
            )
            rows = cursor.fetchall()
        return tuple(_plan_from_row(row) for row in rows)


def _plan_from_row(row: Sequence[Any]) -> TaskPlan:
    return TaskPlan(
        id=row[0],
        realm_id=row[1],
        project_id=row[2],
        work_item_id=row[3],
        revision=row[4],
        source_revision=row[5],
        policy_digest=row[6],
        steps=tuple(
            PlanStep(
                step_id=item["step_id"],
                title=item["title"],
                effect=EffectKind(item["effect"]),
                logical_resources=tuple(item.get("logical_resources") or []),
                depends_on=tuple(item.get("depends_on") or []),
                risk=item.get("risk", "low"),
            )
            for item in row[7] or []
        ),
        created_at=row[8],
    )


@dataclass(frozen=True, slots=True)
class WorkQueryRepository:
    """Kanonik kayittan dogrudan yanit ureten sorgular.

    Bu sorgular semantic index veya vector store kullanmaz; yanit her zaman
    Work Graph'tan gelir.
    """

    connection: Any
    realm_id: UUID

    def today(self, *, now: dt.datetime | None = None, limit: int = 50) -> tuple[WorkItem, ...]:
        """Bugun uzerinde calisilabilecek isler.

        Sira: active, verification, blocked, ready, proposed; sonra guncellenme
        zamani.
        """
        del now  # Su an tarih filtresi yok; imza zamanla genisleyecek.
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_WORK_COLUMNS} from work.work_item"
                " where state in ('active', 'verification', 'blocked', 'ready', 'proposed')"
                " order by case state"
                "   when 'active' then 0"
                "   when 'verification' then 1"
                "   when 'blocked' then 2"
                "   when 'ready' then 3"
                "   else 4 end,"
                " updated_at desc, id desc"
                " limit %s",
                (limit,),
            )
            rows = cursor.fetchall()
        return tuple(_work_from_row(row) for row in rows)

    def next_actionable(self, project_id: UUID | None = None) -> WorkItem | None:
        """Bagimliligi karsilanmis, bloklanmamis en oncelikli is."""
        query = (
            f"select {_WORK_COLUMNS} from work.work_item w"
            " where w.state in ('ready', 'active')"
            "   and not exists ("
            "     select 1 from work.work_relation r join work.work_item t on t.id = r.target_id"
            "     where r.source_id = w.id and r.kind = 'depends-on' and t.state <> 'completed')"
            "   and not exists ("
            "     select 1 from work.work_relation r join work.work_item s on s.id = r.source_id"
            "     where r.target_id = w.id and r.kind = 'blocks'"
            "       and s.state in ('proposed','ready','active','blocked','verification'))"
        )
        parameters: list[Any] = []
        if project_id is not None:
            query += " and w.project_id = %s"
            parameters.append(project_id)
        query += (
            " order by case w.state when 'active' then 0 else 1 end, w.updated_at desc, w.id desc"
            " limit 1"
        )
        with self.connection.cursor() as cursor:
            cursor.execute(query, parameters)
            row = cursor.fetchone()
        return None if row is None else _work_from_row(row)

    def blocked_with_reasons(self) -> tuple[tuple[WorkItem, tuple[UUID, ...]], ...]:
        """Bloklu isleri ve blocker kimliklerini dondurur."""
        relations = WorkRelationRepository(self.connection, self.realm_id)
        items = WorkItemRepository(self.connection, self.realm_id).list_open()
        result: list[tuple[WorkItem, tuple[UUID, ...]]] = []
        for item in items:
            reasons = relations.unmet_dependencies(item.id) + relations.blockers(item.id)
            if reasons or item.state is WorkState.BLOCKED:
                result.append((item, reasons))
        return tuple(result)

    def recent_activity(self, *, limit: int = 20) -> tuple[dict[str, Any], ...]:
        """Son olaylari kanonik olay kaydindan okur."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select e.sequence, e.event_type, e.entity_id, e.occurred_at, w.title, w.state"
                " from core.event e"
                " left join work.work_item w on w.id = e.entity_id"
                " where e.entity_type = 'work.item'"
                " order by e.sequence desc limit %s",
                (limit,),
            )
            rows = cursor.fetchall()
        return tuple(
            {
                "sequence": int(row[0]),
                "event_type": row[1],
                "work_item_id": str(row[2]),
                "occurred_at": row[3],
                "title": row[4],
                "state": row[5],
            }
            for row in rows
        )
