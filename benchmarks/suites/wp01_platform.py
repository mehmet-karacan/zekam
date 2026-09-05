"""Shared, evidence-oriented platform helpers for WP-01 bake-offs."""

from __future__ import annotations

import ctypes
import platform
import sys
from typing import Literal

type AcceptancePlatform = Literal["macos-arm64", "windows-x64"]


def current_acceptance_platform() -> AcceptancePlatform | None:
    """Return an acceptance platform only for the exact supported architecture."""
    machine = platform.machine().casefold()
    if sys.platform == "darwin" and machine == "arm64" and ctypes.sizeof(ctypes.c_void_p) == 8:
        return "macos-arm64"
    if (
        sys.platform == "win32"
        and machine in {"amd64", "x86_64"}
        and ctypes.sizeof(ctypes.c_void_p) == 8
    ):
        return "windows-x64"
    return None
