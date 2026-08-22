"""Proje entegrasyon yasam dongusu.

Asamalar sirayla ilerler ve her ilerleme kendi kanitini ister:

```text
registered -> bound -> discovered -> profiled -> current
                                        |
                                        +-> stale   (kaynak degisti)
                                        +-> unbound (yerel yol kayboldu)
```

Kurallar:

- Harici kaynak koku hicbir kosulda yazilmaz.
- Kaynak surumu degisirse asama otomatik `current` kalmaz; `stale` olur ve yeni
  tarama gerekir.
- Rapor her zaman tek bir "sonraki guvenli aksiyon" uretir.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from zekam.application.capability_profile import CapabilityProfile, build_profile
from zekam.application.source_discovery import DiscoveryPolicy, DiscoveryReport, discover
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7, normalize_slug, validate_slug
from zekam.domain.project import (
    BindingStatus,
    IntegrationStage,
    Project,
    ProjectAlias,
    SourceBinding,
    SourceBindingKind,
    SourceRevision,
    SourceRevisionKind,
)
from zekam.domain.realm import Realm
from zekam.infrastructure.git import source_reader
from zekam.infrastructure.postgres.project_repository import (
    CapabilityProfileRepository,
    IntegrationStateRepository,
    ProjectRepository,
    SourceBindingRepository,
)


def locator_digest_for(path: Path) -> str:
    """Fiziksel konumun tek yonlu parmak izi.

    Yolun kendisi kanonik kayda yazilmaz; yalnizca tasinma tespiti icin digest
    saklanir.
    """
    return digest_of_bytes(str(path.resolve()).encode("utf-8"))


@dataclass(frozen=True, slots=True)
class IntegrationReport:
    """Bir projenin entegrasyon durumu ve sonraki guvenli aksiyonu."""

    project: Project
    stage: IntegrationStage
    next_action: str
    is_stale: bool
    binding: SourceBinding | None
    current_revision: SourceRevision | None
    observed_revision: SourceRevision | None
    profile_digest: str | None
    blockers: tuple[str, ...] = ()
    knowledge_index: dict[str, Any] | None = None

    @property
    def is_current(self) -> bool:
        """Kaynak taramasinin kaydedilmis revision ile guncel olup olmadigi."""
        return self.stage is IntegrationStage.CURRENT and not self.is_stale

    @property
    def is_fully_integrated(self) -> bool:
        """Guncel kaynagin knowledge index'i de hazirsa true dondurur."""
        index_ready = self.knowledge_index is None or self.knowledge_index.get("state") == "ready"
        return self.is_current and index_ready

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project.as_dict(),
            "stage": self.stage.value,
            "next_action": self.next_action,
            "is_stale": self.is_stale,
            "is_current": self.is_current,
            "is_fully_integrated": self.is_fully_integrated,
            "binding": None if self.binding is None else self.binding.as_dict(),
            "recorded_revision": (
                None if self.current_revision is None else self.current_revision.as_dict()
            ),
            "observed_revision": (
                None if self.observed_revision is None else self.observed_revision.as_dict()
            ),
            "profile_digest": self.profile_digest,
            "knowledge_index": self.knowledge_index,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Tarama sonucu."""

    project: Project
    revision: SourceRevision
    discovery: DiscoveryReport
    profile: CapabilityProfile
    stage: IntegrationStage
    changed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": self.project.as_dict(),
            "revision": self.revision.as_dict(),
            "discovery": self.discovery.as_dict(),
            "profile_digest": self.profile.digest,
            "primary_language": self.profile.primary_language,
            "stage": self.stage.value,
            "changed": self.changed,
        }


@dataclass(frozen=True, slots=True)
class ProjectIntegrationService:
    """Proje kaydi, baglama, tarama ve devam raporu."""

    connection: Any
    realm: Realm

    @property
    def projects(self) -> ProjectRepository:
        return ProjectRepository(self.connection, self.realm.id)

    @property
    def bindings(self) -> SourceBindingRepository:
        return SourceBindingRepository(self.connection, self.realm.id)

    @property
    def profiles(self) -> CapabilityProfileRepository:
        return CapabilityProfileRepository(self.connection, self.realm.id)

    @property
    def states(self) -> IntegrationStateRepository:
        return IntegrationStateRepository(self.connection, self.realm.id)

    # -- kayit ------------------------------------------------------------------

    def register(
        self,
        *,
        source_path: Path,
        slug: str | None = None,
        display_name: str | None = None,
        aliases: tuple[str, ...] = (),
        now: dt.datetime | None = None,
    ) -> Project:
        """Projeyi kaydeder ve kaynak agacina salt okunur baglar.

        Kaynak agacina hicbir sey yazilmaz; yalnizca varligi ve turu okunur.
        """
        moment = now or dt.datetime.now(dt.UTC)
        resolved = source_path.expanduser()
        if not resolved.is_dir():
            raise PolicyViolation("Kaynak koku bir dizin olmali")
        resolved = resolved.resolve(strict=True)

        project_slug = validate_slug(slug) if slug else normalize_slug(resolved.name)
        project = Project.create(
            realm=self.realm,
            slug=project_slug,
            display_name=display_name or resolved.name,
            now=moment,
        )
        self.projects.add(project)

        for index, alias in enumerate((project_slug, *aliases)):
            normalized = normalize_slug(alias)
            if index > 0 and normalized == project_slug:
                continue
            self.projects.add_alias(
                ProjectAlias.create(project=project, alias=alias, is_primary=index == 0, now=moment)
            )

        kind = (
            SourceBindingKind.GIT_REPOSITORY
            if source_reader.is_git_repository(resolved)
            else SourceBindingKind.DIRECTORY
        )
        binding = SourceBinding(
            id=new_uuid7(now=moment),
            realm_id=self.realm.id,
            project_id=project.id,
            kind=kind,
            root_label=resolved.name,
            locator_digest=locator_digest_for(resolved),
            status=BindingStatus.BOUND,
            created_at=moment,
        )
        self.bindings.bind(binding, absolute_path=resolved)
        self.states.set(project.id, stage=IntegrationStage.BOUND)
        return project

    def rebind(self, project_id: UUID, *, source_path: Path) -> SourceBinding:
        """Kaynak baska bir konuma tasindiginda baglantiyi tazeler."""
        resolved = source_path.expanduser()
        if not resolved.is_dir():
            raise PolicyViolation("Kaynak koku bir dizin olmali")
        resolved = resolved.resolve(strict=True)
        bindings = self.bindings.for_project(project_id)
        if not bindings:
            raise NotFound("Projenin baglantisi yok")
        binding = bindings[0]
        self.bindings.rebind(
            binding.id, absolute_path=resolved, locator_digest=locator_digest_for(resolved)
        )
        self.states.set(project_id, stage=IntegrationStage.BOUND)
        return self.bindings.get(binding.id)

    # -- tarama -----------------------------------------------------------------

    def scan(
        self,
        project_id: UUID,
        *,
        policy: DiscoveryPolicy | None = None,
        now: dt.datetime | None = None,
    ) -> ScanResult:
        """Kaynak agacini tarar, surum gozlemi ve capability profili uretir."""
        moment = now or dt.datetime.now(dt.UTC)
        project = self.projects.get(project_id)
        binding, root = self._require_bound_source(project_id)

        discovery = discover(root, policy=policy)
        observation = source_reader.observe(root)
        previous = self.bindings.latest_revision(binding.id)

        revision = self.bindings.record_revision(
            binding_id=binding.id,
            kind=(
                SourceRevisionKind.GIT_COMMIT
                if observation is not None
                else SourceRevisionKind.TREE_DIGEST
            ),
            revision=observation.commit if observation is not None else discovery.tree_digest,
            tree_digest=discovery.tree_digest,
            branch=observation.branch if observation is not None else None,
            is_dirty=observation.is_dirty if observation is not None else False,
            file_count=discovery.file_count,
            now=moment,
        )

        profile = build_profile(root, discovery)
        self.profiles.store(
            project_id=project_id,
            source_revision_id=revision.id,
            profile=profile,
            now=moment,
        )
        self.states.set(
            project_id,
            stage=IntegrationStage.CURRENT,
            observed_revision_id=revision.id,
            detail={
                "file_count": discovery.file_count,
                "secret_finding_count": len(discovery.secrets),
                "truncated": discovery.truncated,
                "profile_digest": profile.digest,
                "knowledge_index": {"state": "pending"},
            },
        )
        changed = previous is None or not previous.matches(revision)
        return ScanResult(
            project=project,
            revision=revision,
            discovery=discovery,
            profile=profile,
            stage=IntegrationStage.CURRENT,
            changed=changed,
        )

    # -- durum ------------------------------------------------------------------

    def evaluate(self, project_id: UUID) -> IntegrationReport:
        """Kaydedilmis durumu gercek kaynakla karsilastirir ve rapor uretir."""
        project = self.projects.get(project_id)
        stage, _recorded_revision_id, detail = self.states.get(project_id)
        bindings = self.bindings.for_project(project_id)
        binding = bindings[0] if bindings else None
        blockers: list[str] = []

        if binding is None:
            return IntegrationReport(
                project=project,
                stage=IntegrationStage.REGISTERED,
                next_action="Kaynak agacini `zekam project rebind` ile baglayin",
                is_stale=False,
                binding=None,
                current_revision=None,
                observed_revision=None,
                profile_digest=None,
                blockers=("source-binding-missing",),
            )

        recorded = self.bindings.latest_revision(binding.id)
        local_path = self.bindings.local_path(binding.id)

        if local_path is None or not local_path.is_dir():
            blockers.append("local-path-unavailable")
            return IntegrationReport(
                project=project,
                stage=IntegrationStage.UNBOUND,
                next_action="Bu makinede kaynak yolu yok; `zekam project rebind` calistirin",
                is_stale=True,
                binding=binding,
                current_revision=recorded,
                observed_revision=None,
                profile_digest=detail.get("profile_digest"),
                blockers=tuple(blockers),
            )

        current_discovery = discover(local_path)
        observation = source_reader.observe(local_path)
        observed_revision_text = (
            observation.commit if observation is not None else current_discovery.tree_digest
        )
        is_stale = (
            recorded is None
            or current_discovery.truncated
            or recorded.tree_digest != current_discovery.tree_digest
            or (
                observation is not None
                and (
                    recorded.revision != observation.commit
                    or observation.is_dirty != recorded.is_dirty
                )
            )
        )

        if locator_digest_for(local_path) != binding.locator_digest:
            blockers.append("source-moved")
            is_stale = True

        if is_stale and stage is IntegrationStage.CURRENT:
            stage = IntegrationStage.STALE

        knowledge_index = detail.get("knowledge_index")
        next_action = _next_action(stage, is_stale=is_stale, blockers=tuple(blockers))
        if (
            stage is IntegrationStage.CURRENT
            and not is_stale
            and isinstance(knowledge_index, dict)
            and knowledge_index.get("state") != "ready"
        ):
            next_action = "Kaynak indeksini `zekam project index` ile uretin"
        observed = (
            None
            if recorded is None
            else SourceRevision(
                id=recorded.id,
                realm_id=recorded.realm_id,
                binding_id=recorded.binding_id,
                kind=recorded.kind,
                revision=observed_revision_text,
                tree_digest=current_discovery.tree_digest,
                branch=observation.branch if observation is not None else None,
                is_dirty=observation.is_dirty if observation is not None else False,
                file_count=current_discovery.file_count,
                observed_at=recorded.observed_at,
            )
        )
        return IntegrationReport(
            project=project,
            stage=stage,
            next_action=next_action,
            is_stale=is_stale,
            binding=binding,
            current_revision=recorded,
            observed_revision=observed,
            profile_digest=detail.get("profile_digest"),
            blockers=tuple(blockers),
            knowledge_index=(knowledge_index if isinstance(knowledge_index, dict) else None),
        )

    def resolve_source_root(self, project_id: UUID) -> Path:
        """Bu makinedeki kaynak kokunu dondurur."""
        _, root = self._require_bound_source(project_id)
        return root

    def _require_bound_source(self, project_id: UUID) -> tuple[SourceBinding, Path]:
        bindings = self.bindings.for_project(project_id)
        if not bindings:
            raise NotFound("Projenin kaynak baglantisi yok")
        binding = bindings[0]
        if not binding.is_usable:
            raise PolicyViolation("Kaynak baglantisi kullanilabilir durumda degil")
        root = self.bindings.local_path(binding.id)
        if root is None or not root.is_dir():
            raise NotFound("Bu makinede kaynak yolu bulunamadi")
        return binding, root


def _next_action(stage: IntegrationStage, *, is_stale: bool, blockers: tuple[str, ...]) -> str:
    if "source-moved" in blockers:
        return "Kaynak tasinmis; `zekam project rebind` ile yeni konumu baglayin"
    if stage is IntegrationStage.UNBOUND:
        return "Bu makinede kaynak yolu yok; `zekam project rebind` calistirin"
    if stage is IntegrationStage.REGISTERED:
        return "Kaynak agacini baglayin"
    if stage is IntegrationStage.BOUND:
        return "`zekam project scan` ile ilk taramayi calistirin"
    if stage is IntegrationStage.DISCOVERED:
        return "Capability profilini uretin"
    if stage is IntegrationStage.PROFILED:
        return "Entegrasyonu dogrulayip `current` yapin"
    if stage is IntegrationStage.STALE or is_stale:
        return "Kaynak degismis; `zekam project scan` ile yeniden tarayin"
    return "Entegrasyon guncel; is secimine gecebilirsiniz"
