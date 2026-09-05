"""Benchmark fixture/suite hazirlama ve durable execution koordinasyonu."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID

import yaml

from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
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
from zekam.domain.runtime import ClaimedWork, EffectClaim, FailureCategory
from zekam.domain.security import Authorization, AuthorizationState

if TYPE_CHECKING:
    from zekam.application.execution import ExecutionHost

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


def _exact_bool(document: dict[str, Any], key: str) -> bool:
    value = document[key]
    if type(value) is not bool:
        raise ValidationFailed(f"Benchmark {key} bool olmali")
    return value


def _exact_text(document: dict[str, Any], key: str) -> str:
    value = document[key]
    if type(value) is not str:
        raise ValidationFailed(f"Benchmark {key} metin olmali")
    return value


def _exact_int(document: dict[str, Any], key: str, *, default: int | None = None) -> int:
    value = document.get(key, default)
    if type(value) is not int:
        raise ValidationFailed(f"Benchmark {key} integer olmali")
    return value


def _exact_float(document: dict[str, Any], key: str, *, default: float | None = None) -> float:
    value = document.get(key, default)
    if type(value) is not float or not math.isfinite(value):
        raise ValidationFailed(f"Benchmark {key} sonlu float olmali")
    return value


def trial_from_mapping(document: dict[str, Any]) -> TrialResult:
    if type(document) is not dict:
        raise ValidationFailed("Benchmark trial nesne olmali")
    actual = document.get("actual_cost")
    if actual is not None and (type(actual) is not float or not math.isfinite(actual)):
        raise ValidationFailed("Benchmark actual_cost sonlu float olmali")
    return TrialResult(
        fixture_digest=_exact_text(document, "fixture_digest"),
        repetition=_exact_int(document, "repetition"),
        status=TrialStatus(_exact_text(document, "status")),
        parse_ok=_exact_bool(document, "parse_ok"),
        format_ok=_exact_bool(document, "format_ok"),
        evidence_ok=_exact_bool(document, "evidence_ok"),
        verifier_approved=_exact_bool(document, "verifier_approved"),
        quality=_exact_float(document, "quality"),
        reliability=_exact_float(document, "reliability"),
        latency_ms=_exact_int(document, "latency_ms"),
        input_tokens=_exact_int(document, "input_tokens"),
        output_tokens=_exact_int(document, "output_tokens"),
        retry_count=_exact_int(document, "retry_count", default=0),
        human_corrections=_exact_int(document, "human_corrections", default=0),
        estimated_cost=_exact_float(document, "estimated_cost", default=0.0),
        actual_cost=actual,
        response_digest=_exact_text(document, "response_digest"),
        evidence_digest=_exact_text(document, "evidence_digest"),
        failure_category=(
            None
            if document.get("failure_category") is None
            else _exact_text(document, "failure_category")
        ),
        tool_correctness=_exact_float(document, "tool_correctness", default=0.0),
        recovery=_exact_float(document, "recovery", default=0.0),
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


class BenchmarkArtifactSink(Protocol):
    def store_artifact(self, kind: str, payload: bytes) -> str: ...

    def bind_artifacts(
        self, *, response_digest: str, raw_digest: str, normalized_digest: str
    ) -> str: ...


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

    def retain_failure(
        self,
        *,
        plan_id: UUID,
        claim_id: UUID,
        fixture_digest: str,
        repetition: int,
        phase: str,
        category: str,
        result: TrialResult | None = None,
    ) -> str: ...


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

    def retain_failure(
        self,
        *,
        plan_id: UUID,
        claim_id: UUID,
        fixture_digest: str,
        repetition: int,
        phase: str,
        category: str,
        result: TrialResult | None = None,
    ) -> str:
        del plan_id, fixture_digest, repetition, phase, result
        claim = self._claims.get(claim_id)
        if claim is None:
            raise PolicyViolation("Benchmark failure claim gateway identity eslesmedi")
        try:
            failure = FailureCategory(category)
        except ValueError:
            failure = FailureCategory.ADAPTER
        evidence = digest({"claim_id": str(claim_id), "category": failure.value})
        self.host.record_failure(claim, category=failure, failure_digest=evidence)
        return evidence


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


@dataclass(frozen=True, slots=True)
class _JsonProcessResult:
    document: dict[str, Any]
    stdout: bytes


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)


def _bounded_process(
    argv: tuple[str, ...],
    request: bytes,
    timeout: int,
    *,
    read_allow_roots: tuple[Path, ...] = (),
) -> tuple[int, bytes, bytes]:
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    try:
        process = subprocess.Popen(
            _sandboxed_argv(argv, read_allow_roots),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env={"NO_COLOR": "1", "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
            start_new_session=True,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise PolicyViolation("Local benchmark process pipes unavailable")
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        offset = 0
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in selector.select(min(remaining, 0.1)):
                if key.data == "stdin":
                    try:
                        written = os.write(key.fd, request[offset : offset + 65_536])
                    except BrokenPipeError:
                        written = 0
                        offset = len(request)
                    offset += written
                    if offset >= len(request):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                    continue
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                target, cap = (stdout, 1_000_000) if key.data == "stdout" else (stderr, 16_384)
                target.extend(chunk)
                if len(target) > cap:
                    raise PolicyViolation("Local benchmark adapter bounded output exceeded")
            if process.poll() is not None:
                stdin_key = next(
                    (key for key in selector.get_map().values() if key.data == "stdin"), None
                )
                if stdin_key is not None:
                    selector.unregister(process.stdin)
                    process.stdin.close()
        return (
            process.wait(timeout=max(0.1, deadline - time.monotonic())),
            bytes(stdout),
            bytes(stderr),
        )
    except BaseException:
        if process is not None:
            _terminate_process(process)
        raise
    finally:
        selector.close()
        if process is not None:
            for final_stream in (process.stdin, process.stdout, process.stderr):
                if final_stream is not None and not final_stream.closed:
                    final_stream.close()


def _sandboxed_argv(argv: tuple[str, ...], read_allow_roots: tuple[Path, ...]) -> tuple[str, ...]:
    """Wrap a Mac benchmark command in a deny-write/deny-network OS sandbox."""
    if sys.platform != "darwin":
        raise PolicyViolation("Local benchmark OS sandbox is only accepted on macOS")
    sandbox = Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file():
        raise PolicyViolation("Local benchmark sandbox executable unavailable")
    home = Path.home().resolve(strict=True)
    executable = Path(argv[0])
    if not executable.is_absolute() or not executable.is_file():
        raise PolicyViolation("Local adapter executable absolute normal dosya olmali")
    allowed: set[Path] = set()
    for item in read_allow_roots:
        if not isinstance(item, Path) or not item.is_absolute() or item.is_symlink():
            raise PolicyViolation("Local benchmark sandbox read root invalid")
        resolved = item.resolve(strict=True)
        if resolved == home:
            raise PolicyViolation("Local benchmark sandbox cannot allow the whole user home")
        allowed.add(resolved)
    resolved_executable = executable.resolve(strict=True)
    if executable.parent.name in {"bin", "Scripts"}:
        allowed.add(executable.parent.parent.resolve(strict=True))
    elif resolved_executable.is_relative_to(home):
        allowed.add(resolved_executable.parent)
    for argument in argv[1:]:
        candidate = Path(argument)
        if candidate.is_absolute() and candidate.is_file() and not candidate.is_symlink():
            allowed.add(candidate.resolve(strict=True))

    def denied_home_entries(directory: Path) -> tuple[Path, ...]:
        denied: list[Path] = []
        try:
            children = tuple(directory.iterdir())
        except OSError as exc:
            raise PolicyViolation("Local benchmark home read policy kurulamadı") from exc
        for child in children:
            resolved_child = child.resolve(strict=False)
            exact_allowed = any(path == resolved_child for path in allowed)
            allowed_descendant = any(
                directory == path or directory in path.parents for path in allowed
            )
            child_has_allowed_descendant = any(resolved_child in path.parents for path in allowed)
            if exact_allowed:
                continue
            if child_has_allowed_descendant and child.is_dir() and not child.is_symlink():
                denied.extend(denied_home_entries(child))
                continue
            if not allowed_descendant or not child_has_allowed_descendant:
                denied.append(child)
        return tuple(denied)

    def quoted(value: Path) -> str:
        return json.dumps(str(value))

    # CPython/Homebrew launch-time services abort under a deny-default profile.
    # Keep launch/read compatibility, but deny every filesystem mutation and all
    # network access at the kernel sandbox boundary. Declared roots remain
    # validated and become part of the adapter contract; they never grant write.
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(deny network*)",
    ]
    for path in denied_home_entries(home):
        operation = "subpath" if path.is_dir() and not path.is_symlink() else "literal"
        rules.append(f"(deny file-read* ({operation} {quoted(path)}))")
    return (str(sandbox), "-p", " ".join(rules), *argv)


def _run_json_process(
    argv: tuple[str, ...],
    payload: dict[str, Any],
    timeout: int,
    *,
    read_allow_roots: tuple[Path, ...] = (),
) -> _JsonProcessResult:
    if not argv or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file():
        raise PolicyViolation("Local adapter executable absolute normal dosya olmali")
    if timeout < 1 or timeout > 600:
        raise PolicyViolation("Local adapter timeout 1..600 saniye olmali")
    request = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    if not 0 < len(request) <= 1_048_576:
        raise PolicyViolation("Local benchmark adapter request size invalid")
    try:
        returncode, stdout, _stderr = _bounded_process(
            argv, request, timeout, read_allow_roots=read_allow_roots
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PolicyViolation("Local benchmark adapter calistirilamadi") from exc
    if returncode != 0:
        raise PolicyViolation("Local benchmark adapter sanitized failure")
    try:
        document = json.loads(
            stdout.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite {value}")),
        )
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise ValidationFailed("Local benchmark adapter JSON contract gecersiz") from exc
    if not isinstance(document, dict):
        raise ValidationFailed("Local benchmark adapter JSON nesnesi dondurmeli")
    return _JsonProcessResult(document, stdout)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _adapter_failure_category(exc: Exception) -> str:
    cause = exc.__cause__
    if isinstance(exc, subprocess.TimeoutExpired) or isinstance(cause, subprocess.TimeoutExpired):
        return FailureCategory.TIMEOUT.value
    if isinstance(exc, ValidationFailed):
        return FailureCategory.VALIDATION.value
    if isinstance(exc, PolicyViolation):
        return FailureCategory.ADAPTER.value
    return FailureCategory.INTERNAL.value


@dataclass(frozen=True, slots=True)
class LocalProcessBenchmarkAdapter:
    """Gercek tested model icin shell'siz, typed JSON stdin/stdout process siniri."""

    routed_model_id: str
    argv: tuple[str, ...]
    oracle: DeterministicLocalBenchmarkAdapter
    invocation_audit: Callable[[str, str, str], None]
    timeout_seconds: int = 60
    artifact_sink: BenchmarkArtifactSink | None = None
    read_allow_roots: tuple[Path, ...] = ()

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
        request = {
            "schema": "zekam-benchmark-tested-request/v1",
            "model_id": self.model_id,
            "fixture": artifact,
            "fixture_digest": fixture.fixture_digest,
            "repetition": repetition,
        }
        self.invocation_audit(
            "tested", digest({"phase": "tested", "model_id": self.model_id}), digest(request)
        )
        process_result = _run_json_process(
            self.argv,
            request,
            self.timeout_seconds,
            read_allow_roots=self.read_allow_roots,
        )
        output = process_result.document
        required = {
            "schema",
            "model_id",
            "status",
            "parse_ok",
            "format_ok",
            "evidence_ok",
            "quality",
            "reliability",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "actual_cost",
            "tool_correctness",
            "recovery",
            "response",
        }
        optional = {
            "retry_count",
            "human_corrections",
            "estimated_cost",
            "failure_category",
        }
        if (
            output.get("schema") != "zekam-benchmark-tested-result/v1"
            or not required <= set(output) <= required | optional
        ):
            raise ValidationFailed("Tested adapter response schema gecersiz")
        if output.get("model_id") != self.model_id:
            raise PolicyViolation("Tested adapter model identity drift")
        response_bytes = canonical_bytes(output.get("response"))
        response_digest = digest_of_bytes(response_bytes)
        if self.artifact_sink is not None:
            raw_digest = self.artifact_sink.store_artifact("raw", process_result.stdout)
            normalized_digest = self.artifact_sink.store_artifact("normalized", response_bytes)
            if normalized_digest != response_digest:
                raise PolicyViolation("Benchmark normalized artifact digest drift")
            self.artifact_sink.bind_artifacts(
                response_digest=response_digest,
                raw_digest=raw_digest,
                normalized_digest=normalized_digest,
            )
        return TrialResult(
            fixture_digest=fixture.fixture_digest,
            repetition=repetition,
            status=TrialStatus(_exact_text(output, "status")),
            parse_ok=_exact_bool(output, "parse_ok"),
            format_ok=_exact_bool(output, "format_ok"),
            evidence_ok=_exact_bool(output, "evidence_ok"),
            verifier_approved=False,
            quality=_exact_float(output, "quality"),
            reliability=_exact_float(output, "reliability"),
            latency_ms=_exact_int(output, "latency_ms"),
            input_tokens=_exact_int(output, "input_tokens"),
            output_tokens=_exact_int(output, "output_tokens"),
            retry_count=_exact_int(output, "retry_count", default=0),
            human_corrections=_exact_int(output, "human_corrections", default=0),
            estimated_cost=_exact_float(output, "estimated_cost", default=0.0),
            actual_cost=_exact_float(output, "actual_cost", default=0.0),
            tool_correctness=_exact_float(output, "tool_correctness"),
            recovery=_exact_float(output, "recovery"),
            response_digest=response_digest,
            evidence_digest=digest({"adapter": self.adapter_digest, "response": response_digest}),
            failure_category=output.get("failure_category"),
        )


@dataclass(frozen=True, slots=True)
class LocalProcessBenchmarkVerifier:
    identity: VerifierIdentity
    argv: tuple[str, ...]
    invocation_audit: Callable[[str, str, str], None]
    timeout_seconds: int = 60
    artifact_sink: BenchmarkArtifactSink | None = None
    read_allow_roots: tuple[Path, ...] = ()

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
        request = {
            "schema": "zekam-benchmark-verifier-request/v1",
            "tested_model_id": plan.model_id,
            "verifier_model_id": self.verifier.model_id,
            "tested_response_digest": result.response_digest,
            "fixture_digest": fixture.fixture_digest,
        }
        self.invocation_audit(
            "verifier",
            digest({"phase": "verifier", "model_id": self.verifier.model_id}),
            digest(request),
        )
        process_result = _run_json_process(
            self.argv,
            request,
            self.timeout_seconds,
            read_allow_roots=self.read_allow_roots,
        )
        output = process_result.document
        if output.get("schema") != "zekam-benchmark-verifier-result/v1" or set(output) != {
            "schema",
            "tested_model_id",
            "verifier_model_id",
            "tested_response_digest",
            "approved",
            "evidence",
        }:
            raise ValidationFailed("Verifier response schema gecersiz")
        expected = (plan.model_id, self.verifier.model_id, result.response_digest)
        actual = (
            output.get("tested_model_id"),
            output.get("verifier_model_id"),
            output.get("tested_response_digest"),
        )
        if actual != expected:
            raise PolicyViolation("Verifier result identity/response binding drift")
        if self.artifact_sink is not None and (
            self.artifact_sink.store_artifact("verifier", process_result.stdout)
            != digest_of_bytes(process_result.stdout)
        ):
            raise PolicyViolation("Verifier artifact sink digest drift")
        evidence = digest(
            {
                "verifier_provenance": self.verifier.provenance_digest,
                "tested_response": result.response_digest,
                "approved": _exact_bool(output, "approved"),
                "verifier_evidence": output.get("evidence"),
            }
        )
        return VerifierVerdict(
            tested_model_id=plan.model_id,
            verifier_model_id=self.verifier.model_id,
            execution_identity=self.verifier.execution_identity,
            tested_response_digest=result.response_digest,
            approved=_exact_bool(output, "approved"),
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
                active_claim_id = tested_claim_id
                active_phase = "tested"
                result: TrialResult | None = None
                try:
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
                    active_claim_id = verifier_claim_id
                    active_phase = "verifier"
                    verdict = verifier_adapter.verify(plan=plan, fixture=fixture, result=result)
                    if (
                        verdict.tested_model_id != plan.model_id
                        or verdict.verifier_model_id != verifier_adapter.verifier.model_id
                        or verdict.execution_identity
                        != verifier_adapter.verifier.execution_identity
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
                except Exception as exc:
                    category = _adapter_failure_category(exc)
                    claims.retain_failure(
                        plan_id=plan_id,
                        claim_id=active_claim_id,
                        fixture_digest=fixture.fixture_digest,
                        repetition=repetition,
                        phase=active_phase,
                        category=category,
                        result=result,
                    )
                    raise PolicyViolation("Benchmark post-claim failure retained") from exc
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
