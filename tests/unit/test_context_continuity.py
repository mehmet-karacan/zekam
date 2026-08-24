"""P08 deterministic context, journal, checkpoint ve resume tests."""

from __future__ import annotations

import datetime as dt
import itertools
from uuid import uuid4

import pytest

from zekam.application.context_continuity_service import ContextContinuityService
from zekam.domain.canonical import digest
from zekam.domain.clients import ClientDescriptor, ClientKind, ClientPermissionManifest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    Checkpoint,
    ContextCandidate,
    ContinuitySnapshot,
    EvidenceReference,
    FinalizedHandoff,
    JournalEntry,
    OmittedReason,
    TargetRouteBinding,
    compile_context,
    verify_journal,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed

NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
DIGEST = digest("p08")


class RouteGuard:
    def require_current(
        self,
        decision_id,  # type: ignore[no-untyped-def]
        *,
        target_model_ref: str,
        target_client_id: str,
        project_id: str,
        at: dt.datetime | None,
    ) -> TargetRouteBinding:
        del target_client_id, project_id
        moment = at or NOW
        return TargetRouteBinding(
            decision_id,
            digest((str(decision_id), target_model_ref)),
            target_model_ref,
            moment + dt.timedelta(minutes=5),
            moment,
        )


def _candidate(
    name: str,
    *,
    authority: AuthorityLevel = AuthorityLevel.VERIFIED,
    age: int = 0,
    tokens: int = 10,
    required: bool = False,
) -> ContextCandidate:
    return ContextCandidate(
        name,
        authority,
        NOW - dt.timedelta(seconds=age),
        "revision-1",
        digest(name),
        tokens,
        required,
    )


def test_context_selection_is_deterministic_and_required_first() -> None:
    candidates = (
        _candidate("optional-canonical", authority=AuthorityLevel.CANONICAL, tokens=20),
        _candidate("required", authority=AuthorityLevel.VERIFIED, tokens=10, required=True),
        _candidate("optional-fresh", tokens=10),
    )
    expected: tuple[str, ...] | None = None
    for ordering in itertools.permutations(candidates):
        manifest = compile_context(
            ordering, token_budget=20, minimum_authority=AuthorityLevel.OBSERVED, now=NOW
        )
        selected = tuple(item.candidate_id for item in manifest.selected)
        expected = selected if expected is None else expected
        assert selected == expected == ("required", "optional-fresh")
        assert manifest.selected[0].reason == "required-first"


def test_required_overflow_fails_and_omission_reasons_are_explicit() -> None:
    with pytest.raises(PolicyViolation, match="Required"):
        compile_context(
            (
                _candidate("one", tokens=8, required=True),
                _candidate("two", tokens=8, required=True),
            ),
            token_budget=10,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
        )
    manifest = compile_context(
        (
            _candidate("selected", tokens=8),
            _candidate("budget", tokens=8),
            _candidate("weak", authority=AuthorityLevel.UNTRUSTED),
            _candidate("stale", age=31 * 24 * 60 * 60),
        ),
        token_budget=8,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    reasons = {item.candidate_id: item.reason for item in manifest.omitted}
    assert reasons == {
        "selected": OmittedReason.BUDGET,
        "weak": OmittedReason.INSUFFICIENT_AUTHORITY,
        "stale": OmittedReason.STALE,
    }


def test_required_stale_context_fails_closed() -> None:
    stale = _candidate(
        "required-stale",
        authority=AuthorityLevel.CANONICAL,
        age=31 * 24 * 60 * 60,
        required=True,
    )
    with pytest.raises(PolicyViolation, match="Required context candidate"):
        compile_context(
            (stale,),
            token_budget=50,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
        )


def test_manifest_fingerprint_changes_on_source_or_digest_drift() -> None:
    first = compile_context(
        (_candidate("a"),), token_budget=20, minimum_authority=AuthorityLevel.OBSERVED, now=NOW
    )
    changed = ContextCandidate(
        "a", AuthorityLevel.VERIFIED, NOW, "revision-2", digest("changed"), 10
    )
    second = compile_context(
        (changed,), token_budget=20, minimum_authority=AuthorityLevel.OBSERVED, now=NOW
    )
    assert first.candidate_fingerprint != second.candidate_fingerprint


def test_journal_detects_insert_remove_reorder_tamper_and_truncation() -> None:
    first = JournalEntry(1, "work-1", "started", digest("one"), None, False, NOW)
    second = JournalEntry(2, "work-1", "summary", digest("two"), first.entry_digest, True, NOW)
    head = verify_journal((first, second))
    assert verify_journal((first, second), head) == head
    tampered = JournalEntry(
        2, "work-1", "summary", digest("changed"), first.entry_digest, True, NOW
    )
    for broken in ((second,), (second, first), (first, tampered)):
        with pytest.raises(ValidationFailed):
            verify_journal(broken, head)


def _checkpoint() -> Checkpoint:
    return Checkpoint(
        "checkpoint-1",
        "project-1",
        "work-1",
        "plan-1",
        "revision-1",
        ("read", "build"),
        ("read",),
        ("build",),
        (("read", digest("read-result")),),
        digest("manifest"),
        digest("journal"),
        "reacquire-work",
        NOW,
    )


def test_checkpoint_requires_exact_partition_and_completed_results() -> None:
    checkpoint = _checkpoint()
    assert checkpoint.checkpoint_digest.startswith("sha256:")
    with pytest.raises(ValidationFailed, match="partition"):
        Checkpoint(
            checkpoint.checkpoint_id,
            checkpoint.project_id,
            checkpoint.work_item_id,
            checkpoint.plan_revision_id,
            checkpoint.source_revision,
            checkpoint.plan_steps,
            ("read",),
            (),
            checkpoint.step_results,
            checkpoint.context_manifest_digest,
            checkpoint.journal_head_digest,
            checkpoint.next_safe_action,
            NOW,
        )


@pytest.mark.parametrize(
    "target",
    (
        ("codex", "claude-code", ClientKind.CLAUDE_CODE),
        ("claude-code", "opencode", ClientKind.OPENCODE),
        ("opencode", "codex", ClientKind.CODEX),
    ),
)
def test_transcript_free_cross_client_resume_requires_reacquire(
    target: tuple[str, str, ClientKind],
) -> None:
    checkpoint = _checkpoint()
    snapshot = ContinuitySnapshot(
        "project-1",
        "work-1",
        checkpoint.checkpoint_digest,
        checkpoint.journal_head_digest,
        checkpoint.context_manifest_digest,
        checkpoint.source_revision,
        ("docs/context.md",),
        ("reacquire-work",),
        (EvidenceReference("benchmark", "model-decision:latest", DIGEST),),
        NOW,
    )
    target_descriptor = ClientDescriptor(
        target[2],
        target[1],
        target[1],
        frozenset({"code", "structured-result"}),
        "1",
        ClientPermissionManifest(
            f"{target[1]}-test", ("filesystem.read", "process.run"), managed=True
        ),
    )
    source_permissions = ClientPermissionManifest(
        f"{target[0]}-test", ("filesystem.read", "process.run"), managed=True
    )
    route_id = uuid4()
    handoff = FinalizedHandoff(
        target[0],
        target[1],
        "model-ref-a",
        "model-ref-b",
        snapshot.snapshot_digest,
        checkpoint.checkpoint_digest,
        checkpoint.source_revision,
        NOW,
        source_client_capability_digest=digest(f"capability:{target[0]}"),
        target_client_capability_digest=(target_descriptor.capability_manifest.capability_digest),
        source_client_permission_digest=source_permissions.permission_digest,
        target_client_permission_digest=(target_descriptor.permission_manifest.permission_digest),
        target_route_decision_id=route_id,
        target_route_decision_digest=digest((str(route_id), "model-ref-b")),
        target_route_valid_until=NOW + dt.timedelta(minutes=5),
        target_route_fresh=True,
    )
    service = ContextContinuityService(route_guard=RouteGuard())
    resumed = service.resume(
        handoff=handoff,
        snapshot=snapshot,
        checkpoint=checkpoint,
        current_source_revision="revision-1",
        target_client=target_descriptor,
        observed_at=NOW,
    )
    assert resumed.client == target[1]
    assert resumed.reacquire_work and not resumed.transcript_used and not resumed.grants_authority
    drifted_target = ClientDescriptor(
        target[2],
        target[1],
        target[1],
        frozenset({"structured-result"}),
        "1",
        target_descriptor.permission_manifest,
    )
    with pytest.raises(PolicyViolation, match="capability digest drift"):
        service.resume(
            handoff=handoff,
            snapshot=snapshot,
            checkpoint=checkpoint,
            current_source_revision="revision-1",
            target_client=drifted_target,
            observed_at=NOW,
        )
    with pytest.raises(PolicyViolation, match="stale"):
        service.resume(
            handoff=handoff,
            snapshot=snapshot,
            checkpoint=checkpoint,
            current_source_revision="revision-2",
            target_client=target_descriptor,
            observed_at=NOW,
        )


@pytest.mark.parametrize(
    ("target_capabilities", "target_permissions", "expected_replan"),
    (
        (
            frozenset({"code", "structured-result"}),
            ("filesystem.read", "process.run"),
            "client-capability-reroute",
        ),
        (
            frozenset({"code", "structured-result", "tool-use"}),
            ("filesystem.read",),
            "client-permission-reroute",
        ),
    ),
)
def test_cross_client_capability_ve_permission_kapisi_fail_closed(
    target_capabilities: frozenset[str],
    target_permissions: tuple[str, ...],
    expected_replan: str,
) -> None:
    checkpoint = _checkpoint()
    snapshot = ContinuitySnapshot(
        "project-1",
        "work-1",
        checkpoint.checkpoint_digest,
        checkpoint.journal_head_digest,
        checkpoint.context_manifest_digest,
        checkpoint.source_revision,
        ("docs/context.md",),
        ("reacquire-work",),
        (EvidenceReference("benchmark", "model-decision:latest", DIGEST),),
        NOW,
    )
    source = ClientDescriptor(
        ClientKind.CODEX,
        "codex",
        "codex",
        frozenset({"code", "structured-result", "tool-use"}),
        "1",
        ClientPermissionManifest("source", ("filesystem.read", "process.run"), managed=True),
    )
    target = ClientDescriptor(
        ClientKind.OPENCODE,
        "opencode",
        "opencode",
        target_capabilities,
        "1",
        ClientPermissionManifest("target", target_permissions, managed=True),
    )
    service = ContextContinuityService(route_guard=RouteGuard())
    handoff = service.finalize_cross_client_handoff(
        source_client=source,
        target_client=target,
        source_model_ref="model-a",
        target_model_ref="model-b",
        snapshot=snapshot,
        checkpoint=checkpoint,
        required_capabilities=("tool-use",),
        required_permissions=("filesystem.read", "process.run"),
        target_route_decision_id=uuid4(),
        created_at=NOW,
    )
    assert expected_replan in handoff.required_replan_items
    assert not handoff.cross_client_ready
    with pytest.raises(PolicyViolation, match="capability/route replan"):
        service.resume(
            handoff=handoff,
            snapshot=snapshot,
            checkpoint=checkpoint,
            current_source_revision=checkpoint.source_revision,
        )
