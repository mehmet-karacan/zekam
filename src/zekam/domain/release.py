"""Release, DR kapsulu, SBOM ve Global DoD degerlendirme sozlesmesi.

Bir kriter yalniz **kanit** ile kapanir; waiver yoktur. Release artifact'i checksum
ve SBOM olmadan uretilemez. Proje kapsulu absolute path, aktif lease veya secret
tasiyamaz. Rename yalniz eski depo kaldirildiginda ve butun kapilar gectiginde
mumkundur.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

_SENSITIVE = re.compile(
    r"(?:secret|credential|password|parola|api[-_ ]?key|private[-_ ]?key|owner[-_ ]?token)",
    re.IGNORECASE,
)


class CriterionState(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


#: Bir kriterin kapanmasi icin gereken kanit turleri.
REQUIRED_EVIDENCE_KINDS = (
    "test-or-evaluation",
    "canonical-record-or-artifact",
    "verifier-or-review",
)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """Tek kanit girdisi. Referans portable, digest dogrulanabilir."""

    kind: str
    reference: str
    digest_value: str

    def __post_init__(self) -> None:
        parse_digest(self.digest_value)
        if self.kind not in REQUIRED_EVIDENCE_KINDS:
            raise ValidationFailed("kanit turu taninmiyor")
        if not self.reference.strip():
            raise ValidationFailed("kanit referansi bos olamaz")
        if _absolute(self.reference):
            raise PolicyViolation("kanit referansi absolute path tasiyamaz")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "reference": self.reference, "digest": self.digest_value}


def _absolute(value: str) -> bool:
    return value.startswith("/") or PureWindowsPath(value).is_absolute() or "\\" in value


@dataclass(frozen=True, slots=True)
class DodCriterion:
    """Global DoD kriteri. Waiver yoktur: kanit yoksa kapanmaz."""

    criterion_id: str
    category: str
    statement: str
    state: CriterionState
    evidence: tuple[EvidenceItem, ...] = ()
    blocker: str | None = None
    waiver_allowed: bool = False

    def __post_init__(self) -> None:
        if self.waiver_allowed:
            raise PolicyViolation("Global DoD kriteri waiver kabul etmez")
        if self.state is CriterionState.PASSED:
            missing = tuple(
                kind
                for kind in REQUIRED_EVIDENCE_KINDS
                if kind not in {item.kind for item in self.evidence}
            )
            if missing:
                raise PolicyViolation(f"kanit eksik: {', '.join(missing)}")
        if self.state is CriterionState.BLOCKED and not (self.blocker or "").strip():
            raise ValidationFailed("blocked kriter gerekce ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "category": self.category,
            "statement": self.statement,
            "state": str(self.state),
            "evidence": [item.as_dict() for item in self.evidence],
            "blocker": self.blocker,
        }


@dataclass(frozen=True, slots=True)
class DodAssessment:
    """Butun kriterlerin degerlendirmesi. Kismi tamamlanma release acmaz."""

    criteria: tuple[DodCriterion, ...]
    assessed_at: dt.datetime

    def __post_init__(self) -> None:
        if not self.criteria:
            raise ValidationFailed("degerlendirme en az bir kriter ister")
        identifiers = [item.criterion_id for item in self.criteria]
        if len(set(identifiers)) != len(identifiers):
            raise ValidationFailed("kriter kimlikleri tekrar edemez")

    def by_state(self, state: CriterionState) -> tuple[DodCriterion, ...]:
        return tuple(item for item in self.criteria if item.state is state)

    @property
    def is_complete(self) -> bool:
        return all(item.state is CriterionState.PASSED for item in self.criteria)

    @property
    def completion_ratio(self) -> float:
        return len(self.by_state(CriterionState.PASSED)) / len(self.criteria)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-dod-assessment/v1",
            "total": len(self.criteria),
            "passed": len(self.by_state(CriterionState.PASSED)),
            "pending": len(self.by_state(CriterionState.PENDING)),
            "failed": len(self.by_state(CriterionState.FAILED)),
            "blocked": len(self.by_state(CriterionState.BLOCKED)),
            "completion_ratio": round(self.completion_ratio, 6),
            "is_complete": self.is_complete,
            "criteria": [item.as_dict() for item in self.criteria],
        }

    @property
    def assessment_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class SbomEntry:
    """Yazilim malzeme listesi girdisi."""

    name: str
    version: str
    license_id: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ValidationFailed("bagimlilik adi ve surumu bos olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "license": self.license_id}


@dataclass(frozen=True, slots=True)
class Sbom:
    """Surumlu malzeme listesi."""

    entries: tuple[SbomEntry, ...]
    generated_at: dt.datetime

    def __post_init__(self) -> None:
        if not self.entries:
            raise ValidationFailed("SBOM bos olamaz")
        names = [item.name.lower() for item in self.entries]
        if len(set(names)) != len(names):
            raise ValidationFailed("bagimlilik adi tekrar edemez")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-sbom/v1",
            "entries": [item.as_dict() for item in sorted(self.entries, key=lambda x: x.name)],
        }

    @property
    def sbom_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    """Yayin artifact'i. Checksum ve SBOM olmadan uretilemez."""

    name: str
    version: str
    content_digest: str
    sbom_digest: str
    dod_digest: str
    signed: bool = False

    def __post_init__(self) -> None:
        for value in (self.content_digest, self.sbom_digest, self.dod_digest):
            parse_digest(value)
        if not re.match(r"^\d+\.\d+\.\d+", self.version):
            raise ValidationFailed("surum semantik olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "content_digest": self.content_digest,
            "sbom_digest": self.sbom_digest,
            "dod_digest": self.dod_digest,
            "signed": self.signed,
        }


def build_release(
    *, name: str, version: str, content_digest: str, sbom: Sbom, assessment: DodAssessment
) -> ReleaseArtifact:
    """Release yalniz Global DoD tamamlandiginda uretilir."""

    if not assessment.is_complete:
        raise PolicyViolation(
            f"Global DoD tamamlanmadan release uretilemez "
            f"({len(assessment.by_state(CriterionState.PASSED))}/{len(assessment.criteria)})"
        )
    return ReleaseArtifact(
        name=name,
        version=version,
        content_digest=content_digest,
        sbom_digest=sbom.sbom_digest,
        dod_digest=assessment.assessment_digest,
    )


@dataclass(frozen=True, slots=True)
class ProjectCapsule:
    """Tasinabilir proje kapsulu.

    Absolute path, aktif lease veya secret tasiyamaz: baska bir makinede acilan
    kapsul yetki devralmaz, yalniz veri tasir.
    """

    project_ref: str
    source_revision: str
    relative_paths: tuple[str, ...]
    content_digest: str
    carries_active_lease: bool = False
    carries_secret: bool = False

    def __post_init__(self) -> None:
        parse_digest(self.content_digest)
        if self.carries_active_lease:
            raise PolicyViolation("kapsul aktif lease tasiyamaz")
        if self.carries_secret:
            raise PolicyViolation("kapsul secret tasiyamaz")
        if not self.relative_paths:
            raise ValidationFailed("kapsul en az bir yol ister")
        for path in self.relative_paths:
            if _absolute(path):
                raise PolicyViolation("kapsul absolute path tasiyamaz")
            if ".." in PurePosixPath(path).parts:
                raise PolicyViolation("kapsul traversal tasiyamaz")
            if _SENSITIVE.search(path):
                raise PolicyViolation("kapsul secret benzeri yol tasiyamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-project-capsule/v1",
            "project_ref": self.project_ref,
            "source_revision": self.source_revision,
            "relative_paths": sorted(self.relative_paths),
            "content_digest": self.content_digest,
            "carries_active_lease": False,
            "carries_secret": False,
        }

    @property
    def capsule_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class RestoreCheck:
    """Yalitilmis geri yukleme dogrulamasi."""

    capsule_digest: str
    restored_digest: str
    source_bound: bool
    active_lease_present: bool

    def __post_init__(self) -> None:
        parse_digest(self.capsule_digest)
        parse_digest(self.restored_digest)

    @property
    def is_valid(self) -> bool:
        """Geri yukleme: icerik ayni, kaynak bagli degil, lease yok."""

        return (
            self.capsule_digest == self.restored_digest
            and not self.source_bound
            and not self.active_lease_present
        )

    def failures(self) -> tuple[str, ...]:
        problems: list[str] = []
        if self.capsule_digest != self.restored_digest:
            problems.append("icerik digest'i uyusmuyor")
        if self.source_bound:
            problems.append("geri yuklenen kapsul kaynaga bagli gelmemeli")
        if self.active_lease_present:
            problems.append("geri yuklemede aktif lease bulunmamali")
        return tuple(problems)

    def as_dict(self) -> dict[str, Any]:
        return {
            "capsule_digest": self.capsule_digest,
            "restored_digest": self.restored_digest,
            "is_valid": self.is_valid,
            "failures": list(self.failures()),
        }


@dataclass(frozen=True, slots=True)
class BackpressureDecision:
    """Kapasite karari. Kuyruk dolarken yeni is kabul edilmez."""

    queue_depth: int
    max_queue_depth: int
    active_workers: int
    max_workers: int

    def __post_init__(self) -> None:
        if min(self.max_queue_depth, self.max_workers) <= 0:
            raise ValidationFailed("kapasite sinirlari pozitif olmali")
        if min(self.queue_depth, self.active_workers) < 0:
            raise ValidationFailed("olcumler negatif olamaz")

    @property
    def accepts_work(self) -> bool:
        return self.queue_depth < self.max_queue_depth and self.active_workers < self.max_workers

    def reason(self) -> str:
        if self.queue_depth >= self.max_queue_depth:
            return "kuyruk derinligi sinirinda; yeni is kabul edilmiyor"
        if self.active_workers >= self.max_workers:
            return "worker kapasitesi dolu"
        return "kapasite uygun"

    def as_dict(self) -> dict[str, Any]:
        return {
            "queue_depth": self.queue_depth,
            "max_queue_depth": self.max_queue_depth,
            "active_workers": self.active_workers,
            "max_workers": self.max_workers,
            "accepts_work": self.accepts_work,
            "reason": self.reason(),
        }


@dataclass(frozen=True, slots=True)
class CancellationRequest:
    """Sert iptal. Iptal edilen is terminal sonuc yayimlayamaz."""

    run_ref: str
    requested_at: dt.datetime
    acknowledged: bool = False
    force: bool = False

    def __post_init__(self) -> None:
        if not self.run_ref.strip():
            raise ValidationFailed("run referansi bos olamaz")
        if self.requested_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")

    def assert_no_result_after_cancel(self, *, result_published: bool) -> None:
        if self.acknowledged and result_published:
            raise PolicyViolation("iptal edilen calisma terminal sonuc yayimlayamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_ref": self.run_ref,
            "acknowledged": self.acknowledged,
            "force": self.force,
        }
