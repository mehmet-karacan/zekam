from __future__ import annotations

import os
from pathlib import Path

import pytest

import scripts.install_local_e5_model as installer

pytestmark = pytest.mark.unit


def test_installer_rejects_a_reparse_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "redirected" / installer.MODEL_REVISION
    try:
        os.symlink(outside, root.parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink olusturulamadi: {exc}")

    with pytest.raises(ValueError, match="parent symlink/reparse"):
        installer._assert_safe_root(root, create=True)
