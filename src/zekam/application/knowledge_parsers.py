"""Parser router ve yerlesik parser'lar.

Parser dogrudan vector uretmez: normalize edilmis, locator tasiyan `ContentUnit`
uretir. Bir format icin parser yoksa sessizce metin varsayilmaz; islem reddedilir.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import (
    ContentUnit,
    Locator,
    SourceFormat,
    UnitKind,
)

#: Markdown baslik satiri.
_HEADING = re.compile(r"^(#{1,6})\s+(?P<text>\S.*)$")
#: Markdown liste ogesi.
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\S")
#: Fenced kod blogu siniri.
_FENCE = re.compile(r"^\s*```")
_TRANSCRIPT_RANGE = re.compile(
    r"^\s*\[?(?P<start>\d{1,4}:\d{2}(?::\d{2}(?:[.,]\d{1,3})?)?)\s*"
    r"(?:-->|-|\N{EN DASH})\s*"
    r"(?P<end>\d{1,4}:\d{2}(?::\d{2}(?:[.,]\d{1,3})?)?)\]?"
    r"\s*(?:[:\N{EM DASH}]\s*)?(?P<text>\S.*)$"
)
_TRANSCRIPT_SINGLE = re.compile(
    r"^\s*\[?(?P<start>\d{1,4}:\d{2}(?::\d{2}(?:[.,]\d{1,3})?)?)\]?"
    r"\s*(?:[-:\N{EN DASH}\N{EM DASH}]\s*)?(?P<text>\S.*)$"
)
_TRANSCRIPT_METADATA = re.compile(
    r"^\s*(?:title|video[ _-]?id|date|language|baslik|video[ _-]?kimligi|tarih|dil)\s*:\s*\S",
    re.IGNORECASE,
)


def _timestamp_ms(value: str) -> int:
    """`MM:SS` veya `HH:MM:SS(.mmm)` degerini kesin milisaniyeye cevirir."""

    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) not in {2, 3}:
        raise ValidationFailed("timestamp bicimi gecersiz")
    seconds_text = parts[-1]
    seconds_parts = seconds_text.split(".", 1)
    seconds = int(seconds_parts[0])
    if seconds >= 60:
        raise ValidationFailed("timestamp saniyesi 60'tan kucuk olmali")
    milliseconds = int(seconds_parts[1].ljust(3, "0")) if len(seconds_parts) == 2 else 0
    if len(parts) == 2:
        minutes = int(parts[0])
        hours = 0
    else:
        hours = int(parts[0])
        minutes = int(parts[1])
        if minutes >= 60:
            raise ValidationFailed("timestamp dakikasi 60'tan kucuk olmali")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


class Parser(Protocol):
    """Bir kaynak formatini normalize icerige cevirir."""

    @property
    def parser_ref(self) -> str: ...

    @property
    def parser_version(self) -> str: ...

    @property
    def parser_profile(self) -> dict[str, Any]: ...

    def parse(self, payload: bytes) -> tuple[ContentUnit, ...]: ...


def _decode(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("icerik UTF-8 olarak cozulemedi") from exc


@dataclass(frozen=True, slots=True)
class PlainTextParser:
    """Duz metin: bos satirla ayrilmis paragraflar, blok indeksiyle konumlanir."""

    parser_ref: str = "zekam.parser.text"
    parser_version: str = "1"

    @property
    def parser_profile(self) -> dict[str, Any]:
        return {"schema": "zekam-parser-profile/v1", "encoding": "utf-8"}

    def parse(self, payload: bytes) -> tuple[ContentUnit, ...]:
        text = _decode(payload)
        units: list[ContentUnit] = []
        for index, block in enumerate(part.strip() for part in re.split(r"\n\s*\n", text)):
            if not block:
                continue
            units.append(
                ContentUnit(
                    unit_id=f"txt-{index}",
                    kind=UnitKind.PARAGRAPH,
                    text=block,
                    locator=Locator(block_index=index),
                    order=len(units),
                )
            )
        if not units:
            raise ValidationFailed("metin kaynagi bos")
        return tuple(units)


@dataclass(frozen=True, slots=True)
class MarkdownParser:
    """Markdown: baslik yolu korunur; kod ve liste ayri birim turu olur."""

    parser_ref: str = "zekam.parser.markdown"
    parser_version: str = "1"

    @property
    def parser_profile(self) -> dict[str, Any]:
        return {"schema": "zekam-parser-profile/v1", "encoding": "utf-8"}

    def parse(self, payload: bytes) -> tuple[ContentUnit, ...]:
        lines = _decode(payload).splitlines()
        units: list[ContentUnit] = []
        heading_path: list[str] = []
        buffer: list[str] = []
        buffer_kind = UnitKind.PARAGRAPH
        in_code = False

        def flush(block_index: int) -> None:
            nonlocal buffer, buffer_kind
            body = "\n".join(buffer).strip()
            buffer = []
            if not body:
                buffer_kind = UnitKind.PARAGRAPH
                return
            units.append(
                ContentUnit(
                    unit_id=f"md-{len(units)}",
                    kind=buffer_kind,
                    text=body,
                    locator=Locator(
                        heading_path=tuple(heading_path),
                        block_index=block_index,
                        line_start=max(1, block_index - len(body.splitlines()) + 1),
                        line_end=max(1, block_index),
                    ),
                    order=len(units),
                )
            )
            buffer_kind = UnitKind.PARAGRAPH

        for index, line in enumerate(lines, start=1):
            if _FENCE.match(line):
                if in_code:
                    flush(index)
                    in_code = False
                else:
                    flush(index)
                    in_code = True
                    buffer_kind = UnitKind.CODE
                continue
            if in_code:
                buffer.append(line)
                continue
            heading = _HEADING.match(line)
            if heading is not None:
                flush(index)
                level = len(heading.group(1))
                del heading_path[level - 1 :]
                heading_path.append(heading.group("text").strip())
                units.append(
                    ContentUnit(
                        unit_id=f"md-{len(units)}",
                        kind=UnitKind.HEADING,
                        text=heading.group("text").strip(),
                        locator=Locator(
                            heading_path=tuple(heading_path),
                            block_index=index,
                            line_start=index,
                            line_end=index,
                        ),
                        order=len(units),
                    )
                )
                continue
            if not line.strip():
                flush(index)
                continue
            if _LIST_ITEM.match(line) and not buffer:
                buffer_kind = UnitKind.LIST
            buffer.append(line)

        flush(len(lines) + 1)
        if not units:
            raise ValidationFailed("markdown kaynagi bos")
        return tuple(units)


@dataclass(frozen=True, slots=True)
class TimestampTranscriptParser:
    """Zaman damgali transcript metnini citation-bound birimlere ayirir.

    Parser speaker veya timestamp tahmin etmez. Kaynakta acik tek timestamp ya
    da aralik varsa onu korur; parse edilemeyen satirlar yalniz line locator alir.
    """

    entry_path: str
    video_id: str | None = None
    max_merged_lines: int = 8
    max_merged_chars: int = 4000
    parser_ref: str = "zekam.parser.timestamp-transcript"
    parser_version: str = "1"

    def __post_init__(self) -> None:
        from zekam.domain.knowledge import assert_safe_relative

        assert_safe_relative(self.entry_path, "transcript entry path")
        if self.video_id is not None and not self.video_id.strip():
            raise ValidationFailed("video kimligi bos olamaz")
        if self.max_merged_lines < 1 or self.max_merged_chars < 1:
            raise ValidationFailed("transcript birlestirme siniri pozitif olmali")

    @property
    def parser_profile(self) -> dict[str, Any]:
        return {
            "schema": "zekam-parser-profile/v1",
            "adapter": self.parser_ref,
            "adapter_version": self.parser_version,
            "encoding": "utf-8",
            "timestamp_formats": ["MM:SS", "HH:MM:SS", "HH:MM:SS.mmm"],
            "speaker_inference": False,
            "max_merged_lines": self.max_merged_lines,
            "max_merged_chars": self.max_merged_chars,
        }

    def parse(self, payload: bytes) -> tuple[ContentUnit, ...]:
        lines = _decode(payload).splitlines()
        units: list[ContentUnit] = []
        plain: list[tuple[int, str]] = []

        def locator(
            line_start: int,
            line_end: int,
            *,
            timestamp_start_ms: int | None = None,
            timestamp_end_ms: int | None = None,
        ) -> Locator:
            return Locator(
                entry_path=self.entry_path,
                line_start=line_start,
                line_end=line_end,
                timestamp_start_ms=timestamp_start_ms,
                timestamp_end_ms=timestamp_end_ms,
                video_id=self.video_id,
            )

        def append(kind: UnitKind, text: str, item_locator: Locator) -> None:
            units.append(
                ContentUnit(
                    unit_id=f"transcript-{len(units)}",
                    kind=kind,
                    text=text,
                    locator=item_locator,
                    order=len(units),
                )
            )

        def flush_plain() -> None:
            nonlocal plain
            if not plain:
                return
            append(
                UnitKind.PARAGRAPH,
                "\n".join(text for _, text in plain),
                locator(plain[0][0], plain[-1][0]),
            )
            plain = []

        def add_plain(line_number: int, line: str) -> None:
            pending_chars = sum(len(text) for _, text in plain) + len(plain) + len(line)
            if plain and (
                len(plain) >= self.max_merged_lines or pending_chars > self.max_merged_chars
            ):
                flush_plain()
            plain.append((line_number, line))

        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line:
                flush_plain()
                continue
            match = _TRANSCRIPT_RANGE.match(raw_line)
            if match is not None:
                try:
                    start_ms = _timestamp_ms(match.group("start"))
                    end_ms = _timestamp_ms(match.group("end"))
                except ValidationFailed:
                    # Bicim timestamp'e benzese de parse edilemiyorsa satir korunur.
                    add_plain(line_number, line)
                    continue
                if end_ms <= start_ms:
                    add_plain(line_number, line)
                    continue
                flush_plain()
                append(
                    UnitKind.TRANSCRIPT_SEGMENT,
                    match.group("text").strip(),
                    locator(
                        line_number,
                        line_number,
                        timestamp_start_ms=start_ms,
                        timestamp_end_ms=end_ms,
                    ),
                )
                continue
            single = _TRANSCRIPT_SINGLE.match(raw_line)
            if single is not None:
                try:
                    start_ms = _timestamp_ms(single.group("start"))
                except ValidationFailed:
                    add_plain(line_number, line)
                    continue
                flush_plain()
                append(
                    UnitKind.TRANSCRIPT_SEGMENT,
                    single.group("text").strip(),
                    locator(
                        line_number,
                        line_number,
                        timestamp_start_ms=start_ms,
                    ),
                )
            elif _TRANSCRIPT_METADATA.match(raw_line):
                flush_plain()
                append(UnitKind.METADATA, line, locator(line_number, line_number))
            elif line.startswith("#") and line.lstrip("#").strip():
                flush_plain()
                append(
                    UnitKind.TRANSCRIPT_HEADING,
                    line.lstrip("#").strip(),
                    locator(line_number, line_number),
                )
            else:
                add_plain(line_number, line)
        flush_plain()
        if not units:
            raise ValidationFailed("transcript kaynagi bos")
        return tuple(units)


@dataclass(frozen=True, slots=True)
class OcrParser:
    """OCR saglayicisini normalize icerige baglar.

    Saglayici disaridan verilir; her blok sayfa, bbox ve confidence tasimalidir.
    Bu alanlar eksikse uydurulmaz, birim reddedilir.
    """

    provider: Callable[[bytes], tuple[dict[str, Any], ...]]
    parser_ref: str = "zekam.parser.ocr"
    parser_version: str = "1"
    minimum_confidence: float = 0.0

    @property
    def parser_profile(self) -> dict[str, Any]:
        return {
            "schema": "zekam-parser-profile/v1",
            "adapter": self.parser_ref,
            "minimum_confidence": self.minimum_confidence,
        }

    def parse(self, payload: bytes) -> tuple[ContentUnit, ...]:
        units: list[ContentUnit] = []
        for index, block in enumerate(self.provider(payload)):
            missing = {"text", "page", "bbox", "confidence"} - set(block)
            if missing:
                raise ValidationFailed(f"OCR blogu eksik alan tasiyor: {sorted(missing)}")
            confidence = float(block["confidence"])
            if confidence < self.minimum_confidence:
                continue
            corners = tuple(float(value) for value in block["bbox"])
            if len(corners) != 4:
                raise ValidationFailed("OCR bbox dort deger ister")
            bbox = (corners[0], corners[1], corners[2], corners[3])
            units.append(
                ContentUnit(
                    unit_id=f"ocr-{index}",
                    kind=UnitKind.OCR_BLOCK,
                    text=str(block["text"]),
                    locator=Locator(page=int(block["page"]), bbox=bbox),
                    order=len(units),
                    confidence=confidence,
                )
            )
        if not units:
            raise ValidationFailed("OCR sonucu bos")
        return tuple(units)


@dataclass(frozen=True, slots=True)
class StructuredDocumentParser:
    """DOCX/PDF gibi yapili belgeler icin saglayici temelli parser.

    Saglayici her blok icin tur ve locator bilgisi dondurur. PDF'te sayfa,
    DOCX'te heading path beklenir; **uydurma sayfa numarasi uretilmez**.
    """

    provider: Callable[[bytes], tuple[dict[str, Any], ...]]
    source_format: SourceFormat
    parser_ref: str = "zekam.parser.structured"
    parser_version: str = "1"

    @property
    def parser_profile(self) -> dict[str, Any]:
        return {
            "schema": "zekam-parser-profile/v1",
            "adapter": self.parser_ref,
            "source_format": str(self.source_format),
        }

    def parse(self, payload: bytes) -> tuple[ContentUnit, ...]:
        units: list[ContentUnit] = []
        for index, block in enumerate(self.provider(payload)):
            if "text" not in block or "kind" not in block:
                raise ValidationFailed("yapili blok text ve kind ister")
            kind = UnitKind(str(block["kind"]))
            locator = self._locator(block)
            if locator.is_empty:
                raise ValidationFailed("yapili blok locator ister")
            units.append(
                ContentUnit(
                    unit_id=f"doc-{index}",
                    kind=kind,
                    text=str(block["text"]),
                    locator=locator,
                    order=len(units),
                )
            )
        if not units:
            raise ValidationFailed("yapili belge bos")
        return tuple(units)

    def _locator(self, block: dict[str, Any]) -> Locator:
        page = block.get("page")
        if self.source_format is SourceFormat.DOCX and page is not None:
            raise PolicyViolation("DOCX icin sayfa numarasi uydurulamaz")
        heading = tuple(block.get("heading_path", ()))
        return Locator(
            page=int(page) if page is not None else None,
            heading_path=heading,
            block_index=block.get("block_index"),
        )


@dataclass(frozen=True, slots=True)
class ParserRouter:
    """Formata gore parser secer. Bilinmeyen format sessizce metin sayilmaz."""

    parsers: dict[SourceFormat, Parser]

    def resolve(self, source_format: SourceFormat) -> Parser:
        parser = self.parsers.get(source_format)
        if parser is None:
            raise PolicyViolation(f"format icin parser tanimli degil: {source_format}")
        return parser

    def parse(self, source_format: SourceFormat, payload: bytes) -> tuple[ContentUnit, ...]:
        return self.resolve(source_format).parse(payload)

    def supported(self) -> tuple[SourceFormat, ...]:
        return tuple(sorted(self.parsers, key=str))


def default_router() -> ParserRouter:
    """Metin ve izin verici lisansli yerel belge parser'larini kurar."""

    from zekam.infrastructure.knowledge.document_parsers import offline_document_parsers

    return ParserRouter(
        {
            SourceFormat.TXT: PlainTextParser(),
            SourceFormat.MARKDOWN: MarkdownParser(),
            **offline_document_parsers(),
        }
    )
