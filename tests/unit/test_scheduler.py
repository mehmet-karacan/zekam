"""P15-T01..T06 scheduler, gelen belge ve gunluk rapor testleri."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.scheduler import (
    REQUIRED_JOB_INTERVALS,
    REQUIRED_JOBS,
    REQUIRED_REPORT_SECTIONS,
    DailyReport,
    IncomingDocument,
    JobDefinition,
    MisfirePolicy,
    NightBudget,
    OverlapPolicy,
    ReportSection,
    RouteDecision,
    Schedule,
    SchedulerIncident,
    SchedulerSnapshot,
    SchedulerState,
    missing_required_jobs,
    plan_trigger,
    required_job_definitions,
    route_document,
)

NOW = dt.datetime(2026, 8, 21, 6, 0, tzinfo=dt.UTC)


def _definition(**kwargs: object) -> JobDefinition:
    defaults: dict[str, object] = {
        "job_name": "daily-report",
        "schedule": Schedule(interval="1d"),
    }
    defaults.update(kwargs)
    return JobDefinition(**defaults)  # type: ignore[arg-type]


# -- T01: zamanlama, misfire, overlap, idempotency ----------------------------


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("30m", dt.timedelta(minutes=30)),
        ("6h", dt.timedelta(hours=6)),
        ("1d", dt.timedelta(days=1)),
    ],
)
def test_aralik_ayristirilir(interval: str, expected: dt.timedelta) -> None:
    assert Schedule(interval=interval).delta == expected


@pytest.mark.parametrize("interval", ["", "0m", "5x", "-1h", "abc"])
def test_gecersiz_aralik_reddedilir(interval: str) -> None:
    with pytest.raises(ValidationFailed):
        Schedule(interval=interval)


def test_naive_zaman_damgasi_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        Schedule(interval="1h").next_after(dt.datetime(2026, 8, 21))
    with pytest.raises(ValidationFailed):
        plan_trigger(_definition(), last_run_at=None, now=dt.datetime(2026, 8, 21))


def test_zamani_gelmeyen_is_calismaz() -> None:
    plan = plan_trigger(
        _definition(schedule=Schedule(interval="1d")),
        last_run_at=NOW - dt.timedelta(hours=2),
        now=NOW,
    )
    assert plan.should_run is False
    assert "zamani gelmedi" in plan.reason


def test_ilk_calisma_hemen_planlanir() -> None:
    plan = plan_trigger(_definition(), last_run_at=None, now=NOW)
    assert plan.should_run is True
    assert plan.scheduled_for == NOW
    assert plan.idempotency_key is not None


def test_kacirilan_calismalar_gorunur_kalir() -> None:
    """Kacirilan calisma sessizce yutulmaz; sayisi raporlanir."""

    definition = _definition(schedule=Schedule(interval="1h"), misfire=MisfirePolicy.SKIP_VISIBLE)
    plan = plan_trigger(definition, last_run_at=NOW - dt.timedelta(hours=5), now=NOW)
    assert plan.should_run is False
    assert plan.missed == 5
    assert "kacirildi" in plan.reason


def test_run_once_misfire_tek_telafi_calistirir() -> None:
    definition = _definition(schedule=Schedule(interval="1h"), misfire=MisfirePolicy.RUN_ONCE)
    plan = plan_trigger(definition, last_run_at=NOW - dt.timedelta(hours=5), now=NOW)
    assert plan.should_run is True
    assert plan.missed == 5


def test_overlap_skip_onceki_calisma_surerken_atlar() -> None:
    definition = _definition(schedule=Schedule(interval="1h"), overlap=OverlapPolicy.SKIP)
    plan = plan_trigger(definition, last_run_at=NOW - dt.timedelta(hours=2), now=NOW, running=True)
    assert plan.should_run is False
    assert "onceki calisma" in plan.reason


def test_overlap_queue_calismaya_izin_verir() -> None:
    definition = _definition(schedule=Schedule(interval="1h"), overlap=OverlapPolicy.QUEUE)
    plan = plan_trigger(definition, last_run_at=NOW - dt.timedelta(hours=2), now=NOW, running=True)
    assert plan.should_run is True


def test_ayni_tetikleme_iki_kez_is_uretmez() -> None:
    definition = _definition(schedule=Schedule(interval="1h"))
    first = plan_trigger(definition, last_run_at=NOW - dt.timedelta(hours=1), now=NOW)
    assert first.should_run is True
    second = plan_trigger(
        definition,
        last_run_at=NOW - dt.timedelta(hours=1),
        now=NOW,
        known_keys=frozenset({first.idempotency_key or ""}),
    )
    assert second.should_run is False
    assert "zaten kaydedildi" in second.reason


def test_idempotency_anahtari_payloada_duyarli() -> None:
    plain = _definition().idempotency_key(NOW)
    with_payload = _definition(payload_digest=digest("p")).idempotency_key(NOW)
    assert plain != with_payload
    assert plain == _definition().idempotency_key(NOW)


def test_farkli_timezone_ayni_ani_ayni_anahtar_uretir() -> None:
    """Anahtar mutlak ana baglidir; yerel saat gosterimi degistirmez."""

    other = NOW.astimezone(dt.timezone(dt.timedelta(hours=3)))
    assert _definition().idempotency_key(NOW) == _definition().idempotency_key(other)


# -- T06: pause, resume, cancel -----------------------------------------------


def test_duraklatilmis_is_calismaz() -> None:
    paused = _definition().pause()
    assert paused.state is SchedulerState.PAUSED
    assert plan_trigger(paused, last_run_at=None, now=NOW).should_run is False


def test_devam_ettirilen_is_yeniden_calisir() -> None:
    resumed = _definition().pause().resume()
    assert resumed.state is SchedulerState.ACTIVE
    assert plan_trigger(resumed, last_run_at=None, now=NOW).should_run is True


def test_iptal_edilmis_is_duraklatilamaz() -> None:
    cancelled = _definition().cancel()
    assert plan_trigger(cancelled, last_run_at=None, now=NOW).should_run is False
    with pytest.raises(PolicyViolation):
        cancelled.pause()


def test_aktif_is_devam_ettirilemez() -> None:
    with pytest.raises(PolicyViolation):
        _definition().resume()


def test_snapshot_yeniden_baslatmada_durumu_tasir() -> None:
    snapshot = SchedulerSnapshot(
        definitions=(_definition(), _definition(job_name="model-health").pause())
    )
    assert [item.job_name for item in snapshot.runnable()] == ["daily-report"]
    assert snapshot.as_dict()["runnable_count"] == 1


def test_olay_bir_sonraki_guvenli_adimi_bildirir() -> None:
    incident = SchedulerIncident(
        job_name="night-research",
        kind="recovery-required",
        detail="claim var receipt yok",
        next_safe_action="adapter kanitini uzlastir",
    )
    assert incident.as_dict()["next_safe_action"]
    with pytest.raises(ValidationFailed):
        SchedulerIncident(job_name="x", kind="failure", detail="d", next_safe_action="  ")
    with pytest.raises(ValidationFailed):
        SchedulerIncident(job_name="x", kind="bilinmeyen", detail="d", next_safe_action="a")


# -- T04: zorunlu bakim isleri -------------------------------------------------


def test_zorunlu_bakim_isleri_eksikse_gorunur() -> None:
    assert missing_required_jobs(()) == REQUIRED_JOBS
    assert missing_required_jobs(REQUIRED_JOBS) == ()
    eksik = missing_required_jobs(tuple(name for name in REQUIRED_JOBS if name != "memory-hygiene"))
    assert eksik == ("memory-hygiene",)


# -- T02: gelen belgeler -------------------------------------------------------


def _document(**kwargs: object) -> IncomingDocument:
    defaults: dict[str, object] = {
        "relative_path": "makale.pdf",
        "content_digest": digest("icerik"),
        "byte_size": 1024,
        "last_modified": NOW - dt.timedelta(seconds=30),
        "observed_at": NOW,
    }
    defaults.update(kwargs)
    return IncomingDocument(**defaults)  # type: ignore[arg-type]


def test_hala_yazilan_dosya_ingest_edilmez() -> None:
    unstable = _document(last_modified=NOW - dt.timedelta(seconds=1))
    assert unstable.is_stable is False
    result = route_document(unstable, targets=("knowledge",))
    assert result.decision is RouteDecision.UNSTABLE


def test_ayni_icerik_ikinci_kez_islenmez() -> None:
    document = _document()
    result = route_document(
        document, known_digests=frozenset({document.content_digest}), targets=("knowledge",)
    )
    assert result.decision is RouteDecision.DUPLICATE


def test_birden_fazla_hedefte_secim_istenir() -> None:
    result = route_document(_document(), targets=("knowledge", "research"))
    assert result.decision is RouteDecision.CHOICE_REQUIRED
    assert result.options == ("knowledge", "research")
    assert result.target is None


def test_tek_hedef_kabul_edilir() -> None:
    result = route_document(_document(), targets=("knowledge",))
    assert result.decision is RouteDecision.ACCEPTED
    assert result.target == "knowledge"


def test_hedefsiz_belge_reddedilir() -> None:
    assert route_document(_document(), targets=()).decision is RouteDecision.REJECTED


def test_gelen_belge_yolu_portable_olmali() -> None:
    with pytest.raises(PolicyViolation):
        _document(relative_path="/etc/passwd")
    with pytest.raises(PolicyViolation):
        _document(relative_path="../disari.pdf")
    with pytest.raises(PolicyViolation):
        _document(relative_path="C:\\gizli\\rapor.pdf")


def test_bos_dosya_yonlendirilmez() -> None:
    with pytest.raises(ValidationFailed):
        _document(byte_size=0)


def test_kabul_karari_hedef_ister() -> None:
    from zekam.domain.scheduler import RouteResult

    with pytest.raises(ValidationFailed):
        RouteResult(_document(), RouteDecision.ACCEPTED, None, "hedefsiz kabul")


# -- T03: gece butcesi ---------------------------------------------------------


def test_bilinmeyen_kota_gece_isini_engeller() -> None:
    budget = NightBudget(max_tokens=1000, max_cost_units=10, max_minutes=60)
    allowed, reason = budget.permits(remaining_quota=None)
    assert allowed is False
    assert "tahmin edilmez" in reason


def test_dusuk_kota_gece_isini_engeller() -> None:
    budget = NightBudget(max_tokens=1000, max_cost_units=10, max_minutes=60, quota_floor=0.3)
    assert budget.permits(remaining_quota=0.1)[0] is False
    assert budget.permits(remaining_quota=0.5)[0] is True


def test_sinirsiz_gece_butcesi_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        NightBudget(max_tokens=0, max_cost_units=10, max_minutes=60)
    with pytest.raises(ValidationFailed):
        NightBudget(max_tokens=10, max_cost_units=10, max_minutes=60, quota_floor=1.5)


# -- T05: gunluk rapor ---------------------------------------------------------


def _sections() -> dict[str, ReportSection]:
    return {
        name: ReportSection(title=name.replace("-", " "), lines=("ornek satir",))
        for name in REQUIRED_REPORT_SECTIONS
    }


def test_rapor_zorunlu_bolumleri_ister() -> None:
    eksik = _sections()
    del eksik["onerilen-next-actions"]
    with pytest.raises(ValidationFailed) as error:
        DailyReport(generated_at=NOW, scope="genel", sections=eksik)
    assert "onerilen-next-actions" in str(error.value)


def test_tam_rapor_markdown_uretir() -> None:
    report = DailyReport(generated_at=NOW, scope="genel", sections=_sections())
    markdown = report.to_markdown()
    assert markdown.startswith("# Zekam gunluk rapor (genel)")
    for name in REQUIRED_REPORT_SECTIONS:
        assert name.replace("-", " ") in markdown
    assert report.report_digest.startswith("sha256:")


def test_bos_bolum_kayit_yok_yazar() -> None:
    sections = _sections()
    sections["security-policy-olaylari"] = ReportSection(title="security", lines=())
    report = DailyReport(generated_at=NOW, scope="genel", sections=sections)
    assert "kayit yok" in report.to_markdown()


def test_rapor_authority_veremez() -> None:
    with pytest.raises(PolicyViolation):
        DailyReport(generated_at=NOW, scope="genel", sections=_sections(), grants_authority=True)
    report = DailyReport(generated_at=NOW, scope="genel", sections=_sections())
    assert report.body()["grants_authority"] is False


def test_rapor_digesti_kararlidir() -> None:
    first = DailyReport(generated_at=NOW, scope="genel", sections=_sections())
    later = DailyReport(
        generated_at=NOW + dt.timedelta(hours=1), scope="genel", sections=_sections()
    )
    assert first.report_digest == later.report_digest, "digest icerige baglidir"


def test_zorunlu_isler_varsayilan_araliklariyla_uretilir() -> None:
    """Kanonik liste ile aralik tablosu ayrisamaz."""

    definitions = required_job_definitions()
    assert tuple(item.job_name for item in definitions) == REQUIRED_JOBS
    assert all(item.state is SchedulerState.ACTIVE for item in definitions)
    assert all(item.schedule.timezone == "UTC" for item in definitions)
    compiler = next(item for item in definitions if item.job_name == "memory-candidate-compile")
    assert compiler.schedule.interval == "5m"
    assert compiler.misfire is MisfirePolicy.RUN_ONCE
    assert compiler.overlap is OverlapPolicy.SKIP


def test_araligi_olmayan_zorunlu_is_uydurulmaz(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aralik tablosunda eksik varsa tanim uretilmez, hata yukselir."""

    eksik = dict(REQUIRED_JOB_INTERVALS)
    eksik.pop("daily-report")
    monkeypatch.setattr("zekam.domain.scheduler.REQUIRED_JOB_INTERVALS", eksik)
    with pytest.raises(ValidationFailed):
        required_job_definitions()
