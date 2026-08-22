"""Model-provider endpoint eslemesi ve agsiz dry-run dogrulamasi."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from zekam.application.model_registry import load_inventory
from zekam.application.provider_configuration import (
    REQUIRED_PROVIDER_MODALITIES,
    ProviderBindingSet,
    RequestFormat,
    evaluate_provider_configuration,
    load_provider_bindings,
)
from zekam.domain.errors import ValidationFailed
from zekam.domain.model_inventory import Modality
from zekam.domain.security import SecretBackend, SecretRef

pytestmark = pytest.mark.unit


def test_default_mapping_selects_exact_model_for_every_required_modality() -> None:
    bindings = load_provider_bindings()
    assert len(bindings.bindings) == len(REQUIRED_PROVIDER_MODALITIES) == 7
    assert {item.modality for item in bindings.bindings} == set(REQUIRED_PROVIDER_MODALITIES)
    assert bindings.for_modality(Modality.EMBEDDING).model_id == (
        "7622a967-8c01-4172-af52-e16b4a5b3fd0"
    )
    assert bindings.for_modality(Modality.RERANK).model_id == (
        "5499ecda-bf10-4553-9776-4bc97ee2c00e"
    )
    assert bindings.for_modality(Modality.AUDIO_TRANSCRIPTION).request_format is (
        RequestFormat.MULTIPART
    )


def test_default_mapping_exactly_matches_canonical_inventory() -> None:
    report = evaluate_provider_configuration(
        bindings=load_provider_bindings(),
        inventory=load_inventory(),
        secret_refs={},
        environ={},
    )
    assert all(item.inventory_match for item in report.checks)
    assert report.binding_set_digest.startswith("sha256:")


def test_empty_environment_is_visible_but_never_calls_provider() -> None:
    report = evaluate_provider_configuration(secret_refs={}, environ={})
    document = report.as_dict()
    assert report.ready_count == 0
    assert document["provider_calls_made"] == 0
    assert document["network_calls_made"] == 0
    assert document["secret_values_reported"] == 0
    assert all("endpoint-value-missing" in item.reasons for item in report.checks)
    assert all("credential-value-missing" in item.reasons for item in report.checks)
    assert all("secret-ref-missing" in item.reasons for item in report.checks)


def test_blank_endpoint_and_credential_are_missing_not_ready() -> None:
    bindings = load_provider_bindings()
    environment = {
        locator: ""
        for binding in bindings.bindings
        for locator in (binding.endpoint_env, binding.credential_env)
    }

    report = evaluate_provider_configuration(bindings=bindings, secret_refs={}, environ=environment)

    assert report.ready is False
    assert all(not item.endpoint_value_present for item in report.checks)
    assert all(not item.credential_value_present for item in report.checks)


def test_complete_metadata_and_environment_pass_dry_run_without_value_disclosure() -> None:
    bindings = load_provider_bindings()
    realm_id = uuid4()
    environment: dict[str, str] = {}
    references: dict[str, SecretRef] = {}
    sensitive_values: list[str] = []
    for binding in bindings.bindings:
        endpoint_value = f"https://models.example.test{binding.path_hint}"
        credential_value = f"credential-value-{binding.modality.value}"
        environment[binding.endpoint_env] = endpoint_value
        environment[binding.credential_env] = credential_value
        sensitive_values.extend((endpoint_value, credential_value))
        references[binding.secret_ref_name] = SecretRef.create(
            realm_id=realm_id,
            name=binding.secret_ref_name,
            provider=binding.provider_ref,
            purpose=f"{binding.modality.value} contract test",
            allowed_operations=(binding.operation,),
            store_backend=SecretBackend.ENVIRONMENT,
            store_locator=binding.credential_env,
        )
    report = evaluate_provider_configuration(
        bindings=bindings,
        inventory=load_inventory(),
        secret_refs=references,
        environ=environment,
    )
    rendered = json.dumps(report.as_dict())
    assert report.ready
    assert report.ready_count == 7
    assert all(value not in rendered for value in sensitive_values)
    assert "://" not in rendered


def test_secretref_operation_or_locator_mismatch_is_fail_closed() -> None:
    bindings = load_provider_bindings()
    binding = bindings.bindings[0]
    reference = SecretRef.create(
        realm_id=uuid4(),
        name=binding.secret_ref_name,
        provider=binding.provider_ref,
        purpose="wrong scope",
        allowed_operations=("different-operation",),
        store_backend=SecretBackend.ENVIRONMENT,
        store_locator="DIFFERENT_LOCATOR",
    )
    report = evaluate_provider_configuration(
        bindings=bindings,
        secret_refs={binding.secret_ref_name: reference},
        environ={
            binding.endpoint_env: f"https://models.example.test{binding.path_hint}",
            binding.credential_env: "present",
        },
    )
    check = next(item for item in report.checks if item.binding == binding)
    assert not check.ready
    assert "secret-ref-mismatch" in check.reasons


def test_duplicate_modality_and_unsafe_path_are_rejected() -> None:
    bindings = load_provider_bindings()
    with pytest.raises(ValidationFailed, match="tam bir binding"):
        ProviderBindingSet((*bindings.bindings, bindings.bindings[0]))
    with pytest.raises(ValidationFailed, match="path_hint"):
        replace(bindings.bindings[0], path_hint="/v1/../admin")
