"""Strict loader for reviewed synthetic secret-fixture allowances."""

from __future__ import annotations

from pathlib import Path

import yaml

from zekam.application.source_security import SecretScanAllowance, SecretScanAllowlist
from zekam.domain.errors import PolicyViolation, ValidationFailed

ALLOWLIST_SCHEMA = "zekam-secret-scan-allowlist/v1"


def default_secret_scan_allowlist_file() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "secret_scan_allowlist.yaml"


def load_secret_scan_allowlist(path: Path | None = None) -> SecretScanAllowlist:
    candidate = path or default_secret_scan_allowlist_file()
    if candidate.is_symlink():
        raise PolicyViolation("Secret scan allowlist symlink olamaz")
    target = candidate.resolve(strict=True)
    if not target.is_file() or target.stat().st_size > 64 * 1024:
        raise PolicyViolation("Secret scan allowlist guvenli regular file olmali")
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"schema", "allowances"}:
        raise ValidationFailed("Secret scan allowlist exact shape ister")
    raw_allowances = document["allowances"]
    if document["schema"] != ALLOWLIST_SCHEMA or not isinstance(raw_allowances, list):
        raise ValidationFailed("Secret scan allowlist schema gecersiz")
    allowances: list[SecretScanAllowance] = []
    for raw in raw_allowances:
        if not isinstance(raw, dict) or set(raw) != {
            "surface",
            "path",
            "rule_id",
            "fingerprint",
        }:
            raise ValidationFailed("Secret scan allowance exact shape ister")
        allowances.append(
            SecretScanAllowance(
                surface=str(raw["surface"]),
                relative_path=str(raw["path"]),
                rule_id=str(raw["rule_id"]),
                fingerprint=str(raw["fingerprint"]),
            )
        )
    return SecretScanAllowlist(tuple(allowances))
