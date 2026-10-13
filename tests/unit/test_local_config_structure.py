"""Hassas yerel config yapisi icin sentetik, provider-free sinir testleri."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application.local_config_structure import (
    MAX_LOCAL_CONFIG_BYTES,
    parse_local_config_structure,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.session_continuity import DataClassification


def test_env_returns_only_safe_keys_and_not_values(tmp_path: Path) -> None:
    path = tmp_path / ".env.local"
    path.write_text(
        "# synthetic fixture\nexport SERVICE_HOST=test-only-marker-42\nEMPTY=\n",
        encoding="utf-8",
    )
    before = path.read_bytes()

    result = parse_local_config_structure(tmp_path, ".env.local")

    assert result.classification is DataClassification.LOCAL_ONLY
    assert result.source_format == "env"
    assert [(key.name, key.line_number, key.group) for key in result.keys] == [
        ("SERVICE_HOST", 2, None),
        ("EMPTY", 3, None),
    ]
    assert result.line_count == 3
    assert "test-only-marker-42" not in repr(result)
    assert "test-only-marker-42" not in json.dumps(result.as_dict())
    assert "digest" not in json.dumps(result.as_dict())
    assert path.read_bytes() == before

    path.write_bytes(before.replace(b"test-only-marker-42", b"test-only-marker-99"))
    assert parse_local_config_structure(tmp_path, ".env.local") == result


def test_properties_returns_group_and_no_value(tmp_path: Path) -> None:
    path = tmp_path / "service.properties"
    path.write_text("# fixture\nserver.port: 1234\nserver.name=synthetic-value\n", encoding="utf-8")

    result = parse_local_config_structure(tmp_path, path.name)

    assert result.source_format == "properties"
    assert [(key.name, key.group) for key in result.keys] == [
        ("server.port", "server"),
        ("server.name", "server"),
    ]
    assert "synthetic-value" not in repr(result)
    assert "1234" not in json.dumps(result.as_dict())


@pytest.mark.parametrize("relative_path", ["../outside.env", "/outside.env", "note.txt"])
def test_rejects_unsupported_or_escaping_paths(tmp_path: Path, relative_path: str) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        parse_local_config_structure(tmp_path, relative_path)


def test_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.properties"
    target.write_text("safe.key=synthetic-value\n", encoding="utf-8")
    link = tmp_path / "linked.properties"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(PolicyViolation):
        parse_local_config_structure(tmp_path, link.name)


def test_limits_and_errors_do_not_include_values(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("GOOD=synthetic-value\n", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        parse_local_config_structure(tmp_path, path.name, max_bytes=MAX_LOCAL_CONFIG_BYTES + 1)
    with pytest.raises(PolicyViolation):
        parse_local_config_structure(tmp_path, path.name, max_bytes=4)

    path.write_text("BAD KEY=synthetic-value\n", encoding="utf-8")
    with pytest.raises(ValidationFailed) as error:
        parse_local_config_structure(tmp_path, path.name)
    assert "synthetic-value" not in str(error.value)

    path.write_bytes(b"GOOD=\xff\n")
    with pytest.raises(ValidationFailed) as error:
        parse_local_config_structure(tmp_path, path.name)
    assert "GOOD" not in str(error.value)


def test_redacts_sensitive_key_and_rejects_continuation(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "SERVICE_HOST=synthetic-safe\nPASSWORD=synthetic-value\nAWS_ACCESS_KEY_ID=synthetic-id\n",
        encoding="utf-8",
    )
    result = parse_local_config_structure(tmp_path, path.name)
    assert result.redacted_key_count == 2
    assert [key.name for key in result.keys] == ["SERVICE_HOST"]
    assert "PASSWORD" not in repr(result)
    assert "AWS_ACCESS_KEY_ID" not in json.dumps(result.as_dict())
    assert "synthetic-value" not in json.dumps(result.as_dict())
    path.write_text("SAFE=synthetic-value\\\n", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        parse_local_config_structure(tmp_path, path.name)
