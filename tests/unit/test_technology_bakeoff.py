from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application.technology_bakeoff import (
    CandidateEvidence,
    MacProvisionalDecision,
    assess_sqlite_wal_safety,
    canonical_json_digest,
    load_candidate_evidence,
    select_single_candidate,
)
from zekam.domain.errors import ValidationFailed

_DIGEST = "sha256:" + "a" * 64
_PASSING_GATES = {
    "no_server_or_docker": True,
    "offline_runtime": True,
    "persistent_local_state": True,
    "reproducible_install": True,
    "macos_arm64": True,
    "windows_x64": True,
    "crash_integrity": True,
    "rebuild_or_restore": True,
}


@pytest.mark.parametrize("version", ["3.44.6", "3.50.7", "3.51.3", "3.53.4"])
def test_wal_safety_accepts_fixed_versions(version: str) -> None:
    assert assess_sqlite_wal_safety(version).safe_for_multi_connection_wal is True


@pytest.mark.parametrize("version", ["3.44.5", "3.50.6", "3.51.2"])
def test_wal_safety_rejects_vulnerable_versions(version: str) -> None:
    result = assess_sqlite_wal_safety(version)
    assert result.safe_for_multi_connection_wal is False
    assert "single-writer" in result.reason


@pytest.mark.parametrize("version", ["", "3.53", "3.53.4.1", "v3.53.4", "3.x.4"])
def test_wal_safety_rejects_malformed_versions(version: str) -> None:
    with pytest.raises(ValidationFailed):
        assess_sqlite_wal_safety(version)


def _candidate(**overrides: object) -> CandidateEvidence:
    value: dict[str, object] = {
        "candidate": "sqlite",
        "engine_kind": "operational",
        "artifact_digest": _DIGEST,
        "executed_platforms": ["macos-arm64", "windows-x64"],
        "hard_gates": dict(_PASSING_GATES),
        "measured": True,
    }
    value.update(overrides)
    return CandidateEvidence.from_mapping(value)


def test_select_requires_exactly_one_fully_measured_candidate() -> None:
    assert select_single_candidate([_candidate()], engine_kind="operational").candidate == "sqlite"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"measured": False}, "olcumu yok"),
        ({"executed_platforms": ["macos-arm64"]}, "windows-x64"),
        (
            {"hard_gates": {**_PASSING_GATES, "crash_integrity": False}},
            "crash_integrity",
        ),
        (
            {
                "hard_gates": {
                    key: value for key, value in _PASSING_GATES.items() if key != "offline_runtime"
                }
            },
            "offline_runtime",
        ),
    ],
)
def test_select_rejects_unproven_candidate(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationFailed, match=message):
        select_single_candidate([_candidate(**overrides)], engine_kind="operational")


def test_select_rejects_ambiguous_passing_candidates() -> None:
    with pytest.raises(ValidationFailed, match="bulunan=2"):
        select_single_candidate(
            [_candidate(), _candidate(candidate="pyturso")], engine_kind="operational"
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"artifact_digest": "sha256:not-a-digest"},
        {"measured": 1},
        {"executed_platforms": ["macos-arm64", "macos-arm64"]},
        {"hard_gates": {"offline_runtime": 1}},
        {"unexpected": True},
    ],
)
def test_candidate_mapping_rejects_wrong_types_duplicates_and_unknowns(
    overrides: dict[str, object],
) -> None:
    value: dict[str, object] = {
        "candidate": "sqlite",
        "engine_kind": "operational",
        "artifact_digest": _DIGEST,
        "executed_platforms": ["macos-arm64", "windows-x64"],
        "hard_gates": dict(_PASSING_GATES),
        "measured": True,
    }
    value.update(overrides)
    with pytest.raises(ValidationFailed):
        CandidateEvidence.from_mapping(value)


def test_evidence_loader_rejects_duplicate_json_key(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text('[{"candidate":"sqlite","candidate":"turso"}]', encoding="utf-8")
    with pytest.raises(ValidationFailed, match="duplicate key"):
        load_candidate_evidence(path)


def test_evidence_loader_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    value = [
        {
            "candidate": "sqlite",
            "engine_kind": "operational",
            "artifact_digest": _DIGEST,
            "executed_platforms": ["macos-arm64", "windows-x64"],
            "hard_gates": _PASSING_GATES,
            "measured": True,
        }
    ]
    path.write_text(json.dumps(value), encoding="utf-8")
    assert load_candidate_evidence(path)[0].candidate == "sqlite"


def test_canonical_digest_rejects_non_finite_json() -> None:
    with pytest.raises(ValueError):
        canonical_json_digest({"latency": float("nan")})


def _mac_decision(**overrides: object) -> MacProvisionalDecision:
    value: dict[str, object] = {
        "operational": "cpython-sqlite",
        "knowledge": "sqlite-fts5+sqlite-vec",
        "analytics": "duckdb",
        "evidence_digests": [_DIGEST],
        "status": "macos-accepted-windows-deferred",
        "windows_x64_deferred": True,
    }
    value.update(overrides)
    return MacProvisionalDecision.from_mapping(value)


def test_mac_provisional_decision_is_explicitly_not_global_acceptance() -> None:
    decision = _mac_decision()
    assert decision.windows_x64_deferred is True
    assert decision.status == "macos-accepted-windows-deferred"


@pytest.mark.parametrize(
    "overrides",
    [
        {"windows_x64_deferred": False},
        {"status": "accepted"},
        {"evidence_digests": []},
        {"evidence_digests": [_DIGEST, _DIGEST]},
        {"operational": ""},
        {"unexpected": True},
    ],
)
def test_mac_provisional_decision_rejects_ambiguous_or_global_claims(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationFailed):
        _mac_decision(**overrides)
