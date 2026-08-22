"""Secret Broker.

Sozlesme (`guvenlik/SECRET_BROKER_VE_OUTBOUND_POLICY.md`):

1. Adapter exact operation ve `SecretRef` ister.
2. Broker kapsam, amac, izinli operasyon, surum, expiry ve yetkiyi dogrular.
3. Deger arka uctan **yalnizca cagri aninda** process bellegine cozulur.
4. Cagri bitince deger bellekten dusurulur; hicbir yere yazilmaz.

Broker plaintext degeri dondurmez; `SecretValue` dondurur. Bu nesnenin
varsayilan gorunumu maskelidir ve gercek degere yalnizca `reveal()` ile
ulasilir.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from zekam.application.environment import environment_value
from zekam.domain.errors import AuthorizationRequired, NotFound, PolicyViolation
from zekam.domain.security import (
    Authorization,
    SecretBackend,
    SecretRef,
    SecretValue,
)


class SecretStore(Protocol):
    """Secret degerini cozen arka uc.

    `backend` salt okunurdur; depolar degismez (frozen) nesneler olarak tanimlanir.
    """

    @property
    def backend(self) -> SecretBackend:
        """Bu deponun cozebildigi arka uc."""
        ...

    def resolve(self, reference: SecretRef) -> SecretValue:
        """Degeri cozer. Bulunamazsa `NotFound` yukseltir."""
        ...


@dataclass(frozen=True, slots=True)
class EnvironmentSecretStore:
    """Ortam degiskeninden okuyan arka uc.

    `store_locator` degerin **adi**dir, kendisi degildir. Gelistirme ve CI icin
    uygundur; uretimde keychain/Vault/KMS adapterleri kullanilir.
    """

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    backend: SecretBackend = SecretBackend.ENVIRONMENT

    def resolve(self, reference: SecretRef) -> SecretValue:
        if reference.store_backend is not SecretBackend.ENVIRONMENT:
            raise PolicyViolation(
                f"Bu depo yalnizca environment backend cozer: {reference.store_backend.value}"
            )
        raw = environment_value(self.environ, reference.store_locator)
        if not raw:
            raise NotFound(f"Secret degeri bulunamadi: {reference.name}")
        return SecretValue(raw)


@dataclass(frozen=True, slots=True)
class InMemorySecretStore:
    """Test ve yerel gelistirme icin bellek ici depo."""

    values: Mapping[str, str]
    backend: SecretBackend = SecretBackend.LOCAL_ENCRYPTED

    def resolve(self, reference: SecretRef) -> SecretValue:
        raw = self.values.get(reference.store_locator)
        if not raw:
            raise NotFound(f"Secret degeri bulunamadi: {reference.name}")
        return SecretValue(raw)


@dataclass(frozen=True, slots=True)
class BrokerDecision:
    """Broker'in verdigi karar. Deger icermez."""

    allowed: bool
    reason: str
    secret_ref_id: UUID
    operation: str

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "secret_ref_id": str(self.secret_ref_id),
            "operation": self.operation,
        }


class SecretBroker:
    """SecretRef'i dogrulayip degeri gecici olarak cozer."""

    def __init__(self, stores: Mapping[SecretBackend, SecretStore]) -> None:
        self._stores = dict(stores)

    def supports(self, backend: SecretBackend) -> bool:
        return backend in self._stores

    def evaluate(
        self,
        reference: SecretRef,
        *,
        operation: str,
        authorization: Authorization | None = None,
        now: dt.datetime | None = None,
    ) -> BrokerDecision:
        """Karari verir fakat degeri **cozmez**. Salt okunur kontroldur."""
        moment = now or dt.datetime.now(dt.UTC)

        if not reference.is_usable(now=moment):
            return BrokerDecision(
                allowed=False,
                reason=f"secret-not-usable:{reference.status.value}",
                secret_ref_id=reference.id,
                operation=operation,
            )
        if not reference.permits(operation):
            return BrokerDecision(
                allowed=False,
                reason="operation-not-permitted",
                secret_ref_id=reference.id,
                operation=operation,
            )
        if not self.supports(reference.store_backend):
            return BrokerDecision(
                allowed=False,
                reason=f"backend-unsupported:{reference.store_backend.value}",
                secret_ref_id=reference.id,
                operation=operation,
            )
        if authorization is None:
            return BrokerDecision(
                allowed=False,
                reason="authorization-required",
                secret_ref_id=reference.id,
                operation=operation,
            )
        rejection = authorization.rejection_reason(moment)
        if rejection is not None:
            return BrokerDecision(
                allowed=False,
                reason=rejection,
                secret_ref_id=reference.id,
                operation=operation,
            )
        if reference.id not in authorization.scope.secret_ref_ids:
            return BrokerDecision(
                allowed=False,
                reason="secret-out-of-authorization-scope",
                secret_ref_id=reference.id,
                operation=operation,
            )
        if reference.realm_id != authorization.realm_id:
            return BrokerDecision(
                allowed=False,
                reason="cross-realm-secret",
                secret_ref_id=reference.id,
                operation=operation,
            )
        return BrokerDecision(
            allowed=True,
            reason="allowed",
            secret_ref_id=reference.id,
            operation=operation,
        )

    @contextmanager
    def resolve(
        self,
        reference: SecretRef,
        *,
        operation: str,
        authorization: Authorization | None = None,
        now: dt.datetime | None = None,
    ) -> Iterator[SecretValue]:
        """Degeri cozer ve blok bitince bellekten dusurur.

        Kullanim:

        ```python
        with broker.resolve(reference, operation="chat", authorization=auth) as secret:
            client.call(api_key=secret.reveal())
        # blok bittiginde deger temizlenmistir
        ```
        """
        decision = self.evaluate(
            reference, operation=operation, authorization=authorization, now=now
        )
        if not decision.allowed:
            if decision.reason in {"authorization-required", "secret-out-of-authorization-scope"}:
                raise AuthorizationRequired(f"Secret cozulemedi: {decision.reason}")
            raise PolicyViolation(f"Secret cozulemedi: {decision.reason}")

        store = self._stores[reference.store_backend]
        value = store.resolve(reference)
        try:
            yield value
        finally:
            value.clear()
