"""Read-only, repeatable-snapshot source for Markdown project projections."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.errors import NotFound, ValidationFailed
from zekam.domain.markdown_projection import (
    ObsidianNoteKind,
    ObsidianProjectionRecord,
    ProjectionRecord,
    ProjectionRelationRef,
    ProjectionSourceRef,
)
from zekam.domain.session_continuity import DataClassification, TruthClass
from zekam.domain.work import (
    AcceptanceCriterion,
    EvidenceRef,
    WorkItem,
    WorkState,
    WorkType,
)

MAX_PROJECTION_RECORDS = 1000
MAX_PROJECTION_RELATIONS = 5000


def _one_line(value: Any, fallback: str) -> str:
    collapsed = " ".join(str(value).split())
    return collapsed or fallback


def _relation(
    *, relation_id: str, direction: str, kind: str, other_id: str
) -> ProjectionRelationRef:
    return ProjectionRelationRef(
        relation_id,
        direction,
        kind,
        other_id,
        digest(
            {
                "id": relation_id,
                "direction": direction,
                "kind": kind,
                "other_entity_id": other_id,
            }
        ),
    )


def _sorted_relations(
    values: list[ProjectionRelationRef],
) -> tuple[ProjectionRelationRef, ...]:
    return tuple(sorted(values, key=lambda item: tuple(item.as_dict().values())))


def _work_records(
    items: tuple[tuple[WorkItem, str], ...],
    relations: list[tuple[UUID, UUID, UUID, str]],
) -> tuple[ProjectionRecord, ...]:
    item_ids = tuple(item.id for item, _ in items)
    relation_refs: dict[UUID, list[ProjectionRelationRef]] = {item_id: [] for item_id in item_ids}
    for relation_id, source_id, target_id, kind in relations:
        relation_digest = digest(
            {
                "id": str(relation_id),
                "source_id": str(source_id),
                "target_id": str(target_id),
                "kind": kind,
            }
        )
        if source_id in relation_refs:
            relation_refs[source_id].append(
                ProjectionRelationRef(
                    str(relation_id), "outgoing", kind, str(target_id), relation_digest
                )
            )
        if target_id in relation_refs:
            relation_refs[target_id].append(
                ProjectionRelationRef(
                    str(relation_id), "incoming", kind, str(source_id), relation_digest
                )
            )
    return tuple(
        ProjectionRecord(
            entity_type="work",
            entity_id=str(item.id),
            title=item.title,
            status=item.state.value,
            summary=item.summary.strip() or "Ozet kaydedilmedi.",
            source_refs=(
                ProjectionSourceRef(
                    "work-item",
                    str(item.id),
                    f"revision-{item.revision}",
                    stored_digest,
                ),
            ),
            related_entity_ids=tuple(
                sorted({entry.other_entity_id for entry in relation_refs[item.id]})
            ),
            relation_refs=_sorted_relations(relation_refs[item.id]),
        )
        for item, stored_digest in items
    )


def _work_item(row: Sequence[Any]) -> tuple[WorkItem, str]:
    item = WorkItem(
        id=UUID(str(row[0])),
        realm_id=UUID(str(row[1])),
        project_id=UUID(str(row[2])),
        external_number=row[3],
        type=WorkType(row[4]),
        state=WorkState(row[5]),
        title=str(row[6]),
        summary=str(row[7]),
        revision=int(row[8]),
        acceptance_criteria=tuple(
            AcceptanceCriterion(str(entry["text"]), bool(entry.get("verified", False)))
            for entry in row[9] or []
        ),
        acceptance_evidence=tuple(
            EvidenceRef(
                str(entry["kind"]),
                str(entry["reference"]),
                entry.get("digest"),
            )
            for entry in row[10] or []
        ),
        created_at=row[11],
        updated_at=row[12],
    )
    stored_digest = str(row[13])
    if item.record_digest != stored_digest:
        raise ValidationFailed("projection DB work record digest semantic govdeyle uyusmuyor")
    return item, stored_digest


@dataclass(frozen=True, slots=True)
class PostgresMarkdownProjectionRepository:
    connection: Any
    realm_id: UUID

    def load_project_records(
        self, project_id: UUID, *, limit: int = 500
    ) -> tuple[ProjectionRecord, ...]:
        """Read one exact repeatable DB snapshot; never creates projection authority."""

        if not 1 <= limit <= MAX_PROJECTION_RECORDS:
            raise ValidationFailed("projection DB limiti 1..1000 araliginda olmali")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute("set transaction isolation level repeatable read read only")
            cursor.execute(
                "select id,realm_id,project_id,external_number,type,state,title,summary,revision,"
                " acceptance_criteria,acceptance_evidence,created_at,updated_at,record_digest"
                " from work.work_item"
                " where realm_id=%s and project_id=%s"
                " order by id limit %s",
                (self.realm_id, project_id, limit + 1),
            )
            rows = cursor.fetchall()
            if len(rows) > limit:
                raise ValidationFailed("projection DB snapshot bounded limiti asiyor")
            items = tuple(_work_item(row) for row in rows)
            item_ids = tuple(item.id for item, _ in items)
            relations: list[tuple[UUID, UUID, UUID, str]] = []
            if item_ids:
                cursor.execute(
                    "select id,source_id,target_id,kind from work.work_relation"
                    " where realm_id=%s and project_id=%s"
                    " and (source_id=any(%s) or target_id=any(%s))"
                    " order by source_id,target_id,kind,id limit %s",
                    (
                        self.realm_id,
                        project_id,
                        list(item_ids),
                        list(item_ids),
                        MAX_PROJECTION_RELATIONS + 1,
                    ),
                )
                relations = [
                    (UUID(str(relation_id)), UUID(str(source_id)), UUID(str(target_id)), str(kind))
                    for relation_id, source_id, target_id, kind in cursor.fetchall()
                ]
                if len(relations) > MAX_PROJECTION_RELATIONS:
                    raise ValidationFailed("projection DB relation bounded limiti asiyor")
        return _work_records(items, relations)

    def load_obsidian_records(
        self,
        project_id: UUID,
        *,
        realm_slug: str,
        limit: int = MAX_PROJECTION_RECORDS,
    ) -> tuple[ObsidianProjectionRecord, ...]:
        """Read all supported views from one bounded repeatable-read snapshot."""

        if not isinstance(project_id, UUID):
            raise ValidationFailed("Obsidian DB snapshot exact project UUID ister")
        if not 1 <= limit <= MAX_PROJECTION_RECORDS:
            raise ValidationFailed("Obsidian DB limiti 1..1000 araliginda olmali")

        def bounded(cursor: Any, sql: str, params: tuple[Any, ...]) -> list[Any]:
            cursor.execute(sql, (*params, limit + 1))
            rows = list(cursor.fetchall())
            if len(rows) > limit:
                raise ValidationFailed("Obsidian DB source family bounded limiti asiyor")
            return rows

        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute("set transaction isolation level repeatable read read only")
            cursor.execute(
                "select 1 from projects.project where realm_id=%s and id=%s",
                (self.realm_id, project_id),
            )
            if cursor.fetchone() is None:
                raise NotFound("Obsidian exact project bulunamadi")
            work_rows = bounded(
                cursor,
                "select id,realm_id,project_id,external_number,type,state,title,summary,revision,"
                " acceptance_criteria,acceptance_evidence,created_at,updated_at,record_digest"
                " from work.work_item where realm_id=%s and project_id=%s"
                " order by id limit %s",
                (self.realm_id, project_id),
            )
            items = tuple(_work_item(row) for row in work_rows)
            item_ids = tuple(item.id for item, _ in items)
            work_relations: list[tuple[UUID, UUID, UUID, str]] = []
            if item_ids:
                cursor.execute(
                    "select id,source_id,target_id,kind from work.work_relation"
                    " where realm_id=%s and project_id=%s"
                    " and (source_id=any(%s) or target_id=any(%s))"
                    " order by source_id,target_id,kind,id limit %s",
                    (
                        self.realm_id,
                        project_id,
                        list(item_ids),
                        list(item_ids),
                        MAX_PROJECTION_RELATIONS + 1,
                    ),
                )
                work_relations = [
                    (UUID(str(row[0])), UUID(str(row[1])), UUID(str(row[2])), str(row[3]))
                    for row in cursor.fetchall()
                ]
                if len(work_relations) > MAX_PROJECTION_RELATIONS:
                    raise ValidationFailed("Obsidian work relation limiti asiyor")
            decision_rows = bounded(
                cursor,
                "select d.id,d.work_item_id,d.revision,d.question,d.chosen_option,"
                " d.rationale,d.decision_digest,d.decided_at"
                " from work.decision d join work.work_item w"
                " on w.realm_id=d.realm_id and w.id=d.work_item_id"
                " where d.realm_id=%s and w.project_id=%s"
                " order by d.work_item_id,d.revision,d.id limit %s",
                (self.realm_id, project_id),
            )
            memory_rows = bounded(
                cursor,
                "select id,memory_class,state,revision,valid_from,valid_until,"
                " superseded_by,record_digest,created_at"
                " from memory.record where realm_id=%s and project_id=%s"
                " order by id limit %s",
                (self.realm_id, project_id),
            )
            memory_ids = tuple(UUID(str(row[0])) for row in memory_rows)
            memory_relations: list[Any] = []
            if memory_ids:
                cursor.execute(
                    "select id,from_id,to_id,kind from memory.relation"
                    " where realm_id=%s and from_id=any(%s) and to_id=any(%s)"
                    " order by from_id,to_id,kind,id limit %s",
                    (
                        self.realm_id,
                        list(memory_ids),
                        list(memory_ids),
                        MAX_PROJECTION_RELATIONS + 1,
                    ),
                )
                memory_relations = list(cursor.fetchall())
                if len(memory_relations) > MAX_PROJECTION_RELATIONS:
                    raise ValidationFailed("Obsidian memory relation limiti asiyor")
            skill_rows = bounded(
                cursor,
                "select id,name,body_digest,state,revision,created_at"
                " from skills.skill where realm_id=%s and project_id=%s"
                " order by id limit %s",
                (self.realm_id, project_id),
            )
            failure_rows = bounded(
                cursor,
                "select id,evidence_digest,failure_category,observed_at"
                " from skills.failure_occurrence where realm_id=%s and project_id=%s"
                " order by id limit %s",
                (self.realm_id, project_id),
            )
            candidate_rows = bounded(
                cursor,
                "select c.id,c.logical_candidate_id,c.candidate_type,c.truth_class,"
                " c.classification,c.risk,c.state,c.is_current,c.superseded_by,"
                " c.candidate_digest,c.created_at"
                " from memory.compiler_candidate c join memory.compiler_run r"
                " on r.realm_id=c.realm_id and r.id=c.compiler_run_id"
                " where c.realm_id=%s and r.project_id=%s"
                " order by c.logical_candidate_id,c.created_at,c.id limit %s",
                (self.realm_id, project_id),
            )

        if (
            sum(
                len(rows)
                for rows in (
                    work_rows,
                    decision_rows,
                    memory_rows,
                    skill_rows,
                    failure_rows,
                    candidate_rows,
                )
            )
            > limit
        ):
            raise ValidationFailed("Obsidian combined DB snapshot bounded limiti asiyor")
        result: list[ObsidianProjectionRecord] = []
        work_records = {row.entity_id: row for row in _work_records(items, work_relations)}
        for item, _ in items:
            record = work_records[str(item.id)]
            result.append(
                ObsidianProjectionRecord(
                    record,
                    ObsidianNoteKind.WORK,
                    realm_slug,
                    project_id,
                    TruthClass.REPO_FACT,
                    DataClassification.INTERNAL,
                    item.updated_at,
                )
            )

        decision_ids_by_work: dict[str, list[str]] = {}
        for row in decision_rows:
            decision_ids_by_work.setdefault(str(row[1]), []).append(str(row[0]))
        for row in decision_rows:
            decision_id, work_id, revision = str(row[0]), str(row[1]), int(row[2])
            chain = decision_ids_by_work[work_id]
            index = chain.index(decision_id)
            relation = _relation(
                relation_id=f"decision-work:{decision_id}",
                direction="outgoing",
                kind="derived-from",
                other_id=work_id,
            )
            record = ProjectionRecord(
                "decision",
                decision_id,
                _one_line(row[3], "Decision"),
                "active" if index == len(chain) - 1 else "superseded",
                f"Chosen: {_one_line(row[4], 'Kaydedilmedi')}. "
                f"Rationale: {_one_line(row[5], 'Kaydedilmedi')}",
                (
                    ProjectionSourceRef(
                        "work-decision",
                        decision_id,
                        f"revision-{revision}",
                        str(row[6]),
                    ),
                ),
                (work_id,),
                (relation,),
            )
            result.append(
                ObsidianProjectionRecord(
                    record,
                    ObsidianNoteKind.DECISION,
                    realm_slug,
                    project_id,
                    TruthClass.USER_DECISION,
                    DataClassification.INTERNAL,
                    row[7],
                    supersedes=(() if index == 0 else (chain[index - 1],)),
                    superseded_by=(() if index == len(chain) - 1 else (chain[index + 1],)),
                )
            )

        memory_refs: dict[str, list[ProjectionRelationRef]] = {
            str(row[0]): [] for row in memory_rows
        }
        for relation_id, from_id, to_id, kind in memory_relations:
            source_id, target_id = str(from_id), str(to_id)
            relation_digest = digest(
                {
                    "id": str(relation_id),
                    "from_id": source_id,
                    "to_id": target_id,
                    "kind": str(kind),
                }
            )
            if source_id in memory_refs:
                memory_refs[source_id].append(
                    ProjectionRelationRef(
                        str(relation_id),
                        "outgoing",
                        str(kind),
                        target_id,
                        relation_digest,
                    )
                )
            if target_id in memory_refs:
                memory_refs[target_id].append(
                    ProjectionRelationRef(
                        str(relation_id),
                        "incoming",
                        str(kind),
                        source_id,
                        relation_digest,
                    )
                )
        supersedes_by_replacement = {
            str(row[6]): str(row[0]) for row in memory_rows if row[6] is not None
        }
        for row in memory_rows:
            memory_id, memory_class = str(row[0]), str(row[1])
            refs = _sorted_relations(memory_refs[memory_id])
            record = ProjectionRecord(
                "memory",
                memory_id,
                f"{memory_class.title()} memory {memory_id}",
                str(row[2]),
                f"Legacy unclassified content omitted; canonical digest: {row[7]}",
                (
                    ProjectionSourceRef(
                        "memory-record",
                        memory_id,
                        f"revision-{int(row[3])}",
                        str(row[7]),
                    ),
                ),
                tuple(sorted({item.other_entity_id for item in refs})),
                refs,
            )
            result.append(
                ObsidianProjectionRecord(
                    record,
                    ObsidianNoteKind.KNOWLEDGE,
                    realm_slug,
                    project_id,
                    TruthClass.UNKNOWN,
                    DataClassification.INTERNAL,
                    row[8],
                    memory_class=memory_class,
                    valid_from=row[4],
                    valid_until=row[5],
                    supersedes=(
                        ()
                        if memory_id not in supersedes_by_replacement
                        else (supersedes_by_replacement[memory_id],)
                    ),
                    superseded_by=(() if row[6] is None else (str(row[6]),)),
                )
            )

        for row in skill_rows:
            skill_id = str(row[0])
            record = ProjectionRecord(
                "skill",
                skill_id,
                _one_line(row[1], f"Skill {skill_id}"),
                str(row[3]),
                f"Skill body digest: {row[2]}",
                (ProjectionSourceRef("skill", skill_id, f"revision-{int(row[4])}", str(row[2])),),
            )
            result.append(
                ObsidianProjectionRecord(
                    record,
                    ObsidianNoteKind.SKILL,
                    realm_slug,
                    project_id,
                    TruthClass.UNKNOWN,
                    DataClassification.INTERNAL,
                    row[5],
                    memory_class="procedural",
                )
            )

        for row in failure_rows:
            failure_id = str(row[0])
            record = ProjectionRecord(
                "failure",
                failure_id,
                f"Failure {_one_line(row[2], failure_id)}",
                "observed",
                f"Occurrence metadata omitted; evidence digest: {row[1]}",
                (
                    ProjectionSourceRef(
                        "failure-occurrence",
                        failure_id,
                        "observation-1",
                        str(row[1]),
                    ),
                ),
            )
            result.append(
                ObsidianProjectionRecord(
                    record,
                    ObsidianNoteKind.FAILURE,
                    realm_slug,
                    project_id,
                    TruthClass.REPO_FACT,
                    DataClassification.INTERNAL,
                    row[3],
                    memory_class="failure",
                )
            )

        supersedes_by_candidate = {
            str(row[8]): str(row[0]) for row in candidate_rows if row[8] is not None
        }
        for row in candidate_rows:
            candidate_id = str(row[0])
            candidate_type = str(row[2])
            note_kind = (
                ObsidianNoteKind.SKILL
                if candidate_type == "skill_candidate"
                else ObsidianNoteKind.FAILURE
                if candidate_type == "failure_pattern"
                else ObsidianNoteKind.DECISION
                if candidate_type == "durable_decision"
                else ObsidianNoteKind.KNOWLEDGE
            )
            record = ProjectionRecord(
                "compiler-candidate",
                candidate_id,
                f"Candidate {_one_line(row[1], candidate_id)}",
                str(row[6]),
                f"Type: {candidate_type}; risk: {row[5]}; current: {bool(row[7])}",
                (
                    ProjectionSourceRef(
                        "compiler-candidate",
                        candidate_id,
                        "candidate-v1",
                        str(row[9]),
                    ),
                ),
            )
            result.append(
                ObsidianProjectionRecord(
                    record,
                    note_kind,
                    realm_slug,
                    project_id,
                    TruthClass(str(row[3])),
                    DataClassification(str(row[4])),
                    row[10],
                    supersedes=(
                        ()
                        if candidate_id not in supersedes_by_candidate
                        else (supersedes_by_candidate[candidate_id],)
                    ),
                    superseded_by=(() if row[8] is None else (str(row[8]),)),
                )
            )
        return tuple(sorted(result, key=lambda item: item.identity))
