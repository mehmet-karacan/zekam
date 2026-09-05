from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from zekam.application.context_compiler import compile_context_v2
from zekam.application.context_ranking import ContextRankingRequest, count_context_tokens
from zekam.application.local_continuity import ContinuityBinding, LocalContext
from zekam.application.local_continuity_v4_ingress import (
    MAX_ADDITIONAL_CONTEXT_UTF8_BYTES,
    MAX_SESSION_START_SUCCESS_STDOUT_UTF8_BYTES,
    MAX_STARTUP_FRAGMENT_BUDGET_UNITS,
    STARTUP_CONTEXT_BUDGET_PROFILE,
    FrozenCurrentStartupContext,
    SessionStartIngressResult,
    _session_start_success_stdout,
    _validate_current_context_inputs,
    startup_fragment_budget_units,
)
from zekam.application.local_continuity_v4_writer import CurrentSourceSnapshot
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure import local_continuity_v4_composition as composition_module

NOW = "2026-09-03T12:00:00+00:00"
SESSION = "018f0000-0000-7000-8000-000000000001"
PROJECT = "018f0000-0000-7000-8000-000000000002"
REALM = "018f0000-0000-7000-8000-000000000003"
SNAPSHOT = "018f0000-0000-7000-8000-000000000004"


def _frozen(
    *,
    binding: ContinuityBinding | None = None,
    context: LocalContext | None = None,
    text: str = "health endpoint verifies database readiness",
) -> FrozenCurrentStartupContext:
    selected_binding = binding or _binding()
    selected_context = context or _context(text)
    source = CurrentSourceSnapshot(SNAPSHOT, "a" * 40, digest("source"))
    environment = digest("environment")
    hydration_key = "codex0151-session-start-018f0000-0000-7000-8000-000000000099"
    manifest, hydration, additional, stdout = _validate_current_context_inputs(
        binding=selected_binding,
        context=selected_context,
        source_snapshot=source,
        environment_evidence_digest=environment,
        hydration_key=hydration_key,
        observed_at=NOW,
    )
    values: dict[str, object] = {
        "binding": selected_binding,
        "binding_digest": selected_binding.binding_digest,
        "source_snapshot": source,
        "environment_evidence_digest": environment,
        "context": selected_context,
        "manifest_body_json": canonical_json(manifest),
        "manifest_digest": digest(manifest),
        "hydration_key": hydration_key,
        "hydration_body_json": canonical_json(hydration),
        "hydration_receipt_digest": digest(hydration),
        "observed_at": NOW,
        "additional_context": additional,
        "output_digest": digest(additional),
        "success_stdout": stdout,
    }
    result = object.__new__(FrozenCurrentStartupContext)
    for name in FrozenCurrentStartupContext.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    result.__post_init__()
    return result


def _replace_frozen(
    frozen: FrozenCurrentStartupContext, **changes: object
) -> FrozenCurrentStartupContext:
    values = {
        name: changes.get(name, getattr(frozen, name))
        for name in FrozenCurrentStartupContext.__dataclass_fields__
    }
    result = object.__new__(FrozenCurrentStartupContext)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    result.__post_init__()
    return result


def _binding() -> ContinuityBinding:
    return ContinuityBinding(
        SESSION,
        "codex-session",
        PROJECT,
        REALM,
        "codex",
        "macbook",
        SNAPSHOT,
        digest("task"),
        digest("plan"),
        digest("policy"),
    )


def _context(
    text: str = "health endpoint verifies database readiness",
    *,
    source_ref: str = "src/akilli_kasa/api/saglik.py",
) -> LocalContext:
    candidate = ContextCandidate(
        candidate_id="source-health",
        authority=AuthorityLevel.VERIFIED,
        observed_at=dt.datetime.fromisoformat(NOW),
        source_revision="a" * 40,
        content_digest=digest(text),
        token_count=count_context_tokens(text),
        required=True,
        kind=ContextCandidateKind.SOURCE_SLICE,
        source_ref=source_ref,
        scope_ref=f"project/{PROJECT}",
        identity_refs=("task/wp08",),
        applicable_roles=("builder",),
        canonical_revision_id=SNAPSHOT,
    )
    ranking = ContextRankingRequest(
        role="builder",
        target_identity_refs=("task/wp08",),
        step_scope_ref=None,
        work_scope_ref=None,
        project_scope_ref=f"project/{PROJECT}",
        realm_scope_ref=f"realm/{REALM}",
        current_source_revision="a" * 40,
        compatible_source_revisions=(),
        task_terms=(),
        tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
    )
    manifest = compile_context_v2(
        (candidate,),
        ranking_request=ranking,
        token_budget=2048,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=dt.datetime.fromisoformat(NOW),
        contents={candidate.candidate_id: text},
        ranking_snapshot_digest=digest(ranking.body()),
        candidate_set_digest=digest(candidate.candidate_digest),
        recipe_id="codex0151-session-start",
        recipe_digest=digest("codex0151-session-start"),
        target_role="builder",
    )
    return LocalContext(
        manifest,
        ((candidate.candidate_id, text),),
        ranking,
        (candidate,),
    )


def test_frozen_context_builds_exact_precommit_output() -> None:
    assert not hasattr(composition_module, "_from_current_source")
    binding = _binding()
    context = _context()
    frozen = _frozen(binding=binding, context=context)
    assert type(frozen) is FrozenCurrentStartupContext
    document = json.loads(frozen.additional_context)
    assert document["schema"] == "zekam-codex-session-start-context/v1"
    assert document["grants_authority"] is False
    assert document["fragments"][0]["text"] == dict(context.fragments)["source-health"]
    assert frozen.output_digest == digest(frozen.additional_context)
    assert frozen.manifest_digest == digest(json.loads(frozen.manifest_body_json))
    assert frozen.success_stdout.endswith(b"\n")
    assert json.loads(frozen.success_stdout)["hookSpecificOutput"]["additionalContext"] == (
        frozen.additional_context
    )


def test_context_freeze_rejects_wrong_types_secret_and_surrogate() -> None:
    binding = _binding()
    context = _context()
    snapshot = CurrentSourceSnapshot(SNAPSHOT, "a" * 40, digest("source"))
    environment = digest("environment")
    hydration_key = "codex0151-session-start-018f0000-0000-7000-8000-000000000099"
    with pytest.raises(ValidationFailed):
        _validate_current_context_inputs(
            binding=binding,
            context=object(),  # type: ignore[arg-type]
            source_snapshot=snapshot,
            environment_evidence_digest=environment,
            hydration_key=hydration_key,
            observed_at=NOW,
        )
    with pytest.raises(ValidationFailed):
        _validate_current_context_inputs(
            binding=object(),  # type: ignore[arg-type]
            context=context,
            source_snapshot=snapshot,
            environment_evidence_digest=environment,
            hydration_key=hydration_key,
            observed_at=NOW,
        )
    with pytest.raises(PolicyViolation, match="concrete-source"):
        FrozenCurrentStartupContext()
    with pytest.raises((ValidationFailed, PolicyViolation)):
        _context("AKIA" + "A" * 16)
    with pytest.raises((ValidationFailed, PolicyViolation, UnicodeEncodeError)):
        _context("\ud800")


def test_public_fragment_and_stdout_helpers_reject_untyped_or_invalid_utf8() -> None:
    with pytest.raises(ValidationFailed, match="fragment text"):
        startup_fragment_budget_units(None)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed, match="additional context"):
        _session_start_success_stdout(None)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed, match="UTF-8"):
        _session_start_success_stdout("\ud800")


def test_frozen_context_deep_values_cannot_be_relabelled() -> None:
    context = _context()
    frozen = _frozen(context=context)
    with pytest.raises((AttributeError, TypeError)):
        frozen.manifest_digest = digest("forged")  # type: ignore[misc]
    assert canonical_json(json.loads(frozen.additional_context)) == frozen.additional_context


def test_frozen_context_revalidates_scope_bodies_digests_and_stdout() -> None:
    binding = _binding()
    frozen = _frozen(binding=binding)
    mutations: tuple[dict[str, object], ...] = (
        {"manifest_body_json": "{}"},
        {"manifest_digest": digest("forged-manifest")},
        {"hydration_body_json": "{}"},
        {"hydration_receipt_digest": digest("forged-hydration")},
        {"additional_context": canonical_json({"schema": "forged"})},
        {"output_digest": digest("forged-output")},
        {"success_stdout": b"{}\n"},
    )
    for mutation in mutations:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            _replace_frozen(frozen, **mutation)
    invalid_mutations: tuple[dict[str, object], ...] = (
        {"binding": object()},
        {"source_snapshot": object()},
        {"context": object()},
        {"manifest_body_json": None},
        {"hydration_body_json": None},
        {"additional_context": None},
        {"success_stdout": "{}\n"},
        {"success_stdout": b"{}"},
    )
    for mutation in invalid_mutations:
        with pytest.raises((PolicyViolation, ValidationFailed, AttributeError)):
            _replace_frozen(frozen, **mutation)
    foreign = replace(binding, project_id="018f0000-0000-7000-8000-000000000088")
    with pytest.raises(PolicyViolation, match="scope"):
        _frozen(binding=foreign, context=_context())


def test_additional_context_limit_rejects_without_truncating() -> None:
    assert STARTUP_CONTEXT_BUDGET_PROFILE == "utf8-bytes-minimum-one/v1"
    assert startup_fragment_budget_units("") == 1
    assert startup_fragment_budget_units("é") == 2
    exact = "x" * MAX_STARTUP_FRAGMENT_BUDGET_UNITS
    assert _frozen(text=exact).context.manifest.selected[0].token_count == 2048
    text = "x" * (MAX_STARTUP_FRAGMENT_BUDGET_UNITS + 1)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        _frozen(text=text)


def test_success_stdout_serializer_has_independent_exact_escape_bound() -> None:
    inner = '"' * MAX_ADDITIONAL_CONTEXT_UTF8_BYTES
    output = _session_start_success_stdout(inner)
    assert len(output) == MAX_SESSION_START_SUCCESS_STDOUT_UTF8_BYTES
    with pytest.raises(ValidationFailed, match="stdout"):
        _session_start_success_stdout(inner + "x")


@pytest.mark.parametrize(
    "source_ref",
    ("/tmp/secret", "../secret", "src/../secret", "./source", "x//y", "x\\y"),
)
def test_output_rejects_nonportable_source_references(source_ref: str) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _frozen(context=_context(source_ref=source_ref))


def test_output_requires_exact_git_style_source_revision() -> None:
    with pytest.raises(PolicyViolation, match="Git object"):
        _validate_current_context_inputs(
            binding=_binding(),
            context=_context(),
            source_snapshot=CurrentSourceSnapshot(SNAPSHOT, "z" * 40, digest("source")),
            environment_evidence_digest=digest("environment"),
            hydration_key="codex0151-session-start-018f0000-0000-7000-8000-000000000099",
            observed_at=NOW,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_snapshot", object()),
        ("environment_evidence_digest", None),
        ("hydration_key", "bad key"),
        ("observed_at", None),
        ("observed_at", "2026-09-03T12:00:00.1+00:00"),
    ),
)
def test_current_context_input_contract_rejects_untyped_authority_fields(
    field: str, value: object
) -> None:
    values: dict[str, object] = {
        "binding": _binding(),
        "context": _context(),
        "source_snapshot": CurrentSourceSnapshot(SNAPSHOT, "a" * 40, digest("source")),
        "environment_evidence_digest": digest("environment"),
        "hydration_key": "codex0151-session-start-018f0000-0000-7000-8000-000000000099",
        "observed_at": NOW,
    }
    values[field] = value
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _validate_current_context_inputs(**values)  # type: ignore[arg-type]


def test_current_context_rejects_snapshot_scope_drift() -> None:
    with pytest.raises(PolicyViolation, match="snapshot scope"):
        _validate_current_context_inputs(
            binding=_binding(),
            context=_context(),
            source_snapshot=CurrentSourceSnapshot(
                "018f0000-0000-7000-8000-000000000099", "a" * 40, digest("source")
            ),
            environment_evidence_digest=digest("environment"),
            hydration_key="codex0151-session-start-018f0000-0000-7000-8000-000000000099",
            observed_at=NOW,
        )


@pytest.mark.parametrize(
    "values",
    (
        (b"{}", None, None, None, None, False, False),
        (b"{}\n", "broken", None, None, None, False, False),
        (b"{}\n", None, None, None, None, 0, False),
        (b"{}\n", None, None, None, None, False, 0),
    ),
)
def test_session_start_result_requires_exact_stdout_digests_and_flags(
    values: tuple[object, ...],
) -> None:
    with pytest.raises(ValidationFailed):
        SessionStartIngressResult(*values)  # type: ignore[arg-type]


def test_akilli_health_fixture_is_read_only_and_bounded() -> None:
    path = Path("/Users/mkaracan/Projeler/akilli-kasa/src/akilli_kasa/api/saglik.py")
    before = path.read_bytes()
    assert 0 < len(before) < 2 * 1024 * 1024
    assert path.read_bytes() == before
