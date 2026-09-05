from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import zekam.application.chaos_command_composition as chaos
from zekam.application.execution import AdmissionDecision, AdmissionState, check_admission
from zekam.domain.canonical import digest
from zekam.domain.chaos_campaign import (
    ChaosAuditEvent,
    ChaosCampaignPlan,
    ChaosCampaignPolicy,
    ChaosOperatorRecord,
    ChaosScenario,
    FaultInjectionAuthorization,
    FaultInjectionReceipt,
    FaultPoint,
    RuntimeSafetySnapshot,
    default_chaos_scenarios,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.runtime import Job, JobKind

pytestmark = pytest.mark.unit
NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
REALM_ID = UUID("018f0000-0000-7000-8000-000000000001")
PROJECT_ID = UUID("018f0000-0000-7000-8000-000000000002")


def _job(kind: JobKind = JobKind.READ_ONLY, *, max_attempts: int = 3) -> Job:
    return Job.create(
        realm_id=REALM_ID,
        project_id=PROJECT_ID,
        kind=kind,
        idempotency_key=f"job:{kind}",
        max_attempts=max_attempts,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("changes", "kind", "decision", "reason"),
    [
        ({"maintenance": True}, JobKind.READ_ONLY, AdmissionDecision.DEFER, "maintenance"),
        ({"draining": True}, JobKind.READ_ONLY, AdmissionDecision.DEFER, "draining"),
        ({"backup_lock": True}, JobKind.READ_ONLY, AdmissionDecision.DEFER, "backup-lock"),
        (
            {"migration_pending": True},
            JobKind.READ_ONLY,
            AdmissionDecision.DEFER,
            "migration-pending",
        ),
        (
            {"running_jobs": 8, "project_concurrency_limit": 8},
            JobKind.READ_ONLY,
            AdmissionDecision.DEFER,
            "concurrency",
        ),
        (
            {"queue_backlog": 11, "max_queue_backlog": 10},
            JobKind.READ_ONLY,
            AdmissionDecision.DEFER,
            "queue-backlog",
        ),
        (
            {"quota_available": False},
            JobKind.PROVIDER_CALL,
            AdmissionDecision.DEFER,
            "quota",
        ),
        (
            {"sandbox_available": False},
            JobKind.MUTATION,
            AdmissionDecision.DEFER,
            "sandbox",
        ),
        (
            {"verifier_available": False},
            JobKind.VERIFICATION,
            AdmissionDecision.DEFER,
            "verifier",
        ),
        (
            {"remaining_cost_micros": 0},
            JobKind.READ_ONLY,
            AdmissionDecision.DEFER,
            "budget",
        ),
        (
            {"remaining_tokens": 0},
            JobKind.READ_ONLY,
            AdmissionDecision.DEFER,
            "budget",
        ),
    ],
)
def test_admission_fail_closed_matrix(
    changes: dict[str, object],
    kind: JobKind,
    decision: AdmissionDecision,
    reason: str,
) -> None:
    result = check_admission(_job(kind), replace(AdmissionState(), **changes))  # type: ignore[arg-type]
    assert result.decision is decision
    assert reason in result.reason
    assert result.admitted is False
    assert result.as_dict() == {"decision": decision.value, "reason": result.reason}


def test_admission_accepts_healthy_job_and_rejects_exhausted_job() -> None:
    admitted = check_admission(_job(), AdmissionState())
    assert admitted.admitted is True
    assert admitted.as_dict() == {"decision": "admit", "reason": "admitted"}
    exhausted = replace(_job(max_attempts=1), attempt_count=1)
    result = check_admission(exhausted, AdmissionState())
    assert result.decision is AdmissionDecision.REJECT
    assert result.reason == "max-attempts-reached"


def test_chaos_time_and_string_boundaries_are_strict() -> None:
    moment_values: tuple[object, ...] = (None, 1, [])
    for moment_value in moment_values:
        with pytest.raises(ValidationFailed, match="string olmali"):
            chaos._moment(moment_value)
    with pytest.raises(ValidationFailed, match="gecersiz"):
        chaos._moment("not-a-time")
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        chaos._moment("2026-09-04T12:00:00")
    assert chaos._moment("2026-09-04T12:00:00Z") == NOW
    strings_values: tuple[object, ...] = (None, "x", ["ok", 1])
    for strings_value in strings_values:
        with pytest.raises(ValidationFailed, match="string listesi"):
            chaos._strings(strings_value, "items")
    assert chaos._strings(["a", "b"], "items") == ("a", "b")


@pytest.mark.parametrize(
    ("argv", "timeout"),
    [
        ((), 10),
        (("",), 10),
        (("driver",), 0),
        (("driver",), 601),
    ],
)
def test_chaos_driver_constructor_rejects_unsafe_bounds(
    argv: tuple[str, ...], timeout: int
) -> None:
    with pytest.raises(ValidationFailed):
        chaos.ChaosCommandDriver(argv, timeout)


def test_chaos_driver_rejects_operation_process_and_json_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = chaos.ChaosCommandDriver(("driver",), 1)
    with pytest.raises(PolicyViolation, match="allowlist"):
        driver.call("unknown", {})

    outcomes = iter(
        (
            subprocess.CompletedProcess(["driver"], 1, b"", b"failed"),
            subprocess.CompletedProcess(["driver"], 0, b"not-json", b""),
            subprocess.CompletedProcess(["driver"], 0, b"[]", b""),
            subprocess.CompletedProcess(["driver"], 0, b'{"ok":true}', b""),
        )
    )

    def fake_run(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        return next(outcomes)

    monkeypatch.setattr("zekam.application.chaos_command_composition.subprocess.run", fake_run)
    with pytest.raises(ValidationFailed, match="basarisiz"):
        driver.call("snapshot", {})
    with pytest.raises(ValidationFailed, match="canonical JSON"):
        driver.call("snapshot", {})
    with pytest.raises(ValidationFailed, match="JSON object"):
        driver.call("snapshot", {})
    assert driver.call("snapshot", {}) == {"ok": True}


def test_chaos_authorization_verification_requires_exact_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = FaultInjectionAuthorization(
        realm_id="realm",
        scenario_digest=digest("scenario"),
        target="isolated-target",
        actor_identity="operator",
        valid_from=NOW,
        valid_until=NOW + dt.timedelta(minutes=1),
        repetition=1,
        authorization_record_digest=digest("authorization"),
    )
    exact = {"current": True, "authorization_digest": authorization.authorization_digest}
    responses = iter((exact, exact | {"extra": True}))

    def fake_call(
        self: chaos.ChaosCommandDriver, operation: str, body: dict[str, object]
    ) -> dict[str, object]:
        del self
        assert operation == "authorize-verify"
        assert body == {"authorization": authorization.as_dict()}
        return next(responses)

    monkeypatch.setattr(chaos.ChaosCommandDriver, "call", fake_call)
    provider = chaos.CommandAuthorizationProvider(chaos.ChaosCommandDriver(("driver",)), "realm")
    assert provider.verify_current(authorization)
    assert not provider.verify_current(authorization)


def test_chaos_composition_rejects_bad_config_before_any_process(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    cases = (
        b"not-json",
        b"[]",
        json.dumps({"driver_argv": [], "verifier_argv": ["verify"]}).encode(),
        json.dumps(
            {
                "driver_argv": ["same"],
                "verifier_argv": ["same"],
                "artifact_root": str(tmp_path / "artifacts"),
            }
        ).encode(),
        json.dumps(
            {
                "driver_argv": ["inject"],
                "verifier_argv": ["verify"],
                "artifact_root": str(source / "inside"),
            }
        ).encode(),
    )
    for index, body in enumerate(cases):
        config = tmp_path / f"case-{index}.json"
        config.write_bytes(body)
        with pytest.raises((ValidationFailed, PolicyViolation)):
            chaos.compose_command_chaos_handler(config, source_root=source)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"schedule_interval": "daily"},
        {"repetitions": 0},
        {"repetitions": 6},
        {"policy_version": ""},
    ],
)
def test_chaos_policy_rejects_noncanonical_schedule_and_bounds(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        ChaosCampaignPolicy(**kwargs)  # type: ignore[arg-type]


def test_chaos_scenario_and_plan_bind_exact_canonical_matrix() -> None:
    for values in (
        ("", "phase", "action"),
        ("target", "", "action"),
        ("target", "phase", ""),
    ):
        with pytest.raises(ValidationFailed, match="target, phase"):
            ChaosScenario(next(iter(FaultPoint)), *values)
    scenarios = default_chaos_scenarios()
    policy = ChaosCampaignPolicy()
    suite_digest = digest([item.as_dict() for item in scenarios])
    plan = ChaosCampaignPlan("campaign", "revision", suite_digest, policy, scenarios)
    assert plan.plan_digest == digest(plan.as_dict())
    with pytest.raises(ValidationFailed, match="identity"):
        replace(plan, campaign_id="")
    with pytest.raises(ValidationFailed, match="canonical sirada"):
        replace(plan, scenarios=tuple(reversed(scenarios)))
    with pytest.raises(ValidationFailed, match="suite digest"):
        replace(plan, suite_digest=digest("wrong-suite"))


def test_runtime_snapshot_rejects_empty_unsorted_and_duplicate_bindings() -> None:
    valid = RuntimeSafetySnapshot("realm", (), (), (), ("realm",), digest("audit"))
    assert valid.snapshot_digest == digest(valid.as_dict())
    with pytest.raises(ValidationFailed, match="realm ister"):
        replace(valid, realm_id="")
    with pytest.raises(ValidationFailed, match="sirali"):
        replace(valid, authority_bindings=("b", "a"))
    with pytest.raises(ValidationFailed, match="tekil"):
        replace(valid, non_terminal_state_refs=("same", "same"))
    repeated_effect = replace(valid, irreversible_effect_occurrences=("same", "same"))
    assert repeated_effect.irreversible_effect_occurrences == ("same", "same")


def test_chaos_audit_authorization_receipt_and_operator_guards() -> None:
    event = ChaosAuditEvent("realm", 1, "fault", "record", digest("previous"))
    assert event.event_digest == digest(event.as_dict())
    with pytest.raises(ValidationFailed, match="identity"):
        replace(event, sequence=0)

    authorization = FaultInjectionAuthorization(
        "realm",
        digest("scenario"),
        "target",
        "operator",
        NOW,
        NOW + dt.timedelta(minutes=1),
        1,
        digest("record"),
    )
    with pytest.raises(ValidationFailed, match="realm, target"):
        replace(authorization, actor_identity="")
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        replace(authorization, valid_from=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationFailed, match="sure/repetition"):
        replace(authorization, valid_until=NOW)

    receipt = FaultInjectionReceipt(
        digest("scenario"),
        1,
        "injector",
        authorization.authorization_digest,
        digest("before"),
        digest("after"),
        NOW,
        NOW,
    )
    assert receipt.receipt_digest == digest(receipt.as_dict())
    with pytest.raises(ValidationFailed, match="repetition"):
        replace(receipt, repetition=0)
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        replace(receipt, completed_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationFailed, match="once"):
        replace(receipt, completed_at=NOW - dt.timedelta(seconds=1))

    operator = ChaosOperatorRecord("realm", digest("scenario"), "state", "recover", digest("audit"))
    assert operator.record_digest == digest(operator.as_dict())
    with pytest.raises(ValidationFailed, match="realm/state/action"):
        replace(operator, next_safe_action="")
