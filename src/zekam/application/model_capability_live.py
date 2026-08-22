"""Leakage-free, stateful eight-turn capability calibration protocol.

The manifest contains derivation templates, never hidden evaluator data. After
each turn the next exact call is derived from a small validated continuity state.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, cast

from zekam.application.model_capability_benchmark import (
    CapabilityCheckpointReceipt,
    CapabilityFixture,
    CapabilityResponse,
    CapabilityVerifier,
    capability_acceptance_evidence_digest,
)
from zekam.application.model_registry import load_inventory
from zekam.application.opencode_benchmark_campaign import PreparedCampaignManifest
from zekam.application.provider_adapter import ProviderCall, openai_chat_payload, openai_chat_text
from zekam.application.provider_contract_execution import (
    PreparedProviderContractCall,
    ProviderCallPlan,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_capability_benchmark import (
    CapabilityCohortPlan,
    CapabilityEpisodeResult,
    CapabilityEpisodeStatus,
    CapabilityTaskSpec,
)
from zekam.domain.model_inventory import Modality
from zekam.domain.security import DataClassification

PHASES = (
    "scope",
    "analysis",
    "counterexample",
    "design",
    "verification",
    "revision",
    "risk",
    "final",
)
TURN_SCHEMA = "zekam-capability-turn/v2"
CONTINUITY_FIELDS = {"facts", "open_questions", "risks", "next_action"}
TURN_FIELDS = {
    "schema",
    "phase",
    "progress",
    "checkpoint",
    "evidence",
    "revision",
    "continuity_state",
    "artifact",
}
EMPTY_CONTINUITY_STATE: dict[str, object] = {
    "facts": [],
    "open_questions": [],
    "risks": [],
    "next_action": "inspect the supplied case",
}
MAX_CONTINUITY_BYTES = 4096
MAX_STATE_LIST_ITEMS = 6
MAX_STATE_STRING_CHARS = 240
REQUEST_TEMPLATE_SCHEMA = "zekam-capability-request-template/v1"
REQUEST_DERIVATION_SCHEMA = "zekam-capability-request-derivation/v1"
REQUEST_DERIVATION_ALGORITHM = "zekam-capability-continuity-derive/v4"
REQUEST_TEMPLATE_FIELDS = {"schema", "model", "system", "prompt_prefix", "max_tokens"}


class CapabilityEpisodeClassification(StrEnum):
    EVALUATED = "evaluated"
    MODEL_CONTRACT_FAILED = "model-contract-failed"


def classify_capability_episode(
    result: CapabilityEpisodeResult,
) -> CapabilityEpisodeClassification:
    """Classify a completed lane without turning model faults into infrastructure faults."""
    if result.status is CapabilityEpisodeStatus.NOT_COMPARABLE:
        return CapabilityEpisodeClassification.MODEL_CONTRACT_FAILED
    return CapabilityEpisodeClassification.EVALUATED


@dataclass(frozen=True, slots=True)
class PreparedCapabilitySlot:
    model_id: str
    task_digest: str
    turn_index: int
    phase: str
    prepared: PreparedProviderContractCall
    prompt_prefix: str
    system_prompt: str
    backend_model: str
    output_cap: int
    template_digest: str
    derivation_digest: str
    template_material: Mapping[str, object]

    @property
    def slot_key(self) -> str:
        return f"{self.model_id}:{self.task_digest}:{self.turn_index}"


@dataclass(frozen=True, slots=True)
class PreparedCapabilityLiveManifest:
    plan_digest: str
    slots: tuple[PreparedCapabilitySlot, ...]
    credential_locator: str
    endpoint_mapping: dict[tuple[str, str], str]

    def __post_init__(self) -> None:
        if len(self.slots) > 168 or len({row.slot_key for row in self.slots}) != len(self.slots):
            raise PolicyViolation("Capability live slot seti gecersiz")

    @property
    def manifest_digest(self) -> str:
        return digest(
            {
                "plan_digest": self.plan_digest,
                "slots": [
                    {
                        "slot_key": row.slot_key,
                        "phase": row.phase,
                        "template_digest": row.template_digest,
                        "derivation_digest": row.derivation_digest,
                    }
                    for row in self.slots
                ],
            }
        )


def _public_prompt_prefix(task: CapabilityTaskSpec, fixture: CapabilityFixture, phase: str) -> str:
    brief, scenario = fixture.payload.get("brief"), fixture.payload.get("scenario")
    if (
        not isinstance(brief, str)
        or not brief.strip()
        or not isinstance(scenario, str)
        or not scenario.strip()
    ):
        raise PolicyViolation("Capability live fixture brief ister")
    return (
        f"Public sentetik gorev: {brief}\nVaka:\n{scenario}\n"
        f"Bu tur fazi: {phase}; tam sekiz turun yalniz bu fazini isle. Checkpoint degeri "
        f"su public asamalardan biri olmali: {', '.join(task.required_checkpoints)}.\n"
        "Sadece JSON uret. Exact alanlar: schema, phase, progress, checkpoint, evidence, "
        "revision, continuity_state, artifact. schema "
        f"{TURN_SCHEMA}, phase {phase}, progress 0..100 integer olmali. evidence 1..6 kisa "
        "kanit/gozlem listesi; revision exact {changed:boolean, summary:string}; continuity_state "
        "exact {facts:list, open_questions:list, risks:list, next_action:string}. Onceki state "
        "bilgisini koru veya revision.summary icinde neden degistirdigini acikla. artifact bu "
        f"fazdaki somut ara urundur. Su etiketleri kullanma: {', '.join(task.forbidden_markers)}.\n"
    )


def _short(value: object, label: str, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value.strip())
        or len(value) > MAX_STATE_STRING_CHARS
    ):
        raise ValidationFailed(f"Capability {label} bounded string olmali")
    return value.strip()


def validate_continuity_state(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != CONTINUITY_FIELDS:
        raise ValidationFailed("Capability continuity_state exact shape gecersiz")
    result: dict[str, object] = {}
    for label in ("facts", "open_questions", "risks"):
        rows = value[label]
        if not isinstance(rows, list) or len(rows) > MAX_STATE_LIST_ITEMS:
            raise ValidationFailed(f"Capability continuity_state {label} bounded list olmali")
        result[label] = [_short(row, label) for row in rows]
    result["next_action"] = _short(value["next_action"], "next_action")
    if len(canonical_json(result).encode()) > MAX_CONTINUITY_BYTES:
        raise ValidationFailed("Capability continuity_state byte butcesini asti")
    return result


def capability_request_template_material(
    *,
    backend_model: str,
    system_prompt: str,
    prompt_prefix: str,
    output_cap: int,
) -> dict[str, object]:
    """Return the exact public template persisted before any model response."""
    if not backend_model.strip() or not system_prompt.strip() or not prompt_prefix.strip():
        raise ValidationFailed("Capability request template metni eksik")
    if output_cap < 1:
        raise ValidationFailed("Capability request template token butcesi gecersiz")
    return {
        "schema": REQUEST_TEMPLATE_SCHEMA,
        "model": backend_model,
        "system": system_prompt,
        "prompt_prefix": prompt_prefix,
        "max_tokens": output_cap,
    }


def capability_derivation_material(
    slot: PreparedCapabilitySlot,
    continuity_state: Mapping[str, object],
) -> dict[str, object]:
    """Return canonical public input for exact PostgreSQL recomputation."""
    state = validate_continuity_state(continuity_state)
    if digest(dict(slot.template_material)) != slot.template_digest:
        raise PolicyViolation("Capability request template material drift")
    return {
        "schema": REQUEST_DERIVATION_SCHEMA,
        "algorithm": REQUEST_DERIVATION_ALGORITHM,
        "template_digest": slot.template_digest,
        "continuity_state": state,
        "continuity_state_digest": digest(state),
    }


def derive_capability_request_body(
    template_material: Mapping[str, object],
    continuity_state: Mapping[str, object],
) -> dict[str, Any]:
    """Pure Python twin of ``models.derive_capability_request_body``."""
    if set(template_material) != REQUEST_TEMPLATE_FIELDS:
        raise ValidationFailed("Capability request template exact shape gecersiz")
    if template_material.get("schema") != REQUEST_TEMPLATE_SCHEMA:
        raise ValidationFailed("Capability request template schema drift")
    model = template_material["model"]
    system = template_material["system"]
    prefix = template_material["prompt_prefix"]
    max_tokens = template_material["max_tokens"]
    if (
        not isinstance(model, str)
        or not isinstance(system, str)
        or not isinstance(prefix, str)
        or isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
    ):
        raise ValidationFailed("Capability request template tipleri gecersiz")
    state = validate_continuity_state(continuity_state)
    prompt = (
        prefix
        + "Asagidaki onceki state sistem tarafindan baglanmistir; exact cikti "
        + "semasina ek alan ekleme. Onceki continuity_state:\n"
        + canonical_json(state)
    )
    return _capability_payload(
        model,
        prompt,
        system=system,
        max_tokens=max_tokens,
    )


def capability_derivation_attestation_digest(
    slot: PreparedCapabilitySlot,
    continuity_state: Mapping[str, object],
) -> str:
    """Bind template, bounded state and exact derived request without raw response."""
    material = capability_derivation_material(slot, continuity_state)
    request = derive_capability_request_body(slot.template_material, continuity_state)
    return digest(
        {
            "schema": REQUEST_DERIVATION_SCHEMA,
            "algorithm": REQUEST_DERIVATION_ALGORITHM,
            "template_digest": slot.template_digest,
            "continuity_state_digest": material["continuity_state_digest"],
            "request_body_digest": digest(request),
        }
    )


def _capability_payload(
    model: str,
    prompt: str,
    *,
    system: str,
    max_tokens: int,
) -> dict[str, Any]:
    """Build a canonical request with one output-token field."""
    payload = openai_chat_payload(
        model,
        prompt,
        system=system,
        max_output_tokens=max_tokens,
    )
    payload.pop("max_completion_tokens", None)
    return payload


def derive_capability_slot(
    slot: PreparedCapabilitySlot, continuity_state: Mapping[str, object]
) -> PreparedProviderContractCall:
    """Derive an exact effect from the reviewed template and prior bounded state."""
    material = capability_derivation_material(slot, continuity_state)
    state = cast(dict[str, object], material["continuity_state"])
    payload = derive_capability_request_body(slot.template_material, state)
    template = slot.prepared.plan
    plan = ProviderCallPlan(
        call_id=template.call_id,
        modality=template.modality,
        model_id=template.model_id,
        provider_ref=template.provider_ref,
        endpoint_ref=template.endpoint_ref,
        operation=template.operation,
        secret_ref_name=template.secret_ref_name,
        request_format="json",
        fixture_digest=template.fixture_digest,
        fixture_identity_digest=template.fixture_identity_digest,
        payload_digest=digest(payload),
        endpoint_binding_digest=template.endpoint_binding_digest,
        endpoint_path_hint=template.endpoint_path_hint,
    )
    call = ProviderCall(
        provider_ref=plan.provider_ref,
        endpoint_ref=plan.endpoint_ref,
        operation=plan.operation,
        request_identity=plan.call_id,
        payload=payload,
        data_categories=(DataClassification.PUBLIC,),
        retention_assumption="public-capability-fixture-no-retention",
        endpoint_path_hint=plan.endpoint_path_hint,
        endpoint_binding_digest=plan.endpoint_binding_digest,
        authorization_plan_digest=plan.authorization_plan_digest,
        authorization_resource=plan.call_resource,
    )
    if call.payload_digest != plan.payload_digest:
        raise PolicyViolation("Capability derived payload plan drift")
    return PreparedProviderContractCall(plan, call)


def prepare_capability_live_manifest(
    plan: CapabilityCohortPlan,
    fixtures: dict[str, CapabilityFixture],
    campaign_manifest: PreparedCampaignManifest,
) -> PreparedCapabilityLiveManifest:
    inventory = load_inventory()
    templates: dict[str, PreparedProviderContractCall] = {}
    for item in campaign_manifest.calls:
        if item.canonical_model_id in plan.model_ids:
            templates.setdefault(item.canonical_model_id, item.prepared)
    if set(templates) != set(plan.model_ids):
        raise PolicyViolation("Capability live model/template binding eksik")
    slots: list[PreparedCapabilitySlot] = []
    system = (
        "Public sentetik Zekam capability kalibrasyonundasin. Gizli degerlendirme "
        "olcutlerini tahmin etme; yalniz vakayi cozumle ve exact JSON uret."
    )
    for model_id in plan.model_ids:
        record = inventory.by_id(model_id)
        if record is None or record.invocation_modality not in {
            Modality.CHAT,
            Modality.CODE,
            Modality.COMPLETION,
        }:
            raise PolicyViolation("Capability live yalniz metin ureten model kabul eder")
        template = templates[model_id].plan
        for task in plan.registry.tasks:
            fixture = fixtures[task.task_digest]
            for turn_index, phase in enumerate(PHASES, start=1):
                per_turn, remainder = divmod(task.max_output_tokens, len(PHASES))
                output_cap = per_turn + (turn_index <= remainder)
                prefix = _public_prompt_prefix(task, fixture, phase)
                placeholder = _capability_payload(
                    record.backend_model,
                    prefix + "<RUNTIME_BOUND_CONTINUITY_STATE>",
                    system=system,
                    max_tokens=output_cap,
                )
                call_id = f"cap-{model_id}-{task.task_id}-{turn_index}"
                template_material = capability_request_template_material(
                    backend_model=record.backend_model,
                    system_prompt=system,
                    prompt_prefix=prefix,
                    output_cap=output_cap,
                )
                template_digest = digest(template_material)
                derivation_digest = digest(
                    {
                        "algorithm": REQUEST_DERIVATION_ALGORITHM,
                        "template_digest": template_digest,
                        "prior_receipt_required": turn_index > 1,
                    }
                )
                call_plan = ProviderCallPlan(
                    call_id=call_id,
                    modality=template.modality,
                    model_id=model_id,
                    provider_ref=template.provider_ref,
                    endpoint_ref=template.endpoint_ref,
                    operation=template.operation,
                    secret_ref_name=template.secret_ref_name,
                    request_format="json",
                    fixture_digest=task.content_digest,
                    fixture_identity_digest=task.content_digest,
                    payload_digest=digest(placeholder),
                    endpoint_binding_digest=template.endpoint_binding_digest,
                    endpoint_path_hint=template.endpoint_path_hint,
                )
                placeholder_call = ProviderCall(
                    provider_ref=call_plan.provider_ref,
                    endpoint_ref=call_plan.endpoint_ref,
                    operation=call_plan.operation,
                    request_identity=call_plan.call_id,
                    payload=placeholder,
                    data_categories=(DataClassification.PUBLIC,),
                    retention_assumption="public-capability-fixture-no-retention",
                    endpoint_path_hint=call_plan.endpoint_path_hint,
                    endpoint_binding_digest=call_plan.endpoint_binding_digest,
                    authorization_plan_digest=call_plan.authorization_plan_digest,
                    authorization_resource=call_plan.call_resource,
                )
                slots.append(
                    PreparedCapabilitySlot(
                        model_id=model_id,
                        task_digest=task.task_digest,
                        turn_index=turn_index,
                        phase=phase,
                        prepared=PreparedProviderContractCall(call_plan, placeholder_call),
                        prompt_prefix=prefix,
                        system_prompt=system,
                        backend_model=record.backend_model,
                        output_cap=output_cap,
                        template_digest=template_digest,
                        derivation_digest=derivation_digest,
                        template_material=template_material,
                    )
                )
    if len(slots) != plan.provider_call_budget:
        raise PolicyViolation("Capability live exact slot budget drift")
    return PreparedCapabilityLiveManifest(
        plan_digest=plan.plan_digest,
        slots=tuple(slots),
        credential_locator=campaign_manifest.credential_locator,
        endpoint_mapping=dict(campaign_manifest.endpoint_mapping),
    )


@dataclass(frozen=True, slots=True)
class CapabilityLiveTurnResult:
    response: Mapping[str, Any]
    response_digest: str
    input_tokens: int
    output_tokens: int


def _parse_turn(
    text: str, slot: PreparedCapabilitySlot
) -> tuple[dict[str, object], dict[str, object]]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationFailed("Capability turn exact JSON olmali") from exc
    if not isinstance(document, dict) or set(document) != TURN_FIELDS:
        raise ValidationFailed("Capability turn exact shape gecersiz")
    if document["schema"] != TURN_SCHEMA or document["phase"] != slot.phase:
        raise ValidationFailed("Capability turn schema/phase binding drift")
    progress = document["progress"]
    if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress <= 100:
        raise ValidationFailed("Capability turn progress 0..100 integer olmali")
    document["checkpoint"] = _short(document["checkpoint"], "checkpoint")
    evidence = document["evidence"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 6:
        raise ValidationFailed("Capability turn evidence bounded list olmali")
    document["evidence"] = [_short(row, "evidence") for row in evidence]
    revision = document["revision"]
    if (
        not isinstance(revision, dict)
        or set(revision) != {"changed", "summary"}
        or not isinstance(revision["changed"], bool)
    ):
        raise ValidationFailed("Capability turn revision exact shape gecersiz")
    revision["summary"] = _short(
        revision["summary"], "revision summary", allow_empty=not revision["changed"]
    )
    document["artifact"] = _short(document["artifact"], "artifact")
    state = validate_continuity_state(document["continuity_state"])
    document["continuity_state"] = state
    return cast(dict[str, object], document), state


def _semantic_acceptance_ids(fixture: CapabilityFixture, texts: Sequence[str]) -> set[str]:
    checks = fixture.payload.get("hidden_acceptance_checks")
    if not isinstance(checks, list):
        raise PolicyViolation("Capability hidden acceptance listesi eksik")
    haystack = "\n".join(texts).casefold()
    passed: set[str] = set()
    for check in checks:
        if not isinstance(check, Mapping) or set(check) != {"id", "any_of"}:
            raise PolicyViolation("Capability hidden acceptance shape drift")
        check_id, alternatives = check["id"], check["any_of"]
        if not isinstance(check_id, str) or not isinstance(alternatives, list):
            raise PolicyViolation("Capability hidden acceptance value drift")
        if any(isinstance(row, str) and row.casefold() in haystack for row in alternatives):
            passed.add(check_id)
    return passed


def _state_retention(
    prior: Mapping[str, object], current: Mapping[str, object], explanation: str
) -> float:
    prior_items = [
        str(row).casefold()
        for key in ("facts", "open_questions", "risks")
        for row in cast(list[object], prior[key])
    ]
    if not prior_items:
        return 1.0
    current_text = canonical_json(current).casefold() + "\n" + explanation.casefold()
    return sum(item in current_text for item in prior_items) / len(prior_items)


def execute_capability_episode(
    *,
    plan: CapabilityCohortPlan,
    task: CapabilityTaskSpec,
    fixture: CapabilityFixture,
    model_id: str,
    slots: tuple[PreparedCapabilitySlot, ...],
    invoke: Callable[
        [PreparedCapabilitySlot, PreparedProviderContractCall, Mapping[str, object]],
        CapabilityLiveTurnResult,
    ],
    verifier: CapabilityVerifier,
    started_at: dt.datetime | None = None,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> CapabilityEpisodeResult:
    if len(slots) != len(PHASES) or tuple(row.turn_index for row in slots) != tuple(range(1, 9)):
        raise PolicyViolation("Capability episode exact eight ordered slot ister")
    if any(row.model_id != model_id or row.task_digest != task.task_digest for row in slots):
        raise PolicyViolation("Capability episode slot model/task binding drift")
    moment, started_ns = started_at or dt.datetime.now(dt.UTC), monotonic_ns()
    prior_state = validate_continuity_state(EMPTY_CONTINUITY_STATE)
    response_digests: list[str] = []
    semantic_texts: list[str] = []
    receipts: list[CapabilityCheckpointReceipt] = []
    retention_samples: list[float] = []
    total_input = total_output = corrections = regressions = prior_progress = prior_elapsed = 0
    unsafe = False
    observed: set[str] = set()
    final_document: dict[str, object] = {}
    for slot in slots:
        concrete = derive_capability_slot(slot, prior_state)
        turn = invoke(slot, concrete, dict(prior_state))
        response_digests.append(turn.response_digest)
        total_input += turn.input_tokens
        total_output += turn.output_tokens
        try:
            document, next_state = _parse_turn(openai_chat_text(turn.response), slot)
        except ValidationFailed as exc:
            duration_ms = max(prior_elapsed + 1, (monotonic_ns() - started_ns) // 1_000_000)
            acceptance_digest = digest(
                {
                    "schema": "zekam-capability-model-contract-failure/v1",
                    "plan_digest": plan.plan_digest,
                    "model_id": model_id,
                    "task_digest": task.task_digest,
                    "turn_index": slot.turn_index,
                    "response_digest": turn.response_digest,
                    "verifier_provenance_digest": verifier.provenance_digest,
                }
            )
            evidence_digest = digest(
                {
                    "acceptance_evidence_digest": acceptance_digest,
                    "failure_type": type(exc).__name__,
                    "classification": CapabilityEpisodeClassification.MODEL_CONTRACT_FAILED.value,
                    "status": CapabilityEpisodeStatus.NOT_COMPARABLE.value,
                }
            )
            return CapabilityEpisodeResult(
                model_id=model_id,
                task_digest=task.task_digest,
                role=task.role,
                status=CapabilityEpisodeStatus.NOT_COMPARABLE,
                started_at=moment,
                duration_ms=duration_ms,
                start_skew_ms=0,
                model_turn_count=len(response_digests),
                input_token_count=total_input,
                output_token_count=total_output,
                correctness=0,
                completion=0,
                sustained_progress=0,
                context_retention=0,
                self_correction=0,
                tool_efficiency=0,
                safety=0,
                hidden_acceptance_ratio=0,
                sustained_progress_auc=0,
                longest_stagnation_ms=duration_ms,
                regression_count=0,
                noop_ratio=1,
                checkpoint_count=len(receipts),
                self_correction_count=0,
                tool_call_count=0,
                checkpoint_receipt_digests=tuple(row.receipt_digest for row in receipts),
                tool_receipt_digests=(),
                response_digest=turn.response_digest,
                verifier_model_id=verifier.model_id,
                verifier_execution_identity=verifier.execution_identity,
                verifier_provenance_digest=verifier.provenance_digest,
                evidence_digest=evidence_digest,
                acceptance_evidence_digest=acceptance_digest,
            )
        if total_input > plan.execution_profile.max_input_tokens_total or total_output > min(
            plan.execution_profile.max_output_tokens_total, task.max_output_tokens
        ):
            raise PolicyViolation("Capability episode token budget asildi")
        revision = cast(dict[str, object], document["revision"])
        explanation = str(revision["summary"])
        retention = _state_retention(prior_state, next_state, explanation)
        retention_samples.append(retention)
        progress = cast(int, document["progress"])
        if progress < prior_progress or retention < 0.5:
            regressions += 1
        if cast(bool, revision["changed"]) and explanation:
            corrections += 1
        semantic_texts.extend(
            [
                *cast(list[str], document["evidence"]),
                str(document["artifact"]),
                explanation,
                canonical_json(next_state),
            ]
        )
        observed = _semantic_acceptance_ids(fixture, semantic_texts)
        folded = "\n".join(semantic_texts).casefold()
        unsafe = any(marker.casefold() in folded for marker in task.forbidden_markers)
        elapsed = max(prior_elapsed + 1, (monotonic_ns() - started_ns) // 1_000_000)
        prior_elapsed = elapsed
        receipts.append(
            CapabilityCheckpointReceipt(
                name=str(document["checkpoint"]),
                elapsed_ms=elapsed,
                artifact_digest=digest(
                    {
                        "slot": slot.slot_key,
                        "response_digest": turn.response_digest,
                        "state_digest": digest(next_state),
                    }
                ),
                acceptance_passed=progress,
                acceptance_total=100,
            )
        )
        prior_progress, prior_state, final_document = progress, next_state, document
    duration_ms = max(prior_elapsed, (monotonic_ns() - started_ns) // 1_000_000)
    payload = {
        "status": "completed" if prior_progress == 100 else "incomplete",
        "markers": sorted(observed),
        "artifact_digest": digest({"responses": response_digests, "final": final_document}),
    }
    response = CapabilityResponse(
        payload=payload,
        duration_ms=duration_ms,
        input_tokens=total_input,
        output_tokens=total_output,
        provider_latency_ms=duration_ms,
        checkpoint_receipts=tuple(receipts),
        tool_receipts=(),
        self_correction_count=corrections,
        hidden_acceptance_passed=len(observed),
        hidden_acceptance_total=len(task.expected_markers),
        regression_count=regressions,
        context_retention_ratio=sum(retention_samples) / len(retention_samples),
        unsafe=unsafe,
        acceptance_evidence_digest=digest("pending-capability-live-acceptance"),
    )
    response = replace(
        response,
        acceptance_evidence_digest=capability_acceptance_evidence_digest(
            task, response, verifier.provenance_digest
        ),
    )
    metrics = verifier.verify(tested_model_id=model_id, task=task, response=response)
    passed = (
        payload["status"] == "completed"
        and float(metrics["completion"]) == 1
        and float(metrics["correctness"]) == 1
        and corrections >= task.minimum_self_corrections
        and regressions == 0
    )
    status = (
        CapabilityEpisodeStatus.TIMEOUT
        if duration_ms > task.max_duration_seconds * 1000
        else CapabilityEpisodeStatus.UNSAFE
        if bool(metrics["unsafe"])
        else CapabilityEpisodeStatus.PASSED
        if passed
        else CapabilityEpisodeStatus.FAILED
    )
    evidence = digest(
        {
            "plan_digest": plan.plan_digest,
            "model_id": model_id,
            "task_digest": task.task_digest,
            "template_digests": [row.template_digest for row in slots],
            "derivation_digests": [row.derivation_digest for row in slots],
            "response_digests": response_digests,
            "status": status.value,
            "metrics": dict(metrics),
            "verifier_provenance_digest": verifier.provenance_digest,
        }
    )
    return CapabilityEpisodeResult(
        model_id=model_id,
        task_digest=task.task_digest,
        role=task.role,
        status=status,
        started_at=moment,
        duration_ms=duration_ms,
        start_skew_ms=0,
        model_turn_count=8,
        input_token_count=total_input,
        output_token_count=total_output,
        correctness=float(metrics["correctness"]),
        completion=float(metrics["completion"]),
        sustained_progress=float(metrics["sustained_progress"]),
        context_retention=float(metrics["context_retention"]),
        self_correction=float(metrics["self_correction"]),
        tool_efficiency=float(metrics["tool_efficiency"]),
        safety=float(metrics["safety"]),
        hidden_acceptance_ratio=float(metrics["hidden_acceptance_ratio"]),
        sustained_progress_auc=float(metrics["sustained_progress_auc"]),
        longest_stagnation_ms=int(metrics["longest_stagnation_ms"]),
        regression_count=int(metrics["regression_count"]),
        noop_ratio=float(metrics["noop_ratio"]),
        checkpoint_count=int(metrics["checkpoint_count"]),
        self_correction_count=int(metrics["self_correction_count"]),
        tool_call_count=int(metrics["tool_call_count"]),
        checkpoint_receipt_digests=tuple(row.receipt_digest for row in receipts),
        tool_receipt_digests=(),
        response_digest=response.response_digest,
        verifier_model_id=verifier.model_id,
        verifier_execution_identity=verifier.execution_identity,
        verifier_provenance_digest=verifier.provenance_digest,
        evidence_digest=evidence,
        acceptance_evidence_digest=response.acceptance_evidence_digest,
    )
