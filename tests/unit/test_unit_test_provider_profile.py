"""Profile projections are checked without provider/model/Maven calls."""

import json

import pytest
from typer.testing import CliRunner

from zekam.application.unit_test_provider_profile import (
    BatchProvider,
    BatchProviderProfile,
    PROFILE_SCHEMA,
    load_batch_provider_profile,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.opencode_batch import batch_config
from zekam.interfaces.cli.main import app


def profile_document(endpoint="http://127.0.0.1:11434/v1", scope="loopback"):
    return {
        "schema": PROFILE_SCHEMA,
        "providers": [
            {
                "id": "zekam-batch-gpu",
                "endpoint": endpoint,
                "scope": scope,
                "models": {"builder": "qwen-coder", "verifier": "glm-review"},
            }
        ],
    }


def test_profile_only_projects_reviewed_endpoint_and_one_selected_model(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile_document()), encoding="utf-8")
    profile = load_batch_provider_profile(path)
    assert (
        profile.validate_selection("zekam-batch-gpu/builder", "zekam-batch-gpu/verifier") is False
    )
    config = json.loads(batch_config(profile, model_id="zekam-batch-gpu/builder"))
    provider = config["providers"]["zekam-batch-gpu"]
    assert provider["settings"] == {"baseURL": "http://127.0.0.1:11434/v1"}
    assert provider["models"] == {"builder": {"modelID": "qwen-coder"}}
    assert provider["package"] == "@opencode/ai/providers/openai-compatible"
    assert config["permissions"][-1]["effect"] == "deny"
    assert config["agents"]["zekam-test-batch"]["permissions"][-1]["effect"] == "deny"
    assert "endpoint" not in profile.summary()["providers"][0]


@pytest.mark.parametrize(
    "endpoint,scope",
    [
        ("http://localhost:11434/v1", "loopback"),
        ("http://10.0.0.5:8000/v1", "loopback"),
        ("https://example.com/v1", "loopback"),
        ("http://example.com/v1", "remote"),
        ("https://user:password@example.com/v1", "remote"),
        ("https://example.com/v1?key=private", "remote"),
        ("https://example.com/{env:PRIVATE}/v1", "remote"),
        ("http://127.0.0.1:99999/v1", "loopback"),
    ],
)
def test_locality_credentials_and_interpolation_cannot_escape_profile(endpoint, scope):
    with pytest.raises((ValidationFailed, PolicyViolation)):
        BatchProvider("zekam-batch-gpu", endpoint, scope, (("builder", "qwen-coder"),))


def test_different_aliases_cannot_hide_same_upstream_model():
    profile = BatchProviderProfile(
        (
            BatchProvider(
                "zekam-batch-gpu",
                "http://127.0.0.1:8000/v1",
                "loopback",
                (("builder", "same"), ("verifier", "SAME")),
            ),
        )
    )
    with pytest.raises(PolicyViolation, match="upstream"):
        profile.validate_selection("zekam-batch-gpu/builder", "zekam-batch-gpu/verifier")


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "utf16", "oversize"])
def test_profile_loader_rejects_unsafe_or_ambiguous_input(tmp_path, mutation):
    document = profile_document()
    path = tmp_path / "profile.json"
    if mutation == "unknown":
        document["providers"][0]["plugins"] = ["file://untrusted-plugin"]
    raw = json.dumps(document).encode("utf-8")
    if mutation == "duplicate":
        raw = raw.replace(b'"scope": "loopback"', b'"scope":"remote","scope":"loopback"')
    elif mutation == "utf16":
        raw = raw.decode().encode("utf-16")
    elif mutation == "oversize":
        raw = b" " * 16385
    path.write_bytes(raw)
    with pytest.raises((ValidationFailed, OSError)):
        load_batch_provider_profile(path)


def test_endpoint_and_upstream_change_invalidates_cli_plan_but_loopback_needs_no_remote_budget(
    tmp_path,
):
    runner = CliRunner()
    path = tmp_path / "profile.json"
    args = [
        "test",
        "plan",
        "--project-id",
        "project",
        "--source-binding-id",
        "binding",
        "--source-revision",
        "revision",
        "--source",
        "src/main/java/Example.java",
        "--percent",
        "80",
        "--model",
        "zekam-batch-gpu/builder",
        "--verifier-model",
        "zekam-batch-gpu/verifier",
        "--provider-profile",
        str(path),
    ]
    digests = []
    for endpoint in ("http://127.0.0.1:11434/v1", "http://127.0.0.1:8000/v1"):
        path.write_text(json.dumps(profile_document(endpoint)), encoding="utf-8")
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout
        plan = json.loads(result.stdout)
        assert plan["agent_execution"]["binding_status"] == "ready-for-runtime-check"
        assert plan["agent_execution"]["max_remote_calls"] == 0
        assert endpoint not in result.stdout
        digests.append(plan["plan_digest"])
    assert digests[0] != digests[1]
