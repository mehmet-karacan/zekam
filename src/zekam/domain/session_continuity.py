"""Typed, authority-free session continuity contracts.

The records in this module carry only portable references, digests and bounded
metadata.  Raw prompts, responses, transcripts and secret values deliberately
have no representation in the contracts.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_REFERENCES = 128
MAX_REFERENCE_LENGTH = 512
MAX_REASON_LENGTH = 96
MAX_RECURSION_DEPTH = 16
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_.:-]{0,95}$")
_FORBIDDEN_KEY = re.compile(
    r"^(?:secret(?:[-_]?value)?|credential(?:[-_]?value)?|password|private[-_]?key|"
    r"prompt(?:[-_]?(?:body|text))?|response(?:[-_]?(?:body|text))?|"
    r"transcript(?:[-_]?(?:body|text))?|raw[-_]?(?:content|prompt|response|transcript)|"
    r"owner[-_]?token)$",
    re.IGNORECASE,
)


class TruthClass(StrEnum):
    USER_DECISION = "USER_DECISION"
    REPO_FACT = "REPO_FACT"
    EXTERNAL_VERIFIED_FACT = "EXTERNAL_VERIFIED_FACT"
    MODEL_INFERENCE = "MODEL_INFERENCE"
    TEMPORARY_ASSUMPTION = "TEMPORARY_ASSUMPTION"
    SUPERSEDED = "SUPERSEDED"
    UNKNOWN = "UNKNOWN"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"
    LOCAL_ONLY = "local-only"
    PII = "pii"
    CORPORATE_CONFIDENTIAL = "corporate-confidential"
    SECRET = "secret"
    RAW_TRANSCRIPT = "raw-transcript"
    DIAGNOSTIC_PAYLOAD = "diagnostic-payload"


class CloseStatus(StrEnum):
    CLOSED = "closed"
    DEGRADED = "degraded"
    RECOVERY_REQUIRED = "recovery-required"
    FAILED = "failed"


class CompactionStatus(StrEnum):
    PREPARED = "prepared"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery-required"
    FAILED = "failed"


def _timezone_aware(value: dt.datetime, label: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationFailed(f"{label} timezone-aware olmali")


def _portable_ref(value: str, label: str) -> None:
    if not value or value != value.strip() or len(value) > MAX_REFERENCE_LENGTH:
        raise ValidationFailed(f"{label} bos, padded veya fazla uzun olamaz")
    if any(ord(char) < 32 for char in value):
        raise ValidationFailed(f"{label} control karakteri tasiyamaz")
    if PureWindowsPath(value).is_absolute() or value.startswith("/") or "\\" in value:
        raise PolicyViolation(f"{label} absolute veya platforma bagli path tasiyamaz")
    if ".." in PurePosixPath(value).parts:
        raise PolicyViolation(f"{label} traversal tasiyamaz")


def _safe_name(value: str, label: str) -> None:
    if not _SAFE_NAME.fullmatch(value):
        raise ValidationFailed(f"{label} canonical isim formatinda olmali")
    if _FORBIDDEN_KEY.search(value):
        raise PolicyViolation(f"{label} raw veya hassas alan adi tasiyamaz")


def _bounded(items: tuple[Any, ...], label: str, *, required: bool = False) -> None:
    if required and not items:
        raise ValidationFailed(f"{label} bos olamaz")
    if len(items) > MAX_REFERENCES:
        raise ValidationFailed(f"{label} en fazla {MAX_REFERENCES} oge tasiyabilir")


def _unique(values: tuple[str, ...], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValidationFailed(f"{label} tekil olmali")


@dataclass(frozen=True, slots=True)
class DigestReference:
    ref: str
    digest_value: str
    truth_class: TruthClass

    def __post_init__(self) -> None:
        _portable_ref(self.ref, "Digest reference")
        parse_digest(self.digest_value)
        if not isinstance(self.truth_class, TruthClass):
            raise ValidationFailed("Truth class registry disinda")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "digest": self.digest_value,
            "truth_class": self.truth_class.value,
        }


@dataclass(frozen=True, slots=True)
class TypedMetadata:
    name: str
    value_ref: str
    value_digest: str
    truth_class: TruthClass

    def __post_init__(self) -> None:
        _safe_name(self.name, "Metadata name")
        _portable_ref(self.value_ref, "Metadata value ref")
        parse_digest(self.value_digest)

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "value_ref": self.value_ref,
            "value_digest": self.value_digest,
            "truth_class": self.truth_class.value,
        }


@dataclass(frozen=True, slots=True)
class ContextSelectionReference:
    ref: str
    content_digest: str
    token_count: int
    truth_class: TruthClass

    def __post_init__(self) -> None:
        _portable_ref(self.ref, "Context selection ref")
        parse_digest(self.content_digest)
        if self.token_count < 1:
            raise ValidationFailed("Context selection token count pozitif olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "content_digest": self.content_digest,
            "token_count": self.token_count,
            "truth_class": self.truth_class.value,
        }


@dataclass(frozen=True, slots=True)
class ContextOmissionReference:
    ref: str
    reason_code: str
    required: bool = False

    def __post_init__(self) -> None:
        _portable_ref(self.ref, "Context omission ref")
        _safe_name(self.reason_code, "Context omission reason")

    def as_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "reason_code": self.reason_code, "required": self.required}


@dataclass(frozen=True, slots=True)
class FreshnessDimension:
    name: str
    observed_digest: str
    expected_digest: str
    current: bool

    def __post_init__(self) -> None:
        _safe_name(self.name, "Freshness dimension")
        parse_digest(self.observed_digest)
        parse_digest(self.expected_digest)
        if self.current != (self.observed_digest == self.expected_digest):
            raise ValidationFailed("Freshness current degeri digest karsilastirmasiyla uyusmuyor")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "observed_digest": self.observed_digest,
            "expected_digest": self.expected_digest,
            "current": self.current,
        }


@dataclass(frozen=True, slots=True)
class SessionLifecycleEvent:
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    session_id: str
    client_id: str
    event_id: UUID
    event_type: str
    sequence: int
    previous_digest: str | None
    origin: str
    causation_id: str
    correlation_id: str
    recursion_depth: int
    source_revision: str
    plan_ref: str
    checkpoint_ref: str | None
    context_ref: str | None
    payload_digest: str
    metadata: tuple[TypedMetadata, ...]
    classification: DataClassification
    occurred_at: dt.datetime
    ingested_at: dt.datetime
    contains_prompt: bool = False
    contains_response: bool = False
    contains_transcript: bool = False
    grants_authority: bool = False

    def __post_init__(self) -> None:
        for value, label in (
            (self.session_id, "Session id"),
            (self.client_id, "Client id"),
            (self.origin, "Origin"),
            (self.causation_id, "Causation id"),
            (self.correlation_id, "Correlation id"),
            (self.source_revision, "Source revision"),
            (self.plan_ref, "Plan ref"),
        ):
            _portable_ref(value, label)
        _safe_name(self.event_type, "Event type")
        if self.checkpoint_ref is not None:
            _portable_ref(self.checkpoint_ref, "Checkpoint ref")
        if self.context_ref is not None:
            _portable_ref(self.context_ref, "Context ref")
        if self.sequence < 1 or (self.sequence == 1) != (self.previous_digest is None):
            raise ValidationFailed("Session lifecycle sequence/previous zinciri gecersiz")
        if self.previous_digest is not None:
            parse_digest(self.previous_digest)
        parse_digest(self.payload_digest)
        if not 0 <= self.recursion_depth <= MAX_RECURSION_DEPTH:
            raise PolicyViolation("Lifecycle recursion depth policy sinirini asiyor")
        _bounded(self.metadata, "Lifecycle metadata")
        _unique(tuple(item.name for item in self.metadata), "Lifecycle metadata names")
        _timezone_aware(self.occurred_at, "Lifecycle occurred_at")
        _timezone_aware(self.ingested_at, "Lifecycle ingested_at")
        if self.ingested_at < self.occurred_at:
            raise ValidationFailed("Lifecycle ingested_at occurred_at oncesi olamaz")
        if (
            self.contains_prompt
            or self.contains_response
            or self.contains_transcript
            or self.grants_authority
        ):
            raise PolicyViolation("Lifecycle raw content veya authority tasiyamaz")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-session-lifecycle-event/v1",
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "run_id": str(self.run_id),
            "session_id": self.session_id,
            "client_id": self.client_id,
            "event_id": str(self.event_id),
            "event_type": self.event_type,
            "sequence": self.sequence,
            "previous_digest": self.previous_digest,
            "origin": self.origin,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "recursion_depth": self.recursion_depth,
            "source_revision": self.source_revision,
            "plan_ref": self.plan_ref,
            "checkpoint_ref": self.checkpoint_ref,
            "context_ref": self.context_ref,
            "payload_digest": self.payload_digest,
            "metadata": [item.as_dict() for item in self.metadata],
            "classification": self.classification.value,
            "occurred_at": self.occurred_at,
            "ingested_at": self.ingested_at,
            "contains_prompt": False,
            "contains_response": False,
            "contains_transcript": False,
            "grants_authority": False,
        }

    @property
    def event_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class SessionHydrationReceipt:
    receipt_id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    session_id: str
    client_id: str
    plan_ref: str
    checkpoint_ref: str
    source_digest: str
    policy_digest: str
    migration_digest: str
    inventory_digest: str
    context_digest: str
    required_selections: tuple[ContextSelectionReference, ...]
    optional_selections: tuple[ContextSelectionReference, ...]
    omissions: tuple[ContextOmissionReference, ...]
    token_budget: int
    tokens_used: int
    freshness: tuple[FreshnessDimension, ...]
    projection_refs: tuple[DigestReference, ...]
    hydration_event_digest: str
    created_at: dt.datetime
    fresh: bool
    complete: bool
    grants_authority: bool = False

    def __post_init__(self) -> None:
        for value, label in (
            (self.session_id, "Hydration session"),
            (self.client_id, "Hydration client"),
            (self.plan_ref, "Hydration plan"),
            (self.checkpoint_ref, "Hydration checkpoint"),
        ):
            _portable_ref(value, label)
        for value in (
            self.source_digest,
            self.policy_digest,
            self.migration_digest,
            self.inventory_digest,
            self.context_digest,
            self.hydration_event_digest,
        ):
            parse_digest(value)
        for items, label in (
            (self.required_selections, "Required hydration selections"),
            (self.optional_selections, "Optional hydration selections"),
            (self.omissions, "Hydration omissions"),
            (self.freshness, "Hydration freshness"),
            (self.projection_refs, "Hydration projection refs"),
        ):
            _bounded(items, label)
        selected_refs = tuple(
            item.ref for item in self.required_selections + self.optional_selections
        )
        _unique(selected_refs, "Hydration selections")
        _unique(tuple(item.ref for item in self.omissions), "Hydration omissions")
        if set(selected_refs) & {item.ref for item in self.omissions}:
            raise ValidationFailed("Hydration selection ayni anda omitted olamaz")
        expected_tokens = sum(
            item.token_count for item in self.required_selections + self.optional_selections
        )
        if self.token_budget < 1 or self.tokens_used != expected_tokens:
            raise ValidationFailed("Hydration token kullanimi exact selection toplami olmali")
        required_tokens = sum(item.token_count for item in self.required_selections)
        if required_tokens > self.token_budget or any(item.required for item in self.omissions):
            raise PolicyViolation("Required continuity set sessizce truncate edilemez")
        if self.tokens_used > self.token_budget:
            raise ValidationFailed("Hydration token budget asildi")
        expected_fresh = bool(self.freshness) and all(item.current for item in self.freshness)
        if self.fresh != expected_fresh:
            raise ValidationFailed("Hydration fresh flag dimension sonucu ile uyusmuyor")
        if self.complete != (not any(item.required for item in self.omissions)):
            raise ValidationFailed("Hydration complete flag required omission sonucu ile uyusmuyor")
        _timezone_aware(self.created_at, "Hydration created_at")
        if self.grants_authority:
            raise PolicyViolation("Hydration receipt authority uretemez")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-session-hydration-receipt/v1",
            "receipt_id": str(self.receipt_id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "run_id": str(self.run_id),
            "session_id": self.session_id,
            "client_id": self.client_id,
            "plan_ref": self.plan_ref,
            "checkpoint_ref": self.checkpoint_ref,
            "source_digest": self.source_digest,
            "policy_digest": self.policy_digest,
            "migration_digest": self.migration_digest,
            "inventory_digest": self.inventory_digest,
            "context_digest": self.context_digest,
            "required_selections": [item.as_dict() for item in self.required_selections],
            "optional_selections": [item.as_dict() for item in self.optional_selections],
            "omissions": [item.as_dict() for item in self.omissions],
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "freshness": [item.as_dict() for item in self.freshness],
            "projection_refs": [item.as_dict() for item in self.projection_refs],
            "hydration_event_digest": self.hydration_event_digest,
            "created_at": self.created_at,
            "fresh": self.fresh,
            "complete": self.complete,
            "grants_authority": False,
        }

    @property
    def receipt_digest(self) -> str:
        return digest(self.body())

    def document(self) -> dict[str, Any]:
        return self.body() | {"receipt_digest": self.receipt_digest}


@dataclass(frozen=True, slots=True)
class SessionCloseReceipt:
    receipt_id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    session_id: str
    client_id: str
    job_id: UUID
    attempt_id: UUID
    envelope_digest: str
    fencing_token: int
    completed_steps: tuple[DigestReference, ...]
    changed_artifacts: tuple[DigestReference, ...]
    verified_outcomes: tuple[DigestReference, ...]
    pending_steps: tuple[DigestReference, ...]
    next_safe_action: DigestReference
    human_decisions: tuple[DigestReference, ...]
    discovered_constraints: tuple[DigestReference, ...]
    failure_recovery_refs: tuple[DigestReference, ...]
    candidate_lessons: tuple[DigestReference, ...]
    candidate_skills: tuple[DigestReference, ...]
    checkpoint_ref: DigestReference
    journal_head: DigestReference
    source_digest: str
    policy_digest: str
    migration_digest: str
    context_digest: str
    status: CloseStatus
    closed_at: dt.datetime
    grants_authority: bool = False

    def __post_init__(self) -> None:
        _portable_ref(self.session_id, "Close session")
        _portable_ref(self.client_id, "Close client")
        for value in (
            self.envelope_digest,
            self.source_digest,
            self.policy_digest,
            self.migration_digest,
            self.context_digest,
        ):
            parse_digest(value)
        if self.fencing_token < 1:
            raise ValidationFailed("Close fencing token pozitif olmali")
        for items, label in (
            (self.completed_steps, "Completed steps"),
            (self.changed_artifacts, "Changed artifacts"),
            (self.verified_outcomes, "Verified outcomes"),
            (self.pending_steps, "Pending steps"),
            (self.human_decisions, "Human decisions"),
            (self.discovered_constraints, "Discovered constraints"),
            (self.failure_recovery_refs, "Failure/recovery refs"),
            (self.candidate_lessons, "Candidate lessons"),
            (self.candidate_skills, "Candidate skills"),
        ):
            _bounded(items, label)
            _unique(tuple(item.ref for item in items), label)
        if self.status is CloseStatus.CLOSED and self.pending_steps:
            raise ValidationFailed("Closed receipt pending step tasiyamaz")
        if self.status is CloseStatus.RECOVERY_REQUIRED and not self.failure_recovery_refs:
            raise ValidationFailed("Recovery-required close recovery ref ister")
        _timezone_aware(self.closed_at, "Close closed_at")
        if self.grants_authority:
            raise PolicyViolation("Close receipt authority uretemez")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-session-close-receipt/v1",
            "receipt_id": str(self.receipt_id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "run_id": str(self.run_id),
            "session_id": self.session_id,
            "client_id": self.client_id,
            "job_id": str(self.job_id),
            "attempt_id": str(self.attempt_id),
            "envelope_digest": self.envelope_digest,
            "fencing_token": self.fencing_token,
            "completed_steps": [item.as_dict() for item in self.completed_steps],
            "changed_artifacts": [item.as_dict() for item in self.changed_artifacts],
            "verified_outcomes": [item.as_dict() for item in self.verified_outcomes],
            "pending_steps": [item.as_dict() for item in self.pending_steps],
            "next_safe_action": self.next_safe_action.as_dict(),
            "human_decisions": [item.as_dict() for item in self.human_decisions],
            "discovered_constraints": [item.as_dict() for item in self.discovered_constraints],
            "failure_recovery_refs": [item.as_dict() for item in self.failure_recovery_refs],
            "candidate_lessons": [item.as_dict() for item in self.candidate_lessons],
            "candidate_skills": [item.as_dict() for item in self.candidate_skills],
            "checkpoint_ref": self.checkpoint_ref.as_dict(),
            "journal_head": self.journal_head.as_dict(),
            "source_digest": self.source_digest,
            "policy_digest": self.policy_digest,
            "migration_digest": self.migration_digest,
            "context_digest": self.context_digest,
            "status": self.status.value,
            "closed_at": self.closed_at,
            "grants_authority": False,
        }

    @property
    def receipt_digest(self) -> str:
        return digest(self.body())

    def document(self) -> dict[str, Any]:
        return self.body() | {"receipt_digest": self.receipt_digest}


@dataclass(frozen=True, slots=True)
class CompactionReceipt:
    receipt_id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    session_id: str
    client_id: str
    pre_compaction_event_digest: str
    checkpoint_draft_digest: str
    outbox_ref: str
    outbox_payload_digest: str
    worker_result_digest: str | None
    checkpoint_ref: str | None
    checkpoint_digest: str | None
    post_compaction_event_digest: str | None
    rehydration_receipt_digest: str | None
    status: CompactionStatus
    created_at: dt.datetime
    completed_at: dt.datetime | None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        _portable_ref(self.session_id, "Compaction session")
        _portable_ref(self.client_id, "Compaction client")
        _portable_ref(self.outbox_ref, "Compaction outbox")
        for value in (
            self.pre_compaction_event_digest,
            self.checkpoint_draft_digest,
            self.outbox_payload_digest,
        ):
            parse_digest(value)
        for digest_value in (
            self.worker_result_digest,
            self.checkpoint_digest,
            self.post_compaction_event_digest,
            self.rehydration_receipt_digest,
        ):
            if digest_value is not None:
                parse_digest(digest_value)
        if self.checkpoint_ref is not None:
            _portable_ref(self.checkpoint_ref, "Compaction checkpoint")
        completed_values = (
            self.worker_result_digest,
            self.checkpoint_ref,
            self.checkpoint_digest,
            self.post_compaction_event_digest,
            self.rehydration_receipt_digest,
            self.completed_at,
        )
        if self.status is CompactionStatus.COMPLETED and any(
            value is None for value in completed_values
        ):
            raise ValidationFailed("Completed compaction exact terminal zincir ister")
        if self.status is not CompactionStatus.COMPLETED and self.completed_at is not None:
            raise ValidationFailed("Terminal olmayan compaction completed_at tasiyamaz")
        _timezone_aware(self.created_at, "Compaction created_at")
        if self.completed_at is not None:
            _timezone_aware(self.completed_at, "Compaction completed_at")
            if self.completed_at < self.created_at:
                raise ValidationFailed("Compaction completed_at created_at oncesi olamaz")
        if self.grants_authority:
            raise PolicyViolation("Compaction receipt authority uretemez")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-compaction-receipt/v1",
            "receipt_id": str(self.receipt_id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "run_id": str(self.run_id),
            "session_id": self.session_id,
            "client_id": self.client_id,
            "pre_compaction_event_digest": self.pre_compaction_event_digest,
            "checkpoint_draft_digest": self.checkpoint_draft_digest,
            "outbox_ref": self.outbox_ref,
            "outbox_payload_digest": self.outbox_payload_digest,
            "worker_result_digest": self.worker_result_digest,
            "checkpoint_ref": self.checkpoint_ref,
            "checkpoint_digest": self.checkpoint_digest,
            "post_compaction_event_digest": self.post_compaction_event_digest,
            "rehydration_receipt_digest": self.rehydration_receipt_digest,
            "status": self.status.value,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "grants_authority": False,
        }

    @property
    def receipt_digest(self) -> str:
        return digest(self.body())

    def document(self) -> dict[str, Any]:
        return self.body() | {"receipt_digest": self.receipt_digest}


@dataclass(frozen=True, slots=True)
class ProjectionGenerationReceipt:
    receipt_id: UUID
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    source_ref: str
    source_digest: str
    projection_ref: str
    projection_digest: str
    generator_version: str
    generated_at: dt.datetime
    classification: DataClassification = DataClassification.PUBLIC
    public_filtered: bool = True
    grants_authority: bool = False

    def __post_init__(self) -> None:
        for value, label in (
            (self.source_ref, "Projection source ref"),
            (self.projection_ref, "Projection ref"),
            (self.generator_version, "Projection generator version"),
        ):
            _portable_ref(value, label)
        parse_digest(self.source_digest)
        parse_digest(self.projection_digest)
        _timezone_aware(self.generated_at, "Projection generated_at")
        if self.classification is not DataClassification.PUBLIC or not self.public_filtered:
            raise PolicyViolation("Projection public-filtered ve public sinifli olmali")
        if self.grants_authority:
            raise PolicyViolation("Projection receipt authority uretemez")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-projection-generation-receipt/v1",
            "receipt_id": str(self.receipt_id),
            "realm_id": str(self.realm_id),
            "project_id": str(self.project_id),
            "work_item_id": str(self.work_item_id),
            "source_ref": self.source_ref,
            "source_digest": self.source_digest,
            "projection_ref": self.projection_ref,
            "projection_digest": self.projection_digest,
            "generator_version": self.generator_version,
            "generated_at": self.generated_at,
            "classification": self.classification.value,
            "public_filtered": True,
            "grants_authority": False,
        }

    @property
    def receipt_digest(self) -> str:
        return digest(self.body())

    def document(self) -> dict[str, Any]:
        return self.body() | {"receipt_digest": self.receipt_digest}
