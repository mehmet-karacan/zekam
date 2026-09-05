"""Independent v2 service/composition bridge admission and rollback gates."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from tests.integration.test_local_continuity_composition import _start
from tests.integration.test_local_continuity_composition import runtime as runtime
from tests.unit.test_local_continuity_bridge_close import _stage
from tests.unit.test_local_continuity_environment import environment as environment
from tests.unit.test_local_continuity_startup import SOURCE_REF
from tests.unit.test_local_startup_composition import composition as composition

from zekam.application.local_continuity_close import (
    CANDIDATE_RECIPE_DIGEST,
    CloseCandidateBundle,
    CloseCandidateClaim,
    CloseSummary,
)
from zekam.application.mutation_admission import (
    MutationAdmissionExemption,
    assert_local_effect_admission,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest

pytestmark = pytest.mark.integration


def test_only_exact_freeze_v2_leaf_has_local_effect_admission() -> None:
    admission = assert_local_effect_admission(("continuity", "local", "freeze-v2"))
    assert admission.command_path == ("continuity", "local", "freeze-v2")
    assert admission.mutating is True
    assert admission.exemption is MutationAdmissionExemption.LOCAL_EFFECT
    assert admission.requires_full_continuity is False
    assert admission.requires_existing_hydration is False
    assert admission.grants_authority is False
    for neighbor in (
        ("continuity", "local", "freeze-v3"),
        ("continuity", "local", "freeze-v2-extra"),
        ("continuity", "local", "freeze-v2", "extra"),
        ("continuity", "freeze-v2"),
        ("future", "local", "freeze-v2"),
    ):
        with pytest.raises(PolicyViolation, match="exact reviewed admission"):
            assert_local_effect_admission(neighbor)


def _ready(value: dict[str, Any]) -> tuple[str, CloseSummary, CloseCandidateBundle]:
    context = _start(value)
    _stage(value, "Stop")
    assert value["command"].drain()["persisted_spool_count"] == 2
    summary = CloseSummary(
        ("Inspected the exact bounded health source.",),
        (),
        (),
        ("Candidate promotion remains a separate human action.",),
        "Review the generated inbox candidates.",
        ((SOURCE_REF, digest(value["text"])),),
        ((f"context/{context[7:]}", context),),
    )
    claim = CloseCandidateClaim(
        "Remember the verified health endpoint shape.", summary.sources, summary.evidence
    )
    return context, summary, CloseCandidateBundle(memory=(claim,))


def _stored_close(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as db:
        raw = db.execute("select input_json from continuity_close_request").fetchone()[0]
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value


def test_explicit_v2_bridge_reports_recipe_and_freezes_exact_v2(runtime: dict[str, Any]) -> None:
    context, summary, candidates = _ready(runtime)
    result = runtime["command"].freeze_v2(
        summary, candidates, context, "independent-explicit-v2-freeze"
    )
    assert result["operation"] == "freeze-v2"
    assert result["candidate_recipe_digest"] == CANDIDATE_RECIPE_DIGEST
    assert result["native_ack"] is result["grants_authority"] is False
    body = _stored_close(runtime["path"])
    assert body["schema"] == "zekam-local-close/v2"
    assert body["candidate_recipe_digest"] == CANDIDATE_RECIPE_DIGEST
    assert CloseCandidateBundle.from_body(body["candidate_bundle"]) == candidates


def test_default_freeze_bridge_remains_exact_v1(runtime: dict[str, Any]) -> None:
    context, summary, _ = _ready(runtime)
    result = runtime["command"].freeze(summary, context, "independent-default-v1-freeze")
    assert result["operation"] == "freeze"
    assert "candidate_recipe_digest" not in result
    body = _stored_close(runtime["path"])
    assert body["schema"] == "zekam-local-close/v1"
    assert "candidate_bundle" not in body


class _CandidateSubclass(CloseCandidateBundle):
    def __post_init__(self) -> None:
        """A hostile override must never run past the exact public type gate."""


class _SummarySubclass(CloseSummary):
    def __post_init__(self) -> None:
        """A hostile override must never run past the exact public type gate."""


def test_candidate_wrongtype_or_subclass_rejects_before_checkpoint_or_authority(
    runtime: dict[str, Any],
) -> None:
    context, summary, _ = _ready(runtime)
    before = logical_database_digest(runtime["path"])
    invalid_candidates: tuple[object, ...] = (None, True, [], {}, _CandidateSubclass())
    for candidate in invalid_candidates:
        with pytest.raises(ValidationFailed, match="Exact typed local v2"):
            runtime["command"].freeze_v2(
                summary,
                cast(CloseCandidateBundle, candidate),
                context,
                "invalid-candidate-must-not-checkpoint",
            )
        assert logical_database_digest(runtime["path"]) == before


def test_summary_subclass_rejects_before_checkpoint_or_authority(runtime: dict[str, Any]) -> None:
    context, summary, candidates = _ready(runtime)
    hostile = _SummarySubclass(
        summary.performed,
        summary.decisions,
        summary.failures,
        summary.remaining,
        summary.next_safe_step,
        summary.sources,
        summary.evidence,
    )
    before = logical_database_digest(runtime["path"])
    with pytest.raises(ValidationFailed, match="Exact typed local v2"):
        runtime["command"].freeze_v2(
            hostile, candidates, context, "summary-subclass-must-not-checkpoint"
        )
    assert logical_database_digest(runtime["path"]) == before


def test_scalar_prevalidation_rejects_before_checkpoint_or_authority(
    runtime: dict[str, Any],
) -> None:
    valid_context, summary, candidates = _ready(runtime)
    before = logical_database_digest(runtime["path"])
    for context, key in (("not-a-digest", "valid-key"), (valid_context, "")):
        with pytest.raises(ValidationFailed):
            runtime["command"].freeze_v2(summary, candidates, context, key)
        assert logical_database_digest(runtime["path"]) == before


def test_foreign_candidate_ref_rejects_before_checkpoint_or_authority(
    runtime: dict[str, Any],
) -> None:
    context, summary, _ = _ready(runtime)
    foreign = CloseCandidateClaim(
        "A literal but foreign candidate.",
        (("foreign/source", digest("foreign-source")),),
        summary.evidence,
    )
    before = logical_database_digest(runtime["path"])
    with pytest.raises(PolicyViolation, match="summary provenance"):
        runtime["command"].freeze_v2(
            summary,
            CloseCandidateBundle(memory=(foreign,)),
            context,
            "foreign-ref-must-not-checkpoint",
        )
    assert logical_database_digest(runtime["path"]) == before


@pytest.mark.parametrize("first", ["v1", "v2"])
def test_v1_v2_replay_drift_never_creates_second_authority(
    runtime: dict[str, Any], first: str
) -> None:
    context, summary, candidates = _ready(runtime)
    if first == "v1":
        runtime["command"].freeze(summary, context, "first-v1-freeze")
    else:
        runtime["command"].freeze_v2(summary, candidates, context, "first-v2-freeze")
    before = logical_database_digest(runtime["path"])
    with pytest.raises(PolicyViolation, match="drift"):
        if first == "v1":
            runtime["command"].freeze_v2(summary, candidates, context, "second-v2-freeze")
        else:
            runtime["command"].freeze(summary, context, "second-v1-freeze")
    assert logical_database_digest(runtime["path"]) == before
    with sqlite3.connect(runtime["path"]) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone()[0] == 1
        assert db.execute("select count(*) from local_job").fetchone()[0] == 1
        assert (
            db.execute(
                "select count(*) from local_outbox where event_kind='continuity.compile'"
            ).fetchone()[0]
            == 1
        )
