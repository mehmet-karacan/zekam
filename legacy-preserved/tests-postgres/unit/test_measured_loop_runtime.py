from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from zekam.application.execution import ExecutionHost
from zekam.application.measured_loop_runtime import (
    LocalMeasuredLoopDriver,
    PinnedLocalDriverSpec,
    load_local_driver_config,
)
from zekam.application.worker import Worker, WorkerSettings
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation
from zekam.domain.runtime import AttemptOutcome, FailureCategory, JobKind
from zekam.infrastructure.postgres.runtime_repository import ClaimedWork
from zekam.infrastructure.process.capability_worker import (
    CapabilityWorkerResult,
    CapabilityWorkerStatus,
)
from zekam.interfaces.cli.worker import app

pytestmark = pytest.mark.unit


def _config(path: Path, *, network_allowed: bool = False) -> Path:
    builder = path.parent / "builder.exe"
    verifier = path.parent / "verifier.exe"
    builder.write_bytes(b"native-builder")
    verifier.write_bytes(b"native-verifier")
    path.write_text(
        json.dumps(
            {
                "schema": "zekam-measured-loop-local-drivers/v2",
                "builder_argv": [str(builder), "--json-ipc"],
                "builder_executable_sha256": digest_of_bytes(builder.read_bytes()),
                "verifier_argv": [str(verifier), "--json-ipc"],
                "verifier_executable_sha256": digest_of_bytes(verifier.read_bytes()),
                "timeout_seconds": 30,
                "network_allowed": network_allowed,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_local_driver_config_is_exact_and_network_deny(tmp_path: Path) -> None:
    builder, verifier, timeout = load_local_driver_config(_config(tmp_path / "driver.json"))
    assert builder.argv[1:] == ("--json-ipc",)
    assert verifier.argv[1:] == ("--json-ipc",)
    assert builder.executable_digest != verifier.executable_digest
    assert timeout == 30

    with pytest.raises(PolicyViolation, match="network-deny"):
        load_local_driver_config(_config(tmp_path / "remote-driver.json", network_allowed=True))


def test_measured_loop_cli_dry_run_exposes_exact_capability(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["measured-loop-tick", "--config", str(_config(tmp_path / "driver.json")), "--json"],
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["required_capability"] == "loop.measured-attempt"
    assert document["max_attempts_per_job"] == 1
    assert document["network_allowed"] is False
    assert document["provider_calls"] == 0
    assert document["applied"] is False


def test_pinned_driver_rejects_interpreter_and_digest_drift(tmp_path: Path) -> None:
    executable = Path(sys.executable).resolve()
    executable_digest = digest_of_bytes(executable.read_bytes())
    with pytest.raises(PolicyViolation, match="shell/interpreter"):
        PinnedLocalDriverSpec((str(executable),), executable_digest)
    native = tmp_path / "native-driver.exe"
    native.write_bytes(b"native")
    with pytest.raises(PolicyViolation, match="SHA-256 drift"):
        PinnedLocalDriverSpec((str(native),), digest("wrong"))


def test_child_result_echo_is_exact_and_measurement_identity_is_runtime_derived() -> None:
    from zekam.application import measured_loop_runtime as runtime

    loop_id = uuid4()
    attempt_id = uuid4()
    scope_digest = digest("scope")
    payload = {
        "schema": "zekam-measured-loop-builder-result/v1",
        "loop_id": str(loop_id),
        "attempt_id": str(attempt_id),
        "execution_scope_digest": scope_digest,
    }
    result = SimpleNamespace(status=CapabilityWorkerStatus.COMPLETED, payload=payload)
    policy = SimpleNamespace(id=loop_id)
    admission = SimpleNamespace(attempt_id=attempt_id)
    assert (
        runtime._bound_process_result(
            result,
            schema="zekam-measured-loop-builder-result/v1",
            policy=policy,
            admission=admission,
            execution_scope_digest=scope_digest,
        )
        == payload
    )
    with pytest.raises(PolicyViolation, match="scope echo drift"):
        runtime._bound_process_result(
            SimpleNamespace(
                status=CapabilityWorkerStatus.COMPLETED,
                payload={**payload, "attempt_id": str(uuid4())},
            ),
            schema="zekam-measured-loop-builder-result/v1",
            policy=policy,
            admission=admission,
            execution_scope_digest=scope_digest,
        )

    evidence = runtime._evidence(
        [
            {
                "metric_id": "quality",
                "value": 1.0,
                "evidence_ref": "evidence:quality",
                "evidence_digest": digest("evidence"),
                "measured_at": dt.datetime.now(dt.UTC).isoformat(),
                "measurement_identity": "untrusted-child-identity",
            }
        ],
        source_revision="git:source",
        measurement_identity="canonical-builder-invocation",
        verifier_identity="canonical-verifier-invocation",
    )
    assert evidence[0].measurement_identity == "canonical-builder-invocation"


def test_driver_safety_allows_usage_counters_but_rejects_sensitive_content() -> None:
    from zekam.application import measured_loop_runtime as runtime

    runtime._assert_safe(
        {
            "actual_input_tokens": 12,
            "actual_output_tokens": 4,
            "remaining_tokens": 100,
            "evidence_ref": "evidence:quality",
        }
    )
    with pytest.raises(PolicyViolation, match="sensitive alan"):
        runtime._assert_safe({"raw_transcript": "redacted"})
    with pytest.raises(PolicyViolation, match="sensitive deger"):
        runtime._assert_safe({"note": "person@example.invalid"})


def test_effect_sonrasi_crash_failed_degildir_recovery_required_olur() -> None:
    host = Mock(spec=ExecutionHost)
    work = SimpleNamespace(job=SimpleNamespace(id=uuid4(), kind=JobKind.MUTATION))
    host.acquire_work.return_value = work
    host.ledger.claims_for_job.return_value = (SimpleNamespace(id=uuid4()),)

    def finish(_work, *, outcome, **_kwargs):  # type: ignore[no-untyped-def]
        assert outcome is AttemptOutcome.RECOVERY_REQUIRED
        return True

    host.finish.side_effect = finish

    def crash_after_effect(_work: ClaimedWork) -> str:
        raise RuntimeError("effect committed; receipt write crashed")

    worker = Worker(
        host=cast(ExecutionHost, host),
        settings=WorkerSettings(
            worker_label="measured-loop-worker",
            capabilities=("loop.measured-attempt",),
            max_iterations=1,
        ),
        handlers={str(JobKind.MUTATION): crash_after_effect},
    )
    result = worker.tick()
    assert result.outcome is AttemptOutcome.RECOVERY_REQUIRED
    assert host.finish.call_count == 1
    assert host.finish.call_args_list[0].kwargs == {
        "outcome": AttemptOutcome.RECOVERY_REQUIRED,
        "failure_category": FailureCategory.ADAPTER,
        "result_digest": host.finish.call_args_list[0].kwargs["result_digest"],
        "now": host.finish.call_args_list[0].kwargs["now"],
    }
    host.acquire_work.assert_called_once()


def test_local_driver_effect_marker_sonrasi_failed_output_receipt_yazmaz(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from zekam.application import measured_loop_runtime as runtime

    marker = tmp_path / "effect.marker"
    builder_executable = tmp_path / "builder.exe"
    verifier_executable = tmp_path / "verifier.exe"
    builder_executable.write_bytes(b"builder")
    verifier_executable.write_bytes(b"verifier")
    realm_id = uuid4()
    loop_id = uuid4()
    builder_id = uuid4()
    verifier_id = uuid4()
    objective = SimpleNamespace(objective_digest=digest("objective"))
    policy = SimpleNamespace(
        id=loop_id,
        project_id=uuid4(),
        assignment_id=builder_id,
        validator_assignment_id=verifier_id,
        source_revision="git:local-test",
        plan_digest=digest("plan"),
        validator_spec_digest=digest("validator"),
    )
    monkeypatch.setattr(
        runtime.PostgresMeasuredLoopContractLoader,
        "load",
        lambda self, _loop_id: (objective, policy),
    )
    assignments = Mock()
    assignments.get.side_effect = lambda assignment_id: SimpleNamespace(
        id=assignment_id,
        agent_ref=("local-builder" if assignment_id == builder_id else "local-verifier"),
    )
    policy_repository = Mock()
    monkeypatch.setattr(
        runtime,
        "legacy_repository",
        lambda kind, *args, **kwargs: {
            "agent_assignment": assignments,
            "loop_policy": policy_repository,
        }[kind],
    )
    monkeypatch.setattr(runtime, "_source_root", lambda *args: tmp_path)
    monkeypatch.setattr(
        runtime,
        "_assert_bounded_loop_topology",
        lambda *args, **kwargs: (uuid4(), digest("topology")),
    )
    monkeypatch.setattr(
        runtime, "measured_loop_effect_scope_digest", lambda **kwargs: digest("scope")
    )
    monkeypatch.setattr(
        runtime,
        "_consume_and_claim_effect",
        lambda *args, **kwargs: SimpleNamespace(id=uuid4()),
    )
    monkeypatch.setattr(runtime, "_database_now", lambda *_: dt.datetime.now(dt.UTC))
    process = Mock()

    def failed_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        marker.write_text("effect-ran", encoding="utf-8")
        return CapabilityWorkerResult(
            "builder",
            CapabilityWorkerStatus.FAILED,
            error_code="crash-after-effect",
        )

    process.run.side_effect = failed_run
    monkeypatch.setattr(runtime, "CapabilityProcessWorker", lambda: process)
    host = Mock()
    host.claim_effect.return_value = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(runtime, "ExecutionHost", lambda *args, **kwargs: host)
    work = SimpleNamespace(
        job=SimpleNamespace(
            id=uuid4(),
            realm_id=realm_id,
            work_item_id=uuid4(),
            plan_id=uuid4(),
            resources=(),
            payload={
                "effect_authorization": {
                    "authorization_id": str(uuid4()),
                    "effect_digest": digest("scope"),
                },
                "effect_scope_digest": digest("scope"),
                "topology": {
                    "decision_id": str(uuid4()),
                    "decision_digest": digest("topology"),
                    "pattern": "bounded-loop",
                },
            },
        ),
        lease=SimpleNamespace(
            heartbeat_at=dt.datetime.now(dt.UTC),
            fencing_token=1,
        ),
        attempt_id=uuid4(),
        owner_token="owner",
    )
    admission = SimpleNamespace(loop_id=loop_id, attempt_id=uuid4(), ordinal=1)

    with pytest.raises(PolicyViolation, match="recovery-required"):
        LocalMeasuredLoopDriver(
            object(),
            realm_id,
            PinnedLocalDriverSpec(
                (str(builder_executable),), digest_of_bytes(builder_executable.read_bytes())
            ),
            PinnedLocalDriverSpec(
                (str(verifier_executable),), digest_of_bytes(verifier_executable.read_bytes())
            ),
            timeout_seconds=10,
        ).run(work, admission)

    assert marker.read_text(encoding="utf-8") == "effect-ran"
    host.record_success.assert_not_called()
    host.record_failure.assert_not_called()
