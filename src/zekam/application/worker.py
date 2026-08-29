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
from pathlib import Path
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
ScheduledHandler = Callable[[dt.datetime], str]

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
    """Zamanlanmis isleri tetikleyen, istege bagli kuyruk tuketen surec."""

    host: ExecutionHost
    settings: WorkerSettings
    scheduler: SchedulerGateway | None = None
    handlers: dict[str, Handler] = field(default_factory=dict)
    scheduled_handlers: dict[str, ScheduledHandler] = field(default_factory=dict)
    consume_queue: bool = True
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
        decision = self.capacity(queue_depth=queue_depth if self.consume_queue else 0)
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
        """Tek dongu: kapasite -> zamanlama -> istege bagli kuyruk."""

        moment = now or dt.datetime.now(dt.UTC)
        decision = self.capacity(queue_depth=queue_depth if self.consume_queue else 0)
        if not decision.accepts_work:
            return TickResult(accepted_work=False, skipped_reason=decision.reason())

        triggered = self._run_schedules(moment)
        if not self.consume_queue:
            return TickResult(
                accepted_work=False,
                triggered_jobs=triggered,
                skipped_reason="scheduled-only: queue claim disabled",
            )
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
            depth = queue_depth() if queue_depth is not None and self.consume_queue else 0
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
            handler = self.scheduled_handlers.get(definition.job_name)
            if handler is None:
                if definition.job_name not in {"diagnostic-trace-purge", "chaos-campaign"}:
                    self.scheduler.touch_definition(definition_id, now=now)
                    self.scheduler.finish_run(
                        run_id, state="succeeded", detail="tetikleme kaydedildi", now=now
                    )
                    triggered.append(definition.job_name)
                    continue
                detail = f"Zamanlanmis handler tanimsiz: {definition.job_name}"
                self.scheduler.finish_run(run_id, state="failed", detail=detail, now=now)
                self.scheduler.record_incident(
                    definition.job_name,
                    kind="failure",
                    detail=detail,
                    next_safe_action="exact scheduled handler'i worker composition'a baglayin",
                    now=now,
                )
                continue
            try:
                detail = handler(now)
            except Exception as exc:
                failure = f"Scheduled handler basarisiz: {type(exc).__name__}"
                self.scheduler.finish_run(run_id, state="failed", detail=failure, now=now)
                self.scheduler.record_incident(
                    definition.job_name,
                    kind="failure",
                    detail=failure,
                    next_safe_action="incident kanitini inceleyip guvenli yeniden deneme yapin",
                    now=now,
                )
                continue
            self.scheduler.touch_definition(definition_id, now=now)
            self.scheduler.finish_run(run_id, state="succeeded", detail=detail, now=now)
            triggered.append(definition.job_name)
        return tuple(triggered)

    def _process(self, work: ClaimedWork, now: dt.datetime) -> AttemptOutcome:
        """Bir is birimini isler ve terminal duruma alir."""

        def finish_exact(
            outcome: AttemptOutcome,
            *,
            failure_category: FailureCategory | None = None,
            result_digest: str | None = None,
        ) -> None:
            """Never report a terminal outcome unless the canonical finish committed."""

            try:
                finished = self.host.finish(
                    work,
                    outcome=outcome,
                    failure_category=failure_category,
                    result_digest=result_digest,
                    now=now,
                )
            except Exception as exc:
                recovery_digest = digest(
                    {
                        "schema": "zekam-worker-finish-recovery/v1",
                        "job_id": str(work.job.id),
                        "requested_outcome": outcome.value,
                        "finish_error": type(exc).__name__,
                    }
                )
                try:
                    recovered = self.host.finish(
                        work,
                        outcome=AttemptOutcome.RECOVERY_REQUIRED,
                        result_digest=recovery_digest,
                        now=now,
                    )
                except Exception as recovery_exc:
                    raise PolicyViolation(
                        "Worker finish belirsiz; recovery-required kaydi da reddedildi"
                    ) from recovery_exc
                if not recovered:
                    raise PolicyViolation(
                        "Worker finish belirsiz; recovery-required gorunurlugu olusmadi"
                    ) from exc
                raise PolicyViolation(
                    "Worker finish belirsiz; is recovery-required olarak kapatildi"
                ) from exc
            if finished:
                return
            recovery_digest = digest(
                {
                    "schema": "zekam-worker-finish-recovery/v1",
                    "job_id": str(work.job.id),
                    "requested_outcome": outcome.value,
                    "finish_error": "finish-returned-false",
                }
            )
            recovered = self.host.finish(
                work,
                outcome=AttemptOutcome.RECOVERY_REQUIRED,
                result_digest=recovery_digest,
                now=now,
            )
            if not recovered:
                raise PolicyViolation(
                    "Worker terminal finish ve recovery-required finish reddedildi"
                )
            raise PolicyViolation(
                "Worker terminal finish reddedildi; is recovery-required olarak kapatildi"
            )

        self._active += 1
        try:
            cancellation = self.cancellations.get(work.job.id)
            if cancellation is not None:
                finish_exact(AttemptOutcome.ABANDONED)
                cancellation.assert_no_result_after_cancel(result_published=False)
                return AttemptOutcome.ABANDONED

            handler = self.handlers.get(str(work.job.kind))
            if handler is None:
                finish_exact(
                    outcome=AttemptOutcome.FAILED,
                    failure_category=FailureCategory.POLICY,
                )
                return AttemptOutcome.FAILED

            try:
                result_digest = handler(work)
            except Exception as exc:
                finish_exact(
                    outcome=AttemptOutcome.FAILED,
                    failure_category=FailureCategory.ADAPTER,
                    result_digest=digest({"error": type(exc).__name__}),
                )
                return AttemptOutcome.FAILED

            # Terminal receipt'i olmayan claim varsa finish reddeder.
            finish_exact(AttemptOutcome.SUCCEEDED, result_digest=result_digest)
            return AttemptOutcome.SUCCEEDED
        finally:
            self._active -= 1


def build_worker(
    connection: Any,
    realm_id: UUID,
    *,
    settings: WorkerSettings,
    handlers: dict[str, Handler] | None = None,
    scheduled_handlers: dict[str, ScheduledHandler] | None = None,
    with_scheduler: bool = True,
    allow_empty_handlers: bool = False,
    consume_queue: bool = True,
) -> Worker:
    """Worker'i kanonik baglantilarla kurar."""

    resolved_handlers = dict(handlers or {})
    if consume_queue and not resolved_handlers and not allow_empty_handlers:
        raise PolicyViolation("Worker en az bir explicit handler ile baslatilmali")
    if not consume_queue and resolved_handlers:
        raise PolicyViolation("Scheduled-only worker queue handler kabul etmez")
    host = ExecutionHost(connection, realm_id, worker_label=settings.worker_label)
    gateway = SchedulerGateway(connection, realm_id) if with_scheduler else None
    return Worker(
        host=host,
        settings=settings,
        scheduler=gateway,
        handlers=resolved_handlers,
        scheduled_handlers=dict(scheduled_handlers or {}),
        consume_queue=consume_queue,
    )


def default_capabilities() -> tuple[str, ...]:
    """Yerlesik worker'in beyan ettigi yetenekler."""

    return ("sandbox.write", "knowledge.ingest", "report.render")


def run_codex_lifecycle_once(
    connection: Any,
    realm_id: UUID,
    *,
    home: Path,
    settings: WorkerSettings,
) -> str | None:
    """Recover a committed ACK or claim one exact Codex lifecycle queue job."""

    from zekam.application.client_lifecycle_composition import (
        compose_codex_lifecycle_handler,
        recover_committed_codex_delivery,
    )
    from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
    from zekam.domain.runtime import JobState
    from zekam.infrastructure.postgres.client_lifecycle_repository import (
        ClientLifecycleRepository,
    )

    if settings.capabilities != ("client.lifecycle.codex-drain",):
        raise PolicyViolation("Codex lifecycle worker exact tek capability ister")
    repository = ClientLifecycleRepository(connection, realm_id)
    spool = ClientLifecycleSpool(home, client_id="codex")
    pending = spool.pending(limit=1)
    if pending and repository.committed_admission_exists(pending[0].entry_digest):
        recovered = recover_committed_codex_delivery(spool=spool, repository=repository)
        if recovered is None or recovered.canonical_ack_digest is None:
            raise PolicyViolation("Committed Codex lifecycle local ACK recovery basarisiz")
        return str(recovered.canonical_ack_digest)
    job_id = repository.next_codex_lifecycle_job_id()
    if job_id is None:
        return None
    host = ExecutionHost(connection, realm_id, worker_label=settings.worker_label)
    job = host.jobs.get(job_id)
    work_item_id = job.work_item_id
    plan_id = job.plan_id
    step_id = job.step_id
    assignment_id = job.assignment_id
    run_id = job.run_id
    if (
        work_item_id is None
        or plan_id is None
        or step_id is None
        or assignment_id is None
        or run_id is None
    ):
        raise PolicyViolation("Codex lifecycle exact queue identity eksik")
    work = host.jobs.claim_exact(
        job.id,
        project_id=job.project_id,
        work_item_id=work_item_id,
        plan_id=plan_id,
        step_id=step_id,
        assignment_id=assignment_id,
        run_id=run_id,
        capabilities=settings.capabilities,
        worker_label=settings.worker_label,
        lease_seconds=settings.lease_seconds,
    )
    from zekam.application.client_runtime_bootstrap import (
        ClaimedLifecycleBootstrapService,
    )

    try:
        ClaimedLifecycleBootstrapService(connection, realm_id).bind_child_envelope(
            work,
            now=dt.datetime.now(dt.UTC),
        )
        handler = compose_codex_lifecycle_handler(
            connection=connection,
            realm_id=realm_id,
            home=home,
        )
        return handler(work)
    except Exception:
        current = host.jobs.get(job.id)
        if current.state is JobState.RUNNING:
            claims = host.ledger.claims_for_job(job.id)
            outcome = AttemptOutcome.RECOVERY_REQUIRED if claims else AttemptOutcome.FAILED
            finished = host.finish(
                work,
                outcome=outcome,
                failure_category=(None if claims else FailureCategory.ADAPTER),
                result_digest=digest(
                    {
                        "schema": "zekam-codex-lifecycle-worker-failure/v1",
                        "job_id": str(job.id),
                        "receiptless_claim": bool(claims),
                    }
                ),
            )
            if not finished:
                raise PolicyViolation("Codex lifecycle worker terminal finish reddedildi") from None
        raise


def run_codex_lifecycle_bootstrap_once(
    connection: Any,
    realm_id: UUID,
    *,
    home: Path,
    settings: WorkerSettings,
) -> str | None:
    """Claim and materialize one exact governed Codex lifecycle parent job.

    The parent is a separate database-write mutation.  It never executes the
    lifecycle hook; it only produces the immutable child job and the canonical
    runtime records that the child must bind to after its own claim.
    """

    from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
    from zekam.application.client_runtime_bootstrap import (
        ClaimedLifecycleBootstrapService,
    )
    from zekam.domain.runtime import JobState
    from zekam.infrastructure.postgres.lifecycle_runtime_template_repository import (
        LifecycleRuntimeTemplateRepository,
    )

    if settings.capabilities != ("client.lifecycle.codex-bootstrap",):
        raise PolicyViolation("Codex lifecycle bootstrap worker exact tek capability ister")
    spool = ClientLifecycleSpool(home, client_id="codex")
    pending = spool.pending(limit=1)
    if not pending:
        return None
    entry = pending[0]
    repository = LifecycleRuntimeTemplateRepository(connection, realm_id)
    job_id = repository.next_bootstrap_job_id()
    if job_id is None:
        return None
    host = ExecutionHost(connection, realm_id, worker_label=settings.worker_label)
    job = host.jobs.get(job_id)
    if (
        job.work_item_id is None
        or job.plan_id is None
        or job.step_id is None
        or job.assignment_id is None
        or job.run_id is None
    ):
        raise PolicyViolation("Codex lifecycle bootstrap exact queue identity eksik")
    if job.payload.get("entry_digest") != entry.entry_digest:
        raise PolicyViolation("Codex lifecycle bootstrap spool head drift")
    # Template readiness is an admission precondition.  Fail before creating a
    # job attempt/lease/effect claim so missing control-plane state cannot strand
    # a receiptless parent mutation.
    repository.current_for_bootstrap_job(job.id)
    work = host.jobs.claim_exact(
        job.id,
        project_id=job.project_id,
        work_item_id=job.work_item_id,
        plan_id=job.plan_id,
        step_id=job.step_id,
        assignment_id=job.assignment_id,
        run_id=job.run_id,
        capabilities=settings.capabilities,
        worker_label=settings.worker_label,
        lease_seconds=settings.lease_seconds,
    )
    service = ClaimedLifecycleBootstrapService(connection, realm_id)
    try:
        return service.materialize(work, home=home, now=dt.datetime.now(dt.UTC))
    except Exception:
        current = host.jobs.get(job.id)
        if current.state is JobState.RUNNING:
            claims = host.ledger.claims_for_job(job.id)
            outcome = AttemptOutcome.RECOVERY_REQUIRED if claims else AttemptOutcome.FAILED
            finished = host.finish(
                work,
                outcome=outcome,
                failure_category=(None if claims else FailureCategory.ADAPTER),
                result_digest=digest(
                    {
                        "schema": "zekam-codex-lifecycle-bootstrap-worker-failure/v1",
                        "job_id": str(job.id),
                        "receiptless_claim": bool(claims),
                    }
                ),
            )
            if not finished:
                raise PolicyViolation(
                    "Codex lifecycle bootstrap worker terminal finish reddedildi"
                ) from None
        raise


def run_projection_close_once(
    connection: Any,
    realm_id: UUID,
    *,
    settings: WorkerSettings,
) -> str | None:
    """Claim one verified staged pre-close and atomically complete its Work."""

    from zekam.application.projection_close_runtime import ProjectionCloseRuntimeService
    from zekam.domain.runtime import JobState

    if settings.capabilities != ("client.lifecycle.projection-close",):
        raise PolicyViolation("Projection close worker exact tek capability ister")
    service = ProjectionCloseRuntimeService(connection, realm_id)
    job_id = service.next_ready_job_id()
    if job_id is None:
        return None
    service.assert_release_ready(job_id)
    host = ExecutionHost(connection, realm_id, worker_label=settings.worker_label)
    job = host.jobs.get(job_id)
    if any(
        value is None
        for value in (
            job.work_item_id,
            job.plan_id,
            job.step_id,
            job.assignment_id,
            job.run_id,
        )
    ):
        raise PolicyViolation("Projection close exact queue identity eksik")
    work = host.jobs.claim_exact(
        job.id,
        project_id=job.project_id,
        work_item_id=job.work_item_id,
        plan_id=job.plan_id,
        step_id=job.step_id,
        assignment_id=job.assignment_id,
        run_id=job.run_id,
        capabilities=settings.capabilities,
        worker_label=settings.worker_label,
        lease_seconds=settings.lease_seconds,
    )
    try:
        return service.execute(work, now=dt.datetime.now(dt.UTC))
    except Exception:
        current = host.jobs.get(job.id)
        if current.state is JobState.RUNNING:
            claims = host.ledger.claims_for_job(job.id)
            outcome = AttemptOutcome.RECOVERY_REQUIRED if claims else AttemptOutcome.FAILED
            finished = host.finish(
                work,
                outcome=outcome,
                failure_category=(None if claims else FailureCategory.ADAPTER),
                result_digest=digest(
                    {
                        "schema": "zekam-projection-close-worker-failure/v1",
                        "job_id": str(job.id),
                        "receiptless_claim": bool(claims),
                    }
                ),
            )
            if not finished:
                raise PolicyViolation(
                    "Projection close worker terminal finish reddedildi"
                ) from None
        raise


def run_codex_runtime_once(
    connection: Any,
    realm_id: UUID,
    *,
    home: Path,
    worker_label: str = "codex-lifecycle-worker",
    lease_seconds: int = 30,
) -> str | None:
    """Run the reviewed parent bootstrap first, then its exact lifecycle child."""

    parent_result = run_codex_lifecycle_bootstrap_once(
        connection,
        realm_id,
        home=home,
        settings=WorkerSettings(
            worker_label=f"{worker_label}-bootstrap",
            capabilities=("client.lifecycle.codex-bootstrap",),
            lease_seconds=lease_seconds,
            max_iterations=1,
        ),
    )
    child_result = run_codex_lifecycle_once(
        connection,
        realm_id,
        home=home,
        settings=WorkerSettings(
            worker_label=worker_label,
            capabilities=("client.lifecycle.codex-drain",),
            lease_seconds=lease_seconds,
            max_iterations=1,
        ),
    )
    close_result = run_projection_close_once(
        connection,
        realm_id,
        settings=WorkerSettings(
            worker_label=f"{worker_label}-close",
            capabilities=("client.lifecycle.projection-close",),
            lease_seconds=lease_seconds,
            max_iterations=1,
        ),
    )
    return close_result or child_result or parent_result


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
