"""Guvenlik alan modeli: veri siniflandirmasi, SecretRef, secret degeri ve yetki.

En onemli sinif `SecretValue`'dur. Bu sinif bir secret'i process bellegi icinde
tasir fakat:

- `repr()` ve `str()` cagrilarinda **maskelenmis** deger doner,
- f-string, log, exception ve JSON serilestirmede maskelenir,
- gercek degere yalnizca `reveal()` ile ulasilir ve bu cagri kod incelemesinde
  gorunur bir isarettir,
- `clear()` ile bellekten silinebilir.

Boylece "yanlislikla loglamak" varsayilan davranis olmaktan cikar.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7

#: Maskeleme icin kullanilan sabit metin.
REDACTED = "***"

#: Maskeleme sirasinda gormezden gelinecek kadar kisa degerler.
MIN_REDACTABLE_LENGTH = 4


class DataClassification(StrEnum):
    """Veri hassasiyet sinifi."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"
    SECRET = "secret"
    LOCAL_ONLY = "local-only"


#: Uzak saglayiciya hicbir kosulda gonderilemeyecek siniflar.
NEVER_OUTBOUND: frozenset[DataClassification] = frozenset(
    {DataClassification.SECRET, DataClassification.LOCAL_ONLY}
)

#: Acik izin ve gozden gecirilmis disclosure gerektiren siniflar.
REVIEW_REQUIRED_OUTBOUND: frozenset[DataClassification] = frozenset({DataClassification.RESTRICTED})


class SecretStatus(StrEnum):
    """SecretRef yasam dongusu."""

    ACTIVE = "active"
    ROTATING = "rotating"
    REVOKED = "revoked"


class SecretBackend(StrEnum):
    """Secret degerinin saklandigi arka uc."""

    ENVIRONMENT = "environment"
    OS_KEYCHAIN = "os-keychain"
    VAULT = "vault"
    KMS = "kms"
    LOCAL_ENCRYPTED = "local-encrypted"


class AuthorizationState(StrEnum):
    """Exact authorization durumu."""

    ISSUED = "issued"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


class OutboundState(StrEnum):
    """Disari acilan istegin durumu."""

    PREPARED = "prepared"
    APPROVED = "approved"
    EXECUTED = "executed"
    DENIED = "denied"


class SecretValue:
    """Bellekte tasinan secret degeri. Varsayilan gorunumu maskelidir.

    Bu sinif `dataclass` degildir ve `__slots__` kullanir; boylece otomatik
    `repr` uretimi ve beklenmedik oznitelik eklenmesi engellenir.
    """

    __slots__ = ("_cleared", "_value")

    def __init__(self, value: str) -> None:
        if not value:
            raise ValidationFailed("Secret degeri bos olamaz")
        self._value = value
        self._cleared = False

    def reveal(self) -> str:
        """Gercek degeri dondurur. Yalnizca adapter cagrisi aninda kullanilir."""
        if self._cleared:
            raise PolicyViolation("Secret degeri temizlenmis")
        return self._value

    def clear(self) -> None:
        """Degeri bellekten dusurur."""
        self._value = ""
        self._cleared = True

    @property
    def is_cleared(self) -> bool:
        return self._cleared

    def __repr__(self) -> str:
        return f"SecretValue({REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, format_spec: str) -> str:
        del format_spec
        return REDACTED

    def __eq__(self, other: object) -> bool:
        # Karsilastirma yalnizca ayni tip icin ve sabit zamanli olmayan bir
        # esitlik gerektirmeyecek sekilde sinirlidir.
        if not isinstance(other, SecretValue):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:  # pragma: no cover - kullanilmiyor fakat sozlesme geregi
        raise TypeError("SecretValue hash'lenemez")

    def __bool__(self) -> bool:
        return bool(self._value)

    def __reduce__(self) -> tuple[Any, ...]:
        raise TypeError("SecretValue serilestirilemez")


def redact(text: str, secrets: tuple[SecretValue, ...]) -> str:
    """Metin icindeki secret degerlerini maskeler.

    Log, exception ve rapor yollarinda son savunma katmanidir; birincil savunma
    degerin oraya hic ulasmamasidir.
    """
    result = text
    for secret in secrets:
        if secret.is_cleared:
            continue
        value = secret.reveal()
        if len(value) >= MIN_REDACTABLE_LENGTH:
            result = result.replace(value, REDACTED)
    return result


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Secret'in kimligi ve kapsami. Degeri tasimaz."""

    id: UUID
    realm_id: UUID
    name: str
    provider: str
    purpose: str
    allowed_operations: tuple[str, ...]
    store_backend: SecretBackend
    store_locator: str
    project_id: UUID | None = None
    version: int = 1
    status: SecretStatus = SecretStatus.ACTIVE
    expires_at: dt.datetime | None = None
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if not self.allowed_operations:
            raise ValidationFailed("SecretRef en az bir izinli operasyon tasimali")
        if not self.purpose.strip():
            raise ValidationFailed("SecretRef amaci bos olamaz")
        if not self.store_locator.strip():
            raise ValidationFailed("SecretRef store locator bos olamaz")
        if self.version < 1:
            raise ValidationFailed("SecretRef surumu 1'den kucuk olamaz")

    @classmethod
    def create(
        cls,
        *,
        realm_id: UUID,
        name: str,
        provider: str,
        purpose: str,
        allowed_operations: tuple[str, ...],
        store_backend: SecretBackend,
        store_locator: str,
        project_id: UUID | None = None,
        version: int = 1,
        expires_at: dt.datetime | None = None,
        now: dt.datetime | None = None,
    ) -> SecretRef:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm_id,
            name=name,
            provider=provider,
            purpose=purpose,
            allowed_operations=tuple(sorted(allowed_operations)),
            store_backend=store_backend,
            store_locator=store_locator,
            project_id=project_id,
            version=version,
            expires_at=expires_at,
            created_at=moment,
        )

    def is_usable(self, *, now: dt.datetime | None = None) -> bool:
        """Aktif ve suresi dolmamis mi?"""
        moment = now or dt.datetime.now(dt.UTC)
        if self.status is not SecretStatus.ACTIVE:
            return False
        return self.expires_at is None or self.expires_at > moment

    def permits(self, operation: str) -> bool:
        return operation in self.allowed_operations

    def body(self) -> dict[str, Any]:
        """Digest hesaplanan, deger icermeyen govde."""
        return {
            "name": self.name,
            "provider": self.provider,
            "purpose": self.purpose,
            "allowed_operations": list(self.allowed_operations),
            "store_backend": self.store_backend.value,
            "store_locator": self.store_locator,
            "project_id": None if self.project_id is None else str(self.project_id),
            "version": self.version,
        }

    @property
    def metadata_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        """Rapor gorunumu. Deger icermez ve iceremez."""
        return self.body() | {
            "id": str(self.id),
            "status": self.status.value,
            "expires_at": self.expires_at,
            "metadata_digest": self.metadata_digest,
        }


@dataclass(frozen=True, slots=True)
class AuthorizationScope:
    """Bir yetkinin exact kapsami.

    Kapsam yalnizca daraltilabilir; genisletme yeni yetki ister.
    """

    allowed_resources: tuple[str, ...] = ()
    allowed_effects: tuple[str, ...] = ()
    provider_refs: tuple[str, ...] = ()
    secret_ref_ids: tuple[UUID, ...] = ()
    data_classifications: tuple[DataClassification, ...] = ()

    def __post_init__(self) -> None:
        if not self.allowed_effects:
            raise ValidationFailed("Yetki en az bir etki turu icermeli")

    def covers_resource(self, resource: str) -> bool:
        """Kaynagin kapsam icinde olup olmadigini soyler.

        Yalnizca tam eslesme veya acik `prefix:*` kalibi kabul edilir; ornortulu
        genisleme yoktur.
        """
        for allowed in self.allowed_resources:
            if allowed == resource:
                return True
            if allowed.endswith("*") and resource.startswith(allowed[:-1]):
                return True
        return False

    def covers_effect(self, effect: str) -> bool:
        return effect in self.allowed_effects

    def body(self) -> dict[str, Any]:
        return {
            "allowed_resources": sorted(self.allowed_resources),
            "allowed_effects": sorted(self.allowed_effects),
            "provider_refs": sorted(self.provider_refs),
            "secret_ref_ids": sorted(str(item) for item in self.secret_ref_ids),
            "data_classifications": sorted(item.value for item in self.data_classifications),
        }

    def as_dict(self) -> dict[str, Any]:
        return self.body()


@dataclass(frozen=True, slots=True)
class Authorization:
    """Exact one-shot yetki kaydi.

    Yetki tek bir plan ve tek bir effect digest'ine baglidir. Suresizlik,
    genel kapsam ve yeniden kullanim yoktur.
    """

    id: UUID
    realm_id: UUID
    actor_id: UUID
    plan_digest: str
    effect_digest: str
    scope: AuthorizationScope
    risk: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    work_item_id: UUID | None = None
    plan_id: UUID | None = None
    state: AuthorizationState = AuthorizationState.ISSUED
    consumed_at: dt.datetime | None = None
    consumed_by: str | None = None
    revoked_at: dt.datetime | None = None
    revocation_reason: str | None = None

    def __post_init__(self) -> None:
        parse_digest(self.plan_digest)
        parse_digest(self.effect_digest)
        if self.expires_at <= self.issued_at:
            raise ValidationFailed("Yetki suresi verilis aninin sonrasinda olmali")

    @classmethod
    def issue(
        cls,
        *,
        realm_id: UUID,
        actor_id: UUID,
        plan_digest: str,
        effect_digest: str,
        scope: AuthorizationScope,
        risk: str,
        lifetime: dt.timedelta,
        work_item_id: UUID | None = None,
        plan_id: UUID | None = None,
        now: dt.datetime | None = None,
    ) -> Authorization:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm_id,
            actor_id=actor_id,
            plan_digest=plan_digest,
            effect_digest=effect_digest,
            scope=scope,
            risk=risk,
            issued_at=moment,
            expires_at=moment + lifetime,
            work_item_id=work_item_id,
            plan_id=plan_id,
        )

    def is_valid_at(self, moment: dt.datetime) -> bool:
        return self.state is AuthorizationState.ISSUED and self.expires_at > moment

    def rejection_reason(self, moment: dt.datetime) -> str | None:
        """Neden kullanilamaz? Kullanilabiliyorsa `None`."""
        if self.state is AuthorizationState.CONSUMED:
            return "authorization-already-consumed"
        if self.state is AuthorizationState.REVOKED:
            return "authorization-revoked"
        if self.state is AuthorizationState.EXPIRED or self.expires_at <= moment:
            return "authorization-expired"
        return None

    def body(self) -> dict[str, Any]:
        return {
            "actor_id": str(self.actor_id),
            "work_item_id": None if self.work_item_id is None else str(self.work_item_id),
            "plan_digest": self.plan_digest,
            "effect_digest": self.effect_digest,
            "scope": self.scope.body(),
            "risk": self.risk,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    @property
    def authorization_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {
            "id": str(self.id),
            "state": self.state.value,
            "consumed_at": self.consumed_at,
            "revoked_at": self.revoked_at,
            "revocation_reason": self.revocation_reason,
            "authorization_digest": self.authorization_digest,
        }


@dataclass(frozen=True, slots=True)
class OutboundRequest:
    """Disari acilan tek bir istek.

    Payload icerigi degil, yalnizca digest ve veri kategorileri kaydedilir.
    """

    id: UUID
    realm_id: UUID
    provider_ref: str
    endpoint_ref: str
    operation: str
    payload_digest: str
    request_identity: str
    data_categories: tuple[DataClassification, ...] = ()
    retention_assumption: str = "unknown"
    region: str = "unknown"
    authorization_id: UUID | None = None
    state: OutboundState = OutboundState.PREPARED
    denial_reason: str | None = None
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        parse_digest(self.payload_digest)
        for label, value in (
            ("Provider referansi", self.provider_ref),
            ("Endpoint referansi", self.endpoint_ref),
            ("Operasyon", self.operation),
            ("Istek kimligi", self.request_identity),
        ):
            if not value.strip():
                raise ValidationFailed(f"{label} bos olamaz")

    @classmethod
    def prepare(
        cls,
        *,
        realm_id: UUID,
        provider_ref: str,
        endpoint_ref: str,
        operation: str,
        payload_digest: str,
        request_identity: str,
        data_categories: tuple[DataClassification, ...] = (),
        retention_assumption: str = "unknown",
        region: str = "unknown",
        now: dt.datetime | None = None,
    ) -> OutboundRequest:
        """Salt okunur hazirlik. Ag cagrisi yapmaz, secret cozmez."""
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm_id,
            provider_ref=provider_ref,
            endpoint_ref=endpoint_ref,
            operation=operation,
            payload_digest=payload_digest,
            request_identity=request_identity,
            data_categories=tuple(sorted(set(data_categories), key=lambda item: item.value)),
            retention_assumption=retention_assumption,
            region=region,
            created_at=moment,
        )

    @property
    def target(self) -> str:
        """Yetki eslesmesinde kullanilan exact hedef."""
        return f"{self.provider_ref}:{self.endpoint_ref}:{self.operation}"

    def blocking_classifications(self) -> tuple[DataClassification, ...]:
        """Disari cikmasi yasak veri siniflari."""
        return tuple(item for item in self.data_categories if item in NEVER_OUTBOUND)

    def review_required_classifications(self) -> tuple[DataClassification, ...]:
        return tuple(item for item in self.data_categories if item in REVIEW_REQUIRED_OUTBOUND)

    def with_state(
        self,
        state: OutboundState,
        *,
        authorization_id: UUID | None = None,
        denial_reason: str | None = None,
    ) -> Self:
        return type(self)(
            id=self.id,
            realm_id=self.realm_id,
            provider_ref=self.provider_ref,
            endpoint_ref=self.endpoint_ref,
            operation=self.operation,
            payload_digest=self.payload_digest,
            request_identity=self.request_identity,
            data_categories=self.data_categories,
            retention_assumption=self.retention_assumption,
            region=self.region,
            authorization_id=authorization_id or self.authorization_id,
            state=state,
            denial_reason=denial_reason,
            created_at=self.created_at,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "provider_ref": self.provider_ref,
            "endpoint_ref": self.endpoint_ref,
            "operation": self.operation,
            "payload_digest": self.payload_digest,
            "request_identity": self.request_identity,
            "data_categories": [item.value for item in self.data_categories],
            "retention_assumption": self.retention_assumption,
            "region": self.region,
            "authorization_id": (
                None if self.authorization_id is None else str(self.authorization_id)
            ),
            "state": self.state.value,
            "denial_reason": self.denial_reason,
        }
