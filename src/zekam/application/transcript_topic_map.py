"""Transcript duplicate ve topic cluster haritasi builder'i."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from zekam.application.transcript_corpus_import import (
    ContentAddressedStore,
    TranscriptArchiveScan,
    normalize_transcript_payload,
)
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.transcript_topic_map import (
    DuplicateKind,
    TranscriptDedupeProfile,
    TranscriptDuplicateGroup,
    TranscriptMapSource,
    TranscriptTopicCluster,
    TranscriptTopicMap,
    transcript_duplicate_group_id,
    transcript_topic_cluster_id,
)

_TERM = re.compile(r"[^\W_]+", re.UNICODE)
_STOPWORDS = frozenset(
    {
        "acaba",
        "ama",
        "ancak",
        "bir",
        "bu",
        "da",
        "de",
        "daha",
        "icin",
        "ile",
        "ise",
        "mi",
        "mu",
        "ve",
        "veya",
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
    }
)


@dataclass(frozen=True, slots=True)
class _PreparedSource:
    source: TranscriptMapSource
    terms: frozenset[str]
    shingles: frozenset[tuple[str, ...]]


class _UnionFind:
    def __init__(self, values: tuple[str, ...]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        self.parent[second] = first

    def components(self) -> tuple[tuple[str, ...], ...]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for value in sorted(self.parent):
            grouped[self.find(value)].append(value)
        return tuple(tuple(values) for _, values in sorted(grouped.items()) if len(values) >= 2)


def _terms(text: str) -> tuple[str, ...]:
    return tuple(
        term
        for term in (value.casefold() for value in _TERM.findall(text))
        if len(term) >= 3 and term not in _STOPWORDS
    )


def _shingles(terms: tuple[str, ...], size: int) -> frozenset[tuple[str, ...]]:
    if len(terms) < size:
        return frozenset()
    return frozenset(tuple(terms[index : index + size]) for index in range(len(terms) - size + 1))


def _jaccard(left: frozenset[object], right: frozenset[object]) -> float:
    union = left | right
    return 0.0 if not union else len(left & right) / len(union)


def _canonical(members: tuple[str, ...], by_id: dict[str, _PreparedSource]) -> str:
    def key(value: str) -> tuple[str, str, str, str]:
        source = by_id[value].source
        return (
            source.declared_date or "9999-12-31",
            source.video_id or "",
            source.relative_path.casefold(),
            value,
        )

    return min(members, key=key)


def _candidate_pairs(
    prepared: tuple[_PreparedSource, ...],
    *,
    feature: str,
    maximum: int,
) -> tuple[tuple[str, str], ...]:
    inverted: dict[object, list[str]] = defaultdict(list)
    for item in prepared:
        feature_values = item.shingles if feature == "shingles" else item.terms
        for feature_value in feature_values:
            inverted[feature_value].append(item.source.source_version_digest)
    pairs: set[tuple[str, str]] = set()
    for source_values in inverted.values():
        ordered = sorted(set(source_values))
        for left_index, left in enumerate(ordered):
            for right in ordered[left_index + 1 :]:
                pairs.add((left, right))
                if len(pairs) > maximum:
                    raise PolicyViolation("transcript similarity candidate pair butcesini asiyor")
    return tuple(sorted(pairs))


def build_transcript_topic_map(
    scans: tuple[TranscriptArchiveScan, ...],
    *,
    profile: TranscriptDedupeProfile | None = None,
) -> TranscriptTopicMap:
    """Korpus scan'lerinden deterministic duplicate ve topic map uretir."""

    selected_profile = profile or TranscriptDedupeProfile()
    prepared_by_id: dict[str, _PreparedSource] = {}
    for scan in scans:
        scan.validate()
        for (_path, payload), entry in zip(scan.entry_payloads, scan.manifest.entries, strict=True):
            normalized_text, normalized_payload = normalize_transcript_payload(payload)
            if digest_of_bytes(normalized_payload) != entry.content_digest:
                raise ValidationFailed("topic map content digest drift")
            source_version = scan.manifest.source_version_digest(entry)
            source = TranscriptMapSource(
                source_version_digest=source_version,
                archive_digest=scan.manifest.archive_digest,
                entry=entry,
                parser_ref=scan.manifest.parser_ref,
                parser_version=scan.manifest.parser_version,
                parser_profile_digest=scan.manifest.parser_profile_digest,
                source_policy_digest=scan.manifest.source_policy_digest,
            )
            terms = _terms(normalized_text)
            candidate = _PreparedSource(
                source=source,
                terms=frozenset(terms),
                shingles=_shingles(terms, selected_profile.shingle_size),
            )
            previous = prepared_by_id.get(source_version)
            if previous is not None and previous != candidate:
                raise ValidationFailed("source version digest collision/drift")
            prepared_by_id[source_version] = candidate
    if not prepared_by_id:
        raise ValidationFailed("topic map en az bir source ister")
    if len(prepared_by_id) > selected_profile.max_sources:
        raise PolicyViolation("topic map source sayisi profil sinirini asiyor")
    prepared = tuple(prepared_by_id[key] for key in sorted(prepared_by_id))
    exact_groups: dict[str, list[str]] = defaultdict(list)
    for item in prepared:
        exact_groups[item.source.content_digest].append(item.source.source_version_digest)
    near_scores: dict[tuple[str, str], float] = {}
    for left, right in _candidate_pairs(
        prepared,
        feature="shingles",
        maximum=selected_profile.max_candidate_pairs,
    ):
        left_item, right_item = prepared_by_id[left], prepared_by_id[right]
        if (
            min(len(left_item.shingles), len(right_item.shingles))
            < selected_profile.minimum_near_shingles
        ):
            continue
        score = _jaccard(left_item.shingles, right_item.shingles)
        near_scores[(left, right)] = score

    duplicate_components: list[tuple[str, ...]] = [
        tuple(sorted(members)) for members in exact_groups.values()
    ]
    duplicate_components.sort()
    while True:
        merge_candidates: list[tuple[float, tuple[str, ...], tuple[str, ...]]] = []
        for left_index, left_component in enumerate(duplicate_components):
            for right_component in duplicate_components[left_index + 1 :]:
                cross_scores = [
                    near_scores.get((left, right) if left < right else (right, left), 0.0)
                    for left in left_component
                    for right in right_component
                ]
                minimum = min(cross_scores)
                if minimum >= selected_profile.near_threshold:
                    merge_candidates.append((minimum, left_component, right_component))
        if not merge_candidates:
            break
        _, left_component, right_component = min(
            merge_candidates,
            key=lambda item: (-item[0], item[1], item[2]),
        )
        duplicate_components.remove(left_component)
        duplicate_components.remove(right_component)
        duplicate_components.append(tuple(sorted((*left_component, *right_component))))
        duplicate_components.sort()

    duplicate_groups: list[TranscriptDuplicateGroup] = []
    duplicate_replacement: dict[str, str] = {}
    for component in (item for item in duplicate_components if len(item) >= 2):
        canonical = _canonical(component, prepared_by_id)
        content_digests = {prepared_by_id[value].source.content_digest for value in component}
        kind = DuplicateKind.EXACT if len(content_digests) == 1 else DuplicateKind.NEAR
        all_pair_scores = [
            near_scores.get((left, right) if left < right else (right, left), 0.0)
            for left_index, left in enumerate(component)
            for right in component[left_index + 1 :]
        ]
        minimum_similarity = 1.0 if kind is DuplicateKind.EXACT else min(all_pair_scores)
        group_id = transcript_duplicate_group_id(
            kind,
            canonical,
            component,
            minimum_similarity,
            selected_profile.profile_digest,
        )
        duplicate_groups.append(
            TranscriptDuplicateGroup(
                group_id=group_id,
                kind=kind,
                canonical_source_version_digest=canonical,
                member_source_version_digests=component,
                minimum_similarity=minimum_similarity,
                profile_digest=selected_profile.profile_digest,
            )
        )
        duplicate_replacement.update(dict.fromkeys(component, canonical))

    topic_sources = tuple(
        item
        for item in prepared
        if duplicate_replacement.get(
            item.source.source_version_digest, item.source.source_version_digest
        )
        == item.source.source_version_digest
    )
    topic_ids = tuple(item.source.source_version_digest for item in topic_sources)
    topic_uf = _UnionFind(topic_ids)
    for left, right in _candidate_pairs(
        topic_sources,
        feature="terms",
        maximum=selected_profile.max_candidate_pairs,
    ):
        left_terms, right_terms = prepared_by_id[left].terms, prepared_by_id[right].terms
        if min(len(left_terms), len(right_terms)) < selected_profile.minimum_topic_terms:
            continue
        if _jaccard(left_terms, right_terms) >= selected_profile.topic_threshold:
            topic_uf.union(left, right)

    topic_clusters: list[TranscriptTopicCluster] = []
    for component in topic_uf.components():
        counts = Counter(term for member in component for term in prepared_by_id[member].terms)
        shared = tuple(sorted(term for term, count in counts.items() if count >= 2))
        if not shared:
            continue
        canonical = _canonical(component, prepared_by_id)
        cluster_id = transcript_topic_cluster_id(
            canonical,
            component,
            shared,
            selected_profile.profile_digest,
        )
        topic_clusters.append(
            TranscriptTopicCluster(
                cluster_id=cluster_id,
                canonical_source_version_digest=canonical,
                member_source_version_digests=component,
                shared_terms=shared,
                profile_digest=selected_profile.profile_digest,
            )
        )
    return TranscriptTopicMap(
        profile=selected_profile,
        sources=tuple(item.source for item in prepared),
        duplicate_groups=tuple(sorted(duplicate_groups, key=lambda item: item.group_id)),
        topic_clusters=tuple(sorted(topic_clusters, key=lambda item: item.cluster_id)),
    )


@dataclass(frozen=True, slots=True)
class StoredTranscriptTopicMap:
    topic_map: TranscriptTopicMap
    object_digest: str

    def __post_init__(self) -> None:
        if self.object_digest != digest_of_bytes(self.topic_map.to_bytes()):
            raise ValidationFailed("stored topic map digest uyusmuyor")


def persist_transcript_topic_map(
    topic_map: TranscriptTopicMap, store: ContentAddressedStore
) -> StoredTranscriptTopicMap:
    topic_map.validate()
    payload = topic_map.to_bytes()
    expected = digest_of_bytes(payload)
    info = store.put(
        payload,
        media_type="application/vnd.zekam.transcript-topic-map+json",
        metadata={"map_digest": topic_map.map_digest},
    )
    if info.digest != expected or not store.exists(expected) or store.get(expected) != payload:
        raise ValidationFailed("topic map CAS read-after-write dogrulamasi basarisiz")
    return StoredTranscriptTopicMap(topic_map=topic_map, object_digest=expected)
