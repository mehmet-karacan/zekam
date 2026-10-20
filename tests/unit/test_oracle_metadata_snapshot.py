"""Synthetic pytest coverage for zekam.application.oracle_metadata_snapshot (pure file fixtures)."""

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from zekam.application.oracle_metadata_index import build_oracle_metadata_index_plan
from zekam.application.oracle_metadata_snapshot import (
    load_oracle_metadata_snapshot,
    oracle_snapshot_freshness,
)
from zekam.domain.errors import ZekamError

SNAP = "snap"

COLUMN = {
    "name": "ID", "position": 1, "type": "NUMBER", "nullable": False,
    "byteLength": 22, "charLength": 0, "charSemantics": None,
    "precision": None, "scale": None,
    "sourceRef": "oracle://SAMPLE/TABLE/CHILD/COLUMN/ID",
}
CONSTRAINT = {
    "name": "PK_CHILD", "kind": "P", "columns": ["ID"], "references": None,
    "referenceColumns": [], "sourceRef": "oracle://SAMPLE/TABLE/CHILD/CONSTRAINT/PK_CHILD",
}
INDEX = {
    "name": "IX_CHILD", "kind": "NORMAL", "columns": ["ID"], "unique": True,
    "expressionOmitted": False, "status": "VALID", "visibility": "VISIBLE",
    "sourceRef": "oracle://SAMPLE/TABLE/CHILD/INDEX/IX_CHILD",
}
METADATA = {
    "formatVersion": 2, "applicationVersion": "1.0.0", "schema": "SAMPLE",
    "database": "TEST",
    "tables": [{"name": "CHILD", "sourceRef": "oracle://SAMPLE/TABLE/CHILD",
                "columns": [COLUMN], "constraints": [CONSTRAINT], "indexes": [INDEX]}],
}
COUNTS = {"tables": 1, "columns": 1, "constraints": 1, "indexes": 1}

PARENT_COLUMN = {
    "name": "ID", "position": 1, "type": "NUMBER", "nullable": False,
    "byteLength": 22, "charLength": 0, "charSemantics": None,
    "precision": None, "scale": None,
    "sourceRef": "oracle://SAMPLE/TABLE/PARENT/COLUMN/ID",
}
FK = {
    "name": "FK_CHILD", "kind": "R", "columns": ["ID"],
    "references": "PARENT", "referenceColumns": ["ID"],
    "sourceRef": "oracle://SAMPLE/TABLE/CHILD/CONSTRAINT/FK_CHILD",
}
METADATA_FK = {
    "formatVersion": 2, "applicationVersion": "1.0.0", "schema": "SAMPLE",
    "database": "TEST",
    "tables": [
        {"name": "PARENT", "sourceRef": "oracle://SAMPLE/TABLE/PARENT",
         "columns": [PARENT_COLUMN], "constraints": [], "indexes": []},
        {"name": "CHILD", "sourceRef": "oracle://SAMPLE/TABLE/CHILD",
         "columns": [COLUMN], "constraints": [CONSTRAINT, FK], "indexes": [INDEX]},
    ],
}
COUNTS_FK = {"tables": 2, "columns": 2, "constraints": 2, "indexes": 1}


def _raw(metadata: dict) -> bytes:
    return json.dumps(metadata, ensure_ascii=False, indent=1).encode("utf-8")


def _manifest(raw: bytes, counts: dict) -> dict:
    return {"formatVersion": 2, "schema": "SAMPLE", "database": "TEST",
            "applicationVersion": "1.0.0", "metadataSha256": hashlib.sha256(raw).hexdigest(),
            "counts": dict(counts)}


def _write_snapshot(root: Path, metadata: dict, *, mutate=None, counts=None) -> bytes:
    raw = _raw(metadata)
    snap = root / SNAP
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "metadata.json").write_bytes(raw)
    manifest = _manifest(raw, counts if counts is not None else dict(COUNTS))
    if mutate is not None:
        mutate(manifest)
    (snap / "manifest.json").write_bytes(json.dumps(manifest).encode("utf-8"))
    return raw


def _state(directory: str, revision: str) -> dict:
    return {"oracle_snapshot_source": directory, "oracle_snapshot_revision_digest": revision}


@pytest.fixture()
def snapshot_root(tmp_path: Path) -> Path:
    _write_snapshot(tmp_path, METADATA)
    return tmp_path


def _load(root: Path, relative: str = SNAP):
    return load_oracle_metadata_snapshot(root, relative)


def test_fresh_snapshot_summary(snapshot_root: Path) -> None:
    snapshot = _load(snapshot_root)
    assert len(snapshot.objects) == 1
    assert snapshot.schema_name == "SAMPLE"
    text = "\n".join(obj.ddl for obj in snapshot.objects)
    assert "ID" in text
    assert "NUMBER" in text
    assert "IX_CHILD" in text
    assert "PK_CHILD" in text
    summary = snapshot.sanitized()
    assert summary["invalid_object_count"] == 0
    assert summary["unknown_object_count"] == 1


def test_index_plan_chunks(snapshot_root: Path) -> None:
    plan = build_oracle_metadata_index_plan(
        project_id=uuid.UUID("00000000-0000-4000-8000-000000000000"),
        project_slug="demo",
        snapshot=_load(snapshot_root),
    )
    chunks = plan.chunks
    assert isinstance(chunks, tuple)
    assert chunks
    assert all(c.locator.object_name == "SAMPLE.CHILD:TABLE" for c in chunks)
    assert plan.embedding_profile.dimension == 384
    ids = [c.chunk_id for c in chunks]
    assert len(set(ids)) == len(ids)  # chunk UUIDs are random: uniqueness only


def test_bad_manifest_hash_rejected(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, METADATA, mutate=lambda m: m.update(metadataSha256="0" * 64))
    with pytest.raises(ZekamError):
        _load(tmp_path)


def test_counts_mismatch_rejected(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, METADATA, mutate=lambda m: m["counts"].update(columns=2))
    with pytest.raises(ZekamError):
        _load(tmp_path)


def test_duplicate_json_key_rejected(tmp_path: Path) -> None:
    marker = '"schema": "SAMPLE"'
    text = _raw(METADATA).decode("utf-8").replace(marker, marker + ", " + marker, 1)
    snap = tmp_path / SNAP
    snap.mkdir()
    (snap / "metadata.json").write_bytes(text.encode("utf-8"))
    (snap / "manifest.json").write_bytes(json.dumps(_manifest(text.encode("utf-8"), dict(COUNTS))).encode("utf-8"))
    with pytest.raises(ZekamError):
        _load(tmp_path)


def test_traversal_and_absolute_path_rejected(snapshot_root: Path) -> None:
    with pytest.raises(ZekamError):
        _load(snapshot_root, "../snap")
    with pytest.raises(ZekamError):
        _load(snapshot_root, str(snapshot_root / SNAP))


def _dup_columns(md: dict) -> None:
    dup = dict(md["tables"][0]["columns"][0])
    dup["position"] = 2
    dup["sourceRef"] = "oracle://SAMPLE/TABLE/CHILD/COLUMN/ID2"
    md["tables"][0]["columns"].append(dup)


REJECTIONS = [
    (lambda md: md["tables"][0]["columns"][0].update(sourceRef="oracle://SAMPLE/TABLE/OTHER/COLUMN/ID"), 1),
    (lambda md: md["tables"][0].update(rows=[["1"]]), 1),
    (lambda md: md["tables"][0]["columns"][0].update(type="NUMBER\nDROP TABLE X"), 1),
    (_dup_columns, 2),
]


@pytest.mark.parametrize("mutate,columns", REJECTIONS,
                         ids=["source-ref-mismatch", "unexpected-rows-field", "unknown-type-newline", "duplicate-column-ids"])
def test_metadata_rejections(tmp_path: Path, mutate, columns: int) -> None:
    metadata = json.loads(json.dumps(METADATA))
    mutate(metadata)
    _write_snapshot(tmp_path, metadata, mutate=lambda m: m["counts"].update(columns=columns))
    with pytest.raises(ZekamError):
        _load(tmp_path)


def test_pinned_snapshot_stale_revision(tmp_path: Path) -> None:
    changed = json.loads(json.dumps(METADATA))
    changed["tables"][0]["columns"][0]["nullable"] = True
    _write_snapshot(tmp_path, METADATA)
    old_snapshot = _load(tmp_path)
    old_revision = old_snapshot.revision_digest
    _write_snapshot(tmp_path, changed)  # manifest hash rebuilt over edited content
    issues = oracle_snapshot_freshness(
        _state(SNAP, old_revision), tmp_path)
    assert issues


def test_missing_source_unavailable(tmp_path: Path) -> None:
    issues = oracle_snapshot_freshness(_state("gone", "sha256:" + "0" * 64), tmp_path)
    assert issues


def test_no_bound_snapshot_returns_empty(snapshot_root: Path) -> None:
    assert oracle_snapshot_freshness({}, snapshot_root) == []
    assert oracle_snapshot_freshness({}, None) == []


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    _write_snapshot(tmp_path / "outside", METADATA)
    link = tmp_path / "snaplink"
    try:
        link.symlink_to(tmp_path / "outside", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this platform")
    with pytest.raises(ZekamError):
        _load(tmp_path, "snaplink")


def test_fk_snapshot_summary(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, METADATA_FK, counts=COUNTS_FK)
    snapshot = _load(tmp_path)
    assert len(snapshot.objects) == 2
    assert snapshot.schema_name == "SAMPLE"
    text = "\n".join(obj.ddl for obj in snapshot.objects)
    assert "ID" in text
    assert "NUMBER" in text
    assert "IX_CHILD" in text
    assert "PK_CHILD" in text


def test_index_status_na_roundtrip(tmp_path: Path) -> None:
    import copy
    metadata = copy.deepcopy(METADATA)
    metadata = json.loads(json.dumps(metadata))
    metadata["tables"][0]["indexes"][0]["status"] = "N/A"
    _write_snapshot(tmp_path, metadata)
    snapshot = _load(tmp_path)
    assert "status=N/A" in snapshot.objects[0].ddl
