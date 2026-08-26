"""Candidate-only Memory Compiler output contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.policy import RiskLevel
from zekam.domain.session_continuity import (
    DataClassification,
    DigestReference,
    TruthClass,
    _bounded,
    _portable_ref,
    _safe_name,
    _unique,
)


class CompilerCandidateType(StrEnum):
    DURABLE_DECISION = "durable_decision"
    PROJECT_FACT = "project_fact"
    PROJECT_CONVENTION = "project_convention"
    REUSABLE_LESSON = "reusable_lesson"
    SKILL_CANDIDATE = "skill_candidate"
    FAILURE_PATTERN = "failure_pattern"
    KNOWN_ISSUE = "known_issue"
    UNRESOLVED_WORK = "unresolved_work"
    OBSOLETE_KNOWLEDGE_CANDIDATE = "obsolete_knowledge_candidate"
    CONFLICT_CANDIDATE = "conflict_candidate"
    PROJECTION_REFRESH_REQUEST = "projection_refresh_request"


@dataclass(frozen=True, slots=True)
class CompilerCandidate:
    candidate_id: str
    logical_key: str
    content_ref: str
    content_digest: str
    truth_class: TruthClass
    classification: DataClassification
    candidate_type: CompilerCandidateType
    risk: RiskLevel
    source_refs: tuple[DigestReference, ...]
    evidence_refs: tuple[DigestReference, ...]
    review_required: bool = True

    def __post_init__(self) -> None:
        _portable_ref(self.candidate_id, "Compiler candidate id")
        _portable_ref(self.logical_key, "Compiler logical key")
        _portable_ref(self.content_ref, "Compiler content ref")
        parse_digest(self.content_digest)
        _bounded(self.source_refs, "Compiler candidate sources", required=True)
        _bounded(self.evidence_refs, "Compiler candidate evidence", required=True)
        if self.risk not in (
            RiskLevel.LOW,
            RiskLevel.MEDIUM,
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        ):
            raise ValidationFailed("Compiler candidate risk low..critical olmali")
        if self.truth_class in (TruthClass.SUPERSEDED, TruthClass.UNKNOWN):
            raise ValidationFailed("Superseded/unknown compiler candidate uretilemez")
        if not self.review_required:
            raise PolicyViolation("Compiler candidate independent review kapisini atlayamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "logical_key": self.logical_key,
            "content_ref": self.content_ref,
            "content_digest": self.content_digest,
            "truth_class": self.truth_class.value,
            "classification": self.classification.value,
            "candidate_type": self.candidate_type.value,
            "risk": self.risk.value,
            "source_refs": [item.as_dict() for item in self.source_refs],
            "evidence_refs": [item.as_dict() for item in self.evidence_refs],
            "state": "candidate",
            "review_required": True,
            "direct_promotion": False,
            "grants_authority": False,
        }

    @property
    def candidate_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class CompilerRejection:
    source_ref: str
    reason_code: str
    evidence_digest: str
    quarantined: bool

    def __post_init__(self) -> None:
        _portable_ref(self.source_ref, "Compiler rejection source")
        _safe_name(self.reason_code, "Compiler rejection reason")
        parse_digest(self.evidence_digest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_ref": self.source_ref,
            "reason_code": self.reason_code,
            "evidence_digest": self.evidence_digest,
            "quarantined": self.quarantined,
        }


@dataclass(frozen=True, slots=True)
class CandidateGroup:
    group_digest: str
    candidate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        parse_digest(self.group_digest)
        if not 2 <= len(self.candidate_ids) <= 128:
            raise ValidationFailed("Compiler group iki ile 128 candidate ister")
        _unique(self.candidate_ids, "Compiler candidate group")
        for candidate_id in self.candidate_ids:
            _portable_ref(candidate_id, "Compiler group candidate")

    def as_dict(self) -> dict[str, Any]:
        return {"group_digest": self.group_digest, "candidate_ids": list(self.candidate_ids)}


@dataclass(frozen=True, slots=True)
class MemoryCompilerOutput:
    output_id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    source_set: tuple[DigestReference, ...]
    source_watermark: str
    parser_digest: str
    compiler_digest: str
    policy_digest: str
    profile_digest: str
    candidates: tuple[CompilerCandidate, ...]
    rejected: tuple[CompilerRejection, ...]
    duplicate_groups: tuple[CandidateGroup, ...]
    conflict_groups: tuple[CandidateGroup, ...]
    gateway_request_ref: str | None
    gateway_request_digest: str | None
    gateway_response_ref: str | None
    gateway_response_digest: str | None
    created_at: dt.datetime
    direct_promotion: bool = False
    grants_authority: bool = False

    def __post_init__(self) -> None:
        _bounded(self.source_set, "Compiler source set", required=True)
        _bounded(self.candidates, "Compiler candidates")
        _bounded(self.rejected, "Compiler rejections")
        _bounded(self.duplicate_groups, "Compiler duplicate groups")
        _bounded(self.conflict_groups, "Compiler conflict groups")
        _portable_ref(self.source_watermark, "Compiler source watermark")
        for value in (
            self.parser_digest,
            self.compiler_digest,
            self.policy_digest,
            self.profile_digest,
        ):
            parse_digest(value)
        gateway_fields = (
            self.gateway_request_ref,
            self.gateway_request_digest,
            self.gateway_response_ref,
            self.gateway_response_digest,
        )
        if any(value is not None for value in gateway_fields) and any(
            value is None for value in gateway_fields
        ):
            raise ValidationFailed("Compiler gateway refs/digests hep birlikte olmali")
        for ref in (self.gateway_request_ref, self.gateway_response_ref):
            if ref is not None:
                _portable_ref(ref, "Compiler gateway ref")
        for digest_value in (self.gateway_request_digest, self.gateway_response_digest):
            if digest_value is not None:
                parse_digest(digest_value)
        candidate_ids = tuple(candidate.candidate_id for candidate in self.candidates)
        _unique(candidate_ids, "Compiler candidates")
        known = set(candidate_ids)
        for group in self.duplicate_groups + self.conflict_groups:
            if not set(group.candidate_ids) <= known:
                raise ValidationFailed("Compiler group output disi candidate tasiyor")
        rejected_refs = tuple(item.source_ref for item in self.rejected)
        _unique(rejected_refs, "Compiler rejection sources")
        if (
            self.created_at.tzinfo is None
            or self.created_at.tzinfo.utcoffset(self.created_at) is None
        ):
            raise ValidationFailed("Compiler output created_at timezone-aware olmali")
        if self.direct_promotion or self.grants_authority:
            raise PolicyViolation("Compiler output direct promotion veya authority uretemez")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-compiler-output/v1",
            "output_id": str(self.output_id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "run_id": str(self.run_id),
            "source_set": [item.as_dict() for item in self.source_set],
            "source_watermark": self.source_watermark,
            "parser_digest": self.parser_digest,
            "compiler_digest": self.compiler_digest,
            "policy_digest": self.policy_digest,
            "profile_digest": self.profile_digest,
            "candidates": [item.as_dict() for item in self.candidates],
            "rejected": [item.as_dict() for item in self.rejected],
            "duplicate_groups": [item.as_dict() for item in self.duplicate_groups],
            "conflict_groups": [item.as_dict() for item in self.conflict_groups],
            "gateway_request_ref": self.gateway_request_ref,
            "gateway_request_digest": self.gateway_request_digest,
            "gateway_response_ref": self.gateway_response_ref,
            "gateway_response_digest": self.gateway_response_digest,
            "created_at": self.created_at,
            "direct_promotion": False,
            "grants_authority": False,
        }

    @property
    def output_digest(self) -> str:
        return digest(self.body())

    def document(self) -> dict[str, Any]:
        return self.body() | {"output_digest": self.output_digest}
