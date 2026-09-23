"""False-positive regression: sha256 hex digest must not trip the TR phone PII rule.

Reproducer: PROJECT.yaml carries `last_source_snapshot: sha256:<64 hex>`. A numeric
substring inside the hex digest (e.g. ``01638239149``) was wrongly treated as a real
Turkish phone, so `assert_public_safe_projection` atomically rejected the RAG
activation (chunks=0, unavailable). The bug is deterministic (same source_revision
yields the same digest), so it reproduced on every re-index.

The fix guards only the phone PII rule: a phone match that is a contiguous hex segment
embedded in a larger hex token (digest) is not flagged. Real Turkish phone numbers and
all other rules (SECRET_RULES / TCKN / IBAN / Luhn) are unchanged.
"""

from __future__ import annotations

import pytest

from zekam.application.knowledge_file_plane import assert_public_safe_projection
from zekam.domain.errors import PolicyViolation

pytestmark = pytest.mark.security


def test_digest_like_11_digit_sequence_passes() -> None:
    """A sha256 hex digest containing an 11-digit substring must pass the scan."""
    digest_value = "a" * 38 + "01638239149" + "b" * 15
    assert len(digest_value) == 64
    payload = f"last_source_snapshot: sha256:{digest_value}\nrevision: 3\n".encode()
    # Would raise PolicyViolation (false positive) before the fix.
    result = assert_public_safe_projection(payload, relative_path="public/project.yaml")
    assert isinstance(result, str) and result


def test_real_tr_phone_mobile_still_rejected() -> None:
    with pytest.raises(PolicyViolation, match="secret/PII"):
        assert_public_safe_projection(
            b"phone: +90 532 123 45 67\n", relative_path="public/note.md"
        )


def test_real_tr_phone_landline_still_rejected() -> None:
    with pytest.raises(PolicyViolation, match="secret/PII"):
        assert_public_safe_projection(
            b"tel: 0212 555 12 34\n", relative_path="public/note.md"
        )


def test_digest_and_clean_note_passes_sanity() -> None:
    digest_value = "9" * 8 + "01638239149" + "c" * 45
    assert len(digest_value) == 64
    payload = (
        f"project: demo\nlast_source_snapshot: sha256:{digest_value}\n"
        "note: bir not\n"
    ).encode()
    result = assert_public_safe_projection(payload, relative_path="public/project.yaml")
    assert isinstance(result, str) and result


def test_secret_and_other_pii_behavior_unchanged() -> None:
    """Sanity: a plain secret/password is still rejected (guard scope stays narrow)."""
    with pytest.raises(PolicyViolation, match="secret/PII"):
        assert_public_safe_projection(
            b"API_KEY = 'p9x7m2q4v8n6'\n", relative_path="public/note.md"
        )
    with pytest.raises(PolicyViolation, match="secret/PII"):
        assert_public_safe_projection(
            b"owner: user@example.com\n", relative_path="public/note.md"
        )
