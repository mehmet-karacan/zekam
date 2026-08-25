"""ZK-P2-003 transcript dedupe/topic map kabul testleri."""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from zekam.application.transcript_corpus_import import scan_transcript_archive
from zekam.application.transcript_topic_map import (
    build_transcript_topic_map,
    persist_transcript_topic_map,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.transcript_topic_map import DuplicateKind, TranscriptDedupeProfile
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

NOW = dt.datetime(2026, 8, 25, tzinfo=dt.UTC)
POLICY = digest("transcript-policy")


def _zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, payload in entries.items():
            archive.writestr(path, payload)
    return output.getvalue()


def _scan(name: str, entries: dict[str, bytes]):  # type: ignore[no-untyped-def]
    return scan_transcript_archive(
        _zip(entries),
        archive_name=f"{name}.zip",
        corpus_id=name,
        source_policy_digest=POLICY,
        imported_by="researcher",
        created_at=NOW,
    )


def test_exact_duplicates_form_group() -> None:
    text = b"Title: A\n[00:01-00:02] context checkpoint continuity resume durable state\n"
    exact = (
        _scan("exact-a", {"a.txt": text}),
        _scan("exact-b", {"b.txt": text}),
    )
    topic_map = build_transcript_topic_map(exact)

    assert len(topic_map.duplicate_groups) == 1
    group = topic_map.duplicate_groups[0]
    assert group.kind is DuplicateKind.EXACT
    assert group.minimum_similarity == 1.0
    assert group.canonical_source_version_digest in group.member_source_version_digests


def test_canonical_duplicate_source_uses_earliest_declared_date() -> None:
    common = "context checkpoint continuity resume durable state agent ownership verifier"
    scans = (
        _scan(
            "later",
            {"later.txt": f"Date: 2026-08-24\n[00:01-00:02] {common} alpha\n".encode()},
        ),
        _scan(
            "earlier",
            {"earlier.txt": f"Date: 2026-08-20\n[00:01-00:02] {common} beta\n".encode()},
        ),
    )
    topic_map = build_transcript_topic_map(
        scans,
        profile=TranscriptDedupeProfile(near_threshold=0.60, topic_threshold=0.25),
    )
    group = topic_map.duplicate_groups[0]
    canonical = next(
        source
        for source in topic_map.sources
        if source.source_version_digest == group.canonical_source_version_digest
    )

    assert canonical.declared_date == "2026-08-20"


def test_near_duplicates_group_without_automatic_content_merge() -> None:
    common = "context checkpoint continuity resume durable state agent ownership verifier"
    scans = (
        _scan("a", {"a.txt": f"[00:01-00:02] {common} alpha\n".encode()}),
        _scan("b", {"b.txt": f"[00:01-00:02] {common} beta\n".encode()}),
    )
    topic_map = build_transcript_topic_map(
        scans,
        profile=TranscriptDedupeProfile(near_threshold=0.70, topic_threshold=0.25),
    )

    group = topic_map.duplicate_groups[0]
    assert group.kind is DuplicateKind.NEAR
    assert group.as_dict()["automatic_content_merge"] is False
    collapsed = topic_map.collapse_duplicates(group.member_source_version_digests)
    assert collapsed == (group.canonical_source_version_digest,)


def test_topic_cluster_is_relation_not_truth_and_is_not_duplicate_collapsed() -> None:
    scans = (
        _scan(
            "a",
            {"a.txt": b"[00:01-00:02] checkpoint continuity durable recovery ownership alpha\n"},
        ),
        _scan(
            "b",
            {"b.txt": b"[00:01-00:02] checkpoint continuity durable recovery validator beta\n"},
        ),
        _scan("c", {"c.txt": b"[00:01-00:02] oracle index query optimizer execution plan\n"}),
    )
    topic_map = build_transcript_topic_map(
        scans,
        profile=TranscriptDedupeProfile(near_threshold=0.90, topic_threshold=0.30),
    )

    assert not topic_map.duplicate_groups
    assert len(topic_map.topic_clusters) == 1
    cluster = topic_map.topic_clusters[0].as_dict()
    assert cluster["relation"] == "same-topic-as"
    assert cluster["implies_support"] is False
    assert cluster["implies_truth"] is False
    members = tuple(cluster["member_source_version_digests"])
    assert topic_map.collapse_duplicates(members) == tuple(sorted(members))


def test_map_is_deterministic_across_scan_order_and_persists_in_cas(tmp_path: Path) -> None:
    scans = (
        _scan("a", {"a.txt": b"[00:01-00:02] context checkpoint continuity durable state\n"}),
        _scan("b", {"b.txt": b"[00:01-00:02] context checkpoint continuity durable state\n"}),
    )
    first = build_transcript_topic_map(scans)
    second = build_transcript_topic_map(tuple(reversed(scans)))
    store = LocalContentAddressedStore(tmp_path / "objects").ensure()
    receipt = persist_transcript_topic_map(first, store)

    assert first.map_digest == second.map_digest
    assert first.to_bytes() == second.to_bytes()
    assert store.get(receipt.object_digest) == first.to_bytes()
    assert first.as_dict()["grants_authority"] is False


def test_short_or_unrelated_sources_do_not_invent_near_or_topic_relation() -> None:
    topic_map = build_transcript_topic_map(
        (
            _scan("a", {"a.txt": b"[00:01-00:02] kisa metin\n"}),
            _scan("b", {"b.txt": b"[00:01-00:02] baska konu\n"}),
        )
    )
    assert topic_map.duplicate_groups == ()
    assert topic_map.topic_clusters == ()


def test_candidate_pair_and_source_budgets_fail_closed() -> None:
    scans = tuple(
        _scan(
            f"s{index}",
            {f"{index}.txt": f"[00:01-00:02] ortak konu kelime {index}\n".encode()},
        )
        for index in range(4)
    )
    with pytest.raises(PolicyViolation, match="source sayisi"):
        build_transcript_topic_map(scans, profile=TranscriptDedupeProfile(max_sources=2))
    with pytest.raises(PolicyViolation, match="candidate pair"):
        build_transcript_topic_map(
            scans,
            profile=TranscriptDedupeProfile(
                near_threshold=0.82,
                topic_threshold=0.30,
                max_candidate_pairs=1,
            ),
        )


def test_collapse_rejects_unknown_source() -> None:
    topic_map = build_transcript_topic_map(
        (_scan("a", {"a.txt": b"[00:01-00:02] tek kaynak transcript metni\n"}),)
    )
    with pytest.raises(ValidationFailed, match="bilinmeyen"):
        topic_map.collapse_duplicates((digest("unknown"),))


def test_near_duplicate_clustering_is_complete_link_not_transitive_chain() -> None:
    scans = (
        _scan("a", {"a.txt": b"[00:01-00:02] aaa bbb ccc\n"}),
        _scan("b", {"b.txt": b"[00:01-00:02] aaa bbb ccc ddd eee\n"}),
        _scan("c", {"c.txt": b"[00:01-00:02] ccc ddd eee\n"}),
    )
    topic_map = build_transcript_topic_map(
        scans,
        profile=TranscriptDedupeProfile(
            near_threshold=0.60,
            topic_threshold=0.20,
            shingle_size=1,
            minimum_near_shingles=3,
        ),
    )

    assert len(topic_map.duplicate_groups) == 1
    assert len(topic_map.duplicate_groups[0].member_source_version_digests) == 2
    assert topic_map.duplicate_groups[0].minimum_similarity >= 0.60


def test_forged_group_semantic_id_is_rejected_before_cas_effect() -> None:
    topic_map = build_transcript_topic_map(
        (
            _scan("a", {"a.txt": b"[00:01-00:02] ayni transcript metni\n"}),
            _scan("b", {"b.txt": b"[00:01-00:02] ayni transcript metni\n"}),
        )
    )
    group = topic_map.duplicate_groups[0]
    with pytest.raises(ValidationFailed, match="semantic digest"):
        replace(group, group_id=digest("forged-unrelated-id"))

    class CountingStore:
        calls = 0

        def put(self, payload: bytes, **kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("pre-effect gate put cagirmamali")

        def exists(self, object_digest: str) -> bool:
            return False

        def get(self, object_digest: str) -> bytes:
            raise AssertionError("pre-effect gate get cagirmamali")

    object.__setattr__(group, "group_id", digest("forged-unrelated-id"))
    store = CountingStore()
    with pytest.raises(ValidationFailed, match="semantic digest"):
        persist_transcript_topic_map(topic_map, store)  # type: ignore[arg-type]
    assert store.calls == 0


def test_forged_group_canonical_and_similarity_are_rejected() -> None:
    topic_map = build_transcript_topic_map(
        (
            _scan("a", {"a.txt": b"[00:01-00:02] ayni transcript metni\n"}),
            _scan("b", {"b.txt": b"[00:01-00:02] ayni transcript metni\n"}),
        )
    )
    group = topic_map.duplicate_groups[0]
    other = next(
        value
        for value in group.member_source_version_digests
        if value != group.canonical_source_version_digest
    )
    with pytest.raises(ValidationFailed, match="semantic digest"):
        replace(group, canonical_source_version_digest=other)
    with pytest.raises(ValidationFailed, match="semantic digest"):
        replace(group, minimum_similarity=0.0)
