"""Source indexing skips ignored descendants without claiming a full inventory."""

from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from zekam.application import project_rag_runtime as runtime
from zekam.application import source_discovery as discovery
from zekam.application.source_discovery import DiscoveryPolicy, InventoryStatus, StopReason
from zekam.domain.errors import PolicyViolation


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('source')\n", encoding="utf-8")
    (root / "README.md").write_text("# Project source\n", encoding="utf-8")
    deps = root / "node_modules" / "vendor"
    deps.mkdir(parents=True)
    for number in range(20):
        (deps / f"generated-{number}.js").write_bytes(b"generated")
    return root


def test_source_scope_does_not_visit_ignored_descendants(tmp_path, monkeypatch):
    root = _source(tmp_path)
    full = discovery.discover(root, policy=DiscoveryPolicy(max_inventory_entries=12))
    assert full.inventory_status is InventoryStatus.PARTIAL
    assert full.inventory_stop_reason is StopReason.INVENTORY_CAP
    original = discovery._list_bounded

    def source_only(directory, *args, **kwargs):
        assert "node_modules" not in directory.relative_to(root).parts
        return original(directory, *args, **kwargs)

    monkeypatch.setattr(discovery, "_list_bounded", source_only)
    scoped = discovery.discover(
        root, policy=DiscoveryPolicy(max_inventory_entries=12, include_ignored_descendants=False)
    )
    assert scoped.inventory_status is InventoryStatus.COMPLETE
    assert scoped.inventory_scope == "source-tree"
    assert scoped.as_dict()["inventory"]["scope"] == "source-tree"
    assert {item.relative_path for item in scoped.files} == {"README.md", "src/main.py"}
    assert "node_modules" in {item.relative_path for item in scoped.inventory}
    assert not any(item.relative_path.startswith("node_modules/") for item in scoped.inventory)


def test_scope_is_bound_even_for_identical_inventory_entries(tmp_path):
    (tmp_path / "node_modules").mkdir()
    full = discovery.discover(tmp_path)
    scoped = discovery.discover(tmp_path, policy=DiscoveryPolicy(include_ignored_descendants=False))
    assert full.inventory == scoped.inventory
    assert full.inventory_digest != scoped.inventory_digest
    assert full.metadata_inventory_digest != scoped.metadata_inventory_digest
    assert "include_ignored_descendants" not in full.policy.as_dict()
    assert "scope" not in full.as_dict()["inventory"]


def test_scoped_freshness_and_legacy_full_tree_remain_distinct(tmp_path):
    root = _source(tmp_path)
    full = discovery.discover(root).metadata_inventory_digest
    policy = DiscoveryPolicy(include_ignored_descendants=False)
    scoped = discovery.discover(root, policy=policy).metadata_inventory_digest
    metadata = discovery.discover(root, policy=replace(policy, read_content=False))
    assert metadata.metadata_inventory_digest == scoped
    (root / "node_modules" / "vendor" / "new.js").write_bytes(b"new generated file")
    reasons, observations = runtime._metadata_inventory_freshness_for_query(
        {"repository_metadata_inventory_digest": scoped,
         "repository_metadata_inventory_scope": "source-tree"}, root,
    )
    assert reasons == []
    assert observations["metadata_inventory_current"] is True
    reasons, _ = runtime._metadata_inventory_freshness_for_query(
        {"repository_metadata_inventory_digest": full}, root,
    )
    assert reasons == ["project-source-inventory-stale"]
    (root / "src" / "main.py").write_text("print('changed source content')\n", encoding="utf-8")
    assert not runtime._metadata_inventory_freshness_signal(
        root, declared_digest=scoped, inventory_scope="source-tree"
    )["metadata_inventory_current"]


@pytest.mark.parametrize("scope", [None, [], "unknown-scope"])
def test_invalid_scope_fails_closed_without_discovery(tmp_path, monkeypatch, scope):
    monkeypatch.setattr(runtime, "discover", lambda *a, **k: pytest.fail("must not scan"))
    assert runtime._metadata_inventory_freshness_signal(
        tmp_path, declared_digest="sha256:" + "a" * 64, inventory_scope=scope
    ) == {"metadata_inventory_unknown": True}


def test_project_binding_and_index_plan_use_the_same_source_scope(tmp_path, monkeypatch):
    root = _source(tmp_path)
    monkeypatch.setattr(runtime, "_registered_project", lambda *args: UUID(int=1))
    monkeypatch.setattr(runtime, "_git_source_state", lambda *a, **k: ("a" * 40, "b" * 64, "revision"))
    monkeypatch.setattr(runtime, "PROJECT_SOURCE_DISCOVERY_POLICY", DiscoveryPolicy(
        max_inventory_entries=12, include_ignored_descendants=False,
    ))
    binding = runtime.build_project_source_binding_plan(tmp_path / "home", "project", root)
    report, plan = runtime._project_plan(root, project_id=UUID(int=1), project_slug="project")
    assert binding["metadata_inventory_scope"] == "source-tree"
    assert binding["metadata_inventory_digest"] == report.metadata_inventory_digest
    assert plan.selected_file_count == 2
    assert plan.discovery.inventory_scope == "source-tree"


def test_source_scope_still_rejects_an_incomplete_source_tree(tmp_path, monkeypatch):
    root = _source(tmp_path)
    for number in range(20):
        (root / "src" / f"source-{number}.py").write_text("print('source')\n")
    monkeypatch.setattr(runtime, "_git_source_state", lambda *a, **k: ("a" * 40, "b" * 64, "revision"))
    monkeypatch.setattr(runtime, "PROJECT_SOURCE_DISCOVERY_POLICY", DiscoveryPolicy(
        max_inventory_entries=12, include_ignored_descendants=False,
    ))
    with pytest.raises(PolicyViolation, match="partial"):
        runtime._project_plan(root, project_id=UUID(int=1), project_slug="project")


@pytest.mark.parametrize("value", [None, 0, "false"])
def test_scope_policy_requires_a_boolean(value):
    with pytest.raises(ValueError, match="include_ignored_descendants"):
        DiscoveryPolicy(include_ignored_descendants=value)
