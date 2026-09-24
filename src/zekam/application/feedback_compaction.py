"""Feedback Compaction - runtime feedback'i durable lesson koprusune baglar.

AC-13: binlerce tekrarlayan feedback expensive reasoner context'ine verilmez.
Once deterministic normalize -> dedup -> cluster yapilir ve yalniz compact,
sanitized lesson/candidate ozeti cikar.

Guvenlik ve otorite siniri:
- Raw prompt/response burada DERMEDURULMEZ; yalniz bounded content digest ve
  sanitized (secret-taranmis) compact ozet uretilir.
- Feedback pipeline yeni authority/permission URETMEZ (feedback != authority;
  candidate != approval). `grants_authority` her zaman False'dir.
- Secret degerler hicbir asamada durable feedback ozetine sizamaz.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.learning import FailureOccurrence, LearningCandidate, LearningTarget
from zekam.domain.policy import RiskLevel

FEEDBACK_COMPACTION_VERSION = "feedback-compaction-v1"
MAX_FEEDBACK_CHARS = 4096
MAX_FEEDBACK_ITEMS = 1024

# Feedback ozetine sizmamasi gereken secret/taslak desenleri.
_SENSITIVE = re.compile(
    r"(?:api[-_ ]?key|secret|credential|password|parola|private[-_ ]?key|"
    r"owner[-_ ]?token|bearer\s+[A-Za-z0-9._-]{8,}|"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)


class FeedbackKind(StrEnum):
    """Runtime feedback kaynaginin turu."""

    FAILURE = "failure"
    CORRECTION = "correction"
    ROUTING_DISUTILITY = "routing-disutility"
    EVIDENCE_GAP = "evidence-gap"
    CONTEXT_INEFFECTIVENESS = "context-ineffectiveness"


@dataclass(frozen=True, slots=True)
class FeedbackItem:
    """Sanitized feedback girdisi. Raw prompt/response tasimaz, digest tasir."""

    item_id: str
    kind: FeedbackKind
    content: str
    evidence_digest: str
    source_ref: str
    observed_at: dt.datetime
    occurrence_key: str
    risk: RiskLevel = RiskLevel.MEDIUM

    def __post_init__(self) -> None:
        if not self.item_id.strip() or not self.source_ref.strip():
            raise ValidationFailed("Feedback item id ve source ref bos olamaz")
        if len(self.content) > MAX_FEEDBACK_CHARS:
            raise ValidationFailed("Feedback content budget asildi")
        if not self.content.strip():
            raise ValidationFailed("Feedback content bos olamaz")
        parse_digest(self.evidence_digest)
        self._assert_no_raw_secret(self.content)
        if self.kind not in tuple(FeedbackKind):
            raise ValidationFailed("Feedback kind gecersiz")
        if self.observed_at.tzinfo is None:
            raise ValidationFailed("Feedback time aware olmali")
        if self.risk not in (
            RiskLevel.LOW,
            RiskLevel.MEDIUM,
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        ):
            raise ValidationFailed("Feedback risk low..critical olmali")

    @staticmethod
    def _assert_no_raw_secret(content: str) -> None:
        if _SENSITIVE.search(content):
            raise PolicyViolation("Feedback raw secret/credential tasiyamaz")


@dataclass(frozen=True, slots=True)
class CompactFeedbackCluster:
    """Deterministik olarak normalize/dedup edilmis feedback kumesi."""

    cluster_id: str
    normalized_key: str
    content_digest: str
    item_count: int
    kinds: tuple[FeedbackKind, ...]
    source_refs: tuple[str, ...]
    occurred_first: dt.datetime
    occurred_last: dt.datetime

    def __post_init__(self) -> None:
        if self.item_count < 1:
            raise ValidationFailed("Feedback cluster en az bir madde ister")
        parse_digest(self.content_digest)
        if self.occurred_first.tzinfo is None or self.occurred_last.tzinfo is None:
            raise ValidationFailed("Feedback cluster zaman damgalari aware olmali")
        if self.occurred_last < self.occurred_first:
            raise ValidationFailed("Feedback cluster aralik sirasi gecersiz")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-feedback-compact-cluster/v1",
            "cluster_id": self.cluster_id,
            "content_digest": self.content_digest,
            "item_count": self.item_count,
            "kinds": [kind.value for kind in sorted(self.kinds)],
            "source_refs": list(self.source_refs),
            "occurred_first": self.occurred_first,
            "occurred_last": self.occurred_last,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class DurableLessonBridge:
    """Compact feedback cluster'dan LearningCandidate'a kopru.

    Kopru yalniz evidence + proposal uretir; promotion/approval uretmez.
    `approval_derived` her zaman False'dir.
    """

    candidate: LearningCandidate
    approval_derived: bool = False

    def __post_init__(self) -> None:
        if self.approval_derived:
            raise PolicyViolation("Feedback pipeline approval/authority uretemez")


@dataclass(frozen=True, slots=True)
class FeedbackCompactionBudget:
    """Compaction butcesi. Sinirsiz feedback accumulation yoktur."""

    max_items: int = MAX_FEEDBACK_ITEMS
    max_total_chars: int = 262_144
    max_clusters: int = 256

    def __post_init__(self) -> None:
        if self.max_items < 1 or self.max_clusters < 1:
            raise ValidationFailed("Feedback compaction limitleri pozitif olmali")
        if self.max_total_chars < 1:
            raise ValidationFailed("Feedback compaction character budget pozitif olmali")


@dataclass(frozen=True, slots=True)
class FeedbackCompactionOutput:
    """Compaction sonucu. Yalniz compact/sanitize ozet; raw yok."""

    output_id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    clusters: tuple[CompactFeedbackCluster, ...]
    discarded: tuple[str, ...]
    created_at: dt.datetime
    output_digest: str
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Feedback compaction authority uretemez")
        if self.output_digest:
            parse_digest(self.output_digest)
            if self.output_digest != self.computed_digest:
                raise PolicyViolation("Feedback compaction output digest mismatch")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("Feedback compaction time aware olmali")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-feedback-compaction-output/v1",
            "compaction_version": FEEDBACK_COMPACTION_VERSION,
            "output_id": str(self.output_id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "run_id": str(self.run_id),
            "clusters": [item.body() for item in self.clusters],
            "discarded": list(self.discarded),
            "created_at": self.created_at,
            "grants_authority": False,
        }

    @property
    def computed_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def create(cls, **values: Any) -> FeedbackCompactionOutput:
        values["clusters"] = tuple(sorted(values["clusters"], key=lambda item: item.cluster_id))
        values["discarded"] = tuple(sorted(set(values["discarded"])))
        values["output_digest"] = ""
        draft = cls(**values)
        return cls(**{**values, "output_digest": draft.computed_digest})


_DIACRITIC = str.maketrans(
    {
        "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ı": "i", "İ": "i",
        "ş": "s", "Ş": "s", "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g",
        "â": "a", "î": "i", "û": "u", "é": "e", "è": "e", "à": "a",
    }
)


def _normalize(content: str) -> str:
    folded = unicodedata.normalize("NFKC", content)
    folded = folded.translate(_DIACRITIC)
    folded = folded.casefold()
    folded = re.sub(r"\s+", " ", folded)
    return folded.strip()


class FeedbackCompactor:
    """Runtime feedback'leri deterministic normalize -> dedup -> cluster yapar.

    MemoryCandidateCompiler ile ayni dusunce: birden cok kaynaktan gelen
    tekrarlayan feedback, expensive reasoner context'ine girmeden once
    compact/sanitized gruba indirgenir. Module raw state yazmaz.
    """

    def __init__(self, budget: FeedbackCompactionBudget | None = None) -> None:
        self._budget = budget or FeedbackCompactionBudget()

    def compact(
        self,
        items: tuple[FeedbackItem, ...],
        *,
        output_id: UUID,
        realm_id: UUID,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        created_at: dt.datetime,
    ) -> FeedbackCompactionOutput:
        if not items:
            raise ValidationFailed("Feedback compaction bos kaynak ister")
        if len(items) > self._budget.max_items:
            raise PolicyViolation("Feedback compaction item budget asildi")
        if sum(len(item.content) for item in items) > self._budget.max_total_chars:
            raise PolicyViolation("Feedback compaction character budget asildi")
        for value in (output_id, realm_id, project_id, work_item_id, run_id):
            if not isinstance(value, UUID):
                raise ValidationFailed("Feedback compaction ids UUID olmali")
        if created_at.tzinfo is None:
            raise ValidationFailed("Feedback compaction time aware olmali")

        # normalized key (caseless/folded) -> content digest -> cluster
        by_norm: dict[str, list[FeedbackItem]] = {}
        for item in items:
            key = _normalize(item.content)
            if not key:
                # Deterministik discard: bos/zararli icerik
                continue
            by_norm.setdefault(key, []).append(item)
        if len(by_norm) > self._budget.max_clusters:
            raise PolicyViolation("Feedback compaction cluster budget asildi")

        clusters: list[CompactFeedbackCluster] = []
        for norm_key in sorted(by_norm):
            group = by_norm[norm_key]
            content_digest = digest(norm_key)
            times = sorted(item.observed_at for item in group)
            cluster_id = f"feedback-cluster:{content_digest.removeprefix('sha256:')[:24]}"
            clusters.append(
                CompactFeedbackCluster(
                    cluster_id=cluster_id,
                    normalized_key=norm_key,
                    content_digest=content_digest,
                    item_count=len(group),
                    kinds=tuple({item.kind for item in group}),
                    source_refs=tuple(sorted({item.source_ref for item in group})),
                    occurred_first=times[0],
                    occurred_last=times[-1],
                )
            )

        return FeedbackCompactionOutput.create(
            output_id=output_id,
            realm_id=realm_id,
            project_id=project_id,
            work_item_id=work_item_id,
            run_id=run_id,
            clusters=tuple(clusters),
            discarded=(),
            created_at=created_at,
        )

    def bridge_to_durable_lesson(
        self,
        clusters: tuple[CompactFeedbackCluster, ...],
        *,
        author_ref: str,
    ) -> tuple[DurableLessonBridge, ...]:
        """Compact cluster'lari durable lesson adayina kopruler.

        Yalnizcak authority-permission uretmez; `approval_derived` False kalir.
        Her cluster, en az iki bagimsiz item topladiginda lesson adayina
        donusturulebilir (Feedback -> Lesson). Tek item'lik tartisma aday
        uretmez (yeterli kanit esigi).
        """
        if author_ref.strip() == "":
            raise ValidationFailed("Lesson author ref bos olamaz")
        bridges: list[DurableLessonBridge] = []
        for cluster in clusters:
            if cluster.item_count < 2:
                # Yalniz bir gozlem: verified root cause / cift observation esigi
                # olmadan ders uretilmez (learning MINIMUM_OBSERVATIONS uyumu).
                continue
            if set(cluster.kinds) & {FeedbackKind.FAILURE, FeedbackKind.CORRECTION}:
                target = LearningTarget.GUIDANCE
            else:
                target = LearningTarget.EVAL
            content_digest = cluster.content_digest
            # Durable lesson proposal'i sanitized normalize icerikten deterministik.
            proposal = _lesson_proposal(cluster.normalized_key)
            occurrence_key = f"feedback:{content_digest.removeprefix('sha256:')[:24]}"
            # Deri/kg: tekrarlayan normalized feedback tek occurrence olarak sayilir
            # (ayni content digest ayri run'lardan gelse de tek gozlemdir - learning
            # distinct_observations uyumu). Root cause henuz verified degil.
            occurrence = FailureOccurrence(
                occurrence_key=occurrence_key,
                evidence_digest=content_digest,
                run_ref=cluster.source_refs[0],
                observed_at=cluster.occurred_last,
                failure_category=(
                    "feedback-failure" if FeedbackKind.FAILURE in cluster.kinds else "feedback"
                ),
            )
            candidate = LearningCandidate(
                candidate_id=f"fb-{cluster.cluster_id}",
                occurrence_key=occurrence_key,
                occurrences=(occurrence,),
                target=target,
                proposal=proposal,
                author_ref=author_ref,
            )
            bridges.append(DurableLessonBridge(candidate=candidate, approval_derived=False))
        return tuple(bridges)


def _lesson_proposal(normalized_key: str) -> str:
    """Deterministik, sanitized lesson proposal uretir."""
    trimmed = normalized_key[:240]
    return f"Tekrar eden feedback compact edildi: {trimmed}"
