#!/usr/bin/env python3
"""Deterministic Jira title and intent helpers; performs no network or Jira writes."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata

_DRAFT_PHRASES = ("hazırla", "taslak", "öner", "nasıl yazalım", "örnek")
_WRITE = re.compile(
    r"(?:\boluştur\b|\bjira['’]?ya\s+ekle\b|\byorumu?\s+ekle\b|"
    r"\bgüncelle\b|\bkaydet\b)",
    re.IGNORECASE,
)
_NEGATED_WRITE = re.compile(
    r"(?:\bekleme\b|\beklemek\s+istemiyorum\b|\beklemesini\s+istemiyorum\b|"
    r"\boluşturma\b|\boluşturmak\s+istemiyorum\b|\bgüncelleme\b|\bkaydetme\b)",
    re.IGNORECASE,
)
_TECHNICAL = re.compile(
    r"(?:[A-ZÇĞİÖŞÜ0-9]+(?:[_./:-][A-Za-zÇĞİÖŞÜçğıöşü0-9]+)+|"
    r"[A-Za-zÇĞİÖŞÜçğıöşü]+_[A-Za-zÇĞİÖŞÜçğıöşü0-9_]+|"
    r"[A-ZÇĞİÖŞÜ]{2,}(?:\s+\d+[A-Za-z]?)?)\Z"
)
_LOWER_WORDS = frozenset({"ve", "ile", "veya", "için", "ya", "da", "de"})
_WIKI_OR_MARKDOWN = re.compile(
    r"(?:^h[1-6]\.\s|\{(?:code|panel|noformat|color|quote)(?::|\})|"
    r"\{\{[^}]+\}\}|\?\?[^?]+\?\?|^#{1,6}\s|\*{1,2}[^*]+\*{1,2}|"
    r"(?<!\w)_[^_\r\n]+_(?!\w)|![^!]+!|\[\^[^\]]+\]|\[[^\]]+\|[^\]]+\]|"
    r"\[[^\]]+\]\([^\)]+\))",
    re.IGNORECASE,
)


def classify_intent(text: str) -> str:
    """Return read, draft, or explicit-write without granting any authority."""

    folded = unicodedata.normalize("NFC", text).casefold()
    if _NEGATED_WRITE.search(folded):
        return "draft" if any(phrase in folded for phrase in _DRAFT_PHRASES) else "read"
    if _WRITE.search(folded):
        return "explicit-write"
    if any(phrase in folded for phrase in _DRAFT_PHRASES):
        return "draft"
    return "read"


def turkish_title(text: str) -> str:
    """Title-case ordinary Turkish words while retaining technical tokens."""

    normalized = unicodedata.normalize("NFC", " ".join(text.split()))
    if not normalized:
        raise ValueError("başlık boş olamaz")
    words = normalized.split(" ")
    result: list[str] = []
    for index, word in enumerate(words):
        if _TECHNICAL.fullmatch(word) or any(char.isdigit() for char in word):
            result.append(word)
            continue
        lower = word.casefold().replace("i̇", "i")
        if index and lower in _LOWER_WORDS:
            result.append(lower)
            continue
        first = lower[0]
        upper = "İ" if first == "i" else first.upper()
        result.append(upper + lower[1:])
    return " ".join(result)


def build_summary(title: str, *, source_kind: str | None, source_id: str | None) -> str:
    heading = turkish_title(title)
    if source_kind is None and source_id is None:
        summary = heading
    elif source_kind in {"Talep", "Defect"} and source_id and "\n" not in source_id:
        summary = f"{source_kind} ID: {source_id.strip()} - {heading}"
    else:
        raise ValueError("source_kind/source_id birlikte ve Talep veya Defect olmalı")
    validate_summary(summary)
    return summary


def validate_summary(summary: str) -> None:
    if unicodedata.normalize("NFC", summary) != summary:
        raise ValueError("özet NFC olmalı")
    if not 1 <= len(summary) <= 254:
        raise ValueError("özet 1-254 karakter olmalı; otomatik kırpma yapılmadı")
    if "\n" in summary or "\r" in summary or _WIKI_OR_MARKDOWN.search(summary):
        raise ValueError("özet düz metin olmalı")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    classify = sub.add_parser("classify")
    classify.add_argument("text")
    summary = sub.add_parser("summary")
    summary.add_argument("title")
    summary.add_argument("--source-kind", choices=("Talep", "Defect"))
    summary.add_argument("--source-id")
    args = parser.parse_args()
    if args.command == "classify":
        document = {"intent": classify_intent(args.text), "grants_authority": False}
    else:
        try:
            value = build_summary(
                args.title, source_kind=args.source_kind, source_id=args.source_id
            )
        except ValueError as exc:
            document = {"valid": False, "error": str(exc), "summary": None}
            print(json.dumps(document, ensure_ascii=False, sort_keys=True))
            return 2
        document = {"valid": True, "summary": value, "length": len(value)}
    print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
