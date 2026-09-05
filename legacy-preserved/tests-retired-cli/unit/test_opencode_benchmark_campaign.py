from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from zekam.application.model_benchmark_service import default_fixture_file, load_fixture_registry
from zekam.application.opencode_benchmark_campaign import (
    discover_campaign,
    load_campaign_scope,
)
from zekam.application.opencode_remote_benchmark import (
    EVALUATOR_PROVENANCE_DIGEST,
    DeterministicProviderNeutralVerifier,
    load_remote_fixture,
)
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_inventory import Modality
from zekam.interfaces.cli.main import app
from zekam.interfaces.cli.model_campaign import _remote_response

runner = CliRunner()


def test_malformed_completed_guardrail_response_is_model_failure_not_recovery() -> None:
    fixture = next(
        item
        for item in load_fixture_registry().fixtures
        if item.modality == Modality.GUARDRAIL.value and "opencode-remote" in item.tags
    )
    artifact = load_remote_fixture(
        fixture, allow_root=default_fixture_file().parent.resolve(strict=True)
    )

    response = _remote_response(
        SimpleNamespace(modality=Modality.GUARDRAIL),
        raw_response={"choices": [{"message": {"content": "not-json"}}]},
        artifact=artifact,
        latency_ms=5,
    )
    evaluation = DeterministicProviderNeutralVerifier().verify(artifact, response.payload)

    assert response.payload == {}
    assert not evaluation.approved
    assert not evaluation.format_ok


def _config(path: Path, model_ids: list[str]) -> Path:
    path.write_text(
        json.dumps(
            {
                "enabled_providers": ["litellm"],
                "provider": {
                    "litellm": {
                        "options": {
                            "baseURL": "https://aihub-api.turktelekom.com.tr/v1",
                            "apiKey": "{env:ZEKAM_TEST_AIHUB_KEY}",
                        },
                        "models": {model_id: {"name": model_id} for model_id in model_ids},
                    }
                },
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    return path


def test_discovery_covers_every_reviewed_configured_model_without_audio_calls(
    tmp_path: Path,
) -> None:
    scope = load_campaign_scope()
    config = _config(
        tmp_path / "opencode.json",
        [item.configured_model_id for item in reversed(scope.targets)],
    )

    discovery = discover_campaign(
        config_file=config,
        verifier_provenance_digest=EVALUATOR_PROVENANCE_DIGEST,
    )

    assert discovery.configured_model_count == 17
    assert discovery.canonical_target_count == 18
    assert discovery.audio_excluded_count == 1
    assert discovery.health_call_count == 17
    assert discovery.tested_call_count == 85
    assert discovery.provider_call_budget == 102
    assert discovery.discovery_digest.startswith("sha256:")
    assert all(
        not item.fixture_digests for item in discovery.targets if item.excluded_reason is not None
    )


def test_catalog_drift_is_fail_closed(tmp_path: Path) -> None:
    scope = load_campaign_scope()
    config = _config(
        tmp_path / "opencode.json",
        [item.configured_model_id for item in scope.targets[:-1]],
    )

    with pytest.raises(PolicyViolation, match="scope drift"):
        discover_campaign(
            config_file=config,
            verifier_provenance_digest=EVALUATOR_PROVENANCE_DIGEST,
        )


def test_discovery_digest_is_config_order_independent(tmp_path: Path) -> None:
    scope = load_campaign_scope()
    ids = [item.configured_model_id for item in scope.targets]
    first = discover_campaign(
        config_file=_config(tmp_path / "one.json", ids),
        verifier_provenance_digest=EVALUATOR_PROVENANCE_DIGEST,
    )
    second = discover_campaign(
        config_file=_config(tmp_path / "two.json", list(reversed(ids))),
        verifier_provenance_digest=EVALUATOR_PROVENANCE_DIGEST,
    )

    assert first.discovery_digest == second.discovery_digest


def test_campaign_plan_cli_is_read_only_and_exact(tmp_path: Path) -> None:
    scope = load_campaign_scope()
    config = _config(
        tmp_path / "opencode.json",
        [item.configured_model_id for item in scope.targets],
    )

    result = runner.invoke(
        app,
        ["model", "campaign", "plan", "--config", str(config), "--json"],
    )

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["call_count"] == 102
    assert document["health_call_count"] == 17
    assert document["tested_call_count"] == 85
    assert document["authority_records_created"] == 0
    assert document["provider_calls_made"] == 0
    assert document["audio_provider_calls_made"] == 0
    serialized = json.dumps(document)
    assert "apiKey" not in serialized and "baseURL" not in serialized
