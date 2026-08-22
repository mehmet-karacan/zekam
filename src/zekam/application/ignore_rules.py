"""`.gitignore` benzeri yoksayma kurallari.

Desteklenen sozdizimi (Git'in yaygin alt kumesi):

- `#` ile baslayan satirlar yorumdur, bos satirlar yoksayilir.
- `!` oneki onceki bir kurali iptal eder (negation).
- Sonda `/` yalnizca dizinleri eslestirir.
- Basta `/` kurali bulundugu dizine sabitler.
- `*` bir yol parcasi icinde herhangi bir karakter dizisini, `?` tek karakteri,
  `**` birden fazla yol parcasini eslestirir.
- Icinde `/` bulunmayan kural her derinlikte eslesir.

Kapsam disi: `\\` ile kacis, `[...]` karakter siniflari ve `.gitattributes`
etkilesimleri. Bunlar gerektiginde ayri bir revizyonla eklenir.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class IgnoreRule:
    """Tek bir yoksayma kurali."""

    pattern: str
    negated: bool
    directory_only: bool
    anchored: bool
    regex: re.Pattern[str]
    base: str = ""

    def matches(self, relative_path: str, *, is_directory: bool) -> bool:
        """Kok'e gore verilen yolun kuralla eslesip eslesmedigini soyler."""
        if self.directory_only and not is_directory:
            return False
        candidate = relative_path
        if self.base:
            prefix = f"{self.base}/"
            if not candidate.startswith(prefix):
                return False
            candidate = candidate[len(prefix) :]
        return self.regex.match(candidate) is not None


def _translate(pattern: str, *, anchored: bool) -> re.Pattern[str]:
    """Glob desenini bastan eslesen bir duzenli ifadeye cevirir."""
    parts: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                parts.append("(?:.*/)?")
                index += 3
                continue
            if pattern.startswith("**", index):
                parts.append(".*")
                index += 2
                continue
            parts.append("[^/]*")
            index += 1
            continue
        if char == "?":
            parts.append("[^/]")
            index += 1
            continue
        parts.append(re.escape(char))
        index += 1

    body = "".join(parts)
    # Anchored kural kok'e sabitlenir; digeri her derinlikte eslesir.
    expression = f"^{body}(?:/.*)?$" if anchored else f"^(?:.*/)?{body}(?:/.*)?$"
    return re.compile(expression)


def parse_rule(line: str, *, base: str = "") -> IgnoreRule | None:
    """Tek satiri kurala cevirir. Yorum veya bos satir icin `None` doner."""
    stripped = line.rstrip("\n").rstrip("\r")
    if not stripped.strip() or stripped.lstrip().startswith("#"):
        return None

    negated = stripped.startswith("!")
    if negated:
        stripped = stripped[1:]

    directory_only = stripped.endswith("/")
    if directory_only:
        stripped = stripped[:-1]

    anchored = stripped.startswith("/") or "/" in stripped.rstrip("/")
    stripped = stripped.removeprefix("/")
    if not stripped:
        return None

    return IgnoreRule(
        pattern=stripped,
        negated=negated,
        directory_only=directory_only,
        anchored=anchored,
        regex=_translate(stripped, anchored=anchored),
        base=base,
    )


@dataclass(frozen=True, slots=True)
class IgnoreMatcher:
    """Sirali kurallardan olusan yoksayma degerlendirici."""

    rules: tuple[IgnoreRule, ...] = ()

    @classmethod
    def from_lines(cls, lines: Iterable[str], *, base: str = "") -> IgnoreMatcher:
        """Satir dizisinden matcher uretir."""
        parsed = [parse_rule(line, base=base) for line in lines]
        return cls(tuple(rule for rule in parsed if rule is not None))

    def extended(self, other: IgnoreMatcher) -> IgnoreMatcher:
        """Kurallari sirali olarak birlestirir; sonraki kurallar oncelikli olur."""
        return IgnoreMatcher(self.rules + other.rules)

    def is_ignored(self, relative_path: str, *, is_directory: bool = False) -> bool:
        """Yolun yoksayilip yoksayilmayacagini son eslesen kurala gore belirler."""
        normalized = PurePosixPath(relative_path).as_posix()
        decision = False
        for rule in self.rules:
            if rule.matches(normalized, is_directory=is_directory):
                decision = not rule.negated
        return decision

    def is_path_ignored(self, relative_path: str, *, is_directory: bool = False) -> bool:
        """Ust dizinlerden biri yoksayilmissa yolu da yoksayar."""
        parts = PurePosixPath(relative_path).parts
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if self.is_ignored(parent, is_directory=True):
                return True
        return self.is_ignored(relative_path, is_directory=is_directory)


#: Kaynak taramasinda her zaman disarida birakilan dizin ve dosyalar.
SYSTEM_DENY_LINES: tuple[str, ...] = (
    ".git/",
    ".hg/",
    ".svn/",
    "node_modules/",
    "__pycache__/",
    ".venv/",
    "venv/",
    ".tox/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".gradle/",
    ".idea/",
    ".vscode/",
    "*.egg-info/",
    "*.pyc",
    "*.pyo",
    "*.tsbuildinfo",
    "*.class",
    "*.o",
    "*.so",
    "*.dll",
    "*.dylib",
    "*.exe",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".env",
    ".env.*",
    "!.env.example",
    "!.env.sample",
)


def system_deny_matcher() -> IgnoreMatcher:
    """Sistem seviyesinde her zaman uygulanan yoksayma kurallari."""
    return IgnoreMatcher.from_lines(SYSTEM_DENY_LINES)
