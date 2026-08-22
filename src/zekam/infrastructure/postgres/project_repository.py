"""Proje kayit defteri icin PostgreSQL adapterleri.

Butun sorgular realm kapsamlidir. Yerel (makineye ozel) yol yalnizca
`projects.source_binding_local` uzerinden okunur ve portable kayitlara sizmaz.
"""

from __future__ import annotations

import datetime as dt
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from zekam.application.capability_profile import PROFILER_VERSION, CapabilityProfile
from zekam.domain.canonical import canonical_json
from zekam.domain.errors import NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.project import (
    BindingStatus,
    IntegrationStage,
    Project,
    ProjectAlias,
    ProjectCandidate,
    ProjectResolution,
    ResolutionKind,
    SourceBinding,
    SourceBindingKind,
    SourceRevision,
    SourceRevisionKind,
)
from zekam.domain.realm import LifecycleStatus

#: Bulanik eslesmenin kabul edilmesi icin gereken en dusuk benzerlik.
FUZZY_ACCEPT_THRESHOLD = 0.62

#: Aday listesine alinmasi icin gereken en dusuk benzerlik.
FUZZY_CANDIDATE_THRESHOLD = 0.28

#: Tek adayin kesin sayilmasi icin ikinci adaydan bu kadar yuksek olmasi gerekir.
FUZZY_MARGIN = 0.15


def machine_label() -> str:
    """Makineye ozel kayitlari isaretleyen etiket."""
    return socket.gethostname().lower()


@dataclass(frozen=True, slots=True)
class ProjectRepository:
    """Proje ve alias kayitlari."""

    connection: Any
    realm_id: UUID

    # -- proje ------------------------------------------------------------------

    def add(self, project: Project) -> Project:
        if project.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm proje ekleme reddedildi")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into projects.project"
                " (id, realm_id, slug, display_name, status, revision, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                (
                    project.id,
                    project.realm_id,
                    project.slug,
                    project.display_name,
                    project.status.value,
                    project.revision,
                    project.created_at,
                ),
            )
            cursor.execute(
                "insert into projects.integration_state (project_id, realm_id, stage)"
                " values (%s, %s, %s)",
                (project.id, project.realm_id, IntegrationStage.REGISTERED.value),
            )
        return project

    def get(self, project_id: UUID) -> Project:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_PROJECT_COLUMNS} from projects.project where id = %s", (project_id,)
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Proje bulunamadi")
        return _project_from_row(row)

    def find_by_slug(self, slug: str) -> Project | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_PROJECT_COLUMNS} from projects.project where slug = %s", (slug,)
            )
            row = cursor.fetchone()
        return None if row is None else _project_from_row(row)

    def list_all(self, *, include_archived: bool = False) -> tuple[Project, ...]:
        query = f"select {_PROJECT_COLUMNS} from projects.project"
        if not include_archived:
            query += " where status <> 'archived'"
        query += " order by slug"
        with self.connection.cursor() as cursor:
            cursor.execute(query)
            rows = cursor.fetchall()
        return tuple(_project_from_row(row) for row in rows)

    def set_status(self, project_id: UUID, status: LifecycleStatus) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update projects.project set status = %s, revision = revision + 1 where id = %s",
                (status.value, project_id),
            )

    # -- alias ------------------------------------------------------------------

    def add_alias(self, alias: ProjectAlias) -> ProjectAlias:
        if alias.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm alias ekleme reddedildi")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into projects.project_alias"
                " (id, realm_id, project_id, alias, normalized, is_primary, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                (
                    alias.id,
                    alias.realm_id,
                    alias.project_id,
                    alias.alias,
                    alias.normalized,
                    alias.is_primary,
                    alias.created_at,
                ),
            )
        return alias

    def aliases_of(self, project_id: UUID) -> tuple[ProjectAlias, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, project_id, alias, normalized, is_primary, created_at"
                " from projects.project_alias where project_id = %s order by normalized",
                (project_id,),
            )
            rows = cursor.fetchall()
        return tuple(
            ProjectAlias(
                id=row[0],
                realm_id=row[1],
                project_id=row[2],
                alias=row[3],
                normalized=row[4],
                is_primary=row[5],
                created_at=row[6],
            )
            for row in rows
        )


_PROJECT_COLUMNS = "id, realm_id, slug, display_name, status, revision, created_at"


def _project_from_row(row: tuple[Any, ...]) -> Project:
    return Project(
        id=row[0],
        realm_id=row[1],
        slug=row[2],
        display_name=row[3],
        status=LifecycleStatus(row[4]),
        revision=row[5],
        created_at=row[6],
    )


@dataclass(frozen=True, slots=True)
class SourceBindingRepository:
    """Source binding, yerel yol ve source revision kayitlari."""

    connection: Any
    realm_id: UUID

    def bind(self, binding: SourceBinding, *, absolute_path: Path) -> SourceBinding:
        """Baglantiyi ve makineye ozel yolu kaydeder."""
        if binding.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm binding reddedildi")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into projects.source_binding"
                " (id, realm_id, project_id, kind, root_label, locator_digest, status,"
                "  access_mode, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    binding.id,
                    binding.realm_id,
                    binding.project_id,
                    binding.kind.value,
                    binding.root_label,
                    binding.locator_digest,
                    binding.status.value,
                    binding.access_mode,
                    binding.created_at,
                ),
            )
            cursor.execute(
                "insert into projects.source_binding_local"
                " (binding_id, realm_id, machine_label, absolute_path) values (%s, %s, %s, %s)",
                (binding.id, binding.realm_id, machine_label(), str(absolute_path)),
            )
        return binding

    def rebind(self, binding_id: UUID, *, absolute_path: Path, locator_digest: str) -> None:
        """Kaynak baska bir konuma tasindiginda yerel yolu tazeler."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update projects.source_binding set locator_digest = %s, status = %s where id = %s",
                (locator_digest, BindingStatus.BOUND.value, binding_id),
            )
            if cursor.rowcount == 0:
                raise NotFound("Baglanti bulunamadi")
            cursor.execute(
                "insert into projects.source_binding_local"
                " (binding_id, realm_id, machine_label, absolute_path) values (%s, %s, %s, %s)"
                " on conflict (binding_id) do update"
                " set absolute_path = excluded.absolute_path,"
                "     machine_label = excluded.machine_label,"
                "     updated_at = now()",
                (binding_id, self.realm_id, machine_label(), str(absolute_path)),
            )

    def mark_unbound(self, binding_id: UUID) -> None:
        """Yerel yolu kaldirir ve baglantiyi `unbound` yapar."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update projects.source_binding set status = %s where id = %s",
                (BindingStatus.UNBOUND.value, binding_id),
            )
            cursor.execute(
                "delete from projects.source_binding_local where binding_id = %s", (binding_id,)
            )

    def get(self, binding_id: UUID) -> SourceBinding:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_BINDING_COLUMNS} from projects.source_binding where id = %s",
                (binding_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Baglanti bulunamadi")
        return _binding_from_row(row)

    def for_project(self, project_id: UUID) -> tuple[SourceBinding, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_BINDING_COLUMNS} from projects.source_binding"
                " where project_id = %s order by created_at",
                (project_id,),
            )
            rows = cursor.fetchall()
        return tuple(_binding_from_row(row) for row in rows)

    def local_path(self, binding_id: UUID) -> Path | None:
        """Bu makinedeki fiziksel yolu dondurur. Portable kayitlara yazilmaz."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select absolute_path from projects.source_binding_local"
                " where binding_id = %s and machine_label = %s",
                (binding_id, machine_label()),
            )
            row = cursor.fetchone()
        return None if row is None else Path(row[0])

    # -- source revision ---------------------------------------------------------

    def record_revision(
        self,
        *,
        binding_id: UUID,
        kind: SourceRevisionKind,
        revision: str,
        tree_digest: str,
        branch: str | None = None,
        is_dirty: bool = False,
        file_count: int = 0,
        now: dt.datetime | None = None,
    ) -> SourceRevision:
        """Yeni bir kaynak surumu gozlemi ekler (append-only)."""
        moment = now or dt.datetime.now(dt.UTC)
        record = SourceRevision(
            id=new_uuid7(now=moment),
            realm_id=self.realm_id,
            binding_id=binding_id,
            kind=kind,
            revision=revision,
            tree_digest=tree_digest,
            branch=branch,
            is_dirty=is_dirty,
            file_count=file_count,
            observed_at=moment,
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into projects.source_revision"
                " (id, realm_id, binding_id, revision_kind, revision, tree_digest, branch,"
                "  is_dirty, file_count, observed_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.id,
                    record.realm_id,
                    record.binding_id,
                    record.kind.value,
                    record.revision,
                    record.tree_digest,
                    record.branch,
                    record.is_dirty,
                    record.file_count,
                    record.observed_at,
                ),
            )
        return record

    def latest_revision(self, binding_id: UUID) -> SourceRevision | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_REVISION_COLUMNS} from projects.source_revision"
                " where binding_id = %s order by observed_at desc, id desc limit 1",
                (binding_id,),
            )
            row = cursor.fetchone()
        return None if row is None else _revision_from_row(row)

    def revision_history(self, binding_id: UUID, *, limit: int = 20) -> tuple[SourceRevision, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_REVISION_COLUMNS} from projects.source_revision"
                " where binding_id = %s order by observed_at desc, id desc limit %s",
                (binding_id, limit),
            )
            rows = cursor.fetchall()
        return tuple(_revision_from_row(row) for row in rows)


_BINDING_COLUMNS = (
    "id, realm_id, project_id, kind, root_label, locator_digest, status, access_mode, created_at"
)

_REVISION_COLUMNS = (
    "id, realm_id, binding_id, revision_kind, revision, tree_digest, branch,"
    " is_dirty, file_count, observed_at"
)


def _binding_from_row(row: tuple[Any, ...]) -> SourceBinding:
    return SourceBinding(
        id=row[0],
        realm_id=row[1],
        project_id=row[2],
        kind=SourceBindingKind(row[3]),
        root_label=row[4],
        locator_digest=row[5],
        status=BindingStatus(row[6]),
        access_mode=row[7],
        created_at=row[8],
    )


def _revision_from_row(row: tuple[Any, ...]) -> SourceRevision:
    return SourceRevision(
        id=row[0],
        realm_id=row[1],
        binding_id=row[2],
        kind=SourceRevisionKind(row[3]),
        revision=row[4],
        tree_digest=row[5],
        branch=row[6],
        is_dirty=row[7],
        file_count=row[8],
        observed_at=row[9],
    )


@dataclass(frozen=True, slots=True)
class CapabilityProfileRepository:
    """Capability profillerini saklar."""

    connection: Any
    realm_id: UUID

    def store(
        self,
        *,
        project_id: UUID,
        source_revision_id: UUID,
        profile: CapabilityProfile,
        now: dt.datetime | None = None,
    ) -> UUID:
        """Profili kaydeder; ayni surum icin idempotenttir."""
        moment = now or dt.datetime.now(dt.UTC)
        record_id = new_uuid7(now=moment)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into projects.capability_profile"
                " (id, realm_id, project_id, source_revision_id, profile, profile_digest,"
                "  generator_version, generated_at)"
                " values (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)"
                " on conflict (source_revision_id, generator_version) do nothing"
                " returning id",
                (
                    record_id,
                    self.realm_id,
                    project_id,
                    source_revision_id,
                    canonical_json(profile.body()),
                    profile.digest,
                    profile.generator_version,
                    moment,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0]))
            cursor.execute(
                "select id from projects.capability_profile"
                " where source_revision_id = %s and generator_version = %s",
                (source_revision_id, profile.generator_version),
            )
            existing = cursor.fetchone()
        if existing is None:  # pragma: no cover - conflict sonrasi kayit her zaman vardir
            raise NotFound("Profil kaydedilemedi")
        return UUID(str(existing[0]))

    def latest_for_project(self, project_id: UUID) -> tuple[str, dict[str, Any]] | None:
        """En son profili (digest, govde) olarak dondurur."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select profile_digest, profile from projects.capability_profile"
                " where project_id = %s and generator_version = %s"
                " order by generated_at desc limit 1",
                (project_id, PROFILER_VERSION),
            )
            row = cursor.fetchone()
        return None if row is None else (row[0], row[1])

    def for_revision(self, source_revision_id: UUID) -> tuple[str, dict[str, Any]] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select profile_digest, profile from projects.capability_profile"
                " where source_revision_id = %s and generator_version = %s",
                (source_revision_id, PROFILER_VERSION),
            )
            row = cursor.fetchone()
        return None if row is None else (row[0], row[1])


@dataclass(frozen=True, slots=True)
class IntegrationStateRepository:
    """Entegrasyon asamasini okur ve gunceller."""

    connection: Any
    realm_id: UUID

    def get(self, project_id: UUID) -> tuple[IntegrationStage, UUID | None, dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select stage, observed_revision_id, detail from projects.integration_state"
                " where project_id = %s",
                (project_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Entegrasyon durumu bulunamadi")
        return IntegrationStage(row[0]), row[1], dict(row[2] or {})

    def set(
        self,
        project_id: UUID,
        *,
        stage: IntegrationStage,
        observed_revision_id: UUID | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update projects.integration_state"
                " set stage = %s, observed_revision_id = %s, detail = %s::jsonb"
                " where project_id = %s",
                (
                    stage.value,
                    observed_revision_id,
                    canonical_json(detail or {}),
                    project_id,
                ),
            )
            if cursor.rowcount == 0:
                raise NotFound("Entegrasyon durumu bulunamadi")


@dataclass(frozen=True, slots=True)
class ProjectResolver:
    """Dogal dil proje cozumleyicisi.

    Sira: exact kimlik -> exact slug -> exact alias -> normalize edilmis alias ->
    trigram benzerligi. Belirsizlikte mutation yapilmaz; kullanici secim yapar.
    """

    connection: Any
    realm_id: UUID

    def resolve(self, query: str) -> ProjectResolution:
        text = query.strip()
        if not text:
            return ProjectResolution(query=query, kind=ResolutionKind.NOT_FOUND)

        exact = self._by_identifier(text)
        if exact is not None:
            return ProjectResolution(query=query, kind=ResolutionKind.EXACT_ID, resolved=exact)

        slug_match = self._by_slug(text.lower())
        if slug_match is not None:
            return ProjectResolution(
                query=query, kind=ResolutionKind.EXACT_SLUG, resolved=slug_match
            )

        alias_match = self._by_alias(text)
        if alias_match is not None:
            return ProjectResolution(
                query=query, kind=ResolutionKind.EXACT_ALIAS, resolved=alias_match
            )

        candidates = self._fuzzy(text)
        if not candidates:
            return ProjectResolution(query=query, kind=ResolutionKind.NOT_FOUND)

        best = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None
        clear_winner = runner_up is None or (best.score - runner_up.score) >= FUZZY_MARGIN
        if best.score >= FUZZY_ACCEPT_THRESHOLD and clear_winner:
            return ProjectResolution(
                query=query,
                kind=ResolutionKind.FUZZY,
                resolved=best,
                candidates=candidates,
            )
        return ProjectResolution(query=query, kind=ResolutionKind.AMBIGUOUS, candidates=candidates)

    def _by_identifier(self, text: str) -> ProjectCandidate | None:
        try:
            identifier = UUID(text)
        except ValueError:
            return None
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, slug, display_name from projects.project where id = %s", (identifier,)
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return ProjectCandidate(
            project_id=row[0], slug=row[1], display_name=row[2], matched_on="id", score=1.0
        )

    def _by_slug(self, text: str) -> ProjectCandidate | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, slug, display_name from projects.project where slug = %s", (text,)
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return ProjectCandidate(
            project_id=row[0], slug=row[1], display_name=row[2], matched_on="slug", score=1.0
        )

    def _by_alias(self, text: str) -> ProjectCandidate | None:
        from zekam.domain.identifiers import normalize_slug

        try:
            normalized = normalize_slug(text)
        except Exception:
            return None
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select p.id, p.slug, p.display_name, a.alias"
                " from projects.project_alias a"
                " join projects.project p on p.id = a.project_id"
                " where a.normalized = %s",
                (normalized,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return ProjectCandidate(
            project_id=row[0],
            slug=row[1],
            display_name=row[2],
            matched_on=f"alias:{row[3]}",
            score=1.0,
        )

    def _fuzzy(self, text: str) -> tuple[ProjectCandidate, ...]:
        from zekam.domain.identifiers import normalize_slug

        try:
            normalized = normalize_slug(text)
        except Exception:
            return ()
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                select id, slug, display_name, matched_on, score
                from (
                    select p.id,
                           p.slug,
                           p.display_name,
                           'slug' as matched_on,
                           similarity(p.slug, %(q)s) as score
                    from projects.project p
                    union all
                    select p.id,
                           p.slug,
                           p.display_name,
                           'alias:' || a.alias as matched_on,
                           similarity(a.normalized, %(q)s) as score
                    from projects.project_alias a
                    join projects.project p on p.id = a.project_id
                ) ranked
                where score >= %(threshold)s
                order by score desc, slug
                """,
                {"q": normalized, "threshold": FUZZY_CANDIDATE_THRESHOLD},
            )
            rows = cursor.fetchall()

        best_per_project: dict[UUID, ProjectCandidate] = {}
        for row in rows:
            candidate = ProjectCandidate(
                project_id=row[0],
                slug=row[1],
                display_name=row[2],
                matched_on=row[3],
                score=float(row[4]),
            )
            current = best_per_project.get(candidate.project_id)
            if current is None or candidate.score > current.score:
                best_per_project[candidate.project_id] = candidate
        return tuple(sorted(best_per_project.values(), key=lambda item: (-item.score, item.slug)))
