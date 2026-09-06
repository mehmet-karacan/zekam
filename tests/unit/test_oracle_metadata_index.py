from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.oracle_metadata_index import (
    OracleDdlObject,
    OracleMetadataSnapshot,
    build_oracle_metadata_index_plan,
    load_project_oracle_datasource,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ConfigurationError, PolicyViolation


def _config(password: str = "not-logged-secret") -> str:
    return f"""
spring:
  datasource:
    url: jdbc:oracle:thin:@//db.internal.example:1521/GPU
    username: GPU_APP
    password: {password}
app:
  schema:
    name: GPU_APP
"""


def test_project_oracle_datasource_is_sanitized(tmp_path: Path) -> None:
    config = tmp_path / "application-local.yaml"
    config.write_text(_config(), encoding="utf-8")

    datasource = load_project_oracle_datasource(tmp_path, "application-local.yaml")

    assert datasource.schema_name == "GPU_APP"
    assert datasource.dsn == "db.internal.example:1521/GPU"
    assert datasource.password == "not-logged-secret"
    assert "not-logged-secret" not in repr(datasource)
    assert "not-logged-secret" not in str(datasource.sanitized())
    assert "db.internal.example" not in str(datasource.sanitized())


def test_project_oracle_datasource_rejects_duplicate_and_traversal(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(_config() + "app: {}\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="duplicate"):
        load_project_oracle_datasource(tmp_path, "duplicate.yaml")
    with pytest.raises(PolicyViolation, match="traversal"):
        load_project_oracle_datasource(tmp_path, "../outside.yaml")


def test_project_oracle_datasource_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    target.write_text(_config(), encoding="utf-8")
    link = tmp_path / "link.yaml"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Windows symlink yetkisi yok")

    with pytest.raises(ConfigurationError, match="link/reparse"):
        load_project_oracle_datasource(tmp_path, "link.yaml")


def test_oracle_metadata_plan_is_ddl_digest_bound_and_local_only() -> None:
    ddl = 'CREATE TABLE "GPU_APP"."PRODUCT" ("ID" NUMBER NOT NULL)'
    object_metadata = OracleDdlObject(
        owner="GPU_APP",
        object_name="PRODUCT",
        object_type="TABLE",
        status="VALID",
        last_ddl_at="2026-08-21T12:00:00",
        ddl_digest=digest_of_bytes(ddl.encode()),
        _ddl=ddl,
    )
    snapshot = OracleMetadataSnapshot(
        schema_name="GPU_APP",
        connection_identity_digest=digest({"connection": "reviewed"}),
        database_identity_digest=digest({"database": "reviewed"}),
        objects=(object_metadata,),
        excluded_secret_objects=0,
    )

    plan = build_oracle_metadata_index_plan(
        project_id=uuid4(), project_slug="gpu-fusion", snapshot=snapshot
    )

    assert plan.document.unit_count == 1
    assert len(plan.chunks) == 1
    assert plan.chunks[0].locator.object_name == "GPU_APP.PRODUCT:TABLE"
    assert plan.embedding_profile.dimension == 1024
    assert plan.as_dict()["row_data_included"] is False
    embedding = plan.as_dict()["embedding"]
    assert isinstance(embedding, dict)
    assert embedding["remote_provider_used"] is False

    refreshed = build_oracle_metadata_index_plan(
        project_id=plan.project_id,
        project_slug="gpu-fusion",
        snapshot=replace(snapshot, database_identity_digest=digest({"database": "refreshed"})),
    )
    assert refreshed.snapshot.revision_digest != plan.snapshot.revision_digest
    assert refreshed.chunks[0].chunk_id == plan.chunks[0].chunk_id
