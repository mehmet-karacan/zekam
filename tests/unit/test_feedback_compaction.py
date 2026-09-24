"""AC-13 feedback compaction: normalize -> dedup -> cluster -> durable lesson.

- Binlerce tekrarlayan feedback, expensive reasoner context'ine girmeden once
  compact/sanitized gruba indirgenir.
- Raw prompt/response default kali CIKMAZ; secret hicbir asamada sizmaz.
- Feedback pipeline authority uretmez (feedback != authority).
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.application.feedback_compaction import (
    FeedbackCompactionOutput,
    FeedbackCompactor,
    FeedbackItem,
    FeedbackKind,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.policy import RiskLevel

NOW = dt.datetime(2026, 9, 2, 9, 0, tzinfo=dt.UTC)


def _item(
    content: str,
    *,
    key: str = "k",
    kind: FeedbackKind = FeedbackKind.FAILURE,
    run: str = "r1",
    evidence: str = "e",
) -> FeedbackItem:
    return FeedbackItem(
        item_id=f"id-{run}-{evidence}",
        kind=kind,
        content=content,
        evidence_digest=digest(evidence),
        source_ref=f"run:{run}",
        observed_at=NOW,
        occurrence_key=key,
        risk=RiskLevel.MEDIUM,
    )


def _compact(items: tuple[FeedbackItem, ...]) -> FeedbackCompactionOutput:
    return FeedbackCompactor().compact(
        items,
        output_id=uuid4(),
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        run_id=uuid4(),
        created_at=NOW,
    )


def test_redundant_feedback_is_compacted_into_single_cluster() -> None:
    items = (
        _item("Migration tekrari checksum drift uretiyor", key="k1"),
        _item("Migration tekrari checksum drift uretiyor", key="k1", run="r2", evidence="e2"),
        _item("Migration tekrari checksum drift uretiyor", key="k1", run="r3", evidence="e3"),
    )
    output = _compact(items)
    assert len(output.clusters) == 1
    cluster = output.clusters[0]
    assert cluster.item_count == 3
    assert len(cluster.source_refs) == 3


def test_distinct_feedback_produces_distinct_clusters() -> None:
    output = _compact(
        (
            _item("Drift senaryosu eksik", key="k1"),
            _item("Coklu model cagrisi gecikti", key="k2"),
        )
    )
    assert len(output.clusters) == 2


def test_normalization_folds_caseless_whitespace_and_unicode() -> None:
    output = _compact(
        (
            _item("  Migration\tTEKRARI  checksum drift üretiyor ", key="k1"),
            _item("migration tekrari CHECKSUM drift uretiyor", key="k1", run="r2"),
        )
    )
    assert len(output.clusters) == 1
    assert output.clusters[0].item_count == 2


def test_compaction_output_authority_free_and_deterministic() -> None:
    first = FeedbackCompactor().compact(
        (_item("A", key="k1"),),
        output_id=uuid4(), realm_id=uuid4(), project_id=uuid4(),
        work_item_id=uuid4(), run_id=uuid4(), created_at=NOW,
    )
    second = FeedbackCompactor().compact(
        (_item("A", key="k1"),),
        output_id=uuid4(), realm_id=uuid4(), project_id=uuid4(),
        work_item_id=uuid4(), run_id=uuid4(), created_at=NOW,
    )
    # Deterministik icerik digest (ids diger).
    assert first.clusters[0].content_digest == second.clusters[0].content_digest
    assert first.body()["grants_authority"] is False
    assert first.grants_authority is False


def test_raw_prompt_response_payload_is_never_persisted_in_output() -> None:
    output = _compact((_item("Migration tekrari checksum drift uretiyor", key="k1"),))
    serialized = str(output.body())
    # Compact ozet yalniz normalized key + digest tasir; ham icerik body'de yok.
    assert "drift uretiyor" not in serialized
    assert any(item.content_digest for item in output.clusters)


def test_secret_feedback_is_rejected_at_item_construction() -> None:
    with pytest.raises(PolicyViolation, match="secret"):
        _item("api_key=TOPSECRET123456 correct the code", key="k1")


def test_feedback_item_cannot_grant_authority() -> None:
    item = _item("A", key="k1")
    body = item  # frozenset; as_dict yok ama digest tabanli
    assert item.evidence_digest and not hasattr(body, "grants_authority")


def test_durable_lesson_bridge_produces_candidate_not_approval() -> None:
    output = _compact(
        (
            _item("Migration tekrari checksum drift uretiyor", key="k1"),
            _item("Migration tekrari checksum drift uretiyor", key="k1", run="r2"),
        )
    )
    bridges = FeedbackCompactor().bridge_to_durable_lesson(
        output.clusters, author_ref="author-a"
    )
    assert len(bridges) == 1
    bridge = bridges[0]
    # feedback != authority: proposal authority vermez, candidate sadece aday.
    assert bridge.approval_derived is False
    assert bridge.candidate.author_ref == "author-a"
    # Root cause verified olmadigi icin ready DE�IL -> promotion verifier olmadan yok.
    ready, _ = bridge.candidate.readiness()
    assert ready is False


def test_durable_lesson_bridge_requires_independent_promotion() -> None:
    """AC-12: lesson verifier'i yazarla ayni kimlik olamaz; feedback==authority degil."""
    from zekam.domain.errors import PolicyViolation as PV
    from zekam.domain.learning import promote_learning

    output = _compact(
        (
            _item("Migration tekrari checksum drift uretiyor", key="k1"),
            _item("Migration tekrari checksum drift uretiyor", key="k1", run="r2"),
        )
    )
    bridges = FeedbackCompactor().bridge_to_durable_lesson(output.clusters, author_ref="a")
    candidate = bridges[0].candidate
    with pytest.raises(PV, match="ayni kimlik"):
        promote_learning(candidate, verifier_ref="a")


def test_single_observation_cluster_never_forges_lesson() -> None:
    output = _compact((_item("Tek gozlemli feedback", key="k1"),))
    bridges = FeedbackCompactor().bridge_to_durable_lesson(output.clusters, author_ref="a")
    assert bridges == ()


def test_empty_and_oversized_inputs_fail_closed() -> None:
    with pytest.raises(ValidationFailed):
        _compact(())
    with pytest.raises(ValidationFailed):
        _item("x" * 5000)
