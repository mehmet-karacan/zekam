"""Explicit composition boundary for historical repository-backed services.

New local-first services receive narrow ports directly.  Historical workflows
that have not yet been replaced use this fail-closed provider so application
modules never select a database engine or import a concrete adapter.
"""

from __future__ import annotations

from threading import Lock
from typing import Any, Protocol
from uuid import UUID

from zekam.domain.errors import ConfigurationError


class LegacyRepositoryProvider(Protocol):
    def build(
        self,
        kind: str,
        connection: Any,
        realm_id: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> Any: ...

    def maintain(self, operation: str, connection: Any, *args: Any, **kwargs: Any) -> Any: ...


_LOCK = Lock()
_PROVIDER: LegacyRepositoryProvider | None = None


def install_legacy_repository_provider(provider: LegacyRepositoryProvider) -> None:
    """Install one stateless provider at an outer composition root.

    Re-installing the same provider class is idempotent, which keeps CLI and
    test composition deterministic.  Switching engines in-process is rejected.
    """

    global _PROVIDER
    with _LOCK:
        if _PROVIDER is not None and type(_PROVIDER) is not type(provider):
            raise ConfigurationError("Legacy repository provider runtime'da degistirilemez")
        _PROVIDER = provider


def legacy_repository(
    kind: str,
    connection: Any,
    realm_id: UUID,
    *args: Any,
    **kwargs: Any,
) -> Any:
    provider = _PROVIDER
    if provider is None:
        raise ConfigurationError("Legacy repository provider composition root'ta kurulmamıs")
    return provider.build(kind, connection, realm_id, *args, **kwargs)


def legacy_database_maintenance(operation: str, connection: Any, *args: Any, **kwargs: Any) -> Any:
    provider = _PROVIDER
    if provider is None:
        raise ConfigurationError("Legacy maintenance provider composition root'ta kurulmamıs")
    return provider.maintain(operation, connection, *args, **kwargs)
