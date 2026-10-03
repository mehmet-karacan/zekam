"""Dogal dil unit-test isteginin deterministik, provider-free siniflandirmasi.

W07 ve AKTIF_GOREV 10.2 kapsamindadir.

Amac: Turkce/Ingilizce bir cumlenin (a) genel kavram sorusu mu, (b) unit-test yazma/kapsama
istegi mi oldugunu ve (c) exact hedefin (dosya/sinif, yuzde) cumleden guvenilir sekilde
cikip cikmadigini karar vermek. Bu modul hicbir tool calistirmaz, model cagirmaz, dosya
okumaz ve yetki uretmez; yalniz ``zekam test plan`` ve route katmaninin yonlendirme girdisidir.

Kurallar:

- "unit test nedir?" gibi genel soru test yazma veya tool execution tetiklemez
  (``CONCEPT_QUESTION``).
- Yazma/kapsama fiili var ama hedef dosya/sinif yoksa ``WRITE_REQUEST`` + ``target-files-missing``:
  once belirsizlik cozulur, hedef uydurulmaz.
- Esik (yuzde) cumlede yoksa ``threshold-missing``; varsayilan esik uydurulmaz.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

_TR_FOLD: Final = str.maketrans(
    {
        "ı": "i",
        "İ": "i",
        "ş": "s",
        "Ş": "s",
        "ğ": "g",
        "Ğ": "g",
        "ü": "u",
        "Ü": "u",
        "ö": "o",
        "Ö": "o",
        "ç": "c",
        "Ç": "c",
    }
)

_MAX_TEXT_BYTES: Final = 16_384
_UNIT_WORDS: Final = re.compile(r"(?<![a-z0-9])(?:unit\s*-?\s*test\w*|birim\s+test\w*|junit)")
_TEST_WORD: Final = re.compile(r"(?<![a-z0-9])(?:test|testler|testlerini|testleri|testi)\w*")
_COVERAGE_WORDS: Final = re.compile(
    r"(?<![a-z0-9])(?:coverage|kapsam\w*|kapsa\w*|jacoco|cover)(?![a-z0-9])"
)
_WRITE_VERBS: Final = re.compile(
    r"(?<![a-z0-9])(?:yaz\w*|olustur\w*|uret\w*|ekle\w*|artir\w*|tamamla\w*|gelistir\w*|"
    r"write|generate|create|add|increase|improve|raise|complete)(?![a-z0-9])"
)
_RUN_ONLY: Final = re.compile(
    r"(?<![a-z0-9])(?:calistir\w*|kos\w*|run|execute|dogrula\w*|ilerleme|durum\w*|nerede)"
)
_QUESTION_MARKERS: Final = re.compile(
    r"(?<![a-z0-9])(?:nedir|ne\s+demek|nasil\s+calisir|nasil\s+yazilir|neden|niye|"
    r"farki|farklari|aciklar\s+misin|acikla|anlat|ornek\s+ver|what\s+is|what\s+are|"
    r"explain|why|how\s+does|how\s+do|difference)(?![a-z0-9])|\?\s*$"
)
_EXPLICIT_REQUEST: Final = re.compile(
    r"(?<![a-z0-9])(?:yaz\w*\s+misin|yazar\s+misin|olustur\w*\s+misin|yaz|olustur|uret|"
    r"hedef|write|generate)(?![a-z0-9])"
)
_JAVA_PATH: Final = re.compile(r"(?<![\w./\\-])((?:[\w.-]+[/\\])*[A-Za-z_][\w$]*\.java)(?![\w])")
_CLASS_TOKEN: Final = re.compile(r"(?<![\w.])([A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+)(?![\w.])")
_NOT_CLASS_TOKENS: Final = frozenset(
    {
        "JaCoCo",
        "JUnit",
        "OpenCode",
        "Zekam",
        "PostgreSQL",
        "SpringBoot",
        "GitHub",
        "JavaScript",
        "TypeScript",
        "MockMvc",
        "Mockito",
        "AssertJ",
        "ClaudeCode",
    }
)
_PERCENT: Final = re.compile(
    r"(?:%\s*(\d{1,3}(?:[.,]\d{1,2})?)|(?<![\w.,])(\d{1,3}(?:[.,]\d{1,2})?)\s*%|"
    r"yuzde\s*(\d{1,3}(?:[.,]\d{1,2})?)|(?:hedef|target|threshold|esik)\D{0,12}"
    r"(\d{1,3}(?:[.,]\d{1,2})?))"
)
_BRANCH: Final = re.compile(r"(?<![a-z0-9])(?:branch|dal|dallanma)\w*")
_AGGREGATE: Final = re.compile(
    r"(?<![a-z0-9])(?:aggregate|toplam\s+kapsam|toplu|genel\s+kapsam|hepsinin\s+toplam)"
)


class UnitTestIntentKind(StrEnum):
    NONE = "none"
    CONCEPT_QUESTION = "concept-question"
    WRITE_REQUEST = "write-request"


@dataclass(frozen=True, slots=True)
class UnitTestIntent:
    kind: UnitTestIntentKind
    files: tuple[str, ...] = ()
    class_names: tuple[str, ...] = ()
    percent: str | None = None
    metric: str | None = None
    policy: str | None = None
    clarifications: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @property
    def is_write(self) -> bool:
        return self.kind is UnitTestIntentKind.WRITE_REQUEST

    @property
    def ready(self) -> bool:
        """Yazma istegi ve exact hedef + esik cumleden cikti: plana gecilebilir."""

        return self.is_write and not self.clarifications

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "files": list(self.files),
            "class_names": list(self.class_names),
            "percent": self.percent,
            "metric": self.metric,
            "policy": self.policy,
            "clarifications": list(self.clarifications),
            "reason_codes": list(self.reason_codes),
            "triggers_tool_execution": False,
        }


def _fold(text: str) -> str:
    return " ".join(text.translate(_TR_FOLD).casefold().split())


def _percent(text: str) -> str | None:
    found: list[str] = []
    for match in _PERCENT.finditer(text):
        raw = next(group for group in match.groups() if group is not None)
        value = raw.replace(",", ".")
        try:
            number = float(value)
        except ValueError:
            continue
        if 0 < number <= 100:
            found.append(value)
    if not found:
        return None
    # Birden fazla farkli esik: belirsiz; ilkini secme, netlestirme istenir.
    return found[0] if len(set(found)) == 1 else "ambiguous"


def classify_unit_test_request(text: str) -> UnitTestIntent:
    """Cumleyi siniflandirir; bos/cok buyuk metin ``ValueError`` verir."""

    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError("Istek bounded non-empty metin olmali")
    folded = _fold(text)
    has_unit = _UNIT_WORDS.search(folded) is not None
    has_test = has_unit or _TEST_WORD.search(folded) is not None
    has_coverage = _COVERAGE_WORDS.search(folded) is not None
    if not (has_test or has_coverage):
        return UnitTestIntent(UnitTestIntentKind.NONE)

    files = tuple(dict.fromkeys(m.group(1).replace("\\", "/") for m in _JAVA_PATH.finditer(text)))
    file_stems = {f.rsplit("/", 1)[-1].removesuffix(".java") for f in files}
    classes = tuple(
        dict.fromkeys(
            token
            for token in _CLASS_TOKEN.findall(text)
            if token not in _NOT_CLASS_TOKENS and token not in file_stems
        )
    )
    percent = _percent(folded)
    has_write = _WRITE_VERBS.search(folded) is not None
    question = _QUESTION_MARKERS.search(folded) is not None
    explicit = _EXPLICIT_REQUEST.search(folded) is not None
    has_target = bool(files or classes)

    # Genel kavram sorusu: soru isareti/kalibi var, exact hedef ve esik yok, acik istek fiili yok.
    if question and not has_target and percent is None and not (has_write and explicit):
        return UnitTestIntent(
            UnitTestIntentKind.CONCEPT_QUESTION, reason_codes=("unit-test-concept-question",)
        )
    if (
        _RUN_ONLY.search(folded) is not None
        and not has_write
        and not has_coverage
        and percent is None
    ):
        # "testleri calistir/durum" mevcut testi kosma/durum sorusudur; yazma istegi degil.
        return UnitTestIntent(UnitTestIntentKind.NONE, reason_codes=("not-a-write-request",))
    if not (has_write or (has_coverage and percent is not None)):
        if question or not has_target:
            return UnitTestIntent(
                UnitTestIntentKind.CONCEPT_QUESTION, reason_codes=("unit-test-concept-question",)
            )
        return UnitTestIntent(UnitTestIntentKind.NONE, reason_codes=("not-a-write-request",))

    clarifications: list[str] = []
    if not has_target:
        clarifications.append("target-files-missing")
    if percent is None:
        clarifications.append("threshold-missing")
    elif percent == "ambiguous":
        clarifications.append("threshold-ambiguous")
    metric = "branch" if _BRANCH.search(folded) else None
    policy = "aggregate" if _AGGREGATE.search(folded) else None
    return UnitTestIntent(
        UnitTestIntentKind.WRITE_REQUEST,
        files=files,
        class_names=classes,
        percent=None if percent in {None, "ambiguous"} else percent,
        metric=metric,
        policy=policy,
        clarifications=tuple(clarifications),
        reason_codes=("unit-test-write-request",),
    )
