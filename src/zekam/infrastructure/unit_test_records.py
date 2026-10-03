"""Unit-test dongusu icin yerel, kalici `AssignmentStore` ve calisma manifesti (W07).

Ayri veritabani, PostgreSQL veya JSON state dosyasi yoktur. Kayitlar mevcut icerik adresli
nesne deposuna (CAS) yazilir ve ``unit_test_*`` ledger'inin bagli oldugu operational store'un
``artifact_ref`` kayit defterine kaydedilir (``register_artifact``). Kayit turu/anahtari/istek
kimligi CAS metadata'sindadir; metadata secret tasiyamaz.

Claim-before-effect sirasi ``CanonicalAgentDispatchService``'te zorlanir ve burada dogrulanir:

1. ``create(assignment)`` -> assignment kaydi (ayni kimlik farkli icerikle gelirse reddedilir),
2. ``record_invocation`` -> assignment kaydi YOKSA reddedilir,
3. ``store_result`` -> invocation kaydi YOKSA reddedilir; ayni sonuc replay'i idempotenttir,
   farkli envelope digest'i reddedilir.

Kayit icerigi zaman damgasi tasimaz (replay ayni digest'i uretir). Ham model ciktisi, transcript
veya secret saklanmaz: yalniz kimlik, digest ve istemci/rol bilgisi.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

from zekam.application.object_store import ObjectInfo, ObjectStore
from zekam.domain.agents import AgentAssignment, AgentInvocation
from zekam.domain.canonical import canonical_bytes, parse_digest
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed

KIND_ASSIGNMENT: Final = "unit-test-agent-assignment"
KIND_INVOCATION: Final = "unit-test-agent-invocation"
KIND_RESULT: Final = "unit-test-agent-result"
KIND_RUN_MANIFEST: Final = "unit-test-run-manifest"
_META_KIND: Final = "zekam-record"
_META_KEY: Final = "zekam-key"
_META_REQUEST: Final = "zekam-request"
_JSON: Final = "application/json"

#: Operational ``artifact_ref`` kayit defterine bildirim (digest, boyut, media type).
ArtifactRegistrar = Callable[[str, int, str], None]


@dataclass(slots=True)
class CasRecordIndex:
    """CAS metadata'si uzerinden tur+anahtar aramasi. Salt okunur tarama + onbellek."""

    objects: ObjectStore
    _cache: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    _scanned: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _scan(self) -> None:
        with self._lock:
            found: dict[tuple[str, str], list[str]] = {}
            for info in self.objects.iter_objects():
                kind = info.metadata.get(_META_KIND)
                key = info.metadata.get(_META_KEY)
                if kind is None or key is None:
                    continue
                found.setdefault((kind, key), []).append(info.digest)
            self._cache = {pair: tuple(digests) for pair, digests in found.items()}
            self._scanned = True

    def put(
        self,
        kind: str,
        key: str,
        body: dict[str, Any],
        *,
        request_digest: str | None = None,
        registrar: ArtifactRegistrar | None = None,
    ) -> tuple[ObjectInfo, bool]:
        """Kaydi yazar; ``created=False`` ayni icerigin zaten var oldugunu belirtir."""

        metadata = {_META_KIND: kind, _META_KEY: key}
        if request_digest is not None:
            parse_digest(request_digest)
            metadata[_META_REQUEST] = request_digest
        payload = canonical_bytes(body)
        from zekam.domain.canonical import digest_of_bytes

        digest = digest_of_bytes(payload)
        existed = self.objects.exists(digest)
        info = self.objects.put(payload, media_type=_JSON, metadata=metadata)
        if registrar is not None:
            registrar(info.digest, info.size_bytes, _JSON)
        with self._lock:
            known = self._cache.get((kind, key), ())
            if info.digest not in known:
                self._cache[(kind, key)] = (*known, info.digest)
        return info, not existed

    def find(self, kind: str, key: str) -> tuple[str, ...]:
        if not self._scanned:
            self._scan()
        return self._cache.get((kind, key), ())

    def read(self, digest: str) -> dict[str, Any]:
        import json

        loaded = json.loads(self.objects.get(digest).decode("utf-8"))
        if not isinstance(loaded, dict):
            raise ValidationFailed("Kayit nesnesi sozluk degil")
        return loaded

    def iter_kind(self, kind: str, *, request_digest: str | None = None) -> Iterator[ObjectInfo]:
        for info in self.objects.iter_objects():
            if info.metadata.get(_META_KIND) != kind:
                continue
            if request_digest is not None and info.metadata.get(_META_REQUEST) != request_digest:
                continue
            yield info


def _assignment_body(assignment: AgentAssignment) -> dict[str, Any]:
    return {
        "record": "zekam-unit-test-agent-assignment/v1",
        **assignment.identity_body(),
        "assignment_digest": assignment.assignment_digest,
    }


@dataclass(slots=True)
class CasAssignmentStore:
    """Gercek ``AssignmentStore``: assignment-first, kalici, idempotent, sirasi dogrulanir."""

    index: CasRecordIndex
    request_digest: str
    registrar: ArtifactRegistrar | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        parse_digest(self.request_digest)

    def create(self, assignment: AgentAssignment) -> tuple[UUID, bool]:
        assignment.assert_digest()
        body = _assignment_body(assignment)
        key = str(assignment.id)
        with self._lock:
            existing = self.index.find(KIND_ASSIGNMENT, key)
            info, created = self.index.put(
                KIND_ASSIGNMENT,
                key,
                body,
                request_digest=self.request_digest,
                registrar=self.registrar,
            )
            if existing and info.digest not in existing:
                raise PolicyViolation("Assignment kimligi farkli icerikle yeniden kullanildi")
        return assignment.id, created

    def record_invocation(self, invocation: AgentInvocation) -> tuple[UUID, bool]:
        invocation.assert_digest()
        with self._lock:
            if not self.index.find(KIND_ASSIGNMENT, str(invocation.assignment_id)):
                raise PolicyViolation("Invocation icin assignment kaydi yok (assignment-first)")
            body = {
                "record": "zekam-unit-test-agent-invocation/v1",
                "id": str(invocation.id),
                "realm_id": str(invocation.realm_id),
                "assignment_id": str(invocation.assignment_id),
                "client_id": invocation.client_id,
                "execution_identity": invocation.execution_identity,
                "invocation_digest": invocation.invocation_digest,
            }
            key = str(invocation.id)
            existing = self.index.find(KIND_INVOCATION, key)
            info, created = self.index.put(
                KIND_INVOCATION,
                key,
                body,
                request_digest=self.request_digest,
                registrar=self.registrar,
            )
            if existing and info.digest not in existing:
                raise PolicyViolation("Invocation kimligi farkli icerikle yeniden kullanildi")
        return invocation.id, created

    def store_result(
        self, *, assignment_id: UUID, invocation_id: UUID, envelope_digest: str
    ) -> None:
        parse_digest(envelope_digest)
        with self._lock:
            invocation_records = self.index.find(KIND_INVOCATION, str(invocation_id))
            if not invocation_records:
                raise PolicyViolation("Sonuc icin invocation kaydi yok (assignment-first)")
            bound = any(
                self.index.read(record_digest).get("assignment_id") == str(assignment_id)
                for record_digest in invocation_records
            )
            if not bound:
                raise PolicyViolation("Sonuc invocation-assignment bagini bozuyor")
            key = f"{assignment_id}:{invocation_id}"
            body = {
                "record": "zekam-unit-test-agent-result/v1",
                "assignment_id": str(assignment_id),
                "invocation_id": str(invocation_id),
                "envelope_digest": envelope_digest,
            }
            existing = self.index.find(KIND_RESULT, key)
            info, _created = self.index.put(
                KIND_RESULT,
                key,
                body,
                request_digest=self.request_digest,
                registrar=self.registrar,
            )
            if existing and info.digest not in existing:
                raise PolicyViolation(
                    "Invocation sonucu farkli envelope digest'i ile degistirilemez"
                )

    # -- okuma (report/status) -------------------------------------------------------

    def invocation_summary(self) -> dict[str, Any]:
        """Bu istek icin ajan cagri ozeti: istemci ve rol bazinda sayi; icerik yok."""

        by_client: dict[str, int] = {}
        total = 0
        for info in self.index.iter_kind(KIND_INVOCATION, request_digest=self.request_digest):
            record = self.index.read(info.digest)
            client = str(record.get("client_id", "?"))
            by_client[client] = by_client.get(client, 0) + 1
            total += 1
        results = sum(
            1 for _ in self.index.iter_kind(KIND_RESULT, request_digest=self.request_digest)
        )
        return {
            "invocations": total,
            "results_bound": results,
            "by_client": dict(sorted(by_client.items())),
        }


def latest_run_manifest(objects: ObjectStore, request_digest: str) -> dict[str, Any] | None:
    """Istegin son calisma manifesti (sirasi: ``sequence``); yoksa ``None``."""

    index = CasRecordIndex(objects)
    best: dict[str, Any] | None = None
    for info in index.iter_kind(KIND_RUN_MANIFEST, request_digest=request_digest):
        try:
            record = index.read(info.digest)
        except (NotFound, ValidationFailed, ValueError):
            continue
        if best is None or int(record.get("sequence", 0)) >= int(best.get("sequence", 0)):
            best = record
    return best


def write_run_manifest(
    index: CasRecordIndex,
    *,
    request_digest: str,
    sequence: int,
    body: dict[str, Any],
    registrar: ArtifactRegistrar | None = None,
) -> str:
    """Calisma saglayici/olcum kaydi (replay mi gercek mi): rapor durustlugu icin."""

    document = {"record": "zekam-unit-test-run-manifest/v1", "sequence": sequence, **body}
    info, _created = index.put(
        KIND_RUN_MANIFEST,
        f"{request_digest}:{sequence}",
        document,
        request_digest=request_digest,
        registrar=registrar,
    )
    return info.digest
