from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.application.work_graph import WorkGraphService
from zekam.domain.errors import PolicyViolation
from zekam.domain.realm import Realm
from zekam.domain.work import AcceptanceCriterion, WorkItem, WorkState, WorkType

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.UTC)


class Cursor:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args):  # type: ignore[no-untyped-def]
        return None

    def execute(self, statement: str, parameters: object) -> None:
        assert "from work.work_item" in statement

    def fetchone(self):  # type: ignore[no-untyped-def]
        return self.row


class Connection:
    def __init__(self, item: WorkItem) -> None:
        self.row = (
            item.id,
            item.realm_id,
            item.project_id,
            item.external_number,
            item.type.value,
            item.state.value,
            item.title,
            item.summary,
            item.revision,
            [entry.as_dict() for entry in item.acceptance_criteria],
            [entry.as_dict() for entry in item.acceptance_evidence],
            item.created_at,
            item.updated_at,
        )

    def cursor(self) -> Cursor:
        return Cursor(self.row)


def test_direct_completed_transition_is_fail_closed() -> None:
    realm = Realm.create(slug="unit-realm", display_name="Unit realm", now=NOW)
    item = WorkItem(
        id=uuid4(),
        realm_id=realm.id,
        project_id=uuid4(),
        type=WorkType.TASK,
        state=WorkState.VERIFICATION,
        title="Verified work",
        acceptance_criteria=(AcceptanceCriterion("verified", verified=True),),
        created_at=NOW,
        updated_at=NOW,
    )

    with pytest.raises(PolicyViolation, match="ProjectionAwareClosureService"):
        WorkGraphService(Connection(item), realm).transition(
            item.id,
            WorkState.COMPLETED,
            now=NOW,
        )
