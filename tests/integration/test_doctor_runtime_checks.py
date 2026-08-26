"""ZEKAM-DOD-002 genisletilmis doctor kontrolleri.

Kontroller gercek PostgreSQL uzerinde calisir ve **salt okunurdur**: kuyruktan is
almaz, model cagirmaz, policy degistirmez.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import pytest

from zekam.application.composition import build_doctor_checks
from zekam.application.config import DatabaseSettings
from zekam.application.diagnostics import CheckStatus
from zekam.application.worker import SchedulerGateway
from zekam.domain.scheduler import REQUIRED_JOBS, missing_required_jobs
from zekam.infrastructure.doctor import runtime_checks

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

NOW = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)


def _unreachable() -> DatabaseSettings:
    return DatabaseSettings(host="127.0.0.1", port=1, name="yok", user="yok")


def _count(settings: DatabaseSettings, query: str) -> int:
    from zekam.infrastructure.postgres.connection import connect

    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(query)
        return int(cursor.fetchone()[0])


def test_kuyruk_kontrolu_derinlik_ve_recovery_raporlar(
    migrated_database: DatabaseSettings,
) -> None:
    """Beklenti veritabaninin gercek durumundan turetilir; test sirasina bagli degildir."""

    pending = _count(
        migrated_database,
        "select count(*) from runtime.job where state in ('ready', 'running')",
    )
    raw_recovery = _count(
        migrated_database,
        "select count(*) from runtime.job where state = 'recovery-required'",
    )
    result = runtime_checks.QueueCheck(settings=migrated_database).run()
    assert result.evidence["pending"] == pending
    resolved = int(result.evidence["recovery_resolved_by_continuation"])
    recovery = raw_recovery - resolved
    assert result.evidence["recovery"] == recovery
    breakdown = result.evidence["raw_recovery_breakdown"]
    assert set(breakdown) == {
        "no_claim",
        "claim_without_receipt",
        "failed_receipt",
        "completed_receipt",
    }
    assert sum(int(value) for value in breakdown.values()) == raw_recovery
    assert result.evidence["cross_realm"] is True
    expected = CheckStatus.DEGRADED if recovery else CheckStatus.PASSED
    assert result.status is expected


def test_model_kontrolu_envanter_durumunu_bildirir(
    migrated_database: DatabaseSettings,
) -> None:
    imported = _count(migrated_database, "select count(*) from models.model_inventory")
    result = runtime_checks.ModelInventoryCheck(settings=migrated_database).run()
    assert result.evidence["imported"] == imported
    if imported == 0:
        assert result.status is CheckStatus.DEGRADED
        assert any(item.code == "runtime.models-empty" for item in result.findings)
    assert all(item.next_action for item in result.findings)


def test_policy_kontrolu_policy_durumunu_bildirir(
    migrated_database: DatabaseSettings,
) -> None:
    policies = _count(migrated_database, "select count(*) from security.policy")
    result = runtime_checks.PolicyCheck(settings=migrated_database).run()
    assert result.evidence["policy_versions"] == policies
    if policies == 0:
        assert result.status is CheckStatus.DEGRADED
        assert any(item.code == "runtime.policy-missing" for item in result.findings)
    else:
        assert result.status is CheckStatus.PASSED


def test_scheduler_kontrolu_eksikten_tama_geciyor(
    realm_session: tuple[Any, Any], migrated_database: DatabaseSettings
) -> None:
    """Gecisi tek testte dogrular: once eksik, `scheduler init` sonrasi tam.

    Tanimlar ham SQL ile degil urunun kendi yolundan eklenir; boylece
    `zekam scheduler init --uygula` komutunun doctor'i gercekten healthy
    yaptigi dogrulanir (ZEKAM-DEF-004).
    """

    realm, connection = realm_session
    with connection.cursor() as cursor:
        cursor.execute("select job_name from ops.job_definition where realm_id = %s", (realm.id,))
        onceki = tuple(str(row[0]) for row in cursor.fetchall())
    # Taze realm: kanonik islerin hicbiri tanimli degil.
    assert missing_required_jobs(onceki) == REQUIRED_JOBS

    created = SchedulerGateway(connection, realm.id).ensure_required_definitions(now=NOW)
    connection.commit()
    assert set(created) == set(REQUIRED_JOBS)

    after = runtime_checks.SchedulerCheck(settings=migrated_database).run()
    assert after.status is CheckStatus.PASSED
    assert after.evidence["missing"] == []


def test_istemci_kontrolu_yapilandirma_yoksa_atlanir() -> None:
    result = runtime_checks.ClientsCheck().run()
    assert result.status is CheckStatus.SKIPPED
    assert result.evidence["configured"] == 0


def test_istemci_kontrolu_eksik_dosyayi_bildirir(tmp_path: Path) -> None:
    mevcut = tmp_path / "codex.exe"
    mevcut.write_bytes(b"MZ")
    result = runtime_checks.ClientsCheck(
        executables=(("codex", str(mevcut)), ("claude", str(tmp_path / "yok.exe")))
    ).run()
    assert result.status is CheckStatus.DEGRADED
    assert result.evidence["missing"] == ["claude"]


def test_istemci_kontrolu_hepsi_varsa_gecer(tmp_path: Path) -> None:
    target = tmp_path / "codex.exe"
    target.write_bytes(b"MZ")
    result = runtime_checks.ClientsCheck(executables=(("codex", str(target)),)).run()
    assert result.status is CheckStatus.PASSED


def test_opencode_spool_check_reports_legacy_candidate_without_mutation(tmp_path: Path) -> None:
    candidate_id = "00000000-0000-4000-8000-000000000001"
    candidate = (
        tmp_path
        / "global"
        / "runtime"
        / "opencode-plugin-spool"
        / f".drain.candidate.{candidate_id}"
    )
    candidate.mkdir(parents=True)
    (candidate / "owner.json").write_text(
        json.dumps({"pid": 999_999, "ownerToken": candidate_id}),
        encoding="utf-8",
    )
    old = dt.datetime.now(dt.UTC).timestamp() - 600
    os.utime(candidate, (old, old))

    result = runtime_checks.OpenCodeSpoolCheck(home=tmp_path).run()

    assert result.status is CheckStatus.DEGRADED
    assert result.evidence["legacy_candidates"] == 1
    assert result.evidence["eligible_legacy_candidates"] == 1
    assert candidate.exists()


def test_opencode_spool_check_passes_when_spool_is_absent(tmp_path: Path) -> None:
    result = runtime_checks.OpenCodeSpoolCheck(home=tmp_path).run()

    assert result.status is CheckStatus.PASSED
    assert result.evidence["legacy_candidates"] == 0


def test_opencode_spool_check_reports_queued_delivery(tmp_path: Path) -> None:
    spool = tmp_path / "global" / "runtime" / "opencode-plugin-spool"
    spool.mkdir(parents=True)
    (spool / "delivery.json").write_text("{}", encoding="utf-8")

    result = runtime_checks.OpenCodeSpoolCheck(home=tmp_path).run()

    assert result.status is CheckStatus.DEGRADED
    assert result.evidence["queued"] == 1
    assert [item.code for item in result.findings] == ["runtime.opencode-spool-queued"]


def test_komut_yuzeyi_kontrolu_sapmayi_yakalar() -> None:
    result = runtime_checks.CommandSurfaceCheck().run()
    assert result.status is CheckStatus.PASSED
    assert result.evidence["missing"] == []
    assert result.evidence["registered"] >= result.evidence["contract"]


@pytest.mark.parametrize(
    "check",
    [
        runtime_checks.QueueCheck(settings=_unreachable()),
        runtime_checks.ModelInventoryCheck(settings=_unreachable()),
        runtime_checks.PolicyCheck(settings=_unreachable()),
        runtime_checks.SchedulerCheck(settings=_unreachable()),
    ],
)
def test_erisilemeyen_kaynak_sahte_passed_uretmez(check: Any) -> None:
    """Kanonik kayit okunamiyorsa dogru cevap `skipped`'tir, `passed` degil."""

    result = check.run()
    assert result.status is CheckStatus.SKIPPED
    assert "reason" in result.evidence


def test_doctor_kapsami_zorunlu_alanlari_icerir(
    context: Any, migrated_database: DatabaseSettings
) -> None:
    """ZEKAM-DOD-002: DB, pgvector, storage, queue, clients, models ve policy."""

    identifiers = {check.check_id for check in build_doctor_checks(context)}
    for required in (
        "postgres.connection",
        "postgres.migrations",
        "storage.object-store",
        "runtime.queue",
        "runtime.clients",
        "runtime.opencode-spool",
        "runtime.models",
        "runtime.policy",
    ):
        assert required in identifiers, f"{required} doctor kapsaminda yok"
