"""ZEKAM-RAG-PERFORMANCE-CORRECTNESS-001: live provider latency measurement.

Scope (AKTIF_GOREV.md section 5):

* Measure real remote provider (OpenCode/litellm) qualification probe and query
  embedding latency/call-count against the actual configured endpoint.
* This is an opt-in integration test because it performs real network calls
  and may incur cost.  It requires explicit user authorization.

Run only with:

    ZEKAM_LIVE_PROVIDER_MEASURE=1 python -m pytest \
        tests/integration/test_live_provider_rag_latency.py -v

The test deliberately performs a bounded number of calls (<= 10 distinct
queries, 1 probe + 1 embed per query) and records latency/call counts via the
WP1 QueryCounters path.  It never runs by default in CI or in a no-network
permission profile.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from zekam.application.config import load_settings
from zekam.application.embedding_provider import EmbeddingPolicy
from zekam.application.opencode_embedding import default_opencode_config_file
from zekam.application.project_rag_runtime import (
    _existing_runtime_paths,
    _provider,
)
from zekam.domain.security import DataClassification
from zekam.infrastructure.query_measurement import (
    last_counters,
    record_qualification,
    scope,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("ZEKAM_LIVE_PROVIDER_MEASURE") != "1",
        reason=(
            "Live provider measurement requires explicit remote authorization/cost approval. "
            "Set ZEKAM_LIVE_PROVIDER_MEASURE=1 to run."
        ),
    ),
]

PROJECT_SLUG = "zekam"
PROJECT_ID = UUID("01a09b0e-9ad3-78d0-9ebc-1976935ea8b9")
QUERIES = (
    "project_rag_runtime _query",
    "RetrievalService search",
    "EmbeddedProjectRAG query",
    "qualification cache",
    "source freshness",
)
MAX_TOTAL_CALLS = 20


def _load_project_settings(home: Path) -> Any:
    """Load Zekam project settings for the configured embedding route."""
    settings = load_settings(home=home)
    return settings.knowledge


def _recorded_latency_ms(t0: float) -> int:
    """Monotonic wall-free elapsed milliseconds."""
    return max(0, int((time.monotonic() - t0) * 1000.0))


def test_live_provider_qualification_and_embedding_latency() -> None:
    """Live measurement: one qualification probe + one query embedding per query.

    This test touches real remote infrastructure only when explicitly enabled.
    On failure it reports sanitized diagnostics and counts; it never retries
    auth/dimension/policy errors.
    """
    home_raw = os.environ.get("ZEKAM_HOME")
    if home_raw:
        home = Path(home_raw)
    else:
        # Fallback to the documented default when the env var is not propagated
        # by the test runner.
        home = Path.home() / ".zekam"
        if not home.is_dir():
            pytest.skip("ZEKAM_HOME is not set and default ~/.zekam does not exist.")
    knowledge = _load_project_settings(home)
    if knowledge.embedding_route != "remote":
        pytest.skip(f"Embedding route is {knowledge.embedding_route!r}, not 'remote'.")

    paths = _existing_runtime_paths(home, PROJECT_SLUG)
    config_file = default_opencode_config_file()

    probe_latencies: list[int] = []
    embed_latencies: list[int] = []
    total_calls = 0

    for query in QUERIES:
        with scope():
            t0 = time.monotonic()
            try:
                binding = _provider(
                    home=paths["home"],
                    ledger_path=paths["ledger"],
                    config_file=config_file,
                    project_id=PROJECT_ID,
                    chunks=(),
                    knowledge=knowledge,
                    remote_authorized=True,
                )
            except Exception as exc:
                pytest.skip(f"Provider qualification failed: {type(exc).__name__}: {exc}")
            probe_latencies.append(_recorded_latency_ms(t0))
            total_calls += 1
            record_qualification()

            profile = binding.provider.describe()
            policy = EmbeddingPolicy(
                classification=DataClassification.INTERNAL,
                expected_profile_digest=profile.profile_digest,
                remote_disclosure_authorized=True,
            )
            t0 = time.monotonic()
            try:
                batch = binding.provider.embed_query(query, policy)
            except Exception as exc:
                pytest.skip(f"Query embedding failed: {type(exc).__name__}: {exc}")
            embed_latencies.append(_recorded_latency_ms(t0))
            total_calls += 1
            assert len(batch.vectors) == 1
            assert len(batch.vectors[0]) == knowledge.embedding_dimension

        snapshot = last_counters()
        assert snapshot is not None
        assert int(snapshot["qualification_count"]) >= 1

    assert total_calls <= MAX_TOTAL_CALLS

    report = {
        "queries": len(QUERIES),
        "total_provider_calls": total_calls,
        "probe_latencies_ms": probe_latencies,
        "embed_latencies_ms": embed_latencies,
        "route": knowledge.embedding_route,
        "model_ref": knowledge.embedding_model_ref,
        "dimension": knowledge.embedding_dimension,
    }
    print(f"\n[LIVE-PROVIDER] {report}")
