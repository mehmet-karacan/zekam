"""Narrow global-note opt-in; real Akilli Kasa text remains context, never authority."""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from zekam.application.context_compiler import compile_context_v2
from zekam.application.context_ranking import (
    ContextRankingFeatureBuilder,
    ContextRankingRequest,
    count_context_tokens,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
    ContextManifest,
    OmittedReason,
)
from zekam.domain.context_scoring import ScopeProximity
from zekam.domain.errors import PolicyViolation, ValidationFailed

ROOT = Path("/Users/mkaracan/Projeler/akilli-kasa")
SOURCE_REF = "src/akilli_kasa/api/saglik.py"
NOW = dt.datetime(2026, 9, 2, 18, tzinfo=dt.UTC)
LEGACY_BODY = {
    "role": "builder",
    "target_identity_refs": ["work/akilli-kasa-health"],
    "step_scope_ref": None,
    "work_scope_ref": "work/akilli-kasa-health",
    "project_scope_ref": "project/akilli-kasa",
    "realm_scope_ref": "realm/local-test",
    "current_source_revision": "source/akilli-kasa-health",
    "compatible_source_revisions": [],
    "task_terms": ["saglik"],
    "tokenizer_profile_digest": DEFAULT_TOKENIZER_PROFILE_DIGEST,
}
LEGACY_DIGEST = "sha256:114792374eef93e6099fa7bddf7721e965d61f7805c66d00119c6a80c745888b"


def _request(**overrides: Any) -> ContextRankingRequest:
    values: dict[str, Any] = {
        **LEGACY_BODY,
        "target_identity_refs": ("work/akilli-kasa-health",),
        "compatible_source_revisions": (),
        "task_terms": ("saglik",),
    }
    return ContextRankingRequest(**(values | overrides))


@pytest.fixture
def real_text() -> str:
    path = ROOT / SOURCE_REF
    if not path.is_file():
        pytest.skip("Real read-only Akilli Kasa source is unavailable")
    return path.read_text(encoding="utf-8")


def _candidate(text: str, **overrides: Any) -> ContextCandidate:
    values: dict[str, Any] = {
        "candidate_id": "global-akilli-health",
        "authority": AuthorityLevel.OBSERVED,
        "observed_at": NOW,
        "source_revision": "source/akilli-kasa-health",
        "content_digest": digest(text),
        "token_count": count_context_tokens(text),
        "kind": ContextCandidateKind.KNOWLEDGE,
        "source_ref": SOURCE_REF,
        "scope_ref": "global-user",
        "identity_refs": (),
        "applicable_roles": ("builder",),
        "task_terms": ("saglik",),
    }
    return ContextCandidate(**(values | overrides))


def _compile(
    request: ContextRankingRequest,
    candidates: tuple[ContextCandidate, ...],
    text: str,
    *,
    token_budget: int = 16384,
) -> ContextManifest:
    return compile_context_v2(
        candidates,
        ranking_request=request,
        token_budget=token_budget,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        contents={item.candidate_id: text for item in candidates},
        ranking_snapshot_digest=digest(request.body()),
        candidate_set_digest=digest([item.candidate_digest for item in candidates]),
    )


def test_default_and_explicit_empty_request_preserve_exact_legacy_bytes_and_digest() -> None:
    for request in (_request(), _request(additional_scope_refs=())):
        assert request.body() == LEGACY_BODY
        assert canonical_json(request.body()) == canonical_json(LEGACY_BODY)
        assert digest(request.body()) == LEGACY_DIGEST
        assert "additional_scope_refs" not in request.body()


def test_explicit_global_opt_in_is_digest_bound_and_does_not_mutate_legacy_body() -> None:
    before = copy.deepcopy(LEGACY_BODY)
    request = _request(additional_scope_refs=("global-user",))
    assert request.body() == LEGACY_BODY | {"additional_scope_refs": ["global-user"]}
    assert digest(request.body()) != LEGACY_DIGEST
    assert before == LEGACY_BODY
    assert _request().body() == before


@pytest.mark.parametrize(
    "value",
    [
        None,
        False,
        True,
        0,
        "global-user",
        [],
        ["global-user"],
        {},
        ("global-user", "global-user"),
        ("project/foreign",),
        ("*",),
        ("GLOBAL-USER",),
        ("global-user", "realm/foreign"),
        (None,),
    ],
)
def test_additional_scope_rejects_wrong_types_duplicates_and_arbitrary_scope(value: object) -> None:
    with pytest.raises(ValidationFailed, match="additional scope"):
        _request(additional_scope_refs=value)


@pytest.mark.parametrize("realm", [None, "", " ", False, 17])
def test_global_opt_in_requires_bound_realm(realm: object) -> None:
    with pytest.raises(ValidationFailed, match="realm"):
        _request(additional_scope_refs=("global-user",), realm_scope_ref=realm)


def test_default_request_omits_global_knowledge_with_explicit_scope_reason(real_text: str) -> None:
    manifest = _compile(_request(), (_candidate(real_text),), real_text)
    assert manifest.selected == ()
    assert len(manifest.omitted) == 1
    assert manifest.omitted[0].reason is OmittedReason.SCOPE_MISMATCH


def test_global_opt_in_selects_without_scope_spoof_rank_boost_or_authority(real_text: str) -> None:
    request = _request(additional_scope_refs=("global-user",))
    global_note = _candidate(real_text)
    project_note = replace(
        global_note, candidate_id="project-health", scope_ref="project/akilli-kasa"
    )
    candidates = (global_note, project_note)
    features = ContextRankingFeatureBuilder(request).build_all(
        candidates,
        {item.candidate_id: real_text for item in candidates},
        now=NOW,
    )
    assert features[global_note.candidate_id].scope_proximity is ScopeProximity.EXTERNAL
    assert features[project_note.candidate_id].scope_proximity is ScopeProximity.PROJECT
    manifest = _compile(request, candidates, real_text)
    assert [item.candidate_id for item in manifest.selected] == [
        project_note.candidate_id,
        global_note.candidate_id,
    ]
    assert global_note.scope_ref == "global-user"
    assert manifest.omitted == ()
    assert manifest.body()["grants_authority"] is False
    assert all(item.authority is AuthorityLevel.OBSERVED for item in manifest.selected)


@pytest.mark.parametrize(
    "kind", [item for item in ContextCandidateKind if item is not ContextCandidateKind.KNOWLEDGE]
)
def test_global_opt_in_does_not_admit_other_candidate_kinds(
    real_text: str,
    kind: ContextCandidateKind,
) -> None:
    manifest = _compile(
        _request(additional_scope_refs=("global-user",)),
        (_candidate(real_text, kind=kind),),
        real_text,
    )
    assert manifest.selected == ()
    assert manifest.omitted[0].reason is OmittedReason.SCOPE_MISMATCH


@pytest.mark.parametrize(
    "scope", ["project/foreign", "realm/foreign", "global-user/foreign", "GLOBAL-USER"]
)
def test_global_opt_in_does_not_admit_other_owner_scopes(real_text: str, scope: str) -> None:
    manifest = _compile(
        _request(additional_scope_refs=("global-user",)),
        (_candidate(real_text, scope_ref=scope),),
        real_text,
    )
    assert manifest.selected == ()
    assert manifest.omitted[0].reason is OmittedReason.SCOPE_MISMATCH


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"source_revision": "source/foreign"}, OmittedReason.SOURCE_REVISION_MISMATCH),
        ({"identity_refs": ("work/foreign",)}, OmittedReason.IDENTITY_MISMATCH),
        ({"applicable_roles": ("verifier",)}, OmittedReason.ROLE_MISMATCH),
        ({"task_terms": ("unrelated",)}, OmittedReason.LOW_RELEVANCE),
        ({"observed_at": NOW - dt.timedelta(days=31)}, OmittedReason.STALE),
        ({"superseded": True}, OmittedReason.SUPERSEDED),
        ({"authority": AuthorityLevel.UNTRUSTED}, OmittedReason.INSUFFICIENT_AUTHORITY),
        ({"conflict_refs": ("note/contradiction",)}, OmittedReason.CONFLICT),
    ],
)
def test_global_opt_in_does_not_bypass_other_admission_gates(
    real_text: str,
    overrides: dict[str, Any],
    reason: OmittedReason,
) -> None:
    manifest = _compile(
        _request(additional_scope_refs=("global-user",)),
        (_candidate(real_text, **overrides),),
        real_text,
    )
    assert manifest.selected == ()
    assert manifest.omitted[0].reason is reason


def test_required_global_context_still_fails_closed_without_opt_in_or_budget(
    real_text: str,
) -> None:
    candidate = _candidate(real_text, required=True)
    with pytest.raises(PolicyViolation, match="Required"):
        _compile(_request(), (candidate,), real_text)
    with pytest.raises(PolicyViolation, match="Required"):
        _compile(
            _request(additional_scope_refs=("global-user",)),
            (candidate,),
            real_text,
            token_budget=1,
        )


def test_compilation_and_mutated_export_do_not_change_frozen_inputs(real_text: str) -> None:
    request = _request(additional_scope_refs=("global-user",))
    candidate = _candidate(real_text)
    before = copy.deepcopy((request, candidate))
    manifest = _compile(request, (candidate,), real_text)
    exported = request.body()
    exported["additional_scope_refs"].append("project/foreign")
    exported["target_identity_refs"].clear()
    assert (request, candidate) == before
    assert request.additional_scope_refs == ("global-user",)
    assert manifest.body()["grants_authority"] is False
    with pytest.raises(FrozenInstanceError):
        request.additional_scope_refs = ()  # type: ignore[misc]
