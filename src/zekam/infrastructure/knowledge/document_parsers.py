"""Cevrimdisi DOCX, PDF ve OCR parser adapter'lari.

Bu modul ag veya bulut saglayicisi kullanmaz. DOCX stdlib OOXML ile okunur;
OCR Tesseract CLI'ya typed argv ile gider; PDFium bagimliligi yoksa PDF destegi
fail-closed ``ConfigurationError`` uretir.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import io
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.knowledge import (
    MAX_DOCUMENT_BYTES,
    ContentUnit,
    Locator,
    SourceFormat,
    UnitKind,
)

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_W = f"{{{_WORD_NS}}}"
_R = f"{{{_REL_NS}}}"

MAX_DOCX_ENTRIES = 4_096
MAX_DOCX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 100
MAX_IMAGE_PIXELS = 40_000_000
MAX_PDF_PAGES = 1_000

_PDFIUM_PERMISSIVE_BUILD_LICENSE_FILES = frozenset(
    {
        "abseil.txt",
        "agg23.txt",
        "fast_float.txt",
        "freetype.txt",
        "icu.txt",
        "lcms.txt",
        "libjpeg_turbo.ijg",
        "libjpeg_turbo.md",
        "libopenjpeg.txt",
        "libpng.txt",
        "libtiff.txt",
        "llvm-libc.txt",
        "pdfium-binaries.txt",
        "pdfium.txt",
        "simdutf.txt",
        "zlib.txt",
    }
)

_MEDIA_TYPES: dict[SourceFormat, str] = {
    SourceFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    SourceFormat.PDF: "application/pdf",
    SourceFormat.PNG: "image/png",
    SourceFormat.JPEG: "image/jpeg",
    SourceFormat.TIFF: "image/tiff",
}


def media_type_for(source_format: SourceFormat) -> str:
    """Bir belge formati icin kanonik media type dondurur."""

    try:
        return _MEDIA_TYPES[source_format]
    except KeyError as exc:
        raise ValidationFailed(f"media type tanimli degil: {source_format}") from exc


def _safe_zip_name(name: str) -> None:
    path = PurePosixPath(name)
    if not name or name.startswith("/") or "\\" in name or ".." in path.parts:
        raise PolicyViolation("DOCX zip traversal girdisi tasiyor")


def _xml(payload: bytes, label: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ValidationFailed(f"DOCX {label} XML gecersiz") from exc


def _text(element: ElementTree.Element) -> str:
    chunks: list[str] = []
    for node in element.iter():
        if node.tag == f"{_W}t" and node.text:
            chunks.append(node.text)
        elif node.tag == f"{_W}tab":
            chunks.append("\t")
        elif node.tag in {f"{_W}br", f"{_W}cr"}:
            chunks.append("\n")
    return "".join(chunks).strip()


def _relationship_is_external(root: ElementTree.Element) -> bool:
    return any(
        node.tag == f"{_R}Relationship"
        and str(node.attrib.get("TargetMode", "")).lower() == "external"
        for node in root
    )


@dataclass(frozen=True, slots=True)
class DocxParser:
    """Stdlib tabanli, bounded OOXML DOCX parser'i."""

    parser_ref: str = "zekam.parser.docx-ooxml"
    parser_version: str = "1"

    @property
    def parser_profile(self) -> dict[str, Any]:
        return {
            "schema": "zekam-parser-profile/v1",
            "adapter": self.parser_ref,
            "adapter_version": self.parser_version,
            "container": "ooxml-zip",
            "xml_engine": "python-stdlib-elementtree",
            "license_ids": ["PSF-2.0"],
            "locator": "heading-path+block-index",
            "limits": {
                "entries": MAX_DOCX_ENTRIES,
                "expanded_bytes": MAX_DOCX_EXPANDED_BYTES,
                "compression_ratio": MAX_DOCX_COMPRESSION_RATIO,
            },
        }

    def parse(self, payload: bytes) -> tuple[ContentUnit, ...]:
        if len(payload) > MAX_DOCUMENT_BYTES or not payload.startswith(b"PK"):
            raise ValidationFailed("DOCX imzasi veya boyutu gecersiz")
        try:
            bundle = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise ValidationFailed("DOCX zip yapisi gecersiz") from exc
        with bundle:
            infos = bundle.infolist()
            if len(infos) > MAX_DOCX_ENTRIES:
                raise PolicyViolation("DOCX girdi sayisi sinirini asiyor")
            expanded = sum(item.file_size for item in infos)
            compressed = sum(item.compress_size for item in infos)
            if expanded > MAX_DOCX_EXPANDED_BYTES:
                raise PolicyViolation("DOCX acilmis boyut sinirini asiyor")
            if compressed > 0 and expanded / compressed > MAX_DOCX_COMPRESSION_RATIO:
                raise PolicyViolation("DOCX sikistirma orani sinirini asiyor")
            names = {item.filename for item in infos}
            for name in names:
                _safe_zip_name(name)
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise ValidationFailed("DOCX zorunlu OOXML parcalarini tasimiyor")
            for name in sorted(value for value in names if value.endswith(".rels")):
                if _relationship_is_external(_xml(bundle.read(name), name)):
                    raise PolicyViolation("DOCX external relationship tasiyor")
            styles = (
                self._heading_styles(_xml(bundle.read("word/styles.xml"), "styles"))
                if "word/styles.xml" in names
                else {}
            )
            document = _xml(bundle.read("word/document.xml"), "document")
        return self._units(document, styles)

    @staticmethod
    def _heading_styles(root: ElementTree.Element) -> dict[str, int]:
        found: dict[str, int] = {}
        for style in root.findall(f".//{_W}style"):
            style_id = style.attrib.get(f"{_W}styleId")
            if not style_id:
                continue
            level: int | None = None
            outline = style.find(f".//{_W}outlineLvl")
            if outline is not None and outline.attrib.get(f"{_W}val", "").isdigit():
                level = int(outline.attrib[f"{_W}val"]) + 1
            name = style.find(f"{_W}name")
            label = "" if name is None else name.attrib.get(f"{_W}val", "")
            match = re.fullmatch(r"heading\s*([1-9])", label, re.IGNORECASE)
            if level is None and match:
                level = int(match.group(1))
            if level is not None and 1 <= level <= 9:
                found[style_id] = level
        return found

    @staticmethod
    def _units(
        document: ElementTree.Element, heading_styles: dict[str, int]
    ) -> tuple[ContentUnit, ...]:
        body = document.find(f"{_W}body")
        if body is None:
            raise ValidationFailed("DOCX document body bulunamadi")
        units: list[ContentUnit] = []
        headings: list[str] = []
        block_index = 0
        for child in body:
            if child.tag == f"{_W}p":
                value = _text(child)
                if not value:
                    block_index += 1
                    continue
                style_node = child.find(f"./{_W}pPr/{_W}pStyle")
                style_id = None if style_node is None else style_node.attrib.get(f"{_W}val")
                level = heading_styles.get(style_id or "")
                kind = UnitKind.PARAGRAPH
                if level is not None:
                    del headings[level - 1 :]
                    while len(headings) < level - 1:
                        headings.append("")
                    headings.append(value)
                    kind = UnitKind.HEADING
                units.append(
                    ContentUnit(
                        unit_id=f"docx-{len(units)}",
                        kind=kind,
                        text=value,
                        locator=Locator(
                            heading_path=tuple(item for item in headings if item),
                            block_index=block_index,
                        ),
                        order=len(units),
                    )
                )
                block_index += 1
            elif child.tag == f"{_W}tbl":
                rows: list[str] = []
                for row in child.findall(f"{_W}tr"):
                    cells = [_text(cell) for cell in row.findall(f"{_W}tc")]
                    if any(cells):
                        rows.append(" | ".join(cells))
                if rows:
                    units.append(
                        ContentUnit(
                            unit_id=f"docx-{len(units)}",
                            kind=UnitKind.TABLE,
                            text="\n".join(rows),
                            locator=Locator(
                                heading_path=tuple(item for item in headings if item),
                                block_index=block_index,
                            ),
                            order=len(units),
                        )
                    )
                block_index += 1
        if not units:
            raise ValidationFailed("DOCX normalize edilebilir icerik tasimiyor")
        return tuple(units)


def _png_dimensions(payload: bytes) -> tuple[tuple[int, int], ...]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValidationFailed("PNG imzasi gecersiz")
    width, height = struct.unpack(">II", payload[16:24])
    return ((width, height),)


def _jpeg_dimensions(payload: bytes) -> tuple[tuple[int, int], ...]:
    if len(payload) < 4 or payload[:2] != b"\xff\xd8":
        raise ValidationFailed("JPEG imzasi gecersiz")
    offset = 2
    while offset + 4 <= len(payload):
        if payload[offset] != 0xFF:
            offset += 1
            continue
        marker = payload[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        length = int.from_bytes(payload[offset : offset + 2], "big")
        if length < 2 or offset + length > len(payload):
            raise ValidationFailed("JPEG segment yapisi gecersiz")
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}:
            height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            return ((width, height),)
        offset += length
    raise ValidationFailed("JPEG boyut bilgisi bulunamadi")


def _tiff_value(payload: bytes, endian: str, value_type: int, count: int, raw: bytes) -> int | None:
    if count != 1 or value_type not in {3, 4}:
        return None
    fmt = f"{endian}{'H' if value_type == 3 else 'I'}"
    size = 2 if value_type == 3 else 4
    return int(struct.unpack(fmt, raw[:size])[0])


def _tiff_dimensions(payload: bytes) -> tuple[tuple[int, int], ...]:
    if len(payload) < 8 or payload[:2] not in {b"II", b"MM"}:
        raise ValidationFailed("TIFF imzasi gecersiz")
    endian = "<" if payload[:2] == b"II" else ">"
    if struct.unpack(f"{endian}H", payload[2:4])[0] != 42:
        raise ValidationFailed("TIFF header gecersiz")
    offset = struct.unpack(f"{endian}I", payload[4:8])[0]
    pages: list[tuple[int, int]] = []
    seen: set[int] = set()
    while offset:
        if offset in seen or offset + 2 > len(payload):
            raise ValidationFailed("TIFF IFD zinciri gecersiz")
        seen.add(offset)
        count = struct.unpack(f"{endian}H", payload[offset : offset + 2])[0]
        cursor = offset + 2
        width: int | None = None
        height: int | None = None
        for _ in range(count):
            if cursor + 12 > len(payload):
                raise ValidationFailed("TIFF IFD girdisi kesik")
            tag, value_type, values = struct.unpack(f"{endian}HHI", payload[cursor : cursor + 8])
            value = _tiff_value(
                payload, endian, value_type, values, payload[cursor + 8 : cursor + 12]
            )
            if tag == 256:
                width = value
            elif tag == 257:
                height = value
            cursor += 12
        if width is None or height is None:
            raise ValidationFailed("TIFF sayfa boyutu bulunamadi")
        pages.append((width, height))
        if cursor + 4 > len(payload):
            raise ValidationFailed("TIFF sonraki IFD alani kesik")
        offset = struct.unpack(f"{endian}I", payload[cursor : cursor + 4])[0]
    return tuple(pages)


def _dimensions(source_format: SourceFormat, payload: bytes) -> tuple[tuple[int, int], ...]:
    if source_format is SourceFormat.PNG:
        result = _png_dimensions(payload)
    elif source_format is SourceFormat.JPEG:
        result = _jpeg_dimensions(payload)
    elif source_format is SourceFormat.TIFF:
        result = _tiff_dimensions(payload)
    else:
        raise ValidationFailed("OCR parser gorsel format ister")
    if not result or any(width <= 0 or height <= 0 for width, height in result):
        raise ValidationFailed("gorsel boyutu gecersiz")
    if any(width * height > MAX_IMAGE_PIXELS for width, height in result):
        raise PolicyViolation("gorsel pixel sinirini asiyor")
    return result


def _tesseract_version(executable: str) -> str:
    try:
        output = subprocess.run(
            [executable, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        raise ConfigurationError("Tesseract surumu okunamadi") from exc
    match = re.match(r"tesseract\s+v?([0-9][0-9A-Za-z.+-]*)", output, re.IGNORECASE)
    if match is None:
        raise ConfigurationError("Tesseract surum ciktisi gecersiz")
    return match.group(1)


def _tessdata_digests(executable: str, languages: tuple[str, ...]) -> dict[str, str]:
    try:
        result = subprocess.run(
            [executable, "--list-langs"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigurationError("Tesseract dil listesi okunamadi") from exc
    first = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    match = re.search(r'"([^"]+)"', first)
    if match is None:
        raise ConfigurationError("Tesseract tessdata konumu bulunamadi")
    root = Path(match.group(1))
    found: dict[str, str] = {}
    for language in languages:
        model = root / f"{language}.traineddata"
        if not model.is_file():
            raise ConfigurationError(f"Tesseract dili bulunamadi: {language}")
        found[language] = "sha256:" + hashlib.sha256(model.read_bytes()).hexdigest()
    return found


@dataclass(frozen=True, slots=True)
class TesseractOcrParser:
    """Yerel Tesseract TSV ciktisini locator'li OCR bloklarina cevirir."""

    source_format: SourceFormat
    languages: tuple[str, ...] = ("tur", "eng")
    minimum_confidence: float = 0.0
    timeout_seconds: int = 60
    oem: int = 1
    psm: int = 3
    executable: str | None = None
    parser_ref: str = "zekam.parser.tesseract"
    parser_version: str = "1"

    def __post_init__(self) -> None:
        if self.source_format not in {SourceFormat.PNG, SourceFormat.JPEG, SourceFormat.TIFF}:
            raise ValidationFailed("Tesseract parser gorsel format ister")
        if not self.languages or any(
            not re.fullmatch(r"[A-Za-z0-9_]+", item) for item in self.languages
        ):
            raise ValidationFailed("Tesseract dil kodu gecersiz")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValidationFailed("OCR confidence esigi 0..1 olmali")

    def _executable(self) -> str:
        candidate = self.executable or shutil.which("tesseract")
        if not candidate:
            raise ConfigurationError("Tesseract bulunamadi")
        return candidate

    @property
    def parser_profile(self) -> dict[str, Any]:
        executable = self._executable()
        return {
            "schema": "zekam-parser-profile/v1",
            "adapter": self.parser_ref,
            "adapter_version": self.parser_version,
            "engine": "tesseract",
            "engine_version": _tesseract_version(executable),
            "engine_license": "Apache-2.0",
            "image_library": "leptonica",
            "image_library_license": "BSD-2-Clause",
            "languages": list(self.languages),
            "tessdata_digests": _tessdata_digests(executable, self.languages),
            "oem": self.oem,
            "psm": self.psm,
            "minimum_confidence": self.minimum_confidence,
            "bbox_space": "normalized-top-left-0..1",
            "timeout_seconds": self.timeout_seconds,
        }

    def parse(self, payload: bytes) -> tuple[ContentUnit, ...]:
        if not payload or len(payload) > MAX_DOCUMENT_BYTES:
            raise ValidationFailed("OCR payload boyutu gecersiz")
        dimensions = _dimensions(self.source_format, payload)
        suffix = {
            SourceFormat.PNG: ".png",
            SourceFormat.JPEG: ".jpg",
            SourceFormat.TIFF: ".tiff",
        }[self.source_format]
        with tempfile.TemporaryDirectory(prefix="zekam-ocr-") as directory:
            source = Path(directory) / f"input{suffix}"
            source.write_bytes(payload)
            try:
                result = subprocess.run(
                    [
                        self._executable(),
                        str(source),
                        "stdout",
                        "-l",
                        "+".join(self.languages),
                        "--oem",
                        str(self.oem),
                        "--psm",
                        str(self.psm),
                        "tsv",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise PolicyViolation("Tesseract zaman asimina ugradi") from exc
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ValidationFailed("Tesseract OCR basarisiz") from exc
        return self._units(result.stdout, dimensions)

    def _units(self, tsv: str, dimensions: tuple[tuple[int, int], ...]) -> tuple[ContentUnit, ...]:
        required = {
            "level",
            "page_num",
            "block_num",
            "par_num",
            "line_num",
            "left",
            "top",
            "width",
            "height",
            "conf",
            "text",
        }
        reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValidationFailed("Tesseract TSV kolonlari eksik")
        grouped: dict[tuple[int, int, int, int], list[tuple[str, int, int, int, int, float]]] = (
            defaultdict(list)
        )
        for row in reader:
            if row["level"] != "5" or not row["text"].strip():
                continue
            try:
                confidence = float(row["conf"]) / 100.0
                page = int(row["page_num"])
                key = (page, int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
                box = (int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"]))
            except (TypeError, ValueError) as exc:
                raise ValidationFailed("Tesseract TSV sayisal alani gecersiz") from exc
            if confidence < self.minimum_confidence or confidence < 0:
                continue
            grouped[key].append((row["text"].strip(), *box, confidence))
        units: list[ContentUnit] = []
        for key in sorted(grouped):
            page = key[0]
            if page < 1 or page > len(dimensions):
                raise ValidationFailed("Tesseract TSV sayfa numarasi gecersiz")
            width, height = dimensions[page - 1]
            words = grouped[key]
            x0 = min(item[1] for item in words)
            y0 = min(item[2] for item in words)
            x1 = max(item[1] + item[3] for item in words)
            y1 = max(item[2] + item[4] for item in words)
            bbox = (x0 / width, y0 / height, min(x1 / width, 1.0), min(y1 / height, 1.0))
            confidence = sum(item[5] for item in words) / len(words)
            units.append(
                ContentUnit(
                    unit_id=f"ocr-{len(units)}",
                    kind=UnitKind.OCR_BLOCK,
                    text=" ".join(item[0] for item in words),
                    locator=Locator(page=page, bbox=bbox),
                    order=len(units),
                    confidence=confidence,
                )
            )
        if not units:
            raise ValidationFailed("OCR sonucu bos")
        return tuple(units)


def _pdfium_build_profile(module: Any) -> dict[str, Any]:
    del module
    try:
        from pypdfium2_raw.version import PDFIUM_INFO
    except ImportError as exc:
        raise ConfigurationError("PDFium build metadata bulunamadi") from exc
    flags = tuple(PDFIUM_INFO.flags or ())
    if any(str(flag).upper() in {"V8", "XFA"} for flag in flags):
        raise PolicyViolation("PDFium V8/XFA build reddedildi")
    try:
        distribution = importlib.metadata.distribution("pypdfium2")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ConfigurationError("pypdfium2 package metadata bulunamadi") from exc
    build_files = tuple(
        item
        for item in (distribution.files or ())
        if "BUILD_LICENSES" in str(item) and Path(str(item)).name
    )
    names = frozenset(Path(str(item)).name for item in build_files)
    unexpected = names - _PDFIUM_PERMISSIVE_BUILD_LICENSE_FILES
    missing = _PDFIUM_PERMISSIVE_BUILD_LICENSE_FILES - names
    if unexpected or missing:
        raise PolicyViolation(
            "PDFium exact wheel BUILD_LICENSES izin verici allowlist ile eslesmiyor"
        )
    manifest = []
    for item in sorted(build_files, key=str):
        payload = Path(str(distribution.locate_file(item))).read_bytes()
        manifest.append((Path(str(item)).name, hashlib.sha256(payload).hexdigest()))
    return {
        "package": "pypdfium2",
        "package_version": distribution.version,
        "package_license": "Apache-2.0 OR BSD-3-Clause",
        "engine": "PDFium",
        "engine_license": "BSD-3-Clause",
        "build_origin": str(PDFIUM_INFO.origin),
        "build_flags": [str(flag) for flag in flags],
        "build_license_manifest_digest": "sha256:"
        + hashlib.sha256(repr(manifest).encode("utf-8")).hexdigest(),
        "license_gate": "exact-wheel-build-licenses-allowlisted",
    }


@dataclass(frozen=True, slots=True)
class PdfParser:
    """PDFium ile dijital metin, bos sayfalarda Tesseract OCR kullanan parser."""

    ocr_languages: tuple[str, ...] = ("tur", "eng")
    minimum_digital_characters: int = 12
    dpi: int = 300
    parser_ref: str = "zekam.parser.pdfium"
    parser_version: str = "1"

    @staticmethod
    def _module() -> Any:
        try:
            import pypdfium2
        except ImportError as exc:
            raise ConfigurationError(
                "PDF parser icin izin verici `zekam[knowledge-docs]` bagimliligi kurulmali"
            ) from exc
        return pypdfium2

    @property
    def parser_profile(self) -> dict[str, Any]:
        module = self._module()
        return {
            "schema": "zekam-parser-profile/v1",
            "adapter": self.parser_ref,
            "adapter_version": self.parser_version,
            **_pdfium_build_profile(module),
            "dpi": self.dpi,
            "digital_character_threshold": self.minimum_digital_characters,
            "ocr_languages": list(self.ocr_languages),
            "bbox_space": "normalized-top-left-0..1",
        }

    def parse(self, payload: bytes) -> tuple[ContentUnit, ...]:
        if len(payload) > MAX_DOCUMENT_BYTES or not payload.startswith(b"%PDF-"):
            raise ValidationFailed("PDF imzasi veya boyutu gecersiz")
        module = self._module()
        _pdfium_build_profile(module)
        try:
            document = module.PdfDocument(payload)
        except Exception as exc:
            raise ValidationFailed("PDF acilamadi") from exc
        if len(document) < 1 or len(document) > MAX_PDF_PAGES:
            raise PolicyViolation("PDF sayfa sayisi sinir disinda")
        units: list[ContentUnit] = []
        try:
            for page_index in range(len(document)):
                page = document[page_index]
                text_page = page.get_textpage()
                text = str(text_page.get_text_bounded()).strip()
                if len(re.sub(r"\s+", "", text)) >= self.minimum_digital_characters:
                    units.append(
                        ContentUnit(
                            unit_id=f"pdf-{len(units)}",
                            kind=UnitKind.PARAGRAPH,
                            text=text,
                            locator=Locator(page=page_index + 1),
                            order=len(units),
                        )
                    )
                    continue
                bitmap = page.render(scale=self.dpi / 72.0)
                image = bitmap.to_pil()
                with io.BytesIO() as stream:
                    image.save(stream, format="PNG")
                    image_payload = stream.getvalue()
                ocr = TesseractOcrParser(
                    SourceFormat.PNG,
                    languages=self.ocr_languages,
                ).parse(image_payload)
                for block in ocr:
                    units.append(
                        ContentUnit(
                            unit_id=f"pdf-{len(units)}",
                            kind=block.kind,
                            text=block.text,
                            locator=Locator(
                                page=page_index + 1,
                                bbox=block.locator.bbox,
                            ),
                            order=len(units),
                            confidence=block.confidence,
                        )
                    )
        finally:
            document.close()
        if not units:
            raise ValidationFailed("PDF normalize edilebilir icerik tasimiyor")
        return tuple(units)


def offline_document_parsers() -> dict[SourceFormat, Any]:
    """Yerel belge parser kayitlarini dondurur.

    PDF parser kaydi dependency olmasa da vardir; kullanildiginda fail-closed
    kurulum mesaji verir. Boylece format sessizce TXT sayilmaz.
    """

    return {
        SourceFormat.DOCX: DocxParser(),
        SourceFormat.PDF: PdfParser(),
        SourceFormat.PNG: TesseractOcrParser(SourceFormat.PNG),
        SourceFormat.JPEG: TesseractOcrParser(SourceFormat.JPEG),
        SourceFormat.TIFF: TesseractOcrParser(SourceFormat.TIFF),
    }
