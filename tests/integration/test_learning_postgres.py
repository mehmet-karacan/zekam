"""P14 ogrenme ve skill PostgreSQL kabul testleri."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest

from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.identifiers import new_uuid7
from zekam.domain.work import WorkType

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

NOW = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)


def _project(connection: Any, realm: Any, tmp_path: Path) -> Any:
    source = tmp_path / "kaynak"
    source.mkdir()
    return ProjectIntegrationService(connection, realm).register(source_path=source)


def _insert_occurrence(connection: Any, realm: Any, evidence: str, run: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into skills.failure_occurrence"
            " (id, realm_id, occurrence_key, evidence_digest, run_ref, failure_category,"
            "  observed_at)"
            " values (%s, %s, 'migration-drift', %s, %s, 'adapter', %s)",
            (new_uuid7(now=NOW), realm.id, digest(evidence), run, NOW),
        )


def _insert_skill(connection: Any, realm: Any, **overrides: Any) -> UUID:
    values: dict[str, Any] = {
        "name": "drift-kontrolu",
        "body_digest": digest("govde"),
        "state": "candidate",
        "revision": 1,
        "author_ref": "agent-a",
        "evaluation_digest": None,
        "approved_by": None,
        "rollback_plan": None,
    }
    values.update(overrides)
    skill_id = new_uuid7(now=NOW)
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into skills.skill"
            " (id, realm_id, name, body_digest, state, revision, author_ref,"
            "  evaluation_digest, approved_by, rollback_plan, created_at)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                skill_id,
                realm.id,
                values["name"],
                values["body_digest"],
                values["state"],
                values["revision"],
                values["author_ref"],
                values["evaluation_digest"],
                values["approved_by"],
                values["rollback_plan"],
                NOW,
            ),
        )
    return skill_id


def _insert_evaluation(
    connection: Any, realm: Any, skill_id: UUID, *, successes: int, baseline: float
) -> str:
    evaluation_digest = digest({"skill": str(skill_id), "successes": successes})
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into skills.skill_evaluation"
            " (id, realm_id, skill_id, fixtures, trials, successes, baseline_success_rate,"
            "  evaluator_ref, verifier_ref, evaluation_digest, created_at)"
            " values (%s, %s, %s, %s::jsonb, 10, %s, %s, 'evaluator-a', 'verifier-b', %s, %s)",
            (
                new_uuid7(now=NOW),
                realm.id,
                skill_id,
                '[{"fixture_id": "f1"}]',
                successes,
                baseline,
                evaluation_digest,
                NOW,
            ),
        )
    return evaluation_digest


def test_ayni_kanit_iki_kez_kaydedilemez(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    _insert_occurrence(connection, realm, "ayni", "run-1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_occurrence(connection, realm, "ayni", "run-2")
    connection.rollback()


def test_farkli_kanit_ayri_kaydedilir(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    _insert_occurrence(connection, realm, "e1", "run-1")
    _insert_occurrence(connection, realm, "e2", "run-2")
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from skills.failure_occurrence where occurrence_key = %s",
            ("migration-drift",),
        )
        assert cursor.fetchone()[0] == 2


def test_kok_nedensiz_ders_onaylanamaz(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into skills.learning_candidate"
            " (id, realm_id, occurrence_key, target, proposal, author_ref, approved,"
            "  verifier_ref, decision_reason, created_at)"
            " values (%s, %s, 'k', 'test', 'oneri', 'agent-a', true, 'verifier-b',"
            "  'onay', %s)",
            (new_uuid7(now=NOW), realm.id, NOW),
        )
    connection.rollback()


def test_ders_verifieri_yazarla_ayni_olamaz(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into skills.learning_candidate"
            " (id, realm_id, occurrence_key, target, proposal, author_ref, verifier_ref,"
            "  created_at)"
            " values (%s, %s, 'k', 'test', 'oneri', 'agent-a', 'agent-a', %s)",
            (new_uuid7(now=NOW), realm.id, NOW),
        )
    connection.rollback()


def test_kok_neden_ucusu_eksik_kaydedilemez(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into skills.learning_candidate"
            " (id, realm_id, occurrence_key, target, proposal, author_ref, root_cause,"
            "  created_at)"
            " values (%s, %s, 'k', 'test', 'oneri', 'agent-a', 'kok neden', %s)",
            (new_uuid7(now=NOW), realm.id, NOW),
        )
    connection.rollback()


def test_skill_kendi_kendini_aktive_edemez(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into skills.skill"
            " (id, realm_id, name, body_digest, state, revision, author_ref,"
            "  self_promoted, created_at)"
            " values (%s, %s, 'x', %s, 'candidate', 1, 'agent-a', true, %s)",
            (new_uuid7(now=NOW), realm.id, digest("g"), NOW),
        )
    connection.rollback()


def test_onaysiz_skill_aktif_olamaz(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_skill(connection, realm, state="active", evaluation_digest=digest("d"))
    connection.rollback()


def test_onay_yazarla_ayni_kimlik_olamaz(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_skill(
            connection,
            realm,
            state="active",
            evaluation_digest=digest("d"),
            approved_by="agent-a",
            rollback_plan="geri al",
        )
    connection.rollback()


def test_rollback_plansiz_skill_aktif_olamaz(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_skill(
            connection,
            realm,
            state="active",
            evaluation_digest=digest("d"),
            approved_by="onaylayan-b",
            rollback_plan="   ",
        )
    connection.rollback()


def test_baseline_gecmeyen_skill_aktive_edilemez(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    skill_id = _insert_skill(connection, realm)
    weak = _insert_evaluation(connection, realm, skill_id, successes=3, baseline=0.5)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "update skills.skill set state = 'active', evaluation_digest = %s,"
            " approved_by = 'onaylayan-b', rollback_plan = 'geri al' where id = %s",
            (weak, skill_id),
        )
    connection.rollback()


def test_iyilesen_olcumle_skill_aktive_edilir(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    skill_id = _insert_skill(connection, realm)
    strong = _insert_evaluation(connection, realm, skill_id, successes=9, baseline=0.5)
    with connection.cursor() as cursor:
        cursor.execute(
            "update skills.skill set state = 'active', evaluation_digest = %s,"
            " approved_by = 'onaylayan-b', rollback_plan = 'registry disina al' where id = %s",
            (strong, skill_id),
        )
        cursor.execute("select state, approved_by from skills.skill where id = %s", (skill_id,))
        state, approved_by = cursor.fetchone()
    assert state == "active"
    assert approved_by == "onaylayan-b"


def test_ayni_govdeli_skill_iki_kez_kaydedilemez(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    _insert_skill(connection, realm)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_skill(connection, realm, name="baska-ad")
    connection.rollback()


def test_yetersiz_denemeli_olcum_reddedilir(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    skill_id = _insert_skill(connection, realm)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into skills.skill_evaluation"
            " (id, realm_id, skill_id, fixtures, trials, successes, baseline_success_rate,"
            "  evaluator_ref, verifier_ref, evaluation_digest, created_at)"
            " values (%s, %s, %s, %s::jsonb, 3, 3, 0.5, 'e', 'v', %s, %s)",
            (
                new_uuid7(now=NOW),
                realm.id,
                skill_id,
                '[{"fixture_id": "f1"}]',
                digest("az"),
                NOW,
            ),
        )
    connection.rollback()


def test_ayni_kimlikli_degerlendirme_reddedilir(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    skill_id = _insert_skill(connection, realm)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into skills.skill_evaluation"
            " (id, realm_id, skill_id, fixtures, trials, successes, baseline_success_rate,"
            "  evaluator_ref, verifier_ref, evaluation_digest, created_at)"
            " values (%s, %s, %s, %s::jsonb, 10, 9, 0.5, 'ayni', 'ayni', %s, %s)",
            (
                new_uuid7(now=NOW),
                realm.id,
                skill_id,
                '[{"fixture_id": "f1"}]',
                digest("ayni"),
                NOW,
            ),
        )
    connection.rollback()


def test_dongu_iterasyonu_kalicilasir(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    project = _project(connection, realm, tmp_path)
    item = WorkGraphService(connection, realm).create_item(
        project_id=project.id, type=WorkType.TASK, title="Olculu dongu"
    )
    with connection.cursor() as cursor:
        for iteration, score in ((1, 0.3), (2, 0.6)):
            cursor.execute(
                "insert into skills.loop_iteration"
                " (id, realm_id, work_item_id, iteration, score, cost_units, verified, created_at)"
                " values (%s, %s, %s, %s, %s, 10, true, %s)",
                (new_uuid7(now=NOW), realm.id, item.id, iteration, score, NOW),
            )
        cursor.execute(
            "select count(*) from skills.loop_iteration where work_item_id = %s", (item.id,)
        )
        assert cursor.fetchone()[0] == 2

    with pytest.raises(psycopg.errors.UniqueViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into skills.loop_iteration"
            " (id, realm_id, work_item_id, iteration, score, cost_units, verified, created_at)"
            " values (%s, %s, %s, 1, 0.9, 5, true, %s)",
            (new_uuid7(now=NOW), realm.id, item.id, NOW),
        )
    connection.rollback()


def test_gozlem_ve_olcum_degistirilemez(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    _insert_occurrence(connection, realm, "e1", "run-1")
    for table in ("skills.failure_occurrence", "skills.skill_evaluation", "skills.loop_iteration"):
        for statement in (f"update {table} set realm_id = realm_id", f"delete from {table}"):
            with (
                pytest.raises(Exception, match=r"append-only|permission denied"),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement)
            connection.rollback()
