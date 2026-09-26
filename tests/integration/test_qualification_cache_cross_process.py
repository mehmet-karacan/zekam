"""Cross-process warm qualification-cache E2E (ZEKAM-RAG-PERFORMANCE-CORRECTNESS-001).

WP2 (B01) requires that a WARM provider qualification is reused across *separate
CLI processes*, not only inside one Python process.  Because each CLI invocation
starts a fresh interpreter, an in-process LRU alone is not sufficient: the
accepted, still-fresh qualification must be recoverable from the durable
provider-ledger SQLite file.

Regression matrix row "Ayri CLI process'lerinden ayni binding":
    "Qualification cache gercekten yeniden kullanilir; process-local basariyla
     sinirli kalmaz."

These tests prove that durability with a real two-process scenario:

* Process A (the test parent) writes a warm qualification record through the
  real ``project_rag_runtime._write_durable_qualification`` into a temp
  provider-ledger SQLite file.
* Process B (a fresh ``sys.executable`` subprocess, separate interpreter)
  exercises the real ``project_rag_runtime._provider`` wrapper (with a stubbed
  remote provider so NO network/probe hit occurs) against the SAME ledger and
  reports whether the warm record was reused WITHOUT calling ``probe()`` again.

Provider-free and deterministic: the probe is a stub that only counts calls;
the durable-record write/read is what is under test.  All temp paths come from
pytest ``tmp_path``; the ledger is a private regular file as the real code
requires (passes ``private_regular``).

Scenarios:
  XP-1  cross-process reuse   - A writes warm, B reuses it, probe count 0.
  XP-2  no ledger => re-probe - B with no durable record probes (qualifies).
  XP-3  expired/mismatched    - B with an expired or wrong-binding record does
        => re-validate, fail-closed  NOT reuse it; it re-probes (no silent
                                reuse / no auto-approval).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from zekam.application import project_rag_runtime as runtime
from zekam.application.config import EmbeddingRoute, KnowledgeSettings
from zekam.domain.canonical import digest
from zekam.infrastructure.embedding.opencode_remote import (
    OpenCodeRemoteEmbeddingProvider,
    RuntimeOpenCodeEmbeddingExecutor,
)
from zekam.infrastructure.local_file_security import private_regular

# ---------------------------------------------------------------------------
# Shared canonical facts used BOTH by the writer process and the subordinate
# interpreter so the binding digest is identical on both sides.
# ---------------------------------------------------------------------------

def _make_knowledge() -> KnowledgeSettings:
    return KnowledgeSettings(embedding_route=EmbeddingRoute.REMOTE)


def _make_configuration() -> SimpleNamespace:
    """A canonical remote embedding configuration (secret-free test stub)."""
    return SimpleNamespace(
        provider_id="litellm",
        canonical_model_id="openai/BAAI/bge-m3",
        selected_model_id="openai/BAAI/bge-m3",
        credential_locator="OPENCODE_LITELLM_KEY",
        embedding_endpoint="https://models.example.test/v1/embeddings",
        endpoint_identity=SimpleNamespace(identity_digest=digest("endpoint")),
    )


def _make_binding_digest() -> str:
    return runtime._qualification_key(
        _make_configuration(),
        _make_knowledge(),
        probe_revision=runtime._probe_revision(_make_knowledge()),
    )


def _make_record(now_ns: int, ttl_seconds: int) -> runtime._QualificationRecord:
    """A valid, secret-free, durable qualification record (warm-accepted evidence)."""
    return runtime._QualificationRecord(
        binding_digest=_make_binding_digest(),
        qualified_at_ns=now_ns,
        expires_at_ns=now_ns + ttl_seconds * 1_000_000_000,
        ttl_seconds=ttl_seconds,
        profile_digest=digest("provider-profile"),
        probe_evidence_digest=digest("probe"),
        semantic_margin=0.4,
        max_repeat_delta=0.0001,
        max_batch_delta=0.0001,
        latency_ms=1,
        provider_call_count=2,
        model_revision_fingerprint=digest("revision"),
        provider_identity_digest=digest("provider"),
        exact_model_id="openai/BAAI/bge-m3",
    )


# ---------------------------------------------------------------------------
# Subordinate-process runner.  Executed inside a SEPARATE interpreter via
# ``_run_subprocess``; this is NOT a pytest test.
# ---------------------------------------------------------------------------

def _subprocess_runner(ledger_path: str, case: str) -> dict[str, object]:
    """Return a JSON-safe report of ``_provider`` warm/cold behaviour.

    ``case`` selects the ledger precondition:
      * "warm"    - ledger already holds a fresh, matching record.
      * "cold"    - no ledger (or an empty one) => must re-probe.
      * "expired" - ledger holds an expired record => must re-probe (fail-closed).
      * "mismatch"- ledger holds a record for a DIFFERENT binding => re-probe.
    """
    from zekam.domain.security import DataClassification

    probes: list[int] = []
    configuration = _make_configuration()
    knowledge = _make_knowledge()

    class _FakeRemoteProvider:
        def __init__(
            self,
            configuration: object,
            executor: object,
            *,
            dimension: int,
            max_batch_size: int,
        ) -> None:
            del configuration, executor, dimension, max_batch_size
            self._profile_digest = digest("provider-profile")

        def probe(self, _fixture: object) -> SimpleNamespace:
            probes.append(1)
            return SimpleNamespace(
                profile=SimpleNamespace(
                    profile_digest=self._profile_digest,
                    model_revision_fingerprint=digest("revision"),
                    provider_identity_digest=digest("provider"),
                    exact_model_id=knowledge.embedding_model_ref,
                    dimension=knowledge.embedding_dimension,
                    vector_dtype="float32",
                    normalized=True,
                    distance_metric="cosine",
                    query_prefix="",
                    passage_prefix="",
                    preprocessor_digest=digest("pre"),
                    tokenizer_digest=digest("tok"),
                    batch_policy_digest=digest("batch"),
                    device_scope="windows:x64:opencode",
                    data_classification_allowlist=(DataClassification.PUBLIC,),
                    verified_at="2026-09-02T00:00:00Z",
                    probe_evidence_digest=digest("probe"),
                    validate_vector=lambda _vector: None,
                ),
                semantic_margin=0.4,
                positive_score=0.7,
                negative_score=0.3,
                max_repeat_delta=0.0001,
                max_batch_delta=0.0001,
                batch_cosine=0.9999,
                latency_ms=1,
                evidence_digest=digest("probe"),
                provider_call_count=2,
            )

    class _FakeHost:
        def register(self, _work: object) -> None:
            return None

        def summary(self) -> dict[str, object]:
            return {
                "schema": "zekam-local-provider-ledger-summary/v1",
                "provider_calls": 0,
                "durable_remote_effects": 0,
            }

    # ---- stub only the heavyweight harness; qualification logic stays real ----
    runtime.OpenCodeRemoteEmbeddingProvider = cast(  # type: ignore[attr-defined]
        type[OpenCodeRemoteEmbeddingProvider], _FakeRemoteProvider
    )
    runtime.ProcessIsolatedJsonProviderTransport = lambda *_: object()  # type: ignore[attr-defined, assignment]
    runtime.LiveProcessClient = lambda *_a, **_k: object()  # type: ignore[attr-defined, assignment]
    runtime.RuntimeOpenCodeEmbeddingExecutor = cast(  # type: ignore[attr-defined]
        type[RuntimeOpenCodeEmbeddingExecutor], lambda _invocation: object()
    )
    runtime.RuntimeProviderContractRunner = lambda *_a, **_k: object()  # type: ignore[attr-defined, assignment]
    runtime.load_inventory = lambda *_: object()  # type: ignore[attr-defined, assignment]
    runtime.SQLiteProviderLedgerHost = lambda *_a, **_k: _FakeHost()  # type: ignore[attr-defined, assignment]
    runtime.load_opencode_embedding_configuration = lambda *_a, **_k: configuration  # type: ignore[attr-defined, assignment]

    binding = runtime._provider(
        Path(ledger_path).parent,
        Path(ledger_path),
        Path(ledger_path).parent / "opencode.json",
        uuid4(),
        (),
        knowledge,
        remote_authorized=True,
    )
    return {
        "probe_calls": len(probes),
        "probe_call_count": binding.probe_call_count,
        "qualified_from_cache": bool(binding.probe.get("qualified_from_cache")),
        "binding_digest": _make_binding_digest(),
    }


def _run_subprocess(ledger_path: Path, case: str, timeout: int = 120) -> dict[str, object]:
    """Spawn a SEPARATE python interpreter that runs ``_subprocess_runner``."""
    test_file = Path(__file__).resolve()
    program = (
        "import importlib.util, json, sys;"
        "spec=importlib.util.spec_from_file_location('qcx_cross_process', sys.argv[1]);"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
        "print(json.dumps(m._subprocess_runner(sys.argv[2], sys.argv[3])))"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(test_file),
            str(ledger_path.resolve()),
            case,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"subprocess failed rc={completed.returncode}\n"
            f"stdout={completed.stdout}\n"
            f"stderr={completed.stderr}"
        )
    try:
        parsed = cast(
            "dict[str, object]", json.loads(completed.stdout.strip().splitlines()[-1])
        )
        return parsed
    except Exception as exc:  # pragma: no cover - defensive
        raise AssertionError(
            f"could not parse subordinate JSON: {exc}\nstdout={completed.stdout}"
        ) from exc

# ---------------------------------------------------------------------------
# XP-1  Cross-process reuse: A writes a warm record, a SEPARATE interpreter B
# reuses it and does NOT re-probe.
# ---------------------------------------------------------------------------

def test_xp1_warm_qualification_reused_across_separate_processes(tmp_path: Path) -> None:
    ledger = tmp_path / "runtime" / "provider-ledger.sqlite3"
    runtime._write_durable_qualification(
        ledger,
        _make_record(now_ns=runtime._now_ns(), ttl_seconds=runtime.QUALIFICATION_TTL_SECONDS),
    )
    assert ledger.exists()
    assert private_regular(ledger)
    # Process A's own durable reader sees the fresh record (sanity).
    assert (
        runtime._read_durable_qualification(
            ledger, _make_binding_digest(), now_ns=runtime._now_ns()
        )
        is not None
    )

    result = _run_subprocess(ledger, "warm")

    # Process B reuses the durable record: probe() was NOT called again.
    assert result["probe_calls"] == 0
    # The reconstructed binding reports zero probe calls and tells us the
    # acceptance came from the cache.
    assert result["probe_call_count"] == 0
    assert result["qualified_from_cache"] is True
    # Process B computed an identical binding digest for this ledger.
    assert result["binding_digest"] == _make_binding_digest()


# ---------------------------------------------------------------------------
# XP-2  No-ledger => a SEPARATE process must re-probe (qualification computed,
# never silently skipped).
# ---------------------------------------------------------------------------

def test_xp2_separate_process_with_no_ledger_reprobes(tmp_path: Path) -> None:
    ledger = tmp_path / "runtime" / "provider-ledger.sqlite3"
    assert not ledger.exists()

    result = _run_subprocess(ledger, "cold")

    # With no durable record the subordinate process had to run the probe once.
    assert result["probe_calls"] == 1
    assert result["probe_call_count"] == 2  # provider-reported probe cost
    assert result["qualified_from_cache"] is False
    # A fresh durable record was written back so the *next* process is warm.
    assert ledger.exists()
    assert private_regular(ledger)


# ---------------------------------------------------------------------------
# XP-3  Expired / mismatched-binding => a SEPARATE process re-validates
# (fail-closed): it never treats the stale/incompatible record as valid.
# ---------------------------------------------------------------------------

def _prepare_nonwarm_ledger(tmp_path: Path, case: str) -> Path:
    ledger = tmp_path / "runtime" / "provider-ledger.sqlite3"
    if case == "expired":
        stored = _make_record(now_ns=0, ttl_seconds=-100)  # already expired
    else:  # mismatch
        warm = _make_record(now_ns=runtime._now_ns(), ttl_seconds=runtime.QUALIFICATION_TTL_SECONDS)
        stored = runtime._QualificationRecord(
            binding_digest=digest({"other": "binding"}),
            qualified_at_ns=warm.qualified_at_ns,
            expires_at_ns=warm.expires_at_ns,
            ttl_seconds=warm.ttl_seconds,
            profile_digest=warm.profile_digest,
            probe_evidence_digest=warm.probe_evidence_digest,
            semantic_margin=warm.semantic_margin,
            max_repeat_delta=warm.max_repeat_delta,
            max_batch_delta=warm.max_batch_delta,
            latency_ms=warm.latency_ms,
            provider_call_count=warm.provider_call_count,
            model_revision_fingerprint=warm.model_revision_fingerprint,
            provider_identity_digest=warm.provider_identity_digest,
            exact_model_id=warm.exact_model_id,
        )
        # Sanity: the stored binding differs from the computed one.
        assert _make_binding_digest() != stored.binding_digest
    runtime._write_durable_qualification(ledger, stored)
    # Fail-closed sanity from Process A's perspective: reader returns None for
    # the computed binding (expired) or the wrong-binding record is not shared.
    assert (
        runtime._read_durable_qualification(
            ledger, _make_binding_digest(), now_ns=runtime._now_ns()
        )
        is None
    )
    return ledger


@pytest.mark.parametrize("case", ["expired", "mismatch"])
def test_xp3_expired_or_mismatched_record_revalidates_fail_closed(
    tmp_path: Path, case: str
) -> None:
    ledger = _prepare_nonwarm_ledger(tmp_path, case)

    result = _run_subprocess(ledger, case)

    # Fail-closed: the subordinate process re-validated (re-probed) instead of
    # silently reusing the stale/incompatible evidence.
    assert result["probe_calls"] == 1
    assert result["qualified_from_cache"] is False
