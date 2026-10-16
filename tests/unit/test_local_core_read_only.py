"""Read-only composition cannot provision key material or runtime config."""

from __future__ import annotations

import sqlite3

import pytest

from zekam.application.composition import build_context
from zekam.application.home import HomeLayout
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.infrastructure.local_file_security import restrict_private_tree
from zekam.infrastructure.skill_receipt_authority import (
    load_existing_skill_runtime_authority,
    load_or_create_skill_runtime_authority,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap


def _home(tmp_path, *, admit_runtime=True):
    home = HomeLayout(tmp_path / "home").ensure().root
    restrict_private_tree(home / "state")
    database = home / "state" / "operational.db"
    bootstrap(database)
    if admit_runtime:
        SQLiteLocalRuntimeStore(database)
    return home, database


def test_read_only_composition_never_creates_missing_key_or_extensions(tmp_path):
    home, database = _home(tmp_path)
    before = database.read_bytes()
    paths_before = {path.relative_to(home).as_posix() for path in home.rglob("*")}
    services = LocalCoreServices.from_context(build_context(home=home), read_only=True)
    report = services.status()
    assert services.skill_runtime_signer is None
    assert services.learning.skill_runtime_verifier is None
    assert services.runtime.existing_only is True
    assert report["all_ready"] is False
    assert not (home / "state" / "skill-receipt.key").exists()
    assert database.read_bytes() == before
    assert {path.relative_to(home).as_posix() for path in home.rglob("*")} == paths_before


def test_missing_admitted_config_is_not_inserted_by_composition(tmp_path):
    home, database = _home(tmp_path, admit_runtime=False)
    before = database.read_bytes()
    with pytest.raises(PolicyViolation, match="existing admitted config"):
        LocalCoreServices.from_context(build_context(home=home), read_only=True)
    assert database.read_bytes() == before
    assert not (home / "state" / "skill-receipt.key").exists()
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as db:
        assert db.execute("select count(*) from local_runtime_config").fetchone()[0] == 0


def test_existing_key_load_preserves_receipt_verification_without_rotation(tmp_path):
    home, _database = _home(tmp_path)
    key = home / "state" / "skill-receipt.key"
    original, _verifier = load_or_create_skill_runtime_authority(key)
    before = key.read_bytes()
    body = {"schema": "offline-receipt-fixture/v1"}
    expected = original.attest(body)
    services = LocalCoreServices.from_context(build_context(home=home), read_only=True)
    assert services.skill_runtime_signer is not None
    assert services.skill_runtime_signer.attest(body) == expected
    assert services.learning.skill_runtime_verifier is not None
    assert key.read_bytes() == before


def test_existing_only_loader_does_not_generate_missing_material(tmp_path):
    home, _database = _home(tmp_path)
    key = home / "state" / "skill-receipt.key"
    with pytest.raises(PolicyViolation, match="mevcut private dosya"):
        load_existing_skill_runtime_authority(key)
    assert not key.exists()
