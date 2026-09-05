from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from tests.unit.test_transcript_topic_map import _scan

from zekam.application.transcript_topic_map import build_transcript_topic_map
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.transcript_topic_map import (
    DuplicateKind,
    TranscriptDedupeProfile,
    TranscriptDuplicateGroup,
    TranscriptTopicCluster,
    transcript_duplicate_group_id,
    transcript_topic_cluster_id,
)

pytestmark = pytest.mark.unit


def _exact_map() -> Any:
    text = b"[00:01-00:02] context checkpoint continuity resume durable state\n"
    return build_transcript_topic_map(
        (_scan("exact-a", {"a.txt": text}), _scan("exact-b", {"b.txt": text}))
    )


def _topic_map() -> Any:
    return build_transcript_topic_map(
        (
            _scan(
                "topic-a",
                {
                    "a.txt": (
                        b"[00:01-00:02] checkpoint continuity durable recovery ownership alpha\n"
                    )
                },
            ),
            _scan(
                "topic-b",
                {"b.txt": b"[00:01-00:02] checkpoint continuity durable recovery validator beta\n"},
            ),
        ),
        profile=TranscriptDedupeProfile(near_threshold=0.90, topic_threshold=0.30),
    )


def _group(group: TranscriptDuplicateGroup, **changes: Any) -> TranscriptDuplicateGroup:
    values: dict[str, Any] = {
        "kind": group.kind,
        "canonical_source_version_digest": group.canonical_source_version_digest,
        "member_source_version_digests": group.member_source_version_digests,
        "minimum_similarity": group.minimum_similarity,
        "profile_digest": group.profile_digest,
    }
    values.update(changes)
    values["group_id"] = transcript_duplicate_group_id(
        values["kind"],
        values["canonical_source_version_digest"],
        values["member_source_version_digests"],
        values["minimum_similarity"],
        values["profile_digest"],
    )
    return TranscriptDuplicateGroup(**values)


def _cluster(cluster: TranscriptTopicCluster, **changes: Any) -> TranscriptTopicCluster:
    values: dict[str, Any] = {
        "canonical_source_version_digest": cluster.canonical_source_version_digest,
        "member_source_version_digests": cluster.member_source_version_digests,
        "shared_terms": cluster.shared_terms,
        "profile_digest": cluster.profile_digest,
    }
    values.update(changes)
    values["cluster_id"] = transcript_topic_cluster_id(
        values["canonical_source_version_digest"],
        values["member_source_version_digests"],
        values["shared_terms"],
        values["profile_digest"],
    )
    return TranscriptTopicCluster(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"topic_threshold": 0.0},
        {"near_threshold": 1.1},
        {"topic_threshold": 0.9, "near_threshold": 0.8},
        {"shingle_size": 0},
        {"minimum_near_shingles": 0},
        {"minimum_topic_terms": 0},
        {"max_sources": 0},
        {"max_candidate_pairs": 0},
        {"profile_version": " "},
    ],
)
def test_dedupe_profile_rejects_invalid_threshold_limit_and_version(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationFailed):
        TranscriptDedupeProfile(**changes)


def test_source_and_duplicate_group_reject_provenance_members_canonical_and_similarity() -> None:
    topic_map = _exact_map()
    source = topic_map.sources[0]
    with pytest.raises(ValidationFailed, match="provenance drift"):
        replace(source, source_version_digest=digest("forged"))

    group = topic_map.duplicate_groups[0]
    with pytest.raises(ValidationFailed, match="en az iki"):
        _group(group, member_source_version_digests=(group.member_source_version_digests[0],))
    reversed_members = tuple(reversed(group.member_source_version_digests))
    with pytest.raises(ValidationFailed, match="tekil ve sirali"):
        _group(group, member_source_version_digests=reversed_members)
    with pytest.raises(ValidationFailed, match="canonical duplicate"):
        _group(group, canonical_source_version_digest=digest("outsider"))
    with pytest.raises(ValidationFailed, match=r"0\.\.1"):
        _group(group, minimum_similarity=1.1)


def test_topic_cluster_rejects_invalid_members_canonical_terms_and_semantic_id() -> None:
    cluster = _topic_map().topic_clusters[0]
    with pytest.raises(ValidationFailed, match="en az iki"):
        _cluster(cluster, member_source_version_digests=(cluster.member_source_version_digests[0],))
    with pytest.raises(ValidationFailed, match="tekil ve sirali"):
        _cluster(
            cluster,
            member_source_version_digests=tuple(reversed(cluster.member_source_version_digests)),
        )
    with pytest.raises(ValidationFailed, match="canonical topic"):
        _cluster(cluster, canonical_source_version_digest=digest("outsider"))
    with pytest.raises(ValidationFailed, match="shared term"):
        _cluster(cluster, shared_terms=())
    with pytest.raises(ValidationFailed, match="semantic digest"):
        replace(cluster, cluster_id=digest("forged"))


def test_map_rejects_schema_sources_and_duplicate_group_scope_policy_drift() -> None:
    topic_map = _exact_map()
    group = topic_map.duplicate_groups[0]
    with pytest.raises(ValidationFailed, match="schema/authority"):
        replace(topic_map, schema="wrong")
    with pytest.raises(ValidationFailed, match="schema/authority"):
        replace(topic_map, grants_authority=True)
    with pytest.raises(ValidationFailed, match="sources"):
        replace(topic_map, sources=())
    with pytest.raises(ValidationFailed, match="sources"):
        replace(topic_map, sources=(topic_map.sources[0], topic_map.sources[0]))

    other_profile = digest("other-profile")
    with pytest.raises(ValidationFailed, match="profile map"):
        replace(topic_map, duplicate_groups=(_group(group, profile_digest=other_profile),))

    outsider_members = tuple(sorted((digest("outside-a"), digest("outside-b"))))
    outsider = _group(
        group,
        canonical_source_version_digest=outsider_members[0],
        member_source_version_digests=outsider_members,
    )
    with pytest.raises(ValidationFailed, match="source kapsami"):
        replace(topic_map, duplicate_groups=(outsider,))
    with pytest.raises(ValidationFailed, match="source kapsami"):
        replace(topic_map, duplicate_groups=(group, group))

    alternate = next(
        item
        for item in group.member_source_version_digests
        if item != group.canonical_source_version_digest
    )
    with pytest.raises(ValidationFailed, match="canonical secimi"):
        replace(
            topic_map,
            duplicate_groups=(_group(group, canonical_source_version_digest=alternate),),
        )
    with pytest.raises(ValidationFailed, match="exact duplicate similarity"):
        replace(topic_map, duplicate_groups=(_group(group, minimum_similarity=0.9),))
    near = _group(group, kind=DuplicateKind.NEAR, minimum_similarity=0.1)
    with pytest.raises(ValidationFailed, match="near duplicate similarity"):
        replace(topic_map, duplicate_groups=(near,))


def test_map_rejects_topic_cluster_profile_scope_and_canonical_drift() -> None:
    topic_map = _topic_map()
    cluster = topic_map.topic_clusters[0]
    with pytest.raises(ValidationFailed, match="profile map"):
        replace(topic_map, topic_clusters=(_cluster(cluster, profile_digest=digest("other")),))
    outsider_members = tuple(sorted((digest("outside-a"), digest("outside-b"))))
    outsider = _cluster(
        cluster,
        canonical_source_version_digest=outsider_members[0],
        member_source_version_digests=outsider_members,
    )
    with pytest.raises(ValidationFailed, match="bilinmeyen source"):
        replace(topic_map, topic_clusters=(outsider,))
    alternate = next(
        item
        for item in cluster.member_source_version_digests
        if item != cluster.canonical_source_version_digest
    )
    with pytest.raises(ValidationFailed, match="canonical secimi"):
        replace(
            topic_map,
            topic_clusters=(_cluster(cluster, canonical_source_version_digest=alternate),),
        )
