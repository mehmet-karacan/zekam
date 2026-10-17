"""Cross-process local-provider qualification boundary E2E.

WP2 (B01) requires that a WARM provider qualification is reused across *separate
CLI processes*, not only inside one Python process.  Because each CLI invocation
starts a fresh interpreter, an in-process LRU alone is not sufficient: the
accepted, still-fresh qualification must be recoverable from the durable
provider-ledger SQLite file.

Regression matrix row "Ayri CLI process'lerinden ayni binding":
    "Qualification cache gercekten yeniden kullanilir; process-local basariyla
     sinirli kalmaz."

These tests prove the local-only route across two processes:

* Process A (the test parent) may write a legacy remote qualification record.
* Process B (a fresh ``sys.executable`` subprocess, separate interpreter)
  exercises the real ``project_rag_runtime._provider`` wrapper and proves that
  local E5 does not consult or refresh that remote record.

Provider-free and deterministic: the local binding is a bounded test stub.
All temp paths come from pytest ``tmp_path``; legacy fixture ledgers remain
private regular files.

Scenarios:
  XP-1  legacy warm record   - A writes a remote record; B stays local.
  XP-2  no ledger            - B stays local without creating a remote ledger.
  XP-3  stale/mismatched     - B ignores stale remote records and stays local.
"""

from __future__ import annotations

import json
import os
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
from zekam.infrastructure.local_file_security import private_regular

# ---------------------------------------------------------------------------
# Shared canonical facts used BOTH by the writer process and the subordinate
# interpreter so the binding digest is identical on both sides.
# ---------------------------------------------------------------------------


def _make_knowledge() -> KnowledgeSettings:
    return KnowledgeSettings(embedding_route=EmbeddingRoute.LOCAL)


def _make_configuration() -> SimpleNamespace:
    """A stable local-profile identity used only for legacy ledger fixtures."""
    return SimpleNamespace(
        provider_id="local-e5",
        canonical_model_id="intfloat/multilingual-e5-small",
        selected_model_id="intfloat/multilingual-e5-small",
        credential_locator=None,
        embedding_endpoint=None,
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
        exact_model_id="intfloat/multilingual-e5-small",
    )


# ---------------------------------------------------------------------------
# Subordinate-process runner.  Executed inside a SEPARATE interpreter via
# ``_run_subprocess``; this is NOT a pytest test.
# ---------------------------------------------------------------------------


def _subprocess_runner(ledger_path: str, case: str) -> dict[str, object]:
    """Return a JSON-safe report of ``_provider`` warm/cold behaviour.

    ``case`` selects the legacy ledger precondition. The local route must
    produce the same result for every case without consulting that ledger.
    """
    probes: list[int] = []
    knowledge = _make_knowledge()

    def _fake_local(*, classification: object = None) -> SimpleNamespace:
        del classification
        profile = SimpleNamespace(
            profile_digest=digest("local-profile"),
            probe_evidence_digest=digest("local-probe"),
            exact_model_id=knowledge.embedding_model_ref,
            dimension=knowledge.embedding_dimension,
        )
        return SimpleNamespace(provider=object(), policy=object(), profile=profile)

    runtime.build_verified_local_e5_embedding = _fake_local  # type: ignore[assignment]

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
        "qualified_from_cache": bool(binding.probe.get("qualified_from_cache", False)),
        "remote_provider_used": binding.remote_provider_used,
        "ledger_exists": Path(ledger_path).exists(),
        "binding_digest": _make_binding_digest(),
    }


def _run_subprocess(ledger_path: Path, case: str, timeout: int = 120) -> dict[str, object]:
    """Spawn a SEPARATE python interpreter that runs ``_subprocess_runner``."""
    # Alt process pytest'in src-layout import yolunu alamaz: repo `src` dizinini
    # mevcut PYTHONPATH'in onune, yalniz bu subprocess env kopyasina ekle.
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")
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
        env=env,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"subprocess failed rc={completed.returncode}\n"
            f"stdout={completed.stdout}\n"
            f"stderr={completed.stderr}"
        )
    try:
        parsed = cast("dict[str, object]", json.loads(completed.stdout.strip().splitlines()[-1]))
        return parsed
    except Exception as exc:  # pragma: no cover - defensive
        raise AssertionError(
            f"could not parse subordinate JSON: {exc}\nstdout={completed.stdout}"
        ) from exc


# ---------------------------------------------------------------------------
# XP-1  Legacy warm record: a SEPARATE interpreter B stays on local E5.
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

    # Process B ignores the legacy remote record and stays local.
    assert result["probe_calls"] == 0
    assert result["probe_call_count"] == 2
    assert result["qualified_from_cache"] is False
    assert result["remote_provider_used"] is False
    assert result["ledger_exists"] is True
    assert result["binding_digest"] == _make_binding_digest()


# ---------------------------------------------------------------------------
# XP-2  No ledger: local E5 does not create a remote qualification ledger.
# ---------------------------------------------------------------------------


def test_xp2_separate_process_with_no_ledger_reprobes(tmp_path: Path) -> None:
    ledger = tmp_path / "runtime" / "provider-ledger.sqlite3"
    assert not ledger.exists()

    result = _run_subprocess(ledger, "cold")

    assert result["probe_calls"] == 0
    assert result["probe_call_count"] == 2
    assert result["qualified_from_cache"] is False
    assert result["remote_provider_used"] is False
    assert result["ledger_exists"] is False


# ---------------------------------------------------------------------------
# XP-3  Expired / mismatched-binding: local E5 ignores both records.
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

    assert result["probe_calls"] == 0
    assert result["probe_call_count"] == 2
    assert result["qualified_from_cache"] is False
    assert result["remote_provider_used"] is False
    assert result["ledger_exists"] is True
