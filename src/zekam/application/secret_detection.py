"""Kaynak taramasinda secret tespiti.

Kural: bulunan secret **degeri** hicbir yere yazilmaz. Bulgu yalnizca kural
kimligi, dosya yolu, satir numarasi ve maskelenmis bir ozet tasir. Bu sayede
bulgular log, rapor, artifact ve vector store'a guvenle girebilir.

Tespit tam degildir ve olmasi da beklenmez; amaci indeksleme oncesinde riskli
icerigi disarida birakmaktir. False positive, sessiz sizintiya tercih edilir.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: Maskelenmis ozet uzunlugu. Tam digest de degeri dogrulamaya yarayabilecegi icin kisaltilir.
FINGERPRINT_LENGTH = 12


class SecretSeverity(StrEnum):
    """Bulgunun ciddiyeti."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class SecretRule:
    """Tek bir tespit kurali."""

    rule_id: str
    title: str
    severity: SecretSeverity
    pattern: re.Pattern[str]


SECRET_RULES: tuple[SecretRule, ...] = (
    SecretRule(
        rule_id="private-key-block",
        title="Ozel anahtar blogu",
        severity=SecretSeverity.HIGH,
        pattern=re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    ),
    SecretRule(
        rule_id="aws-access-key-id",
        title="AWS erisim anahtari kimligi",
        severity=SecretSeverity.HIGH,
        pattern=re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    SecretRule(
        rule_id="github-token",
        title="GitHub token",
        severity=SecretSeverity.HIGH,
        pattern=re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    ),
    SecretRule(
        rule_id="slack-token",
        title="Slack token",
        severity=SecretSeverity.HIGH,
        pattern=re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    SecretRule(
        rule_id="json-web-token",
        title="JSON Web Token",
        severity=SecretSeverity.MEDIUM,
        pattern=re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    SecretRule(
        rule_id="connection-string-password",
        title="Baglanti dizesinde parola",
        severity=SecretSeverity.HIGH,
        pattern=re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s:/@]{4,}@"),
    ),
    SecretRule(
        rule_id="assigned-credential",
        title="Atanmis kimlik bilgisi",
        severity=SecretSeverity.MEDIUM,
        pattern=re.compile(
            r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token"
            r"|client[_-]?secret|password|passwd|token|secret|credential)\b\s*[:=]\s*"
            r"""(?P<quote>['"])(?P<value>[^'"\s]{8,})(?P=quote)"""
        ),
    ),
    SecretRule(
        rule_id="authorization-header",
        title="Authorization basligi",
        severity=SecretSeverity.MEDIUM,
        pattern=re.compile(r"(?i)\bauthorization\b\s*[:=]\s*['\"]?(?:bearer|basic)\s+\S{8,}"),
    ),
)

#: Ornek/sahte deger iceren satirlar bulgudan dusulur.
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "example",
    "ornek",
    "sample",
    "placeholder",
    "degistir",
    "changeme",
    "your-",
    "xxxx",
    "dummy",
    "redacted",
    "<",
)


@dataclass(frozen=True, slots=True)
class SecretFinding:
    """Tek bir secret bulgusu. Deger tasimaz."""

    rule_id: str
    title: str
    severity: SecretSeverity
    relative_path: str
    line_number: int
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "severity": self.severity.value,
            "path": self.relative_path,
            "line": self.line_number,
            "fingerprint": self.fingerprint,
        }


def _fingerprint(value: str) -> str:
    """Degeri geri getirmeyen kisa parmak izi uretir."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:FINGERPRINT_LENGTH]


def _looks_like_placeholder(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def scan_text(
    text: str,
    *,
    relative_path: str,
    rules: tuple[SecretRule, ...] = SECRET_RULES,
    max_lines: int = 20_000,
) -> tuple[SecretFinding, ...]:
    """Metin icerigini tarar ve deger tasimayan bulgular dondurur."""
    findings: list[SecretFinding] = []
    for line_number, line in enumerate(text.splitlines()[:max_lines], start=1):
        if _looks_like_placeholder(line):
            continue
        for rule in rules:
            match = rule.pattern.search(line)
            if match is None:
                continue
            captured = match.groupdict().get("value") or match.group(0)
            findings.append(
                SecretFinding(
                    rule_id=rule.rule_id,
                    title=rule.title,
                    severity=rule.severity,
                    relative_path=relative_path,
                    line_number=line_number,
                    fingerprint=_fingerprint(captured),
                )
            )
    return tuple(findings)


def highest_severity(findings: tuple[SecretFinding, ...]) -> SecretSeverity | None:
    """Bulgular arasindaki en yuksek ciddiyeti dondurur."""
    if not findings:
        return None
    order = {SecretSeverity.LOW: 0, SecretSeverity.MEDIUM: 1, SecretSeverity.HIGH: 2}
    return max((finding.severity for finding in findings), key=lambda value: order[value])
