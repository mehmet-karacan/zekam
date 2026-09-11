"""Alan hatalari.

Hata mesajlari sanitize edilmis olmalidir: secret degeri, tam dosya yolu veya ham
saglayici cikti metni tasimaz.
"""

from __future__ import annotations


class ZekamError(Exception):
    """Butun Zekam hatalarinin koku."""

    #: Makine tarafindan siniflandirma icin kararli kod.
    code: str = "zekam-error"


class ConfigurationError(ZekamError):
    """Eksik veya tutarsiz yapilandirma."""

    code = "configuration-error"


class LayoutError(ZekamError):
    """ZEKAM_HOME yerlesimi beklenen sozlesmeye uymuyor."""

    code = "layout-error"


class PolicyViolation(ZekamError):
    """Politika veya guvenlik siniri ihlali."""

    code = "policy-violation"


class AuthorizationRequired(ZekamError):
    """Islem exact authorization olmadan yurutulemez."""

    code = "authorization-required"


class ConcurrencyConflict(ZekamError):
    """Optimistic concurrency veya fencing uyusmazligi."""

    code = "concurrency-conflict"


class NotFound(ZekamError):
    """Istenen kayit bulunamadi."""

    code = "not-found"


class ClassifiedConfigurationError(ConfigurationError):
    """Configuration failure with a stable machine-facing reason code."""

    def __init__(self, code: str, message: str) -> None:
        if not code or code != code.strip():
            raise ValueError("configuration error code exact text required")
        self.code = code
        super().__init__(message)


class CredentialUnavailable(NotFound):
    """A secret locator exists but its process-local value is unavailable."""

    code = "credential-env-missing"


class ValidationFailed(ZekamError):
    """Girdi sozlesmeye uymuyor."""

    code = "validation-failed"
