"""Benchmark secret, self-verification ve authority negatif testleri."""

from __future__ import annotations

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_benchmark import (
    BenchmarkFixture,
    DeliberationResult,
    ExecutionEligibility,
    ModelDecision,
)

pytestmark = pytest.mark.security
DIGEST = digest({"safe": True})


def test_fixture_metadata_rejects_endpoint_and_credential_tags() -> None:
    with pytest.raises(PolicyViolation):
        BenchmarkFixture(
            case_id="case",
            version=1,
            workload="code",
            modality="chat",
            fixture_source="benchmark/case.json",
            execution_eligibility=ExecutionEligibility.REMOTE_ALLOWED,
            content_digest=DIGEST,
            expected_schema_digest=DIGEST,
            tags=("https://provider.invalid",),
        )


def test_fixture_metadata_rejects_secret_like_identifiers() -> None:
    with pytest.raises(PolicyViolation):
        BenchmarkFixture(
            case_id="sk-sensitive",
            version=1,
            workload="code",
            modality="chat",
            fixture_source="benchmark/case.json",
            execution_eligibility=ExecutionEligibility.LOCAL_ONLY,
            content_digest=DIGEST,
            expected_schema_digest=DIGEST,
        )


@pytest.mark.parametrize(
    "source",
    (
        "/absolute/case.json",
        "C:\\absolute\\case.json",
        "../escape.json",
        "benchmark/../../escape.json",
        "https://provider.invalid/case.json",
        "10.20.30.40/case.json",
        "benchmark/credential-secret.json",
    ),
)
def test_fixture_source_must_be_secret_free_logical_relative_path(source: str) -> None:
    with pytest.raises(PolicyViolation):
        BenchmarkFixture(
            case_id="case",
            version=1,
            workload="code",
            modality="chat",
            fixture_source=source,
            execution_eligibility=ExecutionEligibility.LOCAL_ONLY,
            content_digest=DIGEST,
            expected_schema_digest=DIGEST,
        )


def test_decision_cannot_grant_authority() -> None:
    with pytest.raises(PolicyViolation, match="authority"):
        ModelDecision(None, None, (), {}, DIGEST, authority_granted=True)


def test_deliberation_cannot_grant_mutation_approval() -> None:
    with pytest.raises(PolicyViolation, match="authority"):
        DeliberationResult(
            DIGEST,
            DIGEST,
            (),
            (),
            "synthesizer",
            False,
            authority_granted=True,
        )
