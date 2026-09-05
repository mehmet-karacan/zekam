"""Fail-closed contracts for the WP-01 embedded technology bake-off.

This module does not select an engine.  It validates measured evidence so an
ADR cannot turn documentation claims or an unexecuted platform row into a
production dependency decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from zekam.domain.errors import ValidationFailed

type EngineKind = Literal["operational", "knowledge", "analytics"]
type PlatformName = Literal["macos-arm64", "windows-x64"]
type DecisionStatus = Literal["macos-accepted-windows-deferred"]

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REQUIRED_HARD_GATES = frozenset(
    {
        "no_server_or_docker",
        "offline_runtime",
        "persistent_local_state",
        "reproducible_install",
        "macos_arm64",
        "windows_x64",
        "crash_integrity",
        "rebuild_or_restore",
    }
)
_BACKPORTED_WAL_RESET_FIXES = {(3, 44, 6), (3, 50, 7)}
_FIRST_MAINLINE_WAL_RESET_FIX = (3, 51, 3)


@dataclass(frozen=True, slots=True)
class SQLiteWalSafety:
    version: str
    safe_for_multi_connection_wal: bool
    reason: str


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    candidate: str
    engine_kind: EngineKind
    artifact_digest: str
    executed_platforms: frozenset[PlatformName]
    hard_gates: dict[str, bool]
    measured: bool

    @classmethod
    def from_mapping(cls, value: object) -> CandidateEvidence:
        if not isinstance(value, dict):
            raise ValidationFailed("Bake-off candidate evidence nesne olmalidir")
        allowed = {
            "candidate",
            "engine_kind",
            "artifact_digest",
            "executed_platforms",
            "hard_gates",
            "measured",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValidationFailed(
                "Bake-off candidate evidence bilinmeyen alan iceriyor: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        if set(value) != allowed:
            missing = allowed - set(value)
            raise ValidationFailed(
                "Bake-off candidate evidence eksik alan iceriyor: " + ", ".join(sorted(missing))
            )
        candidate = value["candidate"]
        engine_kind = value["engine_kind"]
        artifact_digest = value["artifact_digest"]
        platforms = value["executed_platforms"]
        hard_gates = value["hard_gates"]
        measured = value["measured"]
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValidationFailed("Bake-off candidate bos olamaz")
        if engine_kind not in {"operational", "knowledge", "analytics"}:
            raise ValidationFailed("Bake-off engine_kind gecersiz")
        if not isinstance(artifact_digest, str) or _DIGEST_RE.fullmatch(artifact_digest) is None:
            raise ValidationFailed("Bake-off artifact digest canonical sha256 olmali")
        if not isinstance(platforms, list) or any(
            item not in {"macos-arm64", "windows-x64"} for item in platforms
        ):
            raise ValidationFailed("Bake-off platform listesi gecersiz")
        if len(platforms) != len(set(platforms)):
            raise ValidationFailed("Bake-off platform listesi duplicate iceremez")
        if not isinstance(hard_gates, dict) or any(
            not isinstance(key, str) or type(result) is not bool
            for key, result in hard_gates.items()
        ):
            raise ValidationFailed("Bake-off hard gate sonuclari boolean map olmali")
        if type(measured) is not bool:
            raise ValidationFailed("Bake-off measured alani boolean olmali")
        return cls(
            candidate=candidate.strip(),
            engine_kind=cast(EngineKind, engine_kind),
            artifact_digest=artifact_digest,
            executed_platforms=frozenset(cast(list[PlatformName], platforms)),
            hard_gates=cast(dict[str, bool], hard_gates),
            measured=measured,
        )

    def assert_selectable(self) -> None:
        if not self.measured:
            raise ValidationFailed(f"{self.candidate}: gercek bake-off olcumu yok")
        missing_platforms = {"macos-arm64", "windows-x64"} - self.executed_platforms
        if missing_platforms:
            raise ValidationFailed(
                f"{self.candidate}: platform kaniti eksik: {', '.join(sorted(missing_platforms))}"
            )
        missing_gates = _REQUIRED_HARD_GATES - self.hard_gates.keys()
        if missing_gates:
            raise ValidationFailed(
                f"{self.candidate}: hard gate sonucu eksik: {', '.join(sorted(missing_gates))}"
            )
        failed = sorted(name for name, result in self.hard_gates.items() if not result)
        if failed:
            raise ValidationFailed(f"{self.candidate}: hard gate basarisiz: {', '.join(failed)}")


@dataclass(frozen=True, slots=True)
class MacProvisionalDecision:
    """Explicit Mac-only decision that cannot masquerade as global acceptance."""

    operational: str
    knowledge: str
    analytics: str
    evidence_digests: tuple[str, ...]
    status: DecisionStatus
    windows_x64_deferred: bool

    @classmethod
    def from_mapping(cls, value: object) -> MacProvisionalDecision:
        if not isinstance(value, dict):
            raise ValidationFailed("Mac provisional karar nesne olmalidir")
        expected = {
            "operational",
            "knowledge",
            "analytics",
            "evidence_digests",
            "status",
            "windows_x64_deferred",
        }
        if set(value) != expected:
            raise ValidationFailed("Mac provisional karar alanlari exact olmali")
        engines = tuple(value[key] for key in ("operational", "knowledge", "analytics"))
        if any(not isinstance(item, str) or not item.strip() for item in engines):
            raise ValidationFailed("Mac provisional motor kimligi bos olamaz")
        digests = value["evidence_digests"]
        if (
            not isinstance(digests, list)
            or not digests
            or len(digests) != len(set(digests))
            or any(
                not isinstance(item, str) or _DIGEST_RE.fullmatch(item) is None for item in digests
            )
        ):
            raise ValidationFailed("Mac provisional evidence digest listesi gecersiz")
        if value["status"] != "macos-accepted-windows-deferred":
            raise ValidationFailed("Mac provisional karar global acceptance olamaz")
        if value["windows_x64_deferred"] is not True:
            raise ValidationFailed("Windows x64 yalniz explicit deferred olabilir")
        return cls(
            operational=cast(str, engines[0]).strip(),
            knowledge=cast(str, engines[1]).strip(),
            analytics=cast(str, engines[2]).strip(),
            evidence_digests=tuple(cast(list[str], digests)),
            status="macos-accepted-windows-deferred",
            windows_x64_deferred=True,
        )


def assess_sqlite_wal_safety(version: str) -> SQLiteWalSafety:
    """Assess the 2026 WAL-reset fix without guessing from a version prefix."""
    if not isinstance(version, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ValidationFailed("SQLite surumu major.minor.patch biciminde olmali")
    parsed = tuple(int(part) for part in version.split("."))
    safe = parsed in _BACKPORTED_WAL_RESET_FIXES or parsed >= _FIRST_MAINLINE_WAL_RESET_FIX
    reason = "wal-reset-fix-present" if safe else "wal-reset-fix-not-proven-single-writer-required"
    return SQLiteWalSafety(
        version=version,
        safe_for_multi_connection_wal=safe,
        reason=reason,
    )


def select_single_candidate(
    candidates: list[CandidateEvidence], *, engine_kind: EngineKind
) -> CandidateEvidence:
    """Return the only selectable candidate; ambiguity is a hard failure."""
    matching = [candidate for candidate in candidates if candidate.engine_kind == engine_kind]
    selectable: list[CandidateEvidence] = []
    failures: list[str] = []
    for candidate in matching:
        try:
            candidate.assert_selectable()
        except ValidationFailed as exc:
            failures.append(str(exc))
        else:
            selectable.append(candidate)
    if len(selectable) != 1:
        detail = "; ".join(failures) or "selectable candidate sayisi tek degil"
        raise ValidationFailed(
            f"{engine_kind}: tam olarak bir selectable candidate gerekir; "
            f"bulunan={len(selectable)}; {detail}"
        )
    return selectable[0]


def load_candidate_evidence(path: Path) -> list[CandidateEvidence]:
    """Load a duplicate-key-free canonical JSON evidence file."""

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValidationFailed(f"Bake-off JSON duplicate key: {key}")
            result[key] = value
        return result

    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw, object_pairs_hook=reject_duplicate)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Bake-off evidence okunamadi") from exc
    if not isinstance(value, list) or not value:
        raise ValidationFailed("Bake-off evidence bos olmayan liste olmali")
    return [CandidateEvidence.from_mapping(item) for item in value]


def canonical_json_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()
