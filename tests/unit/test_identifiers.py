"""Kimlik uretimi, slug dogrulama ve portability kurallari."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.domain.errors import ValidationFailed
from zekam.domain.identifiers import (
    SLUG_MAX_LENGTH,
    assert_portable,
    new_uuid7,
    normalize_slug,
    uuid7_timestamp,
    validate_slug,
)

pytestmark = pytest.mark.unit


def test_uuid7_has_version_and_variant() -> None:
    value = new_uuid7()
    assert value.version == 7
    assert (value.bytes[8] & 0xC0) == 0x80


def test_uuid7_encodes_timestamp() -> None:
    moment = dt.datetime(2026, 8, 20, 9, 30, tzinfo=dt.UTC)
    decoded = uuid7_timestamp(new_uuid7(now=moment))
    assert abs((decoded - moment).total_seconds()) < 0.001


def test_uuid7_is_time_ordered() -> None:
    early = new_uuid7(now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    late = new_uuid7(now=dt.datetime(2026, 8, 20, tzinfo=dt.UTC))
    assert early.bytes[:6] < late.bytes[:6]


def test_uuid7_values_are_unique() -> None:
    moment = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
    values = {new_uuid7(now=moment) for _ in range(1000)}
    assert len(values) == 1000


def test_uuid7_requires_aware_datetime() -> None:
    with pytest.raises(ValidationFailed):
        new_uuid7(now=dt.datetime(2026, 8, 20))


def test_uuid7_timestamp_rejects_other_versions() -> None:
    from uuid import uuid4

    with pytest.raises(ValidationFailed):
        uuid7_timestamp(uuid4())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("GPU Projesi", "gpu-projesi"),
        ("  Cok   Bosluklu  ", "cok-bosluklu"),
        ("a__b--c", "a-b-c"),
        ("Rapor.2026", "rapor-2026"),
    ],
)
def test_normalize_slug(raw: str, expected: str) -> None:
    assert normalize_slug(raw) == expected


def test_normalize_slug_rejects_empty_result() -> None:
    with pytest.raises(ValidationFailed):
        normalize_slug("///")


def test_normalize_slug_respects_max_length() -> None:
    assert len(normalize_slug("a" * 200)) <= SLUG_MAX_LENGTH


@pytest.mark.parametrize("value", ["gpu", "gpu-projesi", "a1-b2-c3"])
def test_valid_slugs_are_accepted(value: str) -> None:
    assert validate_slug(value) == value


@pytest.mark.parametrize(
    "value",
    ["A", "a", "-gpu", "gpu-", "gpu--projesi", "GPU", "gpu projesi", "gpu_projesi", "a" * 65],
)
def test_invalid_slugs_are_rejected(value: str) -> None:
    with pytest.raises(ValidationFailed):
        validate_slug(value)


@pytest.mark.parametrize(
    "value",
    ["C:/Users/kisi/proje", "/home/kisi/proje", "~/proje", "D:\\veri"],
)
def test_absolute_paths_are_not_portable(value: str) -> None:
    with pytest.raises(ValidationFailed):
        assert_portable(value)


def test_home_directory_is_not_portable() -> None:
    import os

    with pytest.raises(ValidationFailed):
        assert_portable(f"proje: {os.path.expanduser('~')}/kod")


def test_blank_value_is_not_portable() -> None:
    with pytest.raises(ValidationFailed):
        assert_portable("   ")


@pytest.mark.parametrize("value", ["gpu-projesi", "src/zekam/domain", "work:zekam:P01"])
def test_relative_and_logical_values_are_portable(value: str) -> None:
    assert assert_portable(value) == value
