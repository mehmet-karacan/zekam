"""Onayli testability refactor uygulama servisi (W07, AKTIF_GOREV 9.4, D17, U11).

Akis (claim-before-effect): proposal dogrulanir -> onay exact proposal/kaynak/plan/hedef
digest'ine karsi dogrulanir -> degisiklik kumesi proposal'daki exact dosyalarla birebir
eslesmeli -> dogrulama istegi ledger'a yazilir, aday manifesti + preimage yedekleri nesne
deposuna yazilir, attempt CLAIM edilir -> mevcut atomik ``TestPatchWorkspace.apply`` ile
(preimage/ownership kontrollu) yazilir -> mevcut olcum uygulamasiyla testler yeniden
calistirilir -> BAGIMSIZ verifier invocation'i API/config/exception/yan etki degisimini inceler
-> kabul ya da sahiplik kontrollu rollback.

Mevcut ledger semasi degismez: refactor dogrulamasi ayri bir ``UnitTestRequest`` kimligiyle
(``defaults_applied`` isaretli) yazilir; origin istegin final terminal'i ve kaniti DEGISMEZ.
Basarili refactor loop success degildir: terminal yazilmaz, ``VERIFIED`` sonucu yeni baseline +
ayni hedef politikasiyla yeni bir istek (``followup_request``) tasir. ``UnitTestLoopService``
degismeden o istekle yeniden calistirilir (``resume_test_loop``).

"Testler gecti" davranisin ayni kaldiginin ispati degildir; sonuc bunu iddia eden alan tasimaz.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from zekam.application.object_store import ObjectStore
from zekam.application.unit_test_agents import (
    VERIFYING,
    AgentDispatchFailure,
    AgentSpecialty,
    AgentTask,
    UnitTestAgentGateway,
)
from zekam.application.unit_test_ledger import UnitTestLedger
from zekam.application.unit_test_loop import UnitTestLoopService
from zekam.application.unit_test_loop_state import (
    ControlSignal,
    EnvironmentObservation,
    EnvironmentObserver,
    InMemoryLoopControl,
    LoopControl,
    LoopOutcome,
    UnitTestMeasurer,
)
from zekam.application.unit_test_measurement import (
    CurrentState,
    FreshnessVerdict,
    MeasurementRecord,
    MeasurementStatus,
    check_freshness,
)
from zekam.application.unit_test_patch import (
    AppliedPatch,
    CandidateFile,
    CandidateManifest,
    FileChange,
    PreimageMismatch,
    RevertReceipt,
    TestPatch,
    TestPatchWorkspace,
    fold_path,
)
from zekam.application.unit_test_plan import BehaviorPlan
from zekam.application.unit_test_plateau import LoopLimits, UnitTestApprovals
from zekam.application.unit_test_refactor import (
    BUDGET_LIKE_ORIGINS,
    PROPOSAL_ORIGINS,
    ApprovalMismatch,
    OutOfScopeKind,
    ProposalInvalid,
    RefactorApproval,
    RefactorError,
    RefactorProposal,
    ReviewArea,
    ScopeViolation,
    SourceBaseline,
    check_production_target,
    derive_followup_request,
    derive_verification_request,
    lineage_digest,
)
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.unit_test_engineering import (
    AttemptState,
    UnitTestAttempt,
    UnitTestRequest,
    UnitTestStopReason,
    UnitTestTerminal,
)

EVIDENCE_CONTRACT: Final = "zekam-unit-test-refactor-evidence/v1"
VERIFICATION_NOTE: Final = (
    "Gecen testler ve bagimsiz inceleme kanit degeri tasir; davranis korunumunun ispati degildir."
)
_ORIGIN_EVIDENCE_CONTRACT: Final = "zekam-unit-test-terminal-evidence/v1"


def _hex16(value: str) -> str:
    return value.split(":", 1)[-1][:16]


# -- bagimsiz inceleme govdesi -----------------------------------------------------------


class AreaStatus(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED_AS_DECLARED = "changed-as-declared"
    CHANGED_UNDECLARED = "changed-undeclared"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class RefactorReview:
    verdict: str
    areas: tuple[tuple[ReviewArea, AreaStatus], ...]
    scope_findings: tuple[OutOfScopeKind, ...]
    new_material_risks: tuple[str, ...]

    def problems(self, proposal: RefactorProposal) -> tuple[str, ...]:
        found: list[str] = []
        if self.verdict != "accepted":
            found.append("review-rejected")
        reported = dict(self.areas)
        for area in ReviewArea:
            status = reported.get(area)
            if status is None:
                found.append(f"review-area-missing:{area.value}")
            elif status is AreaStatus.INCONCLUSIVE:
                found.append(f"review-area-inconclusive:{area.value}")
            elif status is AreaStatus.CHANGED_UNDECLARED or (
                status is AreaStatus.CHANGED_AS_DECLARED and not proposal.declares(area)
            ):
                found.append(f"review-area-undeclared-change:{area.value}")
        found += [f"scope-decision-required:{kind.value}" for kind in self.scope_findings]
        if self.new_material_risks:
            found.append("new-material-risk")
        return tuple(found)

    def to_payload(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "areas": [[a.value, s.value] for a, s in self.areas],
            "scope_findings": [k.value for k in self.scope_findings],
            "new_material_risks": list(self.new_material_risks),
        }


def parse_refactor_review(body: Mapping[str, Any]) -> RefactorReview:
    allowed = {"verdict", "area_reviews", "scope_findings", "new_material_risks"}
    if set(body) - allowed or "verdict" not in body or "area_reviews" not in body:
        raise ValidationFailed("Refactor review body bilinmeyen/eksik alan")
    verdict = str(body["verdict"])
    if verdict not in {"accepted", "rejected"}:
        raise ValidationFailed("Refactor review verdict gecersiz")
    reviews = body["area_reviews"]
    if not isinstance(reviews, list):
        raise ValidationFailed("area_reviews liste olmali")
    areas: list[tuple[ReviewArea, AreaStatus]] = []
    for item in reviews:
        if not isinstance(item, Mapping) or set(item) - {"area", "status", "note"}:
            raise ValidationFailed("area_reviews ogesi area/status/note tasimali")
        try:
            areas.append((ReviewArea(str(item["area"])), AreaStatus(str(item["status"]))))
        except (ValueError, KeyError) as exc:
            raise ValidationFailed("area_reviews alani gecersiz") from exc
    if len({a for a, _ in areas}) != len(areas):
        raise ValidationFailed("area_reviews alan tekrari")
    raw_scope = body.get("scope_findings", [])
    raw_risks = body.get("new_material_risks", [])
    if not isinstance(raw_scope, list) or not isinstance(raw_risks, list):
        raise ValidationFailed("scope_findings/new_material_risks liste olmali")
    try:
        scope = tuple(OutOfScopeKind(str(k)) for k in raw_scope)
    except ValueError as exc:
        raise ValidationFailed("Bilinmeyen scope bulgusu") from exc
    return RefactorReview(verdict, tuple(areas), scope, tuple(str(r)[:200] for r in raw_risks))


# -- sonuc ------------------------------------------------------------------------------


class RefactorStatus(StrEnum):
    #: Uygulandi, testler + bagimsiz inceleme gecti, yeni baseline olustu. Ispat degildir.
    VERIFIED = "verified"
    #: Reddedildi; kendi degisiklik temiz geri alindi.
    ROLLED_BACK = "rolled-back"
    #: Geri alma temiz degil (kullanici/baska surec dosyayi degistirdi); recovery gerekir.
    ROLLBACK_INCOMPLETE = "rollback-incomplete"


@dataclass(frozen=True, slots=True)
class RefactorResult:
    status: RefactorStatus
    stop_reason: UnitTestStopReason | None
    proposal_digest: str
    approval_digest: str
    verification_request_digest: str
    evidence_digest: str
    detail: tuple[str, ...] = ()
    followup_request: UnitTestRequest | None = None
    lineage_digest: str | None = None
    old_baseline: SourceBaseline | None = None
    new_baseline: SourceBaseline | None = None
    review: RefactorReview | None = None
    revert: RevertReceipt | None = None
    note: str = VERIFICATION_NOTE
    changed_paths: tuple[str, ...] = field(default_factory=tuple)

    def machine_status(self) -> dict[str, str | bool]:
        return {
            "status": self.status.value,
            "reason": self.stop_reason.value if self.stop_reason else "none",
            "final": self.status is not RefactorStatus.ROLLBACK_INCOMPLETE,
        }


@dataclass(frozen=True, slots=True)
class _OriginEvidence:
    state: Mapping[str, Any]
    findings: Mapping[str, Mapping[str, Any]]
    old_baseline: SourceBaseline | None


# -- servis -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefactorService:
    """Port'lara bagli; SQLite/Maven/model somut tiplerini bilmez."""

    ledger: UnitTestLedger
    objects: ObjectStore
    gateway: UnitTestAgentGateway
    measurer: UnitTestMeasurer
    workspace: TestPatchWorkspace
    environment: EnvironmentObserver
    control: LoopControl | None = None
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)

    # -- proposal ----------------------------------------------------------------------

    def validate_proposal(
        self, request: UnitTestRequest, proposal: RefactorProposal, *, check_disk: bool = True
    ) -> _OriginEvidence:
        """Proposal'i origin terminal kanitina ve istek kapsamina baglar.

        Butce/plateau kaynakli oneri, kaynak kanitta somut testability bulgusu yoksa gecersizdir;
        butce bitmesi tek basina gerekce olamaz.
        """

        origin = proposal.origin
        if origin.request_digest != request.request_digest:
            raise ProposalInvalid("origin-request-mismatch")
        if proposal.source_revision != request.source_revision:
            raise ProposalInvalid("source-revision-mismatch")
        if origin.stop_reason not in PROPOSAL_ORIGINS:
            raise ProposalInvalid("origin-not-eligible", origin.stop_reason.value)
        terminals = self.ledger.list_terminals(request.request_digest)
        if not terminals:
            raise ProposalInvalid("origin-terminal-missing")
        last = terminals[-1]
        if (
            last.stop_reason is not origin.stop_reason
            or last.evidence_digest != origin.terminal_evidence_digest
        ):
            raise ProposalInvalid("origin-terminal-mismatch")
        evidence = self._load_origin_evidence(request, origin.terminal_evidence_digest)
        state = evidence["state"]
        raw_findings = state.get("testability_findings") or []
        findings = {digest(dict(f)): dict(f) for f in raw_findings if isinstance(f, Mapping)}
        if not findings:
            raise ProposalInvalid(
                "budget-alone-is-not-a-reason"
                if origin.stop_reason in BUDGET_LIKE_ORIGINS
                else "testability-evidence-missing"
            )
        self._check_problems(request, proposal, findings)
        plan_payload = state.get("plan")
        if plan_payload is None:
            raise ProposalInvalid("origin-plan-missing")
        if BehaviorPlan.from_payload(plan_payload, request).plan_digest != origin.plan_digest:
            raise ProposalInvalid("plan-digest-mismatch")
        self._check_scope(request, proposal)
        if check_disk:
            current = self.workspace.digest_paths(proposal.target_paths)
            for target in proposal.targets:
                if current.get(target.path) != target.preimage_digest:
                    raise ProposalInvalid("stale-proposal", target.path)
        return _OriginEvidence(state, findings, SourceBaseline.from_loop_state(request, state))

    def _load_origin_evidence(
        self, request: UnitTestRequest, evidence_digest: str
    ) -> dict[str, Any]:
        try:
            loaded = json.loads(self.objects.get(evidence_digest).decode("utf-8"))
        except Exception as exc:
            raise ProposalInvalid("origin-evidence-unreadable") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("contract") != _ORIGIN_EVIDENCE_CONTRACT
            or loaded.get("request_digest") != request.request_digest
            or not isinstance(loaded.get("state"), dict)
        ):
            raise ProposalInvalid("origin-evidence-invalid")
        return loaded

    @staticmethod
    def _check_problems(
        request: UnitTestRequest,
        proposal: RefactorProposal,
        findings: Mapping[str, Mapping[str, Any]],
    ) -> None:
        in_scope = {fold_path(p) for p in (*proposal.target_paths, *request.source_files)}
        for problem in proposal.problems:
            finding = findings.get(problem.finding_digest)
            if finding is None:
                raise ProposalInvalid("finding-not-in-origin-evidence")
            if finding.get("observable") != problem.observable:
                raise ProposalInvalid("finding-observable-mismatch")
            if not finding.get("tried_test_only"):
                raise ProposalInvalid("no-test-only-attempt-evidence")
            if not any(fold_path(ref.split(":", 1)[0]) in in_scope for ref in problem.source_refs):
                raise ProposalInvalid("source-ref-outside-scope", problem.observable)

    @staticmethod
    def _check_scope(request: UnitTestRequest, proposal: RefactorProposal) -> None:
        forbidden = {fold_path(p) for p in request.forbidden_paths}
        for target in proposal.targets:
            check_production_target(target.path)
            folded = fold_path(target.path)
            if folded in forbidden:
                raise ScopeViolation("forbidden-path", target.path)
            if any(
                folded == fold_path(a) or folded.startswith(fold_path(a).rstrip("/") + "/")
                for a in request.allowed_test_paths
            ):
                raise ScopeViolation("test-write-path", target.path)
        if not set(request.regression_scope) <= set(proposal.verification.regression_scope):
            raise ProposalInvalid("regression-scope-narrower-than-request")

    def register_proposal(self, request: UnitTestRequest, proposal: RefactorProposal) -> str:
        """Dogrulanmis proposal'i icerik adresli saklar; yetki DOGURMAZ."""

        self.validate_proposal(request, proposal)
        stored = self.objects.put(
            canonical_bytes(proposal.to_payload()), media_type="application/json"
        )
        if stored.digest != proposal.proposal_digest:
            raise ProposalInvalid("stored-digest-mismatch")
        return proposal.proposal_digest

    # -- uygulama ----------------------------------------------------------------------

    @staticmethod
    def _check_changes(proposal: RefactorProposal, changes: Sequence[FileChange]) -> TestPatch:
        """Degisiklik kumesi proposal'daki exact dosyalarla BIREBIR eslesmeli."""

        wanted = {t.path: t for t in proposal.targets}
        seen: set[str] = set()
        for change in changes:
            target = wanted.get(change.path)
            if target is None:
                raise ScopeViolation("out-of-scope-file", change.path)
            if change.preimage_digest != target.preimage_digest:
                raise ScopeViolation("preimage-differs-from-proposal", change.path)
            if change.postimage_digest != target.postimage_digest:
                raise ScopeViolation("postimage-differs-from-proposal", change.path)
            seen.add(change.path)
        missing = sorted(set(wanted) - seen)
        if missing:
            raise ScopeViolation("target-missing-from-change-set", missing[0])
        try:
            return TestPatch(tuple(changes))
        except ValidationFailed as exc:
            raise ScopeViolation("invalid-change-set", str(exc)) from exc

    def apply_approved(
        self,
        request: UnitTestRequest,
        proposal: RefactorProposal,
        approval: RefactorApproval,
        changes: Sequence[FileChange],
        *,
        builder_agent_id: str,
    ) -> RefactorResult:
        """Onayli refactor'u uygular; onaysiz/uyumsuz/kapsam disinda HICBIR sey yazilmaz."""

        if type(builder_agent_id) is not str or not builder_agent_id.strip():
            raise RefactorError("builder-agent-id-required")
        origin = self.validate_proposal(request, proposal, check_disk=False)
        pre = self.environment.observe()
        current = self.workspace.digest_paths(proposal.target_paths)
        approval.verify_for(
            proposal, current_source_revision=pre.source_revision, current_target_digests=current
        )
        patch = self._check_changes(proposal, changes)
        dirty = {fold_path(p) for p in pre.protected_dirty_paths}
        for target in proposal.targets:
            if fold_path(target.path) in dirty:
                raise ScopeViolation("user-dirty-path", target.path)
        control = self.control or InMemoryLoopControl()
        signal = control.signal()
        if signal is not ControlSignal.RUN:
            raise RefactorError(f"{signal.value}-before-claim")

        verification = derive_verification_request(
            request,
            proposal_digest=proposal.proposal_digest,
            approval_digest=approval.approval_digest,
        )
        vd = verification.request_digest
        if self.ledger.list_attempts(vd):
            raise RefactorError("approval-already-consumed")
        self.ledger.register_request(verification, now=self._now())
        preimages = self.workspace.read_bytes(proposal.target_paths)
        # Raw production bytes stay bounded in the workspace memory for this transaction.
        # The CAS receives only canonical manifests/digests below; source content must not
        # become an artifact, because it may contain secrets or personal data.
        manifest = CandidateManifest(
            parent_state_digest=approval.approval_digest,
            files=tuple(
                CandidateFile(f.path, f.preimage_digest, f.postimage_digest)
                for f in sorted(patch.files, key=lambda f: f.path)
            ),
            kind="production-refactor",
        )
        self.objects.put(canonical_bytes(manifest.to_payload()), media_type="application/json")
        self.objects.put(canonical_bytes(proposal.to_payload()), media_type="application/json")
        self.objects.put(canonical_bytes(approval.to_payload()), media_type="application/json")
        attempt = UnitTestAttempt(
            attempt_id=f"ut-refactor-{_hex16(vd)}-1",
            request_digest=vd,
            ordinal=1,
            parent_attempt_id=None,
            plan_digest=proposal.proposal_digest,
            candidate_digest=manifest.candidate_digest,
            idempotency_key=f"ut-refactor-{_hex16(vd)}-1-claim",
            changed_files=manifest.changed_files,
        )
        self.ledger.claim_attempt(attempt, now=self._now())

        base = {
            "contract": EVIDENCE_CONTRACT,
            "proposal_digest": proposal.proposal_digest,
            "approval_digest": approval.approval_digest,
            "origin_request_digest": request.request_digest,
            "verification_request_digest": vd,
            "manifest_digest": manifest.candidate_digest,
            "note": VERIFICATION_NOTE,
        }
        paths = proposal.target_paths
        try:
            applied = self.workspace.apply(patch, policy=None)
        except PreimageMismatch as exc:
            self._close_failed(
                verification,
                attempt,
                base,
                {"apply": "preimage-mismatch"},
                UnitTestStopReason.REFACTOR_APPROVAL_REQUIRED,
            )
            raise ApprovalMismatch("production-digest-changed", exc.path) from exc
        except Exception:
            self._close_failed(
                verification,
                attempt,
                base,
                {"apply": "failed"},
                UnitTestStopReason.RECOVERY_REQUIRED,
            )
            raise
        try:
            return self._verify_and_decide(
                request,
                verification,
                proposal,
                approval,
                attempt,
                manifest,
                applied,
                preimages,
                pre,
                origin,
                base,
                builder_agent_id.strip(),
                control,
            )
        except Exception:
            receipt = self._rollback(applied, preimages, proposal)
            reason = UnitTestStopReason.RECOVERY_REQUIRED
            self._close_failed(
                verification,
                attempt,
                base,
                {"crashed": True, "rollback_clean": receipt[1], "paths": list(paths)},
                reason,
            )
            raise

    # -- dogrulama ve karar --------------------------------------------------------------

    def _verify_and_decide(
        self,
        request: UnitTestRequest,
        verification: UnitTestRequest,
        proposal: RefactorProposal,
        approval: RefactorApproval,
        attempt: UnitTestAttempt,
        manifest: CandidateManifest,
        applied: AppliedPatch,
        preimages: Mapping[str, bytes],
        pre: EnvironmentObservation,
        origin: _OriginEvidence,
        base: dict[str, Any],
        builder_agent_id: str,
        control: LoopControl,
    ) -> RefactorResult:
        reasons: list[str] = []
        stop = UnitTestStopReason.REFACTOR_APPROVAL_REQUIRED
        post = self.environment.observe()
        written = self.workspace.digest_paths(proposal.target_paths)
        for target in proposal.targets:
            if written.get(target.path) != target.postimage_digest:
                reasons.append(f"target-digest-changed-after-apply:{target.path}")
        if post.build_config_digest != pre.build_config_digest:
            reasons.append("build-config-changed-by-refactor")
        if post.launcher_digest != pre.launcher_digest:
            reasons.append("toolchain-changed-by-refactor")

        record: MeasurementRecord | None = None
        review: RefactorReview | None = None
        extra: dict[str, Any] = {}
        if not reasons:
            run = self.measurer.measure(
                verification,
                attempt_id=attempt.attempt_id,
                run_id=f"{attempt.attempt_id}-run",
                candidate_digest=manifest.candidate_digest,
                timeout_seconds=float(verification.budget.process_timeout_seconds),
                cancel=control.cancel_event,
            )
            record = run.record
            if record is None:
                reasons.append("measurement-blocked")
                reasons += list(run.blocked_details)
                stop = run.blocked_reason or UnitTestStopReason.ENVIRONMENT_MISSING
            else:
                extra["measurement_digest"] = record.record_digest
                stop, measurement_reasons = self._judge_measurement(
                    verification, record, post, manifest
                )
                reasons += measurement_reasons
        if not reasons and record is not None:
            review, review_reasons, review_stop = self._review(
                verification, proposal, applied, preimages, record, builder_agent_id
            )
            reasons += review_reasons
            stop = review_stop or stop
            if review is not None:
                extra["review"] = review.to_payload()

        if not reasons:
            # Kabulden hemen once: olcum/inceleme sirasinda baska bir surec yazdiysa kabul YOK.
            final = self.workspace.digest_paths(proposal.target_paths)
            reasons += [
                f"target-digest-changed-before-accept:{t.path}"
                for t in proposal.targets
                if final.get(t.path) != t.postimage_digest
            ]
        if reasons or record is None:
            return self._reject(
                verification,
                proposal,
                approval,
                attempt,
                applied,
                preimages,
                base,
                reasons,
                stop,
                review,
                extra,
            )
        return self._accept(
            request,
            verification,
            proposal,
            approval,
            attempt,
            manifest,
            post,
            origin,
            record,
            review,
            base,
            extra,
        )

    def _judge_measurement(
        self,
        verification: UnitTestRequest,
        record: MeasurementRecord,
        post: EnvironmentObservation,
        manifest: CandidateManifest,
    ) -> tuple[UnitTestStopReason, list[str]]:
        stop = UnitTestStopReason.MEASUREMENT_INCOMPLETE
        if record.status is MeasurementStatus.TESTS_FAILED:
            return UnitTestStopReason.REFACTOR_APPROVAL_REQUIRED, ["regression:tests-failed"]
        if record.status is not MeasurementStatus.ACCEPTED:
            if record.status is MeasurementStatus.NO_TESTS_BASELINE:
                return stop, ["no-tests-to-verify"]
            return record.stop_reason or stop, [f"measurement:{record.status.value}"]
        binding = record.binding
        if binding is None:
            return stop, ["measurement-unbound"]
        toolchain = (
            binding.toolchain.toolchain_digest
            if post.launcher_digest == binding.toolchain.launcher_digest
            else digest("launcher-drift")
        )
        verdict = check_freshness(
            binding,
            CurrentState(
                request_digest=verification.request_digest,
                source_revision=verification.source_revision,
                production_digests=post.production_digests,
                build_config_digest=post.build_config_digest,
                toolchain_digest=toolchain,
                test_candidate_digest=manifest.candidate_digest,
            ),
        )
        if verdict is not FreshnessVerdict.FRESH:
            return stop, [f"freshness:{verdict.value}"]
        return stop, []

    def _review(
        self,
        verification: UnitTestRequest,
        proposal: RefactorProposal,
        applied: AppliedPatch,
        preimages: Mapping[str, bytes],
        record: MeasurementRecord,
        builder_agent_id: str,
    ) -> tuple[RefactorReview | None, list[str], UnitTestStopReason | None]:
        files = []
        for item in applied.files:
            before = preimages.get(item.path)
            files.append(
                {
                    "path": item.path,
                    "preimage_digest": item.preimage_digest,
                    "postimage_digest": item.postimage_digest,
                    "before": None if before is None else before.decode("utf-8", "replace"),
                    "after": self._after_text(item.path),
                }
            )
        context = {
            "review_kind": "production-refactor",
            "proposal_digest": proposal.proposal_digest,
            "technique": proposal.technique,
            "minimal_change": proposal.minimal_change,
            "declared_impact": [i.to_payload() for i in proposal.impact],
            "required_review_areas": [a.value for a in proposal.verification.review_areas],
            "answer": {
                "verdict": "accepted|rejected",
                "area_reviews": [
                    {
                        "area": "api|config|exception|side-effect",
                        "status": "|".join(s.value for s in AreaStatus),
                    }
                ],
                "scope_findings": [k.value for k in OutOfScopeKind],
                "new_material_risks": ["text"],
            },
            "measurement": {
                "status": record.status.value,
                "test_kind": record.evidence.test_kind.value,
                "test_counts": [list(c) for c in record.evidence.test_counts],
            },
            "limits": VERIFICATION_NOTE,
            "files": files,
        }
        task = AgentTask(
            specialty=AgentSpecialty.FINAL_VERIFIER,
            request_digest=verification.request_digest,
            ordinal=1,
            sequence=1,
            context=context,
            read_resources=proposal.target_paths,
            write_resources=(),
            risk="high",
        )
        try:
            response = self.gateway.invoke(task)
        except AgentDispatchFailure as exc:
            stop = (
                UnitTestStopReason.RECOVERY_REQUIRED
                if exc.recoverable
                else UnitTestStopReason.ENVIRONMENT_MISSING
            )
            return None, [f"review-unavailable:{exc.category}"], stop
        except PolicyViolation as exc:
            return (
                None,
                [f"review-context-refused:{type(exc).__name__}"],
                (UnitTestStopReason.ENVIRONMENT_MISSING),
            )
        if response.specialty not in VERIFYING:
            return None, ["review-role-invalid"], None
        if response.envelope.agent_id == builder_agent_id:
            return None, ["review-not-independent"], None
        try:
            review = parse_refactor_review(response.body)
        except ValidationFailed:
            return None, ["review-malformed"], None
        return review, list(review.problems(proposal)), None

    def _after_text(self, path: str) -> str:
        data = self.workspace.read_bytes([path]).get(path, b"")
        return data.decode("utf-8", "replace")

    # -- sonuclar ----------------------------------------------------------------------

    def _rollback(
        self, applied: AppliedPatch, preimages: Mapping[str, bytes], proposal: RefactorProposal
    ) -> tuple[RevertReceipt, bool]:
        receipt = self.workspace.revert(applied, preimages=preimages)
        disk = self.workspace.digest_paths(proposal.target_paths)
        restored = all(disk.get(t.path) == t.preimage_digest for t in proposal.targets)
        return receipt, receipt.clean and restored

    def _reject(
        self,
        verification: UnitTestRequest,
        proposal: RefactorProposal,
        approval: RefactorApproval,
        attempt: UnitTestAttempt,
        applied: AppliedPatch,
        preimages: Mapping[str, bytes],
        base: dict[str, Any],
        reasons: list[str],
        stop: UnitTestStopReason,
        review: RefactorReview | None,
        extra: dict[str, Any],
    ) -> RefactorResult:
        receipt, clean = self._rollback(applied, preimages, proposal)
        status = RefactorStatus.ROLLED_BACK if clean else RefactorStatus.ROLLBACK_INCOMPLETE
        reason = stop if clean else UnitTestStopReason.RECOVERY_REQUIRED
        evidence_digest = self._close_failed(
            verification,
            attempt,
            base,
            {
                "status": status.value,
                "reasons": reasons,
                "restored": list(receipt.restored),
                "foreign_modified": list(receipt.foreign_modified),
                "failed": list(receipt.failed),
                **extra,
            },
            reason,
        )
        return RefactorResult(
            status=status,
            stop_reason=reason,
            proposal_digest=proposal.proposal_digest,
            approval_digest=approval.approval_digest,
            verification_request_digest=verification.request_digest,
            evidence_digest=evidence_digest,
            detail=tuple(reasons),
            review=review,
            revert=receipt,
            changed_paths=() if clean else proposal.target_paths,
        )

    def _accept(
        self,
        request: UnitTestRequest,
        verification: UnitTestRequest,
        proposal: RefactorProposal,
        approval: RefactorApproval,
        attempt: UnitTestAttempt,
        manifest: CandidateManifest,
        post: EnvironmentObservation,
        origin: _OriginEvidence,
        record: MeasurementRecord,
        review: RefactorReview | None,
        base: dict[str, Any],
        extra: dict[str, Any],
    ) -> RefactorResult:
        old = origin.old_baseline
        lineage = lineage_digest(
            origin_request_digest=request.request_digest,
            proposal_digest=proposal.proposal_digest,
            approval_digest=approval.approval_digest,
            verification_request_digest=verification.request_digest,
            post_target_digests=((t.path, t.postimage_digest) for t in proposal.targets),
            old_baseline_id=None if old is None else old.baseline_id,
        )
        new = SourceBaseline.from_measurement(
            verification, record, source_revision=post.source_revision, lineage_digest=lineage
        )
        followup = derive_followup_request(
            request, source_revision=post.source_revision, lineage=lineage
        )
        for item in (old, new):
            if item is not None:
                self.objects.put(canonical_bytes(item.to_payload()), media_type="application/json")
        evidence = self._put_evidence(
            {
                **base,
                "status": RefactorStatus.VERIFIED.value,
                "lineage_digest": lineage,
                "old_baseline_digest": None if old is None else old.baseline_digest,
                "new_baseline_digest": new.baseline_digest,
                "followup_request_digest": followup.request_digest,
                "baseline_comparison": "not-comparable-across-refactor",
                **extra,
            }
        )
        self.ledger.record_attempt_receipt(
            attempt_id=attempt.attempt_id,
            status=AttemptState.COMPLETED,
            evidence_digest=evidence,
            observations=record.evidence.observations,
            now=self._now(),
        )
        return RefactorResult(
            status=RefactorStatus.VERIFIED,
            stop_reason=None,
            proposal_digest=proposal.proposal_digest,
            approval_digest=approval.approval_digest,
            verification_request_digest=verification.request_digest,
            evidence_digest=evidence,
            followup_request=followup,
            lineage_digest=lineage,
            old_baseline=old,
            new_baseline=new,
            review=review,
            changed_paths=proposal.target_paths,
        )

    def _close_failed(
        self,
        verification: UnitTestRequest,
        attempt: UnitTestAttempt,
        base: Mapping[str, Any],
        detail: Mapping[str, Any],
        reason: UnitTestStopReason,
    ) -> str:
        evidence = self._put_evidence({**base, **detail})
        self.ledger.record_attempt_receipt(
            attempt_id=attempt.attempt_id,
            status=AttemptState.FAILED,
            evidence_digest=evidence,
            observations=(),
            now=self._now(),
        )
        self.ledger.record_terminal(
            UnitTestTerminal(verification.request_digest, reason, None, evidence), now=self._now()
        )
        return evidence

    def _put_evidence(self, payload: Mapping[str, Any]) -> str:
        return self.objects.put(
            canonical_bytes(dict(payload)), media_type="application/json"
        ).digest

    def _now(self) -> dt.datetime:
        return self.clock()


def resume_test_loop(
    loop: UnitTestLoopService,
    result: RefactorResult,
    *,
    approvals: UnitTestApprovals,
    limits: LoopLimits | None = None,
) -> LoopOutcome:
    """Dar entegrasyon noktasi: dogrulanmis refactor'un yeni baseline'indan AYNI hedef politikasiyla
    test dongusunu surdurur. ``UnitTestLoopService`` degismez; yeni istek kimligi kullanilir."""

    if result.status is not RefactorStatus.VERIFIED or result.followup_request is None:
        raise RefactorError("followup-not-available", result.status.value)
    return loop.run(result.followup_request, approvals=approvals, limits=limits)
