from __future__ import annotations

import os
import json
from pathlib import Path

import pytest

from zekam.domain.errors import PolicyViolation
from zekam.domain.canonical import digest
from zekam.infrastructure import skill_receipt_authority as authority_module
from zekam.infrastructure.local_file_security import (
    restrict_private_file,
    restrict_private_tree,
)


def _private_root(tmp_path: Path, name: str) -> Path:
    root = (tmp_path / name).resolve()
    root.mkdir(mode=0o700)
    restrict_private_tree(root)
    return root


def test_authority_loader_handles_repeated_short_descriptor_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _private_root(tmp_path, "short-read") / "skill-receipt.key"
    first_signer, _first_verifier = authority_module.load_or_create_skill_runtime_authority(path)
    body = {"schema": "skill-authority-test/v1"}
    expected = first_signer.attest(body)
    original_read = os.read

    def short_read(descriptor: int, count: int) -> bytes:
        return original_read(descriptor, min(count, 1))

    monkeypatch.setattr(authority_module.os, "read", short_read)
    second_signer, _second_verifier = authority_module.load_or_create_skill_runtime_authority(path)

    assert second_signer.attest(body) == expected


def test_authority_loader_rejects_create_time_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _private_root(tmp_path, "replacement") / "skill-receipt.key"

    def replace_after_acl(target: Path, *, mode: int = 0o600) -> None:
        restrict_private_file(target, mode=mode)
        target.unlink()
        target.write_bytes(b"R" * 32)
        restrict_private_file(target, mode=mode)

    monkeypatch.setattr(authority_module, "restrict_private_file", replace_after_acl)

    with pytest.raises(PolicyViolation, match="identity/content drift"):
        authority_module.load_or_create_skill_runtime_authority(path)


def test_explicit_key_restore_preserves_verification_without_exporting_material(
    tmp_path: Path,
) -> None:
    source = _private_root(tmp_path, "source-key") / "skill-receipt.key"
    target = _private_root(tmp_path, "target-key") / "skill-receipt.key"
    signer, _ = authority_module.load_or_create_skill_runtime_authority(source)
    body = {"schema": "fixture-signed-receipt/v1", "subject": digest("fixture-subject")}
    evidence = digest(body | {"runtime_attestation": signer.attest(body)})
    receipt = authority_module.restore_skill_runtime_authority(
        source, target, expected_binding_digest=signer.key_binding_digest
    )
    restored_signer, verifier = authority_module.load_or_create_skill_runtime_authority(target)
    assert verifier.matches_receipt_digest(body, evidence)
    assert restored_signer.key_binding_digest == signer.key_binding_digest
    assert source.read_bytes() == target.read_bytes()
    assert source.read_bytes().hex() not in json.dumps(receipt)
    assert receipt["grants_authority"] is False
    with pytest.raises(FileExistsError):
        authority_module.restore_skill_runtime_authority(
            source, target, expected_binding_digest=signer.key_binding_digest
        )


def test_key_restore_missing_or_wrong_material_does_not_create_files(tmp_path: Path) -> None:
    root = _private_root(tmp_path, "restore-rejections")
    source, target = root / "source.key", root / "target.key"
    with pytest.raises(PolicyViolation, match="mevcut"):
        authority_module.restore_skill_runtime_authority(
            source, target, expected_binding_digest=digest("missing")
        )
    assert not source.exists() and not target.exists()
    authority_module.load_or_create_skill_runtime_authority(source)
    with pytest.raises(PolicyViolation, match="binding mismatch"):
        authority_module.restore_skill_runtime_authority(
            source, target, expected_binding_digest=digest("wrong")
        )
    assert not target.exists()
