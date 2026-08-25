"""Read-only, repeatable-snapshot source for Markdown project projections."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.markdown_projection import (
    ProjectionRecord,
    ProjectionRelationRef,
    ProjectionSourceRef,
)
from zekam.domain.work import (
    AcceptanceCriterion,
    EvidenceRef,
    WorkItem,
    WorkState,
    WorkType,
)

MAX_PROJECTION_RECORDS = 1000
MAX_PROJECTION_RELATIONS = 5000


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
        relation_refs: dict[UUID, list[ProjectionRelationRef]] = {
            item_id: [] for item_id in item_ids
        }
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
                relation_refs=tuple(
                    sorted(
                        relation_refs[item.id],
                        key=lambda entry: tuple(entry.as_dict().values()),
                    )
                ),
            )
            for item, stored_digest in items
        )
