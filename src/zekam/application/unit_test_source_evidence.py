"""Read historical native measurement receipts; never mint publication authority."""

from __future__ import annotations

import json
from pathlib import Path

from zekam.application.canonical_job_runtime import _completed_result
from zekam.application.config import load_settings
from zekam.application.unit_test_runtime import NATIVE_CANDIDATE_CONTRACT, NATIVE_EVIDENCE_CONTRACT
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes, parse_digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import _connect
from zekam.infrastructure.sqlite.unit_test_ledger import SQLiteUnitTestLedger
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.unit_test_runner.tree_fingerprint import fingerprint_tree

MAX_BYTES = 4 * 1024 * 1024
MAX_OBSERVATIONS = 4096


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyViolation("Measurement source duplicate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise PolicyViolation("Measurement source nonfinite JSON value")


def _object(objects, key):
    parse_digest(key)
    info = objects.stat(key)
    if not 0 < info.size_bytes <= MAX_BYTES:
        raise PolicyViolation("Measurement source object exceeds read budget")
    raw = objects.get(key)
    if len(raw) != info.size_bytes or digest_of_bytes(raw) != key:
        raise PolicyViolation("Measurement source CAS digest/size drift")
    body = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(body, dict):
        raise PolicyViolation("Measurement source JSON object required")
    return body


def _linked_measurement(result, envelope, candidate, *, request, attempt, receipt, observations):
    """Cross-check persisted facts, including unsuccessful completed measurements."""
    inner = envelope.get("evidence")
    if not isinstance(inner, dict):
        raise PolicyViolation("Measurement source native evidence missing")
    record, binding = inner.get("measurement"), inner.get("binding")
    if not isinstance(record, dict) or not isinstance(binding, dict):
        raise PolicyViolation("Measurement source bound record required")
    expected_envelope = {
        "contract": NATIVE_EVIDENCE_CONTRACT,
        "request_digest": request.request_digest,
        "attempt_id": attempt.attempt_id,
        "ordinal": attempt.ordinal,
        "status": receipt.status.value,
        "evidence": inner,
    }
    if (
        canonical_bytes(envelope) != canonical_bytes(expected_envelope)
        or result.get("schema") != "zekam-unit-test-measure/v1"
        or result.get("request_digest") != request.request_digest
        or attempt.request_digest != request.request_digest
        or result.get("attempt_id") != attempt.attempt_id
        or type(result.get("ordinal")) is not int
        or result.get("ordinal") != attempt.ordinal
        or result.get("attempt_state") != receipt.status.value
        or result.get("receipt_recorded") is not True
        or result.get("evidence_digest") != receipt.evidence_digest
        or digest(record) != inner.get("measurement_record_digest")
        or digest(record) != result.get("measurement_record_digest")
        or record.get("binding_digest") != digest(binding)
        or binding.get("request_digest") != request.request_digest
        or binding.get("attempt_id") != attempt.attempt_id
        or binding.get("run_id") != f"{attempt.attempt_id}-run"
        or binding.get("test_candidate_digest") != attempt.candidate_digest
        or binding.get("source_revision") != request.source_revision
        or binding.get("scope") != "unit"
        or inner.get("authority") != "observation-only"
        or inner.get("quality_verification") != "ratio-observed-only"
        or result.get("quality_verification") != "ratio-observed-only"
        or result.get("terminal_recorded") is not False
    ):
        raise PolicyViolation("Measurement source request/attempt/record binding drift")
    for outer, nested in (
        ("measurement_status", "status"),
        ("build_exit_code", "exit_code"),
        ("observations", "observations"),
        ("aggregate", "aggregate"),
    ):
        if canonical_bytes(result.get(outer)) != canonical_bytes(record.get(nested)):
            raise PolicyViolation("Measurement source result/record drift")
    counts = record.get("test_counts")
    if (
        not isinstance(counts, list)
        or any(
            not isinstance(p, list)
            or len(p) != 2
            or not isinstance(p[0], str)
            or type(p[1]) is not int
            or p[1] < 0
            for p in counts
        )
        or len({p[0] for p in counts}) != len(counts)
        or canonical_bytes(dict(counts)) != canonical_bytes(result.get("test_counts"))
    ):
        raise PolicyViolation("Measurement source test counts drift")
    for name in ("outcome", "freshness", "drift", "failed_cases"):
        if canonical_bytes(result.get(name)) != canonical_bytes(inner.get(name)):
            raise PolicyViolation("Measurement source native result drift")
    persisted = [item.to_payload() for item in observations]
    measured = record.get("observations")
    if not isinstance(measured, list) or len(measured) > MAX_OBSERVATIONS:
        raise PolicyViolation("Measurement source observations exceed read budget")
    # Failed measurements deliberately persist no accepted observations.
    if receipt.status.value == "completed":
        if sorted(persisted, key=digest) != sorted(measured, key=digest):
            raise PolicyViolation("Measurement source accepted observations drift")
    elif persisted:
        raise PolicyViolation("Measurement source failed attempt has accepted observations")
    files = candidate.get("files")
    if (
        candidate.get("contract") != NATIVE_CANDIDATE_CONTRACT
        or candidate.get("kind") != "native-test-tree"
        or not isinstance(files, list)
        or any(
            not isinstance(p, list)
            or len(p) != 2
            or not isinstance(p[0], str)
            or not isinstance(p[1], str)
            for p in files
        )
        or len({p[0] for p in files}) != len(files)
    ):
        raise PolicyViolation("Measurement source test candidate invalid")
    for _path, value in files:
        parse_digest(value)
    tree_digest = fingerprint_tree(dict(files))
    if (
        candidate.get("test_tree_digest") != tree_digest
        or inner.get("test_tree_digest_before") != tree_digest
    ):
        raise PolicyViolation("Measurement source test tree binding drift")
    return record, binding


def _read_native_measurement_source(home: Path, reference: str) -> tuple:
    """Read a job ID/key with its actual execution key and immutable receipt chain.

    Historical freshness is reported as recorded. No current source check, raw
    Maven report reparse, independent quality review, signing or writes occur.
    """
    database = home / "state/operational.db"
    settings = load_settings(home=home)
    if settings.database.sqlite_path(home).resolve() != database.resolve():
        raise PolicyViolation("Measurement source exact canonical operational database required")
    runtime = SQLiteLocalRuntimeStore(database, existing_only=True)
    snapshot = runtime.job_snapshot(reference)
    if snapshot is None or snapshot["state"] != "completed":
        raise PolicyViolation("Measurement source completed canonical job required")
    binding = runtime.job_binding(snapshot["job_id"])
    payload = snapshot["payload"]
    if binding is None or payload.get("operation") != "test.measure":
        raise PolicyViolation("Measurement source canonical test.measure required")
    objects = LocalContentAddressedStore(home / settings.object_store_relative)
    terminal = _object(objects, snapshot["terminal_evidence_digest"])
    result = _completed_result(
        runtime, objects, snapshot, binding, "test.measure", payload["input_digest"]
    )
    if terminal != {
        k: v for k, v in result.items() if k not in {"canonical_job_id", "canonical_replayed"}
    }:
        raise PolicyViolation("Measurement source canonical result drift")
    connection = _connect(database, read_only=True)
    try:
        connection.execute("begin")
        rows = {}
        for table, field, value in (
            ("unit_test_request", "request_digest", payload["input_digest"]),
            ("unit_test_attempt", "attempt_id", result.get("attempt_id")),
        ):
            row = connection.execute(
                f"select length(cast(body_json as blob)) n from {table} where {field}=?", (value,)
            ).fetchone()
            if row is None or not 0 < row["n"] <= MAX_BYTES:
                raise PolicyViolation("Measurement source missing or oversized ledger body")
            rows[table] = connection.execute(
                f"select * from {table} where {field}=?", (value,)
            ).fetchone()
        ledger = SQLiteUnitTestLedger(connection)
        request = ledger.get_request(payload["input_digest"])
        attempt = ledger.get_attempt(result["attempt_id"])
        receipt = ledger.get_attempt_receipt(attempt.attempt_id)
        stored_attempt = rows["unit_test_attempt"]
        stored_request = rows["unit_test_request"]
        if (
            request.request_digest != payload["input_digest"]
            or request.project_id != binding.project_id
            or request.work_item_id != binding.work_item_id
            or request.run_id != binding.run_id
            or request.source_snapshot_id != binding.source_snapshot_id
            or attempt.plan_digest != binding.plan_digest
            or receipt is None
        ):
            raise PolicyViolation("Measurement source canonical Work/Run/receipt drift")
        for field in (
            "attempt_id",
            "request_digest",
            "ordinal",
            "parent_attempt_id",
            "plan_digest",
            "candidate_digest",
            "effect_digest",
        ):
            if stored_attempt[field] != getattr(attempt, field):
                raise PolicyViolation("Measurement source attempt column/body drift")
        if (
            stored_request["project_id"] != request.project_id
            or stored_request["source_binding_id"] != request.source_binding_id
            or stored_request["metric"] != request.metric.value
            or stored_request["policy"] != request.policy.value
            or stored_request["max_attempts"] != request.budget.max_attempts
        ):
            raise PolicyViolation("Measurement source request column/body drift")
        source = connection.execute(
            "select s.revision_ref,s.source_binding_id from source_snapshot s "
            "join source_binding b on b.id=s.source_binding_id where s.id=? and b.project_id=?",
            (binding.source_snapshot_id, binding.project_id),
        ).fetchone()
        if source is None or tuple(source) != (request.source_revision, request.source_binding_id):
            raise PolicyViolation("Measurement source historical snapshot drift")
        count = connection.execute(
            "select count(*) from (select 1 from unit_test_observation where attempt_id=? limit ?)",
            (attempt.attempt_id, MAX_OBSERVATIONS + 1),
        ).fetchone()[0]
        if count > MAX_OBSERVATIONS:
            raise PolicyViolation("Measurement source observation budget exceeded")
        observations = ledger.list_observations(attempt.attempt_id)
        envelope = _object(objects, receipt.evidence_digest)
        candidate = _object(objects, attempt.candidate_digest)
        record, measurement_binding = _linked_measurement(
            result,
            envelope,
            candidate,
            request=request,
            attempt=attempt,
            receipt=receipt,
            observations=observations,
        )
        facts_digest = envelope["evidence"].get("report_facts_digest")
        facts_state = envelope["evidence"].get("report_facts_state", "not-captured")
        if facts_state not in {"not-captured", "capture-rejected", "captured-parser-projection"}:
            raise PolicyViolation("Measurement source report facts state invalid")
        report_facts = {"state": facts_state, "projection_digest": None}
        if facts_digest is not None:
            from zekam.infrastructure.unit_test_runner.report_facts import validate_report_facts

            if facts_state != "captured-parser-projection":
                raise PolicyViolation("Measurement source report facts state drift")
            report_facts = validate_report_facts(
                _object(objects, facts_digest), record=record, binding=measurement_binding
            )
            if report_facts["projection_digest"] != facts_digest:
                raise PolicyViolation("Measurement source report facts CAS identity drift")
        elif facts_state == "captured-parser-projection":
            raise PolicyViolation("Measurement source captured report facts missing")
    finally:
        connection.close()
    body = {
        "schema": "zekam-native-measurement-source/v1",
        "job_id": snapshot["job_id"],
        "execution_id": binding.run_id,
        "project_id": binding.project_id,
        "work_item_id": binding.work_item_id,
        "config_revision_id": binding.config_revision_id,
        "config_digest": binding.config_digest,
        "source_snapshot_id": binding.source_snapshot_id,
        "request_digest": request.request_digest,
        "source_revision": request.source_revision,
        "source_evidence_digest": snapshot["terminal_evidence_digest"],
        "attempt_id": attempt.attempt_id,
        "attempt_state": receipt.status.value,
        "attempt_evidence_digest": receipt.evidence_digest,
        "measurement_record_digest": digest(record),
        "measurement_binding_digest": digest(measurement_binding),
        "candidate_digest": attempt.candidate_digest,
        "outcome": result["outcome"],
        "measurement_status": record["status"],
        "build_exit_code": record["exit_code"],
        "test_counts": result["test_counts"],
        "recorded_freshness": result["freshness"],
        "recorded_drift": result["drift"],
        "canonical_receipt_verified": True,
        "attempt_receipt_verified": True,
        "parser_report_facts": report_facts,
        "current_source_verified": False,
        "raw_reports_verified": False,
        "independent_quality_verified": False,
        "publication_authorized": False,
        "provider_calls": 0,
        "grants_authority": False,
    }
    tree_key = envelope["evidence"].get("test_tree_evidence_digest")
    if tree_key is not None:
        from zekam.infrastructure.unit_test_runner.tree_evidence import validate_measured_tree

        tree = _object(objects, tree_key)
        files = validate_measured_tree(tree)
        if receipt.status.value == "completed" and (
            tree["state"] != "stable"
            or tree["before_digest"] != candidate["test_tree_digest"]
            or canonical_bytes(sorted(files.items())) != canonical_bytes(candidate["files"])
        ):
            raise PolicyViolation("Measurement source captured full test tree drift")
        body["measured_test_tree"] = {
            "evidence_digest": tree_key,
            "state": tree["state"],
            "before_digest": tree["before_digest"],
            "after_digest": tree["after_digest"],
            "file_count": len(files),
            "module_dirs": tree["module_dirs"],
        }
    return body | {"report_digest": digest(body)}, request, record, measurement_binding, candidate


def read_native_measurement_source(home: Path, reference: str) -> dict:
    """Public historical report; no current checkout or publication authority."""
    return _read_native_measurement_source(home, reference)[0]
