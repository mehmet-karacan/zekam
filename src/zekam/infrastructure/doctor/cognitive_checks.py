"""Salt okunur cognitive health doctor kontrolleri.

Bu modul Zekam'in bellek, bilgi, beceri, sureklilik, context ve ogrenme/evrim
duzlemlerindeki kurulum ve tutarlilik sorunlarini *tespit* eder. Kontroller
mutasyon yapmaz; kanonik SQLite store'lari ve git HEAD'i yalniz `mode=ro`
baglantiyla okur. Repair yalniz plan/digest/apply modeline uygun sekilde
`zekam doctor --uygula` ile ayri yetkilendirilmis bir adimdir ve burada
uretilmez.

Her bulgu frozen `Finding` tasir; guvenli sonraki adim salt okunur inceleme
referanstan oteye yetki istemez (`authority_required=False`). Sessiz
destructive cleanup YASAKTIR.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zekam.application.diagnostics import CheckResult, CheckStatus, Finding, Severity
from zekam.infrastructure.doctor.core_checks import _git_repository_value

if TYPE_CHECKING:
    from zekam.application.operational_store import ArtifactRefRecord

CATEGORY = "cognitive"

#: Acik run terminolojisi kapanmamis run siniflari (ContinuityFreshnessCheck).
_OPEN_RUN_STATES = frozenset({"planned", "running", "unknown"})

#: Memory hijyen bulgu siniflari (local_learning hygiene_proposal).
_MEMORY_HYGIENE_KINDS = frozenset(
    {"duplicate", "conflict", "stale", "supersession", "retention-review"}
)

#: Beceri manifest durumlarinda activation/hazir siniflandirmasi.
_SKILL_CANDIDATE_STATE = "candidate"

#: Source digest drift icin karsilastirma yapilmadan once gorulecek son snapshot satiri.
_SOURCE_DRIFT_LIMIT = 1

#: KnowledgeFiles.audit'in bildirdigi "kirik/kayip" bulgu siniflari.
_BROKEN_KNOWLEDGE_ISSUES = frozenset(
    {
        "missing-note",
        "missing-cas-object",
        "unreadable-note",
        "unreadable-cas-object",
        "corrupt-note",
        "corrupt-cas-object",
        "duplicate-note-ref",
        "duplicate-artifact-ref",
        "secret-in-normal-file-plane",
        "secret-in-normal-cas",
    }
)


def _readonly(path: Path) -> sqlite3.Connection:
    """Kanonik SQLite dosyasini salt okunur acar."""
    return sqlite3.connect(
        f"{path.resolve(strict=True).as_uri()}?mode=ro", uri=True, timeout=5.0
    )


def _query_scalar(connection: sqlite3.Connection, sql: str, *parameters: object) -> int:
    row = connection.execute(sql, parameters).fetchone()
    return int(row[0]) if row is not None else 0


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)
    ).fetchone()
    return row is not None


def _unavailable(check_id: str, detail: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        category=CATEGORY,
        status=CheckStatus.SKIPPED,
        summary="Kanonik kaynak okunamadi; kontrol atlandi",
        evidence={"reason": detail},
    )


def _context_git_head(core_path: Path) -> str | None:
    """Core git HEAD'ini salt okunur okur; git yoksa None."""
    executable = shutil.which("git")
    if executable is None:
        return None
    return _git_repository_value(executable, core_path, "rev-parse", "HEAD")


@dataclass(frozen=True, slots=True)
class MemoryHygieneCheck:
    """Bellek hijyenini (orphan/duplicate/conflict/stale) salt okunur raporlar."""

    context: Any
    check_id: str = "cognitive.memory"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        from zekam.infrastructure.local_core_services import LocalCoreServices

        try:
            services = LocalCoreServices.from_context(self.context)
            learning_path = services.learning.path
            with closing(_readonly(learning_path)) as db:
                db.row_factory = sqlite3.Row
                orphan = _query_scalar(
                    db,
                    "select count(*) from memory_candidate c "
                    "where not exists (select 1 from memory_revision r "
                    "where r.candidate_digest=c.candidate_digest)",
                )
                conflict_active = _query_scalar(
                    db,
                    "select count(*) from memory_relation rel "
                    "join memory_revision fr on fr.revision_digest=rel.from_revision_digest "
                    "join memory_revision tr on tr.revision_digest=rel.to_revision_digest "
                    "where rel.relation_kind='conflicts-with' "
                    "and exists (select 1 from memory_head h"
                    " where h.revision_digest=fr.revision_digest) "
                    "and exists (select 1 from memory_head h"
                    " where h.revision_digest=tr.revision_digest)",
                )
                hygiene = db.execute(
                    "select finding, count(*) as n from hygiene_proposal "
                    "group by finding"
                ).fetchall()
        except (OSError, sqlite3.DatabaseError):
            return _unavailable(self.check_id, "learning store okunamadi")

        hygiene_counts: dict[str, int] = {}
        for row in hygiene:
            kind = str(row["finding"])
            if kind in _MEMORY_HYGIENE_KINDS:
                hygiene_counts[kind] = int(row["n"])
        duplicate = hygiene_counts.get("duplicate", 0)
        stale = hygiene_counts.get("stale", 0) + hygiene_counts.get(
            "retention-review", 0
        ) + hygiene_counts.get("supersession", 0)

        findings: list[Finding] = []
        if orphan:
            findings.append(
                Finding(
                    code="cognitive.memory-orphan-candidate",
                    severity=Severity.WARNING,
                    title=f"{orphan} bellek adayi hicbir revision'a bagli degil",
                    detail="Aday review/revision zincirinin disinda; retrieval'e girmez",
                    next_action=(
                        "Adaylari `zekam memory status` ve hijyen raporuyla salt okunur "
                        "inceleyin; gerekirse ayri plan ile tasfiye onerisi uretin"
                    ),
                    evidence={"orphan_candidates": orphan},
                )
            )
        if duplicate:
            findings.append(
                Finding(
                    code="cognitive.memory-duplicate",
                    severity=Severity.INFO,
                    title=f"{duplicate} duplicate hijyen adayi mevcut",
                    detail="Ayni scope/sinif icerik icin tekrar eden bellek adayi olabilir",
                    next_action="Dedupe onerilerini `zekam memory hygiene` ile inceleyin",
                    evidence={"duplicate": duplicate},
                )
            )
        if conflict_active:
            findings.append(
                Finding(
                    code="cognitive.memory-conflict",
                    severity=Severity.WARNING,
                    title=f"{conflict_active} aktif bellek cifti birbiriyle celisiyor",
                    detail="iki aktif head revision arasinda conflict-with iliskisi var",
                    next_action="Celiskili aktif bellek ciftlerini review ile cozun",
                    evidence={"conflicting_active": conflict_active},
                )
            )
        if stale:
            findings.append(
                Finding(
                    code="cognitive.memory-stale",
                    severity=Severity.INFO,
                    title=f"{stale} stale/retention hijyen adayi mevcut",
                    detail="Gecerliligi dolan veya gozden gecirme bekleyen bellek isaretlendi",
                    next_action="Stale/retention adaylarini review kuyrugunda degerleyin",
                    evidence={"stale": stale, "hygiene_by_kind": hygiene_counts},
                )
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED if not findings else CheckStatus.DEGRADED,
            summary=(
                f"memory: {orphan} orphan, {conflict_active} conflict, "
                f"{duplicate} duplicate, {stale} stale"
            ),
            findings=tuple(findings),
            evidence={
                "orphan_candidates": orphan,
                "conflicting_active": conflict_active,
                "duplicate": duplicate,
                "stale": stale,
                "hygiene_by_kind": hygiene_counts,
                "grants_authority": False,
            },
        )


@dataclass(frozen=True, slots=True)
class KnowledgeHealthCheck:
    """Bilgi notlari/artifacts ve knowledge index tutarliligini raporlar."""

    context: Any
    check_id: str = "cognitive.knowledge"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        from zekam.infrastructure.knowledge_files import KnowledgeFileStore
        from zekam.infrastructure.local_core_services import LocalCoreServices

        try:
            services = LocalCoreServices.from_context(self.context)
            operational_path = services.operational_path
            with closing(_readonly(operational_path)) as db:
                db.row_factory = sqlite3.Row
                note_rows = db.execute("select * from knowledge_note").fetchall()
                artifact_rows = db.execute("select * from artifact_ref").fetchall()
        except (OSError, sqlite3.DatabaseError):
            return _unavailable(self.check_id, "operational store okunamadi")

        notes = tuple(_row_knowledge_note(row) for row in note_rows)
        artifacts = tuple(_row_artifact_ref(row) for row in artifact_rows)

        files = KnowledgeFileStore(self.context.home)
        try:
            issues = files.audit(notes=notes, artifacts=artifacts)
        except Exception as exc:
            return _unavailable(self.check_id, type(exc).__name__)

        broken = tuple(
            issue for issue in sorted(issues) if issue.kind in _BROKEN_KNOWLEDGE_ISSUES
        )
        findings: list[Finding] = []
        if broken:
            findings.append(
                Finding(
                    code="cognitive.knowledge-broken-refs",
                    severity=Severity.WARNING,
                    title=f"{len(broken)} kirik/kayip bilgi referansi",
                    detail=", ".join(f"{item.kind}:{item.portable_ref}" for item in broken[:5]),
                    next_action=(
                        "Kirik/kayip referanslari `zekam knowledge audit` ile salt okunur "
                        "inceleyin; replan dogrulanmadan dosya silme"
                    ),
                    evidence={
                        "broken": [
                            {"kind": item.kind, "portable_ref": item.portable_ref}
                            for item in broken
                        ],
                    },
                )
            )
        pending = tuple(
            issue for issue in issues if issue.kind == "pending-note-materialization"
        )
        if pending:
            findings.append(
                Finding(
                    code="cognitive.knowledge-pending-materialization",
                    severity=Severity.INFO,
                    title=f"{len(pending)} bilgi notu materialize edilmemis",
                    detail="Kanonik not kaydi var ama dosya duzlemi hazir degil",
                    next_action="Materialization planini isleyin",
                    evidence={"pending": len(pending)},
                )
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED if not findings else CheckStatus.DEGRADED,
            summary=(
                f"knowledge: {len(issues)} bulgu, {len(broken)} kirik referans, "
                f"{len(pending)} pending"
            ),
            findings=tuple(findings),
            evidence={
                "checked_notes": len(notes),
                "checked_artifacts": len(artifacts),
                "broken_refs": len(broken),
                "pending_materialization": len(pending),
                "grants_authority": False,
            },
        )


@dataclass(frozen=True, slots=True)
class SkillIntegrityCheck:
    """Beceri paket butunlugu ve activation/evaluation tutarliligini raporlar."""

    context: Any
    check_id: str = "cognitive.skill"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        from zekam.infrastructure.local_core_services import LocalCoreServices

        try:
            services = LocalCoreServices.from_context(self.context)
            with closing(_readonly(services.learning.path)) as db:
                db.row_factory = sqlite3.Row
                total = _query_scalar(db, "select count(*) from skill_manifest")
                activated = _query_scalar(db, "select count(*) from skill_activation")
                candidates = _query_scalar(
                    db,
                    "select count(*) from skill_manifest where state=?",
                    (_SKILL_CANDIDATE_STATE,),
                )
                # v2 tablolari yalniz ileri schema v2'de bulunur; yoksa 0 say (skip degil).
                v2_total = (
                    _query_scalar(db, "select count(*) from skill_revision_v2")
                    if _table_exists(db, "skill_revision_v2")
                    else 0
                )
                v2_activated = (
                    _query_scalar(db, "select count(*) from skill_activation_event_v2")
                    if _table_exists(db, "skill_activation_event_v2")
                    else 0
                )
                # activation olmadan manifest (evaluation/review zinciri eksik kalmis olabilir).
                broken_eval = _query_scalar(
                    db,
                    "select count(*) from skill_manifest m "
                    "where not exists (select 1 from skill_activation a "
                    "where a.manifest_digest=m.manifest_digest)",
                )
        except (OSError, sqlite3.DatabaseError):
            return _unavailable(self.check_id, "learning store okunamadi")

        findings: list[Finding] = []
        if activated < total and total:
            findings.append(
                Finding(
                    code="cognitive.skill-activation-backlog",
                    severity=Severity.INFO,
                    title=f"{candidates} beceri manifest activation bekliyor",
                    detail="Candidate manifest'ler degerlendirme/onay zincirinden gecmemis",
                    next_action="Skill promotion kuyrugunu `zekam skill list` ile inceleyin",
                    evidence={"candidates": candidates},
                )
            )
        if broken_eval:
            findings.append(
                Finding(
                    code="cognitive.skill-evaluation-consistency",
                    severity=Severity.WARNING,
                    title=f"{broken_eval} beceri manifest activation kaydi istiyor",
                    detail="Nihai paket kurallari activation-oncesi dogrulamayi basta tutar",
                    next_action="Activation/evaluation zincirini salt okunur dogrulayin",
                    evidence={"unactivated_manifests": broken_eval},
                )
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED if not findings else CheckStatus.DEGRADED,
            summary=(
                f"skill: {total} manifest / {activated} activation (v2: "
                f"{v2_total} / {v2_activated})"
            ),
            findings=tuple(findings),
            evidence={
                "manifests": total,
                "activated": activated,
                "candidates": candidates,
                "v2_manifests": v2_total,
                "v2_activated": v2_activated,
                "unactivated_manifests": broken_eval,
                "grants_authority": False,
            },
        )


@dataclass(frozen=True, slots=True)
class ContinuityFreshnessCheck:
    """Checkpoint tazeligini ve acik-dongu (kapanmamis) run'lari raporlar."""

    context: Any
    check_id: str = "cognitive.continuity"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        from zekam.infrastructure.local_core_services import LocalCoreServices

        try:
            services = LocalCoreServices.from_context(self.context)
            with closing(_readonly(services.operational_path)) as db:
                db.row_factory = sqlite3.Row
                open_runs = db.execute(
                    "select id,status from run where status in ('planned','running','unknown')"
                ).fetchall()
                checkpoints = _query_scalar(db, "select count(*) from checkpoint")
        except (OSError, sqlite3.DatabaseError):
            return _unavailable(self.check_id, "operational store okunamadi")

        open_run_ids = [str(row["id"]) for row in open_runs]
        findings: list[Finding] = []
        if open_run_ids:
            findings.append(
                Finding(
                    code="cognitive.continuity-open-loop",
                    severity=Severity.WARNING,
                    title=f"{len(open_run_ids)} run terminal olmayan durumda",
                    detail="Kapanmamis run; checkpoint/close receit zinciri eksik olabilir",
                    next_action=(
                        "Acik run'larin claim/receipt durumunu salt okunur izleyin; "
                        "terminal kanit olmadan kapatma"
                    ),
                    evidence={"open_run_ids": open_run_ids},
                )
            )
        if checkpoints == 0:
            findings.append(
                Finding(
                    code="cognitive.continuity-no-checkpoint",
                    severity=Severity.INFO,
                    title="Hic checkpoint kaydi yok",
                    detail="Checkpoint yoksa freshness dogrulanamaz",
                    next_action="Ilerleyen isler icin checkpoint yazimini dogrulayin",
                    evidence={"checkpoints": 0},
                )
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED if not findings else CheckStatus.DEGRADED,
            summary=(
                f"continuity: {len(open_run_ids)} open run, "
                f"{checkpoints} checkpoint"
            ),
            findings=tuple(findings),
            evidence={
                "open_runs": len(open_run_ids),
                "open_run_ids": open_run_ids,
                "checkpoints": checkpoints,
                "grants_authority": False,
            },
        )


@dataclass(frozen=True, slots=True)
class SourceDigestCheck:
    """Context/source digest drift'ini (git HEAD vs kayitli source) raporlar."""

    context: Any
    check_id: str = "cognitive.context"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        from zekam.infrastructure.local_core_services import LocalCoreServices

        try:
            services = LocalCoreServices.from_context(self.context)
            with closing(_readonly(services.operational_path)) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "select revision_ref, captured_at from source_snapshot "
                    "order by captured_at desc limit ?",
                    (_SOURCE_DRIFT_LIMIT,),
                ).fetchall()
        except (OSError, sqlite3.DatabaseError):
            return _unavailable(self.check_id, "operational store okunamadi")

        latest = rows[0] if rows else None
        recorded_ref = str(latest["revision_ref"]) if latest is not None else None
        current_head = _context_git_head(self.context.core_path)
        evidence: dict[str, object] = {
            "recorded_source_ref": recorded_ref,
            "current_core_head": current_head,
            "grants_authority": False,
        }
        if recorded_ref is None:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.PASSED,
                summary="Kayitli source snapshot yok; context drift kontrol edilemez",
                evidence=evidence,
            )
        if current_head is None:
            return _unavailable(self.check_id, "git HEAD okunamadi")

        drifted = current_head != recorded_ref
        if drifted:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.DEGRADED,
                summary="Context source digest kayitli snapshot'tan sapmis",
                findings=(
                    Finding(
                        code="cognitive.context-source-drift",
                        severity=Severity.WARNING,
                        title="Kayitli source revision ile mevcut core HEAD uyusmuyor",
                        detail=f"kayitli={recorded_ref} mevcut={current_head}",
                        next_action=(
                            "Source binding'i yeniden yakalayin; context/digest cover'in "
                            "guncel revision'a baglanmasini dogrulayin"
                        ),
                        evidence=evidence,
                    ),
                ),
                evidence=evidence,
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED,
            summary="Context source digest guncel HEAD ile eslesiyor",
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class LearningBacklogCheck:
    """Ogrenme/evrim ve feedback birikimini salt okunur raporlar."""

    context: Any
    check_id: str = "cognitive.learning"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        from zekam.infrastructure.local_core_services import LocalCoreServices

        try:
            services = LocalCoreServices.from_context(self.context)
            learning_path = services.learning.path
            improvement_path = services.improvement.path
            with closing(_readonly(learning_path)) as db:
                db.row_factory = sqlite3.Row
                failure_without_card = _query_scalar(
                    db,
                    "select count(*) from failure_occurrence o "
                    "where not exists (select 1 from failure_card c "
                    "where c.signature_digest=o.signature_digest)",
                )
                lesson_without_skill = _query_scalar(
                    db,
                    "select count(*) from lesson l "
                    "where not exists (select 1 from skill_manifest m "
                    "where m.lesson_digest=l.lesson_digest)",
                )
            improvement_counts: dict[str, int] = {}
            if improvement_path.is_file():
                with closing(_readonly(improvement_path)) as db:
                    db.row_factory = sqlite3.Row
                    try:
                        feedback = _query_scalar(
                            db, "select count(*) from learning_feedback"
                        )
                        pending_rollout = _query_scalar(
                            db,
                            "select count(*) from typed_rollout_pending "
                            "where status not in ('completed','failed') and "
                            "status <> 'recovery-required'",
                        )
                        recovery_rollout = _query_scalar(
                            db,
                            "select count(*) from typed_rollout_pending "
                            "where status='recovery-required'",
                        )
                    except sqlite3.DatabaseError:
                        return _unavailable(self.check_id, "improvement store okunamadi")
                    improvement_counts = {
                        "learning_feedback": feedback,
                        "pending_rollout": pending_rollout,
                        "recovery_rollout": recovery_rollout,
                    }
        except (OSError, sqlite3.DatabaseError):
            return _unavailable(self.check_id, "learning store okunamadi")

        findings: list[Finding] = []
        if failure_without_card:
            findings.append(
                Finding(
                    code="cognitive.learning-unverified-failure",
                    severity=Severity.WARNING,
                    title=f"{failure_without_card} failure gozlemi card/lesson'a bagli degil",
                    detail="Unverified feedback backlog'u ders uretimini engeller",
                    next_action="Failure karti olusturma planini inceleyin",
                    evidence={"failure_without_card": failure_without_card},
                )
            )
        if lesson_without_skill:
            findings.append(
                Finding(
                    code="cognitive.learning-lesson-to-skill-backlog",
                    severity=Severity.INFO,
                    title=f"{lesson_without_skill} ders henuz skill adayina donusmemis",
                    detail="Dersler beceri adaylastirma kuyrugunda bekliyor",
                    next_action="Ders->skill promotion adaylarini `zekam skill` ile inceleyin",
                    evidence={"lesson_without_skill": lesson_without_skill},
                )
            )
        if improvement_counts:
            feedback = improvement_counts.get("learning_feedback", 0)
            pending_rollout = improvement_counts.get("pending_rollout", 0)
            recovery_rollout = improvement_counts.get("recovery_rollout", 0)
            if recovery_rollout:
                findings.append(
                    Finding(
                        code="cognitive.learning-evolution-recovery",
                        severity=Severity.WARNING,
                        title=f"{recovery_rollout} typed rollout recovery bekliyor",
                        detail="Recovery rollout adaylarinin claim/receipt uzlastirilmasi gerekir",
                        next_action="Recovery rollout planini salt okunur inceleyin",
                        evidence={"recovery_rollout": recovery_rollout},
                    )
                )
            if pending_rollout:
                findings.append(
                    Finding(
                        code="cognitive.learning-evolution-backlog",
                        severity=Severity.INFO,
                        title=f"{pending_rollout} typed rollout plan bekliyor",
                        detail="Bekleyen rollout planlari evrim kuyrugundadir",
                        next_action="Evrim kuyrugunu `zekam evolve report` ile inceleyin",
                        evidence={"pending_rollout": pending_rollout},
                    )
                )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED if not findings else CheckStatus.DEGRADED,
            summary=(
                f"learning: {failure_without_card} unverified failure, "
                f"{lesson_without_skill} lesson backlog, "
                f"feedback={improvement_counts.get('learning_feedback', 0)}"
            ),
            findings=tuple(findings),
            evidence={
                "failure_without_card": failure_without_card,
                "lesson_without_skill": lesson_without_skill,
                "improvement": improvement_counts,
                "grants_authority": False,
            },
        )


def _row_knowledge_note(row: Any) -> Any:
    from zekam.application.operational_store import KnowledgeNoteRecord

    return KnowledgeNoteRecord(
        id=str(row["id"]),
        owner_scope=str(row["owner_scope"]),
        portable_ref=str(row["portable_ref"]),
        note_kind=str(row["note_kind"]),
        authorship=str(row["authorship"]),
        classification=str(row["classification"]),
        content_digest=str(row["content_digest"]),
        state=str(row["state"]),
        realm_id=str(row["realm_id"]),
        project_id=str(row["project_id"]) if row["project_id"] is not None else None,
        project_slug=str(row["project_slug"]) if row["project_slug"] is not None else None,
        materialized=bool(row["materialized"]),
        archived_ref=(
            str(row["archived_ref"]) if row["archived_ref"] is not None else None
        ),
    )


def _row_artifact_ref(row: Any) -> ArtifactRefRecord:
    from zekam.application.operational_store import ArtifactRefRecord

    return ArtifactRefRecord(
        digest=str(row["digest"]),
        media_type=str(row["media_type"]),
        size_bytes=int(row["size_bytes"]),
        classification=str(row["classification"]),
    )
