"""Worker sureci: kuyruk tuketimi ve zamanlanmis is tetikleme.

Worker sohbet surecinden bagimsizdir. Her donguda once **kapasite** kontrol
edilir, sonra zamanlanmis tetiklemeler kaydedilir, sonra kuyruktan is alinir.
Islenen is terminal receipt'i olmadan `completed` yapilamaz; iptal edilen
calisma terminal sonuc yayimlayamaz.
"""

from __future__ import annotations

import datetime as dt
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from zekam.application.execution import ExecutionHost
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.release import BackpressureDecision, CancellationRequest
from zekam.domain.runtime import AttemptOutcome, FailureCategory
from zekam.domain.scheduler import (
    JobDefinition,
    MisfirePolicy,
    OverlapPolicy,
    Schedule,
    SchedulerState,
    TriggerPlan,
    plan_trigger,
    required_job_definitions,
)
from zekam.infrastructure.postgres.runtime_repository import ClaimedWork

#: Bir is birimini isleyen fonksiyon. Basari durumunda result digest doner.
Handler = Callable[[ClaimedWork], str]

DEFAULT_POLL_SECONDS = 2.0
DEFAULT_LEASE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Worker calisma sinirlari. Sinirsiz worker yoktur."""

    worker_label: str
    capabilities: tuple[str, ...]
    poll_seconds: float = DEFAULT_POLL_SECONDS
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    max_queue_depth: int = 100
    max_workers: int = 4
    max_iterations: int | None = None

    def __post_init__(self) -> None:
        if not self.worker_label.strip():
            raise PolicyViolation("worker etiketi bos olamaz")
        if not self.capabilities:
            raise PolicyViolation("worker en az bir yetenek beyan etmeli")
        if self.poll_seconds <= 0 or self.lease_seconds <= 0:
            raise PolicyViolation("poll ve lease degerleri pozitif olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_label": self.worker_label,
            "capabilities": list(self.capabilities),
            "poll_seconds": self.poll_seconds,
            "lease_seconds": self.lease_seconds,
            "max_queue_depth": self.max_queue_depth,
            "max_workers": self.max_workers,
        }


@dataclass(frozen=True, slots=True)
class TickResult:
    """Tek dongunun gozlemlenebilir sonucu."""

    accepted_work: bool
    triggered_jobs: tuple[str, ...] = ()
    skipped_reason: str | None = None
    job_id: UUID | None = None
    outcome: AttemptOutcome | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted_work": self.accepted_work,
            "triggered_jobs": list(self.triggered_jobs),
            "skipped_reason": self.skipped_reason,
            "job_id": str(self.job_id) if self.job_id else None,
            "outcome": str(self.outcome) if self.outcome else None,
        }


@dataclass(slots=True)
class ShutdownSignal:
    """Zarif kapanma bayragi.

    Worker bir isi yarida birakmaz: mevcut is biter, sonraki dongu baslamaz.
    """

    requested: bool = False
    reason: str = ""

    def request(self, reason: str = "signal") -> None:
        self.requested = True
        self.reason = reason

    def install(self) -> None:
        """SIGINT ve SIGTERM'i zarif kapanmaya baglar."""

        for name in ("SIGINT", "SIGTERM"):
            handler = getattr(signal, name, None)
            if handler is None:
                continue
            try:
                # Dongu degiskenini varsayilan argumanla bagla: aksi halde butun
                # sinyaller son ismi raporlardi.
                signal.signal(handler, lambda *_, source=name: self.request(source))
            except (ValueError, OSError):
                # Ana is parcacigi disinda sinyal baglanamaz; bu olumcul degildir.
                continue


@dataclass(frozen=True, slots=True)
class SchedulerGateway:
    """Zamanlanmis is tanimlarini okur ve tetiklemeleri kalicilastirir."""

    connection: Any
    realm_id: UUID

    def definitions(self) -> tuple[tuple[UUID, JobDefinition, dt.datetime | None], ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, job_name, interval_spec, timezone, state, misfire, overlap,"
                "  payload_digest, last_run_at"
                " from ops.job_definition where realm_id = %s order by job_name",
                (self.realm_id,),
            )
            rows = cursor.fetchall()
        found: list[tuple[UUID, JobDefinition, dt.datetime | None]] = []
        for row in rows:
            # Ham metin degil enum uretilir: `is` karsilastirmalari aksi halde
            # sessizce False doner ve aktif tanim calismaz gorunur.
            definition = JobDefinition(
                job_name=str(row[1]),
                schedule=Schedule(interval=str(row[2]), timezone=str(row[3])),
                state=SchedulerState(str(row[4])),
                misfire=MisfirePolicy(str(row[5])),
                overlap=OverlapPolicy(str(row[6])),
                payload_digest=row[7],
            )
            found.append((UUID(str(row[0])), definition, row[8]))
        return tuple(found)

    def ensure_required_definitions(self, *, now: dt.datetime) -> tuple[str, ...]:
        """Eksik zorunlu bakim islerini tanimlar ve yeni tanimlananlari doner.

        Idempotenttir: var olan tanim degistirilmez, ikinci calistirma bos
        doner. Uygulama rolu `misfire` ve `overlap` sutunlarini guncelleyemez
        (en az yetki), bu yuzden politika insert aninda verilir.
        """

        created: list[str] = []
        for definition in required_job_definitions():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "insert into ops.job_definition"
                    " (id, realm_id, job_name, interval_spec, timezone, state, misfire,"
                    "  overlap, created_at)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " on conflict (realm_id, job_name) do nothing returning job_name",
                    (
                        new_uuid7(now=now),
                        self.realm_id,
                        definition.job_name,
                        definition.schedule.interval,
                        definition.schedule.timezone,
                        str(definition.state),
                        str(definition.misfire),
                        str(definition.overlap),
                        now,
                    ),
                )
                row = cursor.fetchone()
            if row is not None:
                created.append(str(row[0]))
        return tuple(created)

    def is_running(self, definition_id: UUID) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select 1 from ops.job_run"
                " where realm_id = %s and definition_id = %s and state in ('pending', 'running')"
                " limit 1",
                (self.realm_id, definition_id),
            )
            return cursor.fetchone() is not None

    def known_keys(self, definition_id: UUID, *, limit: int = 50) -> frozenset[str]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select idempotency_key from ops.job_run"
                " where realm_id = %s and definition_id = %s"
                " order by scheduled_for desc limit %s",
                (self.realm_id, definition_id, limit),
            )
            return frozenset(str(row[0]) for row in cursor.fetchall())

    def record_trigger(
        self, definition_id: UUID, plan: TriggerPlan, *, now: dt.datetime
    ) -> UUID | None:
        """Tetiklemeyi kaydeder. Ayni anahtar ikinci kez kaydedilmez."""

        if not plan.should_run or plan.idempotency_key is None or plan.scheduled_for is None:
            return None
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into ops.job_run"
                " (id, realm_id, definition_id, idempotency_key, scheduled_for, started_at,"
                "  state, missed_count)"
                " values (%s, %s, %s, %s, %s, %s, 'running', %s)"
                " on conflict (realm_id, idempotency_key) do nothing returning id",
                (
                    new_uuid7(now=now),
                    self.realm_id,
                    definition_id,
                    plan.idempotency_key,
                    plan.scheduled_for,
                    now,
                    plan.missed,
                ),
            )
            row = cursor.fetchone()
        return UUID(str(row[0])) if row else None

    def finish_run(self, run_id: UUID, *, state: str, detail: str, now: dt.datetime) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update ops.job_run set state = %s, finished_at = %s, detail = %s"
                " where realm_id = %s and id = %s",
                (state, now, detail, self.realm_id, run_id),
            )

    def touch_definition(self, definition_id: UUID, *, now: dt.datetime) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update ops.job_definition set last_run_at = %s where realm_id = %s and id = %s",
                (now, self.realm_id, definition_id),
            )

    def record_incident(
        self, job_name: str, *, kind: str, detail: str, next_safe_action: str, now: dt.datetime
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into ops.scheduler_incident"
                " (id, realm_id, job_name, kind, detail, next_safe_action, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                (new_uuid7(now=now), self.realm_id, job_name, kind, detail, next_safe_action, now),
            )


@dataclass(slots=True)
class Worker:
    """Kuyruk tuketen ve zamanlanmis isleri tetikleyen surec."""

    host: ExecutionHost
    settings: WorkerSettings
    scheduler: SchedulerGateway | None = None
    handlers: dict[str, Handler] = field(default_factory=dict)
    shutdown: ShutdownSignal = field(default_factory=ShutdownSignal)
    cancellations: dict[UUID, CancellationRequest] = field(default_factory=dict)
    _active: int = 0

    # -- kapasite -------------------------------------------------------------

    def capacity(self, *, queue_depth: int) -> BackpressureDecision:
        return BackpressureDecision(
            queue_depth=queue_depth,
            max_queue_depth=self.settings.max_queue_depth,
            active_workers=self._active,
            max_workers=self.settings.max_workers,
        )

    def cancel(self, job_id: UUID, *, now: dt.datetime, force: bool = False) -> None:
        """Sert iptal talebi kaydeder; is terminal sonuc yayimlayamaz."""

        self.cancellations[job_id] = CancellationRequest(
            run_ref=str(job_id), requested_at=now, acknowledged=True, force=force
        )

    # -- planlama (salt okunur) -----------------------------------------------

    def plan(self, *, now: dt.datetime | None = None, queue_depth: int = 0) -> TickResult:
        """Ne olacagini hicbir sey yazmadan hesaplar.

        `tick` ile ayni kararlari verir ama kuyruktan is almaz ve tetikleme
        kaydetmez; `--uygula` verilmeden calisan yuzey bunu kullanir.
        """

        moment = now or dt.datetime.now(dt.UTC)
        decision = self.capacity(queue_depth=queue_depth)
        if not decision.accepts_work:
            return TickResult(accepted_work=False, skipped_reason=decision.reason())

        would_trigger: list[str] = []
        if self.scheduler is not None:
            for definition_id, definition, last_run in self.scheduler.definitions():
                candidate = plan_trigger(
                    definition,
                    last_run_at=last_run,
                    now=moment,
                    running=self.scheduler.is_running(definition_id),
                    known_keys=self.scheduler.known_keys(definition_id),
                )
                if candidate.should_run:
                    would_trigger.append(definition.job_name)
        return TickResult(
            accepted_work=False,
            triggered_jobs=tuple(would_trigger),
            skipped_reason="dry-run: hicbir sey yazilmadi",
        )

    # -- dongu ----------------------------------------------------------------

    def tick(self, *, now: dt.datetime | None = None, queue_depth: int = 0) -> TickResult:
        """Tek dongu: kapasite -> zamanlama -> kuyruk."""

        moment = now or dt.datetime.now(dt.UTC)
        decision = self.capacity(queue_depth=queue_depth)
        if not decision.accepts_work:
            return TickResult(accepted_work=False, skipped_reason=decision.reason())

        triggered = self._run_schedules(moment)
        work = self.host.acquire_work(
            capabilities=self.settings.capabilities,
            lease_seconds=self.settings.lease_seconds,
            now=moment,
        )
        if work is None:
            return TickResult(
                accepted_work=False, triggered_jobs=triggered, skipped_reason="kuyruk bos"
            )
        outcome = self._process(work, moment)
        return TickResult(
            accepted_work=True,
            triggered_jobs=triggered,
            job_id=work.job.id,
            outcome=outcome,
        )

    def run(self, *, queue_depth: Callable[[], int] | None = None) -> tuple[TickResult, ...]:
        """Kapanma istenene veya iterasyon siniri dolana kadar doner."""

        results: list[TickResult] = []
        iterations = 0
        while not self.shutdown.requested:
            if (
                self.settings.max_iterations is not None
                and iterations >= self.settings.max_iterations
            ):
                break
            depth = queue_depth() if queue_depth else 0
            result = self.tick(queue_depth=depth)
            results.append(result)
            iterations += 1
            if not result.accepted_work and not self.shutdown.requested:
                time.sleep(self.settings.poll_seconds)
        return tuple(results)

    # -- ic akis --------------------------------------------------------------

    def _run_schedules(self, now: dt.datetime) -> tuple[str, ...]:
        """Zamani gelen tanimlar icin calisma kaydi acar."""

        if self.scheduler is None:
            return ()
        triggered: list[str] = []
        for definition_id, definition, last_run in self.scheduler.definitions():
            plan = plan_trigger(
                definition,
                last_run_at=last_run,
                now=now,
                running=self.scheduler.is_running(definition_id),
                known_keys=self.scheduler.known_keys(definition_id),
            )
            if plan.missed > 0 and not plan.should_run:
                # Kacirilan calisma sessizce yutulmaz.
                self.scheduler.record_incident(
                    definition.job_name,
                    kind="misfire",
                    detail=f"{plan.missed} calisma kacirildi: {plan.reason}",
                    next_safe_action="kacirilan pencereyi elle degerlendirin",
                    now=now,
                )
            run_id = self.scheduler.record_trigger(definition_id, plan, now=now)
            if run_id is None:
                continue
            self.scheduler.touch_definition(definition_id, now=now)
            self.scheduler.finish_run(
                run_id, state="succeeded", detail="tetikleme kaydedildi", now=now
            )
            triggered.append(definition.job_name)
        return tuple(triggered)

    def _process(self, work: ClaimedWork, now: dt.datetime) -> AttemptOutcome:
        """Bir is birimini isler ve terminal duruma alir."""

        self._active += 1
        try:
            cancellation = self.cancellations.get(work.job.id)
            if cancellation is not None:
                self.host.finish(work, outcome=AttemptOutcome.ABANDONED, now=now)
                cancellation.assert_no_result_after_cancel(result_published=False)
                return AttemptOutcome.ABANDONED

            handler = self.handlers.get(str(work.job.kind))
            if handler is None:
                self.host.finish(
                    work,
                    outcome=AttemptOutcome.FAILED,
                    failure_category=FailureCategory.POLICY,
                    now=now,
                )
                return AttemptOutcome.FAILED

            try:
                result_digest = handler(work)
            except Exception as exc:
                self.host.finish(
                    work,
                    outcome=AttemptOutcome.FAILED,
                    failure_category=FailureCategory.ADAPTER,
                    result_digest=digest({"error": type(exc).__name__}),
                    now=now,
                )
                return AttemptOutcome.FAILED

            # Terminal receipt'i olmayan claim varsa finish reddeder.
            self.host.finish(
                work, outcome=AttemptOutcome.SUCCEEDED, result_digest=result_digest, now=now
            )
            return AttemptOutcome.SUCCEEDED
        finally:
            self._active -= 1


def build_worker(
    connection: Any,
    realm_id: UUID,
    *,
    settings: WorkerSettings,
    handlers: dict[str, Handler] | None = None,
    with_scheduler: bool = True,
    allow_empty_handlers: bool = False,
) -> Worker:
    """Worker'i kanonik baglantilarla kurar."""

    resolved_handlers = dict(handlers or {})
    if not resolved_handlers and not allow_empty_handlers:
        raise PolicyViolation("Worker en az bir explicit handler ile baslatilmali")
    host = ExecutionHost(connection, realm_id, worker_label=settings.worker_label)
    gateway = SchedulerGateway(connection, realm_id) if with_scheduler else None
    return Worker(
        host=host,
        settings=settings,
        scheduler=gateway,
        handlers=resolved_handlers,
    )


def default_capabilities() -> tuple[str, ...]:
    """Yerlesik worker'in beyan ettigi yetenekler."""

    return ("sandbox.write", "knowledge.ingest", "report.render")


def noop_handler(work: ClaimedWork) -> str:
    """Yalniz test ve kontrollu tanilama icin yan etkisiz isleyici.

    Production handler registry'sine otomatik baglanmaz. Cagiran taraf bunu
    acikca enjekte etmedikce bir isi basarili gostermek icin kullanilamaz.
    """

    return digest({"job": str(work.job.id), "handled_by": "noop"})


def resolve_handlers(
    names: Sequence[str], *, registry: Mapping[str, Handler] | None = None
) -> dict[str, Handler]:
    """Istenen is turlerini explicit registry'den fail-closed cozer."""

    available = dict(registry or {})
    missing = sorted({name for name in names if name not in available})
    if missing:
        raise PolicyViolation(f"Worker handler tanimsiz: {', '.join(missing)}")
    return {name: available[name] for name in names}
