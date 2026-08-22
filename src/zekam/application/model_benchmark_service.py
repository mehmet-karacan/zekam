"""Benchmark fixture/suite hazirlama ve durable execution koordinasyonu."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

import yaml

from zekam.application.execution import ExecutionHost
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import (
    BenchmarkAggregate,
    BenchmarkFixture,
    BenchmarkPlan,
    BenchmarkSuite,
    ExecutionEligibility,
    FixtureRegistry,
    TrialResult,
    TrialStatus,
    VerifierIdentity,
    VerifierVerdict,
    aggregate_trials,
    benchmark_effect_digest,
    benchmark_verifier_effect_digest,
)
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import EffectClaim
from zekam.domain.security import Authorization, AuthorizationState
from zekam.infrastructure.postgres.runtime_repository import ClaimedWork

FIXTURE_SCHEMA = "zekam-model-benchmark-fixtures/v1"


def default_fixture_file() -> Path:
    from zekam.application.config import core_root

    packaged = core_root() / "config" / "model_benchmark_fixtures.yaml"
    if packaged.is_file():
        return packaged
    return Path(__file__).resolve().parents[1] / "_config" / "model_benchmark_fixtures.yaml"


def load_fixture_registry(path: Path | None = None) -> FixtureRegistry:
    target = path or default_fixture_file()
    if not target.is_file():
        raise ConfigurationError("Model benchmark fixture registry bulunamadi")
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != FIXTURE_SCHEMA:
        raise ValidationFailed("Benchmark fixture registry schema gecersiz")
    rows = document.get("fixtures")
    if not isinstance(rows, list):
        raise ValidationFailed("Benchmark fixture listesi gecersiz")
    fixtures: list[BenchmarkFixture] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationFailed("Benchmark fixture kaydi nesne olmali")
        fixtures.append(
            BenchmarkFixture(
                case_id=str(row["case_id"]),
                version=int(row["version"]),
                workload=str(row["workload"]),
                modality=str(row["modality"]),
                fixture_source=str(row["fixture_source"]),
                execution_eligibility=ExecutionEligibility(str(row["execution_eligibility"])),
                content_digest=str(row["content_digest"]),
                expected_schema_digest=str(row["expected_schema_digest"]),
                tags=tuple(str(item) for item in row.get("tags", [])),
            )
        )
    registry = FixtureRegistry(
        schema_version=int(document["schema_version"]), fixtures=tuple(fixtures)
    )
    allow_root = target.parent.resolve(strict=True)
    for fixture in registry.fixtures:
        resolve_fixture_artifact(fixture, allow_root=allow_root)
    return registry


def resolve_fixture_artifact(fixture: BenchmarkFixture, *, allow_root: Path) -> Path:
    """Logical fixture source'u canonical allow-root icinde, symlink'siz cozer."""
    root = allow_root.resolve(strict=True)
    candidate = root / fixture.fixture_source
    if any(part.is_symlink() for part in (candidate, *candidate.parents) if part != root.parent):
        raise PolicyViolation("Fixture source symlink olamaz")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise PolicyViolation("Fixture source allow-root disina cikamaz") from exc
    if not resolved.is_file():
        raise PolicyViolation("Fixture source normal dosya olmali")
    if digest_of_bytes(resolved.read_bytes()) != fixture.content_digest:
        raise PolicyViolation("Fixture source content digest drift")
    return resolved


def trial_from_mapping(document: dict[str, Any]) -> TrialResult:
    return TrialResult(
        fixture_digest=str(document["fixture_digest"]),
        repetition=int(document["repetition"]),
        status=TrialStatus(str(document["status"])),
        parse_ok=bool(document["parse_ok"]),
        format_ok=bool(document["format_ok"]),
        evidence_ok=bool(document["evidence_ok"]),
        verifier_approved=bool(document["verifier_approved"]),
        quality=float(document["quality"]),
        reliability=float(document["reliability"]),
        latency_ms=int(document["latency_ms"]),
        input_tokens=int(document["input_tokens"]),
        output_tokens=int(document["output_tokens"]),
        retry_count=int(document.get("retry_count", 0)),
        human_corrections=int(document.get("human_corrections", 0)),
        estimated_cost=float(document.get("estimated_cost", 0)),
        actual_cost=(
            None if document.get("actual_cost") is None else float(document["actual_cost"])
        ),
        response_digest=str(document["response_digest"]),
        evidence_digest=str(document["evidence_digest"]),
        failure_category=(
            None if document.get("failure_category") is None else str(document["failure_category"])
        ),
    )


class BenchmarkStore(Protocol):
    def ensure_plan(
        self, *, registry: FixtureRegistry, suite: BenchmarkSuite, plan: BenchmarkPlan
    ) -> tuple[UUID, bool]: ...

    def list_trials(self, plan_id: UUID) -> tuple[TrialResult, ...]: ...

    def trial_receipt_matches(
        self,
        *,
        plan_id: UUID,
        tested_claim_id: UUID,
        verifier_claim_id: UUID,
        verdict: VerifierVerdict,
        result: TrialResult,
    ) -> bool: ...

    def record_trial(
        self,
        *,
        plan_id: UUID,
        tested_claim_id: UUID,
        verifier_claim_id: UUID,
        verdict: VerifierVerdict,
        result: TrialResult,
        observed_at: dt.datetime | None = None,
    ) -> tuple[UUID, bool]: ...

    def store_aggregate(self, *, plan_id: UUID, aggregate: BenchmarkAggregate) -> UUID: ...


class BenchmarkAdapter(Protocol):
    """Provider siniri. Ham prompt/yanit repository'ye verilmez."""

    @property
    def execution_mode(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    def invoke(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, repetition: int
    ) -> TrialResult: ...


class BenchmarkVerifierAdapter(Protocol):
    @property
    def execution_mode(self) -> str: ...

    @property
    def verifier(self) -> VerifierIdentity: ...

    def verify(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, result: TrialResult
    ) -> VerifierVerdict: ...


class BenchmarkClaimGateway(Protocol):
    """Adapter cagrisi oncesi claim, sonrasi terminal receipt ureten runtime portu."""

    def claim_tested(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, repetition: int
    ) -> UUID: ...

    def complete_tested(self, *, claim_id: UUID, result: TrialResult) -> None: ...

    def claim_verifier(
        self,
        *,
        plan: BenchmarkPlan,
        fixture: BenchmarkFixture,
        result: TrialResult,
        verifier: VerifierIdentity,
    ) -> UUID: ...

    def complete_verifier(self, *, claim_id: UUID, verdict: VerifierVerdict) -> None: ...


class OutboundBenchmarkGate(Protocol):
    def authorize(self, *, plan: BenchmarkPlan, suite: BenchmarkSuite) -> bool: ...


@dataclass(slots=True)
class RuntimeBenchmarkClaimGateway:
    """Mevcut ExecutionHost lease/fence ve EffectLedger'ini kullanan production gateway."""

    host: ExecutionHost
    work: ClaimedWork
    authorization: Authorization
    adapter_digest: str
    _claims: dict[UUID, EffectClaim]

    def __init__(
        self,
        *,
        host: ExecutionHost,
        work: ClaimedWork,
        authorization: Authorization,
        adapter_digest: str,
    ) -> None:
        if authorization.state is not AuthorizationState.CONSUMED:
            raise PolicyViolation("Benchmark authorization once tuketilmis olmali")
        self.host = host
        self.work = work
        self.authorization = authorization
        self.adapter_digest = adapter_digest
        self._claims = {}

    def claim_tested(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, repetition: int
    ) -> UUID:
        if self.authorization.plan_digest != plan.plan_digest:
            raise PolicyViolation("Authorization benchmark plan digest ile eslesmiyor")
        resource = f"model-benchmark:{plan.model_id}:{plan.suite_digest.removeprefix('sha256:')}"
        claim = self.host.claim_effect(
            self.work,
            operation="model-benchmark-tested",
            effect_digest=benchmark_effect_digest(
                plan.plan_digest, fixture.fixture_digest, repetition
            ),
            authorization_digest=self.authorization.authorization_digest,
            authorization_id=self.authorization.id,
            resources=parse_requests(write=(resource,)),
            adapter_digest=self.adapter_digest,
        )
        self._claims[claim.id] = claim
        return claim.id

    def complete_tested(self, *, claim_id: UUID, result: TrialResult) -> None:
        claim = self._claims.get(claim_id)
        if claim is None:
            raise PolicyViolation("Benchmark claim gateway identity eslesmedi")
        self.host.record_success(
            claim,
            result_digest=result.response_digest,
            adapter_evidence_digest=result.evidence_digest,
            token_count=result.input_tokens + result.output_tokens,
            cost_micros=round(result.cost * 1_000_000),
            latency_ms=result.latency_ms,
        )

    def claim_verifier(
        self,
        *,
        plan: BenchmarkPlan,
        fixture: BenchmarkFixture,
        result: TrialResult,
        verifier: VerifierIdentity,
    ) -> UUID:
        resource = f"model-benchmark:{plan.model_id}:{plan.suite_digest.removeprefix('sha256:')}"
        claim = self.host.claim_effect(
            self.work,
            operation="model-benchmark-verifier",
            effect_digest=benchmark_verifier_effect_digest(
                plan.plan_digest,
                fixture.fixture_digest,
                result.repetition,
                verifier.model_id,
                result.response_digest,
            ),
            authorization_digest=self.authorization.authorization_digest,
            authorization_id=self.authorization.id,
            resources=parse_requests(write=(resource,)),
            adapter_digest=verifier.provenance_digest,
        )
        self._claims[claim.id] = claim
        return claim.id

    def complete_verifier(self, *, claim_id: UUID, verdict: VerifierVerdict) -> None:
        claim = self._claims.get(claim_id)
        if claim is None:
            raise PolicyViolation("Verifier claim gateway identity eslesmedi")
        self.host.record_success(
            claim,
            result_digest=verdict.evidence_digest,
            adapter_evidence_digest=verdict.evidence_digest,
        )


@dataclass(frozen=True, slots=True)
class DeterministicLocalBenchmarkAdapter:
    """Yalniz secret-free fixture contract'ini yukleyen oracle; model sonucu uretmez."""

    allow_root: Path

    @property
    def adapter_digest(self) -> str:
        return digest({"adapter": "deterministic-local-benchmark", "version": 1})

    def load(self, fixture: BenchmarkFixture) -> dict[str, Any]:
        source = resolve_fixture_artifact(fixture, allow_root=self.allow_root)
        document = json.loads(source.read_text(encoding="utf-8"))
        if (
            document.get("schema") != "zekam-local-benchmark-fixture/v1"
            or document.get("case_id") != fixture.case_id
            or int(document.get("version", 0)) != fixture.version
        ):
            raise ValidationFailed("Local fixture artifact contract drift")
        return cast(dict[str, Any], document)


def _run_json_process(
    argv: tuple[str, ...], payload: dict[str, Any], timeout: int
) -> dict[str, Any]:
    if not argv or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file():
        raise PolicyViolation("Local adapter executable absolute normal dosya olmali")
    if timeout < 1 or timeout > 600:
        raise PolicyViolation("Local adapter timeout 1..600 saniye olmali")
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(payload, ensure_ascii=True),
            text=True,
            capture_output=True,
            shell=False,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PolicyViolation("Local benchmark adapter calistirilamadi") from exc
    if completed.returncode != 0 or len(completed.stdout) > 1_000_000:
        raise PolicyViolation("Local benchmark adapter sanitized failure")
    try:
        document = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationFailed("Local benchmark adapter JSON contract gecersiz") from exc
    if not isinstance(document, dict):
        raise ValidationFailed("Local benchmark adapter JSON nesnesi dondurmeli")
    return document


@dataclass(frozen=True, slots=True)
class LocalProcessBenchmarkAdapter:
    """Gercek tested model icin shell'siz, typed JSON stdin/stdout process siniri."""

    routed_model_id: str
    argv: tuple[str, ...]
    oracle: DeterministicLocalBenchmarkAdapter
    timeout_seconds: int = 60

    @property
    def model_id(self) -> str:
        return self.routed_model_id

    @property
    def execution_mode(self) -> str:
        return "local"

    @property
    def adapter_digest(self) -> str:
        return digest({"adapter": "local-process-tested", "model_id": self.model_id, "v": 1})

    def invoke(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, repetition: int
    ) -> TrialResult:
        if plan.model_id != self.model_id or plan.remote_execution:
            raise PolicyViolation("Tested adapter actual model route eslesmiyor")
        artifact = self.oracle.load(fixture)
        output = _run_json_process(
            self.argv,
            {
                "schema": "zekam-benchmark-tested-request/v1",
                "model_id": self.model_id,
                "fixture": artifact,
                "fixture_digest": fixture.fixture_digest,
                "repetition": repetition,
            },
            self.timeout_seconds,
        )
        if output.get("schema") != "zekam-benchmark-tested-result/v1":
            raise ValidationFailed("Tested adapter response schema gecersiz")
        if output.get("model_id") != self.model_id:
            raise PolicyViolation("Tested adapter model identity drift")
        response_digest = digest(output.get("response"))
        return TrialResult(
            fixture_digest=fixture.fixture_digest,
            repetition=repetition,
            status=TrialStatus(str(output["status"])),
            parse_ok=bool(output["parse_ok"]),
            format_ok=bool(output["format_ok"]),
            evidence_ok=bool(output["evidence_ok"]),
            verifier_approved=False,
            quality=float(output["quality"]),
            reliability=float(output["reliability"]),
            latency_ms=int(output["latency_ms"]),
            input_tokens=int(output["input_tokens"]),
            output_tokens=int(output["output_tokens"]),
            retry_count=int(output.get("retry_count", 0)),
            human_corrections=int(output.get("human_corrections", 0)),
            estimated_cost=float(output.get("estimated_cost", 0)),
            actual_cost=float(output.get("actual_cost", 0)),
            response_digest=response_digest,
            evidence_digest=digest({"adapter": self.adapter_digest, "response": response_digest}),
            failure_category=output.get("failure_category"),
        )


@dataclass(frozen=True, slots=True)
class LocalProcessBenchmarkVerifier:
    identity: VerifierIdentity
    argv: tuple[str, ...]
    timeout_seconds: int = 60

    @property
    def verifier(self) -> VerifierIdentity:
        return self.identity

    @property
    def execution_mode(self) -> str:
        return "local"

    def verify(
        self, *, plan: BenchmarkPlan, fixture: BenchmarkFixture, result: TrialResult
    ) -> VerifierVerdict:
        if self.verifier.model_id == plan.model_id:
            raise PolicyViolation("Tested model kendi verifier'i olamaz")
        output = _run_json_process(
            self.argv,
            {
                "schema": "zekam-benchmark-verifier-request/v1",
                "tested_model_id": plan.model_id,
                "verifier_model_id": self.verifier.model_id,
                "tested_response_digest": result.response_digest,
                "fixture_digest": fixture.fixture_digest,
            },
            self.timeout_seconds,
        )
        if output.get("schema") != "zekam-benchmark-verifier-result/v1":
            raise ValidationFailed("Verifier response schema gecersiz")
        expected = (plan.model_id, self.verifier.model_id, result.response_digest)
        actual = (
            output.get("tested_model_id"),
            output.get("verifier_model_id"),
            output.get("tested_response_digest"),
        )
        if actual != expected:
            raise PolicyViolation("Verifier result identity/response binding drift")
        evidence = digest(
            {
                "verifier_provenance": self.verifier.provenance_digest,
                "tested_response": result.response_digest,
                "approved": bool(output["approved"]),
                "verifier_evidence": output.get("evidence"),
            }
        )
        return VerifierVerdict(
            tested_model_id=plan.model_id,
            verifier_model_id=self.verifier.model_id,
            execution_identity=self.verifier.execution_identity,
            tested_response_digest=result.response_digest,
            approved=bool(output["approved"]),
            evidence_digest=evidence,
        )


@dataclass(frozen=True, slots=True)
class BenchmarkExecutionService:
    """Claim-bound trial'lari idempotent kaydeder ve aggregate eder."""

    repository: BenchmarkStore
    registry: FixtureRegistry

    def prepare(self, suite: BenchmarkSuite, plan: BenchmarkPlan) -> tuple[UUID, bool]:
        """Ayni plan digest'i varsa kanonik kaydi dondurur; provider cagrisi yapmaz."""
        if plan.suite_digest != suite.suite_digest:
            raise PolicyViolation("Benchmark plan suite digest stale")
        if plan.fixture_registry_digest != self.registry.registry_digest:
            raise PolicyViolation("Benchmark plan fixture registry digest stale")
        return self.repository.ensure_plan(registry=self.registry, suite=suite, plan=plan)

    def record_trial(
        self,
        plan_id: UUID,
        *,
        tested_claim_id: UUID,
        verifier_claim_id: UUID,
        verdict: VerifierVerdict,
        result: TrialResult,
        observed_at: dt.datetime | None = None,
    ) -> tuple[UUID, bool]:
        if not self.repository.trial_receipt_matches(
            plan_id=plan_id,
            tested_claim_id=tested_claim_id,
            verifier_claim_id=verifier_claim_id,
            verdict=verdict,
            result=result,
        ):
            raise PolicyViolation("Benchmark trial exact plan/effect/receipt evidence ister")
        return self.repository.record_trial(
            plan_id=plan_id,
            tested_claim_id=tested_claim_id,
            verifier_claim_id=verifier_claim_id,
            verdict=verdict,
            result=result,
            observed_at=observed_at,
        )

    def execute(
        self,
        *,
        suite: BenchmarkSuite,
        plan: BenchmarkPlan,
        adapter: BenchmarkAdapter,
        verifier_adapter: BenchmarkVerifierAdapter,
        claims: BenchmarkClaimGateway,
        outbound_gate: OutboundBenchmarkGate | None = None,
    ) -> tuple[UUID, tuple[TrialResult, ...]]:
        """Claim-before-call uygular; mevcut fixture/repetition adapter'e tekrar gitmez."""
        fixtures_by_digest = {item.fixture_digest: item for item in self.registry.fixtures}
        try:
            fixtures = tuple(fixtures_by_digest[value] for value in suite.fixture_digests)
        except KeyError as exc:
            raise PolicyViolation("Suite registry disi fixture digest tasiyor") from exc
        if plan.remote_execution and any(
            item.execution_eligibility is ExecutionEligibility.LOCAL_ONLY for item in fixtures
        ):
            raise PolicyViolation("Local-only fixture remote execution'a acilamaz")
        if plan.remote_execution != (adapter.execution_mode == "remote"):
            raise PolicyViolation("Benchmark plan ve adapter execution mode eslesmiyor")
        if adapter.execution_mode == "remote" and (
            outbound_gate is None or not outbound_gate.authorize(plan=plan, suite=suite)
        ):
            raise PolicyViolation("Remote benchmark outbound/provider authorization ister")
        if adapter.execution_mode not in {"local", "remote"}:
            raise PolicyViolation("Benchmark adapter execution mode gecersiz")
        if adapter.model_id != plan.model_id:
            raise PolicyViolation("Benchmark adapter tested model route eslesmiyor")
        if verifier_adapter.verifier.model_id == plan.model_id:
            raise PolicyViolation("Tested model kendi verifier'i olamaz")
        if verifier_adapter.execution_mode != adapter.execution_mode:
            raise PolicyViolation("Tested ve verifier execution mode eslesmiyor")
        plan_id, _ = self.prepare(suite, plan)
        existing = {
            (item.fixture_digest, item.repetition): item
            for item in self.repository.list_trials(plan_id)
        }
        for fixture in fixtures:
            for repetition in range(1, plan.repetitions + 1):
                key = (fixture.fixture_digest, repetition)
                if key in existing:
                    continue
                tested_claim_id = claims.claim_tested(
                    plan=plan, fixture=fixture, repetition=repetition
                )
                result = adapter.invoke(plan=plan, fixture=fixture, repetition=repetition)
                if (
                    result.fixture_digest != fixture.fixture_digest
                    or result.repetition != repetition
                ):
                    raise PolicyViolation("Adapter trial fixture/repetition drift")
                claims.complete_tested(claim_id=tested_claim_id, result=result)
                verifier_claim_id = claims.claim_verifier(
                    plan=plan,
                    fixture=fixture,
                    result=result,
                    verifier=verifier_adapter.verifier,
                )
                verdict = verifier_adapter.verify(plan=plan, fixture=fixture, result=result)
                if (
                    verdict.tested_model_id != plan.model_id
                    or verdict.verifier_model_id != verifier_adapter.verifier.model_id
                    or verdict.execution_identity != verifier_adapter.verifier.execution_identity
                    or verdict.tested_response_digest != result.response_digest
                ):
                    raise PolicyViolation("Verifier verdict canonical identity binding drift")
                claims.complete_verifier(claim_id=verifier_claim_id, verdict=verdict)
                result = replace(
                    result,
                    verifier_approved=verdict.approved,
                    evidence_digest=digest(
                        {
                            "tested": result.evidence_digest,
                            "verifier": verdict.evidence_digest,
                        }
                    ),
                )
                self.record_trial(
                    plan_id,
                    tested_claim_id=tested_claim_id,
                    verifier_claim_id=verifier_claim_id,
                    verdict=verdict,
                    result=result,
                )
                existing[key] = result
        return plan_id, tuple(existing[key] for key in sorted(existing))

    def aggregate(
        self,
        plan_id: UUID,
        *,
        plan: BenchmarkPlan,
        suite: BenchmarkSuite,
        tested_model_id: str,
        verifier: VerifierIdentity,
    ) -> BenchmarkAggregate:
        trials = self.repository.list_trials(plan_id)
        expected = {
            (fixture_digest, repetition)
            for fixture_digest in suite.fixture_digests
            for repetition in range(1, plan.repetitions + 1)
        }
        actual = {(trial.fixture_digest, trial.repetition) for trial in trials}
        if len(actual) != len(trials) or actual != expected:
            raise PolicyViolation("Aggregate her fixture icin exact repetition seti ister")
        if any(not trial.valid for trial in trials):
            raise PolicyViolation("Aggregate tum suite trial'larinin valid olmasini ister")
        aggregate = aggregate_trials(trials, tested_model_id=tested_model_id, verifier=verifier)
        self.repository.store_aggregate(plan_id=plan_id, aggregate=aggregate)
        return aggregate
