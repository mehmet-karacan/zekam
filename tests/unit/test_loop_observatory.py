from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from zekam.application.loop_observatory import LoopObservatory
from zekam.domain.errors import ValidationFailed

REALM_ID = UUID("00000000-0000-0000-0000-000000000001")
LOOP_ID = UUID("00000000-0000-0000-0000-000000000002")
WORK_ID = UUID("00000000-0000-0000-0000-000000000003")
DIGEST = "sha256:" + "a" * 64
NOW = dt.datetime(2026, 8, 29, tzinfo=dt.UTC)
SCHEMA_NAMES = (
    "optimization-objective.schema.json",
    "metric-spec.schema.json",
    "measurement-evidence.schema.json",
    "progress-vector.schema.json",
    "loop-progress-packet.schema.json",
    "loop-validation-v2.schema.json",
    "loop-suitability-assessment.schema.json",
    "execution-topology-decision.schema.json",
    "validator-asset-manifest.schema.json",
    "graph-execution-receipt.schema.json",
    "tournament-plan.schema.json",
)


class FakeCursor:
    def __init__(self, rows: dict[str, list[tuple[Any, ...]]]) -> None:
        self.rows = rows
        self.selected: list[tuple[str, tuple[object, ...]]] = []
        self.current: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        assert query.lstrip().lower().startswith("select")
        self.selected.append((query, params))
        key = next(key for key in self.rows if key in query)
        self.current = self.rows[key]

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.current[0] if self.current else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.current


class FakeConnection:
    def __init__(self, rows: dict[str, list[tuple[Any, ...]]]) -> None:
        self.fake_cursor = FakeCursor(rows)

    def cursor(self) -> FakeCursor:
        return self.fake_cursor


def _repository() -> LoopObservatory:
    objective_body = {
        "raw_prompt": "password=never-expose",
        "metric_specs": [
            {
                "metric_id": "quality.score",
                "name": "Kullanici Gizli Adi",
                "direction": "maximize",
                "role": "primary",
                "target_value": 0.9,
                "minimum_meaningful_delta": 0.01,
            }
        ],
    }
    packet_body = {
        "raw_response": "secret transcript",
        "objective_digest": DIGEST,
        "plan_digest": DIGEST,
        "patch_digest": DIGEST,
        "failure_signature": DIGEST,
        "current_metric_vector": {
            "baseline_values": {"quality.score": 0.5},
            "current_values": {"quality.score": 0.75},
            "raw_transcript": "never",
        },
        "previous_metric_vector": {"current_values": {"quality.score": 0.6}},
        "metric_deltas": {"quality.score": 0.15},
        "remaining_budget": {
            "attempts": 2,
            "tokens": 100,
            "cost_micros": 10,
            "time_seconds": 30,
        },
    }
    plan_row = (
        LOOP_ID,
        UUID(int=4),
        WORK_ID,
        UUID(int=5),
        "step-1",
        "revision-1",
        4,
        1000,
        500,
        NOW,
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        UUID(int=6),
        DIGEST,
        objective_body,
        UUID(int=7),
        DIGEST,
    )
    attempt_row = (
        UUID(int=8),
        None,
        1,
        "revision-1",
        DIGEST,
        DIGEST,
        DIGEST,
        100,
        50,
        5,
        NOW,
        "passed",
        80,
        20,
        4,
        NOW,
    )
    progress_row = (UUID(int=9), UUID(int=8), 1, packet_body, DIGEST, True, None, 0, NOW)
    rows: dict[str, list[tuple[Any, ...]]] = {
        "from runtime.loop_policy p join": [plan_row],
        "from runtime.loop_attempt a": [attempt_row],
        "from runtime.loop_progress_packet": [progress_row],
        "left join runtime.loop_terminal": [
            (LOOP_ID, "passed", DIGEST, NOW, UUID(int=17), "paused", DIGEST, NOW)
        ],
    }
    return LoopObservatory(FakeConnection(rows), REALM_ID)


def test_status_is_bounded_read_only_and_never_projects_raw_bodies() -> None:
    document = _repository().status(LOOP_ID, limit=10)

    assert document["read_only"] is True
    assert document["grants_authority"] is False
    assert document["loop_control"] == {
        "state": "paused",
        "event_id": str(UUID(int=17)),
        "reason_digest": DIGEST,
        "created_at": NOW,
    }
    plan = cast(dict[str, object], document["plan"])
    assert plan["metrics"] == [
        {
            "metric_id": "quality.score",
            "direction": "maximize",
            "role": "primary",
            "target_value": 0.9,
            "minimum_meaningful_delta": 0.01,
        }
    ]
    progress_rows = cast(list[dict[str, object]], document["progress"])
    progress = progress_rows[0]
    metadata = cast(dict[str, object], progress["metadata"])
    assert metadata["current_values"] == {"quality.score": 0.75}
    serialized = json.dumps(document, default=str)
    for forbidden in ("raw_prompt", "raw_response", "transcript", "password", "secret"):
        assert forbidden not in serialized.casefold()


def test_reader_rejects_unbounded_limits_before_query() -> None:
    with pytest.raises(ValidationFailed, match=r"1\.\.100"):
        _repository().progress(LOOP_ID, limit=101)


def test_graph_tournament_and_ablation_only_project_allowlisted_metadata() -> None:
    graph_body = {
        "critical_path": ["build", "verify"],
        "parallel_overlap_duration_millis": 420,
        "parallel_efficiency_ppm": 750_000,
        "raw_transcript": "never",
    }
    tournament_body = {
        "candidate_assignments": [
            {
                "assignment_id": str(UUID(int=10)),
                "model_id": "local-model",
                "prompt": "never",
            }
        ],
        "selector_assignment_id": str(UUID(int=11)),
        "selector_model_id": "local-verifier",
        "plan_digest": DIGEST,
    }
    ablation_body = {
        "pair_digest": DIGEST,
        "disposition": "deprecation-candidate",
        "gates": [{"code": "quality", "passed": True, "baseline": 0.8, "candidate": 0.9}],
        "raw_response": "never",
    }
    connection = FakeConnection(
        {
            "from runtime.graph_execution_receipt": [
                (UUID(int=12), UUID(int=13), UUID(int=14), graph_body, DIGEST, False, NOW)
            ],
            "from runtime.tournament_plan": [
                (
                    UUID(int=15),
                    UUID(int=13),
                    UUID(int=11),
                    "local-verifier",
                    tournament_body,
                    DIGEST,
                    NOW,
                )
            ],
            "from runtime.scaffolding_ablation": [
                (
                    UUID(int=16),
                    UUID(int=4),
                    UUID(int=5),
                    ablation_body,
                    DIGEST,
                    "keep-baseline",
                    NOW,
                )
            ],
        }
    )
    service = LoopObservatory(connection, REALM_ID)

    documents = (
        service.graph(WORK_ID),
        service.tournament(WORK_ID),
        service.ablation(WORK_ID),
    )
    serialized = json.dumps(documents, default=str)
    assert "critical_path" in serialized
    assert "candidates" in serialized
    assert "gates" in serialized
    assert "raw_transcript" not in serialized
    assert "raw_response" not in serialized


def test_all_measured_loop_json_schemas_are_strict_draft_2020_12() -> None:
    for name in SCHEMA_NAMES:
        schema = json.loads((Path("schemas") / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
