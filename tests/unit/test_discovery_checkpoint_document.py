"""Persisted discovery frontier keeps root and metadata binding."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from zekam.application.source_discovery import (
    DiscoveryPolicy,
    InventoryStatus,
    checkpoint_document,
    checkpoint_from_document,
    discover,
    resume_discovery,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation


def _partial(root: Path):
    root.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / name).write_text(name, encoding="utf-8")
    return discover(root, policy=DiscoveryPolicy(read_content=False, max_inventory_entries=2))


def test_json_checkpoint_roundtrip_and_fresh_complete_traversal(tmp_path: Path) -> None:
    root = tmp_path / "source"
    partial = _partial(root)
    document = json.loads(json.dumps(partial.as_dict()))["inventory"]["resume_checkpoint"]
    assert document["grants_authority"] is False
    restored = checkpoint_from_document(document)
    assert checkpoint_document(restored) == document
    complete = resume_discovery(root, document, policy=DiscoveryPolicy(read_content=False))
    assert complete.inventory_status is InventoryStatus.COMPLETE
    assert complete.inventory_file_paths == ("a.txt", "b.txt", "c.txt")
    assert complete.as_dict()["inventory"]["resume_checkpoint"] is None
    assert complete.files == ()


def test_persisted_checkpoint_rejects_foreign_root_and_frontier_drift(tmp_path: Path) -> None:
    root = tmp_path / "source"
    partial = _partial(root)
    document = json.loads(json.dumps(checkpoint_document(partial.inventory_checkpoint)))
    foreign = tmp_path / "foreign"
    _partial(foreign)
    with pytest.raises(PolicyViolation, match="root drift"):
        resume_discovery(foreign, document)
    frontier = root / document["paths"][0]
    if frontier.is_file():
        frontier.write_text("changed length", encoding="utf-8")
    else:
        # A root-directory frontier is valid, but its metadata must stay bound.
        import os

        metadata = frontier.stat()
        os.utime(frontier, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 10_000_000))
    with pytest.raises(PolicyViolation, match="source drift"):
        resume_discovery(root, document)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(grants_authority=True),
        lambda d: d.update(extra="grant"),
        lambda d: d.update(root_identity_digest=None),
        lambda d: d.update(paths=["../outside"]),
        lambda d: d.update(paths=["/outside"]),
        lambda d: d.update(paths=["C:\\outside"]),
        lambda d: d.update(paths=["a", "b"]),
        lambda d: d["source_snapshot"][0].update(path="other"),
        lambda d: d["source_snapshot"][0].update(size_bytes=True),
        lambda d: d["source_snapshot"][0].update(mtime_ns=False),
        lambda d: d["source_snapshot"][0].update(kind=[]),
    ],
)
def test_checkpoint_structural_rejections_even_with_recomputed_digest(
    tmp_path: Path, mutation
) -> None:
    document = copy.deepcopy(
        checkpoint_document(_partial(tmp_path / "source").inventory_checkpoint)
    )
    mutation(document)
    document["checkpoint_digest"] = digest(
        {k: v for k, v in document.items() if k != "checkpoint_digest"}
    )
    with pytest.raises(PolicyViolation):
        checkpoint_from_document(document)


def test_checkpoint_tamper_and_unbound_legacy_tuple_rejected(tmp_path: Path) -> None:
    root = tmp_path / "source"
    partial = _partial(root)
    document = checkpoint_document(partial.inventory_checkpoint)
    document["source_snapshot"][0]["mtime_ns"] += 1
    with pytest.raises(PolicyViolation, match="digest drift"):
        checkpoint_from_document(document)
    with pytest.raises(PolicyViolation, match="root binding"):
        resume_discovery(root, tuple(partial.inventory_checkpoint))


def test_root_frontier_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "a").write_text("a", encoding="utf-8")
    (root / "b").write_text("b", encoding="utf-8")
    partial = discover(root, policy=DiscoveryPolicy(read_content=False, max_dir_entries=1))
    document = checkpoint_document(partial.inventory_checkpoint)
    assert document["paths"] == ["."]
    complete = resume_discovery(
        root, json.loads(json.dumps(document)), policy=DiscoveryPolicy(read_content=False)
    )
    assert complete.inventory_status is InventoryStatus.COMPLETE
