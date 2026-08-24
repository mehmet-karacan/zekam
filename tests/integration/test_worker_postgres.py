"""Worker sureci PostgreSQL kabul testleri.

Gercek durable queue ve gercek scheduler tablolari kullanilir. Worker sohbet
surecinden bagimsizdir: tanim veritabanindan okunur, tetikleme oraya yazilir.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.worker import (
    SchedulerGateway,
    ShutdownSignal,
    WorkerSettings,
    build_worker,
    noop_handler,
    resolve_handlers,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.runtime import AttemptOutcome, Job, JobKind

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

NOW = dt.datetime(2026, 8, 21, 6, 0, tzinfo=dt.UTC)


def _settings(**kwargs: Any) -> WorkerSettings:
    defaults: dict[str, Any] = {
        "worker_label": "worker-test",
        "capabilities": ("sandbox.write",),
        "poll_seconds": 0.01,
        "max_iterations": 1,
    }
    defaults.update(kwargs)
    return WorkerSettings(**defaults)


def _project(connection: Any, realm: Any, tmp_path: Path) -> Any:
    source = tmp_path / "kaynak"
    source.mkdir()
    return ProjectIntegrationService(connection, realm).register(source_path=source)


def _job(project_id: UUID, realm: Any, *, key: str = "is-1") -> Job:
    return Job.create(
        realm_id=realm.id,
        project_id=project_id,
        kind=JobKind.READ_ONLY,
        idempotency_key=digest(key),
        required_capabilities=("sandbox.write",),
        now=NOW,
    )


def _definition(
    connection: Any,
    realm: Any,
    name: str,
    interval: str = "1h",
    *,
    misfire: str = "run-once",
    last_run_at: dt.datetime | None = None,
) -> UUID:
    """Tanimi dogrudan istenen politikayla olusturur.

    Uygulama rolu `misfire` sutununu guncelleyemez (en az yetki); bu yuzden
    politika insert aninda verilir.
    """

    definition_id = new_uuid7(now=NOW)
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into ops.job_definition"
            " (id, realm_id, job_name, interval_spec, state, misfire, last_run_at, created_at)"
            " values (%s, %s, %s, %s, 'active', %s, %s, %s)",
            (definition_id, realm.id, name, interval, misfire, last_run_at, NOW),
        )
    return definition_id


def test_worker_kuyruktan_is_alir_ve_tamamlar(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    project = _project(connection, realm, tmp_path)
    worker = build_worker(
        connection,
        realm.id,
        settings=_settings(),
        handlers={str(JobKind.READ_ONLY): noop_handler},
        with_scheduler=False,
    )
    worker.host.jobs.enqueue(_job(project.id, realm))

    result = worker.tick(now=NOW)
    assert result.accepted_work is True
    assert result.outcome is AttemptOutcome.SUCCEEDED

    with connection.cursor() as cursor:
        cursor.execute("select state from runtime.job where id = %s", (result.job_id,))
        assert cursor.fetchone()[0] == "completed"


def test_bos_kuyrukta_is_alinmaz(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    worker = build_worker(
        connection,
        realm.id,
        settings=_settings(),
        handlers={},
        with_scheduler=False,
        allow_empty_handlers=True,
    )
    result = worker.tick(now=NOW)
    assert result.accepted_work is False
    assert result.skipped_reason == "kuyruk bos"


def test_isleyicisi_olmayan_worker_baslamaz(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    """Sessiz basari uretilmez; isleyici yoksa is failed olur."""

    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    with pytest.raises(PolicyViolation, match="explicit handler"):
        build_worker(
            connection, realm.id, settings=_settings(), handlers={}, with_scheduler=False
        )


def test_isleyici_hatasi_terminal_duruma_cevrilir(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    project = _project(connection, realm, tmp_path)

    def broken(work: Any) -> str:
        raise RuntimeError("isleyici cokti")

    worker = build_worker(
        connection,
        realm.id,
        settings=_settings(),
        handlers={str(JobKind.READ_ONLY): broken},
        with_scheduler=False,
    )
    worker.host.jobs.enqueue(_job(project.id, realm))
    result = worker.tick(now=NOW)
    assert result.outcome is AttemptOutcome.FAILED

    with connection.cursor() as cursor:
        cursor.execute("select state from runtime.job where id = %s", (result.job_id,))
        assert cursor.fetchone()[0] == "failed"


def test_iptal_edilen_is_terminal_sonuc_yayimlamaz(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    project = _project(connection, realm, tmp_path)
    worker = build_worker(
        connection,
        realm.id,
        settings=_settings(),
        handlers={str(JobKind.READ_ONLY): noop_handler},
        with_scheduler=False,
    )
    job = _job(project.id, realm)
    worker.host.jobs.enqueue(job)
    worker.cancel(job.id, now=NOW)

    result = worker.tick(now=NOW)
    assert result.outcome is AttemptOutcome.ABANDONED
    request = worker.cancellations[job.id]
    with pytest.raises(PolicyViolation):
        request.assert_no_result_after_cancel(result_published=True)


def test_kuyruk_dolunca_is_alinmaz(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    worker = build_worker(
        connection,
        realm.id,
        settings=_settings(max_queue_depth=5),
        handlers={},
        with_scheduler=False,
        allow_empty_handlers=True,
    )
    result = worker.tick(now=NOW, queue_depth=5)
    assert result.accepted_work is False
    assert "kuyruk derinligi" in (result.skipped_reason or "")


def test_zamanlanmis_is_tetiklenir_ve_kalicilasir(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    definition_id = _definition(connection, realm, "model-health")
    worker = build_worker(
        connection,
        realm.id,
        settings=_settings(),
        handlers={},
        with_scheduler=True,
        allow_empty_handlers=True,
    )

    result = worker.tick(now=NOW)
    assert "model-health" in result.triggered_jobs

    with connection.cursor() as cursor:
        cursor.execute(
            "select state, missed_count from ops.job_run where definition_id = %s",
            (definition_id,),
        )
        state, missed = cursor.fetchone()
        assert state == "succeeded"
        assert missed == 0
        cursor.execute("select last_run_at from ops.job_definition where id = %s", (definition_id,))
        assert cursor.fetchone()[0] is not None


def test_ayni_tetikleme_ikinci_dongude_tekrarlanmaz(realm_session: tuple[Any, Any]) -> None:
    """Idempotency: ayni pencere icin ikinci calisma kaydi olusmaz."""

    realm, connection = realm_session
    definition_id = _definition(connection, realm, "memory-hygiene", interval="1h")
    worker = build_worker(
        connection,
        realm.id,
        settings=_settings(),
        handlers={},
        with_scheduler=True,
        allow_empty_handlers=True,
    )

    first = worker.tick(now=NOW)
    assert "memory-hygiene" in first.triggered_jobs

    # Ayni an: zamani gelmedigi icin yeni tetikleme yok.
    second = worker.tick(now=NOW)
    assert second.triggered_jobs == ()

    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from ops.job_run where definition_id = %s", (definition_id,)
        )
        assert cursor.fetchone()[0] == 1


def test_duraklatilmis_tanim_tetiklenmez(realm_session: tuple[Any, Any]) -> None:
    realm, connection = realm_session
    definition_id = _definition(connection, realm, "night-research")
    with connection.cursor() as cursor:
        cursor.execute(
            "update ops.job_definition set state = 'paused' where id = %s", (definition_id,)
        )
    worker = build_worker(
        connection,
        realm.id,
        settings=_settings(),
        handlers={},
        with_scheduler=True,
        allow_empty_handlers=True,
    )
    assert worker.tick(now=NOW).triggered_jobs == ()


def test_kacirilan_calisma_olay_olarak_kaydedilir(realm_session: tuple[Any, Any]) -> None:
    """skip-visible politikasinda kacirilan calisma sessizce yutulmaz."""

    realm, connection = realm_session
    _definition(
        connection,
        realm,
        "backup-verify",
        interval="1h",
        misfire="skip-visible",
        last_run_at=NOW - dt.timedelta(hours=6),
    )
    worker = build_worker(
        connection,
        realm.id,
        settings=_settings(),
        handlers={},
        with_scheduler=True,
        allow_empty_handlers=True,
    )
    result = worker.tick(now=NOW)
    assert result.triggered_jobs == ()

    with connection.cursor() as cursor:
        cursor.execute(
            "select kind, detail, next_safe_action from ops.scheduler_incident"
            " where job_name = 'backup-verify'"
        )
        kind, detail, action = cursor.fetchone()
    assert kind == "misfire"
    assert "kacirildi" in detail
    assert action


def test_gateway_durumu_veritabanindan_okur(realm_session: tuple[Any, Any]) -> None:
    """Worker yeniden baslatildiginda tanim durumu kaybolmaz."""

    realm, connection = realm_session
    _definition(connection, realm, "daily-report")
    _definition(connection, realm, "recovery-scan")
    with connection.cursor() as cursor:
        cursor.execute(
            "update ops.job_definition set state = 'cancelled' where job_name = %s",
            ("recovery-scan",),
        )

    gateway = SchedulerGateway(connection, realm.id)
    names = {item[1].job_name: item[1].is_runnable for item in gateway.definitions()}
    assert names["daily-report"] is True
    assert names["recovery-scan"] is False


def test_zarif_kapanma_dongusu_durdurur(realm_session: tuple[Any, Any], tmp_path: Path) -> None:
    realm, connection = realm_session
    _project(connection, realm, tmp_path)
    worker = build_worker(
        connection,
        realm.id,
        settings=_settings(max_iterations=None),
        handlers=resolve_handlers(
            [str(JobKind.READ_ONLY)], registry={str(JobKind.READ_ONLY): noop_handler}
        ),
        with_scheduler=False,
    )
    worker.shutdown = ShutdownSignal()
    worker.shutdown.request("test")
    assert worker.run() == ()


def test_worker_etiketi_ve_yetenek_zorunlu() -> None:
    with pytest.raises(PolicyViolation):
        WorkerSettings(worker_label="  ", capabilities=("x",))
    with pytest.raises(PolicyViolation):
        WorkerSettings(worker_label="w", capabilities=())
    with pytest.raises(PolicyViolation):
        WorkerSettings(worker_label="w", capabilities=("x",), poll_seconds=0)
