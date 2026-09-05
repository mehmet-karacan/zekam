"""Zekam - kanit tabanli, model bagimsiz calisma ve bilgi platformu.

Bu paket urun cekirdegidir. Kullanici verisi `ZEKAM_HOME` altinda, kanonik durum
Yerel operational store icinde tutulur. Paket icinde kullanici verisi saklanmaz.
"""

from zekam.domain.identity import PRODUCT

__all__ = ["PRODUCT", "__version__"]

__version__ = "0.1.0"
