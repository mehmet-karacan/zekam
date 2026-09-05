from __future__ import annotations

import datetime as dt
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest

import zekam.application.measured_loop_runtime as runtime
from zekam.application.measured_loop_runtime import PinnedLocalDriverSpec
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.process.capability_worker import CapabilityWorkerStatus

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 12))


class _Cursor:
    def __init__(self, row: object) -> None:
        self.row = row
        self.executions: list[tuple[str, object]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, params: object = None) -> None:
        self.executions.append((statement, params))

    def fetchone(self) -> object:
        return self.row


class _Connection:
    def __init__(self, row: object) -> None:
        self.cursor_value = _Cursor(row)

    def cursor(self) -> _Cursor:
        return self.cursor_value

    def transaction(self) -> nullcontext[None]:
        return nullcontext()


def _driver(path: Path, *argv: str) -> PinnedLocalDriverSpec:
    return PinnedLocalDriverSpec(
        (str(path), *argv),
        digest_of_bytes(path.read_bytes()),
    )


def _work() -> SimpleNamespace:
    job = SimpleNamespace(
        id=IDS[1],
        realm_id=IDS[0],
        work_item_id=IDS[2],
        plan_id=IDS[3],
        project_id=IDS[4],
        resources=(SimpleNamespace(resource=SimpleNamespace(text="project:one")),),
        payload={},
    )
    return SimpleNamespace(
        job=job,
        attempt_id=IDS[5],
        owner_token="owner",
        lease=SimpleNamespace(fencing_token=7),
    )


def test_contract_loader_rejects_missing_and_digest_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PolicyViolation, match="bulunamadi"):
        runtime.PostgresMeasuredLoopContractLoader(_Connection(None), IDS[0]).load(IDS[1])

    objective = SimpleNamespace(objective_digest=digest("objective"), realm_id=IDS[0])
    policy = SimpleNamespace(policy_digest=digest("policy"), id=IDS[1])
    monkeypatch.setattr(runtime, "_objective", lambda _body: objective)
    monkeypatch.setattr(runtime, "_policy", lambda _body: policy)
    row = ({"kind": "objective"}, digest("wrong"), {"kind": "policy"}, policy.policy_digest)
    with pytest.raises(PolicyViolation, match="digest drift"):
        runtime.PostgresMeasuredLoopContractLoader(_Connection(row), IDS[0]).load(IDS[1])

    objective_body = {"kind": "objective"}
    policy_body = {"kind": "policy"}
    objective = SimpleNamespace(objective_digest=digest(objective_body), realm_id=IDS[0])
    policy = SimpleNamespace(policy_digest=digest(policy_body), id=IDS[1])
    monkeypatch.setattr(runtime, "_objective", lambda _body: objective)
    monkeypatch.setattr(runtime, "_policy", lambda _body: policy)
    assert runtime.PostgresMeasuredLoopContractLoader(
        _Connection(
            (objective_body, objective.objective_digest, policy_body, policy.policy_digest)
        ),
        IDS[0],
    ).load(IDS[1]) == cast(Any, (objective, policy))


def test_build_worker_rejects_same_driver_command(tmp_path: Path) -> None:
    executable = tmp_path / "native"
    executable.write_bytes(b"native")
    driver = _driver(executable, "same")
    with pytest.raises(PolicyViolation, match="ayri argv"):
        runtime.build_production_measured_loop_worker(
            object(), IDS[0], builder=driver, verifier=driver
        )


def test_source_root_requires_one_usable_local_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = Mock()
    monkeypatch.setattr(runtime, "legacy_repository", lambda *_args: repo)
    repo.for_project.return_value = []
    with pytest.raises(PolicyViolation, match="exact usable"):
        runtime._source_root(object(), IDS[0], IDS[1])

    binding = SimpleNamespace(id=IDS[2], is_usable=False)
    repo.for_project.return_value = [binding]
    with pytest.raises(PolicyViolation, match="exact usable"):
        runtime._source_root(object(), IDS[0], IDS[1])

    binding.is_usable = True
    repo.local_path.return_value = None
    with pytest.raises(PolicyViolation, match="bulunamadi"):
        runtime._source_root(object(), IDS[0], IDS[1])

    repo.local_path.return_value = str(tmp_path)
    assert runtime._source_root(object(), IDS[0], IDS[1]) == tmp_path.resolve()


def test_effect_claim_rejects_authority_drift_and_non_atomic_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = _work()
    policy = SimpleNamespace(plan_digest=digest("plan"))
    authorization = SimpleNamespace(
        realm_id=IDS[0],
        work_item_id=work.job.work_item_id,
        plan_id=work.job.plan_id,
        plan_digest=policy.plan_digest,
        effect_digest=digest("effect"),
        rejection_reason=lambda _now: None,
        scope=SimpleNamespace(allowed_resources=("project:one",), allowed_effects=("process-run",)),
        authorization_digest=digest("authorization"),
    )
    repo = Mock()
    repo.get.return_value = authorization
    monkeypatch.setattr(runtime, "legacy_repository", lambda *_args: repo)
    host = Mock()

    authorization.realm_id = IDS[9]
    with pytest.raises(PolicyViolation, match="authorization drift"):
        runtime._consume_and_claim_effect(
            _Connection(None),
            IDS[0],
            host=host,
            authorization_id=IDS[6],
            work=work,
            policy=cast(Any, policy),
            effect_digest=digest("effect"),
            driver_digest=digest("driver"),
            now=NOW,
        )

    authorization.realm_id = IDS[0]
    repo.consume.return_value = SimpleNamespace(consumed=False, authorization=None)
    with pytest.raises(PolicyViolation, match="atomik tuketilemedi"):
        runtime._consume_and_claim_effect(
            _Connection(None),
            IDS[0],
            host=host,
            authorization_id=IDS[6],
            work=work,
            policy=cast(Any, policy),
            effect_digest=digest("effect"),
            driver_digest=digest("driver"),
            now=NOW,
        )

    expected_consumer = (
        f"measured-loop:{work.job.id}:{work.attempt_id}:{work.lease.fencing_token}:"
        f"{digest('driver')}"
    )
    repo.consume.return_value = SimpleNamespace(
        consumed=True, authorization=SimpleNamespace(consumed_by=expected_consumer)
    )
    host.claim_effect.return_value = "claim"
    assert (
        runtime._consume_and_claim_effect(
            _Connection(None),
            IDS[0],
            host=host,
            authorization_id=IDS[6],
            work=work,
            policy=cast(Any, policy),
            effect_digest=digest("effect"),
            driver_digest=digest("driver"),
            now=NOW,
        )
        == "claim"
    )


def test_database_clock_and_lease_refresh_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    assert runtime._database_now(_Connection((NOW,))) == NOW
    work = _work()
    host = SimpleNamespace(
        connection=_Connection((NOW,)), jobs=SimpleNamespace(heartbeat=Mock(return_value=True))
    )
    runtime._refresh_exact_lease(cast(Any, host), work, 42)
    host.jobs.heartbeat.return_value = False
    with pytest.raises(PolicyViolation, match="stale"):
        runtime._refresh_exact_lease(cast(Any, host), work, 42)


def test_topology_binding_validation_and_persisted_state() -> None:
    work = _work()
    policy = SimpleNamespace(project_id=IDS[4], work_item_id=IDS[2], plan_id=IDS[3])
    for binding, message in (
        (None, "canonical topology"),
        ({"decision_id": "bad", "decision_digest": digest("d"), "pattern": "bounded-loop"}, "UUID"),
        (
            {"decision_id": str(IDS[7]), "decision_digest": digest("d"), "pattern": "fan-out"},
            "bounded-loop",
        ),
    ):
        work.job.payload["topology"] = binding
        with pytest.raises((PolicyViolation, ValidationFailed), match=message):
            runtime._assert_bounded_loop_topology(
                _Connection(None), IDS[0], work=work, policy=cast(Any, policy)
            )

    binding = {
        "decision_id": str(IDS[7]),
        "decision_digest": digest("d"),
        "pattern": "bounded-loop",
    }
    work.job.payload["topology"] = binding
    with pytest.raises(PolicyViolation, match="state drift"):
        runtime._assert_bounded_loop_topology(
            _Connection(None), IDS[0], work=work, policy=cast(Any, policy)
        )

    row = (digest("d"), "bounded-loop", IDS[4], IDS[2], IDS[3])
    assert runtime._assert_bounded_loop_topology(
        _Connection(row), IDS[0], work=work, policy=cast(Any, policy)
    ) == (IDS[7], digest("d"))


def test_process_result_is_fail_closed_and_scope_bound() -> None:
    policy = SimpleNamespace(id=IDS[0])
    admission = SimpleNamespace(attempt_id=IDS[1])
    schema = "schema/v1"
    scope = digest("scope")
    with pytest.raises(PolicyViolation, match="belirsiz"):
        runtime._bound_process_result(
            SimpleNamespace(status=CapabilityWorkerStatus.FAILED, payload={}),
            schema=schema,
            policy=cast(Any, policy),
            admission=admission,
            execution_scope_digest=scope,
        )
    with pytest.raises(PolicyViolation, match="belirsiz"):
        runtime._bound_process_result(
            SimpleNamespace(status=CapabilityWorkerStatus.COMPLETED, payload=None),
            schema=schema,
            policy=cast(Any, policy),
            admission=admission,
            execution_scope_digest=scope,
        )
    body = {
        "schema": schema,
        "loop_id": str(IDS[0]),
        "attempt_id": str(IDS[1]),
        "execution_scope_digest": digest("wrong"),
    }
    with pytest.raises(PolicyViolation, match="scope echo drift"):
        runtime._bound_process_result(
            SimpleNamespace(status=CapabilityWorkerStatus.COMPLETED, payload=body),
            schema=schema,
            policy=cast(Any, policy),
            admission=admission,
            execution_scope_digest=scope,
        )
    body["execution_scope_digest"] = scope
    assert (
        runtime._bound_process_result(
            SimpleNamespace(status=CapabilityWorkerStatus.COMPLETED, payload=body),
            schema=schema,
            policy=cast(Any, policy),
            admission=admission,
            execution_scope_digest=scope,
        )
        == body
    )


def test_measurement_rows_and_recursive_sensitive_value_guard() -> None:
    with pytest.raises(ValidationFailed, match="listesi"):
        runtime._evidence({}, source_revision="r1", measurement_identity="m", verifier_identity="v")
    with pytest.raises(ValidationFailed, match="object"):
        runtime._evidence(
            [1], source_revision="r1", measurement_identity="m", verifier_identity="v"
        )
    with pytest.raises(ValidationFailed, match="gecersiz"):
        runtime._evidence(
            [{}], source_revision="r1", measurement_identity="m", verifier_identity="v"
        )

    row = {
        "metric_id": "latency",
        "value": 1.5,
        "evidence_ref": "artifact:one",
        "evidence_digest": digest("evidence"),
        "measured_at": NOW.isoformat(),
    }
    evidence = runtime._evidence(
        [row], source_revision="r1", measurement_identity="m", verifier_identity="v"
    )
    assert evidence[0].metric_id == "latency"

    runtime._assert_safe({"nested": ["ordinary", 1]})
    for value in (
        {"api_key": "redacted"},
        ["user@example.com"],
        {"nested": ["-----BEGIN " + "PRIVATE KEY-----"]},
    ):
        with pytest.raises(PolicyViolation, match="sensitive"):
            runtime._assert_safe(value)
