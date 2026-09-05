from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from zekam.application import projection_close_runtime as close_runtime
from zekam.application.model_health_service import (
    ModelHealthService,
    ProbeUnavailable,
    StubProviderProbe,
    contract_coverage,
)
from zekam.application.run_reconciliation import TerminalRunReconciliationService
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_health import (
    CONTRACTS_BY_MODALITY,
    CapabilityCheck,
    ProbeFailure,
    ProbeOutcome,
    ProbeStatus,
)
from zekam.domain.model_inventory import (
    HealthState,
    Modality,
    ModelProvenance,
    ModelRecord,
    ProviderProtocol,
)
from zekam.domain.realm import Realm

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(int=value) for value in range(1, 20))


def _model(
    model_id: str,
    modality: Modality = Modality.CHAT,
    *,
    health: HealthState = HealthState.UNTESTED,
    quarantine_until: dt.datetime | None = None,
    enabled: bool = True,
) -> ModelRecord:
    category = "not-mapped" if modality is Modality.UNKNOWN else modality.value
    return ModelRecord(
        model_id=model_id,
        inventory_index=int(model_id.removeprefix("m") or "1"),
        access_name=f"access-{model_id}",
        backend_model=f"backend-{model_id}",
        provider_protocol=ProviderProtocol.OPENAI,
        declared_category=category,
        endpoint_ref=f"model-endpoint:{model_id}",
        credential_ref=f"model-credential:{model_id}",
        provenance=ModelProvenance("inventory.md"),
        health_state=health,
        quarantine_until=quarantine_until,
        enabled=enabled,
    )


class _Inventory:
    def __init__(self, records: tuple[ModelRecord, ...]) -> None:
        self.records = {record.model_id: record for record in records}
        self.metadata: dict[str, tuple[dt.datetime | None, str | None, str | None]] = {}
        self.health_calls: list[dict[str, Any]] = []

    def get(self, model_id: str) -> ModelRecord:
        return self.records[model_id]

    def list_all(self) -> tuple[ModelRecord, ...]:
        return tuple(self.records.values())

    def set_health(self, model_id: str, **values: Any) -> None:
        self.health_calls.append({"model_id": model_id, **values})
        record = self.records[model_id]
        verified = values.get("verified_capabilities")
        self.records[model_id] = replace(
            record,
            health_state=values["state"],
            quarantine_until=values["quarantine_until"],
            benchmark_state=values["benchmark_state"],
            capabilities_verified=(
                record.capabilities_verified if verified is None else tuple(verified)
            ),
        )
        self.metadata[model_id] = (
            values.get("now"),
            values["policy_digest"],
            values["inventory_digest"],
        )

    def health_metadata(self, model_id: str) -> tuple[dt.datetime | None, str | None, str | None]:
        return self.metadata.get(model_id, (None, None, None))


class _Probes:
    def __init__(self) -> None:
        self.values: dict[str, list[ProbeOutcome]] = {}

    def record(self, outcome: ProbeOutcome, **_values: Any) -> UUID:
        self.values.setdefault(outcome.model_id, []).append(outcome)
        return uuid4()

    def history(self, model_id: str, *, limit: int = 20) -> tuple[ProbeOutcome, ...]:
        return tuple(self.values.get(model_id, ())[-limit:])


class _Capabilities:
    def __init__(self) -> None:
        self.values: dict[str, list[CapabilityCheck]] = {}

    def record(self, check: CapabilityCheck) -> UUID:
        self.values.setdefault(check.model_id, []).append(check)
        return uuid4()

    def latest_for_model(self, model_id: str) -> tuple[CapabilityCheck, ...]:
        return tuple(self.values.get(model_id, ()))


class _Quarantine:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, **values: Any) -> UUID:
        self.events.append(values)
        return uuid4()


def _health_service(
    records: tuple[ModelRecord, ...], probe: Any
) -> tuple[ModelHealthService, _Inventory, _Probes, _Capabilities, _Quarantine]:
    inventory = _Inventory(records)
    probes = _Probes()
    capabilities = _Capabilities()
    quarantine = _Quarantine()
    service = ModelHealthService(
        cast(Any, inventory),
        cast(Any, probes),
        cast(Any, capabilities),
        cast(Any, quarantine),
        cast(Any, probe),
    )
    return service, inventory, probes, capabilities, quarantine


def test_health_unknown_passed_invalid_unavailable_and_unexpected_provider() -> None:
    records = tuple(
        _model(f"m{index}", modality)
        for index, modality in enumerate(
            (
                Modality.UNKNOWN,
                Modality.CHAT,
                Modality.CHAT,
                Modality.CHAT,
                Modality.CHAT,
            ),
            1,
        )
    )

    class Probe:
        def run(self, record: ModelRecord, _fixture: Any) -> Mapping[str, Any]:
            if record.model_id == "m2":
                return {"text": "healthy"}
            if record.model_id == "m3":
                return {"text": ""}
            if record.model_id == "m4":
                raise ProbeUnavailable("offline")
            raise RuntimeError("raw-provider-secret-must-not-persist")

    service, inventory, probes, _, quarantine = _health_service(records, Probe())
    results = service.run_all(now=NOW)
    assert [item.outcome.status for item in results] == [
        ProbeStatus.SKIPPED,
        ProbeStatus.PASSED,
        ProbeStatus.FAILED,
        ProbeStatus.FAILED,
        ProbeStatus.FAILED,
    ]
    assert results[3].outcome.failure is ProbeFailure.TRANSPORT
    assert results[4].outcome.failure is ProbeFailure.UNKNOWN
    assert results[4].outcome.detail == "RuntimeError"
    assert "raw-provider-secret" not in repr(results[4].as_dict())
    assert inventory.get("m2").health_state is HealthState.HEALTH_PASSED
    assert len(probes.values) == 5 and not quarantine.events


def test_health_quarantine_release_and_cooldown_boundaries() -> None:
    records = (
        _model("m1"),
        _model("m2", health=HealthState.QUARANTINED, quarantine_until=NOW),
        _model(
            "m3",
            health=HealthState.QUARANTINED,
            quarantine_until=NOW + dt.timedelta(seconds=1),
        ),
        _model("m4", health=HealthState.QUARANTINED, quarantine_until=None),
    )
    service, inventory, _, _, quarantine = _health_service(
        records, StubProviderProbe(unavailable=frozenset({"m1"}))
    )
    service.run_probe("m1", now=NOW)
    result = service.run_probe("m1", now=NOW)
    assert result.quarantined and result.decision.consecutive_failures == 2
    assert quarantine.events[-1]["action"] == "quarantined"
    assert service.release_expired_quarantines(now=NOW) == ("m2",)
    assert inventory.get("m2").health_state is HealthState.UNTESTED
    assert inventory.get("m3").health_state is HealthState.QUARANTINED
    assert inventory.get("m4").health_state is HealthState.QUARANTINED
    assert quarantine.events[-1]["action"] == "released"


def test_health_capability_promotion_staleness_and_benchmark_gate() -> None:
    record = _model("m1", health=HealthState.HEALTH_PASSED)
    service, inventory, _, capabilities, _ = _health_service((record,), StubProviderProbe())
    assert not service.promote_to_contract_passed("m1", now=NOW)
    expected = CONTRACTS_BY_MODALITY[Modality.CHAT]
    for capability in expected:
        check = service.record_capability(
            "m1",
            capability=capability,
            verified=True,
            evidence=f"verified {capability.value}",
            now=NOW,
        )
        assert check.verified
    assert service.promote_to_contract_passed("m1", now=NOW)
    assert inventory.get("m1").health_state is HealthState.CONTRACT_PASSED
    assert set(inventory.get("m1").capabilities_verified) == {item.value for item in expected}
    assert all(contract_coverage(Modality.CHAT, capabilities.latest_for_model("m1")).values())
    assert service.benchmark_eligible(now=NOW) == (inventory.get("m1"),)
    assert (
        service.require_benchmark_eligible(
            "m1", inventory_digest=inventory.get("m1").inventory_digest, now=NOW
        ).model_id
        == "m1"
    )
    with pytest.raises(PolicyViolation, match="digest"):
        service.require_benchmark_eligible("m1", inventory_digest=digest("wrong"), now=NOW)
    assert not service.promote_to_contract_passed("m1", now=NOW)

    inventory.metadata["m1"] = (NOW, digest("old-policy"), record.inventory_digest)
    assert service.staleness_of("m1", now=NOW).stale
    assert service.stale_models(now=NOW) == ("m1",)
    assert service.benchmark_eligible(now=NOW) == ()
    with pytest.raises(PolicyViolation, match="fresh"):
        service.require_benchmark_eligible(
            "m1", inventory_digest=inventory.get("m1").inventory_digest, now=NOW
        )


class _ReconCursor:
    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.rows: list[tuple[Any, ...]] = []
        self.rowcount = 1

    def __enter__(self) -> _ReconCursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, statement: str, _parameters: Any) -> None:
        if "from runtime.execution_run" in statement and "created_at>%s" not in statement:
            header = self.scenario.get("header", (IDS[1], IDS[2], IDS[3], NOW, "git:old"))
            self.rows = [] if header is None else [header]
        elif "from runtime.job job" in statement and "left join" in statement:
            self.rows = list(self.scenario.get("jobs", ()))
        elif "from runtime.lease" in statement:
            self.rows = [(self.scenario.get("leases", 0),)]
        elif "created_at>%s" in statement:
            self.rows = list(self.scenario.get("newer", ()))
        elif "from projects.source_binding" in statement:
            source = self.scenario.get("source")
            self.rows = [] if source is None else [(source,)]
        else:
            raise AssertionError(statement)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


class _ReconConnection:
    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario

    def cursor(self) -> _ReconCursor:
        return _ReconCursor(self.scenario)


def _realm() -> Realm:
    return Realm(IDS[0], "yerel", "Yerel", NOW)


def _job_row(
    *,
    state: str = "failed",
    attempt: UUID | None = IDS[5],
    outcome: str | None = "failed",
    claim: UUID | None = None,
    receipt: UUID | None = None,
    receipt_status: str | None = None,
    receipt_digest: str | None = None,
    capabilities: tuple[str, ...] = ("database.write",),
) -> tuple[Any, ...]:
    return (
        IDS[4],
        state,
        "step",
        attempt,
        outcome,
        digest("result") if outcome else None,
        claim,
        receipt,
        receipt_status,
        receipt_digest,
        capabilities,
    )


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ({"header": None}, "active"),
        ({"header": (IDS[1], None, IDS[3], NOW, "git:old")}, "active"),
        ({"jobs": ()}, "terminal job"),
        ({"jobs": (_job_row(state="running"),)}, "live job"),
        ({"jobs": (_job_row(attempt=None, outcome=None),)}, "attempt"),
        (
            {"jobs": (_job_row(claim=IDS[6], receipt=None),)},
            "receiptless",
        ),
        ({"jobs": (_job_row(),), "leases": 1}, "live lease"),
        ({"jobs": (_job_row(state="cancelled"),)}, "failed veya completed"),
        (
            {"jobs": (_job_row(state="completed", outcome="succeeded"),)},
            "receipt zinciri",
        ),
    ],
)
def test_reconciliation_terminal_evidence_guards(scenario: dict[str, Any], message: str) -> None:
    service = TerminalRunReconciliationService(_ReconConnection(scenario), _realm())
    with pytest.raises(PolicyViolation, match=message):
        service.prepare(run_id=IDS[7], now=NOW)


def test_reconciliation_failed_terminal_plan_and_optional_digest_branches() -> None:
    service = TerminalRunReconciliationService(_ReconConnection({"jobs": (_job_row(),)}), _realm())
    plan = service.prepare(run_id=IDS[7], now=NOW)
    assert plan.mode == "failed-terminal-job"
    assert plan.superseded_by_run_id is None
    assert plan.as_dict()["grants_authority"] is False
    superseded = replace(
        plan,
        mode="superseded-completed-only",
        superseded_by_run_id=IDS[8],
        cancelled_job_ids=(IDS[9],),
    )
    assert superseded.as_dict()["superseded_by_run_id"] == str(IDS[8])
    assert superseded.effect_request.resources == (superseded.resource,)
    assert superseded.plan_digest != plan.plan_digest


class _CloseCursor:
    def __init__(self, responses: dict[str, list[tuple[Any, ...]]]) -> None:
        self.responses = responses
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _CloseCursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, statement: str, _parameters: Any) -> None:
        if "select close_job.id" in statement:
            self.rows = self.responses.get("ready", [])
        elif "select project_id,work_item_id,run_id" in statement:
            self.rows = self.responses.get("job", [])
        elif "select session_id,client_id" in statement:
            self.rows = self.responses.get("run", [])
        elif "select auth.actor_id" in statement:
            self.rows = self.responses.get("actor", [])
        elif "receipt_body->>'migration_digest'" in statement:
            self.rows = self.responses.get("hydration", [])
        else:
            raise AssertionError(statement)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


class _CloseConnection:
    def __init__(self, responses: dict[str, list[tuple[Any, ...]]]) -> None:
        self.responses = responses

    def cursor(self) -> _CloseCursor:
        return _CloseCursor(self.responses)


class _Release:
    expected_projection_source_digest = digest("source")

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def assert_release_ready(self, *, expected_source_digest: str) -> None:
        self.calls.append(expected_source_digest)
        if self.fail:
            raise PolicyViolation("release not ready")


def test_projection_next_ready_and_identity_failures() -> None:
    assert (
        close_runtime.ProjectionCloseRuntimeService(
            _CloseConnection({}), IDS[0]
        ).next_ready_job_id()
        is None
    )
    assert (
        close_runtime.ProjectionCloseRuntimeService(
            _CloseConnection({"ready": [(IDS[1],)]}), IDS[0]
        ).next_ready_job_id()
        == IDS[1]
    )

    missing = close_runtime.ProjectionCloseRuntimeService(_CloseConnection({}), IDS[0])
    with pytest.raises(PolicyViolation, match="ready job identity"):
        missing.assert_release_ready(IDS[1])
    with pytest.raises(PolicyViolation, match="source authority"):
        missing._source_actor_id(
            source_authorization_id=IDS[2], work_item_id=IDS[3], plan_id=IDS[4]
        )
    with pytest.raises(PolicyViolation, match="run identity"):
        missing._run_identity(IDS[5])
    with pytest.raises(PolicyViolation, match="hydration"):
        missing._hydration_identity(IDS[1], IDS[2], IDS[3], "session", "client")

    duplicate_actor = close_runtime.ProjectionCloseRuntimeService(
        _CloseConnection({"actor": [(IDS[6],), (IDS[7],)]}), IDS[0]
    )
    with pytest.raises(PolicyViolation, match="source authority"):
        duplicate_actor._source_actor_id(
            source_authorization_id=IDS[2], work_item_id=IDS[3], plan_id=IDS[4]
        )


def test_projection_release_ready_success_active_run_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: dict[str, list[tuple[Any, ...]]] = {
        "job": [(IDS[1], IDS[2], IDS[3])],
        "run": [("session", "codex")],
    }
    service = close_runtime.ProjectionCloseRuntimeService(_CloseConnection(responses), IDS[0])
    release = _Release()
    repository = SimpleNamespace(read_projection_release_snapshot=lambda **_kwargs: release)
    monkeypatch.setattr(close_runtime, "legacy_repository", lambda *_args: repository)
    service.assert_release_ready(IDS[4])
    assert release.calls == [digest("source")]

    no_run = close_runtime.ProjectionCloseRuntimeService(
        _CloseConnection({"job": responses["job"]}), IDS[0]
    )
    with pytest.raises(PolicyViolation, match="active run"):
        no_run.assert_release_ready(IDS[4])
    failing = _Release(fail=True)
    monkeypatch.setattr(
        close_runtime,
        "legacy_repository",
        lambda *_args: SimpleNamespace(read_projection_release_snapshot=lambda **_kwargs: failing),
    )
    with pytest.raises(PolicyViolation, match="not ready"):
        service.assert_release_ready(IDS[4])


def test_projection_identity_success_and_execute_immutable_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = close_runtime.ProjectionCloseRuntimeService(
        _CloseConnection(
            {
                "actor": [(IDS[5],)],
                "run": [("session", "codex")],
                "hydration": [(digest("migration"), digest("context"))],
            }
        ),
        IDS[0],
    )
    assert (
        service._source_actor_id(
            source_authorization_id=IDS[1], work_item_id=IDS[2], plan_id=IDS[3]
        )
        == IDS[5]
    )
    assert service._run_identity(IDS[4]) == ("session", "codex")
    assert service._hydration_identity(IDS[1], IDS[2], IDS[3], "session", "codex") == (
        digest("migration"),
        digest("context"),
    )

    invalid_job = SimpleNamespace(payload={}, required_capabilities=(), step_id="bad")
    with pytest.raises(PolicyViolation, match="immutable"):
        service.execute(SimpleNamespace(job=invalid_job), now=NOW)

    valid_job = SimpleNamespace(
        payload={
            "schema": "zekam-projection-close-job/v1",
            "source_authorization_id": str(IDS[1]),
            "lifecycle_job_id": str(IDS[2]),
            "entry_digest": digest("entry"),
        },
        required_capabilities=("client.lifecycle.projection-close",),
        step_id="projection-aware-close",
        work_item_id=IDS[3],
        plan_id=IDS[4],
        assignment_id=IDS[5],
        run_id=IDS[6],
        project_id=IDS[7],
        resources=(SimpleNamespace(resource="wrong"),),
    )
    monkeypatch.setattr(
        close_runtime.ProjectionCloseRuntimeService,
        "_source_actor_id",
        lambda *_args, **_kwargs: IDS[8],
    )
    with pytest.raises(PolicyViolation, match="resource drift"):
        service.execute(SimpleNamespace(job=valid_job), now=NOW)
