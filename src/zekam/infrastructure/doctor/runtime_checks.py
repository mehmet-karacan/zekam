"""Kuyruk, istemci, model ve policy doctor kontrolleri.

Kontroller salt okunurdur: kuyruktan is almaz, model cagirmaz, policy degistirmez.
Kanonik tablo yoksa kontrol `skipped` doner — sahte `passed` uretmez.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zekam.application.config import DatabaseSettings
from zekam.application.diagnostics import CheckResult, CheckStatus, Finding, Severity
from zekam.application.opencode_spool import inspect_spool
from zekam.domain.model_inventory import CANONICAL_MODEL_COUNT
from zekam.domain.observability import CANONICAL_COMMANDS, missing_commands
from zekam.domain.scheduler import REQUIRED_JOBS, missing_required_jobs
from zekam.infrastructure.postgres.connection import PSYCOPG_AVAILABLE, connect

CATEGORY = "runtime"

#: Doctor realm kapsami olmadan baglanir. Bu bilinclidir: operasyonel saglik
#: sorusu "bu kurulumda envanter/policy var mi" seklindedir, tek bir realm'in
#: durumu degildir. Sayilar bu yuzden **butun realm'leri** kapsar ve kanit
#: alaninda boyle isaretlenir.
CROSS_REALM = True

#: Kuyruk derinligi bu esigin uzerindeyse bilgilendirici uyari uretilir. Normal
#: ready/running backlog tek basina recovery veya saglik arizasi degildir.
QUEUE_DEPTH_WARNING = 100

#: Recovery bekleyen is sayisi sifirdan buyukse dikkat gerekir.
RECOVERY_ATTENTION = 0

_RESOLVED_CAMPAIGN_RECOVERY_COUNT_SQL = """
select count(*)
from runtime.job parent_job
where parent_job.state = 'recovery-required'
  and not exists (
      select 1 from runtime.claim_without_receipt pending
      where pending.job_id = parent_job.id
  )
  and exists (
      select 1
      from models.opencode_benchmark_campaign parent_campaign
      join models.opencode_benchmark_campaign child_campaign
        on child_campaign.realm_id = parent_campaign.realm_id
       and child_campaign.parent_campaign_id = parent_campaign.id
       and child_campaign.work_item_id = parent_campaign.work_item_id
      join models.opencode_benchmark_campaign_outcome parent_outcome
        on parent_outcome.realm_id = parent_campaign.realm_id
       and parent_outcome.campaign_id = parent_campaign.id
       and parent_outcome.status = 'recovery-required'
      join models.opencode_benchmark_campaign_outcome child_outcome
        on child_outcome.realm_id = child_campaign.realm_id
       and child_outcome.campaign_id = child_campaign.id
       and child_outcome.status in ('passed', 'failed')
      join work.work_item campaign_work
        on campaign_work.realm_id = child_campaign.realm_id
       and campaign_work.id = child_campaign.work_item_id
      join runtime.job child_job
        on child_job.realm_id = child_campaign.realm_id
       and child_job.plan_id = child_campaign.task_plan_id
       and child_job.work_item_id = child_campaign.work_item_id
       and child_job.project_id = campaign_work.project_id
       and child_job.step_id = 'campaign-finalize'
       and child_job.kind = 'provider-call'
       and child_job.state = 'completed'
      join work.checkpoint checkpoint
        on checkpoint.realm_id = child_job.realm_id
       and checkpoint.job_id = child_job.id
       and checkpoint.project_id = child_job.project_id
       and checkpoint.work_item_id = child_campaign.work_item_id
       and checkpoint.task_plan_id = child_campaign.task_plan_id
       and checkpoint.source_revision = child_campaign.source_revision
       and cardinality(checkpoint.pending_steps) = 0
       and not checkpoint.grants_authority
      where parent_campaign.realm_id = parent_job.realm_id
        and parent_campaign.task_plan_id = parent_job.plan_id
        and parent_campaign.work_item_id = parent_job.work_item_id
        and parent_job.project_id = campaign_work.project_id
        and parent_job.step_id = 'campaign-finalize'
        and parent_job.kind = 'provider-call'
  )
"""

_RECOVERY_BREAKDOWN_SQL = """
select
    count(*) filter (where claim_count = 0) as no_claim,
    count(*) filter (where receiptless_count > 0) as claim_without_receipt,
    count(*) filter (
        where claim_count > 0 and receiptless_count = 0 and failed_receipt_count > 0
    ) as failed_receipt,
    count(*) filter (
        where claim_count > 0 and receiptless_count = 0 and failed_receipt_count = 0
    ) as completed_receipt
from (
    select recovery_job.id,
           count(claim.id) as claim_count,
           count(claim.id) filter (where receipt.id is null) as receiptless_count,
           count(receipt.id) filter (where receipt.status = 'failed') as failed_receipt_count
    from runtime.job recovery_job
    left join runtime.effect_claim claim
      on claim.realm_id = recovery_job.realm_id and claim.job_id = recovery_job.id
    left join runtime.effect_receipt receipt
      on receipt.realm_id = claim.realm_id and receipt.claim_id = claim.id
    where recovery_job.state = 'recovery-required'
    group by recovery_job.id
) recovery
"""


def _resolved_campaign_recovery_count(connection: Any) -> int:
    """Terminal continuation ile exact cozulmus tarihsel recovery sayisi."""

    with connection.cursor() as cursor:
        cursor.execute(_RESOLVED_CAMPAIGN_RECOVERY_COUNT_SQL)
        return int(cursor.fetchone()[0])


def _recovery_breakdown(connection: Any) -> dict[str, int]:
    """Recovery state'lerini claim/receipt kanitina gore ayirir."""

    with connection.cursor() as cursor:
        cursor.execute(_RECOVERY_BREAKDOWN_SQL)
        row = cursor.fetchone()
    if row is None:
        return {
            "no_claim": 0,
            "claim_without_receipt": 0,
            "failed_receipt": 0,
            "completed_receipt": 0,
        }
    return {
        "no_claim": int(row[0] or 0),
        "claim_without_receipt": int(row[1] or 0),
        "failed_receipt": int(row[2] or 0),
        "completed_receipt": int(row[3] or 0),
    }


def _unavailable(check_id: str, detail: str) -> CheckResult:
    """Kanonik kaynak okunamadiginda dogru cevap `skipped`'tir."""

    return CheckResult(
        check_id=check_id,
        category=CATEGORY,
        status=CheckStatus.SKIPPED,
        summary="Kanonik kayit okunamadi; kontrol atlandi",
        evidence={"reason": detail},
    )


def _scalar(settings: DatabaseSettings, query: str, *parameters: Any) -> Any:
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(query, parameters or None)
        row = cursor.fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True, slots=True)
class QueueCheck:
    """Durable queue derinligini ve recovery bekleyen isleri raporlar."""

    settings: DatabaseSettings
    check_id: str = "runtime.queue"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        if not PSYCOPG_AVAILABLE:
            return _unavailable(self.check_id, "psycopg yok")
        try:
            with connect(self.settings) as connection, connection.cursor() as cursor:
                cursor.execute("select state, count(*) from runtime.job group by state")
                counts = {str(row[0]): int(row[1]) for row in cursor.fetchall()}
                resolved_recovery = _resolved_campaign_recovery_count(connection)
                raw_recovery_breakdown = _recovery_breakdown(connection)
        except Exception as exc:
            return _unavailable(self.check_id, type(exc).__name__)

        ready = counts.get("ready", 0) + counts.get("running", 0)
        recovery = counts.get("recovery-required", 0) - resolved_recovery
        if recovery < 0:
            return _unavailable(self.check_id, "resolved-recovery-count-drift")
        evidence: dict[str, Any] = {
            "by_state": counts,
            "pending": ready,
            "recovery": recovery,
            "recovery_resolved_by_continuation": resolved_recovery,
            "raw_recovery_breakdown": raw_recovery_breakdown,
            "cross_realm": CROSS_REALM,
        }

        findings: list[Finding] = []
        if recovery > RECOVERY_ATTENTION:
            findings.append(
                Finding(
                    code="runtime.recovery-required",
                    severity=Severity.ERROR,
                    title=f"{recovery} is recovery bekliyor",
                    detail=(
                        "Ham kanit siniflari: "
                        f"claim yok={raw_recovery_breakdown['no_claim']}, "
                        "claim var receipt yok="
                        f"{raw_recovery_breakdown['claim_without_receipt']}, "
                        f"failed receipt={raw_recovery_breakdown['failed_receipt']}, "
                        f"completed receipt={raw_recovery_breakdown['completed_receipt']}; "
                        f"continuation ile cozulmus={resolved_recovery}. "
                        "Her sinif kendi terminal kanitiyla uzlastirilmalidir"
                    ),
                    next_action=(
                        "Yalniz claim var receipt yok sinifi icin `zekam worker "
                        "reconcile-recovery --girdi <plan.json> --json` kullanin; diger "
                        "siniflari mevcut receipt/attempt kanitina gore uzlastirin"
                    ),
                )
            )
        if ready > QUEUE_DEPTH_WARNING:
            findings.append(
                Finding(
                    code="runtime.queue-depth",
                    severity=Severity.WARNING,
                    title=f"Kuyruk derinligi {ready}",
                    detail="Worker kapasitesi yetersiz olabilir",
                    next_action="Worker sayisini artirin veya backpressure sinirini gozden gecirin",
                )
            )
        # Normal ready/running isler kuyrugun calisma durumudur. Yalniz gercek,
        # continuation ile uzlastirilmamis recovery doctor durumunu dusurur;
        # derinlik uyarisi kapasite gorunurlugu olarak kalir.
        status = CheckStatus.DEGRADED if recovery > RECOVERY_ATTENTION else CheckStatus.PASSED
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=status,
            summary=f"kuyruk: {ready} bekleyen, {recovery} recovery",
            findings=tuple(findings),
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class ClientsCheck:
    """Kayitli istemci calistirilabilir dosyalarinin varligini dogrular."""

    executables: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    check_id: str = "runtime.clients"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        if not self.executables:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.SKIPPED,
                summary="Yapilandirilmis istemci yok",
                evidence={"configured": 0},
            )
        missing = tuple(
            name for name, path in self.executables if not Path(path).expanduser().exists()
        )
        evidence = {
            "configured": len(self.executables),
            "clients": sorted(name for name, _ in self.executables),
            "missing": list(missing),
        }
        if missing:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.DEGRADED,
                summary=f"{len(missing)} istemci bulunamadi",
                findings=(
                    Finding(
                        code="runtime.client-missing",
                        severity=Severity.WARNING,
                        title=f"Eksik istemci: {', '.join(missing)}",
                        detail="Beyan edilen calistirilabilir dosya diskte yok",
                        next_action="Yolu duzeltin veya istemciyi yapilandirmadan kaldirin",
                    ),
                ),
                evidence=evidence,
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED,
            summary=f"{len(self.executables)} istemci erisilebilir",
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class OpenCodeSpoolCheck:
    """OpenCode plugin spool backlog, lock ve legacy adaylarini salt okunur raporlar."""

    home: Path
    check_id: str = "runtime.opencode-spool"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        try:
            status = inspect_spool(self.home)
        except Exception as exc:
            return _unavailable(self.check_id, type(exc).__name__)
        evidence = status.as_dict()
        findings: list[Finding] = []
        if status.queued:
            findings.append(
                Finding(
                    code="runtime.opencode-spool-queued",
                    severity=Severity.WARNING,
                    title=f"{status.queued} lifecycle teslimati kuyrukta",
                    detail="Teslimatlar durable spool icinde terminal ACK bekliyor",
                    next_action=(
                        "OpenCode drain calistirip spool-status ile kuyrugu yeniden dogrulayin"
                    ),
                )
            )
        if status.lock_present:
            findings.append(
                Finding(
                    code="runtime.opencode-spool-lock",
                    severity=Severity.ERROR,
                    title="OpenCode drain lock mevcut",
                    detail="Lock owner state typed spool incelemesi ister",
                    next_action="OpenCode process durumunu ve spool-status kanitini inceleyin",
                )
            )
        if status.legacy_candidates:
            findings.append(
                Finding(
                    code="runtime.opencode-spool-legacy-candidates",
                    severity=Severity.WARNING,
                    title=f"{status.legacy_candidates} legacy drain adayi mevcut",
                    detail=(
                        f"Exact stale ve cleanup-eligible: {status.eligible_legacy_candidates}; "
                        f"invalid: {status.invalid_legacy_candidates}"
                    ),
                    next_action=(
                        "`zekam opencode spool-cleanup` planini inceleyip exact digest ile "
                        "yetkili typed quarantine uygulayin"
                    ),
                )
            )
        if status.unrecognized_entries:
            findings.append(
                Finding(
                    code="runtime.opencode-spool-unrecognized",
                    severity=Severity.ERROR,
                    title=f"{status.unrecognized_entries} tanimsiz spool girdisi mevcut",
                    detail="Tanimsiz girdiler otomatik temizlenmez",
                    next_action="Exact dosya tipini manuel ve salt okunur inceleyin",
                )
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED if not findings else CheckStatus.DEGRADED,
            summary=(
                f"spool: {status.queued} queued, {status.legacy_candidates} legacy candidate, "
                f"{status.quarantine} quarantine"
            ),
            findings=tuple(findings),
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class ModelInventoryCheck:
    """Model envanterinin iceri alinip alinmadigini raporlar."""

    settings: DatabaseSettings
    check_id: str = "runtime.models"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        if not PSYCOPG_AVAILABLE:
            return _unavailable(self.check_id, "psycopg yok")
        try:
            imported = int(
                _scalar(self.settings, "select count(*) from models.model_inventory") or 0
            )
        except Exception as exc:
            return _unavailable(self.check_id, type(exc).__name__)

        evidence = {
            "imported": imported,
            "canonical": CANONICAL_MODEL_COUNT,
            "cross_realm": CROSS_REALM,
        }
        if imported == 0:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.DEGRADED,
                summary="Model envanteri bos",
                findings=(
                    Finding(
                        code="runtime.models-empty",
                        severity=Severity.WARNING,
                        title="Hicbir model iceri alinmamis",
                        detail="Routing ve benchmark envanter olmadan calismaz",
                        next_action="`zekam model inventory --uygula` calistirin",
                    ),
                ),
                evidence=evidence,
            )
        if imported < CANONICAL_MODEL_COUNT:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.DEGRADED,
                summary=f"{imported}/{CANONICAL_MODEL_COUNT} model iceri alinmis",
                findings=(
                    Finding(
                        code="runtime.models-partial",
                        severity=Severity.INFO,
                        title="Envanter eksik",
                        detail="Kanonik envanterin bir kismi eksik",
                        next_action="`zekam model inventory --uygula` ile yeniden aktarin",
                    ),
                ),
                evidence=evidence,
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED,
            summary=f"{imported} model kayitli (butun realm'ler)",
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class PolicyCheck:
    """Aktif policy surumunu ve tuketilmemis authorization sayisini raporlar."""

    settings: DatabaseSettings
    check_id: str = "runtime.policy"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        if not PSYCOPG_AVAILABLE:
            return _unavailable(self.check_id, "psycopg yok")
        try:
            with connect(self.settings) as connection, connection.cursor() as cursor:
                cursor.execute("select count(*) from security.policy")
                policies = int(cursor.fetchone()[0])
                cursor.execute(
                    "select count(*) from security.authorization where consumed_at is null"
                )
                open_authorizations = int(cursor.fetchone()[0])
        except Exception as exc:
            return _unavailable(self.check_id, type(exc).__name__)

        evidence = {
            "policy_versions": policies,
            "open_authorizations": open_authorizations,
            "cross_realm": CROSS_REALM,
        }
        if policies == 0:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.DEGRADED,
                summary="Kayitli policy surumu yok",
                findings=(
                    Finding(
                        code="runtime.policy-missing",
                        severity=Severity.WARNING,
                        title="Policy tanimlanmamis",
                        detail="Risk siniflandirmasi varsayilanlarla calisir",
                        next_action="`zekam policy` ile bir surum tanimlayin",
                    ),
                ),
                evidence=evidence,
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED,
            summary=f"{policies} policy surumu, {open_authorizations} acik authorization",
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class SchedulerCheck:
    """Zorunlu bakim islerinin tanimli olup olmadigini raporlar."""

    settings: DatabaseSettings
    check_id: str = "runtime.scheduler"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        if not PSYCOPG_AVAILABLE:
            return _unavailable(self.check_id, "psycopg yok")
        try:
            with connect(self.settings) as connection, connection.cursor() as cursor:
                cursor.execute("select job_name from ops.job_definition")
                defined = tuple(str(row[0]) for row in cursor.fetchall())
        except Exception as exc:
            return _unavailable(self.check_id, type(exc).__name__)

        missing = missing_required_jobs(defined)
        evidence = {
            "defined": len(defined),
            "required": len(REQUIRED_JOBS),
            "missing": list(missing),
        }
        if missing:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.DEGRADED,
                summary=f"{len(missing)} zorunlu bakim isi tanimli degil",
                findings=(
                    Finding(
                        code="runtime.scheduler-missing",
                        severity=Severity.WARNING,
                        title=f"Eksik is: {', '.join(missing[:3])}",
                        detail="Bakim isleri tanimlanmadan worker onlari calistiramaz",
                        next_action="`zekam scheduler init --uygula` calistirin",
                    ),
                ),
                evidence=evidence,
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED,
            summary=f"{len(defined)} bakim isi tanimli",
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class CommandSurfaceCheck:
    """Kanonik komut sozlesmesinin gercekten kayitli oldugunu dogrular."""

    check_id: str = "runtime.surface"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        from zekam.interfaces.cli.main import app
        from zekam.interfaces.cli.surface import registered_commands

        available = registered_commands(app)
        missing = missing_commands(available)
        evidence = {
            "registered": len(available),
            "contract": len(CANONICAL_COMMANDS),
            "missing": list(missing),
        }
        if missing:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.FAILED,
                summary=f"{len(missing)} sozlesme komutu kayitli degil",
                findings=(
                    Finding(
                        code="runtime.surface-drift",
                        severity=Severity.ERROR,
                        title="Belge ile kod arasinda komut sapmasi",
                        detail=f"Eksik: {', '.join(missing)}",
                        next_action="Komutu kaydedin veya sozlesmeden cikarin",
                    ),
                ),
                evidence=evidence,
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED,
            summary=f"{len(CANONICAL_COMMANDS)} sozlesme komutunun tamami kayitli",
            evidence=evidence,
        )
