"""Turkce/Ingilizce dogal dil intake sozlesmesi.

Intake yalniz *ne istendigini* cozer. Authority vermez, mutation yapmaz ve exact
identifier'i asla semantic benzerlikle degistirmez. Belirsizlik sessizce tahmin
edilmez; gorunur `Ambiguity` kaydi olur ve netlestirme istenir.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_REQUEST_CHARS = 4000
MAX_SUBJECT_CHARS = 400
MAX_SUBJECT_AGE_SECONDS = 6 * 60 * 60

_SENSITIVE = re.compile(
    r"(?:secret|credential|password|parola|api[-_ ]?key|private[-_ ]?key|owner[-_ ]?token|"
    r"bearer\s+[A-Za-z0-9._-]{8,}|-----BEGIN)",
    re.IGNORECASE,
)


class RequestClass(StrEnum):
    RESEARCH = "research"
    PROJECT_CHANGE = "project-change"
    STATUS = "status"
    IDEA = "idea"
    AMBIGUOUS = "ambiguous"


class AmbiguityKind(StrEnum):
    MULTIPLE_INTENTS = "multiple-intents"
    NO_INTENT_CUE = "no-intent-cue"
    ANAPHORA_UNRESOLVED = "anaphora-unresolved"
    PROJECT_AMBIGUOUS = "project-ambiguous"
    PROJECT_UNRESOLVED = "project-unresolved"
    IDENTIFIER_UNKNOWN = "identifier-unknown"


class MatchKind(StrEnum):
    EXACT_ID = "exact-id"
    EXACT_ALIAS = "exact-alias"
    NORMALIZED_ALIAS = "normalized-alias"
    TRIGRAM = "trigram"


#: Exact identifier oncelikli oldugundan bu siralama katidir.
_EXACT_MATCHES = (MatchKind.EXACT_ID, MatchKind.EXACT_ALIAS)

_CUES: dict[RequestClass, tuple[str, ...]] = {
    RequestClass.RESEARCH: (
        "arastir",
        "arastirma",
        "kok neden",
        "kok nedenini",
        "incele",
        "karsilastir",
        "analiz et",
        "analiz edip",
        "degerlendir",
        "research",
        "investigate",
        "root cause",
        "compare",
        "analyze",
        "analyse",
        "look into",
    ),
    RequestClass.PROJECT_CHANGE: (
        "uygula",
        "ekle",
        "duzelt",
        "kaldir",
        "sil",
        "tasi",
        "yeniden duzenle",
        "refactor",
        "implement",
        "apply",
        "fix",
        "add",
        "remove",
        "migrate",
        "rename",
    ),
    RequestClass.STATUS: (
        "nerede kaldik",
        "nerde kaldik",
        "durum",
        "hangi islerimiz",
        "hangi isler",
        "listele",
        "goster",
        "rapor ver",
        "status",
        "where did we",
        "what is left",
        "list",
        "show",
    ),
    RequestClass.IDEA: (
        "fikir",
        "oneri",
        "onerim",
        "olabilir mi",
        "ne dersin",
        "dusunuyorum",
        "idea",
        "proposal",
        "what if",
        "should we",
    ),
}

_ANAPHORA = (
    "bunu",
    "bunlari",
    "sunu",
    "onu",
    "buna",
    "bu konuyu",
    "this",
    "that",
    "these",
    "it",
)

_WORK_CODE = re.compile(r"\b([A-Z][A-Z0-9]{1,15}(?:-[A-Z0-9]{1,15}){1,4})\b")
_HASH_NUMBER = re.compile(r"#(\d{1,9})\b")
_TR_NUMBER = re.compile(r"\b(\d{1,9})\s*(?:numarali|numarali|nolu|no'lu)\b", re.IGNORECASE)
_EN_NUMBER = re.compile(r"\b(?:defect|issue|bug|ticket|talep|hata)\s+(\d{1,9})\b", re.IGNORECASE)


def normalize_text(value: str) -> str:
    """Turkce aksanlari duselterek kucuk harfli arama metni uretir."""

    lowered = value.replace("I", "i").replace("İ", "i").lower()
    folded = unicodedata.normalize("NFKD", lowered)
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    replaced = stripped.replace("ı", "i").replace("ğ", "g").replace("ş", "s")
    return re.sub(r"\s+", " ", replaced).strip()


def _reject_sensitive(value: str, label: str) -> None:
    if _SENSITIVE.search(value):
        raise PolicyViolation(f"{label} secret benzeri icerik tasiyamaz")


@dataclass(frozen=True, slots=True)
class ExactIdentifier:
    """Metinde gecen exact kimlik; semantic benzerlik bunu degistiremez."""

    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind not in {"work-code", "number"}:
            raise ValidationFailed("exact identifier turu taninmiyor")
        if not self.value.strip():
            raise ValidationFailed("exact identifier bos olamaz")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value}


def extract_identifiers(text: str) -> tuple[ExactIdentifier, ...]:
    """Exact kimlikleri gorulme sirasiyla ve tekrarsiz dondurur."""

    found: list[ExactIdentifier] = []
    seen: set[tuple[str, str]] = set()

    def _push(kind: str, value: str) -> None:
        key = (kind, value)
        if key not in seen:
            seen.add(key)
            found.append(ExactIdentifier(kind=kind, value=value))

    for match in _WORK_CODE.finditer(text):
        _push("work-code", match.group(1))
    for pattern in (_HASH_NUMBER, _TR_NUMBER, _EN_NUMBER):
        for match in pattern.finditer(text):
            _push("number", match.group(1))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class ConversationSubject:
    """Anaphora cozumu icin bounded konu; transcript degildir."""

    subject: str
    captured_at: dt.datetime
    project_ref: str | None = None
    work_item_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValidationFailed("konu bos olamaz")
        if len(self.subject) > MAX_SUBJECT_CHARS:
            raise ValidationFailed("konu bounded sinirini asiyor")
        if self.captured_at.tzinfo is None:
            raise ValidationFailed("konu zaman damgasi timezone-aware olmali")
        _reject_sensitive(self.subject, "konu")

    def is_fresh(self, now: dt.datetime) -> bool:
        age = (now - self.captured_at).total_seconds()
        return 0 <= age <= MAX_SUBJECT_AGE_SECONDS

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "captured_at": self.captured_at.isoformat(),
            "project_ref": self.project_ref,
            "work_item_ref": self.work_item_ref,
        }


@dataclass(frozen=True, slots=True)
class ProjectCandidate:
    project_ref: str
    display_name: str
    match_kind: MatchKind
    matched_on: str

    def __post_init__(self) -> None:
        if not self.project_ref.strip():
            raise ValidationFailed("proje referansi bos olamaz")

    @property
    def is_exact(self) -> bool:
        return self.match_kind in _EXACT_MATCHES

    def as_dict(self) -> dict[str, str]:
        return {
            "project_ref": self.project_ref,
            "display_name": self.display_name,
            "match_kind": str(self.match_kind),
            "matched_on": self.matched_on,
        }


@dataclass(frozen=True, slots=True)
class Ambiguity:
    kind: AmbiguityKind
    detail: str
    options: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "detail": self.detail, "options": list(self.options)}


@dataclass(frozen=True, slots=True)
class IntakeRequest:
    text: str
    received_at: dt.datetime
    subject: ConversationSubject | None = None
    current_project_ref: str | None = None
    current_work_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValidationFailed("istek metni bos olamaz")
        if len(self.text) > MAX_REQUEST_CHARS:
            raise ValidationFailed("istek metni bounded sinirini asiyor")
        if self.received_at.tzinfo is None:
            raise ValidationFailed("istek zaman damgasi timezone-aware olmali")

    @property
    def normalized(self) -> str:
        return normalize_text(self.text)


@dataclass(frozen=True, slots=True)
class IntakeResolution:
    """Intake sonucu. Authority icermez; yalniz sonraki adimi tarif eder."""

    request_class: RequestClass
    request_digest: str
    matched_cues: tuple[str, ...]
    exact_identifiers: tuple[ExactIdentifier, ...]
    project_ref: str | None
    project_candidates: tuple[ProjectCandidate, ...]
    work_ref: str | None
    subject_used: str | None
    anaphora_present: bool
    ambiguities: tuple[Ambiguity, ...] = field(default_factory=tuple)
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("intake authority veremez")
        if self.ambiguities and self.request_class is not RequestClass.AMBIGUOUS:
            raise ValidationFailed("belirsizlik varken sinif AMBIGUOUS olmali")

    @property
    def requires_clarification(self) -> bool:
        return bool(self.ambiguities)

    @property
    def may_start_work(self) -> bool:
        """Netlestirme gerekmiyorsa siradaki planlama adimi baslatilabilir."""

        return not self.ambiguities and self.request_class is not RequestClass.AMBIGUOUS

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-intake-resolution/v1",
            "request_class": str(self.request_class),
            "request_digest": self.request_digest,
            "matched_cues": list(self.matched_cues),
            "exact_identifiers": [item.as_dict() for item in self.exact_identifiers],
            "project_ref": self.project_ref,
            "project_candidates": [item.as_dict() for item in self.project_candidates],
            "work_ref": self.work_ref,
            "subject_used": self.subject_used,
            "anaphora_present": self.anaphora_present,
            "ambiguities": [item.as_dict() for item in self.ambiguities],
            "grants_authority": False,
        }

    @property
    def resolution_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return dict(self.body(), resolution_digest=self.resolution_digest)


def _match_cues(normalized: str) -> dict[RequestClass, tuple[str, ...]]:
    hits: dict[RequestClass, tuple[str, ...]] = {}
    for request_class, cues in _CUES.items():
        matched = tuple(cue for cue in cues if cue in normalized)
        if matched:
            hits[request_class] = matched
    return hits


def _has_anaphora(normalized: str) -> bool:
    return any(
        re.search(rf"(?:^|\s){re.escape(word)}(?:\s|$|,|\.)", normalized) for word in _ANAPHORA
    )


def classify(request: IntakeRequest) -> tuple[RequestClass, tuple[str, ...], tuple[Ambiguity, ...]]:
    """Istek sinifini, eslesen ipuclarini ve sinif kaynakli belirsizligi dondurur."""

    normalized = request.normalized
    hits = _match_cues(normalized)
    if not hits:
        return (
            RequestClass.AMBIGUOUS,
            (),
            (
                Ambiguity(
                    kind=AmbiguityKind.NO_INTENT_CUE,
                    detail="istek sinifi belirlenemedi; niyet tahmin edilmez",
                ),
            ),
        )
    if len(hits) > 1:
        options = tuple(sorted(str(item) for item in hits))
        return (
            RequestClass.AMBIGUOUS,
            tuple(sorted(cue for cues in hits.values() for cue in cues)),
            (
                Ambiguity(
                    kind=AmbiguityKind.MULTIPLE_INTENTS,
                    detail="birden fazla niyet ipucu var; secim gerekiyor",
                    options=options,
                ),
            ),
        )
    request_class, cues = next(iter(hits.items()))
    return request_class, tuple(sorted(cues)), ()


def resolve_intake(
    request: IntakeRequest,
    *,
    candidates: tuple[ProjectCandidate, ...] = (),
    known_identifiers: frozenset[str] = frozenset(),
    project_required: bool = True,
) -> IntakeResolution:
    """Sinif, exact kimlik, proje ve anaphora cozumunu tek kayda baglar.

    `candidates` cagiran tarafin kanonik registry'den kurdugu aday listesidir; bu
    fonksiyon kendi basina benzerlik uydurmaz. `known_identifiers` verildiginde
    metindeki exact kimlikler exact lookup ile dogrulanir.
    """

    request_class, cues, ambiguities = classify(request)
    identifiers = extract_identifiers(request.text)
    anaphora = _has_anaphora(request.normalized)
    problems = list(ambiguities)

    subject_used: str | None = None
    if anaphora and not identifiers:
        subject = request.subject
        if subject is not None and subject.is_fresh(request.received_at):
            subject_used = subject.subject
        else:
            problems.append(
                Ambiguity(
                    kind=AmbiguityKind.ANAPHORA_UNRESOLVED,
                    detail="isaret zamiri icin taze bounded konu yok; konu uydurulmaz",
                )
            )

    if known_identifiers:
        unknown = tuple(item.value for item in identifiers if item.value not in known_identifiers)
        if unknown:
            problems.append(
                Ambiguity(
                    kind=AmbiguityKind.IDENTIFIER_UNKNOWN,
                    detail="exact kimlik kanonik kayitta bulunamadi",
                    options=unknown,
                )
            )

    project_ref, project_problem = _resolve_project(
        request,
        candidates=candidates,
        subject=request.subject if subject_used else None,
        required=project_required and request_class is not RequestClass.STATUS,
    )
    if project_problem is not None:
        problems.append(project_problem)

    work_ref = request.current_work_ref
    for item in identifiers:
        if item.kind == "work-code":
            work_ref = item.value
            break

    final_class = RequestClass.AMBIGUOUS if problems else request_class
    return IntakeResolution(
        request_class=final_class,
        request_digest=digest({"text": request.text}),
        matched_cues=cues,
        exact_identifiers=identifiers,
        project_ref=project_ref,
        project_candidates=candidates,
        work_ref=work_ref,
        subject_used=subject_used,
        anaphora_present=anaphora,
        ambiguities=tuple(problems),
    )


def _resolve_project(
    request: IntakeRequest,
    *,
    candidates: tuple[ProjectCandidate, ...],
    subject: ConversationSubject | None,
    required: bool,
) -> tuple[str | None, Ambiguity | None]:
    exact = tuple(item for item in candidates if item.is_exact)
    pool = exact or candidates
    if len(pool) == 1:
        return pool[0].project_ref, None
    if len(pool) > 1:
        return None, Ambiguity(
            kind=AmbiguityKind.PROJECT_AMBIGUOUS,
            detail="birden fazla proje adayi var; mutation yapmadan secim gerekiyor",
            options=tuple(sorted(item.project_ref for item in pool)),
        )
    fallback = request.current_project_ref or (subject.project_ref if subject else None)
    if fallback:
        return fallback, None
    if required:
        return None, Ambiguity(
            kind=AmbiguityKind.PROJECT_UNRESOLVED,
            detail="proje cozulemedi; kanonik registry'de aday yok",
        )
    return None, None


def assert_identifier_preserved(resolution: IntakeResolution, *, original_text: str) -> None:
    """Semantic benzerligin exact kimligi degistirmedigini dogrular."""

    for item in resolution.exact_identifiers:
        if item.value not in original_text:
            raise PolicyViolation("exact kimlik degistirilemez")
