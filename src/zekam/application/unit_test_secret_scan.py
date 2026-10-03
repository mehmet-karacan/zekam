"""Agent context/sonuc icin secret DEGERI taramasi (W05).

Temel: repodaki ``zekam.application.secret_detection.SECRET_RULES`` (ozel anahtar, AWS/GitHub/
Slack/JWT, baglanti dizesi parolasi, atanmis kimlik bilgisi, Authorization basligi). O kurallar
dogrudan (``scan_text`` placeholder atlamasi OLMADAN) her string yaprak uzerinde calistirilir:
"example" yazarak taramadan kacmak mumkun olmasin.

Eksik siniflar icin ince katman:

- yapisal anahtar/deger: sozluk anahtari gizli-anahtar adiyla BITIYORSA (``password``,
  ``dbPassword``, ``api_key``, ``AWS_SECRET_ACCESS_KEY`` ...) bos/placeholder olmayan string
  deger, uzunluguna bakilmaksizin bulgudur (anahtar ve deger ayri string olsa da);
- ``anahtar = "deger"`` literal atamasi (kisa degerler dahil) ve satir-tabanli ``ANAHTAR=deger``;
- URL userinfo (``scheme://user:pass@host``, ``jdbc:...`` dahil), sorgu parametresi ``password=``;
- ``Authorization: Basic ...``;
- yuksek entropili uzun rastgele dizeler (hex/digest haric).

Yanlis-pozitif tarafi: ``PasswordValidator``, "expired token", yorumlar, test adlari
(``passwordRule``, ``shouldRejectPassword``) ve digest'ler secret DEGILDIR; yalniz literal
deger atamalari yakalanir. Fonksiyon degeri ASLA dondurmez; yalniz kural kimligi doner.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from typing import Any, Final

from zekam.application.secret_detection import SECRET_RULES

_KEY_WORD: Final = (
    r"(?:api[_-]?key|apikey|secret[_-]?access[_-]?key|secret[_-]?key|access[_-]?token"
    r"|auth[_-]?token|client[_-]?secret|private[_-]?key|passwd|password|pwd|token|secret"
    r"|credential)s?"
)
#: Anahtar adi bu sozcuklerden biriyle BITER (passwordRule/password_policy eslesmez).
_KEY_NAME: Final = re.compile(rf"(?i)(?:^|[^A-Za-z0-9]|(?<=[a-z0-9])){_KEY_WORD}$")
_QUOTED_ASSIGN: Final = re.compile(
    rf"""(?i)\b[\w.-]*{_KEY_WORD}["']?\s*[:=]\s*(?P<q>["'])(?P<v>[^"'\n]+)(?P=q)"""
)
_LINE_ASSIGN: Final = re.compile(
    rf"(?im)^\s*[\w.-]*{_KEY_WORD}\s*[:=]\s*(?P<v>[^\s\"'<${{%#(][^\s(]*)\s*$"
)
_QUERY_PARAM: Final = re.compile(rf"(?i)[?&;]{_KEY_WORD}=(?P<v>[^&;\s\"']+)")
_URL_USERINFO: Final = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@")
_BASIC_AUTH: Final = re.compile(r"(?i)\bauthorization\b\s*[:=]\s*[\"']?basic\s+[A-Za-z0-9+/=]{6,}")
_HIGH_ENTROPY: Final = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_=-]{32,}(?![A-Za-z0-9+/_=-])")
_HEX: Final = re.compile(r"^(?:sha\d+:)?[0-9a-fA-F]{32,}$")
_PLACEHOLDER_VALUE: Final = re.compile(r"^(?:\s*|<.*>|\$\{.*\}|\*+|redacted|null|none)$", re.I)


def _entropy(text: str) -> float:
    counts = Counter(text)
    total = len(text)
    return -sum(c / total * math.log2(c / total) for c in counts.values())


def _is_random_token(text: str) -> bool:
    if _HEX.match(text) or text.startswith("sha256:"):
        return False
    classes = (
        any(c.islower() for c in text),
        any(c.isupper() for c in text),
        any(c.isdigit() for c in text),
    )
    return all(classes) and len(set(text)) >= 16 and _entropy(text) >= 4.0


def _iter_leaves(value: Any) -> Iterator[tuple[str | None, str]]:
    """(ust anahtar adi, string yaprak). Anahtar ayri string olarak da taranir."""

    if isinstance(value, str):
        yield None, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            yield None, name
            if isinstance(item, str):
                yield name, item
            else:
                yield from _iter_leaves(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _iter_leaves(item)


def _scan_text(text: str) -> str | None:
    for rule in SECRET_RULES:
        if rule.pattern.search(text):
            return f"base:{rule.rule_id}"
    for name, pattern in (
        ("quoted-assignment", _QUOTED_ASSIGN),
        ("line-assignment", _LINE_ASSIGN),
        ("query-parameter", _QUERY_PARAM),
    ):
        for match in pattern.finditer(text):
            if not _PLACEHOLDER_VALUE.match(match.group("v")):
                return name
    if _URL_USERINFO.search(text):
        return "url-userinfo"
    if _BASIC_AUTH.search(text):
        return "basic-auth"
    for match in _HIGH_ENTROPY.finditer(text):
        if _is_random_token(match.group(0)):
            return "high-entropy"
    return None


def find_secret_value(document: Any) -> str | None:
    """Belgede secret DEGERI varsa kural kimligini doner; deger asla donmez."""

    for key, text in _iter_leaves(document):
        if key is not None and _KEY_NAME.search(key) and not _PLACEHOLDER_VALUE.match(text):
            return "structured-key-value"
        found = _scan_text(text)
        if found is not None:
            return found
    return None
