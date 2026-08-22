"""Kanonik JSON ve digest sozlesmesi."""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

import pytest

from zekam.domain.canonical import (
    CANONICAL_PROFILE,
    DIGEST_PREFIX,
    canonical_bytes,
    canonical_json,
    digest,
    digest_hex,
    digest_of_bytes,
    digests_match,
    parse_digest,
)
from zekam.domain.errors import ValidationFailed

pytestmark = pytest.mark.unit


class _Kind(StrEnum):
    FIRST = "birinci"


def test_profile_is_declared() -> None:
    assert CANONICAL_PROFILE == "zekam-canonical-json/v1"


def test_keys_are_sorted_and_separators_are_tight() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_nested_keys_are_sorted() -> None:
    assert canonical_json({"z": {"y": 1, "x": 2}}) == '{"z":{"x":2,"y":1}}'


def test_key_order_does_not_change_digest() -> None:
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_list_order_does_change_digest() -> None:
    assert digest([1, 2]) != digest([2, 1])


def test_tuple_and_list_are_equivalent() -> None:
    assert digest((1, 2)) == digest([1, 2])


def test_digest_has_prefix_and_length() -> None:
    value = digest({"a": 1})
    assert value.startswith(DIGEST_PREFIX)
    assert len(parse_digest(value)) == 64
    assert digest_hex({"a": 1}) == parse_digest(value)


def test_unicode_is_not_escaped() -> None:
    assert canonical_json({"k": "ölçü"}) == '{"k":"ölçü"}'
    assert canonical_bytes({"k": "ö"}) == '{"k":"ö"}'.encode()


def test_datetime_is_normalized_to_utc_z() -> None:
    plus_three = dt.timezone(dt.timedelta(hours=3))
    local = dt.datetime(2026, 8, 20, 12, 0, tzinfo=plus_three)
    utc = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)
    assert canonical_json(local) == canonical_json(utc) == '"2026-08-20T09:00:00Z"'


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationFailed, match="timezone"):
        canonical_json(dt.datetime(2026, 8, 20, 12, 0))


def test_date_is_iso_formatted() -> None:
    assert canonical_json(dt.date(2026, 8, 20)) == '"2026-08-20"'


def test_uuid_is_stringified() -> None:
    value = UUID("0198f2a0-0000-7000-8000-000000000000")
    assert canonical_json(value) == f'"{value}"'


def test_enum_uses_value() -> None:
    assert canonical_json(_Kind.FIRST) == '"birinci"'


def test_decimal_is_lossless_text() -> None:
    assert canonical_json(Decimal("0.10")) == '"0.1"'
    assert canonical_json(Decimal("1E+2")) == '"100"'


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_float_is_rejected(value: float) -> None:
    with pytest.raises(ValidationFailed, match="sonlu"):
        canonical_json(value)


def test_non_finite_decimal_is_rejected() -> None:
    with pytest.raises(ValidationFailed, match="sonlu"):
        canonical_json(Decimal("NaN"))


def test_non_string_key_is_rejected() -> None:
    with pytest.raises(ValidationFailed, match="metin anahtar"):
        canonical_json({1: "a"})


def test_set_is_rejected() -> None:
    with pytest.raises(ValidationFailed, match="sirasiz"):
        canonical_json({1, 2})


def test_bytes_are_rejected() -> None:
    with pytest.raises(ValidationFailed, match="bayt"):
        canonical_json(b"veri")


def test_unknown_type_is_rejected_not_stringified() -> None:
    class Custom:
        pass

    with pytest.raises(ValidationFailed, match="desteklenmeyen tip"):
        canonical_json(Custom())


def test_cycle_is_rejected() -> None:
    document: dict[str, object] = {}
    document["self"] = document
    with pytest.raises(ValidationFailed, match="dongusel"):
        canonical_json(document)


def test_bool_is_not_treated_as_int() -> None:
    assert canonical_json({"a": True}) == '{"a":true}'
    assert digest(True) != digest(1)


def test_digest_of_bytes_is_raw() -> None:
    assert digest_of_bytes(b"abc").endswith(
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


@pytest.mark.parametrize(
    "value", ["sha256:kisa", "md5:" + "0" * 64, "0" * 64, "sha256:" + "Z" * 64]
)
def test_invalid_digest_is_rejected(value: str) -> None:
    with pytest.raises(ValidationFailed):
        parse_digest(value)


def test_digests_match_validates_both_sides() -> None:
    left = digest({"a": 1})
    assert digests_match(left, left)
    assert not digests_match(left, digest({"a": 2}))
