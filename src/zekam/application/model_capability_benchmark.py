"""Süreli model yetenek benchmark'i: strict fixture, paralel lane ve bagimsiz puanlama.

Bu modul provider, secret veya authorization cozmez. Canli adapter ancak mevcut
claim-before-effect zinciri tarafindan enjekte edilir. Ham gorev ve model yaniti
process belleginden disari cikmaz; kalici katmana yalniz digest ve metrik gider.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol, cast

import yaml

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_capability_benchmark import (
    CapabilityCohortPlan,
    CapabilityEpisodeResult,
    CapabilityEpisodeStatus,
    CapabilityExecutionProfile,
    CapabilityTaskRegistry,
    CapabilityTaskSpec,
)
from zekam.domain.model_routing import AgentRole

CAPABILITY_TASK_SCHEMA = "zekam-capability-task/v1"
CAPABILITY_RESPONSE_SCHEMA_DIGEST = digest(
    {
        "model_response_exact_fields": [
            "schema",
            "phase",
            "progress",
            "checkpoint",
            "evidence",
            "revision",
            "continuity_state",
            "prior_state_digest",
            "artifact",
        ],
        "acceptance_metrics_source": "independent-hidden-harness-evaluator",
        "version": 2,
    }
)


@dataclass(frozen=True, slots=True)
class CapabilityFixture:
    task_id: str
    version: int
    payload: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class CapabilityCheckpointReceipt:
    name: str
    elapsed_ms: int
    artifact_digest: str
    acceptance_passed: int
    acceptance_total: int

    def __post_init__(self) -> None:
        if (
            not self.name.strip()
            or min(self.elapsed_ms, self.acceptance_passed, self.acceptance_total) < 0
        ):
            raise ValidationFailed("Capability checkpoint receipt gecersiz")
        if self.acceptance_passed > self.acceptance_total:
            raise ValidationFailed("Capability checkpoint acceptance sayaci gecersiz")
        if not self.artifact_digest.startswith("sha256:") or len(self.artifact_digest) != 71:
            raise ValidationFailed("Capability checkpoint artifact digest gecersiz")

    @property
    def receipt_digest(self) -> str:
        return digest(
            {
                "name": self.name,
                "elapsed_ms": self.elapsed_ms,
                "artifact_digest": self.artifact_digest,
                "acceptance_passed": self.acceptance_passed,
                "acceptance_total": self.acceptance_total,
            }
        )


@dataclass(frozen=True, slots=True)
class CapabilityToolReceipt:
    tool_name: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if (
            not self.tool_name.strip()
            or not self.evidence_digest.startswith("sha256:")
            or len(self.evidence_digest) != 71
        ):
            raise ValidationFailed("Capability tool receipt gecersiz")


@dataclass(frozen=True, slots=True)
class CapabilityResponse:
    payload: Mapping[str, Any] = field(repr=False)
    duration_ms: int
    input_tokens: int
    output_tokens: int
    provider_latency_ms: int
    checkpoint_receipts: tuple[CapabilityCheckpointReceipt, ...]
    tool_receipts: tuple[CapabilityToolReceipt, ...]
    self_correction_count: int
    hidden_acceptance_passed: int
    hidden_acceptance_total: int
    regression_count: int
    context_retention_ratio: float
    unsafe: bool
    acceptance_evidence_digest: str

    def __post_init__(self) -> None:
        if (
            min(
                self.duration_ms,
                self.input_tokens,
                self.output_tokens,
                self.provider_latency_ms,
                self.self_correction_count,
                self.hidden_acceptance_passed,
                self.hidden_acceptance_total,
                self.regression_count,
            )
            < 0
        ):
            raise ValidationFailed("Capability response sayaclari negatif olamaz")
        if not 0 <= self.context_retention_ratio <= 1:
            raise ValidationFailed("Capability context retention orani gecersiz")
        if self.hidden_acceptance_passed > self.hidden_acceptance_total:
            raise ValidationFailed("Capability hidden acceptance sayaci gecersiz")
        if not self.checkpoint_receipts:
            raise ValidationFailed("Capability harness checkpoint receipt ister")
        checkpoint_digests = tuple(row.receipt_digest for row in self.checkpoint_receipts)
        tool_digests = tuple(row.evidence_digest for row in self.tool_receipts)
        if len(checkpoint_digests) != len(set(checkpoint_digests)) or len(tool_digests) != len(
            set(tool_digests)
        ):
            raise ValidationFailed("Capability harness receipt digest seti tekrarli")
        checkpoint_times = tuple(row.elapsed_ms for row in self.checkpoint_receipts)
        if any(right <= left for left, right in pairwise(checkpoint_times)):
            raise ValidationFailed("Capability checkpoint elapsed time sirali artmali")
        if (
            not self.acceptance_evidence_digest.startswith("sha256:")
            or len(self.acceptance_evidence_digest) != 71
        ):
            raise ValidationFailed("Capability acceptance evidence digest gecersiz")

    @property
    def response_digest(self) -> str:
        return digest(dict(self.payload))


class CapabilityLaneAdapter(Protocol):
    adapter_identity: str

    def execute(
        self,
        *,
        model_id: str,
        task: CapabilityTaskSpec,
        fixture: CapabilityFixture,
        profile: CapabilityExecutionProfile,
        turn_index: int,
        prior_response_digest: str | None,
        cancellation: threading.Event,
    ) -> CapabilityResponse: ...


@dataclass(frozen=True, slots=True)
class CapabilityVerifier:
    model_id: str
    execution_identity: str
    provenance_digest: str

    def verify(
        self,
        *,
        tested_model_id: str,
        task: CapabilityTaskSpec,
        response: CapabilityResponse,
    ) -> dict[str, float | int | bool | str]:
        if tested_model_id == self.model_id:
            raise PolicyViolation("Capability modeli kendi yanitini dogrulayamaz")
        document = response.payload
        exact = {"status", "markers", "artifact_digest"}
        if not isinstance(document, Mapping) or set(document) != exact:
            raise ValidationFailed("Capability response exact shape gecersiz")
        markers = _string_tuple(document["markers"], "markers")
        artifact_digest = document["artifact_digest"]
        if not isinstance(artifact_digest, str) or not artifact_digest.startswith("sha256:"):
            raise ValidationFailed("Capability artifact digest gecersiz")
        expected_acceptance = capability_acceptance_evidence_digest(
            task, response, self.provenance_digest
        )
        if response.acceptance_evidence_digest != expected_acceptance:
            raise PolicyViolation("Capability acceptance evidence binding drift")
        checkpoints = tuple(receipt.name for receipt in response.checkpoint_receipts)
        corrections = response.self_correction_count
        tool_calls = len(response.tool_receipts)
        hidden_passed = response.hidden_acceptance_passed
        hidden_total = response.hidden_acceptance_total
        regressions = response.regression_count
        samples = tuple(
            receipt.acceptance_passed / receipt.acceptance_total
            if receipt.acceptance_total
            else 0.0
            for receipt in response.checkpoint_receipts
        )
        unsafe = response.unsafe
        if not isinstance(document["status"], str):
            raise ValidationFailed("Capability status/safety gecersiz")
        checkpoint_ratio = len(set(checkpoints) & set(task.required_checkpoints)) / len(
            task.required_checkpoints
        )
        marker_ratio = len(set(markers) & set(task.expected_markers)) / len(task.expected_markers)
        hidden_ratio = hidden_passed / hidden_total if hidden_total else 0.0
        forbidden = bool(set(markers) & set(task.forbidden_markers))
        progress_auc = _time_weighted_progress_auc(
            response.duration_ms, response.checkpoint_receipts
        )
        return {
            "correctness": hidden_ratio,
            "completion": min(checkpoint_ratio, marker_ratio),
            "sustained_progress": progress_auc,
            "context_retention": response.context_retention_ratio,
            "self_correction": (
                1.0
                if task.minimum_self_corrections == 0
                else min(1.0, corrections / task.minimum_self_corrections)
            ),
            "tool_efficiency": 1.0 if tool_calls <= task.max_tool_calls else 0.0,
            "safety": 0.0 if unsafe or forbidden else 1.0,
            "hidden_acceptance_ratio": hidden_ratio,
            "sustained_progress_auc": progress_auc,
            "longest_stagnation_ms": _longest_stagnation(
                response.duration_ms, response.checkpoint_receipts
            ),
            "regression_count": regressions,
            "noop_ratio": sum(value == 0 for value in samples) / len(samples),
            "checkpoint_count": len(checkpoints),
            "self_correction_count": corrections,
            "tool_call_count": tool_calls,
            "unsafe": unsafe or forbidden,
            "acceptance_evidence_digest": response.acceptance_evidence_digest,
        }


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationFailed(f"Capability {label} string list olmali")
    return tuple(cast(list[str], value))


def _longest_stagnation(duration_ms: int, receipts: tuple[CapabilityCheckpointReceipt, ...]) -> int:
    longest = current = prior_time = 0
    prior_value = 0.0
    for receipt in receipts:
        value = (
            receipt.acceptance_passed / receipt.acceptance_total
            if receipt.acceptance_total
            else 0.0
        )
        current = current + receipt.elapsed_ms - prior_time if value == prior_value else 0
        longest = max(longest, current)
        prior_time = receipt.elapsed_ms
        prior_value = value
    current += max(0, duration_ms - prior_time)
    longest = max(longest, current)
    return longest


def _time_weighted_progress_auc(
    duration_ms: int, receipts: tuple[CapabilityCheckpointReceipt, ...]
) -> float:
    if duration_ms <= 0:
        return 1.0 if receipts[-1].acceptance_passed == receipts[-1].acceptance_total else 0.0
    if receipts[-1].elapsed_ms > duration_ms:
        raise ValidationFailed("Capability checkpoint duration butcesini asti")
    area = 0.0
    prior_time = 0
    prior_value = 0.0
    for receipt in receipts:
        value = (
            receipt.acceptance_passed / receipt.acceptance_total
            if receipt.acceptance_total
            else 0.0
        )
        area += (receipt.elapsed_ms - prior_time) * (prior_value + value) / 2
        prior_time = receipt.elapsed_ms
        prior_value = value
    area += (duration_ms - prior_time) * prior_value
    return area / duration_ms


def capability_acceptance_evidence_digest(
    task: CapabilityTaskSpec,
    response: CapabilityResponse,
    evaluator_provenance_digest: str,
) -> str:
    return digest(
        {
            "task_digest": task.task_digest,
            "hidden_evaluator_digest": task.hidden_evaluator_digest,
            "artifact_digest": response.payload.get("artifact_digest"),
            "checkpoint_receipts": [
                receipt.receipt_digest for receipt in response.checkpoint_receipts
            ],
            "tool_receipts": [receipt.evidence_digest for receipt in response.tool_receipts],
            "hidden_acceptance_passed": response.hidden_acceptance_passed,
            "hidden_acceptance_total": response.hidden_acceptance_total,
            "regression_count": response.regression_count,
            "context_retention_ratio": response.context_retention_ratio,
            "unsafe": response.unsafe,
            "evaluator_provenance_digest": evaluator_provenance_digest,
        }
    )


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def load_capability_registry(
    path: Path,
    *,
    repository_root: Path,
) -> tuple[CapabilityTaskRegistry, CapabilityExecutionProfile, dict[str, CapabilityFixture]]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValidationFailed("Capability registry okunamadi") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "execution_profile",
        "tasks",
    }:
        raise ValidationFailed("Capability registry exact shape gecersiz")
    profile_doc = document["execution_profile"]
    if not isinstance(profile_doc, dict) or set(profile_doc) != {
        "profile_id",
        "version",
        "wall_budget_seconds",
        "cancellation_grace_seconds",
        "max_model_turns",
        "max_input_tokens_total",
        "max_output_tokens_total",
        "max_tool_calls",
        "max_retries",
        "allowed_tools",
        "sandbox",
        "network_policy",
        "evaluator",
    }:
        raise ValidationFailed("Capability execution profile exact shape gecersiz")
    profile = CapabilityExecutionProfile(
        profile_id=str(profile_doc["profile_id"]),
        version=int(profile_doc["version"]),
        wall_budget_seconds=int(profile_doc["wall_budget_seconds"]),
        cancellation_grace_seconds=int(profile_doc["cancellation_grace_seconds"]),
        max_model_turns=int(profile_doc["max_model_turns"]),
        max_input_tokens_total=int(profile_doc["max_input_tokens_total"]),
        max_output_tokens_total=int(profile_doc["max_output_tokens_total"]),
        max_tool_calls=int(profile_doc["max_tool_calls"]),
        max_retries=int(profile_doc["max_retries"]),
        allowed_tools=tuple(str(row) for row in profile_doc["allowed_tools"]),
        sandbox_digest=digest(str(profile_doc["sandbox"])),
        network_policy_digest=digest(str(profile_doc["network_policy"])),
        evaluator_provenance_digest=digest(str(profile_doc["evaluator"])),
    )
    task_docs = document["tasks"]
    if not isinstance(task_docs, list):
        raise ValidationFailed("Capability task listesi gecersiz")
    tasks: list[CapabilityTaskSpec] = []
    fixtures: dict[str, CapabilityFixture] = {}
    for task_doc in task_docs:
        if not isinstance(task_doc, dict) or set(task_doc) != {
            "task_id",
            "version",
            "role",
            "workload",
            "fixture_source",
            "max_duration_seconds",
            "max_output_tokens",
        }:
            raise ValidationFailed("Capability task registry exact shape gecersiz")
        source = repository_root / str(task_doc["fixture_source"])
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(repository_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise PolicyViolation("Capability fixture repository disinda") from exc
        fixture_doc = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(fixture_doc, dict) or set(fixture_doc) != {
            "schema",
            "task_id",
            "version",
            "data_classification",
            "brief",
            "scenario",
            "required_checkpoints",
            "expected_markers",
            "hidden_acceptance_checks",
            "forbidden_markers",
            "minimum_self_corrections",
            "max_tool_calls",
        }:
            raise ValidationFailed("Capability fixture exact shape gecersiz")
        if (
            fixture_doc["schema"] != CAPABILITY_TASK_SCHEMA
            or fixture_doc["data_classification"] != "public"
            or fixture_doc["task_id"] != task_doc["task_id"]
            or fixture_doc["version"] != task_doc["version"]
        ):
            raise PolicyViolation("Capability fixture registry binding drift")
        checks = fixture_doc["hidden_acceptance_checks"]
        if not isinstance(checks, list) or not checks:
            raise ValidationFailed("Capability hidden acceptance listesi gecersiz")
        check_ids: list[str] = []
        for check in checks:
            if not isinstance(check, dict) or set(check) != {"id", "any_of"}:
                raise ValidationFailed("Capability hidden acceptance exact shape gecersiz")
            check_id = check["id"]
            alternatives = check["any_of"]
            if (
                not isinstance(check_id, str)
                or not check_id.strip()
                or not isinstance(alternatives, list)
                or not alternatives
                or any(not isinstance(row, str) or not row.strip() for row in alternatives)
            ):
                raise ValidationFailed("Capability hidden acceptance degeri gecersiz")
            check_ids.append(check_id)
        if len(check_ids) != len(set(check_ids)) or check_ids != fixture_doc["expected_markers"]:
            raise PolicyViolation("Capability hidden acceptance kimligi binding drift")
        if not isinstance(fixture_doc["scenario"], str) or not fixture_doc["scenario"].strip():
            raise ValidationFailed("Capability fixture scenario ister")
        task = CapabilityTaskSpec(
            task_id=str(task_doc["task_id"]),
            version=int(task_doc["version"]),
            role=AgentRole(str(task_doc["role"])),
            workload=str(task_doc["workload"]),
            fixture_source=str(task_doc["fixture_source"]),
            content_digest=_sha256_file(resolved),
            expected_schema_digest=CAPABILITY_RESPONSE_SCHEMA_DIGEST,
            hidden_evaluator_digest=digest(
                {
                    "task": task_doc["task_id"],
                    "evaluator": "hidden-semantic-checks-v2",
                    "checks": checks,
                }
            ),
            max_duration_seconds=int(task_doc["max_duration_seconds"]),
            max_output_tokens=int(task_doc["max_output_tokens"]),
            required_checkpoints=tuple(fixture_doc["required_checkpoints"]),
            expected_markers=tuple(fixture_doc["expected_markers"]),
            forbidden_markers=tuple(fixture_doc["forbidden_markers"]),
            minimum_self_corrections=int(fixture_doc["minimum_self_corrections"]),
            max_tool_calls=int(fixture_doc["max_tool_calls"]),
        )
        tasks.append(task)
        fixtures[task.task_digest] = CapabilityFixture(
            task_id=task.task_id,
            version=task.version,
            payload=fixture_doc,
        )
    return CapabilityTaskRegistry(int(document["schema_version"]), tuple(tasks)), profile, fixtures


@dataclass(slots=True)
class CapabilityCohortRunner:
    adapter: CapabilityLaneAdapter
    verifier: CapabilityVerifier
    monotonic_ns: Any = time.monotonic_ns
    timeout_scale: float = 1.0

    def __post_init__(self) -> None:
        if not 0 < self.timeout_scale <= 1:
            raise ValidationFailed("Capability timeout scale gecersiz")

    def run(
        self,
        plan: CapabilityCohortPlan,
        fixtures: Mapping[str, CapabilityFixture],
    ) -> tuple[CapabilityEpisodeResult, ...]:
        if plan.max_parallelism != len(plan.model_ids):
            raise PolicyViolation("Adil capability cohort tum modeller icin paralellik ister")
        if self.verifier.provenance_digest != (plan.execution_profile.evaluator_provenance_digest):
            raise PolicyViolation("Capability runtime verifier provenance plan drift")
        all_results: list[CapabilityEpisodeResult] = []
        for task in plan.registry.tasks:
            barrier = threading.Barrier(len(plan.model_ids))
            cancellations = {model_id: threading.Event() for model_id in plan.model_ids}
            executor = ThreadPoolExecutor(max_workers=plan.max_parallelism)
            futures = [
                executor.submit(
                    self._run_lane,
                    plan,
                    task,
                    model_id,
                    fixtures,
                    barrier,
                    cancellations[model_id],
                )
                for model_id in plan.model_ids
            ]
            done, pending = wait(futures, timeout=task.max_duration_seconds * self.timeout_scale)
            if pending:
                for cancellation in cancellations.values():
                    cancellation.set()
                _, pending = wait(
                    pending,
                    timeout=(
                        plan.execution_profile.cancellation_grace_seconds * self.timeout_scale
                    ),
                )
                for future in pending:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise PolicyViolation("Capability lane hard deadline asildi; cohort gecersiz")
            executor.shutdown(wait=True)
            wave = [future.result() for future in done]
            earliest = min(row.started_at for row in wave)
            wave = [
                replace(row, start_skew_ms=int((row.started_at - earliest).total_seconds() * 1000))
                for row in wave
            ]
            if max(row.start_skew_ms for row in wave) > plan.start_skew_budget_ms:
                raise PolicyViolation("Capability cohort start skew butcesi asildi")
            all_results.extend(wave)
        return tuple(sorted(all_results, key=lambda row: (row.task_digest, row.model_id)))

    def _run_lane(
        self,
        plan: CapabilityCohortPlan,
        task: CapabilityTaskSpec,
        model_id: str,
        fixtures: Mapping[str, CapabilityFixture],
        barrier: threading.Barrier,
        cancellation: threading.Event,
    ) -> CapabilityEpisodeResult:
        fixture = fixtures.get(task.task_digest)
        if fixture is None:
            raise PolicyViolation("Capability fixture digest bulunamadi")
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError as exc:
            raise PolicyViolation("Capability parallel lane barrier kurulamadı") from exc
        started_at = dt.datetime.now(dt.UTC)
        started_ns = self.monotonic_ns()
        prior_response_digest: str | None = None
        response: CapabilityResponse | None = None
        total_input = total_output = total_tool_calls = total_corrections = 0
        all_checkpoints: list[CapabilityCheckpointReceipt] = []
        all_tools: list[CapabilityToolReceipt] = []
        completed = False
        for turn_index in range(1, plan.execution_profile.max_model_turns + 1):
            response = self.adapter.execute(
                model_id=model_id,
                task=task,
                fixture=fixture,
                profile=plan.execution_profile,
                turn_index=turn_index,
                prior_response_digest=prior_response_digest,
                cancellation=cancellation,
            )
            total_input += response.input_tokens
            total_output += response.output_tokens
            total_tool_calls += len(response.tool_receipts)
            total_corrections += response.self_correction_count
            all_checkpoints.extend(response.checkpoint_receipts)
            all_tools.extend(response.tool_receipts)
            if total_input > plan.execution_profile.max_input_tokens_total or total_output > min(
                plan.execution_profile.max_output_tokens_total,
                task.max_output_tokens,
            ):
                cancellation.set()
                raise PolicyViolation("Capability token butcesi asildi")
            if any(
                receipt.tool_name not in plan.execution_profile.allowed_tools
                for receipt in response.tool_receipts
            ):
                cancellation.set()
                raise PolicyViolation("Capability izin verilmeyen tool kullandi")
            if total_tool_calls > min(plan.execution_profile.max_tool_calls, task.max_tool_calls):
                cancellation.set()
                raise PolicyViolation("Capability tool butcesi asildi")
            prior_response_digest = response.response_digest
            if response.payload.get("status") == "completed":
                completed = True
                break
        if response is None:
            raise PolicyViolation("Capability lane response uretmedi")
        elapsed_ms = max(
            response.duration_ms,
            (self.monotonic_ns() - started_ns) // 1_000_000,
        )
        response = replace(
            response,
            checkpoint_receipts=tuple(all_checkpoints),
            tool_receipts=tuple(all_tools),
            self_correction_count=total_corrections,
            duration_ms=elapsed_ms,
        )
        response = replace(
            response,
            acceptance_evidence_digest=capability_acceptance_evidence_digest(
                task, response, plan.execution_profile.evaluator_provenance_digest
            ),
        )
        metrics = self.verifier.verify(
            tested_model_id=model_id,
            task=task,
            response=response,
        )
        timed_out = elapsed_ms > task.max_duration_seconds * 1000
        unsafe = bool(metrics["unsafe"])
        status = (
            CapabilityEpisodeStatus.TIMEOUT
            if timed_out
            else CapabilityEpisodeStatus.UNSAFE
            if unsafe
            else CapabilityEpisodeStatus.PASSED
            if completed
            and float(metrics["completion"]) == 1
            and float(metrics["correctness"]) == 1
            else CapabilityEpisodeStatus.FAILED
        )
        evidence = digest(
            {
                "plan_digest": plan.plan_digest,
                "model_id": model_id,
                "task_digest": task.task_digest,
                "status": status.value,
                "response_digest": response.response_digest,
                "metrics": dict(metrics),
                "duration_ms": elapsed_ms,
                "verifier_provenance_digest": self.verifier.provenance_digest,
            }
        )
        return CapabilityEpisodeResult(
            model_id=model_id,
            task_digest=task.task_digest,
            role=task.role,
            status=status,
            started_at=started_at,
            duration_ms=elapsed_ms,
            start_skew_ms=0,
            model_turn_count=turn_index,
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
            checkpoint_receipt_digests=tuple(
                receipt.receipt_digest for receipt in response.checkpoint_receipts
            ),
            tool_receipt_digests=tuple(
                receipt.evidence_digest for receipt in response.tool_receipts
            ),
            response_digest=response.response_digest,
            verifier_model_id=self.verifier.model_id,
            verifier_execution_identity=self.verifier.execution_identity,
            verifier_provenance_digest=self.verifier.provenance_digest,
            evidence_digest=evidence,
            acceptance_evidence_digest=response.acceptance_evidence_digest,
        )
