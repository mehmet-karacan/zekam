import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from typer.testing import CliRunner

from zekam.application.config import EmbeddingRoute
from zekam.application.project_rag_runtime import (
    _assert_oracle_snapshot_selection,
    _index,
    build_index_job_plan,
)
from zekam.domain.errors import PolicyViolation
from zekam.interfaces.cli.main import app

PROJECT_UUID = UUID("123e4567-e89b-12d3-a456-426614174000")
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
SHA_E = "sha256:" + "e" * 64
REV_A = "sha256:" + "f" * 64
REV_F = "sha256:" + "0" * 64


@pytest.mark.parametrize(
    ("state_content", "expected_error"),
    [
        (None, None),
        ({"project_slug": "demo", "project_id": str(PROJECT_UUID)}, None),
        ({"project_slug": "demo", "project_id": str(PROJECT_UUID), "oracle_snapshot_source": "snap"}, PolicyViolation),
        ("not-json", PolicyViolation),
        ({"project_slug": "other", "project_id": str(PROJECT_UUID)}, PolicyViolation),
    ],
)
def test_assert_oracle_snapshot_selection(monkeypatch, tmp_path, state_content, expected_error):
    monkeypatch.setattr(
        "zekam.application.project_rag_runtime._existing_runtime_paths",
        lambda home, slug: {"state": tmp_path / "state.json"},
    )
    monkeypatch.setattr(
        "zekam.application.project_rag_runtime._registered_project",
        lambda home, slug: PROJECT_UUID,
    )
    if state_content is not None:
        (tmp_path / "state.json").write_text(state_content if isinstance(state_content, str) else json.dumps(state_content))
    if expected_error:
        with pytest.raises(expected_error):
            _assert_oracle_snapshot_selection(tmp_path, "demo", None, None)
    else:
        _assert_oracle_snapshot_selection(tmp_path, "demo", None, None)


def test_explicit_snapshot_bypasses_paths_check(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise AssertionError("should not be called")
    monkeypatch.setattr("zekam.application.project_rag_runtime._existing_runtime_paths", fail)
    monkeypatch.setattr("zekam.application.project_rag_runtime._registered_project", fail)
    result = _assert_oracle_snapshot_selection(tmp_path, "demo", "snap", None)
    assert result is None


def test_both_snapshot_and_config_rejects_before_paths(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise AssertionError("should not be called")
    monkeypatch.setattr("zekam.application.project_rag_runtime._existing_runtime_paths", fail)
    monkeypatch.setattr("zekam.application.project_rag_runtime._registered_project", fail)
    with pytest.raises(PolicyViolation):
        _assert_oracle_snapshot_selection(tmp_path, "demo", "snap", "profile.yaml")


def test_plan_hash_drift(monkeypatch, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    monkeypatch.setattr("zekam.application.project_rag_runtime.resolve_project_source", lambda *a, **k: source)
    monkeypatch.setattr("zekam.application.project_rag_runtime._registered_project", lambda *a, **k: PROJECT_UUID)
    monkeypatch.setattr("zekam.application.project_rag_runtime._git_source_state", lambda *a, **k: ("head", "status", "rev"))
    monkeypatch.setattr("zekam.application.project_rag_runtime.discover", lambda *a, **k: SimpleNamespace(tree_digest=SHA_A, inventory_digest=SHA_B, metadata_inventory_digest=SHA_C, inventory_scope="source-tree"))
    monkeypatch.setattr("zekam.application.project_rag_runtime.load_settings", lambda *a, **k: SimpleNamespace(knowledge=SimpleNamespace(embedding_route=EmbeddingRoute.LOCAL)))
    monkeypatch.setattr("zekam.application.project_rag_runtime._knowledge_binding_digest", lambda *a, **k: SHA_D)
    monkeypatch.setattr("zekam.application.project_rag_runtime.load_smart_binding", lambda *a, **k: None)
    monkeypatch.setattr("zekam.application.project_rag_runtime.collect_reference_corpus", lambda *a, **k: SimpleNamespace(snapshot_digest=SHA_E))
    
    recorded = []
    mutable_rev = [REV_A]
    def mock_load_oracle(root, path):
        recorded.append((root, path))
        return SimpleNamespace(revision_digest=mutable_rev[0])
    monkeypatch.setattr("zekam.application.project_rag_runtime.load_oracle_metadata_snapshot", mock_load_oracle)
    
    plan1 = build_index_job_plan(tmp_path, "demo", oracle_snapshot="snap", batch_size=4, max_remote_provider_calls=2)
    assert plan1["oracle_snapshot_revision_digest"] == REV_A
    assert plan1["provider_calls"] == 0
    assert recorded[0][0] == source
    
    mutable_rev[0] = REV_F
    plan2 = build_index_job_plan(tmp_path, "demo", oracle_snapshot="snap", batch_size=4, max_remote_provider_calls=2)
    assert plan2["oracle_snapshot_revision_digest"] == REV_F
    assert plan1["plan_digest"] != plan2["plan_digest"]
    assert recorded[1][0] == source


def test_index_expected_revision_drift_rejects(monkeypatch, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    monkeypatch.setattr("zekam.application.project_rag_runtime._runtime_paths", lambda *a, **k: {"home": tmp_path})
    monkeypatch.setattr("zekam.application.project_rag_runtime._recover_rag_state_pending", lambda *a, **k: None)
    monkeypatch.setattr("zekam.application.project_rag_runtime.load_settings", lambda *a, **k: SimpleNamespace(knowledge=object()))
    monkeypatch.setattr("zekam.application.project_rag_runtime._assert_canonical_knowledge_profile", lambda *a, **k: None)
    monkeypatch.setattr("zekam.application.project_rag_runtime._git_source_state", lambda *a, **k: ("head", "status", "rev"))
    monkeypatch.setattr("zekam.application.project_rag_runtime._project_plan", lambda *a, **k: (object(), SimpleNamespace(discovery=SimpleNamespace(files=[]))))
    monkeypatch.setattr("zekam.application.project_rag_runtime.collect_reference_corpus", lambda *a, **k: SimpleNamespace(snapshot_digest="ref"))
    
    def mock_load_oracle(root, path):
        assert root == source
        return SimpleNamespace(revision_digest=REV_A)
    monkeypatch.setattr("zekam.application.project_rag_runtime.load_oracle_metadata_snapshot", mock_load_oracle)
    
    def fail_build(*a, **k):
        raise AssertionError("should not be called")
    monkeypatch.setattr("zekam.application.project_rag_runtime.build_oracle_metadata_index_plan", fail_build)
    
    with pytest.raises(PolicyViolation):
        _index(source, tmp_path, PROJECT_UUID, "demo", None, None, batch_size=4, oracle_snapshot="snap", expected_oracle_snapshot_revision=REV_F, max_remote_provider_calls=2)


@pytest.mark.parametrize("extra_args", [[], ["--oracle-config", "profile.yaml", "--authorize-database-metadata"]])
def test_cli_admission(monkeypatch, extra_args):
    def fail_resolve_home(*a, **k):
        raise AssertionError("should not be called")
    monkeypatch.setattr("zekam.interfaces.cli.project.resolve_home", fail_resolve_home)
    runner = CliRunner()
    result = runner.invoke(app, ["project", "index", "demo", "--oracle-snapshot", "snap", "--json", *extra_args])
    assert result.exit_code == 77
    assert "AssertionError" not in result.output
