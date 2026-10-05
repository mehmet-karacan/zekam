"""Continuity bounded JSON okuyucusu: Windows'ta (os.O_NOFOLLOW yok) calisir, baglanti reddeder."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.interfaces.cli import continuity


def test_platform_flags_are_resolved_without_attribute_error() -> None:
    # Windows'ta os.O_NOFOLLOW yoktur; modul import/okuma yolu bunu getattr ile gecmelidir.
    if sys.platform == "win32":
        assert not hasattr(os, "O_NOFOLLOW")
    expected_extra = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    assert continuity._OPEN_FLAGS & expected_extra == expected_extra


def test_bounded_regular_file_is_read_with_identity(tmp_path: Path) -> None:
    path = tmp_path / "ozet.json"
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    document = continuity._json_document(path)
    assert document.body == {"a": 1}
    assert len(document.identity) == 4


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        pytest.param(b"", "bounded regular file", id="empty"),
        pytest.param(b"x" * 40000, "bounded regular file", id="oversized"),
        pytest.param(b'{"a":1,"a":2}', "duplicate JSON key", id="duplicate-key"),
        pytest.param(b'{"a":NaN}', "nonfinite", id="nan"),
        pytest.param(b"[1]", "object required", id="not-object"),
    ],
)
def test_malformed_or_oversized_documents_are_rejected(
    tmp_path: Path, payload: bytes, reason: str
) -> None:
    path = tmp_path / "bozuk.json"
    path.write_bytes(payload)
    with pytest.raises(ValidationFailed, match=reason):
        continuity._json_document(path)


def test_directory_is_not_a_regular_file(tmp_path: Path) -> None:
    with pytest.raises((ValidationFailed, OSError)):
        continuity._json_document(tmp_path)


def test_symlink_document_is_rejected_on_every_platform(tmp_path: Path) -> None:
    target = tmp_path / "gercek.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("dosya symlink'i kurulamadi (ayricalik yok)")
    with pytest.raises(PolicyViolation, match="link or reparse"):
        continuity._json_document(link)


def test_junction_or_directory_link_is_rejected_as_reparse(tmp_path: Path) -> None:
    target = tmp_path / "hedef"
    target.mkdir()
    link = tmp_path / "baglanti"
    if sys.platform == "win32":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if made.returncode != 0:
            pytest.skip("junction kurulamadi")
    else:
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            pytest.skip("symlink kurulamadi")
    with pytest.raises(PolicyViolation, match="link or reparse"):
        continuity._json_document(link)
