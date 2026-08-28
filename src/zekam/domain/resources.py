"""Logical resource kimlikleri ve kilit catisma kurallari.

Portable resource ornekleri (`harness/KILIT_CLAIM_RECEIPT_RECOVERY.md`):

```text
project:<project-id>
work:<project-id>:<work-id>
path:<project-id>:src/module/file.py
db-object:<project-id>:postgresql:table:payments
artifact:<project-id>:<artifact-id>
provider:<provider-ref>:<operation>
model-benchmark:<model-id>:<suite-id>
```

Kurallar:

- Absolute path, `..`, ters bolu ve secret degeri reddedilir.
- Yollar posix formuna normalize edilir; buyuk/kucuk harf korunur fakat
  karsilastirma normalize edilmis metin uzerinden yapilir.
- Ayni resource icin en az biri write ise catisma vardir.
- `project:<id>` write kilidi ayni projenin butun resource'lariyla catisir.
- Path parent/child iliskisinde en az biri write ise catisma vardir.
- Farkli projeler varsayilan olarak catismaz.
- Deadlock'u onlemek icin kilitler lexical sirada alinir.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from zekam.domain.errors import ValidationFailed

#: Resource kimliginde izin verilen karakterler.
_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._:/@-]+$")

#: Proje kapsami tasimayan resource turleri.
GLOBAL_KINDS: frozenset[str] = frozenset({"provider", "model-benchmark", "skill-registry"})

#: Bilinen resource turleri.
KNOWN_KINDS: frozenset[str] = frozenset(
    {
        "project",
        "work",
        "path",
        "db-object",
        "artifact",
        "provider",
        "model-benchmark",
        "skill-registry",
        "memory",
        "runtime-bootstrap",
    }
)


class LockMode(StrEnum):
    """Kilit modu."""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True, order=True)
class LogicalResource:
    """Normalize edilmis logical resource kimligi."""

    kind: str
    scope: str
    rest: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KNOWN_KINDS:
            raise ValidationFailed(f"Bilinmeyen resource turu: {self.kind}")
        if not self.scope:
            raise ValidationFailed("Resource kapsami bos olamaz")

    @classmethod
    def parse(cls, text: str) -> LogicalResource:
        """Metinden resource uretir ve guvenlik kurallarini uygular."""
        candidate = text.strip()
        if not candidate:
            raise ValidationFailed("Resource bos olamaz")
        if "\\" in candidate:
            raise ValidationFailed("Resource ters bolu iceremez")
        if not _SEGMENT_PATTERN.match(candidate):
            raise ValidationFailed("Resource izin verilmeyen karakter iceriyor")

        parts = candidate.split(":")
        kind = parts[0]
        if kind not in KNOWN_KINDS:
            raise ValidationFailed(f"Bilinmeyen resource turu: {kind}")
        if len(parts) < 2:
            raise ValidationFailed("Resource en az tur ve kapsam icermeli")

        scope = parts[1]
        rest = ":".join(parts[2:])
        if kind == "path":
            rest = _normalize_path(rest)
        return cls(kind=kind, scope=scope, rest=rest)

    @property
    def text(self) -> str:
        """Kanonik metin gosterimi."""
        return f"{self.kind}:{self.scope}" + (f":{self.rest}" if self.rest else "")

    @property
    def project(self) -> str | None:
        """Proje kapsami; global resource'lar icin `None`."""
        return None if self.kind in GLOBAL_KINDS else self.scope

    @property
    def path_parts(self) -> tuple[str, ...]:
        return tuple(self.rest.split("/")) if self.kind == "path" and self.rest else ()

    def is_ancestor_of(self, other: LogicalResource) -> bool:
        """Bu yol digerinin ust dizini mi?"""
        if self.kind != "path" or other.kind != "path" or self.project != other.project:
            return False
        mine, theirs = self.path_parts, other.path_parts
        return len(mine) < len(theirs) and theirs[: len(mine)] == mine

    def __str__(self) -> str:
        return self.text


def _normalize_path(raw: str) -> str:
    if not raw:
        raise ValidationFailed("Path resource bir yol icermeli")
    candidate = raw.replace("\\", "/")
    if candidate.startswith("/") or re.match(r"^[A-Za-z]:/", candidate):
        raise ValidationFailed("Path resource mutlak yol olamaz")
    segments = [segment for segment in candidate.split("/") if segment not in {"", "."}]
    if any(segment == ".." for segment in segments):
        raise ValidationFailed("Path resource `..` iceremez")
    if not segments:
        raise ValidationFailed("Path resource bos olamaz")
    return "/".join(segments)


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    """Bir adimin istedigi kilit."""

    resource: LogicalResource
    mode: LockMode

    @classmethod
    def parse(cls, text: str, mode: LockMode) -> ResourceRequest:
        return cls(resource=LogicalResource.parse(text), mode=mode)

    @property
    def is_write(self) -> bool:
        return self.mode is LockMode.WRITE

    def as_dict(self) -> dict[str, str]:
        return {"resource": self.resource.text, "mode": self.mode.value}


def conflicts(left: ResourceRequest, right: ResourceRequest) -> bool:
    """Iki kilit istegi catisiyor mu?"""
    if not left.is_write and not right.is_write:
        # Iki okuma hicbir zaman catismaz.
        return False

    first, second = left.resource, right.resource
    if first.project is not None and second.project is not None and first.project != second.project:
        return False
    if first.project is None or second.project is None:
        # Global resource'lar yalnizca birebir eslesmede catisir.
        return first == second

    # Proje kilidi ayni projedeki her seyle catisir.
    if first.kind == "project" or second.kind == "project":
        return True

    if first == second:
        return True
    if first.kind == "path" and second.kind == "path":
        return first.is_ancestor_of(second) or second.is_ancestor_of(first)
    return False


def conflicting_pairs(
    requests: Sequence[ResourceRequest], held: Sequence[ResourceRequest]
) -> tuple[tuple[ResourceRequest, ResourceRequest], ...]:
    """Istenen ve tutulan kilitler arasindaki butun catismalari dondurur."""
    found: list[tuple[ResourceRequest, ResourceRequest]] = []
    for wanted in requests:
        for existing in held:
            if conflicts(wanted, existing):
                found.append((wanted, existing))
    return tuple(found)


def has_internal_conflict(requests: Sequence[ResourceRequest]) -> bool:
    """Ayni istek kumesi kendi icinde catisiyor mu?"""
    for index, first in enumerate(requests):
        for second in requests[index + 1 :]:
            if conflicts(first, second):
                return True
    return False


def lock_order(requests: Iterable[ResourceRequest]) -> tuple[ResourceRequest, ...]:
    """Deadlock'u onlemek icin kilitleri kararli lexical sirada dondurur."""
    return tuple(sorted(requests, key=lambda request: (request.resource.text, request.mode.value)))


def parse_requests(
    read: Iterable[str] = (), write: Iterable[str] = ()
) -> tuple[ResourceRequest, ...]:
    """Okuma ve yazma listelerinden kilit isteklerini uretir."""
    requests = [ResourceRequest.parse(text, LockMode.READ) for text in read]
    requests += [ResourceRequest.parse(text, LockMode.WRITE) for text in write]
    return lock_order(requests)
