"""WP14 fail-closed security gates for measured loop contracts."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from zekam.application.loop_progress_compiler import LoopProgressCompiler
from zekam.application.tournament import CandidateSubmission
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.loop_progress import LoopProgressCheckpoint
from zekam.domain.optimization import (
    MeasurementEvidence,
    MetricDirection,
    MetricRole,
    MetricSpec,
    ProgressState,
    ValidatorAsset,
    ValidatorAssetManifest,
    ValidatorAssetRole,
    evaluate_progress,
)
from zekam.infrastructure.postgres.connection import configure_session, reset_role

pytestmark = pytest.mark.security
NOW = dt.datetime(2026, 8, 29, 14, 0, tzinfo=dt.UTC)


@pytest.mark.postgres
@pytest.mark.parametrize(
    "unsafe",
    [
        {"nested": {"raw_transcript": "private conversation"}},
        {"candidate": [{"secret": "never persist"}]},
        {"private_reasoning": {"content": "hidden chain"}},
    ],
)
def test_postgres_payload_guard_rejects_nested_raw_or_secret_material(
    realm_session: tuple[object, object], unsafe: dict[str, object]
) -> None:
    realm, connection = realm_session
    reset_role(connection)
    try:
        with connection.transaction(), connection.cursor() as cursor:  # type: ignore[union-attr]
            cursor.execute("savepoint measured_security")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "select runtime.assert_measured_payload_safe(%s::jsonb)",
                    (Jsonb(unsafe),),
                )
            cursor.execute("rollback to savepoint measured_security")
            cursor.execute(
                "select runtime.assert_measured_payload_safe(%s::jsonb)",
                (
                    Jsonb(
                        {
                            "schema": "zekam-loop-progress-packet/v1",
                            "evidence_digest": digest("external-evidence"),
                            "grants_authority": False,
                        }
                    ),
                ),
            )
            assert cursor.fetchone()[0] in {None, ""}
    finally:
        configure_session(connection, realm_id=realm.id)


def _evidence(value: float, label: str, *, self_report: bool = False) -> MeasurementEvidence:
    return MeasurementEvidence(
        "quality",
        value,
        f"evidence:{label}",
        digest((label, value)),
        "git:wp14",
        NOW,
        "measurement-worker",
        "independent-verifier",
        producer_self_report=self_report,
    )


def _checkpoint(focus: str) -> LoopProgressCheckpoint:
    spec = MetricSpec(
        "quality",
        "Quality",
        "points",
        MetricDirection.MAXIMIZE,
        MetricRole.PRIMARY,
        "external-validator",
        target_value=10,
        minimum_meaningful_delta=0.5,
    )
    baseline = (_evidence(5, "baseline"),)
    previous = evaluate_progress((spec,), baseline, baseline, (_evidence(6, "previous"),))
    current = evaluate_progress(
        (spec,), baseline, (_evidence(6, "previous-2"),), (_evidence(7, "current"),)
    )
    return LoopProgressCheckpoint(
        digest("objective"),
        "git:wp14",
        digest("plan"),
        digest("policy"),
        digest("validator-assets"),
        digest("before"),
        digest("after"),
        uuid4(),
        2,
        previous,
        current,
        digest("accepted-hypothesis"),
        (),
        digest("patch"),
        digest("failure"),
        "evidence:diagnosis",
        digest("diagnosis"),
        (),
        1,
        1_000,
        1_000,
        60,
        focus,
        (),
    )


def test_raw_transcript_cannot_enter_packet_and_model_self_report_is_not_progress() -> None:
    with pytest.raises(ValidationFailed, match="raw/multiline"):
        LoopProgressCompiler().compile(_checkpoint("raw transcript\nprivate line"))

    spec = MetricSpec(
        "quality",
        "Quality",
        "points",
        MetricDirection.MAXIMIZE,
        MetricRole.PRIMARY,
        "external-validator",
        target_value=10,
    )
    result = evaluate_progress(
        (spec,),
        (_evidence(1, "baseline"),),
        (_evidence(1, "previous"),),
        (_evidence(10, "self-report", self_report=True),),
    )
    assert result.progress_state is ProgressState.INVALID
    assert result.invalid_reasons == ("current:producer-self-report",)


def test_builder_cannot_write_validator_assets_or_observe_other_tournament_outputs() -> None:
    manifest = ValidatorAssetManifest(
        uuid4(),
        uuid4(),
        digest("validator-spec"),
        "git:wp14",
        uuid4(),
        uuid4(),
        (
            ValidatorAsset(
                "fixture",
                "logical:fixture",
                digest("fixture"),
                ValidatorAssetRole.FIXTURE,
            ),
            ValidatorAsset(
                "threshold",
                "logical:threshold",
                digest("threshold"),
                ValidatorAssetRole.THRESHOLD,
            ),
        ),
        NOW,
    )
    with pytest.raises(PolicyViolation, match="write scope"):
        manifest.assert_builder_write_scope(("logical:artifact", "logical:threshold"))

    with pytest.raises(PolicyViolation, match="baska candidate"):
        CandidateSubmission(
            uuid4(),
            digest("candidate-result"),
            1,
            1,
            NOW,
            (digest("other-candidate-output"),),
        )
