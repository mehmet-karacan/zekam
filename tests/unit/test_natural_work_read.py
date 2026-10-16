"""Natural Work records stay exact, bounded and authority-free."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.natural_work_read import WorkReadRequest, parse_work_read, read_work_request
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.sqlite.operational_schema import bootstrap_v8
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


@pytest.mark.parametrize(
    ("question", "reference", "operation"),
    [
        ("GPU projesinde 17 numaralı işin geçmişini göster", "17", "history"),
        ("17 numarali gorevin bagimliliklarini listele", "17", "dependencies"),
        ("Work SKY-123 detayını göster", "SKY-123", "show"),
        ("work #12 checkpointlerini göster", "12", "checkpoints"),
        ("görev 12'nin durumunu göster", "12", "show"),
    ],
)
def test_explicit_natural_work_reads(question: str, reference: str, operation: str) -> None:
    assert parse_work_read(question) == WorkReadRequest(reference, operation)


@pytest.mark.parametrize(
    "question",
    [
        "SKY-123 defect geçmişini göster",
        "17 sınıfının geçmişini göster",
        "görev yönetimi nasıl çalışır",
        "GPU durum raporu",
        "work 17'yi tamamla",
        "work 12345678901234567890 detayını göster",
    ],
)
def test_other_intents_do_not_become_work_reads(question: str) -> None:
    assert parse_work_read(question) is None


@pytest.mark.parametrize(
    "question",
    [
        "work 12 ve work 13 detaylarını göster",
        "work 12 geçmişini ve bağımlılıklarını göster",
        "work 12 detayını göster sonra kapat",
    ],
)
def test_ambiguous_or_mixed_work_reads_clarify(question: str) -> None:
    assert parse_work_read(question).operation == "clarification"


def test_work_read_is_scoped_bounded_and_does_not_write(tmp_path: Path) -> None:
    database = tmp_path / "operational.db"
    bootstrap_v8(database)
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        a = uow.create_project(slug="aa", display_name="A")
        b = uow.create_project(slug="bb", display_name="B")
        work = uow.create_work(
            project_id=a.id, kind="task", title="A", state="ready", external_number="17"
        )
        uow.create_work(
            project_id=b.id, kind="task", title="B", state="ready", external_number="17"
        )
        for index in range(104):
            work = uow.transition_work(
                work_item_id=work.id,
                expected_revision=work.revision,
                to_state="blocked" if index % 2 == 0 else "ready",
                payload_digest=digest("payload"),
                event_digest=digest(index),
            )
        uow.commit()
    before = database.read_bytes()
    with store.unit_of_work() as uow:
        report = read_work_request(uow, WorkReadRequest("17", "history"), project_ref="aa")
        assert report["work"]["id"] == work.id
        assert len(report["events"]) == 100
        assert report["events"][-1]["revision"] == work.revision
        assert report["truncated"] is True
        assert report["grants_authority"] is False
        assert report["provider_calls"] == 0
        assert report["bound_jobs"] == []
        assert report["bound_jobs_truncated"] is False
        assert report["bound_jobs_scope"] == "exact-work"
        assert report["bound_job_limit"] == 50
        with pytest.raises(ValidationFailed):
            read_work_request(uow, WorkReadRequest(work.id, "show"), project_ref="bb")
        with pytest.raises(ValidationFailed):
            read_work_request(uow, WorkReadRequest("17", "close"), project_ref="aa")
    assert database.read_bytes() == before


def test_dependency_and_checkpoint_reads_use_canonical_store(tmp_path: Path) -> None:
    database = tmp_path / "operational.db"
    bootstrap_v8(database)
    with SQLiteOperationalStore(database).unit_of_work() as uow:
        project = uow.create_project(slug="demo", display_name="Demo")
        a, b = [
            uow.create_work(project_id=project.id, kind="task", title=title, state="ready")
            for title in ("A", "B")
        ]
        uow.change_work_dependency(
            work_item_id=a.id, depends_on_work_id=b.id, expected_revision=1, action="added"
        )
        report = read_work_request(uow, WorkReadRequest(a.id, "dependencies"), project_ref="demo")
        assert report["dependencies"][0]["depends_on_work_id"] == b.id
        assert report["work"]["dependencies"]["blocked"] is True
        checkpoints = read_work_request(
            uow, WorkReadRequest(a.id, "checkpoints"), project_ref="demo"
        )
        assert checkpoints["runs"] == []
        assert checkpoints["bound_jobs"] == []
        assert checkpoints["bound_jobs_scope"] == "exact-work"
