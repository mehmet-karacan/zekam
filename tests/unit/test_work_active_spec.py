from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from zekam.domain.errors import ZekamError
from zekam.interfaces.cli.work import _read_active_spec


def _write(tmp_path: Path, text: str) -> tuple[Path, str]:
    payload = text.encode("utf-8")
    path = tmp_path / "AKTIF_GOREV.md"
    path.write_bytes(payload)
    return path, f"sha256:{hashlib.sha256(payload).hexdigest()}"


def test_active_spec_preserves_utf8_title_and_exact_completion_criteria(tmp_path: Path) -> None:
    path, input_digest = _write(
        tmp_path,
        "# AKTİF GÖREV — Ölçümlü Döngü\n\n"
        "## 17. Tamamlanma ölçütleri\n\n"
        "- [ ] Türkçe ölçüt doğrulandı.\n"
        "- [x] İkinci ölçüt kaynaktan okundu.\n\n"
        "## 18. Yasaklar\n\n- [ ] Kriter değildir.\n",
    )

    title, criteria, observed_digest = _read_active_spec(path, input_digest.upper())

    assert title == "Ölçümlü Döngü"
    assert criteria == (
        "Türkçe ölçüt doğrulandı.",
        "İkinci ölçüt kaynaktan okundu.",
    )
    assert observed_digest == input_digest


def test_active_spec_rejects_digest_drift_duplicate_and_non_utf8(tmp_path: Path) -> None:
    path, _ = _write(
        tmp_path,
        "# Görev\n\n## 17. Tamamlanma ölçütleri\n- [ ] Aynı\n- [ ] Aynı\n",
    )
    with pytest.raises(ZekamError, match="SHA-256 drift"):
        _read_active_spec(path, "sha256:" + "0" * 64)

    payload = path.read_bytes()
    with pytest.raises(ZekamError, match="tekil"):
        _read_active_spec(path, f"sha256:{hashlib.sha256(payload).hexdigest()}")

    path.write_bytes(b"\xff\xfe")
    payload = path.read_bytes()
    with pytest.raises(ZekamError, match="UTF-8"):
        _read_active_spec(path, f"sha256:{hashlib.sha256(payload).hexdigest()}")
