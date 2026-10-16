"""Read actual canonical test.run terminal/attempt/measurement/verifier chains."""

from __future__ import annotations

from pathlib import Path

from zekam.application.canonical_job_runtime import _completed_result
from zekam.application.config import load_settings
from zekam.application.unit_test_agents import Verdict, parse_verify_body, parse_build_body
from zekam.application.unit_test_execution_evidence import (
    read_agent_execution,
    compare_execution_provenance,
)
from zekam.application.unit_test_loop_state import LoopState
from zekam.application.unit_test_outcome import terminal_document
from zekam.application.unit_test_patch import CandidateManifest
from zekam.application.unit_test_plan import unresolved_material
from zekam.application.unit_test_source_evidence import _object
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.unit_test_engineering import (
    UnitTestTerminal,
    UnitTestStopReason,
    evaluate_scope,
    EvaluationVerdict,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import _connect
from zekam.infrastructure.sqlite.unit_test_ledger import SQLiteUnitTestLedger
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.unit_test_runner.report_facts import validate_report_facts

MAX_BODY = 4 * 1024 * 1024
MAX_OBSERVATIONS = 4096


def _bounded_row(connection, table, field, value):
    if (table, field) not in {
        ("unit_test_request", "request_digest"),
        ("unit_test_attempt", "attempt_id"),
    }:
        raise PolicyViolation("Verifier source internal query invalid")
    row = connection.execute(
        f"select length(cast(body_json as blob)) n from {table} where {field}=?", (value,)
    ).fetchone()
    if row is None or not 0 < row["n"] <= MAX_BODY:
        raise PolicyViolation("Verifier source missing/oversized ledger body")
    return connection.execute(f"select * from {table} where {field}=?", (value,)).fetchone()


def read_verifier_source(home: Path, reference: str) -> dict:
    """Historical source report. Never runs a model, Maven, migration or signing."""
    database = home / "state/operational.db"
    settings = load_settings(home=home)
    if settings.database.sqlite_path(home).resolve() != database.resolve():
        raise PolicyViolation("Verifier source exact canonical operational database required")
    runtime = SQLiteLocalRuntimeStore(database, existing_only=True)
    snapshot = runtime.job_snapshot(reference)
    if (
        snapshot is None
        or snapshot["state"] != "completed"
        or snapshot["payload"].get("operation") != "test.run"
    ):
        raise PolicyViolation("Verifier source completed canonical test.run required")
    binding = runtime.job_binding(snapshot["job_id"])
    if binding is None:
        raise PolicyViolation("Verifier source canonical Work binding missing")
    objects = LocalContentAddressedStore(home / settings.object_store_relative)
    _object(objects, snapshot["terminal_evidence_digest"])
    result = _completed_result(
        runtime, objects, snapshot, binding, "test.run", snapshot["payload"]["input_digest"]
    )
    execution_input = result.get("execution_input")
    input_fields = {
        "schema",
        "allow_resume",
        "request_digest",
        "maven_plan_digest",
        "realm_id",
        "assignment_id",
        "model_id",
        "verifier_model_id",
        "provider_profile_digest",
        "remote_model",
        "plan_id",
        "step_id",
        "approvals",
        "limits",
    }
    if (
        not isinstance(execution_input, dict)
        or set(execution_input) != input_fields
        or digest(execution_input) != snapshot["payload"]["input_digest"]
        or execution_input.get("schema") != "zekam-canonical-test-run-input/v1"
    ):
        raise PolicyViolation("Verifier source recorded execution input missing or drifted")
    rd = result.get("request_digest")
    if (
        result.get("schema") != "zekam-unit-test-run/v1"
        or execution_input.get("request_digest") != rd
        or type(result.get("exit_code")) is not int
        or result["exit_code"] != 0
    ):
        raise PolicyViolation("Verifier source exact successful run result required")
    connection = _connect(database, read_only=True)
    try:
        connection.execute("begin")
        request_row = _bounded_row(connection, "unit_test_request", "request_digest", rd)
        ledger = SQLiteUnitTestLedger(connection)
        request = ledger.get_request(rd)
        if (
            request.request_digest != rd
            or request.project_id != binding.project_id
            or request.work_item_id != binding.work_item_id
            or request.run_id != binding.run_id
            or request.source_snapshot_id != binding.source_snapshot_id
            or request_row["project_id"] != request.project_id
            or request_row["source_binding_id"] != request.source_binding_id
            or request_row["metric"] != request.metric.value
            or request_row["policy"] != request.policy.value
            or request_row["max_attempts"] != request.budget.max_attempts
        ):
            raise PolicyViolation("Verifier source request/Work/Run drift")
        source = connection.execute(
            "select s.source_binding_id,s.revision_ref,s.content_digest from source_snapshot s join source_binding b on b.id=s.source_binding_id where s.id=? and b.project_id=?",
            (binding.source_snapshot_id, binding.project_id),
        ).fetchone()
        if source is None or tuple(source)[:2] != (
            request.source_binding_id,
            request.source_revision,
        ):
            raise PolicyViolation("Verifier source historical source snapshot drift")
        row = connection.execute(
            "select stop_reason,receipt_digest,evidence_digest from unit_test_terminal where request_digest=? and final=1",
            (rd,),
        ).fetchone()
        if row is None:
            raise PolicyViolation("Verifier source ledger final terminal missing")
        terminal = UnitTestTerminal(
            rd,
            UnitTestStopReason(row["stop_reason"]),
            row["receipt_digest"],
            row["evidence_digest"],
        )
        if terminal.stop_reason is not UnitTestStopReason.TARGET_REACHED or canonical_bytes(
            result.get("terminal")
        ) != canonical_bytes(terminal_document(terminal)):
            raise PolicyViolation("Verifier source target-reached final verifier terminal required")
        matches = connection.execute(
            "select a.attempt_id from unit_test_attempt a join unit_test_attempt_receipt r on r.attempt_id=a.attempt_id where a.request_digest=? and r.evidence_digest=? limit 2",
            (rd, terminal.receipt_digest),
        ).fetchall()
        if len(matches) != 1:
            raise PolicyViolation("Verifier source exact attempt receipt required")
        attempt_row = _bounded_row(
            connection, "unit_test_attempt", "attempt_id", matches[0]["attempt_id"]
        )
        attempt = ledger.get_attempt(attempt_row["attempt_id"])
        receipt = ledger.get_attempt_receipt(attempt.attempt_id)
        for field in (
            "attempt_id",
            "request_digest",
            "ordinal",
            "parent_attempt_id",
            "plan_digest",
            "candidate_digest",
            "effect_digest",
        ):
            if attempt_row[field] != getattr(attempt, field):
                raise PolicyViolation("Verifier source attempt column/body drift")
        if (
            receipt is None
            or receipt.status.value != "completed"
            or receipt.evidence_digest != terminal.receipt_digest
        ):
            raise PolicyViolation("Verifier source completed attempt required")
        count = connection.execute(
            "select count(*) from (select 1 from unit_test_observation where attempt_id=? limit ?)",
            (attempt.attempt_id, MAX_OBSERVATIONS + 1),
        ).fetchone()[0]
        if count > MAX_OBSERVATIONS:
            raise PolicyViolation("Verifier source observation budget exceeded")
        observations = ledger.list_observations(attempt.attempt_id)
        evaluation = evaluate_scope(request, observations)
        if evaluation.verdict is not EvaluationVerdict.MET:
            raise PolicyViolation("Verifier source actual ledger coverage target not met")
    finally:
        connection.close()
    ended = _object(objects, terminal.evidence_digest)
    attempt_evidence = _object(objects, receipt.evidence_digest)
    if (
        ended.get("contract") != "zekam-unit-test-terminal-evidence/v1"
        or ended.get("request_digest") != rd
        or ended.get("stop_reason") != terminal.stop_reason.value
        or attempt_evidence.get("contract") != "zekam-unit-test-attempt-evidence/v1"
        or attempt_evidence.get("request_digest") != rd
        or attempt_evidence.get("attempt_id") != attempt.attempt_id
        or attempt_evidence.get("ordinal") != attempt.ordinal
        or attempt_evidence.get("status") != "completed"
    ):
        raise PolicyViolation("Verifier source terminal/attempt evidence identity drift")
    extra, state, inner = ended["extra"], ended["state"], attempt_evidence["evidence"]
    if (
        inner.get("kind") != "build"
        or inner.get("verdict") != "accepted"
        or extra.get("builder_provenance_complete") is not True
        or state.get("builder_provenance_complete") is not True
        or canonical_bytes(extra.get("builder_execution_digests"))
        != canonical_bytes(state.get("accepted_builder_execution_digests"))
        or extra.get("measurement_digest") != state.get("accepted_measurement_digest")
        or extra.get("measurement_digest") != inner.get("measurement_digest")
        or extra.get("measurement_evidence_digest")
        != state.get("accepted_measurement_evidence_digest")
        or extra.get("measurement_evidence_digest") != inner.get("measurement_evidence_digest")
        or state.get("accepted_candidate_digest") != attempt.candidate_digest
        or inner.get("candidate_digest") != attempt.candidate_digest
    ):
        raise PolicyViolation(
            "Verifier source accepted builder/measurement provenance missing or drifted"
        )
    hydrated = LoopState.from_payload(state, request)
    if (
        hydrated.plan is None
        or hydrated.defects
        or hydrated.stale_flag
        or hydrated.scope_flag
        or hydrated.context_flag
        or unresolved_material(hydrated.plan, hydrated.board)
    ):
        raise PolicyViolation("Verifier source unresolved material scope or defects")
    measurement = _object(objects, extra["measurement_evidence_digest"])
    record = _object(objects, extra["measurement_digest"])
    measured = _object(objects, record["binding_digest"])
    if (
        measurement.get("contract") != "zekam-unit-test-loop-measurement-evidence/v1"
        or measurement.get("request_digest") != rd
        or measurement.get("attempt_id") != attempt.attempt_id
        or measurement.get("measurement_record_digest") != extra["measurement_digest"]
        or measurement.get("measurement_binding_digest") != record["binding_digest"]
        or measurement.get("grants_authority") is not False
        or measured.get("request_digest") != rd
        or measured.get("attempt_id") != attempt.attempt_id
        or measured.get("test_candidate_digest") != attempt.candidate_digest
        or measured.get("source_revision") != request.source_revision
        or measured.get("scope") != "unit"
        or measured.get("plan_digest") != execution_input.get("maven_plan_digest")
        or record.get("status") != "accepted"
        or record.get("run_status") != "completed"
        or record.get("contract") != "zekam-unit-test-measurement/v1"
        or measured.get("contract") != "zekam-unit-test-measurement/v1"
        or record.get("verdict") != evaluation.verdict.value
        or record.get("aggregate") != [evaluation.aggregate_covered, evaluation.aggregate_total]
        or record.get("blockers") != []
        or record.get("stop_reason") is not None
        or type(record.get("exit_code")) is not int
        or record["exit_code"] != 0
        or record.get("test_kind") != "passed"
        or sorted(record.get("observations", []), key=digest)
        != sorted([o.to_payload() for o in observations], key=digest)
    ):
        raise PolicyViolation("Verifier source persisted measurement binding drift")
    if source["content_digest"] != digest(
        {
            "schema": "zekam-native-source-content/v1",
            "source_revision": request.source_revision,
            "production_digests": dict(measured["production_digests"]),
            "build_config_digest": measured["build_config_digest"],
        }
    ):
        raise PolicyViolation("Verifier source measured production/config snapshot drift")
    provenance = compare_execution_provenance(
        objects,
        builder_digests=tuple(extra["builder_execution_digests"]),
        verifier_digest=extra["final_verifier_execution_digest"],
        candidate_digest=attempt.candidate_digest,
        measurement_digest=extra["measurement_digest"],
    )
    verifier = read_agent_execution(objects, extra["final_verifier_execution_digest"])
    if (
        verifier["request_digest"] != rd
        or verifier["project_id"] != binding.project_id
        or verifier["work_item_id"] != binding.work_item_id
        or verifier["realm_id"] != execution_input["realm_id"]
        or verifier["parent_assignment_id"] != execution_input["assignment_id"]
        or verifier["plan_id"] != execution_input["plan_id"]
        or verifier["step_id"] != execution_input["step_id"]
        or verifier["configured_model"] != execution_input["verifier_model_id"]
        or verifier["provider_profile_digest"] != execution_input["provider_profile_digest"]
        or verifier["envelope_digest"] != extra["final_verifier_envelope"]
        or canonical_bytes(verifier["context"].get("accepted_files"))
        != canonical_bytes(state["accepted_files"])
    ):
        raise PolicyViolation("Verifier source actual final execution/context drift")
    verdict = parse_verify_body(verifier["result_body"], request)
    if verdict.effective_verdict is not Verdict.ACCEPTED or verdict.new_material_risks:
        raise PolicyViolation("Verifier source final quality verdict rejected")
    candidate = CandidateManifest.from_payload(_object(objects, attempt.candidate_digest))
    if candidate.candidate_digest != attempt.candidate_digest or candidate.kind != "patch":
        raise PolicyViolation("Verifier source actual candidate identity drift")
    for key in extra["builder_execution_digests"]:
        builder = read_agent_execution(objects, key)
        if (
            builder["configured_model"] != execution_input["model_id"]
            or builder["provider_profile_digest"] != execution_input["provider_profile_digest"]
            or builder["parent_assignment_id"] != execution_input["assignment_id"]
            or builder["plan_id"] != execution_input["plan_id"]
            or builder["step_id"] != execution_input["step_id"]
        ):
            raise PolicyViolation("Verifier source configured builder lineage drift")
        if key == inner.get("builder_execution_digest"):
            patch = parse_build_body(builder["result_body"], request).patch
            if CandidateManifest.for_patch(
                candidate.parent_state_digest, patch
            ).candidate_digest != attempt.candidate_digest or builder[
                "envelope_digest"
            ] != inner.get("builder_envelope"):
                raise PolicyViolation("Verifier source actual builder patch drift")
    if inner.get("builder_execution_digest") not in extra["builder_execution_digests"]:
        raise PolicyViolation("Verifier source accepted attempt builder missing")
    attempt_verifier = read_agent_execution(objects, inner["verifier_execution_digest"])
    if (
        attempt_verifier["specialty"] != "verifier"
        or attempt_verifier["request_digest"] != rd
        or attempt_verifier["project_id"] != binding.project_id
        or attempt_verifier["work_item_id"] != binding.work_item_id
        or attempt_verifier["realm_id"] != execution_input["realm_id"]
        or attempt_verifier["parent_assignment_id"] != execution_input["assignment_id"]
        or attempt_verifier["configured_model"] != execution_input["verifier_model_id"]
        or attempt_verifier["provider_profile_digest"] != execution_input["provider_profile_digest"]
        or attempt_verifier["envelope_digest"] != inner["verifier_envelope"]
        or attempt_verifier["context"].get("candidate_digest") != attempt.candidate_digest
        or attempt_verifier["context"].get("measurement_digest") != extra["measurement_digest"]
        or attempt_verifier["context"].get("builder_envelope_digest") != inner["builder_envelope"]
        or parse_verify_body(attempt_verifier["result_body"], request).effective_verdict
        is not Verdict.ACCEPTED
    ):
        raise PolicyViolation("Verifier source actual attempt quality review drift")
    facts = {"state": measurement.get("report_facts_state", "not-captured")}
    if facts["state"] not in {"not-captured", "capture-rejected", "captured-parser-projection"}:
        raise PolicyViolation("Verifier source report facts state invalid")
    if measurement.get("report_facts_digest") is not None:
        if facts["state"] != "captured-parser-projection":
            raise PolicyViolation("Verifier source report facts state drift")
        facts = validate_report_facts(
            _object(objects, measurement["report_facts_digest"]), record=record, binding=measured
        )
        if facts["projection_digest"] != measurement["report_facts_digest"]:
            raise PolicyViolation("Verifier source report facts canonical identity drift")
    elif facts["state"] == "captured-parser-projection":
        raise PolicyViolation("Verifier source report facts missing")
    body = {
        "schema": "zekam-unit-test-verifier-source/v1",
        "job_id": snapshot["job_id"],
        "source_evidence_digest": snapshot["terminal_evidence_digest"],
        "execution_id": binding.run_id,
        "project_id": binding.project_id,
        "work_item_id": binding.work_item_id,
        "source_snapshot_id": binding.source_snapshot_id,
        "source_revision": request.source_revision,
        "request_digest": rd,
        "terminal_evidence_digest": terminal.evidence_digest,
        "attempt_evidence_digest": receipt.evidence_digest,
        "measurement_record_digest": extra["measurement_digest"],
        "candidate_digest": attempt.candidate_digest,
        "final_verifier_execution_digest": extra["final_verifier_execution_digest"],
        "builder_execution_digests": extra["builder_execution_digests"],
        "provenance": provenance,
        "parser_report_facts": facts,
        "canonical_terminal_verified": True,
        "quality_review_recorded": True,
        "independent_execution_observed": provenance["execution_independence"]
        == "distinct-native-executions",
        "independent_quality_verified": False,
        "current_source_verified": False,
        "publication_authorized": False,
        "provider_calls": 0,
        "grants_authority": False,
    }
    return body | {"report_digest": digest(body)}
