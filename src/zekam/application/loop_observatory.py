"""Bounded, read-only projection of the measured loop execution plane.

The local operational ledger remains canonical.  This module deliberately projects an
allow-listed set of identifiers, digests, numeric metrics, budget counters and
terminal metadata; JSON bodies are never returned wholesale.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from zekam.domain.errors import NotFound, ValidationFailed

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_MAX_ROWS = 100


def _token(value: object) -> str | None:
    text = str(value)
    return text if _TOKEN_RE.fullmatch(text) else None


def _digest(value: object) -> str | None:
    text = str(value)
    return text if _DIGEST_RE.fullmatch(text) else None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _safe_metric_values(value: object) -> dict[str, int | float]:
    rows = _mapping(value)
    result: dict[str, int | float] = {}
    for key, raw in sorted(rows.items()):
        safe_key = _token(key)
        safe_value = _number(raw)
        if safe_key is not None and safe_value is not None:
            result[safe_key] = safe_value
    return result


def _metric_specs(body: object) -> list[dict[str, object]]:
    raw_specs = _mapping(body).get("metric_specs", ())
    if not isinstance(raw_specs, Sequence) or isinstance(raw_specs, (str, bytes)):
        return []
    result: list[dict[str, object]] = []
    for raw in raw_specs[:_MAX_ROWS]:
        item = _mapping(raw)
        metric_id = _token(item.get("metric_id", ""))
        direction = _token(item.get("direction", ""))
        role = _token(item.get("role", ""))
        if metric_id is None or direction is None or role is None:
            continue
        projected: dict[str, object] = {
            "metric_id": metric_id,
            "direction": direction,
            "role": role,
        }
        for key in (
            "target_value",
            "min_value",
            "max_value",
            "minimum_meaningful_delta",
            "regression_tolerance",
        ):
            number = _number(item.get(key))
            if number is not None:
                projected[key] = number
        result.append(projected)
    return result


def _progress_metadata(body: object) -> dict[str, object]:
    item = _mapping(body)
    current = _mapping(item.get("current_metric_vector"))
    previous = _mapping(item.get("previous_metric_vector"))
    remaining = _mapping(item.get("remaining_budget"))
    result: dict[str, object] = {
        "baseline_values": _safe_metric_values(current.get("baseline_values")),
        "previous_values": _safe_metric_values(previous.get("current_values")),
        "current_values": _safe_metric_values(current.get("current_values")),
        "metric_deltas": _safe_metric_values(item.get("metric_deltas")),
        "remaining_budget": {
            key: number
            for key in ("attempts", "tokens", "cost_micros", "time_seconds")
            if (number := _number(remaining.get(key))) is not None
        },
    }
    for key in (
        "objective_digest",
        "plan_digest",
        "policy_revision_digest",
        "validator_asset_manifest_digest",
        "artifact_before_digest",
        "artifact_after_digest",
        "accepted_hypothesis_digest",
        "patch_digest",
        "failure_signature",
    ):
        safe = _digest(item.get(key, ""))
        if safe is not None:
            result[key] = safe
    return result


def _graph_metadata(body: object) -> dict[str, object]:
    item = _mapping(body)
    critical_path = item.get("critical_path", ())
    safe_path = (
        [safe for value in critical_path[:_MAX_ROWS] if (safe := _token(value)) is not None]
        if isinstance(critical_path, Sequence) and not isinstance(critical_path, (str, bytes))
        else []
    )
    result: dict[str, object] = {"critical_path": safe_path}
    for key in (
        "max_observed_concurrency",
        "parallel_overlap_duration_millis",
        "parallel_efficiency_ppm",
        "coordination_input_tokens",
        "coordination_output_tokens",
        "coordination_cost_micros",
        "coordination_message_count",
    ):
        number = _number(item.get(key))
        if number is not None:
            result[key] = number
    for key in ("plan_digest", "fan_in_result_digest", "receipt_digest"):
        safe = _digest(item.get(key, ""))
        if safe is not None:
            result[key] = safe
    state = _token(item.get("terminal_state", ""))
    if state is not None:
        result["terminal_state"] = state
    return result


def _tournament_metadata(body: object) -> dict[str, object]:
    item = _mapping(body)
    assignments = item.get("candidate_assignments", ())
    candidates: list[dict[str, str]] = []
    if isinstance(assignments, Sequence) and not isinstance(assignments, (str, bytes)):
        for raw in assignments[:_MAX_ROWS]:
            assignment = _mapping(raw)
            assignment_id = _token(assignment.get("assignment_id", ""))
            model_id = _token(assignment.get("model_id", ""))
            if assignment_id is not None and model_id is not None:
                candidates.append({"assignment_id": assignment_id, "model_id": model_id})
    result: dict[str, object] = {"candidates": candidates}
    for key in ("shared_objective_digest", "selector_spec_digest", "plan_digest"):
        safe = _digest(item.get(key, ""))
        if safe is not None:
            result[key] = safe
    for key in ("selector_assignment_id", "selector_model_id"):
        safe = _token(item.get(key, ""))
        if safe is not None:
            result[key] = safe
    return result


def _ablation_metadata(body: object) -> dict[str, object]:
    item = _mapping(body)
    result: dict[str, object] = {}
    for key in ("pair_digest", "policy_digest", "rollback_plan_digest", "decision_digest"):
        safe = _digest(item.get(key, ""))
        if safe is not None:
            result[key] = safe
    for key in ("disposition", "review_status"):
        safe = _token(item.get(key, ""))
        if safe is not None:
            result[key] = safe
    gates = item.get("gates", ())
    safe_gates: list[dict[str, object]] = []
    if isinstance(gates, Sequence) and not isinstance(gates, (str, bytes)):
        for raw in gates[:_MAX_ROWS]:
            gate = _mapping(raw)
            code = _token(gate.get("code", ""))
            if code is None:
                continue
            projected: dict[str, object] = {"code": code, "passed": gate.get("passed") is True}
            for key in ("baseline", "candidate", "limit"):
                number = _number(gate.get(key))
                if number is not None:
                    projected[key] = number
            safe_gates.append(projected)
    result["gates"] = safe_gates
    return result


class LoopObservatory:
    """Read canonical migration-76 records through bounded, allow-listed views."""

    def __init__(self, connection: Any, realm_id: UUID) -> None:
        self.connection = connection
        self.realm_id = realm_id

    @staticmethod
    def _limit(limit: int) -> int:
        if not 1 <= limit <= _MAX_ROWS:
            raise ValidationFailed("Loop observatory limit 1..100 araliginda olmali")
        return limit

    def _one(self, query: str, params: tuple[object, ...], *, label: str) -> tuple[Any, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(query, params)
            row = cursor.fetchone()
        if row is None:
            raise NotFound(f"{label} bulunamadi")
        return tuple(row)

    def _many(self, query: str, params: tuple[object, ...], *, limit: int) -> list[tuple[Any, ...]]:
        exact_limit = self._limit(limit)
        with self.connection.cursor() as cursor:
            cursor.execute(query, (*params, exact_limit))
            return [tuple(row) for row in cursor.fetchall()]

    def plan(self, loop_id: UUID) -> dict[str, object]:
        row = self._one(
            "select p.id,p.project_id,p.work_item_id,p.plan_id,p.step_id,p.source_revision,"
            "p.max_attempts,p.max_tokens,p.max_cost_micros,p.deadline,p.plan_digest,"
            "p.policy_revision_digest,p.policy_digest,v2.stable_objective_digest,"
            "v2.policy_digest,o.id,o.objective_digest,o.objective_body,m.id,m.manifest_digest "
            "from runtime.loop_policy p join runtime.loop_policy_v2 v2 "
            "on v2.realm_id=p.realm_id and v2.loop_id=p.id "
            "join runtime.optimization_objective o on o.realm_id=v2.realm_id "
            "and o.id=v2.objective_id join runtime.validator_asset_manifest m "
            "on m.realm_id=v2.realm_id and m.id=v2.validator_manifest_id "
            "where p.realm_id=%s and p.id=%s",
            (self.realm_id, loop_id),
            label="Loop plan",
        )
        return {
            "schema": "zekam-loop-observatory-plan/v1",
            "loop_id": str(row[0]),
            "project_id": str(row[1]),
            "work_item_id": str(row[2]),
            "plan_id": str(row[3]),
            "step_id": _token(row[4]) or "redacted",
            "source_revision": _token(row[5]) or "redacted",
            "budget": {
                "max_attempts": int(row[6]),
                "max_tokens": int(row[7]),
                "max_cost_micros": int(row[8]),
                "deadline": row[9],
            },
            "plan_digest": str(row[10]),
            "policy_revision_digest": str(row[11]),
            "policy_digest": str(row[12]),
            "stable_objective_digest": str(row[13]),
            "policy_v2_digest": str(row[14]),
            "objective_id": str(row[15]),
            "objective_digest": str(row[16]),
            "metrics": _metric_specs(row[17]),
            "validator_manifest_id": str(row[18]),
            "validator_manifest_digest": str(row[19]),
            "read_only": True,
            "grants_authority": False,
        }

    def attempts(self, loop_id: UUID, *, limit: int = 50) -> dict[str, object]:
        rows = self._many(
            "select a.id,a.predecessor_attempt_id,a.ordinal,a.source_revision,a.plan_digest,"
            "a.policy_revision_digest,a.validator_spec_digest,a.reserved_input_tokens,"
            "a.reserved_output_tokens,a.reserved_cost_micros,a.admitted_at,o.outcome,"
            "o.actual_input_tokens,o.actual_output_tokens,o.actual_cost_micros,o.completed_at "
            "from runtime.loop_attempt a left join runtime.loop_attempt_outcome o "
            "on o.realm_id=a.realm_id and o.attempt_id=a.id "
            "where a.realm_id=%s and a.loop_id=%s order by a.ordinal desc limit %s",
            (self.realm_id, loop_id),
            limit=limit,
        )
        return {
            "schema": "zekam-loop-observatory-attempts/v1",
            "loop_id": str(loop_id),
            "attempts": [
                {
                    "attempt_id": str(row[0]),
                    "predecessor_attempt_id": None if row[1] is None else str(row[1]),
                    "ordinal": int(row[2]),
                    "source_revision": _token(row[3]) or "redacted",
                    "plan_digest": str(row[4]),
                    "policy_revision_digest": str(row[5]),
                    "validator_spec_digest": str(row[6]),
                    "reserved_budget": {
                        "input_tokens": int(row[7]),
                        "output_tokens": int(row[8]),
                        "cost_micros": int(row[9]),
                    },
                    "admitted_at": row[10],
                    "outcome": None if row[11] is None else (_token(row[11]) or "redacted"),
                    "actual_budget": None
                    if row[11] is None
                    else {
                        "input_tokens": int(row[12]),
                        "output_tokens": int(row[13]),
                        "cost_micros": int(row[14]),
                    },
                    "completed_at": row[15],
                }
                for row in rows
            ],
            "read_only": True,
            "grants_authority": False,
        }

    def progress(self, loop_id: UUID, *, limit: int = 50) -> dict[str, object]:
        rows = self._many(
            "select id,attempt_id,ordinal,packet_body,packet_digest,improved,stop_reason,"
            "omission_count,created_at from runtime.loop_progress_packet "
            "where realm_id=%s and loop_id=%s order by ordinal desc limit %s",
            (self.realm_id, loop_id),
            limit=limit,
        )
        return {
            "schema": "zekam-loop-observatory-progress/v1",
            "loop_id": str(loop_id),
            "progress": [
                {
                    "packet_id": str(row[0]),
                    "attempt_id": str(row[1]),
                    "ordinal": int(row[2]),
                    "metadata": _progress_metadata(row[3]),
                    "packet_digest": str(row[4]),
                    "improved": bool(row[5]),
                    "stop_reason": None if row[6] is None else (_token(row[6]) or "redacted"),
                    "omission_count": int(row[7]),
                    "created_at": row[8],
                }
                for row in rows
            ],
            "read_only": True,
            "grants_authority": False,
        }

    def status(self, loop_id: UUID, *, limit: int = 50) -> dict[str, object]:
        terminal = self._one(
            "select p.id,t.state,t.evidence_digest,t.terminal_at,c.id,c.state,"
            "c.reason_digest,c.created_at from runtime.loop_policy p "
            "left join runtime.loop_terminal t on t.realm_id=p.realm_id and t.loop_id=p.id "
            "left join lateral (select event.id,event.state,event.reason_digest,event.created_at "
            "from runtime.loop_control_event event where event.realm_id=p.realm_id "
            "and event.loop_id=p.id order by event.created_at desc,event.id desc limit 1) c "
            "on true "
            "where p.realm_id=%s and p.id=%s",
            (self.realm_id, loop_id),
            label="Loop status",
        )
        return {
            "schema": "zekam-loop-observatory-status/v1",
            "plan": self.plan(loop_id),
            "attempts": self.attempts(loop_id, limit=limit)["attempts"],
            "progress": self.progress(loop_id, limit=limit)["progress"],
            "terminal": None
            if terminal[1] is None
            else {
                "state": _token(terminal[1]) or "redacted",
                "result_digest": str(terminal[2]),
                "created_at": terminal[3],
            },
            "loop_control": {
                "state": "active" if terminal[5] is None else (_token(terminal[5]) or "redacted"),
                "event_id": None if terminal[4] is None else str(terminal[4]),
                "reason_digest": None
                if terminal[6] is None
                else (_digest(terminal[6]) or "redacted"),
                "created_at": terminal[7],
            },
            "read_only": True,
            "grants_authority": False,
        }

    def assess(self, work_item_id: UUID, *, limit: int = 20) -> dict[str, object]:
        rows = self._many(
            "select id,project_id,plan_id,assessment_digest,selected_pattern,decision_digest,"
            "created_at from runtime.execution_topology_decision "
            "where realm_id=%s and work_item_id=%s order by created_at desc,id desc limit %s",
            (self.realm_id, work_item_id),
            limit=limit,
        )
        return {
            "schema": "zekam-loop-observatory-assessments/v1",
            "work_item_id": str(work_item_id),
            "assessments": [
                {
                    "topology_decision_id": str(row[0]),
                    "project_id": str(row[1]),
                    "plan_id": str(row[2]),
                    "assessment_digest": str(row[3]),
                    "selected_pattern": _token(row[4]) or "redacted",
                    "decision_digest": str(row[5]),
                    "created_at": row[6],
                }
                for row in rows
            ],
            "read_only": True,
            "grants_authority": False,
        }

    def graph(self, work_item_id: UUID, *, limit: int = 20) -> dict[str, object]:
        rows = self._many(
            "select r.id,r.topology_decision_id,r.graph_root_id,r.receipt_body,"
            "r.receipt_digest,r.fake_parallelism,r.created_at "
            "from runtime.graph_execution_receipt r "
            "join runtime.execution_topology_decision d on d.realm_id=r.realm_id "
            "and d.id=r.topology_decision_id where r.realm_id=%s and d.work_item_id=%s "
            "order by r.created_at desc,r.id desc limit %s",
            (self.realm_id, work_item_id),
            limit=limit,
        )
        return {
            "schema": "zekam-loop-observatory-graphs/v1",
            "work_item_id": str(work_item_id),
            "graphs": [
                {
                    "receipt_id": str(row[0]),
                    "topology_decision_id": str(row[1]),
                    "graph_root_id": str(row[2]),
                    "metadata": _graph_metadata(row[3]),
                    "receipt_digest": str(row[4]),
                    "fake_parallelism": bool(row[5]),
                    "created_at": row[6],
                }
                for row in rows
            ],
            "read_only": True,
            "grants_authority": False,
        }

    def tournament(self, work_item_id: UUID, *, limit: int = 20) -> dict[str, object]:
        rows = self._many(
            "select p.id,p.topology_decision_id,p.selector_assignment_id,p.selector_model_id,"
            "p.plan_body,p.plan_digest,p.created_at from runtime.tournament_plan p "
            "join runtime.execution_topology_decision d on d.realm_id=p.realm_id "
            "and d.id=p.topology_decision_id where p.realm_id=%s and d.work_item_id=%s "
            "order by p.created_at desc,p.id desc limit %s",
            (self.realm_id, work_item_id),
            limit=limit,
        )
        return {
            "schema": "zekam-loop-observatory-tournaments/v1",
            "work_item_id": str(work_item_id),
            "tournaments": [
                {
                    "tournament_plan_id": str(row[0]),
                    "topology_decision_id": str(row[1]),
                    "selector_assignment_id": str(row[2]),
                    "selector_model_id": _token(row[3]) or "redacted",
                    "metadata": _tournament_metadata(row[4]),
                    "plan_digest": str(row[5]),
                    "created_at": row[6],
                }
                for row in rows
            ],
            "read_only": True,
            "grants_authority": False,
        }

    def ablation(self, work_item_id: UUID, *, limit: int = 20) -> dict[str, object]:
        rows = self._many(
            "select id,project_id,plan_id,ablation_body,ablation_digest,decision,created_at "
            "from runtime.scaffolding_ablation where realm_id=%s and work_item_id=%s "
            "order by created_at desc,id desc limit %s",
            (self.realm_id, work_item_id),
            limit=limit,
        )
        return {
            "schema": "zekam-loop-observatory-ablations/v1",
            "work_item_id": str(work_item_id),
            "ablations": [
                {
                    "ablation_id": str(row[0]),
                    "project_id": str(row[1]),
                    "plan_id": str(row[2]),
                    "metadata": _ablation_metadata(row[3]),
                    "ablation_digest": str(row[4]),
                    "decision": _token(row[5]) or "redacted",
                    "created_at": row[6],
                }
                for row in rows
            ],
            "read_only": True,
            "grants_authority": False,
        }
