"""Yedek manifesti uretimi ve dogrulamasi."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from zekam.application.backup import (
    BACKUP_MANIFEST_SCHEMA,
    BackupManifest,
    VerificationOutcome,
    build_manifest,
    schema_state_from_status,
    verify_manifest,
)
from zekam.domain.canonical import DIGEST_PREFIX
from zekam.infrastructure.storage.local_cas import CONTENT_SUFFIX, LocalContentAddressedStore

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)
SCHEMA_STATE = schema_state_from_status(
    2, [(1, "core_baseline", "a" * 64), (2, "append_only_revision_event", "b" * 64)]
)
CONFIGURATION = {"database": {"name": "zekam", "port": 5433}}


@pytest.fixture
def store(tmp_path: Path) -> LocalContentAddressedStore:
    return LocalContentAddressedStore(tmp_path / "artifacts").ensure()


def _manifest(store: LocalContentAddressedStore) -> BackupManifest:
    return build_manifest(
        schema_state=SCHEMA_STATE, store=store, configuration=CONFIGURATION, now=NOW
    )


def test_manifest_declares_schema_and_product(store: LocalContentAddressedStore) -> None:
    manifest = _manifest(store)
    assert manifest.schema == BACKUP_MANIFEST_SCHEMA
    assert manifest.product == "Zekam"
    assert manifest.created_at == NOW


def test_manifest_lists_every_artifact(store: LocalContentAddressedStore) -> None:
    store.put(b"bir")
    store.put(b"iki")
    manifest = _manifest(store)
    assert len(manifest.artifacts) == 2
    assert manifest.total_bytes == 6
    assert [entry.digest for entry in manifest.artifacts] == sorted(
        entry.digest for entry in manifest.artifacts
    )


def test_manifest_digest_is_deterministic(store: LocalContentAddressedStore) -> None:
    store.put(b"veri")
    assert _manifest(store).manifest_digest == _manifest(store).manifest_digest


def test_manifest_digest_changes_with_content(store: LocalContentAddressedStore) -> None:
    before = _manifest(store).manifest_digest
    store.put(b"yeni")
    assert _manifest(store).manifest_digest != before


def test_manifest_records_schema_head_and_checksums(store: LocalContentAddressedStore) -> None:
    document = _manifest(store).as_dict()
    assert document["schema_state"]["head"] == 2
    assert [item["version"] for item in document["schema_state"]["migrations"]] == [1, 2]


def test_manifest_contains_no_secret_or_absolute_path(store: LocalContentAddressedStore) -> None:
    rendered = repr(_manifest(store).as_dict()).lower()
    for forbidden in ("password", "token", "secret", "c:\\", "/home/"):
        assert forbidden not in rendered


def test_configuration_digest_reflects_configuration(store: LocalContentAddressedStore) -> None:
    other = build_manifest(
        schema_state=SCHEMA_STATE, store=store, configuration={"database": {}}, now=NOW
    )
    assert other.configuration_digest != _manifest(store).configuration_digest


def test_verification_is_valid_for_intact_store(store: LocalContentAddressedStore) -> None:
    store.put(b"saglam")
    result = verify_manifest(_manifest(store), store)
    assert result.outcome is VerificationOutcome.VALID
    assert result.is_valid


def test_verification_detects_missing_artifact(store: LocalContentAddressedStore) -> None:
    info = store.put(b"silinecek")
    manifest = _manifest(store)
    store.delete(info.digest)
    result = verify_manifest(manifest, store)
    assert result.outcome is VerificationOutcome.INCOMPLETE
    assert result.missing == (info.digest,)


def test_verification_detects_corrupt_artifact(store: LocalContentAddressedStore) -> None:
    info = store.put(b"saglam")
    manifest = _manifest(store)
    hexadecimal = info.digest.removeprefix(DIGEST_PREFIX)
    target = (
        store.root
        / "sha256"
        / hexadecimal[:2]
        / hexadecimal[2:4]
        / f"{hexadecimal}{CONTENT_SUFFIX}"
    )
    target.write_bytes(b"bozulmus icerik")
    result = verify_manifest(manifest, store)
    assert result.outcome is VerificationOutcome.CORRUPT
    assert result.corrupt == (info.digest,)


def test_verification_detects_altered_manifest(store: LocalContentAddressedStore) -> None:
    manifest = _manifest(store)
    tampered = BackupManifest(
        schema=manifest.schema,
        product=manifest.product,
        product_version="99.0.0",
        created_at=manifest.created_at,
        schema_state=manifest.schema_state,
        artifacts=manifest.artifacts,
        configuration_digest=manifest.configuration_digest,
        manifest_digest=manifest.manifest_digest,
    )
    assert verify_manifest(tampered, store).outcome is VerificationOutcome.ALTERED


def test_empty_store_produces_valid_empty_manifest(store: LocalContentAddressedStore) -> None:
    manifest = _manifest(store)
    assert manifest.artifacts == ()
    assert manifest.total_bytes == 0
    assert verify_manifest(manifest, store).is_valid
