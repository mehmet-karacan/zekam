from __future__ import annotations

import copy
import datetime as dt
import json
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from zekam.application import project_knowledge_index as knowledge
from zekam.application.continuity_projection import (
    HydrationCategory,
    HydrationItem,
    HydrationOmission,
    HydrationRecipe,
    ProjectionAudience,
    ProjectionGenerationReceipt,
    ProjectionReleaseSnapshot,
    build_hydration_recipe,
)
from zekam.application.memory_upgrade import (
    MemoryUpgradePlan,
    MemoryUpgradeSnapshot,
    MemoryVerificationEvidence,
    UpgradeTarget,
    canonical_projection_source_digest,
)
from zekam.application.model_capability_live import (
    EMPTY_CONTINUITY_STATE,
    TURN_SCHEMA,
    CapabilityTurnFailureCode,
    CapabilityTurnValidationFailed,
    PreparedCapabilityLiveManifest,
    PreparedCapabilitySlot,
    _parse_turn,
    _semantic_acceptance_ids,
    capability_derivation_material,
    capability_request_template_material,
    derive_capability_request_body,
    execute_capability_episode,
    validate_continuity_state,
)
from zekam.application.opencode_embedding import (
    OpenCodeEmbeddingConfiguration,
    _cosine,
    _endpoint,
    _mapping,
    _optional_positive_integer,
    _secure_json_document,
    _strict_fields,
    evaluate_opencode_embedding_response,
)
from zekam.application.provider_contract_execution import (
    ProviderCallPlan,
    ProviderContractFixtures,
    load_provider_contract_fixtures,
    review_whisper_audio_fixture,
)
from zekam.application.resume_apply_service import ResumeApplyService
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.clients import ClientDescriptor, ClientKind
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.model_inventory import Modality
from zekam.domain.resume import ResumeAction, ResumeDisposition, ResumePlan, RuntimeObservation
from zekam.domain.resume_apply import ResumeApplyRequest
from zekam.domain.session_continuity import DataClassification, DigestReference, TruthClass

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 20))


def _snapshot(**changes: object) -> MemoryUpgradeSnapshot:
    values: dict[str, object] = {
        "migration_current": True,
        "migration_head": 56,
        "target_migration": 56,
        "component_present": False,
        "component_revision": None,
        "mode": None,
        "policy_digest": None,
        "projection_receipt_count": 0,
        "latest_projection_receipt_digest": None,
        "latest_projection_digest": None,
        "legacy_projection_count": 0,
        "required_hook_invalid_count": 0,
        "current_hook_set_digest": digest("hooks"),
        "project_id": None,
        "work_item_id": None,
        "source_head": None,
        "source_tree_digest": None,
        "database_revision_digest": None,
        "projection_source_digest": None,
        "projection_current": False,
        "detected_at": NOW,
    }
    values.update(changes)
    return MemoryUpgradeSnapshot(**cast(Any, values))


def _verification(snapshot: MemoryUpgradeSnapshot, **changes: object) -> MemoryVerificationEvidence:
    values: dict[str, object] = {
        "verified_snapshot_digest": snapshot.snapshot_digest,
        "fresh_database_digest": digest("fresh"),
        "upgrade_database_digest": digest("upgrade"),
        "hook_digest": digest("hook"),
        "security_digest": digest("security"),
        "continuity_digest": digest("continuity"),
        "projection_digest": digest("projection"),
        "full_suite_digest": digest("suite"),
        "verifier_model": "verifier-model",
        "verifier_execution_identity": "verifier-process",
        "builder_model": "builder-model",
        "builder_execution_identity": "builder-process",
        "verified_at": NOW,
        "passed": True,
    }
    values.update(changes)
    return MemoryVerificationEvidence(**cast(Any, values))


def test_memory_snapshot_exact_source_and_component_guards() -> None:
    baseline = _snapshot()
    assert (
        baseline.snapshot_digest
        == replace(baseline, detected_at=NOW + dt.timedelta(seconds=1)).snapshot_digest
    )
    source_tree = digest("tree")
    database = digest("database")
    source = canonical_projection_source_digest(
        source_head="abc",
        source_tree_digest=source_tree,
        migration_head=56,
        database_revision_digest=database,
    )
    current = _snapshot(
        project_id=IDS[0],
        work_item_id=IDS[1],
        source_head="abc",
        source_tree_digest=source_tree,
        database_revision_digest=database,
        projection_source_digest=source,
        projection_current=True,
    )
    assert current.body()["projection_current"] is True

    invalid = (
        {"migration_head": -1},
        {"target_migration": 0},
        {"required_hook_invalid_count": -1},
        {"detected_at": NOW.replace(tzinfo=None)},
        {"component_present": True},
        {"project_id": IDS[0]},
        {"source_head": "abc"},
        {"projection_current": True},
        {"current_hook_set_digest": None},
        {"grants_authority": True},
    )
    for changes in invalid:
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _snapshot(**changes)
    with pytest.raises(ValidationFailed):
        canonical_projection_source_digest(
            source_head=" ",
            source_tree_digest=source_tree,
            migration_head=56,
            database_revision_digest=database,
        )


def test_memory_verifier_and_upgrade_plan_are_independent_and_replay_stable() -> None:
    snapshot = _snapshot()
    verification = _verification(snapshot)
    assert verification.verification_digest.startswith("sha256:")
    assert verification.verifier_identity_digest.startswith("sha256:")
    for verification_changes in (
        {"verifier_model": " "},
        {"verifier_model": "builder-model"},
        {"verifier_execution_identity": "builder-process"},
        {"verified_at": NOW.replace(tzinfo=None)},
        {"grants_authority": True},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _verification(snapshot, **verification_changes)

    shadow_source = digest("source")
    shadow = _snapshot(
        project_id=IDS[0],
        work_item_id=IDS[1],
        source_head="head",
        source_tree_digest=digest("tree"),
        database_revision_digest=digest("db"),
        projection_source_digest=shadow_source,
    )
    plan = MemoryUpgradePlan.create(
        snapshot=shadow,
        target=UpgradeTarget.SHADOW,
        rollback_ref="rollback/memory.json",
        rollback_digest=digest("rollback"),
    )
    plan.assert_integrity()
    assert plan.body()["requires_confirmation"] is True
    with pytest.raises(PolicyViolation, match="digest mismatch"):
        replace(plan, plan_digest=digest("wrong")).assert_integrity()
    for rollback_ref in ("", "/absolute", "../escape", "bad\\path"):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            MemoryUpgradePlan.create(
                snapshot=shadow,
                target=UpgradeTarget.SHADOW,
                rollback_ref=rollback_ref,
                rollback_digest=digest("rollback"),
            )
    with pytest.raises(PolicyViolation, match="passing independent"):
        MemoryUpgradePlan.create(
            snapshot=snapshot,
            target=UpgradeTarget.ENFORCED,
            rollback_ref="rollback.json",
            rollback_digest=digest("rollback"),
        )
    with pytest.raises(PolicyViolation, match="package digest"):
        MemoryUpgradePlan.create(
            snapshot=snapshot,
            target=UpgradeTarget.STAMPED,
            rollback_ref="rollback.json",
            rollback_digest=digest("rollback"),
            verification=verification,
        )


def _release(**changes: object) -> ProjectionReleaseSnapshot:
    work_record = digest("work")
    values: dict[str, object] = {
        "project_id": IDS[0],
        "work_item_id": IDS[1],
        "work_revision": 2,
        "work_state": "active",
        "work_record_digest": work_record,
        "source_head": "head",
        "source_tree_digest": digest("tree"),
        "migration_head": 56,
        "database_revision_digest": digest(
            {
                "project_id": str(IDS[0]),
                "work_item_id": str(IDS[1]),
                "work_revision": 2,
                "work_state": "active",
                "work_record_digest": work_record,
            }
        ),
        "projection_ref": "projection/active-work",
        "projection_receipt_digest": digest("receipt"),
        "projection_digest": digest("projection"),
        "projection_source_digest": "",
        "lifecycle_complete": True,
        "pending_lifecycle_steps": (),
        "next_safe_action": "continue",
    }
    values.update(changes)
    if not values["projection_source_digest"]:
        values["projection_source_digest"] = canonical_projection_source_digest(
            source_head=str(values["source_head"]),
            source_tree_digest=str(values["source_tree_digest"]),
            migration_head=int(cast(int, values["migration_head"])),
            database_revision_digest=str(values["database_revision_digest"]),
        )
    return ProjectionReleaseSnapshot(**cast(Any, values))


def test_projection_receipt_release_and_hydration_fail_closed() -> None:
    receipt = ProjectionGenerationReceipt(
        "project",
        digest("source"),
        "head",
        "56",
        digest("db"),
        digest("projection"),
        1,
        0,
        True,
        ProjectionAudience.PUBLIC,
        digest("privacy"),
    )
    assert receipt.receipt_digest.startswith("sha256:")
    for receipt_changes in (
        {"migration_head": "x"},
        {"record_count": 0},
        {"excluded_by_classification": -1},
        {"read_only": False},
        {"grants_authority": True},
        {"audience": "public"},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            replace(receipt, **receipt_changes)

    release = _release()
    release.assert_release_ready(expected_source_digest=release.expected_projection_source_digest)
    assert release.snapshot_digest.startswith("sha256:")
    for release_changes in (
        {"work_revision": 0},
        {"projection_ref": "other"},
        {"pending_lifecycle_steps": ("same", "same")},
        {"pending_lifecycle_steps": ("",)},
        {"next_safe_action": " "},
        {"work_state": "completed"},
        {"grants_authority": True},
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            _release(**release_changes)
    with pytest.raises(PolicyViolation, match="receipt zinciri"):
        replace(release, lifecycle_complete=False).assert_release_ready(
            expected_source_digest=release.expected_projection_source_digest
        )

    item = HydrationItem(
        "work",
        HydrationCategory.ACTIVE_WORK,
        "cas:work",
        DigestReference("context:work", digest("work"), TruthClass.REPO_FACT),
        DataClassification.INTERNAL,
        3,
    )
    assert item.priority.value == "must-load"
    for changes in (
        {"item_id": " "},
        {"content_ref": "/absolute"},
        {"content_ref": "bad\\ref"},
        {"token_cost": 0},
        {"category": "bad"},
    ):
        with pytest.raises(ValidationFailed):
            replace(item, **changes)
    for bad in (
        {"token_budget": 0},
        {"tokens_used": 4},
        {"required_complete": False},
        {"grants_authority": True},
    ):
        values = {
            "selected": (item,),
            "omissions": (),
            "token_budget": 3,
            "tokens_used": 3,
            "required_complete": True,
            "grants_authority": False,
        }
        values.update(bad)
        with pytest.raises((ValidationFailed, PolicyViolation)):
            HydrationRecipe(**cast(Any, values))
    with pytest.raises(ValidationFailed, match="birden fazla"):
        HydrationRecipe((item,), (HydrationOmission("work", "duplicate"),), 3, 3, True)
    with pytest.raises(ValidationFailed, match="tekil"):
        build_hydration_recipe((item, item), token_budget=10)
    with pytest.raises(ValidationFailed, match="bos olamaz"):
        build_hydration_recipe((item,), token_budget=10, allowed_classifications=frozenset())


def test_projection_optional_selection_and_source_drift_branches() -> None:
    with pytest.raises(ValidationFailed, match="kimligi bos"):
        ProjectionGenerationReceipt(
            " ",
            digest("source"),
            "head",
            "56",
            digest("db"),
            digest("projection"),
            1,
            0,
            True,
            ProjectionAudience.PUBLIC,
            digest("privacy"),
        )
    release = _release()
    with pytest.raises(PolicyViolation, match="snapshot stale"):
        release.assert_release_ready(expected_source_digest=digest("other"))
    with pytest.raises(ValidationFailed, match="next-safe-action"):
        _release(next_safe_action=None)
    with pytest.raises(ValidationFailed, match="identity alanlari"):
        _release(source_head=" ", projection_source_digest=digest("source"))
    with pytest.raises(ValidationFailed, match="token budget"):
        build_hydration_recipe((), token_budget=0)

    def hydration_item(
        item_id: str,
        category: HydrationCategory,
        classification: DataClassification,
        token_cost: int,
        *,
        relevant: bool = True,
    ) -> HydrationItem:
        return HydrationItem(
            item_id,
            category,
            f"cas:{item_id}",
            DigestReference(f"context:{item_id}", digest(item_id), TruthClass.REPO_FACT),
            classification,
            token_cost,
            relevant=relevant,
        )

    required = hydration_item(
        "required", HydrationCategory.ACTIVE_WORK, DataClassification.INTERNAL, 3
    )
    private = hydration_item(
        "private", HydrationCategory.HUMAN_DECISION, DataClassification.CONFIDENTIAL, 1
    )
    never = hydration_item("never", HydrationCategory.SECRET, DataClassification.INTERNAL, 1)
    on_demand = hydration_item(
        "demand", HydrationCategory.KNOWLEDGE_ARTICLE, DataClassification.INTERNAL, 1
    )
    irrelevant = hydration_item(
        "irrelevant",
        HydrationCategory.HUMAN_DECISION,
        DataClassification.INTERNAL,
        1,
        relevant=False,
    )
    too_large = hydration_item(
        "too-large", HydrationCategory.HUMAN_DECISION, DataClassification.INTERNAL, 4
    )
    recipe = build_hydration_recipe(
        (required, private, never, on_demand, irrelevant, too_large), token_budget=3
    )
    assert {row.reason_code for row in recipe.omissions} == {
        "classification-excluded",
        "never-auto-load",
        "retrieve-on-demand",
        "not-relevant",
        "optional-budget-exhausted",
    }
    with pytest.raises(PolicyViolation, match="classification policy"):
        build_hydration_recipe(
            (required,),
            token_budget=3,
            allowed_classifications=frozenset({DataClassification.PUBLIC}),
        )
    with pytest.raises(PolicyViolation, match="butceye sigmiyor"):
        build_hydration_recipe((required,), token_budget=2)
    with pytest.raises(ValidationFailed, match="registry disinda"):
        build_hydration_recipe(
            (required,), token_budget=3, allowed_classifications=cast(Any, frozenset({"internal"}))
        )


def test_project_index_text_and_feature_boundaries(tmp_path: Path) -> None:
    vector = knowledge.feature_hash_baseline_vector("Hello, world!", dimensions=16)
    assert len(vector) == 16
    assert abs(sum(value * value for value in vector) - 1.0) < 1e-9
    punctuation = knowledge.feature_hash_baseline_vector("!!!", dimensions=8)
    assert len(punctuation) == 8
    for value, dimensions in (("", 8), ("text", 0)):
        with pytest.raises(ValidationFailed):
            knowledge.feature_hash_baseline_vector(value, dimensions=dimensions)
    assert knowledge._is_supported("src/main.py") is True
    assert knowledge._is_supported("Dockerfile") is True
    assert knowledge._is_supported("image.png") is False
    assert knowledge._file_units("empty.py", "  ", 0) == ()
    units = knowledge._file_units("large.py", "x" * (knowledge.MAX_CHUNK_CHARACTERS + 5), 3)
    assert len(units) == 2 and units[0].order == 3

    binary = tmp_path / "binary.py"
    binary.write_bytes(b"\xff\xfe")
    assert knowledge._verified_text(tmp_path, "binary.py", digest_of_bytes(b"\xff\xfe")) is None
    target = tmp_path / "outside.py"
    target.write_text("safe", encoding="utf-8")
    alias = tmp_path / "alias.py"
    alias.symlink_to(target)
    with pytest.raises(PolicyViolation, match="symlink"):
        knowledge._verified_text(tmp_path, "alias.py", digest_of_bytes(b"safe"))


def test_opencode_secure_json_endpoint_and_integer_validation(tmp_path: Path) -> None:
    valid = tmp_path / "config.json"
    valid.write_text('{"provider":{}}', encoding="utf-8")
    assert _secure_json_document(valid.resolve(), max_bytes=100) == {"provider": {}}
    with pytest.raises(ValidationFailed):
        _secure_json_document(valid.resolve(), max_bytes=0)
    with pytest.raises(ConfigurationError, match="absolute"):
        _secure_json_document(Path("relative.json"), max_bytes=100)
    with pytest.raises(ConfigurationError, match="bulunamadi"):
        _secure_json_document((tmp_path / "missing").resolve(), max_bytes=100)
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a":1,"a":2}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="duplicate"):
        _secure_json_document(duplicate.resolve(), max_bytes=100)
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="object"):
        _secure_json_document(array.resolve(), max_bytes=100)
    alias = tmp_path / "alias.json"
    alias.symlink_to(valid)
    with pytest.raises(ConfigurationError, match="link/reparse"):
        _secure_json_document(alias.absolute(), max_bytes=100)

    identity, endpoint = _endpoint("https://example.com:8443/v1")
    assert identity.port == 8443 and endpoint == "https://example.com:8443/v1/embeddings"
    ipv6, ipv6_endpoint = _endpoint("https://[::1]/")
    assert ipv6.host == "::1" and ipv6_endpoint == "https://[::1]/embeddings"
    for raw in ("", " https://example.com", "https://example.com:bad"):
        with pytest.raises((ConfigurationError, ValidationFailed)):
            _endpoint(raw)
    with pytest.raises(ConfigurationError, match="normalize"):
        _endpoint("https://example.com/a/../b")
    for value in (0, -1, True, "2"):
        with pytest.raises(ConfigurationError):
            _optional_positive_integer({"timeout": value}, "timeout")


def test_opencode_low_level_document_and_shape_guards(tmp_path: Path) -> None:
    short = tmp_path / "short.json"
    short.write_bytes(b"{")
    with pytest.raises(ConfigurationError, match="boyutu"):
        _secure_json_document(short.resolve(), max_bytes=10)
    invalid_utf8 = tmp_path / "utf8.json"
    invalid_utf8.write_bytes(b'{"x":"\xff"}')
    with pytest.raises(ConfigurationError, match="UTF-8 JSON"):
        _secure_json_document(invalid_utf8.resolve(), max_bytes=20)
    with pytest.raises(ConfigurationError, match="JSON object"):
        _mapping([], label="provider")
    with pytest.raises(ConfigurationError, match="bilinmeyen"):
        _strict_fields({"extra": 1}, frozenset({"known"}), label="provider")
    identity, endpoint = _endpoint("http://localhost/")
    assert identity.port == 80 and endpoint == "http://localhost/embeddings"


def _embedding_config() -> OpenCodeEmbeddingConfiguration:
    identity, endpoint = _endpoint("https://models.example.test/v1")
    return OpenCodeEmbeddingConfiguration(
        "provider", identity, ("embed",), "embed", "canonical-embed", "API_KEY", endpoint
    )


def _embedding_response(vectors: list[list[float]]) -> dict[str, object]:
    return {"data": [{"index": i, "embedding": row} for i, row in enumerate(vectors)]}


def test_embedding_metrics_dimension_nan_order_and_zero_norm() -> None:
    config = _embedding_config()
    vectors = [
        [1.0, 0.0],
        [1.0, 0.0],
        [0.9, 0.1],
        [-1.0, 0.0],
        [0.8, 0.2],
        [-0.8, 0.2],
    ]
    metrics = evaluate_opencode_embedding_response(
        config, _embedding_response(vectors), latency_ms=5
    )
    assert metrics.verified is True
    assert metrics.sanitized()["dimension"] == 2
    for response, latency in (
        (_embedding_response(vectors), -1),
        (_embedding_response(vectors[:3]), 1),
        (_embedding_response([[1.0], [1.0, 2.0], [1.0], [1.0]]), 1),
        (_embedding_response([[float("nan")], [1.0], [1.0], [1.0]]), 1),
    ):
        with pytest.raises(ValidationFailed):
            evaluate_opencode_embedding_response(config, response, latency_ms=latency)
    with pytest.raises(ValidationFailed, match="sifir norm"):
        _cosine((0.0, 0.0), (1.0, 0.0))


def test_provider_fixture_and_call_plan_exact_contracts() -> None:
    fixtures = load_provider_contract_fixtures()
    assert fixtures.fixture_digest.startswith("sha256:")
    original = copy.deepcopy(dict(fixtures.document))
    mutations: tuple[dict[str, object], ...] = (
        {"schema": "wrong"},
        {"data_classification": "secret"},
        {"fixtures": []},
    )
    for changes in mutations:
        candidate = copy.deepcopy(original)
        candidate.update(changes)
        with pytest.raises(ValidationFailed):
            ProviderContractFixtures(candidate)

    plan = ProviderCallPlan(
        "call",
        Modality.CHAT,
        "model",
        "provider",
        "endpoint",
        "chat",
        "secret",
        "json",
        digest("fixture"),
        None,
        None,
        "/chat",
    )
    assert plan.runtime_bound is False
    assert plan.authorization_scope()["effect_digest"] is None
    with pytest.raises(ValidationFailed):
        _ = plan.effect_action
    with pytest.raises(ValidationFailed):
        _ = plan.effect_request
    bound = replace(plan, payload_digest=digest("payload"), endpoint_binding_digest=digest("ep"))
    assert bound.runtime_bound is True
    assert bound.effect_request.touches_external_system is True
    assert bound.as_dict()["authorization_scope"]["max_uses"] == 1


def test_provider_fixture_schema_and_audio_review_are_fail_closed(tmp_path: Path) -> None:
    fixtures = load_provider_contract_fixtures()
    original = copy.deepcopy(dict(fixtures.document))
    variants: list[dict[str, object]] = []
    for mutate in ("audio", "chat-keys", "chat-types", "embedding", "guardrail"):
        candidate = copy.deepcopy(original)
        rows = cast(dict[str, Any], candidate["fixtures"])
        if mutate == "audio":
            rows[Modality.AUDIO_TRANSCRIPTION.value]["audio_env"] = ""
        elif mutate == "chat-keys":
            rows[Modality.CHAT.value]["required_json_keys"] = []
        elif mutate == "chat-types":
            rows[Modality.CHAT.value]["required_json_types"] = {"answer": "integer"}
        elif mutate == "embedding":
            rows[Modality.EMBEDDING.value]["repetitions"] = 1
        else:
            rows[Modality.GUARDRAIL.value]["samples"] = [{"unsafe": False}]
        variants.append(candidate)
    for candidate in variants:
        with pytest.raises(ValidationFailed):
            ProviderContractFixtures(candidate)

    not_object = tmp_path / "fixtures.yaml"
    not_object.write_text("- item\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="object"):
        load_provider_contract_fixtures(not_object)
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("[unterminated", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="okunamadi"):
        load_provider_contract_fixtures(malformed)

    audio_fixture = dict(fixtures.for_modality(Modality.AUDIO_TRANSCRIPTION))
    locator = str(audio_fixture["audio_env"])
    with pytest.raises(ConfigurationError, match="locator"):
        review_whisper_audio_fixture(audio_fixture, {})
    with pytest.raises(ConfigurationError, match="absolute"):
        review_whisper_audio_fixture(audio_fixture, {locator: "relative.wav"})
    tiny = tmp_path / "tiny.wav"
    tiny.write_bytes(b"RIFF")
    with pytest.raises(ConfigurationError, match="boyutu"):
        review_whisper_audio_fixture(
            audio_fixture, {locator: str(tiny)}, allowed_root_override=tmp_path
        )

    valid = tmp_path / "valid.wav"
    with wave.open(str(valid), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00")
    reviewed = review_whisper_audio_fixture(
        audio_fixture, {locator: str(valid)}, allowed_root_override=tmp_path
    )
    assert reviewed.size_bytes == valid.stat().st_size
    assert reviewed.content_digest == digest_of_bytes(valid.read_bytes())

    invalid_riff = tmp_path / "invalid.wav"
    invalid_riff.write_bytes(b"x" * 44)
    with pytest.raises(ConfigurationError, match="RIFF/WAVE"):
        review_whisper_audio_fixture(
            audio_fixture, {locator: str(invalid_riff)}, allowed_root_override=tmp_path
        )


def _slot() -> PreparedCapabilitySlot:
    template = capability_request_template_material(
        backend_model="model", system_prompt="system", prompt_prefix="prefix", output_cap=100
    )
    return PreparedCapabilitySlot(
        "model",
        digest("task"),
        1,
        "discover",
        cast(Any, object()),
        "prefix",
        "system",
        "model",
        100,
        ("checkpoint",),
        digest(template),
        digest("derive"),
        template,
    )


def _turn(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": TURN_SCHEMA,
        "phase": "discover",
        "progress": 25,
        "checkpoint": "checkpoint",
        "evidence": ["bounded evidence"],
        "revision": {"changed": False, "summary": ""},
        "continuity_state": dict(EMPTY_CONTINUITY_STATE),
        "artifact": "artifact",
    }
    body.update(changes)
    return body


def test_capability_request_state_and_turn_strictness() -> None:
    slot = _slot()
    payload = derive_capability_request_body(slot.template_material, EMPTY_CONTINUITY_STATE)
    assert payload["model"] == "model"
    assert (
        validate_continuity_state(EMPTY_CONTINUITY_STATE)["next_action"]
        == "inspect the supplied case"
    )
    parsed, state = _parse_turn(json.dumps(_turn()), slot)
    assert parsed["progress"] == 25 and state["next_action"] == "inspect the supplied case"
    fenced, _ = _parse_turn("```json\n" + json.dumps(_turn()) + "\n```", slot)
    assert fenced == parsed

    invalid_turns = (
        ("```\n{}\n```", CapabilityTurnFailureCode.INVALID_JSON_ENVELOPE),
        ("not-json", CapabilityTurnFailureCode.INVALID_JSON_ENVELOPE),
        (json.dumps({}), CapabilityTurnFailureCode.INVALID_SHAPE),
        (json.dumps(_turn(schema="wrong")), CapabilityTurnFailureCode.INVALID_BINDING),
        (json.dumps(_turn(progress=True)), CapabilityTurnFailureCode.INVALID_PROGRESS),
        (json.dumps(_turn(checkpoint="other")), CapabilityTurnFailureCode.INVALID_BINDING),
    )
    for raw, code in invalid_turns:
        with pytest.raises(CapabilityTurnValidationFailed) as captured:
            _parse_turn(raw, slot)
        assert captured.value.failure_code is code
    for invalid_state in (
        {},
        {"facts": [], "open_questions": [], "risks": [], "next_action": "x", "extra": 1},
        {"facts": "bad", "open_questions": [], "risks": [], "next_action": "x"},
    ):
        with pytest.raises(ValidationFailed):
            validate_continuity_state(invalid_state)
    with pytest.raises(ValidationFailed, match="metni eksik"):
        capability_request_template_material(
            backend_model="", system_prompt="system", prompt_prefix="prefix", output_cap=1
        )
    with pytest.raises(ValidationFailed, match="token butcesi"):
        capability_request_template_material(
            backend_model="model", system_prompt="system", prompt_prefix="prefix", output_cap=0
        )
    with pytest.raises(PolicyViolation, match="slot seti"):
        PreparedCapabilityLiveManifest(digest("plan"), (slot, slot), "secret", {})


def test_capability_turn_bounded_fields_and_derivation_drift() -> None:
    slot = _slot()
    invalid_turns: tuple[tuple[dict[str, object], type[Exception]], ...] = (
        (_turn(evidence=[]), ValidationFailed),
        (_turn(evidence=["x" * 513]), CapabilityTurnValidationFailed),
        (_turn(revision=[]), ValidationFailed),
        (_turn(revision={"changed": True, "summary": ""}), CapabilityTurnValidationFailed),
        (_turn(artifact="x" * 8193), CapabilityTurnValidationFailed),
        (
            _turn(
                continuity_state={
                    "facts": ["x"] * 7,
                    "open_questions": [],
                    "risks": [],
                    "next_action": "next",
                }
            ),
            ValidationFailed,
        ),
    )
    for body, exception in invalid_turns:
        with pytest.raises(exception):
            _parse_turn(json.dumps(body), slot)

    changed_template = dict(slot.template_material)
    changed_template["model"] = "other"
    with pytest.raises(PolicyViolation, match="material drift"):
        capability_derivation_material(
            replace(slot, template_material=changed_template), EMPTY_CONTINUITY_STATE
        )
    for template in (
        {"schema": "wrong", "model": "m", "system": "s", "prompt_prefix": "p", "max_tokens": 1},
        {
            "schema": "zekam-capability-request-template/v1",
            "model": "m",
            "system": "s",
            "prompt_prefix": "p",
        },
        {
            "schema": "zekam-capability-request-template/v1",
            "model": "m",
            "system": "s",
            "prompt_prefix": "p",
            "max_tokens": True,
        },
    ):
        with pytest.raises(ValidationFailed):
            derive_capability_request_body(template, EMPTY_CONTINUITY_STATE)

    oversized_state = {
        "facts": ["x" * 240] * 6,
        "open_questions": ["y" * 240] * 6,
        "risks": ["z" * 240] * 6,
        "next_action": "n" * 240,
    }
    with pytest.raises(ValidationFailed, match="byte butcesini"):
        validate_continuity_state(oversized_state)


def test_capability_semantic_checks_and_episode_slot_binding() -> None:
    for checks in (None, ["bad"], [{"id": 1, "any_of": []}]):
        fixture = cast(Any, SimpleNamespace(payload={"hidden_acceptance_checks": checks}))
        with pytest.raises(PolicyViolation):
            _semantic_acceptance_ids(fixture, ("text",))

    task_digest = digest("task")
    task = cast(Any, SimpleNamespace(task_digest=task_digest))
    with pytest.raises(PolicyViolation, match="eight ordered"):
        execute_capability_episode(
            plan=cast(Any, object()),
            task=task,
            fixture=cast(Any, object()),
            model_id="model",
            slots=(),
            invoke=cast(Any, object()),
            verifier=cast(Any, object()),
        )
    slots = tuple(
        replace(_slot(), turn_index=index, phase=f"phase-{index}", task_digest=task_digest)
        for index in range(1, 9)
    )
    with pytest.raises(PolicyViolation, match="model/task binding"):
        execute_capability_episode(
            plan=cast(Any, object()),
            task=task,
            fixture=cast(Any, object()),
            model_id="other",
            slots=slots,
            invoke=cast(Any, object()),
            verifier=cast(Any, object()),
        )


class _Adapter:
    descriptor = ClientDescriptor(
        ClientKind.CODEX, "codex", "codex", frozenset({"structured-result"})
    )


def _resume_plan(**changes: object) -> ResumePlan:
    values: dict[str, object] = {
        "realm_id": IDS[0],
        "project_id": IDS[1],
        "work_item_id": IDS[2],
        "checkpoint_id": IDS[3],
        "checkpoint_digest": digest("checkpoint"),
        "checkpoint_revision": 2,
        "selected_checkpoint_reason": "latest-valid-v2",
        "disposition": ResumeDisposition.SAFE_CONTINUE,
        "stale_dimensions": (),
        "reconciliation_actions": (),
        "reacquire_resources": ("authorization",),
        "logical_read_resources": ("project:p:source",),
        "logical_write_resources": ("project:p:file",),
        "runtime": RuntimeObservation(
            IDS[4],
            IDS[5],
            IDS[6],
            IDS[7],
            IDS[8],
            digest("envelope"),
            IDS[9],
            1,
            "ready",
            NOW - dt.timedelta(seconds=1),
            NOW + dt.timedelta(minutes=10),
        ),
        "target_client_id": "codex",
        "next_step_id": "build",
        "context_recipe": "resume:codex:implementer",
        "required_route_role": "implementer",
        "actions": (ResumeAction("dispatch", "dispatch-next-step", (), "build"),),
        "blockers": (),
        "observed_at": NOW,
        "valid_until": NOW + dt.timedelta(minutes=5),
    }
    values.update(changes)
    return ResumePlan(**cast(Any, values))


def test_resume_apply_rejects_time_timeout_client_and_disposition_before_storage(
    tmp_path: Path,
) -> None:
    service = ResumeApplyService(cast(Any, object()), cast(Any, object()))
    plan = _resume_plan()
    request = ResumeApplyRequest(
        plan, plan.plan_digest, IDS[10], IDS[11], "worker", ("database.write",)
    )
    cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"now": NOW.replace(tzinfo=None), "timeout_seconds": 10}, "timezone-aware"),
        ({"now": NOW, "timeout_seconds": 0}, "timeout"),
        ({"now": plan.valid_until, "timeout_seconds": 10}, "penceresi doldu"),
    )
    for arguments, message in cases:
        with pytest.raises(PolicyViolation, match=message):
            service.apply(request, cast(Any, _Adapter()), cwd=tmp_path, **cast(Any, arguments))
    wrong = SimpleNamespace(descriptor=replace(_Adapter.descriptor, client_id="other"))
    with pytest.raises(PolicyViolation, match="target client drift"):
        service.apply(request, cast(Any, wrong), cwd=tmp_path, timeout_seconds=10, now=NOW)
    unsafe = _resume_plan(disposition=ResumeDisposition.DENIED)
    unsafe_request = ResumeApplyRequest(
        unsafe, unsafe.plan_digest, IDS[10], IDS[11], "worker", ("database.write",)
    )
    with pytest.raises(PolicyViolation, match="safe-continue"):
        service.apply(
            unsafe_request, cast(Any, _Adapter()), cwd=tmp_path, timeout_seconds=10, now=NOW
        )
