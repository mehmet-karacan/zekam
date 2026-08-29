"""Read-only PostgreSQL collector for the 15-dimension memory health report."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from zekam.application.memory_hooks import memory_hook_bundle
from zekam.application.memory_observability import (
    REQUIRED_MEMORY_DIMENSIONS,
    MemoryContinuityHealthReport,
    MemoryDimensionStatus,
    MemoryHealthDimension,
)
from zekam.application.memory_policy import load_memory_policy
from zekam.application.source_security import apply_secret_scan_allowlist, scan_git_security
from zekam.application.source_security_policy import load_secret_scan_allowlist
from zekam.domain.hook_runtime import HookEventType

_CONTINUITY_EVENTS = tuple(
    item.value for item in HookEventType if "_" in item.value and "." not in item.value
)


def _passed(
    dimension_id: str, summary: str, evidence_ref: str, count: int = 0
) -> MemoryHealthDimension:
    return MemoryHealthDimension(
        dimension_id,
        MemoryDimensionStatus.PASSED,
        summary,
        evidence_ref,
        count,
    )


def _unhealthy(
    dimension_id: str,
    summary: str,
    evidence_ref: str,
    count: int,
    code: str,
    action: str,
    *,
    failed: bool = False,
) -> MemoryHealthDimension:
    return MemoryHealthDimension(
        dimension_id,
        MemoryDimensionStatus.FAILED if failed else MemoryDimensionStatus.DEGRADED,
        summary,
        evidence_ref,
        count,
        code,
        action,
    )


def _unavailable(dimension_id: str) -> MemoryHealthDimension:
    return MemoryHealthDimension(
        dimension_id,
        MemoryDimensionStatus.UNAVAILABLE,
        "Kanonik memory component okunamadi",
        "db:continuity/unavailable",
        0,
    )


@dataclass(frozen=True, slots=True)
class PostgresMemoryHealthReader:
    connection: Any
    core_path: Path
    private_store_path: Path
    realm_id: UUID

    def collect(self, *, now: dt.datetime | None = None) -> MemoryContinuityHealthReport:
        moment = now or dt.datetime.now(dt.UTC)
        scope = "current-realm"
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("select to_regclass('continuity.feature_policy_state')")
                component_available = cursor.fetchone()[0] is not None
        except Exception:
            component_available = False
        if not component_available:
            unavailable_dimensions = [_unavailable(item) for item in REQUIRED_MEMORY_DIMENSIONS]
            unavailable_dimensions[0] = _unhealthy(
                "migration-component-state",
                "Memory Continuity migration/component mevcut degil",
                "db:core.schema_migrations/0055",
                1,
                "memory.migration-component-missing",
                "Verified rollback noktasiyla migration 0055 planini yeniden dogrulayin",
            )
            return MemoryContinuityHealthReport(tuple(unavailable_dimensions), moment, scope)

        dimensions = (
            self._migration_component(),
            self._required_hooks(),
            self._origin_recursion_guard(),
            self._hydration(),
            self._close_compaction(),
            self._gaps_recovery(),
            self._compiler_backlog(moment),
            self._quarantine(),
            self._projection(moment),
            self._omissions(),
            self._claim_without_receipt(),
            self._git_security(),
            self._private_store(),
            self._review_debt(moment),
            self._feature_mode(),
        )
        return MemoryContinuityHealthReport(dimensions, moment, scope)

    def _count(self, query: str, parameters: tuple[Any, ...] = ()) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(query, parameters or None)
            row = cursor.fetchone()
        return int(0 if row is None or row[0] is None else row[0])

    def _migration_component(self) -> MemoryHealthDimension:
        with self.connection.cursor() as cursor:
            cursor.execute("select coalesce(max(version),0) from core.schema_migrations")
            head = int(cursor.fetchone()[0])
            cursor.execute(
                "select count(*) from continuity.feature_policy_state"
                " where realm_id=%s and component='memory-continuity-plane' and is_current",
                (self.realm_id,),
            )
            states = int(cursor.fetchone()[0])
        if head < 55 or states == 0:
            return _unhealthy(
                "migration-component-state",
                "Migration veya current component state eksik",
                "db:continuity.feature_policy_state/current",
                max(1, states),
                "memory.component-state-incomplete",
                (
                    "Migration ledger ve feature-policy revision zincirini "
                    "salt okunur yeniden denetleyin"
                ),
                failed=True,
            )
        return _passed(
            "migration-component-state",
            f"Migration head {head}; current component state mevcut",
            "db:continuity.feature_policy_state/current",
            states,
        )

    def _required_hooks(self) -> MemoryHealthDimension:
        expected_bundle = memory_hook_bundle(self.realm_id)
        expected = {
            spec.event_type.value: (spec.hook_digest, runtime.runtime_digest)
            for spec, runtime in zip(
                expected_bundle.specs, expected_bundle.runtimes, strict=True
            )
        }
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select spec.event_type,count(*),min(spec.hook_digest),"
                " min(runtime.runtime_digest)"
                " from hooks.current_generation current_set"
                " join hooks.compiled_set_entry entry"
                " on entry.realm_id=current_set.realm_id"
                " and entry.compiled_set_id=current_set.compiled_set_id"
                " join hooks.spec_revision spec"
                " on spec.realm_id=entry.realm_id and spec.id=entry.spec_revision_id"
                " join hooks.runtime_revision runtime"
                " on runtime.realm_id=entry.realm_id"
                " and runtime.id=entry.runtime_revision_id"
                " where current_set.realm_id=%s and spec.required"
                " and entry.runtime_revision_id is not null"
                " and entry.disabled_reason is null and spec.event_type=any(%s)"
                " group by spec.event_type",
                (self.realm_id, list(_CONTINUITY_EVENTS)),
            )
            observed = {
                str(row[0]): (int(row[1]), str(row[2]), str(row[3]))
                for row in cursor.fetchall()
            }
        invalid = sum(
            observed.get(event, (0, "", ""))
            != (1, expected[event][0], expected[event][1])
            for event in _CONTINUITY_EVENTS
        )
        if invalid:
            return _unhealthy(
                "required-hook-runtime",
                f"{invalid} lifecycle eventi exact code revision handler tasimiyor",
                "db:hooks.current_generation/continuity",
                invalid,
                "memory.required-hook-revision-drift",
                (
                    "Exact memory hook upgrade planini uretin; unrelated hook'lari "
                    "koruyarak governed generation aktivasyonu uygulayin"
                ),
                failed=True,
            )
        return _passed(
            "required-hook-runtime",
            f"{len(_CONTINUITY_EVENTS)} lifecycle eventi exact-one handler tasiyor",
            "db:hooks.current_generation/continuity",
            len(_CONTINUITY_EVENTS),
        )

    def _origin_recursion_guard(self) -> MemoryHealthDimension:
        # Migration limits recursion depth and the application admission validates
        # origin/client binding before a lifecycle event reaches persistence.
        constraints = self._count(
            "select count(*) from pg_constraint c join pg_class t on t.oid=c.conrelid"
            " join pg_namespace n on n.oid=t.relnamespace"
            " where n.nspname='continuity' and t.relname='session_lifecycle_event'"
            " and pg_get_constraintdef(c.oid) like '%recursion_depth%16%'"
        )
        if constraints != 1:
            return _unhealthy(
                "origin-recursion-guard",
                "Lifecycle recursion persistence guard eksik",
                "db:continuity.session_lifecycle_event/recursion-constraint",
                constraints,
                "memory.recursion-guard-missing",
                "Migration constraint ve client lifecycle admission testini yeniden calistirin",
                failed=True,
            )
        return _passed(
            "origin-recursion-guard",
            "Origin admission ve recursion depth guard kayitli",
            "code:client-lifecycle-bridge/v1",
            1,
        )

    def _hydration(self) -> MemoryHealthDimension:
        invalid = self._count(
            "select count(*) from (select distinct on"
            " (realm_id,project_id,work_item_id,run_id,session_id,client_id) fresh,complete"
            " from continuity.session_hydration_receipt where realm_id=%s"
            " order by realm_id,project_id,"
            " work_item_id,run_id,session_id,client_id,created_at desc,id desc) latest"
            " where not fresh or not complete",
            (self.realm_id,),
        )
        if invalid:
            return _unhealthy(
                "hydration-freshness-completeness",
                f"{invalid} latest hydration stale veya incomplete",
                "db:continuity.session_hydration_receipt/latest",
                invalid,
                "memory.hydration-not-current",
                "Hydration prepare planini current source/policy/migration digestleriyle yenileyin",
                failed=True,
            )
        total = self._count(
            "select count(*) from continuity.session_hydration_receipt where realm_id=%s",
            (self.realm_id,),
        )
        return _passed(
            "hydration-freshness-completeness",
            "Latest hydration receipt'leri fresh ve complete",
            "db:continuity.session_hydration_receipt/latest",
            total,
        )

    def _close_compaction(self) -> MemoryHealthDimension:
        invalid = self._count(
            "select (select count(*) from continuity.session_close_receipt"
            " where realm_id=%s and close_status in ('recovery-required','failed'))"
            " + (select count(*) from continuity.compaction_receipt"
            " where realm_id=%s and status in ('prepared','recovery-required','failed'))",
            (self.realm_id, self.realm_id),
        )
        if invalid:
            return _unhealthy(
                "close-compaction-completeness",
                f"{invalid} close/compaction terminal zinciri eksik veya failed",
                "db:continuity.close-compaction/terminal",
                invalid,
                "memory.close-compaction-incomplete",
                "Gap raporundan exact repair plani olusturun; sessiz retry yapmayin",
                failed=True,
            )
        total = self._count(
            "select (select count(*) from continuity.session_close_receipt where realm_id=%s)"
            " + (select count(*) from continuity.compaction_receipt where realm_id=%s)",
            (self.realm_id, self.realm_id),
        )
        return _passed(
            "close-compaction-completeness",
            "Close ve compaction receipt zincirlerinde acik terminal bosluk yok",
            "db:continuity.close-compaction/terminal",
            total,
        )

    def _gaps_recovery(self) -> MemoryHealthDimension:
        count = self._count(
            "select (select count(*) from continuity.gap_recovery_reference"
            " where realm_id=%s and state<>'resolved')"
            " + (select count(*) from runtime.job"
            " where realm_id=%s and state='recovery-required')",
            (self.realm_id, self.realm_id),
        )
        if count:
            return _unhealthy(
                "continuity-gaps-recovery",
                f"{count} continuity gap veya recovery-required job acik",
                "db:continuity.gap_recovery_reference/open",
                count,
                "memory.continuity-gap-open",
                "Exact evidence ref ile repair prepare edin; authorization olmadan apply etmeyin",
                failed=True,
            )
        return _passed(
            "continuity-gaps-recovery",
            "Acik continuity gap veya recovery-required job yok",
            "db:continuity.gap_recovery_reference/open",
        )

    def _compiler_backlog(self, now: dt.datetime) -> MemoryHealthDimension:
        count = self._count(
            "select count(*) from memory.compiler_watermark_claim"
            " where realm_id=%s and (state='recovery-required'"
            " or (state in ('pending','processing') and claimed_at<%s))",
            (self.realm_id, now - dt.timedelta(hours=1)),
        )
        if count:
            return _unhealthy(
                "compiler-watermark-backlog",
                f"{count} compiler watermark claim stale/recovery-required",
                "db:memory.compiler_watermark_claim/open",
                count,
                "memory.compiler-watermark-stalled",
                "Watermark input snapshotini ve terminal receipt'i denetleyip recovery planlayin",
                failed=True,
            )
        return _passed(
            "compiler-watermark-backlog",
            "Compiler watermark lock/backlog saglikli",
            "db:memory.compiler_watermark_claim/open",
        )

    def _quarantine(self) -> MemoryHealthDimension:
        count = self._count(
            "select count(*) from memory.compiler_candidate"
            " where realm_id=%s and state='quarantined'",
            (self.realm_id,),
        )
        if count:
            return _unhealthy(
                "quarantined-candidates",
                f"{count} compiler candidate quarantine incelemesi bekliyor",
                "db:memory.compiler_candidate/quarantined",
                count,
                "memory.compiler-quarantine-pending",
                "Quarantine evidence'ini bagimsiz reviewer ile inceleyin; otomatik promote etmeyin",
            )
        return _passed(
            "quarantined-candidates",
            "Quarantine incelemesi bekleyen compiler candidate yok",
            "db:memory.compiler_candidate/quarantined",
        )

    def _projection(self, now: dt.datetime) -> MemoryHealthDimension:
        count = self._count(
            "select count(*) from continuity.projection_generation_receipt where realm_id=%s",
            (self.realm_id,),
        )
        stale = self._count(
            "select count(*) from (select distinct on (receipt.realm_id,receipt.projection_ref)"
            " receipt.realm_id,receipt.work_item_id,receipt.generated_at"
            " from continuity.projection_generation_receipt receipt"
            " where receipt.realm_id=%s"
            " order by receipt.realm_id,receipt.projection_ref,"
            " receipt.generated_at desc,receipt.id desc) latest"
            " join work.work_item item on item.realm_id=latest.realm_id"
            " and item.id=latest.work_item_id"
            " where item.state=any(%s) and latest.generated_at<%s",
            (self.realm_id, ["active", "verification"], now - dt.timedelta(hours=24)),
        )
        legacy_stale = self._legacy_projection_stale_count()
        if count == 0 or stale or legacy_stale:
            observed = max(1, stale + legacy_stale)
            return _unhealthy(
                "projection-freshness",
                "Projection receipt yok/stale veya legacy root projection parity disinda",
                "db:continuity.projection_generation_receipt/latest",
                observed,
                "memory.projection-stale",
                "Kanonik source digest ile shadow projection uretip diff/receipt dogrulayin",
            )
        return _passed(
            "projection-freshness",
            "Projection generation receipt'leri freshness esigi icinde",
            "db:continuity.projection_generation_receipt/latest",
            count,
        )

    def _legacy_projection_stale_count(self) -> int:
        """Detect old root projections without exposing their content or path."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id::text from work.work_item"
                " where realm_id=%s and title='Zekam Memory Continuity Plane'"
                " and state='active'"
                " order by updated_at desc,id desc limit 1",
                (self.realm_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return 0
        work_id = str(row[0])
        stale = 0
        for name in ("AKTIF_GOREV.yaml", "AKTIF_GOREV.md"):
            candidate = self.core_path / name
            try:
                if not candidate.is_file() or work_id not in candidate.read_text(encoding="utf-8"):
                    stale += 1
            except OSError:
                stale += 1
        return stale

    def _omissions(self) -> MemoryHealthDimension:
        count = self._count(
            "select coalesce(sum(jsonb_array_length(receipt_body->'omissions')),0)"
            " from continuity.session_hydration_receipt where realm_id=%s",
            (self.realm_id,),
        )
        if count:
            return _unhealthy(
                "context-omissions",
                f"{count} bounded optional context omission kayitli",
                "db:continuity.session_hydration_receipt/omissions",
                count,
                "memory.context-omission-present",
                "Omission reason ve token budget'i inceleyin; required omission'i reddedin",
            )
        return _passed(
            "context-omissions",
            "Kayitli context omission/truncation yok",
            "db:continuity.session_hydration_receipt/omissions",
        )

    def _claim_without_receipt(self) -> MemoryHealthDimension:
        count = self._count(
            "select count(*) from runtime.claim_without_receipt where realm_id=%s",
            (self.realm_id,),
        )
        if count:
            return _unhealthy(
                "claim-without-receipt",
                f"{count} effect claim terminal receipt bekliyor",
                "db:runtime.claim_without_receipt/current",
                count,
                "memory.claim-without-receipt",
                "Claim'i sessiz retry etmeden recovery-required akisi ile uzlastirin",
                failed=True,
            )
        return _passed(
            "claim-without-receipt",
            "Terminal receipt bekleyen effect claim yok",
            "db:runtime.claim_without_receipt/current",
        )

    def _git_security(self) -> MemoryHealthDimension:
        try:
            report = apply_secret_scan_allowlist(
                scan_git_security(self.core_path, include_history=True),
                load_secret_scan_allowlist(),
            )
        except Exception:
            return _unhealthy(
                "tracked-secret-public-leak",
                "Git secret/backup/public-leak taramasi calistirilamadi",
                "git:security-report/current",
                1,
                "memory.git-security-scan-failed",
                "Hermetic tracked/index/history taramasini salt okunur yeniden calistirin",
                failed=True,
            )
        if not report.passed:
            return _unhealthy(
                "tracked-secret-public-leak",
                f"{len(report.findings)} Git security bulgusu veya incomplete history",
                "git:security-report/current",
                max(1, len(report.findings)),
                "memory.git-security-blocker",
                "Secret ise revoke/rotate edin; history rewrite'i ayri high-risk planlayin",
                failed=True,
            )
        return _passed(
            "tracked-secret-public-leak",
            "Tracked/index/history secret ve backup gate temiz",
            "git:security-report/current",
            report.reviewed_allowance_count,
        )

    def _private_store(self) -> MemoryHealthDimension:
        try:
            policy = load_memory_policy()
            private_ok = (
                self.private_store_path.is_dir() and not self.private_store_path.is_symlink()
            )
            retention_ok = all(
                item.retention_days is None or 1 <= item.retention_days <= 3650
                for item in policy.classifications
            )
        except Exception:
            private_ok = False
            retention_ok = False
        if not private_ok or not retention_ok:
            return _unhealthy(
                "private-store-retention",
                "Private store veya retention policy dogrulanamadi",
                "policy:memory-continuity/private-retention",
                1,
                "memory.private-store-policy-invalid",
                "Private store ownership ve classification retention policy'sini dogrulayin",
                failed=True,
            )
        return _passed(
            "private-store-retention",
            "Private store ve bounded retention policy dogrulandi",
            "policy:memory-continuity/private-retention",
            len(policy.classifications),
        )

    def _review_debt(self, now: dt.datetime) -> MemoryHealthDimension:
        threshold = now - dt.timedelta(days=30)
        count = self._count(
            "select (select count(*) from memory.compiler_candidate"
            " where realm_id=%s and state in ('candidate','reviewed') and created_at<%s)"
            " + (select count(*) from skills.skill"
            " where realm_id=%s and state in ('candidate','evaluated') and created_at<%s)",
            (self.realm_id, threshold, self.realm_id, threshold),
        )
        if count:
            return _unhealthy(
                "stale-review-debt",
                f"{count} stale memory/skill review kaydi bekliyor",
                "db:memory-skills/review-debt",
                count,
                "memory.review-debt-stale",
                "Stale adaylari evidence/ref ile review, supersede veya quarantine edin",
            )
        return _passed(
            "stale-review-debt",
            "Stale memory/skill review borcu yok",
            "db:memory-skills/review-debt",
        )

    def _feature_mode(self) -> MemoryHealthDimension:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select state,count(*) from continuity.feature_policy_state"
                " where realm_id=%s and component='memory-continuity-plane'"
                " and is_current group by state",
                (self.realm_id,),
            )
            rows = tuple((str(row[0]), int(row[1])) for row in cursor.fetchall())
        modes = {row[0] for row in rows}
        count = sum(row[1] for row in rows)
        if not modes or not modes <= {"disabled", "shadow", "enforced"}:
            return _unhealthy(
                "feature-mode",
                "Memory feature mode current state gecersiz",
                "db:continuity.feature_policy_state/mode",
                max(1, count),
                "memory.feature-mode-invalid",
                "Feature policy revision zincirini denetleyip additive replan olusturun",
                failed=True,
            )
        return _passed(
            "feature-mode",
            "Current mode: " + ",".join(sorted(modes)),
            "db:continuity.feature_policy_state/mode",
            count,
        )
