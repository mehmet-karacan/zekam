"""Sinirli ve guvenli XML okuyucu (yalniz stdlib ``pyexpat``; yeni bagimlilik yok).

- Boyut, derinlik ve eleman sayisi sinirlidir; sinir asilirsa reddedilir.
- Dahili/harici entity bildirimi, parametre entity'si ve harici DTD cozumlemesi yoktur;
  ag veya dosya sistemi okunmaz. JaCoCo raporundaki sabit ``report.dtd`` DOCTYPE'u
  yalniz acikca izin verilen (publicId, systemId) cifti icin kabul edilir ve yine
  cozumlenmez.
- Hata mesaji ham icerik tasimaz.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from xml.parsers import expat

from zekam.domain.errors import ValidationFailed

DEFAULT_MAX_BYTES: Final = 64 * 1024 * 1024
DEFAULT_MAX_DEPTH: Final = 64
DEFAULT_MAX_NODES: Final = 2_000_000

#: JaCoCo XML raporunun standart DOCTYPE'u (cozumlenmez, yalniz taninir).
JACOCO_DOCTYPES: Final = frozenset({("-//JACOCO//DTD Report 1.1//EN", "report.dtd")})


@dataclass(slots=True)
class XmlElement:
    tag: str
    attrs: dict[str, str]
    children: list[XmlElement] = field(default_factory=list)
    text: str = ""

    def find_all(self, tag: str) -> list[XmlElement]:
        return [child for child in self.children if child.tag == tag]


def read_bounded(path: Path, max_bytes: int) -> bytes:
    """En fazla ``max_bytes`` bayt okur; fazlasi varsa sonsuz okumadan reddeder."""

    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValidationFailed("XML dosyasi boyut sinirini asiyor")
    return data


def parse_xml_bounded(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    allowed_doctypes: frozenset[tuple[str, str]] = frozenset(),
) -> XmlElement:
    if len(data) > max_bytes:
        raise ValidationFailed("XML boyut sinirini asiyor")
    parser = expat.ParserCreate()
    parser.buffer_text = True
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.UseForeignDTD(False)

    stack: list[XmlElement] = []
    parts: list[list[str]] = []
    roots: list[XmlElement] = []
    count = 0

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal count
        count += 1
        if count > max_nodes or len(stack) >= max_depth:
            raise ValidationFailed("XML eleman/derinlik siniri asildi")
        element = XmlElement(name, dict(attrs))
        if stack:
            stack[-1].children.append(element)
        else:
            roots.append(element)
        stack.append(element)
        parts.append([])

    def end(_name: str) -> None:
        element = stack.pop()
        element.text = "".join(parts.pop()).strip()

    def chars(data: str) -> None:
        if stack:
            parts[-1].append(data)

    def doctype(_name: str, system_id: str | None, public_id: str | None, internal: int) -> None:
        if internal or (public_id or "", system_id or "") not in allowed_doctypes:
            raise ValidationFailed("XML DOCTYPE/DTD kabul edilmiyor")

    def reject_entity_decl(*_args: object) -> None:
        raise ValidationFailed("XML entity bildirimi kabul edilmiyor")

    def reject_external(*_args: object) -> int:
        raise ValidationFailed("XML harici entity/DTD referansi kabul edilmiyor")

    def default(text: str) -> None:
        if text.startswith("&"):
            raise ValidationFailed("XML entity referansi kabul edilmiyor")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = reject_entity_decl
    parser.ExternalEntityRefHandler = reject_external
    parser.DefaultHandler = default
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise ValidationFailed("XML bicimi bozuk") from exc
    if len(roots) != 1:
        raise ValidationFailed("XML tek kok eleman tasimali")
    return roots[0]


def read_xml_file(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allowed_doctypes: frozenset[tuple[str, str]] = frozenset(),
) -> XmlElement:
    if not os.path.isfile(path):
        raise ValidationFailed("XML dosyasi bulunamadi")
    data = read_bounded(path, max_bytes)
    return parse_xml_bounded(data, max_bytes=max_bytes, allowed_doctypes=allowed_doctypes)
