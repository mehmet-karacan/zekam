"""Parametre kimliklerini sinirla.

Cok buyuk bytes/str parametreleri nodeid'yi (ve pytest'in PYTEST_CURRENT_TEST ortam
degiskenini) Windows'un 32767 karakter sinirinin ustune tasir. Kisa kimlik testin
kendisini degistirmez.
"""

from __future__ import annotations


def short_id(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 40 else f"{type(value).__name__}-len{len(value)}"  # type: ignore[arg-type]
