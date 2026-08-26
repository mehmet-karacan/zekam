from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from zekam.application.source_security_policy import (
    default_secret_scan_allowlist_file,
    load_secret_scan_allowlist,
)
from zekam.domain.errors import ValidationFailed


def test_reviewed_secret_fixture_allowlist_is_exact_and_digest_bound() -> None:
    policy = load_secret_scan_allowlist()
    assert len(policy.allowances) == 29
    assert policy.policy_digest.startswith("sha256:")
    assert all(item.relative_path.startswith("tests/") for item in policy.allowances)


def test_secret_allowlist_unknown_field_is_rejected(tmp_path: Path) -> None:
    document = yaml.safe_load(default_secret_scan_allowlist_file().read_text(encoding="utf-8"))
    document["unexpected"] = True
    target = tmp_path / "allowlist.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValidationFailed, match="exact shape"):
        load_secret_scan_allowlist(target)
