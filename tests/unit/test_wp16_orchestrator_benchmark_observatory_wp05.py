from __future__ import annotations

import datetime as dt
import json
import os
import platform
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from zekam.application import memory_continuity_orchestrator as memory
from zekam.application import model_benchmark_service as benchmark
from zekam.application import observatory
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.hook_runtime import HookEventType
from zekam.domain.model_benchmark import (
    BenchmarkFixture,
    BenchmarkPlan,
    BenchmarkSuite,
    ExecutionEligibility,
    SuiteKind,
    TrialResult,
    TrialStatus,
    VerifierIdentity,
    VerifierVerdict,
)
from zekam.domain.observability import CausalProjection, DerivedGraph, OperationsDashboard
from zekam.domain.security import AuthorizationState
from zekam.infrastructure.clients import codex_macos_0151_lifecycle as lifecycle

NOW = dt.datetime(2026, 9, 5, 12, tzinfo=dt.UTC)


def _load_test_namespace(path: str) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    source = Path(path).read_text(encoding="utf-8")
    exec(compile(source, path, "exec"), namespace)
    return namespace


def test_memory_command_and_hook_defensive_matrix() -> None:
    event_id = str(uuid4())
    payload = {
        "lifecycle": {
            "event_id": event_id,
            "event_type": HookEventType.PRE_COMPACTION.value,
        }
    }
    command = memory.plan_memory_hook(HookEventType.PRE_COMPACTION, payload)
    assert command.command_digest == digest(command.body())
    for changes in (
        {"event_ref": "wrong"},
        {"actions": ()},
        {"grants_authority": True},
    ):
        with pytest.raises((PolicyViolation, ValidationFailed)):
            replace(command, **cast(Any, changes))
    with pytest.raises(ValidationFailed, match="lifecycle"):
        memory.plan_memory_hook(HookEventType.PRE_COMPACTION, {})
    with pytest.raises(PolicyViolation, match="binding"):
        memory.plan_memory_hook(HookEventType.PRE_CLOSE, payload)
    with pytest.raises(ValidationFailed, match="UUID"):
        memory.plan_memory_hook(
            HookEventType.PRE_COMPACTION,
            {"lifecycle": {"event_id": "bad", "event_type": "pre_compaction"}},
        )
    with pytest.raises(PolicyViolation, match="registry"):
        unknown_event = type("UnknownEvent", (), {"value": "unknown"})()
        memory.plan_memory_hook(
            cast(Any, unknown_event),
            {"lifecycle": {"event_id": event_id, "event_type": "unknown"}},
        )


def test_memory_record_result_orchestrator_and_fragment_matrix() -> None:
    ns = _load_test_namespace("tests/unit/test_memory_continuity_orchestrator.py")
    record = ns["_record"]()
    record_changes: tuple[dict[str, object], ...] = (
        {"event_type": "SESSION_START"},
        {"sequence": 0},
        {"session_id": " "},
        {"hook_receipt_count": 0},
        {"event_digest": digest("wrong")},
        {"input_digest": digest("wrong")},
        {"hook_output_digest": digest("wrong")},
        {"occurred_at": NOW.replace(tzinfo=None)},
    )
    for changes in record_changes:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            replace(record, **cast(Any, changes))
    worker_changes: tuple[dict[str, object], ...] = (
        {"source_count": -1},
        {"grants_authority": True},
    )
    for worker_change in worker_changes:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            memory.CompilerWorkerResult(
                memory.CompilerWorkerStatus.IDLE, **cast(Any, worker_change)
            )
    repository = ns["_Repository"]([])
    for batch, stale in (
        (0, dt.timedelta(minutes=1)),
        (129, dt.timedelta(minutes=1)),
        (1, dt.timedelta(0)),
    ):
        with pytest.raises(ValidationFailed):
            memory.MemoryContinuityOrchestrator(
                repository,
                cast(Any, object()),
                ns["_service"](repository).policy,
                batch_limit=batch,
                claim_stale_after=stale,
            )
    service = ns["_service"](repository)
    with pytest.raises(ValidationFailed):
        service.compile_due(now=NOW.replace(tzinfo=None))
    assert service.compile_due(now=NOW).status is memory.CompilerWorkerStatus.IDLE
    for event in (
        HookEventType.ON_FAILURE,
        HookEventType.PRE_COMPACTION,
        HookEventType.ON_STATE_DRIFT,
        HookEventType.POST_CLOSE,
        HookEventType.POST_TASK,
    ):
        changed = ns["_record"](event_type=event.value)
        fragment = service._fragment(changed)
        assert fragment.source_revision == changed.source_revision


def _fixture() -> BenchmarkFixture:
    return BenchmarkFixture(
        case_id="case",
        version=1,
        workload="code",
        modality="text",
        fixture_source="case.json",
        execution_eligibility=ExecutionEligibility.LOCAL_ONLY,
        content_digest=digest("content"),
        expected_schema_digest=digest("schema"),
        tags=(),
    )


def _trial(fixture: BenchmarkFixture, *, approved: bool = False) -> TrialResult:
    return TrialResult(
        fixture.fixture_digest,
        1,
        TrialStatus.PASSED,
        True,
        True,
        True,
        approved,
        1.0,
        1.0,
        1,
        1,
        1,
        0,
        0,
        0.0,
        0.0,
        digest("response"),
        digest("evidence"),
    )


def _plan(
    fixture: BenchmarkFixture, registry_digest: str, *, remote: bool = False
) -> tuple[BenchmarkSuite, BenchmarkPlan]:
    suite = BenchmarkSuite("suite", 1, SuiteKind.GENERAL, (fixture.fixture_digest,))
    return suite, BenchmarkPlan(
        "model",
        suite.suite_digest,
        digest("inventory"),
        digest("policy"),
        registry_digest,
        repetitions=5,
        remote_execution=remote,
    )


def test_benchmark_exact_helpers_gateway_and_prepare_guards() -> None:
    assert benchmark._exact_text({"v": "x"}, "v") == "x"
    assert benchmark._exact_int({"v": 2}, "v") == 2
    assert benchmark._exact_float({"v": 2.5}, "v") == 2.5
    for value in (True, 1, "1", float("nan")):
        with pytest.raises(ValidationFailed):
            benchmark._exact_float({"v": value}, "v")
    gateway = object.__new__(benchmark.RuntimeBenchmarkClaimGateway)
    object.__setattr__(gateway, "_claims", {})
    with pytest.raises(PolicyViolation, match="identity"):
        gateway.complete_tested(claim_id=uuid4(), result=cast(Any, object()))
    with pytest.raises(PolicyViolation, match="identity"):
        gateway.complete_verifier(claim_id=uuid4(), verdict=cast(Any, object()))
    with pytest.raises(PolicyViolation, match="identity"):
        gateway.retain_failure(
            plan_id=uuid4(),
            claim_id=uuid4(),
            fixture_digest=digest("f"),
            repetition=1,
            phase="tested",
            category="timeout",
        )


def test_benchmark_execution_service_route_and_aggregate_guards() -> None:
    fixture = _fixture()
    registry = SimpleNamespace(fixtures=(fixture,), registry_digest=digest("registry"))
    repository = SimpleNamespace(
        ensure_plan=lambda **kwargs: (uuid4(), True),
        trial_receipt_matches=lambda **kwargs: False,
        list_trials=lambda plan_id: (),
    )
    service = benchmark.BenchmarkExecutionService(cast(Any, repository), cast(Any, registry))
    suite = BenchmarkSuite("suite", 1, SuiteKind.GENERAL, (fixture.fixture_digest,))
    plan = BenchmarkPlan(
        model_id="model",
        suite_digest=suite.suite_digest,
        inventory_digest=digest("inventory"),
        policy_digest=digest("policy"),
        fixture_registry_digest=registry.registry_digest,
        repetitions=5,
        remote_execution=False,
    )
    with pytest.raises(PolicyViolation, match="suite"):
        service.prepare(suite, replace(plan, suite_digest=digest("wrong")))
    with pytest.raises(PolicyViolation, match="registry"):
        service.prepare(suite, replace(plan, fixture_registry_digest=digest("wrong")))
    result = cast(Any, SimpleNamespace())
    with pytest.raises(PolicyViolation, match="receipt"):
        service.record_trial(
            uuid4(),
            tested_claim_id=uuid4(),
            verifier_claim_id=uuid4(),
            verdict=result,
            result=result,
        )
    verifier = VerifierIdentity("verifier", "local", digest("prov"))
    with pytest.raises(PolicyViolation, match="repetition"):
        service.aggregate(
            uuid4(), suite=suite, plan=plan, tested_model_id="model", verifier=verifier
        )


def test_benchmark_runtime_gateway_known_claim_paths() -> None:
    fixture = _fixture()
    _suite, plan = _plan(fixture, digest("registry"))
    claim = SimpleNamespace(id=uuid4())
    calls: list[str] = []
    host = SimpleNamespace(
        claim_effect=lambda *_args, **_kwargs: claim,
        record_success=lambda *_args, **_kwargs: calls.append("success"),
        record_failure=lambda *_args, **_kwargs: calls.append("failure"),
    )
    authorization = SimpleNamespace(
        state=AuthorizationState.CONSUMED,
        plan_digest=plan.plan_digest,
        authorization_digest=digest("authorization"),
        id=uuid4(),
    )
    gateway = benchmark.RuntimeBenchmarkClaimGateway(
        host=cast(Any, host),
        work=cast(Any, object()),
        authorization=cast(Any, authorization),
        adapter_digest=digest("adapter"),
    )
    wrong = replace(plan, policy_digest=digest("other"))
    with pytest.raises(PolicyViolation, match="plan digest"):
        gateway.claim_tested(plan=wrong, fixture=fixture, repetition=1)
    tested = gateway.claim_tested(plan=plan, fixture=fixture, repetition=1)
    result = _trial(fixture)
    gateway.complete_tested(claim_id=tested, result=result)
    verifier = VerifierIdentity("verifier", "local", digest("provenance"))
    verifier_claim = gateway.claim_verifier(
        plan=plan, fixture=fixture, result=result, verifier=verifier
    )
    verdict = VerifierVerdict(
        "model", "verifier", "local", result.response_digest, True, digest("verdict")
    )
    gateway.complete_verifier(claim_id=verifier_claim, verdict=verdict)
    assert gateway.retain_failure(
        plan_id=uuid4(),
        claim_id=tested,
        fixture_digest=fixture.fixture_digest,
        repetition=1,
        phase="tested",
        category="not-a-category",
    ).startswith("sha256:")
    assert calls == ["success", "success", "failure"]


def test_benchmark_process_adapters_reject_response_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture()
    _suite, plan = _plan(fixture, digest("registry"))
    oracle = SimpleNamespace(load=lambda _fixture: {"fixture": True})
    adapter = benchmark.LocalProcessBenchmarkAdapter(
        "model", (str(tmp_path / "tested"),), cast(Any, oracle), lambda *_args: None
    )
    with pytest.raises(PolicyViolation, match="route"):
        adapter.invoke(plan=replace(plan, model_id="other"), fixture=fixture, repetition=1)
    bad = benchmark._JsonProcessResult({"schema": "wrong"}, b"{}")
    monkeypatch.setattr(benchmark, "_run_json_process", lambda *_args, **_kwargs: bad)
    with pytest.raises(ValidationFailed, match="schema"):
        adapter.invoke(plan=plan, fixture=fixture, repetition=1)
    tested_body: dict[str, object] = {
        "schema": "zekam-benchmark-tested-result/v1",
        "model_id": "wrong",
        "status": "passed",
        "parse_ok": True,
        "format_ok": True,
        "evidence_ok": True,
        "quality": 1.0,
        "reliability": 1.0,
        "latency_ms": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "actual_cost": 0.0,
        "tool_correctness": 1.0,
        "recovery": 1.0,
        "response": {"ok": True},
    }
    monkeypatch.setattr(
        benchmark,
        "_run_json_process",
        lambda *_args, **_kwargs: benchmark._JsonProcessResult(tested_body, b"{}"),
    )
    with pytest.raises(PolicyViolation, match="identity"):
        adapter.invoke(plan=plan, fixture=fixture, repetition=1)
    tested_body["model_id"] = "model"
    sink = SimpleNamespace(
        store_artifact=lambda kind, _data: (
            digest("wrong") if kind == "normalized" else digest("raw")
        ),
        bind_artifacts=lambda **_kwargs: None,
    )
    object.__setattr__(adapter, "artifact_sink", cast(Any, sink))
    with pytest.raises(PolicyViolation, match="normalized"):
        adapter.invoke(plan=plan, fixture=fixture, repetition=1)

    identity = VerifierIdentity("verifier", "local", digest("provenance"))
    verifier = benchmark.LocalProcessBenchmarkVerifier(
        identity, (str(tmp_path / "verifier"),), lambda *_args: None
    )
    result = _trial(fixture)
    monkeypatch.setattr(benchmark, "_run_json_process", lambda *_args, **_kwargs: bad)
    with pytest.raises(ValidationFailed, match="schema"):
        verifier.verify(plan=plan, fixture=fixture, result=result)
    verifier_body: dict[str, object] = {
        "schema": "zekam-benchmark-verifier-result/v1",
        "tested_model_id": "wrong",
        "verifier_model_id": "verifier",
        "tested_response_digest": result.response_digest,
        "approved": True,
        "evidence": {"ok": True},
    }
    monkeypatch.setattr(
        benchmark,
        "_run_json_process",
        lambda *_args, **_kwargs: benchmark._JsonProcessResult(verifier_body, b"{}"),
    )
    with pytest.raises(PolicyViolation, match="binding"):
        verifier.verify(plan=plan, fixture=fixture, result=result)
    verifier_body["tested_model_id"] = "model"
    object.__setattr__(
        verifier,
        "artifact_sink",
        cast(Any, SimpleNamespace(store_artifact=lambda *_args: digest("wrong"))),
    )
    with pytest.raises(PolicyViolation, match="artifact sink"):
        verifier.verify(plan=plan, fixture=fixture, result=result)


def test_benchmark_execute_route_guard_matrix() -> None:
    fixture = replace(_fixture(), execution_eligibility=ExecutionEligibility.REMOTE_ALLOWED)
    registry_digest = digest("registry")
    registry = SimpleNamespace(fixtures=(fixture,), registry_digest=registry_digest)
    service = benchmark.BenchmarkExecutionService(cast(Any, SimpleNamespace()), cast(Any, registry))
    suite, local_plan = _plan(fixture, registry_digest)
    remote_plan = replace(local_plan, remote_execution=True)
    verifier_id = VerifierIdentity("verifier", "local", digest("provenance"))

    def invoke(
        plan: BenchmarkPlan,
        mode: str,
        model: str,
        verifier_model: str,
        verifier_mode: str,
        gate: object | None = None,
    ) -> None:
        adapter = SimpleNamespace(execution_mode=mode, model_id=model)
        verifier = SimpleNamespace(
            execution_mode=verifier_mode,
            verifier=replace(verifier_id, model_id=verifier_model),
        )
        service.execute(
            suite=suite,
            plan=plan,
            adapter=cast(Any, adapter),
            verifier_adapter=cast(Any, verifier),
            claims=cast(Any, object()),
            outbound_gate=cast(Any, gate),
        )

    cases = (
        (local_plan, "remote", "model", "verifier", "remote", None, "execution mode"),
        (remote_plan, "remote", "model", "verifier", "remote", None, "authorization"),
        (local_plan, "invalid", "model", "verifier", "invalid", None, "mode gecersiz"),
        (local_plan, "local", "wrong", "verifier", "local", None, "model route"),
        (local_plan, "local", "model", "model", "local", None, "kendi verifier"),
        (local_plan, "local", "model", "verifier", "remote", None, "execution mode"),
    )
    for args in cases:
        with pytest.raises(PolicyViolation, match=args[-1]):
            invoke(*args[:-1])


def test_benchmark_execute_retains_verdict_drift_and_invalid_aggregate() -> None:
    fixture = _fixture()
    registry_digest = digest("registry")
    suite, plan = _plan(fixture, registry_digest)
    result = _trial(fixture)
    identity = VerifierIdentity("verifier", "local", digest("provenance"))
    verdict = VerifierVerdict(
        "model", "other-verifier", "local", result.response_digest, True, digest("verdict")
    )
    retained: list[str] = []
    repository = SimpleNamespace(
        ensure_plan=lambda **_kwargs: (uuid4(), True),
        list_trials=lambda _plan_id: (),
        trial_receipt_matches=lambda **_kwargs: True,
    )
    claims = SimpleNamespace(
        claim_tested=lambda **_kwargs: uuid4(),
        complete_tested=lambda **_kwargs: None,
        claim_verifier=lambda **_kwargs: uuid4(),
        complete_verifier=lambda **_kwargs: None,
        retain_failure=lambda **kwargs: retained.append(kwargs["phase"]),
    )
    adapter = SimpleNamespace(
        execution_mode="local", model_id="model", invoke=lambda **_kwargs: result
    )
    verifier_adapter = SimpleNamespace(
        execution_mode="local", verifier=identity, verify=lambda **_kwargs: verdict
    )
    service = benchmark.BenchmarkExecutionService(
        cast(Any, repository),
        cast(Any, SimpleNamespace(fixtures=(fixture,), registry_digest=registry_digest)),
    )
    with pytest.raises(PolicyViolation, match="failure retained"):
        service.execute(
            suite=suite,
            plan=plan,
            adapter=cast(Any, adapter),
            verifier_adapter=cast(Any, verifier_adapter),
            claims=cast(Any, claims),
        )
    assert retained == ["verifier"]
    invalid = replace(result, verifier_approved=False)
    repository.list_trials = lambda _plan_id: (invalid,) * 5
    with pytest.raises(PolicyViolation):
        service.aggregate(
            uuid4(), plan=plan, suite=suite, tested_model_id="model", verifier=identity
        )


def test_observatory_value_helpers_and_snapshot_guards(tmp_path: Path) -> None:
    tiles = observatory.EmptyRuntimeProjectionReader().read().tiles
    with pytest.raises(ValueError, match="missing tiles"):
        observatory.RuntimeProjection(NOW, tiles[:-1])
    snapshot = observatory.ObservatorySnapshot(
        NOW,
        OperationsDashboard(NOW, tiles),
        DerivedGraph((), (), digest("graph")),
        (),
        (),
        (),
        CausalProjection(),
        observatory.CanonicalRuntimeProjection(),
        False,
        "test",
    )
    with pytest.raises(ValueError, match="read-only"):
        replace(snapshot, read_only=False)
    with pytest.raises(ValueError, match="authority"):
        replace(snapshot, grants_authority=True)
    with pytest.raises(ValueError):
        observatory.ObservatoryService(tmp_path, repository_refresh_seconds=0)
    assert observatory._safe_tool_name(None) is None
    assert observatory._safe_tool_name("bad/path") is None
    assert observatory._safe_tool_name("token:bad") is None
    assert observatory._safe_tool_name("tool.ok") == "tool.ok"
    assert observatory._safe_current_action("x", "tool") == "tool"
    for state, expected in (
        ("running", "executing"),
        ("idle", "waiting"),
        ("queued", "planning"),
        ("x", "unknown"),
    ):
        assert observatory._safe_current_action(state, None) == expected
    assert observatory._parse_timestamp("bad").tzinfo is not None
    assert observatory._parse_timestamp("2026-09-05T12:00:00Z") == NOW
    assert observatory._iso(None) is None
    rendered = observatory._iso(NOW.replace(tzinfo=None))
    assert rendered is not None and rendered.endswith("Z")


def test_observatory_files_links_labels_and_cache_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "core"
    root.mkdir()
    source = root / "source.md"
    target = root / "target.md"
    source.write_text(
        "# Source\n[target](target.md) [bad](https://x.md) [bin](x.bin)",
        encoding="utf-8",
    )
    target.write_text("# Target", encoding="utf-8")
    assert observatory._markdown_targets(source, source.read_text(), root) == (target,)
    assert observatory.sanitize_observatory_label("", fallback="token=bad") == "Kayıt"
    assert not observatory._is_safe_markdown(root / "missing.md", root)
    secret = root / "token=bad.md"
    secret.write_text("x", encoding="utf-8")
    assert not observatory._is_safe_markdown(secret, root)
    nested = root / "sessions"
    nested.mkdir()
    (nested / "a.jsonl").write_text("{}", encoding="utf-8")
    (nested / "skip.txt").write_text("x", encoding="utf-8")
    assert observatory._bounded_session_files(root, max_directories=10, max_candidates=1)
    service = observatory.ObservatoryService(root)
    first = service._repository_projection()
    monkeypatch.setattr(
        observatory, "scan_repository", lambda path: (_ for _ in ()).throw(OSError())
    )
    service._repository_cache_at = 0
    assert service._repository_projection() == first


def test_lifecycle_strict_json_bounds_and_scalar_guards() -> None:
    with pytest.raises(ValidationFailed, match="member"):
        lifecycle._strict_document(json.dumps({str(i): i for i in range(65)}).encode())
    with pytest.raises(ValidationFailed, match="array"):
        lifecycle._strict_document(json.dumps({"a": list(range(129))}).encode())
    with pytest.raises(ValidationFailed, match="depth"):
        value: object = 0
        for _ in range(13):
            value = {"a": value}
        lifecycle._strict_document(json.dumps(value).encode())
    for value in (1, "", "bad\x7f"):
        with pytest.raises(ValidationFailed):
            lifecycle._text(value, "field")


def test_lifecycle_parser_missing_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    with pytest.raises(ValidationFailed, match="exact event key"):
        lifecycle.parse_codex_macos_0151(
            json.dumps(
                {
                    "session_id": "session",
                    "cwd": str(tmp_path),
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                }
            ).encode(),
            expected_root=tmp_path,
        )


def test_lifecycle_unsealed_owner_deadline_and_artifact_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = object.__new__(lifecycle.TrustedCodex0151ProcessManager)
    with pytest.raises(PolicyViolation, match="unsealed"):
        manager.capture_process(cast(Any, object()))
    path = tmp_path / "artifact"
    path.write_bytes(b"a")
    calls = 0
    original = Path.stat

    def changing(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal calls
        result = original(self, *args, **kwargs)
        calls += 1
        if calls > 1:
            values = list(result)
            values[1] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", changing)
    with pytest.raises(PolicyViolation, match="changed"):
        lifecycle._raw_file_digest(path)
