"""Etkin context yuku olcum raporu (AKTIF_GOREV 5.2 / D24).

Bu modul authority uretmez ve ayri bir kayit sistemi degildir: mevcut manifest ve
instruction kaynaklarindan salt okunur, geriye uyumlu bir gorunum uretir.

Olcum niteligi (``MeasurementQuality``):

- ``known_loaded``: Zekam icerigi kendisi derledi/yukledi; digest ve boyut exact bilinir.
- ``discovered_only``: Kaynak diskte bulundu ve istemcinin yukleyecegi belgelenmis, ancak
  istemcinin gercekten yukledigi gozlenemedi. Boyut dosyadan olculur, yukleme kaniti degildir.
- ``estimated``: Boyut bir tahmin yontemiyle uretildi; yontem ve surumu etiketlenir.
- ``unobservable``: Istemcinin gizli system prompt'u / tool serialization'i gibi gorulemeyen
  kisim. Hicbir boyut iddia edilmez.

Byte, karakter, token tahmini ve saglayici usage'i birbirinin yerine gecmez. Tam etkin token
sayisi yalniz saglayicinin gercek usage'i verildiginde ve rapor tamamen gozlenmisse
``exact_effective_token_count`` olarak gorunur; aksi halde None kalir.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest, digest_of_bytes, parse_digest
from zekam.domain.context_continuity import ContextManifest, _safe_logical
from zekam.domain.errors import PolicyViolation, ValidationFailed

MEASUREMENT_SCHEMA = "zekam-effective-context-report/v1"
# Tokenizer indirilmez/calistirilmaz; yalniz acik etiketli sabit bir tahmin kullanilir.
ESTIMATOR_METHOD = "utf8-bytes-div-4"
ESTIMATOR_VERSION = "1"
_BYTES_PER_TOKEN_ESTIMATE = 4
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,95}$")


class MeasurementQuality(StrEnum):
    KNOWN_LOADED = "known_loaded"
    DISCOVERED_ONLY = "discovered_only"
    ESTIMATED = "estimated"
    UNOBSERVABLE = "unobservable"


class InstructionSourceKind(StrEnum):
    """Manifest disi etkin yuk kaynak turleri (manifest secimleri ContextSourceKind kullanir)."""

    CLIENT_SYSTEM_PROMPT = "client-system-prompt"
    CLIENT_TOOL_SCHEMA = "client-tool-schema"
    INSTRUCTION_GLOBAL = "instruction-global"
    INSTRUCTION_PROJECT = "instruction-project"
    INSTRUCTION_IMPORT = "instruction-import"
    SKILL = "skill"


def estimate_tokens(byte_count: int) -> int:
    """Belgeli kaba tahmin (ceil(bytes/4)); gercek tokenizer sayisi degildir."""

    if byte_count < 0:
        raise ValidationFailed("Byte sayisi negatif olamaz")
    return -(-byte_count // _BYTES_PER_TOKEN_ESTIMATE)


@dataclass(frozen=True, slots=True)
class SizeMeasure:
    """Birbirinden ayri boyutlar; hicbiri digerinden turetilmis gibi sunulmaz."""

    bytes_count: int | None = None
    char_count: int | None = None
    token_estimate: int | None = None
    token_method: str | None = None
    token_method_version: str | None = None

    def __post_init__(self) -> None:
        for value in (self.bytes_count, self.char_count, self.token_estimate):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValidationFailed("Olcum boyutu negatif olmayan tam sayi olmali")
        if (self.token_estimate is None) != (self.token_method is None) or (
            self.token_estimate is None
        ) != (self.token_method_version is None):
            raise ValidationFailed("Token tahmini yontem ve surum etiketi ister")

    @property
    def empty(self) -> bool:
        return self.bytes_count is None and self.char_count is None and self.token_estimate is None

    def body(self) -> dict[str, Any]:
        return {
            "bytes": self.bytes_count,
            "chars": self.char_count,
            "token_estimate": self.token_estimate,
            "token_method": self.token_method,
            "token_method_version": self.token_method_version,
        }


def measure_text(data: bytes) -> SizeMeasure:
    """Gercek byte/karakter + etiketli tahmin; tokenizer indirmez."""

    text = data.decode("utf-8", errors="replace")
    return SizeMeasure(
        bytes_count=len(data),
        char_count=len(text),
        token_estimate=estimate_tokens(len(data)),
        token_method=ESTIMATOR_METHOD,
        token_method_version=ESTIMATOR_VERSION,
    )


@dataclass(frozen=True, slots=True)
class EffectiveLoadEntry:
    source_kind: str
    logical_ref: str
    quality: MeasurementQuality
    load_reason: str
    load_level: str
    role: str | None = None
    route: str | None = None
    content_digest: str | None = None
    size: SizeMeasure = SizeMeasure()
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Context olcum girdisi authority uretemez")
        if not isinstance(self.quality, MeasurementQuality):
            raise ValidationFailed("Olcum niteligi registry disinda")
        for value, label in (
            (self.source_kind, "Olcum kaynak turu"),
            (self.load_reason, "Olcum yukleme nedeni"),
            (self.load_level, "Olcum yukleme seviyesi"),
        ):
            if not _TOKEN.match(value.lower()):
                raise ValidationFailed(f"{label} kisa registry degeri olmali")
        _safe_logical(self.logical_ref, "Olcum logical ref")
        for optional, optional_label in ((self.role, "Olcum rolu"), (self.route, "Olcum route")):
            if optional is not None:
                _safe_logical(optional, optional_label)
        if self.content_digest is not None:
            parse_digest(self.content_digest)
        if self.quality is MeasurementQuality.UNOBSERVABLE:
            if not self.size.empty or self.content_digest is not None:
                raise ValidationFailed("Gorulemeyen kaynak icin boyut/digest iddia edilemez")
        else:
            if self.size.empty:
                raise ValidationFailed("Gozlenen/tahmini kaynak en az bir boyut ister")
            if self.quality is MeasurementQuality.ESTIMATED and self.size.token_estimate is None:
                raise ValidationFailed("Estimated nitelik token tahmini ve yontem etiketi ister")
            if self.quality is MeasurementQuality.KNOWN_LOADED and self.content_digest is None:
                raise ValidationFailed("Known-loaded kaynak content digest ister")

    def body(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "logical_ref": self.logical_ref,
            "content_digest": self.content_digest,
            "role": self.role,
            "route": self.route,
            "load_reason": self.load_reason,
            "load_level": self.load_level,
            "quality": self.quality.value,
            "size": self.size.body(),
            "grants_authority": False,
        }


def _sum(values: Iterable[int | None]) -> int:
    return sum(value for value in values if value is not None)


@dataclass(frozen=True, slots=True)
class EffectiveContextReport:
    entries: tuple[EffectiveLoadEntry, ...]
    provider_usage_tokens: int | None = None
    provider_usage_source: str | None = None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("Context olcum raporu authority uretemez")
        if (self.provider_usage_tokens is None) != (self.provider_usage_source is None):
            raise ValidationFailed("Provider usage sayisi ve kaynagi birlikte verilmeli")
        if self.provider_usage_tokens is not None and (
            isinstance(self.provider_usage_tokens, bool) or self.provider_usage_tokens < 0
        ):
            raise ValidationFailed("Provider usage negatif olamaz")
        keys = [
            (item.source_kind, item.logical_ref, item.role, item.route) for item in self.entries
        ]
        if len(set(keys)) != len(keys):
            raise ValidationFailed("Olcum girdileri tekil olmali")

    @property
    def fully_observed(self) -> bool:
        return all(item.quality is MeasurementQuality.KNOWN_LOADED for item in self.entries)

    @property
    def completeness(self) -> str:
        if not self.entries:
            return "empty"
        return "observed" if self.fully_observed else "partial"

    @property
    def exact_effective_token_count(self) -> int | None:
        """Yalniz saglayici usage'i ve tam gozlem varken; aksi halde iddia edilmez."""

        return self.provider_usage_tokens if self.fully_observed and self.entries else None

    def totals(self) -> dict[str, Any]:
        """Boyut turleri ayri toplanir; tahmin ile exact byte karistirilmaz."""

        by_quality = {quality.value: 0 for quality in MeasurementQuality}
        for item in self.entries:
            by_quality[item.quality.value] += 1
        return {
            "entry_count": len(self.entries),
            "entries_by_quality": by_quality,
            "bytes": _sum(item.size.bytes_count for item in self.entries),
            "chars": _sum(item.size.char_count for item in self.entries),
            "token_estimate": _sum(item.size.token_estimate for item in self.entries),
            "token_estimate_methods": sorted(
                {
                    f"{item.size.token_method}@{item.size.token_method_version}"
                    for item in self.entries
                    if item.size.token_method is not None
                }
            ),
            "provider_usage_tokens": self.provider_usage_tokens,
            "provider_usage_source": self.provider_usage_source,
            "exact_effective_token_count": self.exact_effective_token_count,
            "unobservable_count": by_quality[MeasurementQuality.UNOBSERVABLE.value],
        }

    def by_source_kind(self) -> dict[str, dict[str, int]]:
        grouped: dict[str, dict[str, int]] = {}
        for item in self.entries:
            row = grouped.setdefault(
                item.source_kind, {"entries": 0, "bytes": 0, "chars": 0, "token_estimate": 0}
            )
            row["entries"] += 1
            row["bytes"] += item.size.bytes_count or 0
            row["chars"] += item.size.char_count or 0
            row["token_estimate"] += item.size.token_estimate or 0
        return dict(sorted(grouped.items()))

    def body(self) -> dict[str, Any]:
        return {
            "schema": MEASUREMENT_SCHEMA,
            "completeness": self.completeness,
            "full_effective_token_count_claimed": self.exact_effective_token_count is not None,
            "entries": [item.body() for item in self.entries],
            "totals": self.totals(),
            "by_source_kind": self.by_source_kind(),
            "grants_authority": False,
        }

    @property
    def report_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"report_digest": self.report_digest}


def compare_reports(
    before: EffectiveContextReport, after: EffectiveContextReport
) -> dict[str, Any]:
    """Kaynak turu bazinda fark; kisalan global dosyayla ayni kalan import toplamini ayirir."""

    old, new = before.by_source_kind(), after.by_source_kind()
    kinds = sorted(set(old) | set(new))
    zero = {"entries": 0, "bytes": 0, "chars": 0, "token_estimate": 0}
    deltas: dict[str, dict[str, int]] = {}
    for kind in kinds:
        left, right = old.get(kind, zero), new.get(kind, zero)
        deltas[kind] = {key: right[key] - left[key] for key in zero}
    totals_before, totals_after = before.totals(), after.totals()
    return {
        "by_source_kind": deltas,
        "total_bytes_delta": totals_after["bytes"] - totals_before["bytes"],
        "total_token_estimate_delta": (
            totals_after["token_estimate"] - totals_before["token_estimate"]
        ),
        "before_digest": before.report_digest,
        "after_digest": after.report_digest,
        "grants_authority": False,
    }


def report_from_manifest(
    manifest: ContextManifest,
    *,
    route: str | None = None,
    provider_usage_tokens: int | None = None,
    provider_usage_source: str | None = None,
) -> EffectiveContextReport:
    """Manifest secimlerinden known_loaded rapor; manifest digest'ini degistirmez."""

    entries: list[EffectiveLoadEntry] = []
    for item in manifest.selected:
        trace = item.measurement_trace()
        byte_exact = trace["bytes"] is not None
        entries.append(
            EffectiveLoadEntry(
                source_kind=str(trace["source_kind"]),
                logical_ref=str(trace["logical_ref"]),
                quality=MeasurementQuality.KNOWN_LOADED,
                load_reason=str(trace["load_reason"]),
                load_level=str(trace["load_level"]),
                role=manifest.target_role,
                route=route,
                content_digest=str(trace["content_digest"]),
                size=SizeMeasure(
                    bytes_count=trace["bytes"] if byte_exact else None,
                    token_estimate=int(trace["token_estimate"]),
                    token_method=str(trace["token_method"]),
                    token_method_version=str(trace["token_method_version"]),
                ),
            )
        )
    return EffectiveContextReport(tuple(entries), provider_usage_tokens, provider_usage_source)


def measured_instruction_entry(
    *,
    source_kind: InstructionSourceKind,
    logical_ref: str,
    data: bytes,
    load_reason: str,
    role: str | None = None,
    route: str | None = None,
    quality: MeasurementQuality = MeasurementQuality.DISCOVERED_ONLY,
) -> EffectiveLoadEntry:
    """Diskten okunan instruction dosyasi: boyut olculur ama yukleme gozlenmedikce
    ``discovered_only`` kalir; ``known_loaded`` yalniz Zekam kendisi yukledi ise verilir."""

    if quality not in {MeasurementQuality.DISCOVERED_ONLY, MeasurementQuality.KNOWN_LOADED}:
        raise ValidationFailed("Olculen instruction yalniz discovered_only veya known_loaded olur")
    return EffectiveLoadEntry(
        source_kind=source_kind.value,
        logical_ref=logical_ref,
        quality=quality,
        load_reason=load_reason,
        load_level="L2",
        role=role,
        route=route,
        content_digest=digest_of_bytes(data),
        size=measure_text(data),
    )


def unobservable_entry(
    *, source_kind: InstructionSourceKind, logical_ref: str, load_reason: str
) -> EffectiveLoadEntry:
    """Istemcinin gizli kismi; boyut/digest iddia edilmez."""

    return EffectiveLoadEntry(
        source_kind=source_kind.value,
        logical_ref=logical_ref,
        quality=MeasurementQuality.UNOBSERVABLE,
        load_reason=load_reason,
        load_level="L0",
    )
