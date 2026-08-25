"""Transcript duplicate gruplari ve authority tasimayan topic haritasi."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import canonical_bytes, digest, parse_digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.transcript_corpus import (
    TranscriptCorpusEntry,
    transcript_source_version_digest,
)


class DuplicateKind(StrEnum):
    EXACT = "exact-duplicate"
    NEAR = "near-duplicate"


def transcript_duplicate_group_id(
    kind: DuplicateKind,
    canonical_source_version_digest: str,
    members: tuple[str, ...],
    minimum_similarity: float,
    profile_digest: str,
) -> str:
    return digest(
        {
            "schema": "zekam-transcript-duplicate-group/v1",
            "kind": str(kind),
            "canonical_source_version_digest": canonical_source_version_digest,
            "members": members,
            "minimum_similarity": minimum_similarity,
            "profile_digest": profile_digest,
        }
    )


def transcript_topic_cluster_id(
    canonical_source_version_digest: str,
    members: tuple[str, ...],
    shared_terms: tuple[str, ...],
    profile_digest: str,
) -> str:
    return digest(
        {
            "schema": "zekam-transcript-topic-cluster/v1",
            "canonical_source_version_digest": canonical_source_version_digest,
            "members": members,
            "shared_terms": shared_terms,
            "profile_digest": profile_digest,
        }
    )


@dataclass(frozen=True, slots=True)
class TranscriptDedupeProfile:
    """Deterministik similarity ve aday butcesi."""

    near_threshold: float = 0.82
    topic_threshold: float = 0.30
    shingle_size: int = 3
    minimum_near_shingles: int = 3
    minimum_topic_terms: int = 4
    max_sources: int = 5000
    max_candidate_pairs: int = 250_000
    profile_version: str = "1"

    def __post_init__(self) -> None:
        if not 0.0 < self.topic_threshold < self.near_threshold <= 1.0:
            raise ValidationFailed("topic/near esikleri sirali 0..1 araliginda olmali")
        if (
            min(
                self.shingle_size,
                self.minimum_near_shingles,
                self.minimum_topic_terms,
                self.max_sources,
                self.max_candidate_pairs,
            )
            <= 0
        ):
            raise ValidationFailed("dedupe profile sinirlari pozitif olmali")
        if not self.profile_version.strip():
            raise ValidationFailed("dedupe profile surumu bos olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-transcript-dedupe-profile/v1",
            "profile_version": self.profile_version,
            "near_threshold": self.near_threshold,
            "topic_threshold": self.topic_threshold,
            "shingle_size": self.shingle_size,
            "minimum_near_shingles": self.minimum_near_shingles,
            "minimum_topic_terms": self.minimum_topic_terms,
            "max_sources": self.max_sources,
            "max_candidate_pairs": self.max_candidate_pairs,
        }

    @property
    def profile_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class TranscriptMapSource:
    source_version_digest: str
    archive_digest: str
    entry: TranscriptCorpusEntry
    parser_ref: str
    parser_version: str
    parser_profile_digest: str
    source_policy_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.source_version_digest,
            self.archive_digest,
            self.entry.entry_digest,
            self.parser_profile_digest,
            self.source_policy_digest,
        ):
            parse_digest(value)
        expected = transcript_source_version_digest(
            archive_digest=self.archive_digest,
            entry_digest=self.entry.entry_digest,
            parser_ref=self.parser_ref,
            parser_version=self.parser_version,
            parser_profile_digest=self.parser_profile_digest,
            source_policy_digest=self.source_policy_digest,
        )
        if self.source_version_digest != expected:
            raise ValidationFailed("topic map source version provenance drift")

    @property
    def content_digest(self) -> str:
        return self.entry.content_digest

    @property
    def relative_path(self) -> str:
        return self.entry.relative_path

    @property
    def declared_date(self) -> str | None:
        return self.entry.declared_date

    @property
    def video_id(self) -> str | None:
        return self.entry.video_id

    @property
    def title(self) -> str | None:
        return self.entry.title

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_version_digest": self.source_version_digest,
            "archive_digest": self.archive_digest,
            "entry": {**self.entry.as_dict(), "entry_digest": self.entry.entry_digest},
            "parser_ref": self.parser_ref,
            "parser_version": self.parser_version,
            "parser_profile_digest": self.parser_profile_digest,
            "source_policy_digest": self.source_policy_digest,
        }


@dataclass(frozen=True, slots=True)
class TranscriptDuplicateGroup:
    group_id: str
    kind: DuplicateKind
    canonical_source_version_digest: str
    member_source_version_digests: tuple[str, ...]
    minimum_similarity: float
    profile_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.group_id)
        parse_digest(self.profile_digest)
        parse_digest(self.canonical_source_version_digest)
        if len(self.member_source_version_digests) < 2:
            raise ValidationFailed("duplicate group en az iki source ister")
        if (
            tuple(sorted(set(self.member_source_version_digests)))
            != self.member_source_version_digests
        ):
            raise ValidationFailed("duplicate group memberlari tekil ve sirali olmali")
        for value in self.member_source_version_digests:
            parse_digest(value)
        if self.canonical_source_version_digest not in self.member_source_version_digests:
            raise ValidationFailed("canonical duplicate group uyesi olmali")
        if not 0.0 <= self.minimum_similarity <= 1.0:
            raise ValidationFailed("duplicate similarity 0..1 olmali")
        if self.group_id != transcript_duplicate_group_id(
            self.kind,
            self.canonical_source_version_digest,
            self.member_source_version_digests,
            self.minimum_similarity,
            self.profile_digest,
        ):
            raise ValidationFailed("duplicate group semantic digest drift")

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "kind": str(self.kind),
            "canonical_source_version_digest": self.canonical_source_version_digest,
            "member_source_version_digests": list(self.member_source_version_digests),
            "minimum_similarity": self.minimum_similarity,
            "profile_digest": self.profile_digest,
            "automatic_content_merge": False,
        }


@dataclass(frozen=True, slots=True)
class TranscriptTopicCluster:
    cluster_id: str
    canonical_source_version_digest: str
    member_source_version_digests: tuple[str, ...]
    shared_terms: tuple[str, ...]
    profile_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.cluster_id)
        parse_digest(self.profile_digest)
        parse_digest(self.canonical_source_version_digest)
        if len(self.member_source_version_digests) < 2:
            raise ValidationFailed("topic cluster en az iki source ister")
        if (
            tuple(sorted(set(self.member_source_version_digests)))
            != self.member_source_version_digests
        ):
            raise ValidationFailed("topic cluster memberlari tekil ve sirali olmali")
        if self.canonical_source_version_digest not in self.member_source_version_digests:
            raise ValidationFailed("canonical topic cluster uyesi olmali")
        if tuple(sorted(set(self.shared_terms))) != self.shared_terms or not self.shared_terms:
            raise ValidationFailed("topic cluster shared term ister")
        if self.cluster_id != transcript_topic_cluster_id(
            self.canonical_source_version_digest,
            self.member_source_version_digests,
            self.shared_terms,
            self.profile_digest,
        ):
            raise ValidationFailed("topic cluster semantic digest drift")

    def as_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "relation": "same-topic-as",
            "canonical_source_version_digest": self.canonical_source_version_digest,
            "member_source_version_digests": list(self.member_source_version_digests),
            "shared_terms": list(self.shared_terms),
            "profile_digest": self.profile_digest,
            "implies_support": False,
            "implies_truth": False,
        }


@dataclass(frozen=True, slots=True)
class TranscriptTopicMap:
    profile: TranscriptDedupeProfile
    sources: tuple[TranscriptMapSource, ...]
    duplicate_groups: tuple[TranscriptDuplicateGroup, ...]
    topic_clusters: tuple[TranscriptTopicCluster, ...]
    schema: str = "zekam-transcript-topic-map/v1"
    grants_authority: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.schema != "zekam-transcript-topic-map/v1" or self.grants_authority:
            raise ValidationFailed("topic map schema/authority gecersiz")
        self.profile.__post_init__()
        for source in self.sources:
            source.__post_init__()
        source_ids = tuple(source.source_version_digest for source in self.sources)
        if source_ids != tuple(sorted(set(source_ids))) or not source_ids:
            raise ValidationFailed("topic map sources tekil ve sirali olmali")
        known = set(source_ids)
        by_id = {source.source_version_digest: source for source in self.sources}

        def canonical(members: tuple[str, ...]) -> str:
            return min(
                members,
                key=lambda value: (
                    by_id[value].declared_date or "9999-12-31",
                    by_id[value].video_id or "",
                    by_id[value].relative_path.casefold(),
                    value,
                ),
            )

        duplicate_members: set[str] = set()
        for group in self.duplicate_groups:
            group.__post_init__()
            if group.profile_digest != self.profile.profile_digest:
                raise ValidationFailed("duplicate group profile map ile uyusmuyor")
            members = set(group.member_source_version_digests)
            if not members <= known or duplicate_members & members:
                raise ValidationFailed("duplicate group source kapsami gecersiz")
            if group.canonical_source_version_digest != canonical(
                group.member_source_version_digests
            ):
                raise ValidationFailed("duplicate group canonical secimi gecersiz")
            if group.kind is DuplicateKind.EXACT and group.minimum_similarity != 1.0:
                raise ValidationFailed("exact duplicate similarity 1.0 olmali")
            if (
                group.kind is DuplicateKind.NEAR
                and group.minimum_similarity < self.profile.near_threshold
            ):
                raise ValidationFailed("near duplicate similarity esik altinda")
            duplicate_members |= members
        for cluster in self.topic_clusters:
            cluster.__post_init__()
            if cluster.profile_digest != self.profile.profile_digest:
                raise ValidationFailed("topic cluster profile map ile uyusmuyor")
            if not set(cluster.member_source_version_digests) <= known:
                raise ValidationFailed("topic cluster bilinmeyen source tasiyor")
            if cluster.canonical_source_version_digest != canonical(
                cluster.member_source_version_digests
            ):
                raise ValidationFailed("topic cluster canonical secimi gecersiz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "profile": self.profile.as_dict(),
            "profile_digest": self.profile.profile_digest,
            "source_set_digest": digest([source.as_dict() for source in self.sources]),
            "sources": [source.as_dict() for source in self.sources],
            "duplicate_groups": [group.as_dict() for group in self.duplicate_groups],
            "topic_clusters": [cluster.as_dict() for cluster in self.topic_clusters],
            "grants_authority": False,
        }

    @property
    def map_digest(self) -> str:
        return digest(self.as_dict())

    def to_bytes(self) -> bytes:
        return canonical_bytes({**self.as_dict(), "map_digest": self.map_digest})

    def collapse_duplicates(self, source_versions: tuple[str, ...]) -> tuple[str, ...]:
        """Yalniz duplicate gruplari collapse eder; topic cluster merge edilmez."""

        replacements = {
            member: group.canonical_source_version_digest
            for group in self.duplicate_groups
            for member in group.member_source_version_digests
        }
        known = {source.source_version_digest for source in self.sources}
        selected: set[str] = set()
        for value in source_versions:
            if value not in known:
                raise ValidationFailed("collapse bilinmeyen source version tasiyor")
            selected.add(replacements.get(value, value))
        return tuple(sorted(selected))
