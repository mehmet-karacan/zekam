from __future__ import annotations

import datetime as dt
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.model_capability_benchmark import (
    CapabilityCheckpointReceipt,
    CapabilityCohortRunner,
    CapabilityFixture,
    CapabilityResponse,
    CapabilityToolReceipt,
    CapabilityVerifier,
    capability_acceptance_evidence_digest,
    load_capability_registry,
)
from zekam.application.model_capability_live import (
    EMPTY_CONTINUITY_STATE,
    MAX_ARTIFACT_CHARS,
    MAX_EVIDENCE_STRING_CHARS,
    REQUEST_DERIVATION_ALGORITHM,
    REQUEST_TEMPLATE_SCHEMA,
    TURN_SCHEMA,
    CapabilityEpisodeClassification,
    CapabilityLiveTurnResult,
    CapabilityTurnFailureCode,
    CapabilityTurnValidationFailed,
    _parse_turn,
    capability_derivation_material,
    classify_capability_episode,
    derive_capability_request_body,
    derive_capability_slot,
    execute_capability_episode,
    prepare_capability_live_manifest,
)
from zekam.application.model_registry import load_inventory
from zekam.application.opencode_benchmark_campaign import (
    discover_campaign,
    prepare_campaign_manifest,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_capability_benchmark import (
    CapabilityCohortPlan,
    CapabilityEpisodeResult,
    CapabilityEpisodeStatus,
    aggregate_capability_episodes,
)
from zekam.domain.model_inventory import Modality

ROOT = Path(__file__).resolve().parents[2]


def _loaded() -> tuple[Any, Any, dict[str, CapabilityFixture]]:
    return load_capability_registry(
        ROOT / "config" / "model_capability_benchmark.yaml",
        repository_root=ROOT,
    )


def _plan(model_ids: tuple[str, ...] = ("model-a", "model-b")) -> CapabilityCohortPlan:
    registry, profile, _ = _loaded()
    return CapabilityCohortPlan(
        source_campaign_id=uuid4(),
        source_revision="revision-1",
        inventory_digest=digest("inventory"),
        policy_digest=digest("policy"),
        verifier_provenance_digest=digest("campaign-verifier"),
        model_ids=model_ids,
        registry=registry,
        execution_profile=profile,
        max_parallelism=len(model_ids),
    )


class FakeAdapter:
    adapter_identity = "fake-capability-adapter"

    def __init__(self) -> None:
        self.starts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def execute(
        self,
        *,
        model_id: str,
        task: Any,
        fixture: Any,
        profile: Any,
        turn_index: int,
        prior_response_digest: str | None,
        cancellation: threading.Event,
    ) -> CapabilityResponse:
        del prior_response_digest
        assert not cancellation.is_set()
        with self._lock:
            self.starts.setdefault(task.task_id, []).append(time.monotonic())
        time.sleep(0.02)
        payload = fixture.payload
        response = CapabilityResponse(
            payload={
                "status": "completed",
                "markers": list(payload["expected_markers"]),
                "artifact_digest": digest((model_id, task.task_digest, turn_index, "artifact")),
            },
            duration_ms=20,
            input_tokens=100,
            output_tokens=200,
            provider_latency_ms=10,
            checkpoint_receipts=tuple(
                CapabilityCheckpointReceipt(
                    name=name,
                    elapsed_ms=(turn_index - 1) * 20 + index * 5,
                    artifact_digest=digest((model_id, task.task_digest, turn_index, name)),
                    acceptance_passed=index,
                    acceptance_total=len(payload["required_checkpoints"]),
                )
                for index, name in enumerate(payload["required_checkpoints"], start=1)
            ),
            tool_receipts=(
                CapabilityToolReceipt(
                    "read", digest((model_id, task.task_digest, turn_index, "read"))
                ),
                CapabilityToolReceipt(
                    "test", digest((model_id, task.task_digest, turn_index, "test"))
                ),
            ),
            self_correction_count=1,
            hidden_acceptance_passed=4,
            hidden_acceptance_total=4,
            regression_count=0,
            context_retention_ratio=1.0,
            unsafe=False,
            acceptance_evidence_digest=digest("pending-acceptance"),
        )
        return replace(
            response,
            acceptance_evidence_digest=capability_acceptance_evidence_digest(
                task, response, profile.evaluator_provenance_digest
            ),
        )


def _verifier() -> CapabilityVerifier:
    return CapabilityVerifier(
        model_id="independent-verifier",
        execution_identity="separate-execution-slot",
        provenance_digest=digest("independent-capability-verifier-v1"),
    )


def test_registry_and_exact_plan_budget() -> None:
    plan = _plan()
    assert len(plan.registry.tasks) == 3
    assert plan.provider_call_budget == 48
    assert plan.maximum_wall_seconds == 900
    assert plan.execution_profile.wall_budget_seconds == 300
    assert plan.execution_profile.max_retries == 0

    seven_model_plan = _plan(tuple(f"model-{index}" for index in range(7)))
    assert seven_model_plan.provider_call_budget == 168
    assert seven_model_plan.max_parallelism == 7


def test_request_derivation_has_stable_python_sql_golden_vector() -> None:
    template = {
        "schema": REQUEST_TEMPLATE_SCHEMA,
        "model": "model/test",
        "system": "system",
        "prompt_prefix": "prefix\n",
        "max_tokens": 17,
    }
    state = {
        "facts": ["alpha", "gamma"],
        "open_questions": ["beta?"],
        "risks": [],
        "next_action": "verify",
    }
    payload = derive_capability_request_body(template, state)
    assert digest(template) == (
        "sha256:7cc1613f26ad1aa39ee75ae98680a4927ae19131f1d78efa349228b21c95502c"
    )
    assert digest(state) == (
        "sha256:95908edc8b10ee95b025cda83d35f49fd8e8986caddff5b1c325d42f87beb3ba"
    )
    assert digest(payload) == (
        "sha256:09221c94678df030360a65799081883ef9b14549c97b4cfa7b3c55cffb9aa4f8"
    )
    assert REQUEST_DERIVATION_ALGORITHM == "zekam-capability-continuity-derive/v4"
    assert (
        digest(
            {
                "schema": "zekam-capability-request-derivation/v1",
                "algorithm": REQUEST_DERIVATION_ALGORITHM,
                "template_digest": digest(template),
                "continuity_state_digest": digest(state),
                "request_body_digest": digest(payload),
            }
        )
        == "sha256:905c0875d749d492362d3a4c9037905188abeab8860aeba9d80d48e334a6b5a3"
    )
    assert set(payload) == {"model", "messages", "temperature", "max_tokens"}


def test_public_fixture_text_does_not_disclose_hidden_answer_concepts() -> None:
    _, _, fixtures = _loaded()
    for fixture in fixtures.values():
        public_text = f"{fixture.payload['brief']}\n{fixture.payload['scenario']}".casefold()
        hidden_terms = {
            str(term).casefold()
            for check in fixture.payload["hidden_acceptance_checks"]
            for term in check["any_of"]
        }
        assert not hidden_terms.intersection(public_text.splitlines())
        assert not any(term in public_text for term in hidden_terms)


def test_live_manifest_prepares_exact_static_168_slots() -> None:
    plan = _plan(tuple(f"model-{index}" for index in range(7)))
    _, _, fixtures = _loaded()
    # The real campaign builder/inventory mapping is exercised in campaign E2E.
    # Here a production seven-model plan is covered by the CLI integration tests;
    # arbitrary fake ids must fail closed instead of producing partial authority.
    discovery = discover_campaign(verifier_provenance_digest=digest("test-verifier"))
    campaign = prepare_campaign_manifest(discovery)
    inventory = load_inventory()
    eligible = tuple(
        target.canonical_model_id
        for target in discovery.targets
        if target.excluded_reason is None
        and (record := inventory.by_id(target.canonical_model_id)) is not None
        and record.invocation_modality in {Modality.CHAT, Modality.CODE, Modality.COMPLETION}
    )[:7]
    assert len(eligible) == 7
    prepared_plan = replace(plan, model_ids=eligible)
    prepared = prepare_capability_live_manifest(prepared_plan, fixtures, campaign)
    assert len(prepared.slots) == 168
    assert len({slot.template_digest for slot in prepared.slots}) == 168
    assert len({slot.derivation_digest for slot in prepared.slots}) == 168
    for candidate_task in prepared_plan.registry.tasks:
        prompts = "\n".join(
            str(slot.prepared.call.payload)
            for slot in prepared.slots
            if slot.task_digest == candidate_task.task_digest
        ).casefold()
        hidden_terms = {
            str(term).casefold()
            for check in fixtures[candidate_task.task_digest].payload["hidden_acceptance_checks"]
            for term in check["any_of"]
        }
        assert not any(term in prompts for term in hidden_terms)
    for model_id in eligible:
        for task in prepared_plan.registry.tasks:
            payloads = [
                slot.prepared.call.payload
                for slot in prepared.slots
                if slot.model_id == model_id and slot.task_digest == task.task_digest
            ]
            assert sum(int(payload["max_tokens"]) for payload in payloads) == task.max_output_tokens
            assert all("max_completion_tokens" not in payload for payload in payloads)

    model_id = eligible[0]
    task = prepared_plan.registry.tasks[0]
    task_payloads = [
        str(slot.prepared.call.payload)
        for slot in prepared.slots
        if slot.task_digest == task.task_digest
    ]
    assert "hidden_acceptance_checks" not in "\n".join(task_payloads)
    assert "Kanit varsa ilgili etiketleri kullan" not in "\n".join(task_payloads)
    leaked = {
        str(alternative).casefold()
        for check in fixtures[task.task_digest].payload["hidden_acceptance_checks"]
        for alternative in check["any_of"]
    }
    assert not any(term in "\n".join(task_payloads).casefold() for term in leaked)
    episode_slots = tuple(
        slot
        for slot in prepared.slots
        if slot.model_id == model_id and slot.task_digest == task.task_digest
    )

    parser_document = {
        "schema": TURN_SCHEMA,
        "phase": episode_slots[0].phase,
        "progress": 10,
        "checkpoint": task.required_checkpoints[0],
        "evidence": ["bounded observation"],
        "revision": {"changed": False, "summary": ""},
        "continuity_state": dict(EMPTY_CONTINUITY_STATE),
        "artifact": "x" * MAX_ARTIFACT_CHARS,
    }
    bare_text = json.dumps(parser_document)
    bare, _ = _parse_turn(bare_text, episode_slots[0])
    fenced, _ = _parse_turn(f"  ```json\n{bare_text}\n```  ", episode_slots[0])
    assert bare == fenced
    wrong_checkpoint = {**parser_document, "checkpoint": "not-reviewed"}
    with pytest.raises(CapabilityTurnValidationFailed) as checkpoint_error:
        _parse_turn(json.dumps(wrong_checkpoint), episode_slots[0])
    assert checkpoint_error.value.failure_code is CapabilityTurnFailureCode.INVALID_BINDING
    for invalid_envelope in (
        f"prefix {bare_text}",
        f"{bare_text} suffix",
        f"```json\n{bare_text}\n```\n```json\n{bare_text}\n```",
        f"```\n{bare_text}\n```",
    ):
        with pytest.raises(CapabilityTurnValidationFailed) as envelope_error:
            _parse_turn(invalid_envelope, episode_slots[0])
        assert envelope_error.value.failure_code is CapabilityTurnFailureCode.INVALID_JSON_ENVELOPE
    extra_document = {**parser_document, "unexpected": True}
    with pytest.raises(CapabilityTurnValidationFailed) as shape_error:
        _parse_turn(json.dumps(extra_document), episode_slots[0])
    assert shape_error.value.failure_code is CapabilityTurnFailureCode.INVALID_SHAPE
    oversized_artifact = {**parser_document, "artifact": "x" * (MAX_ARTIFACT_CHARS + 1)}
    with pytest.raises(CapabilityTurnValidationFailed) as artifact_error:
        _parse_turn(json.dumps(oversized_artifact), episode_slots[0])
    assert artifact_error.value.failure_code is CapabilityTurnFailureCode.FIELD_OVERSIZED
    oversized_evidence = {
        **parser_document,
        "evidence": ["x" * (MAX_EVIDENCE_STRING_CHARS + 1)],
    }
    with pytest.raises(CapabilityTurnValidationFailed) as evidence_error:
        _parse_turn(json.dumps(oversized_evidence), episode_slots[0])
    assert evidence_error.value.failure_code is CapabilityTurnFailureCode.FIELD_OVERSIZED

    continuity: dict[str, object] = dict(EMPTY_CONTINUITY_STATE)

    def invoke(slot: Any, concrete: Any, prior_state: Any) -> CapabilityLiveTurnResult:
        nonlocal continuity
        assert prior_state == continuity
        checks = fixtures[task.task_digest].payload["hidden_acceptance_checks"]
        evidence = [str(check["any_of"][0]) for check in checks]
        next_state: dict[str, object] = {
            "facts": ["remote effect can outlive the client observation"],
            "open_questions": ["which boundary owns the durable result"],
            "risks": ["a repeated attempt can duplicate an external effect"],
            "next_action": "test the next hypothesis",
        }
        checkpoint = task.required_checkpoints[min((slot.turn_index - 1) // 2, 3)]
        document = {
            "schema": TURN_SCHEMA,
            "phase": slot.phase,
            "progress": min(100, slot.turn_index * 13),
            "checkpoint": checkpoint,
            "evidence": evidence,
            "revision": {
                "changed": slot.turn_index == 6,
                "summary": "replaced an earlier retry assumption" if slot.turn_index == 6 else "",
            },
            "continuity_state": next_state,
            "artifact": "a concrete phase artifact with an observable assertion",
        }
        continuity = next_state
        text = json.dumps(document)
        assert concrete.plan.payload_digest != slot.prepared.plan.payload_digest
        response = {"choices": [{"message": {"content": text}}]}
        return CapabilityLiveTurnResult(response, digest(response), 100, 50)

    result = execute_capability_episode(
        plan=prepared_plan,
        task=task,
        fixture=fixtures[task.task_digest],
        model_id=model_id,
        slots=episode_slots,
        invoke=invoke,
        verifier=_verifier(),
    )
    assert result.status is CapabilityEpisodeStatus.PASSED
    assert result.model_turn_count == 8
    assert result.input_token_count == 800
    assert result.context_retention == 1
    assert result.self_correction_count == 1
    assert result.regression_count == 0
    assert result.sustained_progress_auc > 0

    def echo_prompt(slot: Any, concrete: Any, prior_state: Any) -> CapabilityLiveTurnResult:
        del prior_state
        response = {"choices": [{"message": {"content": str(concrete.call.payload)}}]}
        return CapabilityLiveTurnResult(response, digest(response), 10, 10)

    malformed = execute_capability_episode(
        plan=prepared_plan,
        task=task,
        fixture=fixtures[task.task_digest],
        model_id=model_id,
        slots=episode_slots,
        invoke=echo_prompt,
        verifier=_verifier(),
    )
    assert malformed.status is CapabilityEpisodeStatus.NOT_COMPARABLE
    assert classify_capability_episode(malformed) is (
        CapabilityEpisodeClassification.MODEL_CONTRACT_FAILED
    )
    assert malformed.model_turn_count == 1

    def obsolete_digest_echo(
        slot: Any, concrete: Any, prior_state: Any
    ) -> CapabilityLiveTurnResult:
        assert concrete == derive_capability_slot(slot, prior_state)
        document = {
            "schema": TURN_SCHEMA,
            "phase": slot.phase,
            "progress": 10,
            "checkpoint": task.required_checkpoints[0],
            "evidence": ["bounded observation"],
            "revision": {"changed": False, "summary": ""},
            "continuity_state": prior_state,
            "artifact": "bounded artifact",
            "prior_state_digest": digest("wrong-model-echo"),
        }
        response = {"choices": [{"message": {"content": json.dumps(document)}}]}
        return CapabilityLiveTurnResult(response, digest(response), 10, 10)

    obsolete = execute_capability_episode(
        plan=prepared_plan,
        task=task,
        fixture=fixtures[task.task_digest],
        model_id=model_id,
        slots=episode_slots,
        invoke=obsolete_digest_echo,
        verifier=_verifier(),
    )
    assert obsolete.status is CapabilityEpisodeStatus.NOT_COMPARABLE
    assert classify_capability_episode(obsolete) is (
        CapabilityEpisodeClassification.MODEL_CONTRACT_FAILED
    )

    marker_state: dict[str, object] = dict(EMPTY_CONTINUITY_STATE)

    def marker_only(slot: Any, concrete: Any, prior_state: Any) -> CapabilityLiveTurnResult:
        nonlocal marker_state
        del concrete
        assert prior_state == marker_state
        next_state = {
            "facts": [" ".join(task.expected_markers)],
            "open_questions": [],
            "risks": [],
            "next_action": "repeat labels",
        }
        document = {
            "schema": TURN_SCHEMA,
            "phase": slot.phase,
            "progress": min(100, slot.turn_index * 13),
            "checkpoint": task.required_checkpoints[min((slot.turn_index - 1) // 2, 3)],
            "evidence": [" ".join(task.expected_markers)],
            "revision": {
                "changed": slot.turn_index == 6,
                "summary": "changed labels" if slot.turn_index == 6 else "",
            },
            "continuity_state": next_state,
            "artifact": " ".join(task.expected_markers),
        }
        marker_state = next_state
        response = {"choices": [{"message": {"content": json.dumps(document)}}]}
        return CapabilityLiveTurnResult(response, digest(response), 10, 10)

    marker_result = execute_capability_episode(
        plan=prepared_plan,
        task=task,
        fixture=fixtures[task.task_digest],
        model_id=model_id,
        slots=episode_slots,
        invoke=marker_only,
        verifier=_verifier(),
    )
    assert marker_result.status is CapabilityEpisodeStatus.FAILED
    assert marker_result.hidden_acceptance_ratio == 0

    first = episode_slots[0]
    concrete_a = derive_capability_slot(first, EMPTY_CONTINUITY_STATE)
    concrete_b = derive_capability_slot(
        first,
        {**EMPTY_CONTINUITY_STATE, "facts": ["new bounded fact"]},
    )
    assert concrete_a.plan.payload_digest != concrete_b.plan.payload_digest
    material = capability_derivation_material(first, EMPTY_CONTINUITY_STATE)
    assert material["template_digest"] == first.template_digest
    assert material["continuity_state_digest"] == digest(EMPTY_CONTINUITY_STATE)
    assert concrete_a.call.payload == {
        "model": first.backend_model,
        "messages": [
            {"role": "system", "content": first.system_prompt},
            {
                "role": "user",
                "content": (
                    first.prompt_prefix
                    + "Asagidaki onceki state sistem tarafindan baglanmistir; exact cikti "
                    + "semasina ek alan ekleme. Onceki continuity_state:\n"
                    + canonical_json(EMPTY_CONTINUITY_STATE)
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": first.output_cap,
    }


def test_parallel_runner_starts_each_model_in_same_task_wave() -> None:
    plan = _plan(("model-a", "model-b", "model-c"))
    _, _, fixtures = _loaded()
    adapter = FakeAdapter()
    results = CapabilityCohortRunner(adapter, _verifier()).run(plan, fixtures)

    assert len(results) == 9
    assert all(result.status is CapabilityEpisodeStatus.PASSED for result in results)
    assert all(result.start_skew_ms <= plan.start_skew_budget_ms for result in results)
    assert all(max(starts) - min(starts) < 0.1 for starts in adapter.starts.values())
    assert all("payload" not in repr(result).lower() for result in results)


def test_aggregate_is_ability_first_and_latency_is_report_only() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    episodes = CapabilityCohortRunner(FakeAdapter(), _verifier()).run(plan, fixtures)
    result = aggregate_capability_episodes(plan, "model-a", episodes)
    slower = tuple(replace(row, duration_ms=row.duration_ms * 10) for row in episodes)
    slower_result = aggregate_capability_episodes(plan, "model-a", slower)

    assert result.general_score == slower_result.general_score
    assert result.mean_duration_ms < slower_result.mean_duration_ms
    assert {role.value for role, _ in result.role_scores} == {
        "implementer",
        "reviewer",
        "researcher",
    }


def test_self_verifier_and_incomplete_task_coverage_fail_closed() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    episodes = CapabilityCohortRunner(FakeAdapter(), _verifier()).run(plan, fixtures)
    with pytest.raises(PolicyViolation, match="exact task coverage"):
        aggregate_capability_episodes(plan, "model-a", episodes[:-1])

    first: CapabilityEpisodeResult = episodes[0]
    with pytest.raises(PolicyViolation, match="kendi sonucunu"):
        replace(first, verifier_model_id="model-a")


def test_response_shape_and_progress_regression_fail_closed() -> None:
    task = _plan(("model-a",)).registry.tasks[0]
    response = CapabilityResponse(
        payload={"status": "completed"},
        duration_ms=1,
        input_tokens=1,
        output_tokens=1,
        provider_latency_ms=1,
        checkpoint_receipts=(
            CapabilityCheckpointReceipt("empty", 0, digest("empty-checkpoint"), 0, 1),
        ),
        tool_receipts=(),
        self_correction_count=0,
        hidden_acceptance_passed=0,
        hidden_acceptance_total=0,
        regression_count=0,
        context_retention_ratio=1.0,
        unsafe=False,
        acceptance_evidence_digest=digest("invalid-shape"),
    )
    with pytest.raises(ValidationFailed, match="exact shape"):
        _verifier().verify(tested_model_id="model-a", task=task, response=response)

    _, _, fixtures = _loaded()
    fixture = fixtures[task.task_digest]
    payload = {
        "status": "completed",
        "markers": list(fixture.payload["expected_markers"]),
        "artifact_digest": digest("artifact"),
    }
    regressed = replace(
        response,
        duration_ms=2,
        payload=payload,
        checkpoint_receipts=(
            CapabilityCheckpointReceipt("first", 1, digest("first"), 1, 2),
            CapabilityCheckpointReceipt("second", 2, digest("second"), 0, 2),
        ),
        hidden_acceptance_passed=1,
        hidden_acceptance_total=1,
    )
    regressed = replace(
        regressed,
        acceptance_evidence_digest=capability_acceptance_evidence_digest(
            task, regressed, _verifier().provenance_digest
        ),
    )
    regressed = replace(regressed, regression_count=1)
    regressed = replace(
        regressed,
        acceptance_evidence_digest=capability_acceptance_evidence_digest(
            task, regressed, _verifier().provenance_digest
        ),
    )
    metrics = _verifier().verify(tested_model_id="model-a", task=task, response=regressed)
    assert metrics["regression_count"] == 1


def test_registry_digest_drift_and_repository_escape_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "registry.yaml"
    config.write_text(
        (ROOT / "config" / "model_capability_benchmark.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(PolicyViolation, match="repository disinda"):
        load_capability_registry(config, repository_root=tmp_path)


def test_episode_evidence_contains_only_digests_and_metrics() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    result = CapabilityCohortRunner(FakeAdapter(), _verifier()).run(plan, fixtures)[0]
    assert result.evidence_digest.startswith("sha256:")
    assert result.response_digest.startswith("sha256:")
    assert result.capability_score > 0.8
    assert result.started_at.tzinfo is dt.UTC


class BlockingAdapter(FakeAdapter):
    def execute(
        self,
        *,
        model_id: str,
        task: Any,
        fixture: Any,
        profile: Any,
        turn_index: int,
        prior_response_digest: str | None,
        cancellation: threading.Event,
    ) -> CapabilityResponse:
        del model_id, task, fixture, profile, turn_index, prior_response_digest
        cancellation.wait(timeout=1)
        raise PolicyViolation("injected cancellation observed")


def test_hard_deadline_sets_cooperative_cancellation() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    started = time.monotonic()
    with pytest.raises(PolicyViolation, match="hard deadline"):
        CapabilityCohortRunner(BlockingAdapter(), _verifier(), timeout_scale=0.001).run(
            plan, fixtures
        )
    assert time.monotonic() - started < 1


class TokenOverflowAdapter(FakeAdapter):
    def execute(self, **kwargs: Any) -> CapabilityResponse:
        return replace(
            super().execute(**kwargs),
            output_tokens=1_000_000,
        )


def test_token_budget_is_enforced_before_evaluation() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    with pytest.raises(PolicyViolation, match="token butcesi"):
        CapabilityCohortRunner(TokenOverflowAdapter(), _verifier()).run(plan, fixtures)


def test_checkpoint_receipts_must_arrive_in_strict_time_order() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    response = FakeAdapter().execute(
        model_id="model-a",
        task=plan.registry.tasks[0],
        fixture=fixtures[plan.registry.tasks[0].task_digest],
        profile=plan.execution_profile,
        turn_index=1,
        prior_response_digest=None,
        cancellation=threading.Event(),
    )
    with pytest.raises(ValidationFailed, match="sirali artmali"):
        replace(response, checkpoint_receipts=tuple(reversed(response.checkpoint_receipts)))


class NeverCompleteAdapter(FakeAdapter):
    def execute(self, **kwargs: Any) -> CapabilityResponse:
        response = super().execute(**kwargs)
        task = kwargs["task"]
        profile = kwargs["profile"]
        turn_index = kwargs["turn_index"]
        response = replace(
            response,
            payload={**response.payload, "status": "continue"},
            checkpoint_receipts=tuple(
                replace(
                    receipt,
                    acceptance_passed=(turn_index - 1) * len(task.required_checkpoints) + index,
                    acceptance_total=(profile.max_model_turns * len(task.required_checkpoints)),
                )
                for index, receipt in enumerate(response.checkpoint_receipts, start=1)
            ),
        )
        return replace(
            response,
            acceptance_evidence_digest=capability_acceptance_evidence_digest(
                task, response, profile.evaluator_provenance_digest
            ),
        )


def test_turn_exhaustion_cannot_become_passed() -> None:
    plan = _plan(("model-a",))
    _, _, fixtures = _loaded()
    results = CapabilityCohortRunner(NeverCompleteAdapter(), _verifier()).run(plan, fixtures)
    assert len(results) == 3
    assert all(result.status is CapabilityEpisodeStatus.FAILED for result in results)
    assert all(
        result.model_turn_count == plan.execution_profile.max_model_turns for result in results
    )
