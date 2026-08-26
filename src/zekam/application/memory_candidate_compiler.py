"""Secure deterministic, candidate-only Memory Compiler preparation.

The compiler has no tool, file, database, Work Graph, authorization or
promotion capability.  Raw fragments are inspected in process and only bounded
content/source digests and candidate references leave this module.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory_compiler import (
    CandidateGroup,
    CompilerCandidate,
    CompilerCandidateType,
    CompilerRejection,
    MemoryCompilerOutput,
)
from zekam.domain.policy import RiskLevel
from zekam.domain.session_continuity import DataClassification, DigestReference, TruthClass

COMPILER_VERSION = "memory-candidate-compiler-v1"
MAX_FRAGMENT_CHARS = 16_000

_SENSITIVE = re.compile(
    r"(?:api[-_ ]?key|secret|credential|password|parola|private[-_ ]?key|"
    r"owner[-_ ]?token|bearer\s+[A-Za-z0-9._-]{8,}|"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)
_DIRECTIVE_SHAPE = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous\s+instructions|system\s+prompt|"
    r"developer\s+message|execute\s+(?:this\s+)?command|"
    r"write\s+(?:to\s+)?(?:the\s+)?(?:file|database|policy|config)|"
    r"grant\s+(?:me\s+)?authority|activate\s+(?:this\s+)?skill|"
    r"you\s+are\s+now|do\s+not\s+follow|<\s*system\s*>|tool[_ -]?call|"
    r"rm\s+-rf|powershell(?:\.exe)?|curl\s+https?://)",
    re.IGNORECASE,
)


class CompilerSourceKind(StrEnum):
    SESSION_EVENT = "session-event"
    WORK_JOURNAL = "work-journal"
    CHECKPOINT_DELTA = "checkpoint-delta"
    CLOSE_DELTA = "close-delta"
    DAYLOG = "daylog"
    KNOWLEDGE_INDEX = "knowledge-index"
    IMPORTED_TRANSCRIPT = "imported-transcript"
    MODEL_OUTPUT = "model-output"


_UNTRUSTED_KINDS = frozenset(
    {
        CompilerSourceKind.DAYLOG,
        CompilerSourceKind.KNOWLEDGE_INDEX,
        CompilerSourceKind.IMPORTED_TRANSCRIPT,
        CompilerSourceKind.MODEL_OUTPUT,
    }
)
_FACT_CLASSES = frozenset(
    {
        TruthClass.USER_DECISION,
        TruthClass.REPO_FACT,
        TruthClass.EXTERNAL_VERIFIED_FACT,
    }
)


@dataclass(frozen=True, slots=True)
class CompilerBudget:
    max_sources: int = 128
    max_candidates: int = 128
    max_total_chars: int = 128_000
    max_model_calls: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.max_sources <= 128 or not 1 <= self.max_candidates <= 128:
            raise ValidationFailed("compiler source/candidate budget 1..128 olmali")
        if self.max_total_chars < 1:
            raise ValidationFailed("compiler character budget pozitif olmali")
        if self.max_model_calls != 0:
            raise PolicyViolation("deterministic compiler hazirligi provider cagrisi yapamaz")


@dataclass(frozen=True, slots=True)
class CompilerSourceFragment:
    source: DigestReference
    source_kind: CompilerSourceKind
    source_revision: str
    expected_source_revision: str
    logical_key: str
    content_ref: str
    content: str
    expected_content_digest: str
    candidate_type: CompilerCandidateType
    proposed_truth_class: TruthClass
    classification: DataClassification
    risk: RiskLevel
    evidence_refs: tuple[DigestReference, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, CompilerSourceKind):
            raise ValidationFailed("compiler source kind registry disinda")
        if not isinstance(self.candidate_type, CompilerCandidateType):
            raise ValidationFailed("compiler candidate type registry disinda")
        if not isinstance(self.proposed_truth_class, TruthClass) or not isinstance(
            self.classification, DataClassification
        ):
            raise ValidationFailed("compiler truth/classification registry disinda")
        if self.risk not in (
            RiskLevel.LOW,
            RiskLevel.MEDIUM,
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        ):
            raise ValidationFailed("compiler risk low..critical olmali")
        for value, label in (
            (self.source_revision, "source revision"),
            (self.expected_source_revision, "expected source revision"),
            (self.logical_key, "logical key"),
            (self.content_ref, "content ref"),
        ):
            if (
                not value.strip()
                or "\\" in value
                or value.startswith(("/", "~"))
                or PureWindowsPath(value).is_absolute()
                or ".." in PurePosixPath(value).parts
            ):
                raise ValidationFailed(f"compiler {label} portable ve dolu olmali")
        if not self.content.strip() or len(self.content) > MAX_FRAGMENT_CHARS:
            raise ValidationFailed("compiler content bos veya unbounded olamaz")
        parse_digest(self.expected_content_digest)
        if self.expected_content_digest != self.normalized_content_digest:
            raise ValidationFailed("compiler content source digest ile uyusmuyor")
        if not self.evidence_refs:
            raise ValidationFailed("compiler fragment evidence ister")
        ordered_evidence = tuple(
            sorted(self.evidence_refs, key=lambda item: (item.ref, item.digest_value))
        )
        if self.evidence_refs != ordered_evidence or len(set(self.evidence_refs)) != len(
            self.evidence_refs
        ):
            raise ValidationFailed("compiler fragment evidence tekil ve sirali olmali")

    @property
    def normalized_content(self) -> str:
        normalized = unicodedata.normalize(
            "NFC", self.content.replace("\r\n", "\n").replace("\r", "\n")
        )
        return "\n".join(line.rstrip() for line in normalized.strip().splitlines())

    @property
    def normalized_content_digest(self) -> str:
        return digest(self.normalized_content)


@dataclass(frozen=True, slots=True)
class CompilerPreparation:
    output: MemoryCompilerOutput
    idempotency_key: str
    source_set_digest: str
    candidate_queue_digest: str
    replayed: bool
    provider_calls: int = 0
    direct_promotion: bool = False
    grants_authority: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.idempotency_key,
            self.source_set_digest,
            self.candidate_queue_digest,
        ):
            parse_digest(value)
        if self.provider_calls or self.direct_promotion or self.grants_authority:
            raise PolicyViolation("compiler preparation effect/promotion/authority uretemez")


@dataclass(frozen=True, slots=True)
class CompilerDurabilityReceipt:
    output_digest: str
    source_set_digest: str
    candidate_queue_digest: str
    compiler_receipt_digest: str
    outbox_digest: str
    committed_at: dt.datetime
    durable: bool

    def __post_init__(self) -> None:
        for value in (
            self.output_digest,
            self.source_set_digest,
            self.candidate_queue_digest,
            self.compiler_receipt_digest,
            self.outbox_digest,
        ):
            parse_digest(value)
        if self.committed_at.tzinfo is None or self.committed_at.utcoffset() is None:
            raise ValidationFailed("compiler durability receipt zamani timezone-aware olmali")
        if not self.durable:
            raise PolicyViolation("compiler watermark durable terminal receipt ister")


@dataclass(frozen=True, slots=True)
class CommittedCompilerWatermark:
    value: str
    output_digest: str
    durability_receipt_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.output_digest)
        parse_digest(self.durability_receipt_digest)
        if not self.value.startswith("watermark:"):
            raise ValidationFailed("compiler watermark canonical prefix ister")


def _candidate_id(fragment: CompilerSourceFragment) -> str:
    identity = digest(
        {
            "source_ref": fragment.source.as_dict(),
            "logical_key": fragment.logical_key,
            "content_digest": fragment.normalized_content_digest,
            "candidate_type": fragment.candidate_type.value,
            "truth_class": fragment.proposed_truth_class.value,
            "classification": fragment.classification.value,
            "risk": fragment.risk.value,
            "evidence": [item.as_dict() for item in fragment.evidence_refs],
        }
    )
    return f"candidate:{identity.removeprefix('sha256:')}"


def _rejection(
    fragment: CompilerSourceFragment, reason_code: str, *, quarantined: bool
) -> CompilerRejection:
    return CompilerRejection(
        source_ref=fragment.source.ref,
        reason_code=reason_code,
        evidence_digest=digest(
            {
                "source": fragment.source.as_dict(),
                "content_digest": fragment.normalized_content_digest,
                "reason_code": reason_code,
            }
        ),
        quarantined=quarantined,
    )


def _validate_truth(fragment: CompilerSourceFragment) -> str | None:
    if fragment.proposed_truth_class in (TruthClass.SUPERSEDED, TruthClass.UNKNOWN):
        return "truth-class-not-candidate"
    if fragment.source_kind in _UNTRUSTED_KINDS and fragment.proposed_truth_class in _FACT_CLASSES:
        return "untrusted-fact-elevation"
    source_truth = fragment.source.truth_class
    if source_truth is TruthClass.UNKNOWN:
        if fragment.proposed_truth_class not in (
            TruthClass.MODEL_INFERENCE,
            TruthClass.TEMPORARY_ASSUMPTION,
        ):
            return "source-truth-elevation"
    elif fragment.proposed_truth_class is not source_truth:
        return "source-truth-drift"
    return None


@dataclass(frozen=True, slots=True)
class MemoryCandidateCompiler:
    budget: CompilerBudget = CompilerBudget()

    def prepare(
        self,
        fragments: tuple[CompilerSourceFragment, ...],
        *,
        output_id: UUID,
        realm_id: UUID,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        parser_digest: str,
        policy_digest: str,
        profile_digest: str,
        known_references: frozenset[tuple[str, str]],
        created_at: dt.datetime,
        prior_output: MemoryCompilerOutput | None = None,
    ) -> CompilerPreparation:
        for value in (parser_digest, policy_digest, profile_digest):
            parse_digest(value)
        if not fragments:
            raise ValidationFailed("compiler source seti bos olamaz")
        if len(fragments) > self.budget.max_sources:
            raise PolicyViolation("compiler source budget asildi")
        if sum(len(item.content) for item in fragments) > self.budget.max_total_chars:
            raise PolicyViolation("compiler character budget asildi")
        ordered = tuple(sorted(fragments, key=lambda item: item.source.ref))
        source_names = tuple(item.source.ref for item in ordered)
        if len(source_names) != len(set(source_names)):
            raise ValidationFailed("compiler source refs tekil olmali")
        source_set = tuple(item.source for item in ordered)
        source_set_digest = digest(
            [
                {
                    "source": item.source.as_dict(),
                    "source_revision": item.source_revision,
                    "content_digest": item.expected_content_digest,
                    "logical_key": item.logical_key,
                    "candidate_type": item.candidate_type.value,
                    "proposed_truth_class": item.proposed_truth_class.value,
                    "classification": item.classification.value,
                    "risk": item.risk.value,
                    "evidence": [reference.as_dict() for reference in item.evidence_refs],
                }
                for item in ordered
            ]
        )
        watermark = f"watermark:{source_set_digest.removeprefix('sha256:')}"
        compiler_digest = digest(
            {
                "compiler_version": COMPILER_VERSION,
                "parser_digest": parser_digest,
                "policy_digest": policy_digest,
                "profile_digest": profile_digest,
            }
        )
        idempotency_key = digest(
            {
                "realm_id": str(realm_id),
                "project_id": str(project_id),
                "source_set_digest": source_set_digest,
                "policy_digest": policy_digest,
                "compiler_digest": compiler_digest,
            }
        )

        accepted: list[CompilerCandidate] = []
        rejected: list[CompilerRejection] = []
        for fragment in ordered:
            if (fragment.source.ref, fragment.source.digest_value) not in known_references:
                rejected.append(_rejection(fragment, "source-ref-unresolved", quarantined=True))
                continue
            if any(
                (reference.ref, reference.digest_value) not in known_references
                for reference in fragment.evidence_refs
            ):
                rejected.append(_rejection(fragment, "evidence-ref-unresolved", quarantined=True))
                continue
            if fragment.source_revision != fragment.expected_source_revision:
                rejected.append(_rejection(fragment, "source-revision-stale", quarantined=False))
                continue
            if _SENSITIVE.search(fragment.normalized_content):
                rejected.append(_rejection(fragment, "sensitive-content", quarantined=True))
                continue
            if fragment.source_kind in _UNTRUSTED_KINDS and _DIRECTIVE_SHAPE.search(
                fragment.normalized_content
            ):
                rejected.append(_rejection(fragment, "untrusted-directive", quarantined=True))
                continue
            if (
                fragment.source_kind is CompilerSourceKind.IMPORTED_TRANSCRIPT
                and fragment.classification
                not in (DataClassification.RESTRICTED, DataClassification.LOCAL_ONLY)
            ):
                rejected.append(_rejection(fragment, "untrusted-classification", quarantined=True))
                continue
            truth_failure = _validate_truth(fragment)
            if truth_failure is not None:
                rejected.append(_rejection(fragment, truth_failure, quarantined=True))
                continue
            accepted.append(
                CompilerCandidate(
                    candidate_id=_candidate_id(fragment),
                    logical_key=fragment.logical_key,
                    content_ref=fragment.content_ref,
                    content_digest=fragment.normalized_content_digest,
                    truth_class=fragment.proposed_truth_class,
                    classification=fragment.classification,
                    candidate_type=fragment.candidate_type,
                    risk=fragment.risk,
                    source_refs=(fragment.source,),
                    evidence_refs=fragment.evidence_refs,
                )
            )
        if len(accepted) > self.budget.max_candidates:
            raise PolicyViolation("compiler candidate budget asildi")

        duplicate_groups = self._groups(accepted, conflict=False)
        conflict_groups = self._groups(accepted, conflict=True)
        candidate_queue_digest = digest([item.as_dict() for item in accepted])
        output = MemoryCompilerOutput(
            output_id=output_id,
            realm_id=realm_id,
            project_id=project_id,
            work_item_id=work_item_id,
            run_id=run_id,
            source_set=source_set,
            source_watermark=watermark,
            parser_digest=parser_digest,
            compiler_digest=compiler_digest,
            policy_digest=policy_digest,
            profile_digest=profile_digest,
            candidates=tuple(accepted),
            rejected=tuple(rejected),
            duplicate_groups=duplicate_groups,
            conflict_groups=conflict_groups,
            gateway_request_ref=None,
            gateway_request_digest=None,
            gateway_response_ref=None,
            gateway_response_digest=None,
            created_at=created_at,
        )
        replayed = False
        if prior_output is not None:
            prior_identity = (
                prior_output.realm_id,
                prior_output.project_id,
                prior_output.work_item_id,
                prior_output.run_id,
                prior_output.source_set,
                prior_output.source_watermark,
                prior_output.parser_digest,
                prior_output.compiler_digest,
                prior_output.policy_digest,
                prior_output.profile_digest,
                prior_output.candidates,
                prior_output.rejected,
                prior_output.duplicate_groups,
                prior_output.conflict_groups,
            )
            current_identity = (
                output.realm_id,
                output.project_id,
                output.work_item_id,
                output.run_id,
                output.source_set,
                output.source_watermark,
                output.parser_digest,
                output.compiler_digest,
                output.policy_digest,
                output.profile_digest,
                output.candidates,
                output.rejected,
                output.duplicate_groups,
                output.conflict_groups,
            )
            if prior_identity == current_identity:
                output = prior_output
                candidate_queue_digest = digest(
                    [item.as_dict() for item in prior_output.candidates]
                )
                replayed = True
        return CompilerPreparation(
            output=output,
            idempotency_key=idempotency_key,
            source_set_digest=source_set_digest,
            candidate_queue_digest=candidate_queue_digest,
            replayed=replayed,
        )

    @staticmethod
    def _groups(
        candidates: list[CompilerCandidate], *, conflict: bool
    ) -> tuple[CandidateGroup, ...]:
        by_key: dict[str, list[CompilerCandidate]] = {}
        for candidate in candidates:
            by_key.setdefault(candidate.logical_key, []).append(candidate)
        groups: list[CandidateGroup] = []
        for logical_key, members in sorted(by_key.items()):
            grouped_members: tuple[list[CompilerCandidate], ...]
            if conflict:
                if len({item.content_digest for item in members}) < 2:
                    continue
                grouped_members = (members,)
            else:
                by_content: dict[str, list[CompilerCandidate]] = {}
                for item in members:
                    by_content.setdefault(item.content_digest, []).append(item)
                grouped_members = tuple(
                    group for _, group in sorted(by_content.items()) if len(group) >= 2
                )
            for grouped in grouped_members:
                ids = tuple(sorted(item.candidate_id for item in grouped))
                groups.append(
                    CandidateGroup(
                        group_digest=digest(
                            {
                                "logical_key": logical_key,
                                "kind": "conflict" if conflict else "duplicate",
                                "candidate_ids": list(ids),
                            }
                        ),
                        candidate_ids=ids,
                    )
                )
        return tuple(groups)

    @staticmethod
    def finalize_watermark(
        preparation: CompilerPreparation,
        receipt: CompilerDurabilityReceipt,
    ) -> CommittedCompilerWatermark:
        if (
            receipt.output_digest != preparation.output.output_digest
            or receipt.source_set_digest != preparation.source_set_digest
            or receipt.candidate_queue_digest != preparation.candidate_queue_digest
        ):
            raise PolicyViolation("compiler source/output drift; watermark ilerletilemez")
        receipt_digest = digest(
            {
                "output_digest": receipt.output_digest,
                "source_set_digest": receipt.source_set_digest,
                "candidate_queue_digest": receipt.candidate_queue_digest,
                "compiler_receipt_digest": receipt.compiler_receipt_digest,
                "outbox_digest": receipt.outbox_digest,
                "committed_at": receipt.committed_at,
                "durable": True,
            }
        )
        return CommittedCompilerWatermark(
            value=preparation.output.source_watermark,
            output_digest=preparation.output.output_digest,
            durability_receipt_digest=receipt_digest,
        )
