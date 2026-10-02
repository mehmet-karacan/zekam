"""D24/M18: etkin context olcumu geriye uyumlu, durust ve authority-free olmali."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from zekam.application.context_compiler import compile_context_plane_v2
from zekam.application.context_instruction_measurement import measure_instruction_load
from zekam.application.context_ranking import ContextRankingRequest
from zekam.domain.canonical import digest
from zekam.domain.client_integration import ClientIntegrationId
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
    ContextLoadLevel,
    ContextManifest,
    ContextSourceKind,
    OmittedReason,
    compile_context,
)
from zekam.domain.context_measurement import (
    EffectiveContextReport,
    EffectiveLoadEntry,
    InstructionSourceKind,
    MeasurementQuality,
    SizeMeasure,
    compare_reports,
    estimate_tokens,
    measure_text,
    measured_instruction_entry,
    report_from_manifest,
    unobservable_entry,
)
from zekam.domain.errors import BlockedContext, PolicyViolation, ValidationFailed

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 9, 24, tzinfo=dt.UTC)


def _candidate(
    candidate_id: str,
    *,
    tokens: int = 10,
    required: bool = False,
    profile: str = DEFAULT_TOKENIZER_PROFILE_DIGEST,
) -> ContextCandidate:
    value = candidate_id.ljust(tokens, "x")[:tokens]
    return ContextCandidate(
        candidate_id=candidate_id,
        authority=AuthorityLevel.VERIFIED,
        observed_at=NOW,
        source_revision="revision/current",
        content_digest=digest(value),
        token_count=len(value.encode("utf-8")),
        kind=ContextCandidateKind.KNOWLEDGE,
        source_ref=f"context/{candidate_id}",
        identity_refs=("entity/task",),
        scope_ref="work/current",
        applicable_roles=("builder",),
        task_terms=("java",),
        load_level=ContextLoadLevel.L2,
        source_kind=ContextSourceKind.KNOWLEDGE,
        required=required,
        tokenizer_profile_digest=profile,
    )


def _request() -> ContextRankingRequest:
    return ContextRankingRequest(
        role="builder",
        target_identity_refs=("entity/task",),
        step_scope_ref="step/current",
        work_scope_ref="work/current",
        project_scope_ref="project/current",
        realm_scope_ref="realm/current",
        current_source_revision="revision/current",
        compatible_source_revisions=("revision/parent",),
        task_terms=("java",),
        tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
    )


def _plane(candidates: tuple[ContextCandidate, ...], *, budget: int) -> ContextManifest:
    return compile_context_plane_v2(
        candidates,
        ranking_request=_request(),
        token_budget=budget,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        recipe_id="context-measure-test",
        recipe_digest=digest("recipe"),
        target_role="builder",
        contents={
            item.candidate_id: item.candidate_id.ljust(item.token_count, "x")[: item.token_count]
            for item in candidates
        },
        ranking_snapshot_digest=digest("ranking"),
        candidate_set_digest=digest("candidates"),
    )


def test_manifest_digest_and_selection_dict_are_unchanged_by_measurement_fields() -> None:
    manifest = _plane((_candidate("a", tokens=12),), budget=100)
    selection = manifest.selected[0]
    assert set(selection.as_dict()) == {
        "candidate_id",
        "content_digest",
        "token_count",
        "score",
        "reason",
        "kind",
        "source_ref",
        "source_revision",
        "candidate_digest",
        "authority",
        "reason_codes",
    }
    other = replace(selection, tokenizer_profile_digest=digest("baska-profil"))
    # Olcum alani manifest digest'ine girmez: eski kayitlar ve digest'ler degismez.
    assert other.as_dict() == selection.as_dict()
    assert replace(manifest, selected=(other,)).manifest_digest == manifest.manifest_digest


def test_disclosure_trace_keeps_old_keys_and_adds_measurement() -> None:
    selection = _plane((_candidate("a", tokens=12),), budget=100).selected[0]
    trace = selection.disclosure_trace()
    assert trace["load_level"] == "L2" and trace["authority"] is False
    measurement = trace["measurement"]
    assert measurement["quality"] == "known_loaded"
    assert measurement["source_kind"] == "knowledge"
    assert measurement["logical_ref"] == "context/a"
    assert measurement["content_digest"] == selection.content_digest
    assert measurement["load_reason"] == "context-plane-v2"
    assert measurement["load_level"] == "L2"
    assert measurement["bytes"] == 12
    assert measurement["token_method"] == "utf8-byte-count"
    assert measurement["provider_usage_tokens"] is None
    assert measurement["grants_authority"] is False


def test_v1_compiler_also_carries_tokenizer_profile_into_the_selection() -> None:
    profile = digest("provider-tokenizer")
    manifest = compile_context(
        (_candidate("a", tokens=9, profile=profile),),
        token_budget=100,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        recipe_id="r",
        recipe_digest=digest("r"),
        target_role="builder",
    )
    measurement = manifest.selected[0].measurement_trace()
    assert measurement["bytes"] is None  # profil byte sayimi degil: byte iddiasi yok
    assert measurement["token_method"] == "candidate-tokenizer-profile"
    assert measurement["token_method_version"] == profile


def test_report_from_manifest_is_known_loaded_but_never_claims_full_tokens() -> None:
    manifest = _plane((_candidate("a", tokens=12), _candidate("b", tokens=20)), budget=100)
    report = report_from_manifest(manifest, route="coordinator/builder")
    assert report.completeness == "observed"
    assert all(item.quality is MeasurementQuality.KNOWN_LOADED for item in report.entries)
    assert {item.role for item in report.entries} == {"builder"}
    assert report.totals()["bytes"] == 32
    # Provider usage verilmediyse tam etkin token sayisi yok.
    assert report.exact_effective_token_count is None
    assert report.as_dict()["full_effective_token_count_claimed"] is False
    with_usage = report_from_manifest(
        manifest, provider_usage_tokens=480, provider_usage_source="provider-response-usage"
    )
    assert with_usage.exact_effective_token_count == 480
    assert with_usage.totals()["token_estimate"] == 32  # tahmin, usage ile karismaz


def test_sizes_are_distinct_and_estimator_is_labelled_without_tokenizer() -> None:
    data = "çğüş".encode()  # 4 karakter, 8 byte
    size = measure_text(data)
    assert (size.bytes_count, size.char_count) == (8, 4)
    assert size.token_estimate == estimate_tokens(8) == 2
    assert (size.token_method, size.token_method_version) == ("utf8-bytes-div-4", "1")
    with pytest.raises(ValidationFailed, match="yontem ve surum"):
        SizeMeasure(token_estimate=3)


def test_unobservable_makes_no_size_claim_and_partial_report_has_no_exact_tokens() -> None:
    hidden = unobservable_entry(
        source_kind=InstructionSourceKind.CLIENT_SYSTEM_PROMPT,
        logical_ref="client/claude-code/system-prompt",
        load_reason="client-internal",
    )
    with pytest.raises(ValidationFailed, match="Gorulemeyen"):
        EffectiveLoadEntry(
            source_kind="client-system-prompt",
            logical_ref="client/x/system-prompt",
            quality=MeasurementQuality.UNOBSERVABLE,
            load_reason="client-internal",
            load_level="L0",
            size=SizeMeasure(bytes_count=10),
        )
    seen = measured_instruction_entry(
        source_kind=InstructionSourceKind.INSTRUCTION_GLOBAL,
        logical_ref="client/claude-code/global-instructions",
        data=b"x" * 40,
        load_reason="client-auto-load",
    )
    assert seen.quality is MeasurementQuality.DISCOVERED_ONLY
    report = EffectiveContextReport((seen, hidden), 900, "provider-response-usage")
    assert report.completeness == "partial"
    assert report.exact_effective_token_count is None
    assert report.totals()["unobservable_count"] == 1
    with pytest.raises(PolicyViolation):
        EffectiveContextReport((seen,), grants_authority=True)


def test_estimated_quality_requires_labelled_estimate() -> None:
    with pytest.raises(ValidationFailed):
        EffectiveLoadEntry(
            source_kind="skill",
            logical_ref="skill/x",
            quality=MeasurementQuality.ESTIMATED,
            load_reason="skill-body",
            load_level="L1",
            size=SizeMeasure(bytes_count=100),
        )
    entry = EffectiveLoadEntry(
        source_kind="skill",
        logical_ref="skill/x",
        quality=MeasurementQuality.ESTIMATED,
        load_reason="skill-body",
        load_level="L1",
        size=SizeMeasure(
            token_estimate=25, token_method="utf8-bytes-div-4", token_method_version="1"
        ),
    )
    assert EffectiveContextReport((entry,)).completeness == "partial"


def test_global_shrink_with_unchanged_imports_is_visible(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    shared = home / ".claude" / "shared-rules.md"
    shared.write_text("ortak kural " * 200, encoding="utf-8")
    global_file = home / ".claude" / "CLAUDE.md"
    global_file.write_text(
        "# Uzun global\n" + "satir\n" * 300 + "@shared-rules.md\n", encoding="utf-8"
    )
    before = measure_instruction_load(
        user_home=home, client=ClientIntegrationId.CLAUDE_CODE, role="coordinator"
    )
    global_file.write_text("# Kisa global\n@shared-rules.md\n", encoding="utf-8")
    after = measure_instruction_load(
        user_home=home, client=ClientIntegrationId.CLAUDE_CODE, role="coordinator"
    )

    diff = compare_reports(before, after)
    assert diff["by_source_kind"]["instruction-global"]["bytes"] < 0
    assert diff["by_source_kind"]["instruction-import"]["bytes"] == 0
    assert diff["by_source_kind"]["instruction-import"]["entries"] == 0
    assert diff["total_bytes_delta"] == diff["by_source_kind"]["instruction-global"]["bytes"]
    kinds = {item.source_kind: item.quality for item in after.entries}
    assert kinds["instruction-import"] is MeasurementQuality.DISCOVERED_ONLY
    assert kinds["client-system-prompt"] is MeasurementQuality.UNOBSERVABLE
    assert kinds["client-tool-schema"] is MeasurementQuality.UNOBSERVABLE
    assert after.exact_effective_token_count is None
    # Absolute path veya dosya icerigi rapora sizmaz.
    rendered = json.dumps(after.as_dict(), ensure_ascii=False)
    assert str(tmp_path) not in rendered and "ortak kural" not in rendered


def test_imports_outside_root_symlink_and_fenced_code_are_not_followed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("disarida", encoding="utf-8")
    inside = home / ".claude" / "inside.md"
    inside.write_text("iceride", encoding="utf-8")
    (home / ".claude" / "CLAUDE.md").write_text(
        "@../../outside.md\n@inside.md\n```\n@fenced.md\n```\n@yok.md\n", encoding="utf-8"
    )
    (home / ".claude" / "fenced.md").write_text("kod blogu", encoding="utf-8")

    report = measure_instruction_load(user_home=home, client=ClientIntegrationId.CLAUDE_CODE)
    refs = {item.logical_ref for item in report.entries}
    assert "home/.claude/inside.md" in refs
    assert not any("outside" in ref or "fenced" in ref or "yok" in ref for ref in refs)


def test_m18_required_context_that_does_not_fit_blocks_instead_of_truncating() -> None:
    required = _candidate("safety-boundary", tokens=60, required=True)
    optional = _candidate("nice-to-have", tokens=10)
    with pytest.raises(BlockedContext) as plane:
        _plane((required, optional), budget=40)
    assert plane.value.reason == "required-context-does-not-fit"
    assert isinstance(plane.value, PolicyViolation)  # geriye uyum
    with pytest.raises(BlockedContext):
        compile_context(
            (required, optional),
            token_budget=40,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
            recipe_id="r",
            recipe_digest=digest("r"),
            target_role="builder",
        )


def test_m18_low_budget_drops_only_optional_and_keeps_required_whole() -> None:
    required = _candidate("safety-boundary", tokens=30, required=True)
    optional = _candidate("nice-to-have", tokens=30)
    manifest = _plane((required, optional), budget=40)
    assert [item.candidate_id for item in manifest.selected] == ["safety-boundary"]
    assert manifest.selected[0].token_count == 30  # kesilmedi
    assert [(o.candidate_id, o.reason) for o in manifest.omitted] == [
        ("nice-to-have", OmittedReason.BUDGET)
    ]
    report = report_from_manifest(manifest)
    assert [item.logical_ref for item in report.entries] == ["context/safety-boundary"]
