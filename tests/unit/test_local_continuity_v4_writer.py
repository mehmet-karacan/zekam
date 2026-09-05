"""Contract tests for the dormant operational-v4 close writer surface."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from zekam.application.local_continuity import ContinuityBinding, ContinuityTail
from zekam.application.local_continuity_close import CloseCandidateBundle, CloseSummary
from zekam.application.local_continuity_v4_writer import (
    CanonicalManifestProvenance,
    CurrentSourceSnapshot,
    ExactResolvedRecovery,
    FinalizeClosedWriteRequest,
    FrozenCloseWriteRequest,
    FrozenProjectionSnapshot,
    FrozenSpoolSnapshot,
    ResolvedManifestFragment,
    VerifiedManifest,
    derived_operation_key,
    internal_receipt_digest,
    revision_digest,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ValidationFailed

pytestmark = pytest.mark.unit

NOW = "2026-09-03T12:00:00+00:00"


def _binding() -> ContinuityBinding:
    return ContinuityBinding(
        "018f0000-0000-7000-8000-000000000001",
        "external-session",
        "018f0000-0000-7000-8000-000000000002",
        "018f0000-0000-7000-8000-000000000003",
        "codex",
        "macbook",
        "018f0000-0000-7000-8000-000000000004",
        digest("task"),
        digest("plan"),
        digest("policy"),
    )


def _summary() -> CloseSummary:
    return CloseSummary(
        ("Completed bounded close checks.",),
        (),
        (),
        ("Continue with independent verification.",),
        "Await independent verification.",
        (("src/akilli_kasa/api/saglik.py", digest("source")),),
        (("context/evidence", digest("context")),),
    )


def _freeze_request(**changes: object) -> FrozenCloseWriteRequest:
    values: dict[str, object] = {
        "binding": _binding(),
        "expected_attachment_revision_digest": digest("revision"),
        "expected_process_generation_digest": digest("generation"),
        "expected_tail": ContinuityTail(1, digest("tail")),
        "active_manifest_digest": digest("context"),
        "checkpoint_idempotency_key": "close-checkpoint",
        "operation_key": "close-operation",
        "summary": _summary(),
        "candidates": None,
        "observed_at": NOW,
    }
    values.update(changes)
    return FrozenCloseWriteRequest(**values)  # type: ignore[arg-type]


def test_v1_and_explicit_empty_v2_are_distinct_valid_requests() -> None:
    v1 = _freeze_request()
    v2 = _freeze_request(candidates=CloseCandidateBundle())

    assert v1.candidates is None
    assert type(v2.candidates) is CloseCandidateBundle


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_attachment_revision_digest", None),
        ("expected_attachment_revision_digest", str(digest("revision"))),
        ("expected_process_generation_digest", True),
        ("checkpoint_idempotency_key", " "),
        ("operation_key", "x" * 513),
        ("observed_at", "2026-09-03T12:00:00.1+00:00"),
        ("observed_at", "2026-09-03T15:00:00+03:00"),
    ],
)
def test_freeze_request_rejects_noncanonical_inputs(field: str, value: object) -> None:
    if field == "expected_attachment_revision_digest" and isinstance(value, str):

        class DigestSubclass(str):
            pass

        value = DigestSubclass(value)
    with pytest.raises(ValidationFailed):
        _freeze_request(**{field: value})


def test_request_dataclass_is_frozen_and_slotted() -> None:
    request = _freeze_request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.operation_key = "changed"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        request.extra = "no"  # type: ignore[attr-defined]


def test_binding_string_subclass_is_rejected_before_writer_mutation() -> None:
    class TextSubclass(str):
        pass

    binding = dataclasses.replace(_binding(), external_session_id=TextSubclass("external-session"))
    with pytest.raises(ValidationFailed, match="exact binding string"):
        _freeze_request(binding=binding)


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        ("hook", "restored"),
        ("local", "completed"),
        ("local", "delivered"),
    ],
)
def test_exact_resolved_recovery_positive_matrix(kind: str, outcome: str) -> None:
    recovery = ExactResolvedRecovery(
        digest("predecessor"),
        kind,
        "018f0000-0000-7000-8000-000000000005",
        "018f0000-0000-7000-8000-000000000006",
        outcome,
        NOW,
    )

    request = FinalizeClosedWriteRequest(
        _binding(), digest("request"), digest("frozen"), "finalize", NOW, recovery
    )
    assert request.recovery is recovery


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [("hook", "completed"), ("local", "restored"), ("unknown", "delivered")],
)
def test_exact_resolved_recovery_rejects_incompatible_outcome(kind: str, outcome: str) -> None:
    with pytest.raises(ValidationFailed):
        ExactResolvedRecovery(
            digest("predecessor"),
            kind,
            "018f0000-0000-7000-8000-000000000005",
            "018f0000-0000-7000-8000-000000000006",
            outcome,
            NOW,
        )


def test_external_evidence_snapshots_reject_duplicates_and_noncanonical_order() -> None:
    with pytest.raises(ValidationFailed):
        FrozenSpoolSnapshot("session", "external", "codex", (digest("a"), digest("a")))
    with pytest.raises(ValidationFailed):
        FrozenProjectionSnapshot(
            (
                {
                    "portable_ref": "z",
                    "content_digest": digest("z"),
                    "bytes_digest": digest("z-bytes"),
                },
                {
                    "portable_ref": "a",
                    "content_digest": digest("a"),
                    "bytes_digest": digest("a-bytes"),
                },
            )
        )


def test_new_digest_recipes_have_fixed_producer_and_revision_domains() -> None:
    body = {"event_kind": "PRE_CLOSE", "session_id": _binding().session_id}
    assert internal_receipt_digest(
        body, producer_kind="close_request_digest", producer_ref=digest("close")
    ) == digest(
        {
            "schema": "zekam-internal-event-receipt/v1",
            "body": body,
            "producer": {
                "kind": "close_request_digest",
                "ref": digest("close"),
            },
            "grants_authority": False,
            "approval_inherited": False,
        }
    )
    revision = {"state": "frozen", "revision_number": 3}
    assert revision_digest(revision) == digest(revision)
    with pytest.raises(ValidationFailed):
        revision_digest({"revision_digest": digest("self")})


def test_derived_keys_preserve_bounded_explicit_base() -> None:
    assert derived_operation_key("close", "pre-close") == "close:pre-close"
    with pytest.raises(ValidationFailed):
        derived_operation_key("x" * 510, "pre-close")


def test_source_snapshot_and_resolved_fragment_are_exact_frozen_values() -> None:
    snapshot = CurrentSourceSnapshot("snapshot", "HEAD", digest("source"))
    provenance_body = {
        "id": "candidate",
        "source_ref": "src/health.py",
        "digest": digest("health"),
    }
    provenance = CanonicalManifestProvenance(
        "candidate", canonical_json(provenance_body), digest(provenance_body)
    )
    fragment = ResolvedManifestFragment("candidate", "healthy")

    assert snapshot.revision_ref == "HEAD"
    assert provenance.body_digest == digest(provenance_body)
    assert fragment.text == "healthy"
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.revision_ref = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "factory",
    (
        lambda: CurrentSourceSnapshot("snapshot", "HEAD", "not-a-digest"),
        lambda: ResolvedManifestFragment("candidate", ""),
        lambda: ResolvedManifestFragment("candidate", "\ud800"),
        lambda: CanonicalManifestProvenance("candidate", "{}", digest("wrong")),
        lambda: CanonicalManifestProvenance("candidate", '{"b":1,"a":2}', digest({"a": 2, "b": 1})),
    ),
)
def test_source_evidence_values_reject_malformed_or_noncanonical_input(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValidationFailed):
        factory()


def test_verified_manifest_public_constructor_requires_exact_immutable_values() -> None:
    with pytest.raises(ValidationFailed):
        VerifiedManifest(True, 7, True, False, (), ())  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        VerifiedManifest(digest("manifest"), None, 8, 0, [], ())  # type: ignore[arg-type]


def test_projection_snapshot_copies_caller_evidence_into_immutable_mappings() -> None:
    caller_item = {
        "portable_ref": "memory/a.md",
        "content_digest": digest("content"),
        "bytes_digest": digest("bytes"),
    }
    snapshot = FrozenProjectionSnapshot((caller_item,))
    caller_item["portable_ref"] = "memory/changed.md"

    assert snapshot.evidence[0]["portable_ref"] == "memory/a.md"
    with pytest.raises(TypeError):
        snapshot.evidence[0]["portable_ref"] = "memory/mutated.md"  # type: ignore[index]
