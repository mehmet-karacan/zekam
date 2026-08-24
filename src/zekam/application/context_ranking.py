"""Ham context metadata'sindan caller-score kabul etmeden rank feature turetir."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.context_continuity import MAX_FRESHNESS_SECONDS, ContextCandidate
from zekam.domain.context_scoring import (
    ContextRankFeatures,
    ScopeProximity,
    SourceRevisionState,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed

_RANKING_SNAPSHOT_KEY = secrets.token_bytes(32)


def count_context_tokens(content: str) -> int:
    """Provider-independent, deterministic ve konservatif UTF-8 byte budgeti."""

    return max(1, len(content.encode("utf-8")))


@dataclass(frozen=True, slots=True)
class ContextRankingRequest:
    role: str
    target_identity_refs: tuple[str, ...]
    step_scope_ref: str | None
    work_scope_ref: str | None
    project_scope_ref: str | None
    realm_scope_ref: str | None
    current_source_revision: str | None
    compatible_source_revisions: tuple[str, ...]
    task_terms: tuple[str, ...]
    tokenizer_profile_digest: str

    def __post_init__(self) -> None:
        if not self.role.strip():
            raise ValidationFailed("Context ranking role zorunlu")
        parse_digest(self.tokenizer_profile_digest)
        collections = (
            self.target_identity_refs,
            self.compatible_source_revisions,
            self.task_terms,
        )
        if any(len(set(values)) != len(values) for values in collections):
            raise ValidationFailed("Context ranking request degerleri tekil olmali")

    def body(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "target_identity_refs": sorted(self.target_identity_refs),
            "step_scope_ref": self.step_scope_ref,
            "work_scope_ref": self.work_scope_ref,
            "project_scope_ref": self.project_scope_ref,
            "realm_scope_ref": self.realm_scope_ref,
            "current_source_revision": self.current_source_revision,
            "compatible_source_revisions": sorted(self.compatible_source_revisions),
            "task_terms": sorted(self.task_terms),
            "tokenizer_profile_digest": self.tokenizer_profile_digest,
        }


@dataclass(frozen=True, slots=True)
class ContextRankingSnapshot:
    request: ContextRankingRequest
    realm_ref: str
    project_ref: str
    work_ref: str
    step_ref: str
    assignment_id: str
    assignment_digest: str
    source_snapshot_digest: str
    captured_at: dt.datetime
    expires_at: dt.datetime
    issuance_seal: str

    def __post_init__(self) -> None:
        try:
            UUID(self.assignment_id)
        except ValueError as exc:
            raise ValidationFailed("Context ranking assignment UUID gecersiz") from exc
        for value in (
            self.assignment_digest,
            self.source_snapshot_digest,
            self.issuance_seal,
        ):
            parse_digest(value)
        if (
            self.captured_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.expires_at <= self.captured_at
        ):
            raise ValidationFailed("Context ranking snapshot validity window gecersiz")
        if (
            self.request.realm_scope_ref != self.realm_ref
            or self.request.project_scope_ref != self.project_ref
            or self.request.work_scope_ref != self.work_ref
            or self.request.step_scope_ref != self.step_ref
        ):
            raise PolicyViolation("Context ranking snapshot scope binding drift")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-context-ranking-snapshot/v1",
            "request": self.request.body(),
            "realm_ref": self.realm_ref,
            "project_ref": self.project_ref,
            "work_ref": self.work_ref,
            "step_ref": self.step_ref,
            "assignment_id": self.assignment_id,
            "assignment_digest": self.assignment_digest,
            "source_snapshot_digest": self.source_snapshot_digest,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
        }

    @property
    def snapshot_digest(self) -> str:
        return digest(self.body())


class ContextRankingSnapshotIssuer:
    """Canonical assignment/work/source snapshot'ini process-kapsamli olarak muhurlar."""

    @staticmethod
    def issue(
        *,
        request: ContextRankingRequest,
        realm_ref: str,
        project_ref: str,
        work_ref: str,
        step_ref: str,
        assignment_id: str,
        assignment_digest: str,
        source_snapshot_digest: str,
        captured_at: dt.datetime,
        expires_at: dt.datetime,
    ) -> ContextRankingSnapshot:
        unsigned = ContextRankingSnapshot(
            request,
            realm_ref,
            project_ref,
            work_ref,
            step_ref,
            assignment_id,
            assignment_digest,
            source_snapshot_digest,
            captured_at,
            expires_at,
            digest("unsigned-ranking-snapshot"),
        )
        return ContextRankingSnapshot(
            request,
            realm_ref,
            project_ref,
            work_ref,
            step_ref,
            assignment_id,
            assignment_digest,
            source_snapshot_digest,
            captured_at,
            expires_at,
            ContextRankingSnapshotIssuer._seal(unsigned.body()),
        )

    @staticmethod
    def verify(snapshot: ContextRankingSnapshot, *, now: dt.datetime) -> None:
        if now.tzinfo is None or not snapshot.captured_at <= now < snapshot.expires_at:
            raise PolicyViolation("Context ranking snapshot stale veya future")
        if not hmac.compare_digest(
            snapshot.issuance_seal, ContextRankingSnapshotIssuer._seal(snapshot.body())
        ):
            raise PolicyViolation("Context ranking snapshot issuance provenance gecersiz")

    @staticmethod
    def _seal(body: dict[str, Any]) -> str:
        signature = hmac.new(
            _RANKING_SNAPSHOT_KEY,
            digest(body).encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"sha256:{signature}"


@dataclass(frozen=True, slots=True)
class ContextCandidateSet:
    ranking_snapshot_digest: str
    candidates: tuple[ContextCandidate, ...]
    contents: tuple[tuple[str, str], ...]
    candidate_fingerprint: str
    captured_at: dt.datetime
    expires_at: dt.datetime
    issuance_seal: str

    def __post_init__(self) -> None:
        for value in (
            self.ranking_snapshot_digest,
            self.candidate_fingerprint,
            self.issuance_seal,
        ):
            parse_digest(value)
        if not self.candidates:
            raise ValidationFailed("Context candidate set bos olamaz")
        if len({item.candidate_id for item in self.candidates}) != len(self.candidates):
            raise ValidationFailed("Context candidate set kimlikleri tekil olmali")
        if tuple(sorted(self.contents)) != self.contents:
            raise ValidationFailed("Context candidate set contents kararli sirada olmali")
        if {item.candidate_id for item in self.candidates} != {
            candidate_id for candidate_id, _ in self.contents
        }:
            raise PolicyViolation("Context candidate set exact content partition drift")
        expected_fingerprint = digest(
            [
                item.candidate_digest
                for item in sorted(self.candidates, key=lambda row: row.candidate_id)
            ]
        )
        if self.candidate_fingerprint != expected_fingerprint:
            raise PolicyViolation("Context candidate set fingerprint drift")
        if (
            self.captured_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.expires_at <= self.captured_at
        ):
            raise ValidationFailed("Context candidate set validity window gecersiz")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-context-candidate-set/v1",
            "ranking_snapshot_digest": self.ranking_snapshot_digest,
            "candidate_fingerprint": self.candidate_fingerprint,
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "candidate_digest": item.candidate_digest,
                    "content_digest": item.content_digest,
                    "token_count": item.token_count,
                    "required": item.required,
                    "provenance": item.provenance_body,
                }
                for item in sorted(self.candidates, key=lambda row: row.candidate_id)
            ],
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
        }

    @property
    def candidate_set_digest(self) -> str:
        return digest(self.body())

    def content_mapping(self) -> dict[str, str]:
        return dict(self.contents)


class ContextCandidateSetIssuer:
    """Trusted candidate adapter projectionini ranking snapshot'a muhurlar."""

    _key = secrets.token_bytes(32)

    @classmethod
    def issue(
        cls,
        snapshot: ContextRankingSnapshot,
        candidates: tuple[ContextCandidate, ...],
        contents: Mapping[str, str],
        *,
        now: dt.datetime,
    ) -> ContextCandidateSet:
        ContextRankingSnapshotIssuer.verify(snapshot, now=now)
        ContextRankingFeatureBuilder(snapshot.request).build_all(candidates, contents, now=now)
        fingerprint = digest(
            [item.candidate_digest for item in sorted(candidates, key=lambda row: row.candidate_id)]
        )
        unsigned = ContextCandidateSet(
            snapshot.snapshot_digest,
            candidates,
            tuple(sorted(contents.items())),
            fingerprint,
            now,
            snapshot.expires_at,
            digest("unsigned-candidate-set"),
        )
        return ContextCandidateSet(
            snapshot.snapshot_digest,
            candidates,
            tuple(sorted(contents.items())),
            fingerprint,
            now,
            snapshot.expires_at,
            cls._seal(unsigned.body()),
        )

    @classmethod
    def verify(
        cls,
        candidate_set: ContextCandidateSet,
        snapshot: ContextRankingSnapshot,
        *,
        now: dt.datetime,
    ) -> None:
        ContextRankingSnapshotIssuer.verify(snapshot, now=now)
        if (
            candidate_set.ranking_snapshot_digest != snapshot.snapshot_digest
            or not candidate_set.captured_at <= now < candidate_set.expires_at
            or not hmac.compare_digest(candidate_set.issuance_seal, cls._seal(candidate_set.body()))
        ):
            raise PolicyViolation("Context candidate set issuance/current binding gecersiz")

    @classmethod
    def _seal(cls, body: dict[str, Any]) -> str:
        signature = hmac.new(
            cls._key,
            digest(body).encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"sha256:{signature}"


@dataclass(frozen=True, slots=True)
class ContextRankingFeatureBuilder:
    request: ContextRankingRequest

    def build_all(
        self,
        candidates: tuple[ContextCandidate, ...],
        contents: Mapping[str, str],
        *,
        now: dt.datetime,
    ) -> dict[str, ContextRankFeatures]:
        if now.tzinfo is None:
            raise ValidationFailed("Context ranking timezone ister")
        if set(contents) != {item.candidate_id for item in candidates}:
            raise PolicyViolation("Context tokenizer exact content partition ister")
        for candidate in candidates:
            content = contents[candidate.candidate_id]
            if digest(content) != candidate.content_digest:
                raise PolicyViolation("Context tokenizer content digest drift")
            if count_context_tokens(content) != candidate.token_count:
                raise PolicyViolation("Context tokenizer token count drift")
        preliminary = {item.candidate_id: self._build(item, now=now) for item in candidates}
        groups: dict[str, list[str]] = {}
        for candidate in candidates:
            group = self._exact_duplicate_group(candidate)
            groups.setdefault(group, []).append(candidate.candidate_id)
        candidate_by_id = {item.candidate_id: item for item in candidates}
        for members in groups.values():
            token_counts = {candidate_by_id[candidate_id].token_count for candidate_id in members}
            if len(token_counts) != 1:
                raise PolicyViolation("Exact duplicate context token count drift")
        result: dict[str, ContextRankFeatures] = {}
        for candidate in candidates:
            base = preliminary[candidate.candidate_id]
            group = self._exact_duplicate_group(candidate)
            members = groups[group]
            result[candidate.candidate_id] = ContextRankFeatures(
                exact_identity=base.exact_identity,
                scope_proximity=base.scope_proximity,
                source_revision_state=base.source_revision_state,
                evidence_strength=base.evidence_strength,
                role_relevance=base.role_relevance,
                task_relevance=base.task_relevance,
                freshness_bucket=base.freshness_bucket,
                conflict_count=base.conflict_count,
                duplicate_group_digest=group if len(members) > 1 else None,
                duplicate_group_size=len(members),
                tokenizer_profile_digest=base.tokenizer_profile_digest,
            )
        return result

    def _build(self, candidate: ContextCandidate, *, now: dt.datetime) -> ContextRankFeatures:
        if candidate.tokenizer_profile_digest != self.request.tokenizer_profile_digest:
            raise PolicyViolation("Context tokenizer profile drift")
        target_ids = set(self.request.target_identity_refs)
        candidate_ids = set(candidate.identity_refs)
        exact_identity = bool(target_ids and candidate_ids and target_ids & candidate_ids)
        scope = self._scope(candidate.scope_ref)
        revision = self._revision(candidate)
        role_relevance = 4 if self.request.role in candidate.applicable_roles else 0
        if not candidate.applicable_roles:
            role_relevance = 2
        requested_terms = set(self.request.task_terms)
        candidate_terms = set(candidate.task_terms)
        if not requested_terms or not candidate_terms:
            task_relevance = 2
        else:
            overlap = len(requested_terms & candidate_terms)
            task_relevance = min(4, (overlap * 4) // max(1, len(requested_terms)))
        age = max(0, int((now - candidate.observed_at).total_seconds()))
        freshness_bucket = max(0, 4 - (age * 4 // MAX_FRESHNESS_SECONDS))
        return ContextRankFeatures(
            exact_identity=exact_identity,
            scope_proximity=scope,
            source_revision_state=revision,
            evidence_strength=min(
                4, len({item.evidence_digest for item in candidate.evidence_refs})
            ),
            role_relevance=role_relevance,
            task_relevance=task_relevance,
            freshness_bucket=freshness_bucket,
            conflict_count=len(candidate.conflict_refs),
            duplicate_group_digest=None,
            duplicate_group_size=1,
            tokenizer_profile_digest=candidate.tokenizer_profile_digest,
        )

    def _scope(self, candidate_scope: str) -> ScopeProximity:
        for scope, rank in (
            (self.request.step_scope_ref, ScopeProximity.STEP),
            (self.request.work_scope_ref, ScopeProximity.WORK),
            (self.request.project_scope_ref, ScopeProximity.PROJECT),
            (self.request.realm_scope_ref, ScopeProximity.REALM),
        ):
            if scope is not None and candidate_scope == scope:
                return rank
        return (
            ScopeProximity.REALM
            if self.request.realm_scope_ref is None
            else ScopeProximity.EXTERNAL
        )

    def _revision(self, candidate: ContextCandidate) -> SourceRevisionState:
        if candidate.conflict_refs:
            return SourceRevisionState.CONFLICT
        current = self.request.current_source_revision
        if current is None:
            return SourceRevisionState.COMPATIBLE
        if candidate.source_revision == current:
            return SourceRevisionState.CURRENT
        compatible = set(self.request.compatible_source_revisions) | set(
            candidate.compatible_source_revisions
        )
        if candidate.source_revision in compatible:
            return SourceRevisionState.COMPATIBLE
        return SourceRevisionState.MISMATCH

    @staticmethod
    def _exact_duplicate_group(candidate: ContextCandidate) -> str:
        return digest(
            {
                "schema": "zekam-context-exact-duplicate/v1",
                "kind": candidate.kind.value,
                "scope_ref": candidate.scope_ref,
                "source_revision": candidate.source_revision,
                "content_digest": candidate.content_digest,
                "applicable_roles": sorted(candidate.applicable_roles),
            }
        )
