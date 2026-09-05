"""P15 scheduler PostgreSQL kabul testleri."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from uuid import UUID

import psycopg
import pytest

from zekam.application.worker import SchedulerGateway
from zekam.domain.canonical import digest
from zekam.domain.identifiers import new_uuid7
from zekam.domain.scheduler import (
    REQUIRED_JOB_INTERVALS,
    REQUIRED_JOBS,
    REQUIRED_REPORT_SECTIONS,
    missing_required_jobs,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

NOW = dt.datetime(2026, 8, 21, 6, 0, tzinfo=dt.UTC)


def _definition(connection: Any, realm: Any, name: str = "daily-report", **overrides: Any) -> UUID:
    values: dict[str, Any] = {"interval_spec": "1d", "state": "active", "overlap": "skip"}
    values.update(overrides)
    definition_id = new_uuid7(now=NOW)
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.job_definition"
            " (id, realm_id, job_name, interval_spec, state, overlap, created_at)"
            " values (%s, %s, %s, %s, %s, %s, %s)",
            (
                definition_id,
                realm.id,
                name,
                values["interval_spec"],
                values["state"],
                values["overlap"],
                NOW,
            ),
        )
    return definition_id


def _run(
    connection: Any, realm: Any, definition_id: UUID, key: str, *, state: str = "running"
) -> UUID:
    run_id = new_uuid7(now=NOW)
    finished = None if state in {"pending", "running"} else NOW
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.job_run"
            " (id, realm_id, definition_id, idempotency_key, scheduled_for, started_at,"
            "  finished_at, state)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (run_id, realm.id, definition_id, key, NOW, NOW, finished, state),
        )
    return run_id


def test_gecersiz_aralik_veritabaninda_reddedilir(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    with pytest.raises(psycopg.errors.CheckViolation):
        _definition(connection, realm, interval_spec="her gun")
    connection.rollback()


def test_ayni_is_adi_iki_kez_tanimlanmaz(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    _definition(connection, realm)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _definition(connection, realm)
    connection.rollback()


def test_ayni_tetikleme_iki_kez_kaydedilemez(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    definition_id = _definition(connection, realm)
    key = digest("tetikleme-1")
    _run(connection, realm, definition_id, key, state="succeeded")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _run(connection, realm, definition_id, key, state="succeeded")
    connection.rollback()


def test_ayni_anda_tek_aktif_calisma(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    definition_id = _definition(connection, realm)
    _run(connection, realm, definition_id, digest("k1"), state="running")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _run(connection, realm, definition_id, digest("k2"), state="running")
    connection.rollback()


def test_terminal_calisma_bitis_zamani_ister(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    definition_id = _definition(connection, realm)
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.job_run"
            " (id, realm_id, definition_id, idempotency_key, scheduled_for, state)"
            " values (%s, %s, %s, %s, %s, 'succeeded')",
            (new_uuid7(now=NOW), realm.id, definition_id, digest("k3"), NOW),
        )
    connection.rollback()


def test_calisma_bitince_yeni_calisma_baslayabilir(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    definition_id = _definition(connection, realm)
    run_id = _run(connection, realm, definition_id, digest("k1"), state="running")
    with connection.cursor() as cursor:
        cursor.execute(
            "update ops.job_run set state = 'succeeded', finished_at = %s where id = %s",
            (NOW, run_id),
        )
    _run(connection, realm, definition_id, digest("k2"), state="running")
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from ops.job_run where definition_id = %s", (definition_id,)
        )
        assert cursor.fetchone()[0] == 2


def test_durum_yeniden_baslatma_sonrasi_korunur(realm_session: tuple[Any, Any]) -> None:
    """Tanim ve calisma durumu kalicidir; surec yeniden basladiginda okunur."""

    realm, connection = realm_session
    _definition(connection, realm, "model-health", state="paused")
    _definition(connection, realm, "memory-hygiene", state="active")
    with connection.cursor() as cursor:
        cursor.execute(
            "select job_name from ops.job_definition where state = 'active' order by job_name"
        )
        assert [row[0] for row in cursor.fetchall()] == ["memory-hygiene"]


def test_ayni_icerik_ikinci_kez_kaydedilemez(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    content = digest("makale")
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.incoming_document"
            " (id, realm_id, relative_path, content_digest, byte_size, decision, target,"
            "  detail, observed_at)"
            " values (%s, %s, 'makale.pdf', %s, 1024, 'accepted', 'knowledge', 'tek hedef', %s)",
            (new_uuid7(now=NOW), realm.id, content, NOW),
        )
    with pytest.raises(psycopg.errors.UniqueViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.incoming_document"
            " (id, realm_id, relative_path, content_digest, byte_size, decision, target,"
            "  detail, observed_at)"
            " values (%s, %s, 'kopya.pdf', %s, 1024, 'accepted', 'knowledge', 'kopya', %s)",
            (new_uuid7(now=NOW), realm.id, content, NOW),
        )
    connection.rollback()


def test_gelen_belge_yolu_portable_olmali(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    for path in ("/etc/passwd", "../disari.pdf", "C:\\gizli.pdf"):
        with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
            cursor.execute(
                "insert into ops.incoming_document"
                " (id, realm_id, relative_path, content_digest, byte_size, decision,"
                "  detail, observed_at)"
                " values (%s, %s, %s, %s, 10, 'rejected', 'test', %s)",
                (new_uuid7(now=NOW), realm.id, path, digest(path), NOW),
            )
        connection.rollback()


def test_kabul_edilen_belge_hedef_ister(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.incoming_document"
            " (id, realm_id, relative_path, content_digest, byte_size, decision,"
            "  detail, observed_at)"
            " values (%s, %s, 'x.pdf', %s, 10, 'accepted', 'hedefsiz', %s)",
            (new_uuid7(now=NOW), realm.id, digest("x"), NOW),
        )
    connection.rollback()


def _sections(exclude: str | None = None) -> str:
    payload = {
        name: {"title": name, "lines": ["ornek"]}
        for name in REQUIRED_REPORT_SECTIONS
        if name != exclude
    }
    return json.dumps(payload)


def test_eksik_bolumlu_rapor_kaydedilemez(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.daily_report"
            " (id, realm_id, scope, report_date, sections, report_digest, generated_at)"
            " values (%s, %s, 'genel', %s, %s::jsonb, %s, %s)",
            (
                new_uuid7(now=NOW),
                realm.id,
                NOW.date(),
                _sections(exclude="onerilen-next-actions"),
                digest("eksik"),
                NOW,
            ),
        )
    connection.rollback()


def test_tam_rapor_kaydedilir_ve_authority_tasimaz(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.daily_report"
            " (id, realm_id, scope, report_date, sections, report_digest, generated_at)"
            " values (%s, %s, 'genel', %s, %s::jsonb, %s, %s) returning grants_authority",
            (new_uuid7(now=NOW), realm.id, NOW.date(), _sections(), digest("tam"), NOW),
        )
        assert cursor.fetchone()[0] is False

    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.daily_report"
            " (id, realm_id, scope, report_date, sections, report_digest,"
            "  grants_authority, generated_at)"
            " values (%s, %s, 'proje', %s, %s::jsonb, %s, true, %s)",
            (new_uuid7(now=NOW), realm.id, NOW.date(), _sections(), digest("yetki"), NOW),
        )
    connection.rollback()


def test_ayni_gun_ayni_kapsam_iki_rapor_uretmez(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.daily_report"
            " (id, realm_id, scope, report_date, sections, report_digest, generated_at)"
            " values (%s, %s, 'genel', %s, %s::jsonb, %s, %s)",
            (new_uuid7(now=NOW), realm.id, NOW.date(), _sections(), digest("ilk"), NOW),
        )
    with pytest.raises(psycopg.errors.UniqueViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.daily_report"
            " (id, realm_id, scope, report_date, sections, report_digest, generated_at)"
            " values (%s, %s, 'genel', %s, %s::jsonb, %s, %s)",
            (new_uuid7(now=NOW), realm.id, NOW.date(), _sections(), digest("ikinci"), NOW),
        )
    connection.rollback()


def test_olay_bir_sonraki_adimi_bildirmeli(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    with pytest.raises(psycopg.errors.CheckViolation), connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.scheduler_incident"
            " (id, realm_id, job_name, kind, detail, next_safe_action, created_at)"
            " values (%s, %s, 'x', 'failure', 'd', '   ', %s)",
            (new_uuid7(now=NOW), realm.id, NOW),
        )
    connection.rollback()


def test_belge_rapor_ve_olay_degistirilemez(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.scheduler_incident"
            " (id, realm_id, job_name, kind, detail, next_safe_action, created_at)"
            " values (%s, %s, 'x', 'misfire', 'd', 'telafi calistir', %s)",
            (new_uuid7(now=NOW), realm.id, NOW),
        )
    for table in ("ops.incoming_document", "ops.daily_report", "ops.scheduler_incident"):
        for statement in (f"update {table} set realm_id = realm_id", f"delete from {table}"):
            with (
                pytest.raises(Exception, match=r"append-only|permission denied"),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement)
            connection.rollback()


def test_zorunlu_bakim_isleri_tanimlanir_ve_idempotenttir(
    realm_session: tuple[Any, Any],
) -> None:
    """`scheduler init` kanonik listeyi tanimlar; ikinci calistirma yazmaz."""

    realm, connection = realm_session
    gateway = SchedulerGateway(connection, realm.id)

    created = gateway.ensure_required_definitions(now=NOW)
    assert set(created) == set(REQUIRED_JOBS)

    with connection.cursor() as cursor:
        cursor.execute("select job_name, interval_spec from ops.job_definition")
        rows = {str(name): str(interval) for name, interval in cursor.fetchall()}
    assert rows == dict(REQUIRED_JOB_INTERVALS)
    assert missing_required_jobs(tuple(rows)) == ()

    assert gateway.ensure_required_definitions(now=NOW) == ()


def test_var_olan_tanim_ezilmez(realm_session: tuple[Any, Any]) -> None:
    """Operator araligi degistirmisse init onu geri almaz."""

    realm, connection = realm_session
    _definition(connection, realm, "model-health", interval_spec="30m")

    created = SchedulerGateway(connection, realm.id).ensure_required_definitions(now=NOW)
    assert "model-health" not in created

    with connection.cursor() as cursor:
        cursor.execute(
            "select interval_spec from ops.job_definition where job_name = %s", ("model-health",)
        )
        assert str(cursor.fetchone()[0]) == "30m"
