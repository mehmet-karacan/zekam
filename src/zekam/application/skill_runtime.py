"""Trusted fixed-profile skill execution and independent journal readback."""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Protocol

from zekam.application.local_runtime_service import LocalEffectRequest, LocalEffectResult
from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_runtime_effects import LocalJournalEffectExecutor

SKILL_EXECUTE_OPERATION = "skill.execute.local-journal/v1"
SKILL_VERIFY_OPERATION = "skill.verify.local-journal/v1"
SKILL_EXECUTE_HANDLER = "handler:skill.execute.local-journal/v1"
SKILL_VERIFY_HANDLER = "handler:skill.verify.local-journal/v1"
JOURNAL_SKILL_PROFILE = {
    "skill_id": "bounded-journal-entry",
    "required_tools": ("local-journal",),
    "steps": ("Append the canonical single-line record.",),
    "checks": ("Read back the exact appended record.",),
    "permissions_ceiling": "workspace-write-no-network",
}


class SkillRuntimeSigner:
    """Opaque local signing capability injected only into trusted handlers."""

    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise ValidationFailed("Skill runtime signer exact 32-byte key ister")
        self.__key = key

    def attest(self, body: dict[str, object]) -> str:
        value = hmac.digest(self.__key, canonical_json(body).encode(), "sha256").hex()
        return f"hmac-sha256:{value}"



class SkillRuntimeVerifier:
    """Verification-only capability; it never exposes or returns a valid seal."""

    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise ValidationFailed("Skill runtime verifier exact 32-byte key ister")
        self.__key = key

    def matches_receipt_digest(
        self, unsigned_body: dict[str, object], evidence_digest: str
    ) -> bool:
        seal = "hmac-sha256:" + hmac.digest(
            self.__key, canonical_json(unsigned_body).encode(), "sha256"
        ).hex()
        return hmac.compare_digest(
            digest(unsigned_body | {"runtime_attestation": seal}), evidence_digest
        )


class SkillRuntimeLearningPort(Protocol):
    def select_active_skills(
        self, trigger: str, *, maximum: int = 8
    ) -> tuple[dict[str, object], ...]: ...

    def skill_usage_verification_target(self, usage_digest: str) -> dict[str, str]: ...


class TrustedJournalSkillExecutor:
    def __init__(
        self,
        learning: SkillRuntimeLearningPort,
        journal_root: Path,
        authority: SkillRuntimeSigner,
    ) -> None:
        if type(journal_root) is not type(Path()):
            raise ValidationFailed("Skill journal root Path olmali")
        self._learning = learning
        self._journal = LocalJournalEffectExecutor(journal_root)
        self._authority = authority

    def __call__(self, request: LocalEffectRequest) -> LocalEffectResult:
        payload = request.payload
        if request.operation != SKILL_EXECUTE_OPERATION or frozenset(payload) != {
            "activation_digest",
            "trigger",
            "input_digest",
            "line",
        }:
            raise ValidationFailed("Trusted skill exact execution payload ister")
        activation = payload.get("activation_digest")
        trigger = payload.get("trigger")
        input_digest = payload.get("input_digest")
        line = payload.get("line")
        if not all(isinstance(item, str) for item in (activation, trigger, input_digest, line)):
            raise ValidationFailed("Trusted skill execution fields text olmali")
        assert isinstance(activation, str)
        assert isinstance(trigger, str)
        assert isinstance(input_digest, str)
        assert isinstance(line, str)
        parse_digest(activation)
        parse_digest(input_digest)
        if not line or "\n" in line or len(line.encode("utf-8")) > 4096:
            raise ValidationFailed("Trusted skill bounded single-line input ister")
        if scan_text(line, relative_path="skill-runtime-input.txt"):
            raise PolicyViolation("Trusted skill input secret taramasini gecemedi")
        selected = self._learning.select_active_skills(trigger, maximum=8)
        skill = next(
            (item for item in selected if item.get("activation_digest") == activation), None
        )
        if skill is None or any(
            skill.get(key) != value for key, value in JOURNAL_SKILL_PROFILE.items()
        ):
            raise PolicyViolation("Trusted skill activation/profile dispatch ile eslesmiyor")
        journal_ref = f"skills/executions/{activation[7:]}.jsonl"
        inner = self._journal(
            LocalEffectRequest(
                "local.append-journal/v1",
                request.idempotency_key,
                {"relative_path": journal_ref, "line": line},
            )
        )
        body = execution_receipt_body(
            activation_digest=activation,
            trigger=trigger,
            input_digest=input_digest,
            journal_ref=journal_ref,
            line=line,
            idempotency_key=request.idempotency_key,
            journal_evidence_digest=inner.evidence_digest,
            authority=self._authority,
        )
        return LocalEffectResult("completed", digest(body))


class TrustedJournalSkillVerifier:
    def __init__(
        self,
        learning: SkillRuntimeLearningPort,
        journal_root: Path,
        authority: SkillRuntimeSigner,
    ) -> None:
        if type(journal_root) is not type(Path()):
            raise ValidationFailed("Skill verifier journal root Path olmali")
        self._learning = learning
        self._journal = LocalJournalEffectExecutor(journal_root)
        self._authority = authority

    def __call__(self, request: LocalEffectRequest) -> LocalEffectResult:
        if request.operation != SKILL_VERIFY_OPERATION or frozenset(request.payload) != {
            "usage_digest"
        }:
            raise ValidationFailed("Trusted skill verifier exact usage payload ister")
        usage_digest = request.payload.get("usage_digest")
        if not isinstance(usage_digest, str):
            raise ValidationFailed("Trusted skill verifier usage digest ister")
        parse_digest(usage_digest)
        target = self._learning.skill_usage_verification_target(usage_digest)
        verified = self._journal.verify_record(
            relative_path=target["journal_ref"],
            idempotency_key=target["execution_idempotency_key"],
            line=target["line"],
        )
        audit_ref = f"skills/verifications/{usage_digest[7:]}.jsonl"
        audit_line = canonical_json(
            {
                "schema": "zekam-trusted-skill-verification-audit/v1",
                "usage_digest": usage_digest,
                "activation_digest": target["activation_digest"],
                "execution_job_id": target["execution_job_id"],
                "execution_evidence_digest": target["execution_evidence_digest"],
                "journal_ref": target["journal_ref"],
                "journal_record_present": verified,
            }
        )
        audit = self._journal(
            LocalEffectRequest(
                "local.append-journal/v1",
                request.idempotency_key,
                {"relative_path": audit_ref, "line": audit_line},
            )
        )
        body = verification_receipt_body(
            usage_digest=usage_digest,
            activation_digest=target["activation_digest"],
            execution_job_id=target["execution_job_id"],
            execution_evidence_digest=target["execution_evidence_digest"],
            journal_ref=target["journal_ref"],
            journal_record_present=verified,
            verification_audit_ref=audit_ref,
            verification_idempotency_key=request.idempotency_key,
            verification_audit_line=audit_line,
            verification_audit_evidence_digest=audit.evidence_digest,
            authority=self._authority,
        )
        return LocalEffectResult("completed" if verified else "failed", digest(body))


def execution_receipt_body(
    *,
    activation_digest: str,
    trigger: str,
    input_digest: str,
    journal_ref: str,
    line: str,
    idempotency_key: str,
    journal_evidence_digest: str,
    authority: SkillRuntimeSigner,
) -> dict[str, object]:
    body = execution_receipt_unsigned_body(
        activation_digest=activation_digest,
        trigger=trigger,
        input_digest=input_digest,
        journal_ref=journal_ref,
        line=line,
        idempotency_key=idempotency_key,
        journal_evidence_digest=journal_evidence_digest,
    )
    return body | {"runtime_attestation": authority.attest(body)}


def execution_receipt_unsigned_body(
    *,
    activation_digest: str,
    trigger: str,
    input_digest: str,
    journal_ref: str,
    line: str,
    idempotency_key: str,
    journal_evidence_digest: str,
) -> dict[str, object]:
    return {
        "schema": "zekam-trusted-skill-execution-receipt/v1",
        "handler": SKILL_EXECUTE_HANDLER,
        "activation_digest": activation_digest,
        "trigger": trigger,
        "input_digest": input_digest,
        "journal_ref": journal_ref,
        "line_digest": digest(line),
        "execution_idempotency_key": idempotency_key,
        "journal_evidence_digest": journal_evidence_digest,
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }


def verification_receipt_body(
    *,
    usage_digest: str,
    activation_digest: str,
    execution_job_id: str,
    execution_evidence_digest: str,
    journal_ref: str,
    journal_record_present: bool,
    verification_audit_ref: str,
    verification_idempotency_key: str,
    verification_audit_line: str,
    verification_audit_evidence_digest: str,
    authority: SkillRuntimeSigner,
) -> dict[str, object]:
    body = verification_receipt_unsigned_body(
        usage_digest=usage_digest,
        activation_digest=activation_digest,
        execution_job_id=execution_job_id,
        execution_evidence_digest=execution_evidence_digest,
        journal_ref=journal_ref,
        journal_record_present=journal_record_present,
        verification_audit_ref=verification_audit_ref,
        verification_idempotency_key=verification_idempotency_key,
        verification_audit_line=verification_audit_line,
        verification_audit_evidence_digest=verification_audit_evidence_digest,
    )
    return body | {"runtime_attestation": authority.attest(body)}


def verification_receipt_unsigned_body(
    *,
    usage_digest: str,
    activation_digest: str,
    execution_job_id: str,
    execution_evidence_digest: str,
    journal_ref: str,
    journal_record_present: bool,
    verification_audit_ref: str,
    verification_idempotency_key: str,
    verification_audit_line: str,
    verification_audit_evidence_digest: str,
) -> dict[str, object]:
    return {
        "schema": "zekam-trusted-skill-verification-receipt/v1",
        "handler": SKILL_VERIFY_HANDLER,
        "usage_digest": usage_digest,
        "activation_digest": activation_digest,
        "execution_job_id": execution_job_id,
        "execution_evidence_digest": execution_evidence_digest,
        "journal_ref": journal_ref,
        "journal_record_present": journal_record_present,
        "verification_audit_ref": verification_audit_ref,
        "verification_audit_line_digest": digest(verification_audit_line),
        "verification_idempotency_key": verification_idempotency_key,
        "verification_audit_evidence_digest": verification_audit_evidence_digest,
        "outcome": "verified-success" if journal_record_present else "verified-failure",
        "provider_calls": 0,
        "network_calls": 0,
        "grants_authority": False,
    }
