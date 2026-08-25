"""Golden resume corpus ve exact structured regression contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.resume import ResumeDisposition, ResumePlan


class GoldenResumeCategory(StrEnum):
    CRASH = "crash"
    FAILOVER = "failover"
    CLIENT_SWITCH = "client-switch"
    INTEGRITY = "integrity"
    GOVERNANCE = "governance"


@dataclass(frozen=True, slots=True)
class GoldenResumeExpectation:
    outcome: str
    disposition: str | None
    reason_codes: tuple[str, ...]
    action_kinds: tuple[str, ...]
    target_client_id: str | None = None
    preserves_patch_digest: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in {"plan", "rejected"}:
            raise ValidationFailed("golden resume outcome plan veya rejected olmali")
        if (self.outcome == "plan") != (self.disposition is not None):
            raise ValidationFailed("golden resume plan disposition ister")
        if self.disposition is not None and self.disposition not in {
            item.value for item in ResumeDisposition
        }:
            raise ValidationFailed("golden resume disposition gecersiz")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValidationFailed("golden resume reason code'lari tekil ve sirali olmali")
        if any(not value.strip() for value in (*self.reason_codes, *self.action_kinds)):
            raise ValidationFailed("golden resume expectation bos kod tasiyamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "disposition": self.disposition,
            "reason_codes": list(self.reason_codes),
            "action_kinds": list(self.action_kinds),
            "target_client_id": self.target_client_id,
            "preserves_patch_digest": self.preserves_patch_digest,
        }


@dataclass(frozen=True, slots=True)
class GoldenResumeCase:
    case_id: str
    category: GoldenResumeCategory
    description: str
    expectation: GoldenResumeExpectation

    def __post_init__(self) -> None:
        self.expectation.__post_init__()
        if not self.case_id.startswith("resume-") or not self.description.strip():
            raise ValidationFailed("golden resume case kimligi/aciklamasi gecersiz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": str(self.category),
            "description": self.description,
            "expectation": self.expectation.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class GoldenResumeCorpus:
    cases: tuple[GoldenResumeCase, ...]
    version: str = "1"
    schema: str = "zekam-golden-resume-corpus/v1"
    grants_authority: bool = False

    def __post_init__(self) -> None:
        for item in self.cases:
            item.__post_init__()
        ids = tuple(item.case_id for item in self.cases)
        if self.schema != "zekam-golden-resume-corpus/v1" or self.version != "1":
            raise ValidationFailed("golden resume corpus schema/surumu gecersiz")
        if not ids or ids != tuple(sorted(set(ids))):
            raise ValidationFailed("golden resume case'leri tekil ve sirali olmali")
        if self.grants_authority:
            raise ValidationFailed("golden resume corpus authority veremez")

    @property
    def corpus_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "cases": [item.as_dict() for item in self.cases],
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class GoldenResumeActual:
    outcome: str
    disposition: str | None
    reason_codes: tuple[str, ...]
    action_kinds: tuple[str, ...]
    plan_digest: str | None
    plan_body_digest: str | None
    target_client_id: str | None
    patch_digest: str | None
    error_code: str | None = None
    plan: ResumePlan | None = None

    def __post_init__(self) -> None:
        if self.outcome == "plan":
            if not isinstance(self.plan, ResumePlan):
                raise ValidationFailed("golden actual dogrulanmis ResumePlan ister")
            self.plan.__post_init__()
            plan_body = self.plan.body()
            computed = digest(plan_body)
            if computed != self.plan_digest or computed != self.plan_body_digest:
                raise ValidationFailed("golden actual plan body digest drift")
            sandbox = plan_body.get("sandbox")
            expected_patch = sandbox.get("patch_digest") if isinstance(sandbox, dict) else None
            if self.patch_digest != expected_patch:
                raise ValidationFailed("golden actual sandbox patch digest drift")
            if self.patch_digest is not None:
                parse_digest(self.patch_digest)
            if self.error_code is not None:
                raise ValidationFailed("golden actual plan rejection code tasiyamaz")
            expected_disposition = plan_body.get("disposition")
            expected_target = plan_body.get("target_client_id")
            raw_actions = plan_body.get("actions")
            raw_stale = plan_body.get("stale_dimensions")
            raw_reconciliation = plan_body.get("reconciliation_actions")
            raw_blockers = plan_body.get("blockers")
            if not all(
                isinstance(value, list)
                for value in (raw_actions, raw_stale, raw_reconciliation, raw_blockers)
            ):
                raise ValidationFailed("golden actual plan body yapisi gecersiz")
            actions = cast(list[dict[str, Any]], raw_actions)
            stale = cast(list[dict[str, Any]], raw_stale)
            reconciliation = cast(list[dict[str, Any]], raw_reconciliation)
            blockers = cast(list[str], raw_blockers)
            expected_actions = tuple(item["kind"] for item in actions)
            expected_reasons = tuple(
                sorted(
                    {
                        *(str(value) for value in blockers),
                        *(str(item["reason_code"]) for item in stale),
                        *(str(item["reason_code"]) for item in reconciliation),
                    }
                )
            )
            if (
                self.disposition != expected_disposition
                or self.target_client_id != expected_target
                or self.action_kinds != expected_actions
                or self.reason_codes != expected_reasons
            ):
                raise ValidationFailed("golden actual claimed fields plan body ile uyusmuyor")
        elif self.outcome == "rejected":
            if not self.error_code or any(
                value is not None
                for value in (
                    self.disposition,
                    self.plan_digest,
                    self.plan_body_digest,
                    self.target_client_id,
                    self.patch_digest,
                    self.plan,
                )
            ):
                raise ValidationFailed("golden actual rejection yapisi gecersiz")
            if self.reason_codes != (self.error_code,) or self.action_kinds:
                raise ValidationFailed("golden actual rejection kodu uyusmuyor")
        else:
            raise ValidationFailed("golden actual outcome gecersiz")

    @classmethod
    def from_plan(cls, plan: ResumePlan) -> GoldenResumeActual:
        body_digest = digest(plan.body())
        if plan.plan_digest != body_digest:
            raise ValidationFailed("resume plan digest body ile uyusmuyor")
        reasons = tuple(
            sorted(
                {
                    *plan.blockers,
                    *(item.reason_code for item in plan.stale_dimensions),
                    *(item.reason_code for item in plan.reconciliation_actions),
                }
            )
        )
        return cls(
            outcome="plan",
            disposition=str(plan.disposition),
            reason_codes=reasons,
            action_kinds=tuple(item.kind for item in plan.actions),
            plan_digest=plan.plan_digest,
            plan_body_digest=body_digest,
            target_client_id=plan.target_client_id,
            patch_digest=plan.sandbox.patch_digest,
            plan=plan,
        )

    @classmethod
    def rejected(cls, error_code: str) -> GoldenResumeActual:
        if not error_code.strip():
            raise ValidationFailed("golden resume rejection error code ister")
        return cls("rejected", None, (error_code,), (), None, None, None, None, error_code, None)


@dataclass(frozen=True, slots=True)
class GoldenResumeFinding:
    case_id: str
    field: str
    expected: Any
    actual: Any


@dataclass(frozen=True, slots=True)
class GoldenResumeResult:
    corpus_digest: str
    case_id: str
    passed: bool
    findings: tuple[GoldenResumeFinding, ...]
    actual_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.corpus_digest)
        parse_digest(self.actual_digest)
        if self.passed != (not self.findings):
            raise ValidationFailed("golden resume result pass/finding uyusmuyor")


def evaluate_golden_resume(
    corpus: GoldenResumeCorpus,
    case: GoldenResumeCase,
    actual: GoldenResumeActual,
) -> GoldenResumeResult:
    corpus.__post_init__()
    case.__post_init__()
    actual.__post_init__()
    if case not in corpus.cases:
        raise ValidationFailed("golden resume case corpus disinda")
    expected = case.expectation
    checks: tuple[tuple[str, Any, Any], ...] = (
        ("outcome", expected.outcome, actual.outcome),
        ("disposition", expected.disposition, actual.disposition),
        ("reason_codes", expected.reason_codes, actual.reason_codes),
        ("action_kinds", expected.action_kinds, actual.action_kinds),
    )
    findings = [
        GoldenResumeFinding(case.case_id, field, wanted, observed)
        for field, wanted, observed in checks
        if wanted != observed
    ]
    if (
        expected.target_client_id is not None
        and expected.target_client_id != actual.target_client_id
    ):
        findings.append(
            GoldenResumeFinding(
                case.case_id, "target_client_id", expected.target_client_id, actual.target_client_id
            )
        )
    if actual.outcome == "plan" and actual.plan_digest != actual.plan_body_digest:
        findings.append(
            GoldenResumeFinding(
                case.case_id, "plan_digest", actual.plan_body_digest, actual.plan_digest
            )
        )
    if expected.preserves_patch_digest and actual.patch_digest is None:
        findings.append(GoldenResumeFinding(case.case_id, "patch_digest", "present", None))
    actual_body = {
        "outcome": actual.outcome,
        "disposition": actual.disposition,
        "reason_codes": list(actual.reason_codes),
        "action_kinds": list(actual.action_kinds),
        "plan_digest": actual.plan_digest,
        "claimed_plan_body_digest": actual.plan_body_digest,
        "target_client_id": actual.target_client_id,
        "patch_digest": actual.patch_digest,
        "error_code": actual.error_code,
        "computed_plan_body_digest": None if actual.plan is None else digest(actual.plan.body()),
    }
    return GoldenResumeResult(
        corpus.corpus_digest,
        case.case_id,
        not findings,
        tuple(findings),
        digest(actual_body),
    )
