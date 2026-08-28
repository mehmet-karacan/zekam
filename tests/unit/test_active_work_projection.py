from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
import yaml

from zekam.application.active_work_projection import ActiveWorkProjection
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation


def _projection() -> ActiveWorkProjection:
    return ActiveWorkProjection(
        project_id=uuid4(),
        project_slug="zekam",
        work_id=uuid4(),
        title="Memory Continuity",
        summary="Bounded projection",
        state="active",
        work_revision=3,
        work_record_digest=digest("work"),
        acceptance_criteria=({"text": "exact", "verified": False},),
        plan_id=uuid4(),
        plan_revision=5,
        plan_digest=digest("plan"),
        plan_effect_digest=digest("effect"),
        source_revision="git:abc",
        plan_steps=(
            {
                "step_id": "projection",
                "title": "Project",
                "effect": "file-write",
                "depends_on": [],
                "logical_resources": ["path:zekam:AKTIF_GOREV.md"],
                "risk": "high",
            },
        ),
        run_id=uuid4(),
        run_state="active",
        run_digest=digest("run"),
        source_observation_id=uuid4(),
        source_head="abc",
        source_tree_digest=digest("tree"),
        source_branch="feature/memory",
        source_dirty=True,
        source_file_count=10,
        migration_head=55,
        memory_mode="shadow",
        hook_set_digest=digest("hooks"),
        projection_receipt_digest=digest("receipt"),
        projection_source_digest=digest("source"),
        queue_blocked=0,
        queue_pending=0,
        queue_recovery=0,
        claim_without_receipt=0,
        global_dod_digest=digest("global-dod"),
        release_report_digest=digest("release-report"),
    )


def test_active_work_projection_is_deterministic_and_authority_free() -> None:
    projection = _projection()

    assert projection.render_markdown() == projection.render_markdown()
    assert projection.render_yaml() == projection.render_yaml()
    document = yaml.safe_load(projection.render_yaml())
    assert document["projection_digest"] == projection.projection_digest
    assert document["grants_authority"] is False
    assert document["approval_inherited"] is False
    assert document["legacy_global_dod"]["status"] == "preserved-not-reapplied"


def test_completed_active_work_projection_has_no_next_safe_action() -> None:
    projection = _projection()
    completed = replace(projection, state="completed")
    assert completed.document()["next_safe_action"] is None
    assert "Work terminal completed" in completed.render_markdown()

    with pytest.raises(PolicyViolation, match="actionable next-safe-action"):
        replace(projection, state="completed", next_safe_action="continue")
