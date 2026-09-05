"""PostgreSQL Knowledge Plane repository."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.errors import ValidationFailed
from zekam.domain.identifiers import new_uuid7
from zekam.domain.knowledge import (
    Artifact,
    IngestionJob,
    NormalizedDocument,
    SourceFormat,
    SourceVersion,
)


@dataclass(frozen=True, slots=True)
class KnowledgeRepository:
    """Realm kapsamli artifact, ingestion ve normalize icerik kayitlari."""

    connection: Any
    realm_id: UUID
    project_id: UUID | None = None

    # -- artifact ve kaynak ---------------------------------------------------

    def store_artifact(self, artifact: Artifact) -> UUID:
        record_id = new_uuid7(now=artifact.stored_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into knowledge.artifact"
                " (id, realm_id, project_id, content_digest, artifact_digest, byte_size,"
                "  media_type, original_name, stored_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, artifact_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    self.project_id,
                    artifact.content_digest,
                    artifact.artifact_digest,
                    artifact.byte_size,
                    artifact.media_type,
                    artifact.original_name,
                    artifact.stored_at,
                ),
            )
            return self._resolve(
                cursor, "knowledge.artifact", "artifact_digest", artifact.artifact_digest
            )

    def register_source(self, slug: str, source_format: SourceFormat, *, now: dt.datetime) -> UUID:
        record_id = new_uuid7(now=now)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into knowledge.source"
                " (id, realm_id, project_id, slug, source_format, created_at)"
                " values (%s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, slug) do nothing returning id",
                (record_id, self.realm_id, self.project_id, slug, str(source_format), now),
            )
            source_id = self._resolve(cursor, "knowledge.source", "slug", slug)
            cursor.execute(
                "select source_format from knowledge.source where realm_id = %s and id = %s",
                (self.realm_id, source_id),
            )
            stored_format = str(cursor.fetchone()[0])
            if stored_format != str(source_format):
                raise ValidationFailed("ayni source slug farkli formatla kullanilamaz")
            return source_id

    def next_revision(self, source_id: UUID) -> int:
        """Kaynak icin transaction-kapsamli kilitle bir sonraki revision'i hesaplar.

        ``knowledge.source`` append-only oldugu icin uygulama rolune UPDATE verilmez.
        Bu nedenle ``SELECT .. FOR UPDATE`` yerine realm/source kimligine bagli
        transaction advisory lock kullanilir.
        """

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"knowledge-revision:{self.realm_id}:{source_id}",),
            )
            cursor.execute(
                "select id from knowledge.source where realm_id = %s and id = %s",
                (self.realm_id, source_id),
            )
            if cursor.fetchone() is None:
                raise ValidationFailed("knowledge source bulunamadi")
            cursor.execute(
                "select coalesce(max(revision), 0) + 1 from knowledge.source_version"
                " where realm_id = %s and source_id = %s",
                (self.realm_id, source_id),
            )
            return int(cursor.fetchone()[0])

    # -- ingestion ------------------------------------------------------------

    def start_job(
        self, job: IngestionJob, *, source_id: UUID, artifact_id: UUID, now: dt.datetime
    ) -> UUID:
        """Idempotent baslangic: ayni anahtar ikinci kez is yaratmaz."""

        record_id = new_uuid7(now=now)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into knowledge.ingestion_job"
                " (id, realm_id, source_id, artifact_id, idempotency_key, completed_stages,"
                "  updated_at)"
                " values (%s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, source_id, idempotency_key) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    source_id,
                    artifact_id,
                    job.idempotency_key,
                    [str(item) for item in job.completed_stages],
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0]))
            cursor.execute(
                "select id from knowledge.ingestion_job"
                " where realm_id = %s and source_id = %s and idempotency_key = %s",
                (self.realm_id, source_id, job.idempotency_key),
            )
            return UUID(str(cursor.fetchone()[0]))

    def save_progress(self, job_id: UUID, job: IngestionJob, *, now: dt.datetime) -> None:
        """Asama ilerlemesini kalicilastirir; crash sonrasi buradan devam edilir."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "update knowledge.ingestion_job"
                " set completed_stages = %s, failure = %s, updated_at = %s"
                " where realm_id = %s and id = %s",
                (
                    [str(item) for item in job.completed_stages],
                    job.failure,
                    now,
                    self.realm_id,
                    job_id,
                ),
            )

    def load_progress(
        self, idempotency_key: str, *, source_id: UUID | None = None
    ) -> tuple[UUID, tuple[str, ...], str | None] | None:
        with self.connection.cursor() as cursor:
            if source_id is None:
                cursor.execute(
                    "select id, completed_stages, failure from knowledge.ingestion_job"
                    " where realm_id = %s and idempotency_key = %s order by id limit 1",
                    (self.realm_id, idempotency_key),
                )
            else:
                cursor.execute(
                    "select id, completed_stages, failure from knowledge.ingestion_job"
                    " where realm_id = %s and source_id = %s and idempotency_key = %s",
                    (self.realm_id, source_id, idempotency_key),
                )
            row = cursor.fetchone()
        if row is None:
            return None
        return UUID(str(row[0])), tuple(row[1] or ()), row[2]

    # -- surum ve icerik ------------------------------------------------------

    def store_version(self, version: SourceVersion, *, source_id: UUID, artifact_id: UUID) -> UUID:
        record_id = new_uuid7(now=version.created_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into knowledge.source_version"
                " (id, realm_id, source_id, revision, artifact_id, artifact_digest,"
                "  content_digest, state, superseded_by, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, source_id, revision) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    source_id,
                    version.revision,
                    artifact_id,
                    version.artifact_digest,
                    version.content_digest,
                    str(version.state),
                    version.superseded_by,
                    version.created_at,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0]))
            cursor.execute(
                "select id from knowledge.source_version"
                " where realm_id = %s and source_id = %s and revision = %s",
                (self.realm_id, source_id, version.revision),
            )
            return UUID(str(cursor.fetchone()[0]))

    def activate_version(self, version_id: UUID) -> None:
        """Atomik aktivasyon; trigger tamamlanmamis ingestion'i reddeder."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "update knowledge.source_version set state = 'active'"
                " where realm_id = %s and id = %s and state = 'pending'",
                (self.realm_id, version_id),
            )

    def supersede_version(self, version_id: UUID, successor: UUID) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update knowledge.source_version"
                " set state = 'superseded', superseded_by = %s"
                " where realm_id = %s and id = %s and state = 'active'",
                (successor, self.realm_id, version_id),
            )

    def active_version(self, source_id: UUID) -> UUID | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id from knowledge.source_version"
                " where realm_id = %s and source_id = %s and state = 'active'",
                (self.realm_id, source_id),
            )
            row = cursor.fetchone()
        return None if row is None else UUID(str(row[0]))

    def equivalent_version(
        self, source_id: UUID, *, artifact_digest: str, content_digest: str
    ) -> tuple[UUID, int, str] | None:
        """Ayni kaynak/artifact/normalize icerik surumunu bulur."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, revision, state from knowledge.source_version"
                " where realm_id = %s and source_id = %s and artifact_digest = %s"
                " and content_digest = %s order by revision desc limit 1",
                (self.realm_id, source_id, artifact_digest, content_digest),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return UUID(str(row[0])), int(row[1]), str(row[2])

    def store_document(
        self, document: NormalizedDocument, *, version_id: UUID, now: dt.datetime
    ) -> UUID:
        record_id = new_uuid7(now=now)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into knowledge.normalized_document"
                " (id, realm_id, version_id, source_format, parser_ref, parser_version,"
                "  parser_profile, unit_count, content_digest, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)"
                " on conflict (realm_id, content_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    version_id,
                    str(document.source_format),
                    document.parser_ref,
                    document.parser_version,
                    canonical_json(document.parser_profile),
                    document.unit_count,
                    document.content_digest,
                    now,
                ),
            )
            document_id = self._resolve(
                cursor, "knowledge.normalized_document", "content_digest", document.content_digest
            )
            for unit in document.units:
                cursor.execute(
                    "insert into knowledge.content_unit"
                    " (id, realm_id, document_id, unit_ref, kind, unit_order, body, locator,"
                    "  confidence, unit_digest)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)"
                    " on conflict (realm_id, document_id, unit_order) do nothing",
                    (
                        new_uuid7(now=now),
                        self.realm_id,
                        document_id,
                        unit.unit_id,
                        str(unit.kind),
                        unit.order,
                        unit.text,
                        canonical_json(unit.locator.as_dict()),
                        unit.confidence,
                        unit.unit_digest,
                    ),
                )
        return document_id

    def unit_count(self, document_id: UUID) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from knowledge.content_unit"
                " where realm_id = %s and document_id = %s",
                (self.realm_id, document_id),
            )
            return int(cursor.fetchone()[0])

    @staticmethod
    def _resolve(cursor: Any, table: str, column: str, value: str) -> UUID:
        row = cursor.fetchone()
        if row is not None:
            return UUID(str(row[0]))
        cursor.execute(f"select id from {table} where {column} = %s", (value,))
        return UUID(str(cursor.fetchone()[0]))
