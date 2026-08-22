"""Urun kimligi.

Urun adi `mimari/ZEKAM_KIMLIK_SOZLESMESI.md` ile sabittir. Kod icinde serbest metin
yerine bu sabitler kullanilir; boylece kimlik butunlugu tek noktadan test edilebilir.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    """Degistirilemez urun kimligi."""

    name: str
    slug: str
    python_package: str
    cli: str
    data_root_env: str

    def home_env_var(self) -> str:
        return self.data_root_env


PRODUCT: Final = ProductIdentity(
    name="Zekam",
    slug="zekam",
    python_package="zekam",
    cli="zekam",
    data_root_env="ZEKAM_HOME",
)
