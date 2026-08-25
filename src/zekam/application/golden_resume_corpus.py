"""ZK-P2-005 surumlu golden resume benchmark corpus'u."""

from __future__ import annotations

from zekam.domain.golden_resume import (
    GoldenResumeCase,
    GoldenResumeCategory,
    GoldenResumeCorpus,
    GoldenResumeExpectation,
)

GOLDEN_RESUME_V1_DIGEST = "sha256:f73549fde4644b00430320fe2a15be410c7320258e9cb2b55fc0c9870bbc2f37"


def _case(
    case_id: str,
    category: GoldenResumeCategory,
    description: str,
    disposition: str | None,
    reasons: tuple[str, ...],
    actions: tuple[str, ...],
    *,
    outcome: str = "plan",
    target: str | None = None,
    patch: bool = False,
) -> GoldenResumeCase:
    return GoldenResumeCase(
        case_id,
        category,
        description,
        GoldenResumeExpectation(
            outcome, disposition, tuple(sorted(reasons)), actions, target, patch
        ),
    )


def default_golden_resume_corpus() -> GoldenResumeCorpus:
    """Rapordaki 12 senaryoyu exact structured expectation olarak dondurur."""

    cases = (
        _case(
            "resume-01-clean-read-only",
            GoldenResumeCategory.FAILOVER,
            "Revision current, effect yok",
            "safe-continue",
            (),
            ("reacquire", "dispatch-next-step"),
        ),
        _case(
            "resume-02-source-drift",
            GoldenResumeCategory.FAILOVER,
            "Source revision drift replan ister",
            "safe-replan",
            ("resume.replan-required", "resume.source-revision-drift"),
            ("replan",),
        ),
        _case(
            "resume-03-policy-drift",
            GoldenResumeCategory.GOVERNANCE,
            "Policy drift authorization degerlendirmesi ister",
            "safe-recompile",
            ("resume.policy-digest-drift", "resume.recompile-required"),
            ("recompile-context", "reacquire", "dispatch-next-step"),
        ),
        _case(
            "resume-04-route-drift",
            GoldenResumeCategory.FAILOVER,
            "Route drift fresh evidence ile recompile ister",
            "safe-recompile",
            ("resume.model-route-decision-digest-drift", "resume.recompile-required"),
            ("recompile-context", "reacquire", "dispatch-next-step"),
        ),
        _case(
            "resume-05-context-drift",
            GoldenResumeCategory.FAILOVER,
            "Context manifest drift yeniden derlenir",
            "safe-recompile",
            ("resume.context-manifest-digest-drift", "resume.recompile-required"),
            ("recompile-context", "reacquire", "dispatch-next-step"),
        ),
        _case(
            "resume-06-receiptless-effect",
            GoldenResumeCategory.CRASH,
            "Receiptless effect reconcile edilir",
            "recovery-required",
            ("resume.receiptless-or-ambiguous-effect", "resume.unresolved-effect"),
            ("reconcile-effect",),
        ),
        _case(
            "resume-07-completed-without-checkpoint",
            GoldenResumeCategory.CRASH,
            "Completed partition structural checkpoint olmadan manual review",
            "manual-review",
            ("resume.completed-partition-drift",),
            (),
        ),
        _case(
            "resume-08-legacy-v1-handoff",
            GoldenResumeCategory.FAILOVER,
            "Legacy handoff limited manual disposition",
            "manual-review",
            ("resume.legacy-checkpoint-limited",),
            (),
        ),
        _case(
            "resume-09-cross-client",
            GoldenResumeCategory.CLIENT_SWITCH,
            "OpenCode kaydindan Codex'e transcriptsiz devam",
            "safe-continue",
            (),
            ("reacquire", "dispatch-next-step"),
            target="codex",
        ),
        _case(
            "resume-10-cross-realm-forgery",
            GoldenResumeCategory.INTEGRITY,
            "Cross realm bundle fail closed",
            None,
            ("resume.cross-realm-binding",),
            (),
            outcome="rejected",
        ),
        _case(
            "resume-11-dirty-sandbox",
            GoldenResumeCategory.CRASH,
            "Dirty sandbox exact patch digest ile devam eder",
            "safe-continue",
            (),
            ("reacquire", "dispatch-next-step"),
            patch=True,
        ),
        _case(
            "resume-12-budget-exhausted",
            GoldenResumeCategory.GOVERNANCE,
            "Budget bittiginde otomatik dispatch yok",
            "manual-review",
            ("resume.budget-exhausted",),
            (),
        ),
    )
    corpus = GoldenResumeCorpus(tuple(sorted(cases, key=lambda item: item.case_id)))
    if corpus.corpus_digest != GOLDEN_RESUME_V1_DIGEST:
        raise RuntimeError(
            "golden resume v1 matrix drift; version bump veya reviewed digest gerekir"
        )
    return corpus
