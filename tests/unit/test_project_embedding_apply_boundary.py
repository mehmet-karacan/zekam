"""Project index provider failures must happen before durable mutation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.embedding_provider import (
    EmbeddingBatch,
    EmbeddingHealth,
    EmbeddingPolicy,
    EmbeddingProbeFixture,
    EmbeddingProbeResult,
    EmbeddingProfile,
    EmbeddingProviderKind,
    EmbeddingPurpose,
    EmbeddingReceipt,
)
from zekam.application.local_embedding_composition import project_embedding_probe_fixture
from zekam.application.project_knowledge_index import apply_project_index, build_project_index_plan
from zekam.application.source_discovery import discover
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.security import DataClassification

pytestmark = pytest.mark.unit


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="local-bge-test",
        display_name="Local BGE test",
        provider_kind=EmbeddingProviderKind.LOCAL,
        provider_identity_digest=digest("provider"),
        exact_model_id="BAAI/bge-m3",
        model_revision_fingerprint=digest("revision"),
        dimension=1024,
        vector_dtype="float32",
        normalized=True,
        distance_metric="cosine",
        query_prefix="",
        passage_prefix="",
        preprocessor_digest=digest("preprocessor"),
        tokenizer_digest=digest("tokenizer"),
        batch_policy_digest=digest("batch"),
        device_scope="darwin-arm64:mps",
        data_classification_allowlist=(DataClassification.LOCAL_ONLY,),
        verified_at="2026-09-02T00:00:00Z",
        probe_evidence_digest=digest("probe"),
    )


class PartialProvider:
    def __init__(self, profile: EmbeddingProfile) -> None:
        self.profile = profile

    def describe(self) -> EmbeddingProfile:
        return self.profile

    def embed_documents(self, texts: tuple[str, ...], policy: EmbeddingPolicy) -> EmbeddingBatch:
        self.profile.assert_policy(policy)
        return EmbeddingBatch(
            (),
            EmbeddingReceipt(
                EmbeddingPurpose.DOCUMENT,
                self.profile.profile_digest,
                digest(texts),
                digest(()),
                len(texts),
                self.profile.dimension,
                1,
                1,
            ),
        )

    def embed_query(self, text: str, policy: EmbeddingPolicy) -> EmbeddingBatch:
        del text, policy
        raise AssertionError("apply query embedding cagirmamali")

    def probe(self, fixture: EmbeddingProbeFixture) -> EmbeddingProbeResult:
        del fixture
        raise AssertionError("apply probe cagirmamali")

    def health(self) -> EmbeddingHealth:
        return EmbeddingHealth(True, self.profile.profile_digest, None, digest("health"))


class NeverMutatingStore:
    def ensure(self) -> Any:
        raise AssertionError("provider failure CAS mutationindan once olmali")


def _plan(tmp_path: Path) -> Any:
    root = tmp_path / "source"
    root.mkdir()
    (root / "service.py").write_text("class PaymentService:\n    pass\n", encoding="utf-8")
    report = discover(root)
    return build_project_index_plan(
        project_id=uuid4(),
        project_slug="payment",
        source_root=root,
        source_revision="abc123",
        expected_tree_digest=report.tree_digest,
    )


def test_missing_verified_provider_fails_before_cas_or_database(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation, match="Verified embedding provider"):
        apply_project_index(
            _plan(tmp_path),
            connection=object(),
            knowledge=object(),
            retrieval=object(),
            object_store=NeverMutatingStore(),  # type: ignore[arg-type]
        )


def test_project_probe_fixture_is_bounded_to_real_relative_chunk_sources(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    second = replace(
        plan.chunks[0],
        chunk_id=f"{plan.chunks[0].chunk_id}-other",
        text="A different architecture decision about idempotent imports.",
    )
    fixture = project_embedding_probe_fixture(
        (plan.chunks[0], second), classification=DataClassification.LOCAL_ONLY
    )

    assert fixture.positive_passage == plan.chunks[0].text
    assert fixture.negative_passage == second.text
    assert all("/Users/" not in ref for ref in fixture.source_refs)
    assert all(ref.startswith("service.py") for ref in fixture.source_refs)


def test_partial_provider_batch_fails_before_cas_or_database(tmp_path: Path) -> None:
    profile = _profile()
    plan = _plan(tmp_path)
    plan = replace(
        plan,
        embedding_profile=replace(
            plan.embedding_profile,
            provider_profile_digest=profile.profile_digest,
        ),
    )
    with pytest.raises(PolicyViolation, match="batch/receipt"):
        apply_project_index(
            plan,
            connection=object(),
            knowledge=object(),
            retrieval=object(),
            object_store=NeverMutatingStore(),  # type: ignore[arg-type]
            embedding_provider=PartialProvider(profile),
            embedding_policy=EmbeddingPolicy(DataClassification.LOCAL_ONLY, profile.profile_digest),
        )
