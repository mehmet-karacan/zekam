"""Secret tespiti: bulgu uretir, deger sizdirmaz."""

from __future__ import annotations

import pytest

from zekam.application.secret_detection import (
    SECRET_RULES,
    SecretSeverity,
    highest_severity,
    scan_text,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

SECRET_VALUE = "Kx7pQm2ZrT9wLb4Nc1Vd"


def _rule_ids(text: str) -> set[str]:
    return {finding.rule_id for finding in scan_text(text, relative_path="a.txt")}


def test_rules_have_unique_identifiers() -> None:
    identifiers = [rule.rule_id for rule in SECRET_RULES]
    assert len(identifiers) == len(set(identifiers))


def test_private_key_block_is_detected() -> None:
    assert "private-key-block" in _rule_ids("-----BEGIN RSA PRIVATE KEY-----")


def test_aws_access_key_is_detected() -> None:
    assert "aws-access-key-id" in _rule_ids("kimlik = AKIAZZZZQQQQWWWWEEEE")


def test_github_token_is_detected() -> None:
    assert "github-token" in _rule_ids("ghp_" + "a" * 36)


def test_jwt_is_detected() -> None:
    token = (
        "eyJhbGciOiJIUzI1NiJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    assert "json-web-token" in _rule_ids(token)


def test_connection_string_password_is_detected() -> None:
    assert "connection-string-password" in _rule_ids(
        "postgresql://kullanici:CokGizliParola@db:5432/zekam"
    )


def test_assigned_credential_is_detected() -> None:
    assert "assigned-credential" in _rule_ids('api_key = "abcdefgh12345678"')


def test_unquoted_yaml_and_env_credentials_are_detected() -> None:
    assert "assigned-credential" in _rule_ids("password: synthetic-value-12345")
    assert "assigned-credential" in _rule_ids("AUTH_TOKEN=synthetic-token-12345")


def test_environment_reference_is_not_treated_as_secret_value() -> None:
    assert scan_text("password: ${DATABASE_PASSWORD}", relative_path="application.yaml") == ()


@pytest.mark.parametrize(
    "line",
    [
        'password = _required_text(datasource, "password", "password")',
        "credential: SecretValue,",
        "token = self.owner_token",
    ],
)
def test_code_identifiers_are_not_treated_as_unquoted_secret_values(line: str) -> None:
    assert scan_text(line, relative_path="config.py") == ()


def test_scan_limit_excess_fails_closed_without_content() -> None:
    findings = scan_text("safe\nline\nextra", relative_path="large.sql", max_lines=2)
    assert findings[0].rule_id == "scan-limit-exceeded"
    assert findings[0].line_number == 3
    assert "extra" not in repr(findings[0].as_dict())


def test_authorization_header_is_detected() -> None:
    assert "authorization-header" in _rule_ids('Authorization: "Bearer abcdefgh12345678"')


def test_finding_never_contains_the_secret_value() -> None:
    findings = scan_text(f'password = "{SECRET_VALUE}"', relative_path="config.py")
    assert findings
    rendered = repr([finding.as_dict() for finding in findings])
    assert SECRET_VALUE not in rendered


def test_fingerprint_is_short_and_stable() -> None:
    first = scan_text('token = "abcdefgh12345678"', relative_path="a.py")[0]
    second = scan_text('token = "abcdefgh12345678"', relative_path="b.py")[0]
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 12


def test_different_values_get_different_fingerprints() -> None:
    first = scan_text('token = "abcdefgh12345678"', relative_path="a.py")[0]
    second = scan_text('token = "zyxwvuts87654321"', relative_path="a.py")[0]
    assert first.fingerprint != second.fingerprint


def test_line_number_is_reported() -> None:
    text = 'birinci satir\nikinci satir\napi_key = "abcdefgh12345678"\n'
    assert scan_text(text, relative_path="a.py")[0].line_number == 3


@pytest.mark.parametrize(
    "line",
    [
        'password = "degistir-beni"',
        'api_key = "your-api-key-here"',
        'token = "example-token-value"',
        'secret_key = "<REDACTED>"',
    ],
)
def test_placeholder_values_are_not_reported(line: str) -> None:
    assert scan_text(line, relative_path="ornek.env") == ()


def test_clean_source_produces_no_findings() -> None:
    text = "def topla(a: int, b: int) -> int:\n    return a + b\n"
    assert scan_text(text, relative_path="matematik.py") == ()


def test_highest_severity_is_reported() -> None:
    findings = scan_text(
        'api_key = "abcdefgh12345678"\n-----BEGIN PRIVATE KEY-----\n', relative_path="a.py"
    )
    assert highest_severity(findings) is SecretSeverity.HIGH
    assert highest_severity(()) is None
