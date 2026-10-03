"""PIT ``mutations.xml`` -> ayri ham sayaclar (W06 / D10).

- ``safe_xml`` ile okunur: DOCTYPE/DTD, entity ve harici referans reddedilir; boyut, derinlik
  ve eleman sayisi sinirlidir. Hata mesaji ham icerik tasimaz.
- Her ``<mutation>`` yalniz ``status`` niteligiyle siniflanir; yedi sonuc sinifi ayri sayilir.
  ``STARTED``/``NOT_STARTED`` ve taninmayan durumlar sessizce yutulmaz: ayri sayac olur ve
  rapor ``complete=False`` olur. Mutation sayisi 0 ise bu 0 sayacla gorunur (sahte skor yok).
- ``detected`` niteligi ile ``status`` celisirse (ornegin SURVIVED + detected=true) rapor
  reddedilir.
- Skor/payda tanimlari ve ``review_required`` politikasi
  ``zekam.application.unit_test_mutation`` modulundedir; burada skor hesaplanmaz.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from zekam.application.unit_test_mutation import (
    MAX_REVIEW_ITEMS,
    REVIEW_STATUSES,
    UNFINISHED_STATUSES,
    MutantKey,
    PitCounters,
    PitReport,
    PitStatus,
    ReviewItem,
)
from zekam.domain.errors import ValidationFailed
from zekam.infrastructure.unit_test_runner.path_safety import resolve_inside
from zekam.infrastructure.unit_test_runner.safe_xml import (
    DEFAULT_MAX_BYTES,
    XmlElement,
    parse_xml_bounded,
    read_bounded,
)

PIT_MAX_BYTES: Final = DEFAULT_MAX_BYTES
_MAX_FIELD: Final = 400
#: ``detected`` niteligi bu durumlarda sabittir; digerlerinde (TIMED_OUT vb.) capraz kontrol yok.
_DETECTED_EXPECTED: Final = {
    PitStatus.KILLED: True,
    PitStatus.SURVIVED: False,
    PitStatus.NO_COVERAGE: False,
    PitStatus.NON_VIABLE: False,
}


def _field(element: XmlElement, tag: str) -> str:
    found = element.find_all(tag)
    return found[0].text[:_MAX_FIELD] if found else ""


def _short_mutator(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _line(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        return -1
    return value if value >= 0 else -1


def parse_pit_report(data: bytes, *, max_bytes: int = PIT_MAX_BYTES) -> PitReport:
    root = parse_xml_bounded(data, max_bytes=max_bytes)
    if root.tag != "mutations":
        raise ValidationFailed("PIT raporu 'mutations' koku tasimali")
    counts: dict[str, int] = {}
    statuses: list[tuple[MutantKey, PitStatus]] = []
    review: list[ReviewItem] = []
    truncated = False
    seen: dict[tuple[str, ...], int] = {}
    for mutation in root.find_all("mutation"):
        raw = mutation.attrs.get("status", "")
        status = _known_status(raw)
        if status is None:
            bucket = "unfinished" if raw in UNFINISHED_STATUSES else "unknown"
            counts[bucket] = counts.get(bucket, 0) + 1
            continue
        _check_detected(status, mutation.attrs.get("detected"))
        base = (
            _field(mutation, "sourceFile"),
            _field(mutation, "mutatedClass"),
            _field(mutation, "mutatedMethod"),
            _field(mutation, "methodDescription"),
            str(_line(_field(mutation, "lineNumber"))),
            _short_mutator(_field(mutation, "mutator")),
            ",".join(i.text for i in _indexes(mutation)),
        )
        ordinal = seen.get(base, 0)
        seen[base] = ordinal + 1
        key = MutantKey(base[0], base[1], base[2], base[3], int(base[4]), base[5], base[6], ordinal)
        counts[status.value.lower()] = counts.get(status.value.lower(), 0) + 1
        statuses.append((key, status))
        if status in REVIEW_STATUSES:
            if len(review) < MAX_REVIEW_ITEMS:
                review.append(ReviewItem(key, status))
            else:
                truncated = True
    counters = PitCounters(
        killed=counts.get("killed", 0),
        survived=counts.get("survived", 0),
        no_coverage=counts.get("no_coverage", 0),
        non_viable=counts.get("non_viable", 0),
        timed_out=counts.get("timed_out", 0),
        memory_error=counts.get("memory_error", 0),
        run_error=counts.get("run_error", 0),
        unfinished=counts.get("unfinished", 0),
        unknown=counts.get("unknown", 0),
    )
    return PitReport(counters, tuple(statuses), tuple(review), truncated)


def _known_status(raw: str) -> PitStatus | None:
    try:
        return PitStatus(raw)
    except ValueError:
        return None


def _indexes(mutation: XmlElement) -> list[XmlElement]:
    holder = mutation.find_all("indexes")
    return holder[0].find_all("index") if holder else []


def _check_detected(status: PitStatus, detected: str | None) -> None:
    expected = _DETECTED_EXPECTED.get(status)
    if expected is None or detected is None:
        return
    if detected.lower() not in {"true", "false"} or (detected.lower() == "true") != expected:
        raise ValidationFailed("PIT mutation 'detected' niteligi status ile celisiyor")


def read_pit_report(root: Path, relative: str, *, max_bytes: int = PIT_MAX_BYTES) -> bytes:
    """Rapor baytlarini proje-relative, baglanti reddeden ve sinirli okur."""

    path = resolve_inside(root, relative)
    if not path.is_file():
        raise ValidationFailed("PIT raporu bulunamadi")
    return read_bounded(path, max_bytes)
