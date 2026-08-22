"""Yerel icerik adresli nesne deposu davranisi."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from zekam.domain.canonical import DIGEST_PREFIX
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed
from zekam.infrastructure.storage.local_cas import (
    CONTENT_SUFFIX,
    IntegrityError,
    LocalContentAddressedStore,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path) -> LocalContentAddressedStore:
    return LocalContentAddressedStore(tmp_path / "artifacts").ensure()


def test_put_returns_content_digest(store: LocalContentAddressedStore) -> None:
    info = store.put(b"zekam")
    assert info.digest == DIGEST_PREFIX + hashlib.sha256(b"zekam").hexdigest()
    assert info.size_bytes == len(b"zekam")


def test_get_returns_original_bytes(store: LocalContentAddressedStore) -> None:
    info = store.put(b"kanit tabanli")
    assert store.get(info.digest) == b"kanit tabanli"


def test_put_is_idempotent_for_identical_content(store: LocalContentAddressedStore) -> None:
    first = store.put(b"ayni")
    second = store.put(b"ayni")
    assert first.digest == second.digest
    assert first.stored_at == second.stored_at
    assert len(list(store.iter_objects())) == 1


def test_different_content_gets_different_address(store: LocalContentAddressedStore) -> None:
    assert store.put(b"bir").digest != store.put(b"iki").digest
    assert len(list(store.iter_objects())) == 2


def test_metadata_is_persisted_and_sorted(store: LocalContentAddressedStore) -> None:
    info = store.put(b"veri", media_type="text/plain", metadata={"z": "1", "a": "2"})
    stored = store.stat(info.digest)
    assert stored.media_type == "text/plain"
    assert list(stored.metadata) == ["a", "z"]


def test_metadata_rejects_secret_keys(store: LocalContentAddressedStore) -> None:
    with pytest.raises(PolicyViolation):
        store.put(b"veri", metadata={"api_key": "gizli"})


def test_metadata_rejects_non_string_values(store: LocalContentAddressedStore) -> None:
    with pytest.raises(ValidationFailed):
        store.put(b"veri", metadata={"sayi": 1})  # type: ignore[dict-item]


def test_missing_object_raises_not_found(store: LocalContentAddressedStore) -> None:
    with pytest.raises(NotFound):
        store.get(DIGEST_PREFIX + "0" * 64)


def test_invalid_digest_is_rejected(store: LocalContentAddressedStore) -> None:
    with pytest.raises(ValidationFailed):
        store.get("md5:abc")


def test_corrupted_content_is_detected(store: LocalContentAddressedStore) -> None:
    info = store.put(b"saglam")
    hexadecimal = info.digest.removeprefix(DIGEST_PREFIX)
    target = (
        store.root
        / "sha256"
        / hexadecimal[:2]
        / hexadecimal[2:4]
        / f"{hexadecimal}{CONTENT_SUFFIX}"
    )
    target.write_bytes(b"bozuk")
    with pytest.raises(IntegrityError):
        store.get(info.digest)
    assert store.verify_all() == (info.digest,)


def test_verify_all_is_clean_for_healthy_store(store: LocalContentAddressedStore) -> None:
    store.put(b"bir")
    store.put(b"iki")
    assert store.verify_all() == ()


def test_no_partial_file_remains_after_write(store: LocalContentAddressedStore) -> None:
    store.put(b"tam")
    leftovers = list(store.root.rglob(".tmp-*"))
    assert leftovers == []


def test_put_file_reads_from_disk(store: LocalContentAddressedStore, tmp_path: Path) -> None:
    source = tmp_path / "girdi.txt"
    source.write_bytes(b"dosyadan")
    info = store.put_file(source, media_type="text/plain")
    assert store.get(info.digest) == b"dosyadan"


def test_exists_reflects_presence(store: LocalContentAddressedStore) -> None:
    info = store.put(b"var")
    assert store.exists(info.digest)
    assert not store.exists(DIGEST_PREFIX + "1" * 64)


def test_delete_removes_content_and_metadata(store: LocalContentAddressedStore) -> None:
    info = store.put(b"silinecek", metadata={"kaynak": "test"})
    assert store.delete(info.digest) is True
    assert store.delete(info.digest) is False
    assert not store.exists(info.digest)
    assert list(store.iter_objects()) == []


def test_iter_objects_is_sorted_by_digest(store: LocalContentAddressedStore) -> None:
    digests = sorted(store.put(bytes([index])).digest for index in range(5))
    assert [info.digest for info in store.iter_objects()] == digests


def test_ensure_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    LocalContentAddressedStore(root).ensure()
    info = LocalContentAddressedStore(root).ensure().put(b"korunmali")
    LocalContentAddressedStore(root).ensure()
    assert LocalContentAddressedStore(root).get(info.digest) == b"korunmali"
