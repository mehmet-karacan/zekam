"""OpenCode embedding config adapter guvenlik testleri."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.model_registry import load_inventory
from zekam.application.opencode_embedding import (
    AIHUB_PROVIDER_HOST,
    MAX_OPENCODE_CONFIG_BYTES,
    OpenCodeCredentialStore,
    OpenCodeEmbeddingConfiguration,
    build_opencode_embedding_probe_manifest,
    evaluate_embedding_candidates,
    evaluate_opencode_aihub_models,
    evaluate_opencode_embedding_response,
    load_opencode_aihub_catalog,
    load_opencode_embedding_configuration,
)
from zekam.domain.errors import ConfigurationError, NotFound, PolicyViolation, ValidationFailed
from zekam.domain.model_inventory import HealthState, InventorySnapshot
from zekam.domain.security import SecretBackend, SecretRef

pytestmark = pytest.mark.unit

EMBEDDING = "openai/BAAI/bge-m3"
NON_EMBEDDING = "openai/codepilot-qwen3"


def _document(
    *,
    base_url: object = "https://models.example.test/v1",
    api_key: object = "{env:OPENCODE_LITELLM_KEY}",
    models: object | None = None,
    provider_extra: dict[str, object] | None = None,
    options_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    configured_models = models or {
        EMBEDDING: {"name": "Embedding"},
        NON_EMBEDDING: {"name": "Code"},
    }
    options: dict[str, object] = {"baseURL": base_url, "apiKey": api_key, "timeout": 1000}
    options.update(options_extra or {})
    provider: dict[str, object] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "LiteLLM",
        "options": options,
        "models": configured_models,
    }
    provider.update(provider_extra or {})
    return {
        "$schema": "https://opencode.ai/config.json",
        "enabled_providers": ["litellm"],
        "provider": {"litellm": provider},
        "mcp": {"ignored": {"type": "local"}},
    }


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _load(path: Path, *, selected: str = EMBEDDING) -> OpenCodeEmbeddingConfiguration:
    return load_opencode_embedding_configuration(
        path,
        provider_id="litellm",
        selected_model_id=selected,
        inventory=load_inventory(),
    )


def _reference(*, provider: str = "litellm", locator: str = "OPENCODE_LITELLM_KEY") -> SecretRef:
    return SecretRef.create(
        realm_id=uuid4(),
        name="opencode-litellm",
        provider=provider,
        purpose="embedding",
        allowed_operations=("embeddings",),
        store_backend=SecretBackend.ENVIRONMENT,
        store_locator=locator,
    )


def test_load_exposes_only_sanitized_provider_endpoint_and_models(tmp_path: Path) -> None:
    binding = _load(_write(tmp_path / "opencode.json", _document()))

    assert binding.provider_id == "litellm"
    assert binding.embedding_endpoint == "https://models.example.test/v1/embeddings"
    assert binding.endpoint_identity.as_dict() | {} == {
        "scheme": "https",
        "host": "models.example.test",
        "port": 443,
        "base_path": "/v1",
        "identity_digest": binding.endpoint_identity.identity_digest,
    }
    assert EMBEDDING in binding.model_ids
    sanitized = binding.sanitized()
    rendered = repr(sanitized)
    assert sanitized["credential_source"] == "environment"
    assert "OPENCODE_LITELLM_KEY" not in rendered
    assert "apiKey" not in rendered


def test_loopback_http_is_allowed_but_remote_http_is_rejected(tmp_path: Path) -> None:
    loopback = _load(
        _write(tmp_path / "loopback.json", _document(base_url="http://127.0.0.1:4000/v1"))
    )
    assert loopback.embedding_endpoint == "http://127.0.0.1:4000/v1/embeddings"

    with pytest.raises(ValidationFailed, match="loopback"):
        _load(_write(tmp_path / "remote.json", _document(base_url="http://models.test/v1")))
    with pytest.raises(ConfigurationError, match="normalize degil"):
        _load(
            _write(
                tmp_path / "ambiguous.json",
                _document(base_url="https://models.example.test/v1/../admin"),
            )
        )


def test_symlink_or_reparse_config_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write(tmp_path / "opencode.json", _document())
    monkeypatch.setattr(
        "zekam.application.opencode_embedding._is_link_or_reparse",
        lambda path: path == source,
    )
    with pytest.raises(ConfigurationError, match="link/reparse"):
        _load(source)


def test_oversized_and_duplicate_key_documents_are_rejected(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * MAX_OPENCODE_CONFIG_BYTES + b"}")
    with pytest.raises(ConfigurationError, match="boyutu"):
        _load(oversized)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"provider": {}, "provider": {}}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="duplicate"):
        _load(duplicate)


@pytest.mark.parametrize(
    "document",
    [
        _document(provider_extra={"unknown": True}),
        _document(options_extra={"unknown": True}),
        _document(options_extra={"timeout": 0}),
        _document(models={EMBEDDING: {"name": "Embedding", "unknown": True}}),
        _document(api_key=""),
        _document(api_key="literal-secret-must-not-load"),
    ],
)
def test_unknown_malformed_or_literal_secret_fields_fail_closed(
    tmp_path: Path, document: dict[str, object]
) -> None:
    with pytest.raises(ConfigurationError) as caught:
        _load(_write(tmp_path / "opencode.json", document))
    assert "literal-secret-must-not-load" not in str(caught.value)


def test_non_embedding_model_selection_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed, match="embedding modalitesinde degil"):
        _load(_write(tmp_path / "opencode.json", _document()), selected=NON_EMBEDDING)


def test_aihub_catalog_is_exact_and_sanitized(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "opencode.json",
        _document(base_url=f"https://{AIHUB_PROVIDER_HOST}/v1"),
    )

    catalog = load_opencode_aihub_catalog(source, provider_id="litellm")

    assert catalog.provider_family == "aihub"
    assert set(catalog.configured_model_ids) == {EMBEDDING, NON_EMBEDDING}
    rendered = repr(catalog.sanitized())
    assert AIHUB_PROVIDER_HOST not in rendered
    assert "OPENCODE_LITELLM_KEY" not in rendered
    assert "https://" not in rendered


def test_aihub_catalog_rejects_other_provider_host_without_echo(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "opencode.json",
        _document(base_url="https://not-aihub.invalid/v1"),
    )

    with pytest.raises(PolicyViolation, match="AIHub") as caught:
        load_opencode_aihub_catalog(source, provider_id="litellm")

    assert "not-aihub.invalid" not in str(caught.value)


def test_aihub_effective_activation_requires_config_inventory_and_health(
    tmp_path: Path,
) -> None:
    base = load_inventory()
    records = tuple(
        replace(
            record,
            health_state=(
                HealthState.HEALTH_PASSED
                if record.access_name in {EMBEDDING, NON_EMBEDDING}
                else record.health_state
            ),
            enabled=False if record.access_name == NON_EMBEDDING else record.enabled,
        )
        for record in base.records
    )
    inventory = InventorySnapshot(
        schema=base.schema,
        inventory_date=base.inventory_date,
        records=records,
    )
    unknown = "openai/unknown-not-in-canonical-inventory"
    source = _write(
        tmp_path / "opencode.json",
        _document(
            base_url=f"https://{AIHUB_PROVIDER_HOST}/v1",
            models={
                EMBEDDING: {"name": "Embedding"},
                NON_EMBEDDING: {"name": "Code"},
                unknown: {"name": "Unknown"},
            },
        ),
    )
    catalog = load_opencode_aihub_catalog(source, provider_id="litellm")

    results = evaluate_opencode_aihub_models(catalog, inventory)

    active = next(item for item in results if item.configured_model_id == EMBEDDING)
    assert active.health_passed and active.active and active.enabled
    assert active.benchmark_eligible
    disabled = next(item for item in results if item.configured_model_id == NON_EMBEDDING)
    assert disabled.health_passed and not disabled.enabled and not disabled.active
    assert "disabled-in-canonical-inventory" in disabled.reasons
    missing = next(item for item in results if item.configured_model_id == unknown)
    assert not missing.canonical_present and not missing.enabled
    absent = next(
        item
        for item in results
        if item.canonical_model_id == "979c8117-ab95-4f02-ac9b-154284828e27"
    )
    assert not absent.configured and not absent.active and not absent.benchmark_eligible
    assert "not-configured-in-opencode" in absent.reasons

    stale_results = evaluate_opencode_aihub_models(
        catalog,
        inventory,
        fresh_benchmark_eligible_ids=(),
    )
    stale = next(item for item in stale_results if item.configured_model_id == EMBEDDING)
    assert not stale.health_passed and not stale.enabled
    assert "health-not-passed-or-stale" in stale.reasons


def test_aihub_ambiguous_canonical_identity_fails_closed(tmp_path: Path) -> None:
    ambiguous_id = "BAAI/bge-reranker-v2-m3"
    source = _write(
        tmp_path / "opencode.json",
        _document(
            base_url=f"https://{AIHUB_PROVIDER_HOST}/v1",
            models={ambiguous_id: {"name": "Reranker"}},
        ),
    )
    catalog = load_opencode_aihub_catalog(source, provider_id="litellm")

    result = next(
        item
        for item in evaluate_opencode_aihub_models(catalog, load_inventory())
        if item.configured_model_id == ambiguous_id and not item.canonical_present
    )

    assert not result.enabled and not result.benchmark_eligible
    assert result.reasons == ("canonical-model-ambiguous",)


def test_credential_store_is_exact_masked_and_rejects_empty_values() -> None:
    store = OpenCodeCredentialStore(
        provider_id="litellm",
        credential_locator="OPENCODE_LITELLM_KEY",
        environ={"OPENCODE_LITELLM_KEY": "in-memory-secret"},
    )
    secret = store.resolve(_reference())
    assert str(secret) == "***"
    assert repr(secret) == "SecretValue(***)"
    assert secret.reveal() == "in-memory-secret"
    secret.clear()

    with pytest.raises(PolicyViolation):
        store.resolve(_reference(provider="other"))
    with pytest.raises(PolicyViolation):
        store.resolve(_reference(locator="OTHER_KEY"))
    with pytest.raises(NotFound):
        OpenCodeCredentialStore(
            provider_id="litellm",
            credential_locator="OPENCODE_LITELLM_KEY",
            environ={"OPENCODE_LITELLM_KEY": "   "},
        ).resolve(_reference())


def test_embedding_candidate_evaluation_is_inventory_bound_and_sanitized() -> None:
    results = evaluate_embedding_candidates((EMBEDDING,), load_inventory())
    selected = next(item for item in results if item.access_name == EMBEDDING)
    assert selected.configured and selected.enabled and not selected.eligible
    assert "canonical-health-not-eligible" in selected.reasons
    rendered = repr([item.sanitized() for item in results]).lower()
    assert "endpoint" not in rendered
    assert "credential" not in rendered
    assert NON_EMBEDDING not in {item.access_name for item in results}


def test_probe_manifest_is_exact_public_and_call_specific(tmp_path: Path) -> None:
    configuration = _load(_write(tmp_path / "opencode.json", _document()))

    manifest, prepared = build_opencode_embedding_probe_manifest((configuration,))

    assert len(manifest.calls) == len(prepared) == 1
    plan = manifest.calls[0]
    assert plan.runtime_bound
    assert plan.call_resource.endswith(plan.call_id)
    assert plan.authorization_scope()["max_uses"] == 1
    assert plan.authorization_scope()["data_classifications"] == ["public"]
    assert prepared[0].call.payload_digest == plan.payload_digest


def test_probe_response_metrics_verify_duplicate_and_semantic_margin(tmp_path: Path) -> None:
    configuration = _load(_write(tmp_path / "opencode.json", _document()))
    response = {
        "data": [
            {"index": 0, "embedding": [1.0, 0.0]},
            {"index": 1, "embedding": [1.0, 0.0]},
            {"index": 2, "embedding": [0.9, 0.1]},
            {"index": 3, "embedding": [0.0, 1.0]},
            {"index": 4, "embedding": [0.8, 0.2]},
            {"index": 5, "embedding": [0.1, 0.9]},
        ]
    }

    metrics = evaluate_opencode_embedding_response(configuration, response, latency_ms=12)

    assert metrics.verified
    assert metrics.dimension == 2
    assert metrics.duplicate_max_delta == 0.0
    assert metrics.semantic_margin > 0.0


def test_probe_response_rejects_non_deterministic_duplicate(tmp_path: Path) -> None:
    configuration = _load(_write(tmp_path / "opencode.json", _document()))
    response = {
        "data": [
            {"index": 0, "embedding": [1.0, 0.0]},
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 2, "embedding": [0.9, 0.1]},
            {"index": 3, "embedding": [0.0, 1.0]},
            {"index": 4, "embedding": [0.8, 0.2]},
            {"index": 5, "embedding": [0.1, 0.9]},
        ]
    }

    metrics = evaluate_opencode_embedding_response(configuration, response, latency_ms=4)

    assert not metrics.verified
    assert metrics.duplicate_max_delta == 1.0
