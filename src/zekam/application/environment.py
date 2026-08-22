"""Zekam ortam locator cozumlemesi."""

from __future__ import annotations

from collections.abc import Mapping


def environment_value(
    environ: Mapping[str, str],
    locator: str,
    *,
    strip: bool = False,
) -> str | None:
    """Yalniz exact Zekam locator'ini cozer."""
    value = environ.get(locator)
    if value is None:
        return None
    return value.strip() if strip else value
