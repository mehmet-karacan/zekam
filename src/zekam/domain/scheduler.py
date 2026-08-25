"""Durable scheduler, gelen belge yonlendirme ve gunluk rapor sozlesmesi.

Scheduler sohbet surecinden bagimsizdir: tanim kaliciydir, kacirilan calisma
sessizce yutulmaz, ayni tetikleme iki kez is uretmez ve durum yeniden baslatma
sonrasi dogru devam eder.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

#: Bir dosyanin "yazimi bitti" sayilmasi icin gereken sessizlik suresi.
STABLE_AFTER_SECONDS = 5

MAX_MISFIRE_CATCHUP = 1


class SchedulerState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class MisfirePolicy(StrEnum):
    #: Kacirilan calismalar icin yalniz bir kez telafi calistir.
    RUN_ONCE = "run-once"
    #: Kacirilan calismayi atla ama gorunur kaydet.
    SKIP_VISIBLE = "skip-visible"


class OverlapPolicy(StrEnum):
    #: Onceki calisma bitmeden yenisi baslamaz.
    SKIP = "skip"
    #: Onceki calisma bitmeden yeni tetikleme sirayla bekler.
    QUEUE = "queue"


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    RECOVERY_REQUIRED = "recovery-required"


class RouteDecision(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    UNSTABLE = "unstable"
    CHOICE_REQUIRED = "choice-required"
    REJECTED = "rejected"


#: Kanonik bakim isleri; hepsi zamanlanmis olmak zorundadir.
REQUIRED_JOBS = (
    "gelen-belgeler",
    "project-incremental-scan",
    "model-health",
    "benchmark-staleness",
    "memory-hygiene",
    "recovery-scan",
    "stale-index-scan",
    "academic-comparison",
    "night-research",
    "daily-report",
    "backup-verify",
    "retention-review",
    "diagnostic-trace-purge",
    "chaos-campaign",
)

#: Kanonik bakim isleri icin varsayilan araliklar. Anahtarlar REQUIRED_JOBS ile
#: birebir ayni olmak zorundadir; `required_job_definitions` bunu dogrular.
REQUIRED_JOB_INTERVALS: dict[str, str] = {
    "gelen-belgeler": "5m",
    "project-incremental-scan": "1h",
    "model-health": "6h",
    "benchmark-staleness": "1d",
    "memory-hygiene": "1d",
    "recovery-scan": "15m",
    "stale-index-scan": "1d",
    "academic-comparison": "1d",
    "night-research": "1d",
    "daily-report": "1d",
    "backup-verify": "1d",
    "retention-review": "7d",
    "diagnostic-trace-purge": "1d",
    "chaos-campaign": "7d",
}

_INTERVAL = re.compile(r"^(?P<value>[1-9]\d*)(?P<unit>[mhd])$")


@dataclass(frozen=True, slots=True)
class Schedule:
    """Basit, deterministik zamanlama: sabit aralik ve timezone.

    Cron ifadesi yerine acik aralik kullanilir; boylece bir sonraki calisma
    zamani test edilebilir sekilde hesaplanir.
    """

    interval: str
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        if _INTERVAL.match(self.interval) is None:
            raise ValidationFailed("aralik '<sayi><m|h|d>' bicimde olmali")
        if not self.timezone.strip():
            raise ValidationFailed("timezone bos olamaz")

    @property
    def delta(self) -> dt.timedelta:
        match = _INTERVAL.match(self.interval)
        assert match is not None  # __post_init__ dogruladi
        value = int(match.group("value"))
        unit = match.group("unit")
        return {"m": dt.timedelta(minutes=value), "h": dt.timedelta(hours=value)}.get(
            unit, dt.timedelta(days=value)
        )

    def next_after(self, moment: dt.datetime) -> dt.datetime:
        if moment.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")
        return moment + self.delta

    def missed_between(self, last: dt.datetime, now: dt.datetime) -> int:
        """Iki an arasinda kacirilan calisma sayisi."""

        if now <= last:
            return 0
        return int((now - last) / self.delta)

    def as_dict(self) -> dict[str, str]:
        return {"interval": self.interval, "timezone": self.timezone}


@dataclass(frozen=True, slots=True)
class JobDefinition:
    """Kalici zamanlama tanimi. Sohbet surecine bagli degildir."""

    job_name: str
    schedule: Schedule
    state: SchedulerState = SchedulerState.ACTIVE
    misfire: MisfirePolicy = MisfirePolicy.RUN_ONCE
    overlap: OverlapPolicy = OverlapPolicy.SKIP
    payload_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.job_name.strip():
            raise ValidationFailed("is adi bos olamaz")
        if self.payload_digest is not None:
            parse_digest(self.payload_digest)

    @property
    def is_runnable(self) -> bool:
        return self.state is SchedulerState.ACTIVE

    def pause(self) -> JobDefinition:
        if self.state is SchedulerState.CANCELLED:
            raise PolicyViolation("iptal edilmis is duraklatilamaz")
        return JobDefinition(
            job_name=self.job_name,
            schedule=self.schedule,
            state=SchedulerState.PAUSED,
            misfire=self.misfire,
            overlap=self.overlap,
            payload_digest=self.payload_digest,
        )

    def resume(self) -> JobDefinition:
        if self.state is not SchedulerState.PAUSED:
            raise PolicyViolation("yalniz duraklatilmis is devam ettirilir")
        return JobDefinition(
            job_name=self.job_name,
            schedule=self.schedule,
            state=SchedulerState.ACTIVE,
            misfire=self.misfire,
            overlap=self.overlap,
            payload_digest=self.payload_digest,
        )

    def cancel(self) -> JobDefinition:
        return JobDefinition(
            job_name=self.job_name,
            schedule=self.schedule,
            state=SchedulerState.CANCELLED,
            misfire=self.misfire,
            overlap=self.overlap,
            payload_digest=self.payload_digest,
        )

    def idempotency_key(self, scheduled_for: dt.datetime) -> str:
        """Ayni tetikleme icin kararli anahtar; iki kez is uretmez."""

        return digest(
            {
                "job": self.job_name,
                "scheduled_for": scheduled_for.astimezone(dt.UTC).isoformat(),
                "payload": self.payload_digest,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_name": self.job_name,
            "schedule": self.schedule.as_dict(),
            "state": str(self.state),
            "misfire": str(self.misfire),
            "overlap": str(self.overlap),
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True, slots=True)
class TriggerPlan:
    """Bir tetikleme degerlendirmesinin sonucu."""

    should_run: bool
    scheduled_for: dt.datetime | None
    idempotency_key: str | None
    missed: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "should_run": self.should_run,
            "scheduled_for": self.scheduled_for.isoformat() if self.scheduled_for else None,
            "idempotency_key": self.idempotency_key,
            "missed": self.missed,
            "reason": self.reason,
        }


def plan_trigger(
    definition: JobDefinition,
    *,
    last_run_at: dt.datetime | None,
    now: dt.datetime,
    running: bool = False,
    known_keys: frozenset[str] = frozenset(),
) -> TriggerPlan:
    """Isin simdi calisip calismayacagini ve hangi anahtarla calisacagini belirler."""

    if now.tzinfo is None:
        raise ValidationFailed("zaman damgasi timezone-aware olmali")
    if not definition.is_runnable:
        return TriggerPlan(False, None, None, 0, f"is {definition.state} durumunda")

    if last_run_at is None:
        scheduled = now
        missed = 0
    else:
        missed = definition.schedule.missed_between(last_run_at, now)
        if missed == 0:
            return TriggerPlan(False, None, None, 0, "sonraki calisma zamani gelmedi")
        if missed > MAX_MISFIRE_CATCHUP and definition.misfire is MisfirePolicy.SKIP_VISIBLE:
            # Kacirilan calismalar sessizce yutulmaz; sayisi raporlanir.
            return TriggerPlan(False, None, None, missed, f"{missed} calisma kacirildi ve atlandi")
        scheduled = last_run_at + definition.schedule.delta * missed

    if running and definition.overlap is OverlapPolicy.SKIP:
        return TriggerPlan(False, None, None, missed, "onceki calisma surdugu icin atlandi")

    key = definition.idempotency_key(scheduled)
    if key in known_keys:
        return TriggerPlan(False, scheduled, key, missed, "ayni tetikleme zaten kaydedildi")
    return TriggerPlan(True, scheduled, key, missed, "calistirilabilir")


@dataclass(frozen=True, slots=True)
class IncomingDocument:
    """`gelen-belgeler` altinda gorulen dosya."""

    relative_path: str
    content_digest: str
    byte_size: int
    last_modified: dt.datetime
    observed_at: dt.datetime

    def __post_init__(self) -> None:
        parse_digest(self.content_digest)
        if self.byte_size <= 0:
            raise ValidationFailed("bos dosya yonlendirilmez")
        value = self.relative_path
        if "\\" in value or value.startswith("/") or PureWindowsPath(value).is_absolute():
            raise PolicyViolation("gelen belge yolu portable olmali")
        if ".." in PurePosixPath(value).parts:
            raise PolicyViolation("gelen belge yolu traversal tasiyamaz")

    @property
    def is_stable(self) -> bool:
        """Yazimi bitmis mi? Hala yazilan dosya ingest edilmez."""

        quiet = (self.observed_at - self.last_modified).total_seconds()
        return quiet >= STABLE_AFTER_SECONDS

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "content_digest": self.content_digest,
            "byte_size": self.byte_size,
            "is_stable": self.is_stable,
        }


@dataclass(frozen=True, slots=True)
class RouteResult:
    """Gelen belgenin yonlendirme karari."""

    document: IncomingDocument
    decision: RouteDecision
    target: str | None
    detail: str
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decision is RouteDecision.ACCEPTED and not self.target:
            raise ValidationFailed("kabul edilen belge hedef ister")
        if self.decision is RouteDecision.CHOICE_REQUIRED and len(self.options) < 2:
            raise ValidationFailed("secim gerektiren karar en az iki secenek ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "document": self.document.as_dict(),
            "decision": str(self.decision),
            "target": self.target,
            "detail": self.detail,
            "options": list(self.options),
        }


def route_document(
    document: IncomingDocument,
    *,
    known_digests: frozenset[str] = frozenset(),
    targets: tuple[str, ...] = (),
) -> RouteResult:
    """Gelen belgeyi yonlendirir; belirsizlikte secim ister, tahmin etmez."""

    if not document.is_stable:
        return RouteResult(document, RouteDecision.UNSTABLE, None, "dosya hala yaziliyor")
    if document.content_digest in known_digests:
        return RouteResult(document, RouteDecision.DUPLICATE, None, "ayni icerik daha once islendi")
    if not targets:
        return RouteResult(document, RouteDecision.REJECTED, None, "uygun hedef bulunamadi")
    if len(targets) > 1:
        return RouteResult(
            document,
            RouteDecision.CHOICE_REQUIRED,
            None,
            "birden fazla hedef eslesti; secim gerekiyor",
            options=tuple(sorted(targets)),
        )
    return RouteResult(document, RouteDecision.ACCEPTED, targets[0], "tek hedef eslesti")


@dataclass(frozen=True, slots=True)
class ReportSection:
    title: str
    lines: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValidationFailed("bolum basligi bos olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "lines": list(self.lines)}


#: Sabah raporunda bulunmasi zorunlu bolumler.
REQUIRED_REPORT_SECTIONS = (
    "tamamlanan-isler",
    "aktif-lease-ve-recovery",
    "subagent-model-dagilimi",
    "okunan-kaynaklar",
    "token-cost-latency-quota",
    "model-health-benchmark",
    "memory-skill-adaylari",
    "retrieval-index-sorunlari",
    "security-policy-olaylari",
    "onerilen-next-actions",
)


@dataclass(frozen=True, slots=True)
class DailyReport:
    """Gunluk rapor. Zorunlu bolumler eksikse rapor uretilmis sayilmaz."""

    generated_at: dt.datetime
    scope: str
    sections: dict[str, ReportSection]
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("rapor authority veremez")
        missing = tuple(name for name in REQUIRED_REPORT_SECTIONS if name not in self.sections)
        if missing:
            raise ValidationFailed(f"raporda eksik bolum: {', '.join(missing)}")
        if self.generated_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-daily-report/v1",
            "scope": self.scope,
            "sections": {
                name: section.as_dict() for name, section in sorted(self.sections.items())
            },
            "grants_authority": False,
        }

    @property
    def report_digest(self) -> str:
        return digest(self.body())

    def to_markdown(self) -> str:
        lines = [f"# Zekam gunluk rapor ({self.scope})", ""]
        for name in REQUIRED_REPORT_SECTIONS:
            section = self.sections[name]
            lines.append(f"## {section.title}")
            lines.extend(f"- {item}" for item in section.lines or ("kayit yok",))
            lines.append("")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class SchedulerIncident:
    """Zamanlayici olayi ve runbook adimi."""

    job_name: str
    kind: str
    detail: str
    next_safe_action: str

    def __post_init__(self) -> None:
        if self.kind not in {"misfire", "overlap", "failure", "recovery-required"}:
            raise ValidationFailed("olay turu taninmiyor")
        if not self.next_safe_action.strip():
            raise ValidationFailed("olay bir sonraki guvenli adimi bildirmeli")

    def as_dict(self) -> dict[str, str]:
        return {
            "job_name": self.job_name,
            "kind": self.kind,
            "detail": self.detail,
            "next_safe_action": self.next_safe_action,
        }


def missing_required_jobs(defined: tuple[str, ...]) -> tuple[str, ...]:
    """Kanonik bakim islerinden tanimlanmamis olanlar."""

    return tuple(name for name in REQUIRED_JOBS if name not in defined)


def required_job_definitions() -> tuple[JobDefinition, ...]:
    """Zorunlu bakim islerini varsayilan araliklariyla uretir.

    Aralik tablosu eksikse tanim **uydurulmaz**: kanonik liste ile aralik
    tablosu arasindaki her sapma burada gorunur hale gelir.
    """

    eksik = tuple(name for name in REQUIRED_JOBS if name not in REQUIRED_JOB_INTERVALS)
    if eksik:
        raise ValidationFailed(f"zorunlu is icin varsayilan aralik yok: {', '.join(eksik)}")
    return tuple(
        JobDefinition(job_name=name, schedule=Schedule(interval=REQUIRED_JOB_INTERVALS[name]))
        for name in REQUIRED_JOBS
    )


@dataclass(frozen=True, slots=True)
class NightBudget:
    """Gece isleri icin bounded butce. Sinirsiz gece calismasi yoktur."""

    max_tokens: int
    max_cost_units: int
    max_minutes: int
    quota_floor: float = 0.2

    def __post_init__(self) -> None:
        if min(self.max_tokens, self.max_cost_units, self.max_minutes) <= 0:
            raise ValidationFailed("butce degerleri pozitif olmali")
        if not 0.0 <= self.quota_floor <= 1.0:
            raise ValidationFailed("kota tabani 0..1 araliginda olmali")

    def permits(self, *, remaining_quota: float | None) -> tuple[bool, str]:
        """Kota bilinmiyorsa tahmin edilmez; gece isi calismaz."""

        if remaining_quota is None:
            return False, "kota bilinmiyor; tahmin edilmez"
        if remaining_quota < self.quota_floor:
            return False, "kalan kota tabanin altinda"
        return True, "butce ve kota uygun"

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "max_cost_units": self.max_cost_units,
            "max_minutes": self.max_minutes,
            "quota_floor": self.quota_floor,
        }


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    """Yeniden baslatma sonrasi durumu tasiyan salt okunur ozet."""

    definitions: tuple[JobDefinition, ...]
    incidents: tuple[SchedulerIncident, ...] = field(default_factory=tuple)

    def runnable(self) -> tuple[JobDefinition, ...]:
        return tuple(item for item in self.definitions if item.is_runnable)

    def as_dict(self) -> dict[str, Any]:
        return {
            "definitions": [item.as_dict() for item in self.definitions],
            "incidents": [item.as_dict() for item in self.incidents],
            "runnable_count": len(self.runnable()),
        }
