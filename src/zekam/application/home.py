"""ZEKAM_HOME yerlesimi ve core/user-data ayrimi.

Kurallar:

- Kullanici verisi yalnizca `ZEKAM_HOME` altinda tutulur.
- Source repository (core) ile `ZEKAM_HOME` fiziksel olarak ayridir; biri digerinin
  icinde olamaz.
- Her dizinin tek bir sahiplik sinifi vardir.
- Yerlesim `layout.json` ile surumlenir; beklenmeyen surum sessizce yukseltilmez.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from zekam.application.environment import environment_value
from zekam.domain.errors import ConfigurationError, LayoutError
from zekam.domain.identity import PRODUCT
from zekam.domain.ownership import OwnershipClass

LAYOUT_SCHEMA = "zekam-home-layout/v2"
LAYOUT_FILE = "layout.json"


@dataclass(frozen=True, slots=True)
class HomeEntry:
    """ZEKAM_HOME icindeki tek bir dizin."""

    relative: str
    ownership: OwnershipClass
    description: str


#: Kanonik ZEKAM_HOME dizin sozlesmesi.
HOME_ENTRIES: tuple[HomeEntry, ...] = (
    HomeEntry("global", OwnershipClass.USER_DATA, "Proje bagimsiz kullanici verisi"),
    HomeEntry("global/arastirmalar", OwnershipClass.USER_DATA, "Global arastirmalar"),
    HomeEntry("global/fikirler", OwnershipClass.USER_DATA, "Global fikirler"),
    HomeEntry("global/kararlar", OwnershipClass.USER_DATA, "Global kararlar"),
    HomeEntry("global/referanslar", OwnershipClass.USER_DATA, "Global referanslar"),
    HomeEntry("global/gunlukler", OwnershipClass.USER_DATA, "Global gunlukler"),
    HomeEntry("global/kavramlar", OwnershipClass.USER_DATA, "Global kavramlar"),
    HomeEntry("global/baglantilar", OwnershipClass.USER_DATA, "Global baglantilar"),
    HomeEntry("global/politikalar", OwnershipClass.USER_DATA, "Kullanici politika kayitlari"),
    HomeEntry("global/bellek", OwnershipClass.USER_DATA, "Global bellek projeksiyonu"),
    HomeEntry("global/raporlar", OwnershipClass.ARTIFACT, "Gunluk ve sistem raporlari"),
    HomeEntry("projeler", OwnershipClass.USER_DATA, "Kayitli proje kokleri"),
    HomeEntry("state", OwnershipClass.RUNTIME, "Operational state authority"),
    HomeEntry("state/snapshots", OwnershipClass.ARTIFACT, "Operational snapshots"),
    HomeEntry("state/backups", OwnershipClass.ARTIFACT, "Operational backups"),
    HomeEntry("state/manifests", OwnershipClass.DERIVED, "State manifests"),
    HomeEntry("modeller", OwnershipClass.USER_DATA, "Model laboratuvari"),
    HomeEntry("modeller/registry", OwnershipClass.RUNTIME, "Model registry"),
    HomeEntry("modeller/discovery", OwnershipClass.ARTIFACT, "Discovery snapshots"),
    HomeEntry("modeller/providers", OwnershipClass.USER_DATA, "Provider profilleri"),
    HomeEntry("modeller/profiller", OwnershipClass.USER_DATA, "Model profilleri"),
    HomeEntry("modeller/observations", OwnershipClass.ARTIFACT, "Model observations"),
    HomeEntry("modeller/routing", OwnershipClass.DERIVED, "Routing projeksiyonlari"),
    HomeEntry("modeller/raporlar", OwnershipClass.ARTIFACT, "Model raporlari"),
    HomeEntry("benchmarklar", OwnershipClass.ARTIFACT, "Benchmark authority root"),
    HomeEntry("benchmarklar/planlar", OwnershipClass.USER_DATA, "Benchmark planlari"),
    HomeEntry("benchmarklar/kosular", OwnershipClass.ARTIFACT, "Benchmark kosulari"),
    HomeEntry("benchmarklar/artifacts", OwnershipClass.ARTIFACT, "Benchmark artifacts"),
    HomeEntry("benchmarklar/baselines", OwnershipClass.USER_DATA, "Benchmark baselines"),
    HomeEntry("benchmarklar/raporlar", OwnershipClass.ARTIFACT, "Benchmark raporlari"),
    HomeEntry("benchmarklar/quarantine", OwnershipClass.ARTIFACT, "Benchmark quarantine"),
    HomeEntry("knowledge-index", OwnershipClass.DERIVED, "Rebuildable knowledge index"),
    HomeEntry("knowledge-index/exact", OwnershipClass.DERIVED, "Exact index"),
    HomeEntry("knowledge-index/lexical", OwnershipClass.DERIVED, "Lexical index"),
    HomeEntry("knowledge-index/vector", OwnershipClass.DERIVED, "Vector index"),
    HomeEntry("knowledge-index/manifests", OwnershipClass.DERIVED, "Index manifests"),
    HomeEntry("knowledge-index/snapshots", OwnershipClass.DERIVED, "Index snapshots"),
    HomeEntry("knowledge-index/quarantine", OwnershipClass.DERIVED, "Index quarantine"),
    HomeEntry("analytics", OwnershipClass.DERIVED, "Rebuildable analytics"),
    HomeEntry("analytics/imports", OwnershipClass.ARTIFACT, "Analytics imports"),
    HomeEntry("analytics/exports", OwnershipClass.ARTIFACT, "Analytics exports"),
    HomeEntry("analytics/manifests", OwnershipClass.DERIVED, "Analytics manifests"),
    HomeEntry("telemetry", OwnershipClass.ARTIFACT, "Telemetry authority"),
    HomeEntry("telemetry/events", OwnershipClass.ARTIFACT, "Atomic event segments"),
    HomeEntry("telemetry/manifests", OwnershipClass.DERIVED, "Telemetry manifests"),
    HomeEntry("telemetry/quarantine", OwnershipClass.ARTIFACT, "Telemetry quarantine"),
    HomeEntry("artifacts", OwnershipClass.ARTIFACT, "Content addressed artifacts"),
    HomeEntry("artifacts/sha256", OwnershipClass.ARTIFACT, "SHA-256 object namespace"),
    HomeEntry("runtime", OwnershipClass.RUNTIME, "Runtime root"),
    HomeEntry("runtime/locks", OwnershipClass.RUNTIME, "Locks"),
    HomeEntry("runtime/leases", OwnershipClass.RUNTIME, "Leases"),
    HomeEntry("runtime/outbox", OwnershipClass.RUNTIME, "Outbox"),
    HomeEntry("runtime/spool", OwnershipClass.RUNTIME, "Spool"),
    HomeEntry("runtime/health", OwnershipClass.RUNTIME, "Health snapshots"),
    HomeEntry("runtime/tmp", OwnershipClass.RUNTIME, "Bounded temporary files"),
    HomeEntry("runtime/recovery", OwnershipClass.RUNTIME, "Recovery records"),
    HomeEntry("gelen-belgeler", OwnershipClass.ARTIFACT, "Izlenen gelen belge klasoru"),
    HomeEntry("inbox", OwnershipClass.ARTIFACT, "Gelen isler"),
    HomeEntry("archive", OwnershipClass.ARTIFACT, "Arsiv"),
    HomeEntry("sandboxlar", OwnershipClass.LOCAL, "Yalitilmis calisma alanlari"),
    HomeEntry("secrets", OwnershipClass.SECRET, "Yerel secret referans deposu"),
    HomeEntry("yerel", OwnershipClass.LOCAL, "Makineye ozel gecici veri"),
)

#: Proje basina olusturulan alt dizinler.
PROJECT_ENTRIES: tuple[HomeEntry, ...] = (
    HomeEntry("baglantilar", OwnershipClass.USER_DATA, "Source binding kayitlari"),
    HomeEntry("raporlar", OwnershipClass.ARTIFACT, "Proje raporlari"),
    HomeEntry("arastirmalar", OwnershipClass.USER_DATA, "Arastirma kayitlari"),
    HomeEntry("fikirler", OwnershipClass.USER_DATA, "Fikir kayitlari"),
    HomeEntry("kararlar", OwnershipClass.USER_DATA, "Karar kayitlari"),
    HomeEntry("planlar", OwnershipClass.USER_DATA, "Plan kayitlari"),
    HomeEntry("gorevler", OwnershipClass.USER_DATA, "Gorev kayitlari"),
    HomeEntry("referanslar", OwnershipClass.USER_DATA, "Referans kayitlari"),
    HomeEntry("notlar", OwnershipClass.USER_DATA, "Proje notlari"),
    HomeEntry("bellek", OwnershipClass.USER_DATA, "Proje bellegi projeksiyonu"),
    HomeEntry("artifacts", OwnershipClass.ARTIFACT, "Proje artifact deposu"),
    HomeEntry("runtime", OwnershipClass.RUNTIME, "Proje runtime durumu"),
)

#: Yalnizca sahibi tarafindan okunabilmesi gereken dizinler.
RESTRICTED_ENTRIES: frozenset[str] = frozenset({"secrets"})


def default_home() -> Path:
    """Ortam degiskeni yoksa kullanilacak varsayilan kok."""
    return Path.home() / f".{PRODUCT.slug}"


def resolve_home(explicit: str | os.PathLike[str] | None = None) -> Path:
    """`ZEKAM_HOME` kokunu cozer.

    Oncelik: acik parametre, ortam degiskeni, varsayilan kok.
    """
    if explicit is not None:
        raw = Path(explicit)
    else:
        env_value = environment_value(os.environ, PRODUCT.data_root_env)
        raw = Path(env_value) if env_value else default_home()
    if not raw.is_absolute():
        raw = raw.resolve()
    return raw


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def assert_separated_from_core(home: Path, core_root: Path) -> None:
    """Core source ile kullanici verisinin fiziksel ayrimini dogrular."""
    # ``strict=False`` mevcut parent symlink'lerini de cozer. Yalnizca leaf'in
    # varligini kontrol etmek ``alias -> core/hidden-home`` gecisine izin verir.
    home_resolved = home.resolve(strict=False)
    core_resolved = core_root.resolve(strict=False)
    if home_resolved == core_resolved:
        raise ConfigurationError("ZEKAM_HOME core source kokuyle ayni olamaz")
    if _is_within(home_resolved, core_resolved):
        raise ConfigurationError("ZEKAM_HOME core source agacinin icinde olamaz")
    if _is_within(core_resolved, home_resolved):
        raise ConfigurationError("Core source agaci ZEKAM_HOME icinde olamaz")


@dataclass(frozen=True, slots=True)
class LayoutIssue:
    """Yerlesim dogrulamasinda bulunan tek bir sorun."""

    kind: str
    relative: str
    detail: str


@dataclass(frozen=True, slots=True)
class HomeLayout:
    """Cozulmus ve dogrulanabilir ZEKAM_HOME yerlesimi."""

    root: Path

    @property
    def layout_file(self) -> Path:
        return self.root / LAYOUT_FILE

    def path(self, relative: str) -> Path:
        """Kok altindaki guvenli mutlak yolu dondurur."""
        candidate = (self.root / relative).resolve()
        root = self.root.resolve()
        if candidate != root and not _is_within(candidate, root):
            raise LayoutError("Yol ZEKAM_HOME disina cikiyor")
        return candidate

    def project_root(self, project_id: str) -> Path:
        """Proje kokunu dondurur."""
        if not project_id or "/" in project_id or "\\" in project_id or project_id in {".", ".."}:
            raise LayoutError("Gecersiz proje kimligi")
        return self.path(f"projeler/{project_id}")

    def entries(self) -> Sequence[HomeEntry]:
        return HOME_ENTRIES

    def ensure(self) -> HomeLayout:
        """Eksik dizinleri ve layout.json dosyasini olusturur (idempotent)."""
        if self.root.exists() and self.layout_file.exists():
            layout_issues = list(self._verify_layout_file())
            if layout_issues:
                raise LayoutError("Mevcut layout schema sessizce overwrite edilemez")
        elif self.root.exists() and any(self.root.iterdir()):
            raise LayoutError("Layout authority olmayan dolu ZEKAM_HOME bootstrap edilemez")
        self.root.mkdir(parents=True, exist_ok=True)
        for entry in HOME_ENTRIES:
            target = self.path(entry.relative)
            target.mkdir(parents=True, exist_ok=True)
            if entry.relative in RESTRICTED_ENTRIES:
                _restrict(target)
        self._write_layout_file()
        return self

    def ensure_project(self, project_id: str) -> Path:
        """Proje dizin agacini olusturur (idempotent)."""
        root = self.project_root(project_id)
        root.mkdir(parents=True, exist_ok=True)
        for entry in PROJECT_ENTRIES:
            (root / entry.relative).mkdir(parents=True, exist_ok=True)
        return root

    def verify(self) -> list[LayoutIssue]:
        """Yerlesimi dogrular ve bulunan sorunlari dondurur."""
        issues: list[LayoutIssue] = []
        if not self.root.exists():
            return [LayoutIssue("missing-root", ".", "ZEKAM_HOME koku yok")]
        for entry in HOME_ENTRIES:
            target = self.root / entry.relative
            if not target.exists():
                issues.append(LayoutIssue("missing-directory", entry.relative, "Dizin yok"))
            elif not target.is_dir():
                issues.append(LayoutIssue("not-a-directory", entry.relative, "Dizin degil"))
        issues.extend(self._verify_layout_file())
        return issues

    def _verify_layout_file(self) -> Iterable[LayoutIssue]:
        if not self.layout_file.exists():
            yield LayoutIssue("missing-layout-file", LAYOUT_FILE, "layout.json yok")
            return
        try:
            document = json.loads(self.layout_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            yield LayoutIssue("unreadable-layout-file", LAYOUT_FILE, "layout.json okunamadi")
            return
        if document.get("schema") != LAYOUT_SCHEMA:
            yield LayoutIssue(
                "unsupported-layout-schema",
                LAYOUT_FILE,
                f"Beklenen {LAYOUT_SCHEMA}, bulunan {document.get('schema')!r}",
            )

    def _write_layout_file(self) -> None:
        document = {
            "schema": LAYOUT_SCHEMA,
            "product": PRODUCT.name,
            "data_root_env": PRODUCT.data_root_env,
            "entries": [
                {
                    "path": entry.relative,
                    "ownership": entry.ownership.value,
                    "description": entry.description,
                }
                for entry in HOME_ENTRIES
            ],
            "project_entries": [
                {
                    "path": entry.relative,
                    "ownership": entry.ownership.value,
                    "description": entry.description,
                }
                for entry in PROJECT_ENTRIES
            ],
        }
        tmp = self.layout_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        tmp.replace(self.layout_file)


def _restrict(target: Path) -> None:
    """Mumkun oldugunda dizini yalnizca sahibine acik hale getirir."""
    if os.name == "nt":  # pragma: no cover - Windows ACL ayri politika ile yonetilir
        return
    target.chmod(0o700)
