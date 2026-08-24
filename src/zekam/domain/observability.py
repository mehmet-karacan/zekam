"""Telemetri, komut sozlesmesi ve salt okunur projeksiyon sozlesmesi.

Telemetri **secret ve kaynak icerigi tasimaz**: yalniz kimlik, sayi, sure ve
digest tasir. Dashboard ve graph gorunumu derived'dir; authority uretmez ve
kanonik kayda drill-down baglantisi verir.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_ATTRIBUTE_CHARS = 512

_SENSITIVE_KEY = re.compile(
    r"(?:secret|credential|password|parola|api[-_ ]?key|private[-_ ]?key|token|"
    r"authorization|cookie|prompt|response|content|body)",
    re.IGNORECASE,
)
# Deger tarafinda hem ham secret bicimlerini hem de "ANAHTAR=deger" seklindeki
# atamalari yakalamak gerekir: yasak alan adi yerine mesru bir ada gizlenmis
# secret aksi halde telemetriye sizardi.
_SENSITIVE_VALUE = re.compile(
    r"(?:-----BEGIN|Bearer\s+[A-Za-z0-9._-]{8,}|[A-Za-z0-9+/]{40,}={0,2}|"
    r"(?:secret|credential|password|parola|api[-_ ]?key|private[-_ ]?key|token)"
    r"\s*[:=]\s*\S)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(r"(?:^|[\s=])(?:[A-Za-z]:\\|/(?:home|Users|root)/)")


class Surface(StrEnum):
    CLI = "cli"
    API = "api"
    MCP = "mcp"
    SCHEDULER = "scheduler"


class SpanKind(StrEnum):
    REQUEST = "request"
    USE_CASE = "use-case"
    ADAPTER = "adapter"
    DATABASE = "database"


@dataclass(frozen=True, slots=True)
class CommandContract:
    """Bir kanonik komutun sozlesmesi.

    CLI, API ve MCP ayni use-case'i cagirir; yuzey kendi urun kuralini yazmaz.
    """

    name: str
    summary: str
    mutating: bool
    requires_apply_flag: bool = False
    requires_authorization: bool = False
    exit_codes: tuple[int, ...] = (0,)

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.summary.strip():
            raise ValidationFailed("komut adi ve ozeti bos olamaz")
        if self.mutating and not self.requires_apply_flag:
            raise PolicyViolation("mutasyon yapan komut acik --uygula bayragi ister")
        if self.requires_authorization and not self.mutating:
            raise ValidationFailed("salt okunur komut authorization istemez")
        if 0 not in self.exit_codes:
            raise ValidationFailed("komut basarili cikis kodunu bildirmeli")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "mutating": self.mutating,
            "requires_apply_flag": self.requires_apply_flag,
            "requires_authorization": self.requires_authorization,
            "exit_codes": list(self.exit_codes),
        }


#: Kanonik CLI yuzeyi. Yuzeyler bu listeden turer.
CANONICAL_COMMANDS: tuple[CommandContract, ...] = (
    CommandContract("doctor", "Kurulum ve baglanti sagligini raporlar", mutating=False),
    CommandContract(
        "init", "ZEKAM_HOME yerlesimini olusturur", mutating=True, requires_apply_flag=True
    ),
    CommandContract(
        "db upgrade", "Migration'lari uygular", mutating=True, requires_apply_flag=True
    ),
    CommandContract("project add", "Proje kaydeder", mutating=True, requires_apply_flag=True),
    CommandContract(
        "project remove",
        "Projeyi arsivler",
        mutating=True,
        requires_apply_flag=True,
        requires_authorization=True,
    ),
    CommandContract(
        "project restore",
        "Arsivlenmis projeyi geri getirir",
        mutating=True,
        requires_apply_flag=True,
        requires_authorization=True,
    ),
    CommandContract("project list", "Projeleri listeler", mutating=False),
    CommandContract("work create", "Is kaydi olusturur", mutating=True, requires_apply_flag=True),
    CommandContract("work list", "Isleri listeler", mutating=False),
    CommandContract("ask", "Dogal dil istegini cozer", mutating=False),
    CommandContract("research dag", "Kanonik rol DAG'ini gosterir", mutating=False),
    CommandContract(
        "research start", "Arastirma sorusu olusturur", mutating=True, requires_apply_flag=True
    ),
    CommandContract("model inventory", "Model envanterini gosterir veya aktarir", mutating=False),
    CommandContract("model health", "Model saglik durumunu raporlar", mutating=False),
    CommandContract("knowledge scan", "Dizini salt okunur tarar", mutating=False),
    CommandContract(
        "knowledge ingest",
        "Belgeyi normalize eder ve aktive eder",
        mutating=True,
        requires_apply_flag=True,
    ),
    CommandContract("sandbox policy", "Sandbox politikasini gosterir", mutating=False),
    CommandContract("git commit-check", "Commit mesajini dogrular", mutating=False),
    CommandContract("git push-check", "Push kapisini degerlendirir", mutating=False),
    CommandContract("scheduler list", "Zamanlanmis isleri listeler", mutating=False),
    CommandContract(
        "scheduler init",
        "Zorunlu bakim islerini tanimlar",
        mutating=True,
        requires_apply_flag=True,
    ),
    CommandContract("report today", "Gunun raporunu okur", mutating=False),
    CommandContract("backup verify", "Yedek butunlugunu dogrular", mutating=False),
    CommandContract("ui serve", "Salt okunur Neuro Observatory arayuzunu baslatir", mutating=False),
    CommandContract("worker settings", "Worker sinirlarini gosterir", mutating=False),
    CommandContract(
        "worker tick",
        "Tek worker dongusu calistirir",
        mutating=True,
        requires_apply_flag=True,
    ),
    CommandContract(
        "worker run", "Worker dongusunu baslatir", mutating=True, requires_apply_flag=True
    ),
)


def command_names() -> tuple[str, ...]:
    return tuple(item.name for item in CANONICAL_COMMANDS)


def missing_commands(available: tuple[str, ...]) -> tuple[str, ...]:
    """Kanonik yuzeyde tanimli olup uygulanmamis komutlar."""

    return tuple(name for name in command_names() if name not in available)


@dataclass(frozen=True, slots=True)
class TelemetryAttribute:
    """Tek telemetri alani. Secret ve icerik tasiyamaz."""

    key: str
    value: str | int | float | bool

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValidationFailed("telemetri anahtari bos olamaz")
        if _SENSITIVE_KEY.search(self.key):
            raise PolicyViolation(f"telemetri alani yasak: {self.key}")
        text = str(self.value)
        if len(text) > MAX_ATTRIBUTE_CHARS:
            raise ValidationFailed("telemetri degeri bounded sinirini asiyor")
        if _SENSITIVE_VALUE.search(text):
            raise PolicyViolation("telemetri degeri secret benzeri icerik tasiyamaz")
        if _ABSOLUTE_PATH.search(text):
            raise PolicyViolation("telemetri degeri kisisel absolute path tasiyamaz")

    def as_pair(self) -> tuple[str, str | int | float | bool]:
        return self.key, self.value


@dataclass(frozen=True, slots=True)
class TelemetrySpan:
    """Yapisal olay. Correlation zorunlu, icerik yasak."""

    name: str
    kind: SpanKind
    surface: Surface
    trace_id: str
    span_id: str
    started_at: dt.datetime
    duration_ms: int
    attributes: tuple[TelemetryAttribute, ...] = field(default_factory=tuple)
    parent_span_id: str | None = None
    error_category: str | None = None

    def __post_init__(self) -> None:
        for label, value in (("trace_id", self.trace_id), ("span_id", self.span_id)):
            if not value.strip():
                raise ValidationFailed(f"{label} bos olamaz")
        if self.duration_ms < 0:
            raise ValidationFailed("sure negatif olamaz")
        if self.started_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")
        if self.parent_span_id == self.span_id:
            raise ValidationFailed("span kendi ebeveyni olamaz")
        keys = [item.key for item in self.attributes]
        if len(set(keys)) != len(keys):
            raise ValidationFailed("telemetri anahtari tekrar edemez")

    @property
    def succeeded(self) -> bool:
        return self.error_category is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": str(self.kind),
            "surface": str(self.surface),
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "duration_ms": self.duration_ms,
            "error_category": self.error_category,
            "attributes": dict(item.as_pair() for item in self.attributes),
        }


def correlate(spans: tuple[TelemetrySpan, ...]) -> dict[str, tuple[str, ...]]:
    """Trace kimliginden span kimliklerine esleme."""

    grouped: dict[str, list[str]] = {}
    for span in spans:
        grouped.setdefault(span.trace_id, []).append(span.span_id)
    return {trace: tuple(sorted(items)) for trace, items in grouped.items()}


@dataclass(frozen=True, slots=True)
class ProjectionTile:
    """Dashboard karesi. Sayilar kanonik kayittan turer."""

    key: str
    title: str
    value: int
    drill_down: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValidationFailed("projeksiyon degeri negatif olamaz")
        if not self.drill_down.strip():
            raise ValidationFailed("her kare kanonik kayda drill-down baglantisi ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "value": self.value,
            "drill_down": self.drill_down,
            "detail": self.detail,
        }


#: Dashboard'da bulunmasi zorunlu projeksiyonlar.
REQUIRED_TILES = (
    "work",
    "run",
    "model",
    "knowledge",
    "memory",
    "scheduler",
)


@dataclass(frozen=True, slots=True)
class OperationsDashboard:
    """Salt okunur operasyon panosu. Authority uretmez."""

    generated_at: dt.datetime
    tiles: tuple[ProjectionTile, ...]
    read_only: bool = True
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not self.read_only:
            raise PolicyViolation("dashboard salt okunurdur")
        if self.grants_authority:
            raise PolicyViolation("dashboard authority veremez")
        keys = tuple(item.key for item in self.tiles)
        missing = tuple(name for name in REQUIRED_TILES if name not in keys)
        if missing:
            raise ValidationFailed(f"dashboard eksik projeksiyon: {', '.join(missing)}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-dashboard/v1",
            "tiles": [item.as_dict() for item in self.tiles],
            "read_only": True,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    kind: str
    label: str
    canonical_ref: str

    def __post_init__(self) -> None:
        if not self.canonical_ref.strip():
            raise ValidationFailed("graph dugumu kanonik referans ister")

    def as_dict(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "label": self.label,
            "canonical_ref": self.canonical_ref,
        }


@dataclass(frozen=True, slots=True)
class GraphEdge:
    source: str
    target: str
    kind: str

    def __post_init__(self) -> None:
        if self.source == self.target:
            raise ValidationFailed("kenar kendine baglanamaz")

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "target": self.target, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class DerivedGraph:
    """Sinaps gorunumu. Derived'dir: kaybolursa yeniden uretilir."""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    source_digest: str
    derived: bool = True
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not self.derived:
            raise PolicyViolation("graph gorunumu derived olmak zorundadir")
        if self.grants_authority:
            raise PolicyViolation("graph authority veremez")
        parse_digest(self.source_digest)
        known = {item.node_id for item in self.nodes}
        for edge in self.edges:
            if edge.source not in known or edge.target not in known:
                raise ValidationFailed("kenar bilinmeyen dugume isaret ediyor")

    def drill_down(self, node_id: str) -> str:
        """Bir dugumden kanonik kayda inis referansi."""

        for node in self.nodes:
            if node.node_id == node_id:
                return node.canonical_ref
        raise ValidationFailed("dugum bulunamadi")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-derived-graph/v1",
            "nodes": [item.as_dict() for item in self.nodes],
            "edges": [item.as_dict() for item in self.edges],
            "source_digest": self.source_digest,
            "derived": True,
            "grants_authority": False,
        }

    @property
    def graph_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class McpCapability:
    """MCP tarafinda acilan yetenek. Authority Zekam'de kalir."""

    name: str
    kind: str
    mutating: bool = False
    requires_authorization: bool = False

    def __post_init__(self) -> None:
        if self.kind not in {"tool", "resource", "prompt"}:
            raise ValidationFailed("MCP yetenek turu taninmiyor")
        if self.mutating and not self.requires_authorization:
            raise PolicyViolation("mutasyon yapan MCP araci authorization ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "mutating": self.mutating,
            "requires_authorization": self.requires_authorization,
        }


@dataclass(frozen=True, slots=True)
class McpNegotiation:
    """Istemciyle uzlasilan yetenek kumesi.

    Istemcinin destekledigi yetenek kumesi disina cikilmaz; Zekam kendi
    otoritesini MCP'ye devretmez.
    """

    client_supported: frozenset[str]
    offered: tuple[McpCapability, ...]
    authority_owner: str = "zekam"

    def __post_init__(self) -> None:
        if self.authority_owner != "zekam":
            raise PolicyViolation("MCP adapteri authority sahibi olamaz")

    def negotiated(self) -> tuple[McpCapability, ...]:
        return tuple(item for item in self.offered if item.kind in self.client_supported)

    def rejected(self) -> tuple[McpCapability, ...]:
        return tuple(item for item in self.offered if item.kind not in self.client_supported)

    def as_dict(self) -> dict[str, Any]:
        return {
            "client_supported": sorted(self.client_supported),
            "negotiated": [item.as_dict() for item in self.negotiated()],
            "rejected": [item.as_dict() for item in self.rejected()],
            "authority_owner": "zekam",
        }
