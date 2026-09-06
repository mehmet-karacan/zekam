"""Oracle schema metadata extraction and local-only knowledge indexing.

The adapter reads only data-dictionary metadata and ``DBMS_METADATA.GET_DDL``.
Application table rows are never queried. Credentials remain in process memory;
plans, receipts, logs and persisted manifests contain only safe identity digests.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import yaml

from zekam.application.embedding_provider import EmbeddingPolicy, EmbeddingProvider
from zekam.application.knowledge_ingestion import IngestionService, pending_version
from zekam.application.knowledge_parsers import default_router
from zekam.application.project_knowledge_index import (
    REAL_EMBEDDING_DIMENSION,
    REAL_EMBEDDING_MODEL_REF,
)
from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.knowledge import (
    ContentUnit,
    IngestionStage,
    Locator,
    NormalizedDocument,
    SourceFormat,
    UnitKind,
    assert_safe_relative,
)
from zekam.domain.retrieval import Chunk, ChunkProfile, EmbeddingProfile, estimate_tokens
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

MAX_CONFIG_BYTES = 1024 * 1024
MAX_OBJECTS = 20_000
MAX_TOTAL_DDL_BYTES = 256 * 1024 * 1024
MAX_DDL_CHUNK_CHARACTERS = 6000
MAX_DDL_CHUNK_TOKENS = 384
ORACLE_INDEXER_VERSION = "zekam-oracle-metadata-indexer/v1"

_ORACLE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
_JDBC_PREFIX = "jdbc:oracle:thin:@"

# DBMS_METADATA names differ from ALL_OBJECTS labels for body objects.
SUPPORTED_OBJECT_TYPES: tuple[tuple[str, str], ...] = (
    ("TABLE", "TABLE"),
    ("INDEX", "INDEX"),
    ("VIEW", "VIEW"),
    ("MATERIALIZED VIEW", "MATERIALIZED_VIEW"),
    ("SEQUENCE", "SEQUENCE"),
    ("SYNONYM", "SYNONYM"),
    ("TYPE", "TYPE"),
    ("TYPE BODY", "TYPE_BODY"),
    ("PACKAGE", "PACKAGE"),
    ("PACKAGE BODY", "PACKAGE_BODY"),
    ("PROCEDURE", "PROCEDURE"),
    ("FUNCTION", "FUNCTION"),
    ("TRIGGER", "TRIGGER"),
)
_METADATA_TYPE = dict(SUPPORTED_OBJECT_TYPES)


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & 0x400)


class _ExactYamlLoader(yaml.SafeLoader):
    pass


def _exact_mapping(loader: yaml.SafeLoader, node: yaml.Node) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if not isinstance(key, str) or key in result:
            raise ConfigurationError("Oracle datasource YAML duplicate/gecersiz key tasiyor")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_ExactYamlLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _exact_mapping)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"Oracle datasource {label} mapping olmali")
    return value


def _required_text(document: dict[str, Any], key: str, label: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ConfigurationError(f"Oracle datasource {label} metni gecersiz")
    return value


def _secure_config_path(root: Path, relative_path: str) -> Path:
    assert_safe_relative(relative_path, "Oracle datasource config yolu")
    resolved_root = root.resolve(strict=True)
    candidate = resolved_root.joinpath(*PurePosixPath(relative_path).parts)
    current = resolved_root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise ConfigurationError("Oracle datasource config link/reparse olamaz")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        raise ConfigurationError("Oracle datasource config proje koku disina cikamaz") from None
    if not resolved.is_file() or resolved.stat().st_size > MAX_CONFIG_BYTES:
        raise ConfigurationError("Oracle datasource config dosyasi gecersiz")
    return resolved


def _thin_dsn(jdbc_url: str) -> str:
    if not jdbc_url.startswith(_JDBC_PREFIX):
        raise ConfigurationError("Yalniz Oracle thin JDBC URL desteklenir")
    dsn = jdbc_url.removeprefix(_JDBC_PREFIX)
    if dsn.startswith("//"):
        dsn = dsn[2:]
    if not dsn or any(character in dsn for character in ("\r", "\n", "\x00")):
        raise ConfigurationError("Oracle JDBC DSN gecersiz")
    # Credentials URL icinde kabul edilmez. Oracle descriptor icindeki '@' de
    # gereksizdir; username/password ayri alanlardan gelir.
    if "@" in dsn or "://" in dsn:
        raise ConfigurationError("Oracle JDBC URL credential veya nested scheme tasiyamaz")
    return dsn


@dataclass(frozen=True, slots=True)
class OracleDatasource:
    """Process-memory-only Oracle connection material and safe identity."""

    schema_name: str
    connection_identity_digest: str
    config_relative_path: str
    _dsn: str = field(repr=False)
    _username: str = field(repr=False)
    _password: str = field(repr=False)

    @property
    def dsn(self) -> str:
        return self._dsn

    @property
    def username(self) -> str:
        return self._username

    @property
    def password(self) -> str:
        return self._password

    def sanitized(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "connection_identity_digest": self.connection_identity_digest,
            "config_relative_path": self.config_relative_path,
            "credential_source": "project-local-legacy-config/process-memory-only",
        }


def load_project_oracle_datasource(root: Path, relative_path: str) -> OracleDatasource:
    """Load one explicit Spring profile without exposing credential values."""

    path = _secure_config_path(root, relative_path)
    raw = path.read_bytes()
    try:
        document = yaml.load(raw.decode("utf-8"), Loader=_ExactYamlLoader) or {}
    except (UnicodeDecodeError, yaml.YAMLError):
        raise ConfigurationError("Oracle datasource config UTF-8 YAML degil") from None
    root_document = _mapping(document, "root")
    spring = _mapping(root_document.get("spring"), "spring")
    datasource = _mapping(spring.get("datasource"), "spring.datasource")
    app = _mapping(root_document.get("app"), "app")
    schema = _mapping(app.get("schema"), "app.schema")
    jdbc_url = _required_text(datasource, "url", "url")
    username = _required_text(datasource, "username", "username")
    password = _required_text(datasource, "password", "password")
    schema_name = _required_text(schema, "name", "schema name")
    if not _ORACLE_IDENTIFIER.fullmatch(schema_name):
        raise ConfigurationError("Oracle schema identifier gecersiz")
    dsn = _thin_dsn(jdbc_url)
    return OracleDatasource(
        schema_name=schema_name,
        connection_identity_digest=digest(
            {
                "driver": "python-oracledb-thin",
                "dsn": dsn,
                "schema": schema_name,
                "config_relative_path": relative_path,
            }
        ),
        config_relative_path=relative_path,
        _dsn=dsn,
        _username=username,
        _password=password,
    )


@dataclass(frozen=True, slots=True)
class OracleDdlObject:
    owner: str
    object_name: str
    object_type: str
    status: str
    last_ddl_at: str
    ddl_digest: str
    _ddl: str = field(repr=False)

    @property
    def ddl(self) -> str:
        return self._ddl

    def manifest_entry(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "object_name": self.object_name,
            "object_type": self.object_type,
            "status": self.status,
            "last_ddl_at": self.last_ddl_at,
            "ddl_digest": self.ddl_digest,
        }


@dataclass(frozen=True, slots=True)
class OracleMetadataSnapshot:
    schema_name: str
    connection_identity_digest: str
    database_identity_digest: str
    objects: tuple[OracleDdlObject, ...]
    excluded_secret_objects: int

    @property
    def revision_digest(self) -> str:
        return digest(
            {
                "schema_name": self.schema_name,
                "connection_identity_digest": self.connection_identity_digest,
                "database_identity_digest": self.database_identity_digest,
                "objects": [item.manifest_entry() for item in self.objects],
                "excluded_secret_objects": self.excluded_secret_objects,
            }
        )

    def sanitized(self) -> dict[str, object]:
        counts: dict[str, int] = {}
        invalid = 0
        for item in self.objects:
            counts[item.object_type] = counts.get(item.object_type, 0) + 1
            invalid += int(item.status != "VALID")
        return {
            "schema": "zekam-oracle-metadata-snapshot/v1",
            "schema_name": self.schema_name,
            "connection_identity_digest": self.connection_identity_digest,
            "database_identity_digest": self.database_identity_digest,
            "revision_digest": self.revision_digest,
            "object_count": len(self.objects),
            "object_type_counts": dict(sorted(counts.items())),
            "invalid_object_count": invalid,
            "excluded_secret_object_count": self.excluded_secret_objects,
            "row_data_included": False,
        }


@dataclass(slots=True)
class OracleMetadataClient:
    """Bounded python-oracledb Thin adapter for metadata-only reads."""

    connect_timeout_seconds: int = 15
    call_timeout_milliseconds: int = 120_000

    def collect(self, datasource: OracleDatasource) -> OracleMetadataSnapshot:
        phase = "connect"
        try:
            import oracledb
        except ImportError:
            raise ConfigurationError("Oracle metadata icin Zekam oracle extra kurulmali") from None
        try:
            connection = oracledb.connect(
                user=datasource.username,
                password=datasource.password,
                dsn=datasource.dsn,
                tcp_connect_timeout=self.connect_timeout_seconds,
            )
        except Exception as exc:
            raise ConfigurationError(
                f"Oracle metadata baglantisi kurulamadi: {type(exc).__name__}"
            ) from None
        try:
            connection.call_timeout = self.call_timeout_milliseconds
            with connection.cursor() as cursor:
                phase = "session-identity"
                cursor.execute(
                    "select sys_context('USERENV','DB_NAME'),"
                    " sys_context('USERENV','CON_NAME'), sys_context('USERENV','SESSION_USER')"
                    " from dual"
                )
                identity_row = cursor.fetchone()
                if identity_row is None:
                    raise ConfigurationError("Oracle session identity okunamadi")
                database_identity_digest = digest(
                    {
                        "db": identity_row[0],
                        "container": identity_row[1],
                        "session": identity_row[2],
                    }
                )
                phase = "schema-visibility"
                cursor.execute(
                    "select username from all_users where username = :schema_name",
                    schema_name=datasource.schema_name,
                )
                if cursor.fetchone() is None:
                    raise ConfigurationError("Oracle exact schema gorunur degil")
                phase = "metadata-transform"
                cursor.execute(
                    "begin "
                    "dbms_metadata.set_transform_param(dbms_metadata.session_transform,"
                    " 'STORAGE', false);"
                    "dbms_metadata.set_transform_param(dbms_metadata.session_transform,"
                    " 'SEGMENT_ATTRIBUTES', false);"
                    "dbms_metadata.set_transform_param(dbms_metadata.session_transform,"
                    " 'TABLESPACE', false);"
                    "dbms_metadata.set_transform_param(dbms_metadata.session_transform,"
                    " 'SQLTERMINATOR', false);"
                    "dbms_metadata.set_transform_param(dbms_metadata.session_transform,"
                    " 'PRETTY', true); end;"
                )
                object_types = tuple(_METADATA_TYPE)
                binds = ",".join(f":type_{index}" for index in range(len(object_types)))
                parameters = {f"type_{index}": value for index, value in enumerate(object_types)}
                parameters["owner"] = datasource.schema_name
                phase = "object-inventory"
                cursor.execute(
                    "select object_name, object_type, status,"
                    " to_char(last_ddl_time,'YYYY-MM-DD\"T\"HH24:MI:SS')"
                    " from all_objects where owner=:owner"
                    f" and object_type in ({binds})"
                    " and generated = 'N'"
                    " and object_name not like 'BIN$%'"
                    " order by object_type, object_name",
                    parameters,
                )
                rows = cursor.fetchall()
                if not rows:
                    raise ValidationFailed("Oracle schema indekslenebilir nesne tasimiyor")
                if len(rows) > MAX_OBJECTS:
                    raise PolicyViolation("Oracle metadata nesne sayisi siniri asildi")
                objects: list[OracleDdlObject] = []
                excluded_secret_objects = 0
                total_bytes = 0
                for object_name, object_type, status, last_ddl_at in rows:
                    phase = f"get-ddl:{str(object_type).lower().replace(' ', '-')}"
                    metadata_type = _METADATA_TYPE[str(object_type)]
                    cursor.execute(
                        "select dbms_metadata.get_ddl(:metadata_type, :object_name, :owner)"
                        " from dual",
                        metadata_type=metadata_type,
                        object_name=object_name,
                        owner=datasource.schema_name,
                    )
                    ddl_row = cursor.fetchone()
                    if ddl_row is None or ddl_row[0] is None:
                        raise ValidationFailed("Oracle DBMS_METADATA bos DDL dondurdu")
                    raw_ddl = ddl_row[0]
                    ddl = raw_ddl.read() if hasattr(raw_ddl, "read") else str(raw_ddl)
                    if not ddl.strip():
                        raise ValidationFailed("Oracle DBMS_METADATA bos DDL dondurdu")
                    ddl_bytes = ddl.encode("utf-8")
                    total_bytes += len(ddl_bytes)
                    if total_bytes > MAX_TOTAL_DDL_BYTES:
                        raise PolicyViolation("Oracle metadata toplam DDL boyut siniri asildi")
                    findings = scan_text(
                        ddl,
                        relative_path=f"oracle/{object_type}/{object_name}",
                    )
                    if findings:
                        excluded_secret_objects += 1
                        continue
                    objects.append(
                        OracleDdlObject(
                            owner=datasource.schema_name,
                            object_name=str(object_name),
                            object_type=str(object_type),
                            status=str(status),
                            last_ddl_at=str(last_ddl_at),
                            ddl_digest=digest_of_bytes(ddl_bytes),
                            _ddl=ddl,
                        )
                    )
        except (ConfigurationError, PolicyViolation, ValidationFailed):
            raise
        except Exception as exc:
            error = exc.args[0] if exc.args else None
            error_code = getattr(error, "full_code", None) or getattr(error, "code", None)
            identity = type(exc).__name__
            if error_code is not None:
                identity = f"{identity}:{error_code}"
            raise ConfigurationError(
                f"Oracle metadata sorgusu basarisiz: {phase}:{identity}"
            ) from None
        finally:
            connection.close()
        if not objects:
            raise PolicyViolation("Oracle metadata secret-safe indekslenebilir nesne birakmadi")
        return OracleMetadataSnapshot(
            schema_name=datasource.schema_name,
            connection_identity_digest=datasource.connection_identity_digest,
            database_identity_digest=database_identity_digest,
            objects=tuple(objects),
            excluded_secret_objects=excluded_secret_objects,
        )


def _ddl_units(item: OracleDdlObject, start_order: int) -> tuple[ContentUnit, ...]:
    lines = item.ddl.splitlines()
    parts: list[str] = []
    buffer: list[str] = []
    for line in lines:
        candidate = "\n".join((*buffer, line))
        if buffer and (
            len(candidate) > MAX_DDL_CHUNK_CHARACTERS
            or estimate_tokens(candidate) > MAX_DDL_CHUNK_TOKENS
        ):
            parts.append("\n".join(buffer).strip())
            buffer = []
        if len(line) > MAX_DDL_CHUNK_CHARACTERS:
            if buffer:
                parts.append("\n".join(buffer).strip())
                buffer = []
            parts.extend(
                line[offset : offset + MAX_DDL_CHUNK_CHARACTERS].strip()
                for offset in range(0, len(line), MAX_DDL_CHUNK_CHARACTERS)
                if line[offset : offset + MAX_DDL_CHUNK_CHARACTERS].strip()
            )
        else:
            buffer.append(line)
    if buffer:
        parts.append("\n".join(buffer).strip())
    qualified = f"{item.owner}.{item.object_name}:{item.object_type}"
    return tuple(
        ContentUnit(
            unit_id=f"{qualified}#part-{index + 1}",
            kind=UnitKind.DB_OBJECT,
            text=part,
            locator=Locator(object_name=qualified),
            order=start_order + index,
        )
        for index, part in enumerate(parts)
        if part
    )


@dataclass(frozen=True, slots=True)
class OracleMetadataIndexPlan:
    project_id: UUID
    project_slug: str
    snapshot: OracleMetadataSnapshot
    manifest: bytes
    document: NormalizedDocument
    chunk_profile: ChunkProfile
    embedding_profile: EmbeddingProfile
    chunks: tuple[Chunk, ...]

    @property
    def plan_digest(self) -> str:
        return digest(
            {
                "project_id": str(self.project_id),
                "snapshot_revision": self.snapshot.revision_digest,
                "document_digest": self.document.content_digest,
                "chunk_profile_digest": self.chunk_profile.profile_digest,
                "embedding_profile_digest": self.embedding_profile.profile_digest,
                "chunk_count": len(self.chunks),
            }
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "zekam-oracle-metadata-index-plan/v1",
            "project_id": str(self.project_id),
            "project_slug": self.project_slug,
            "snapshot": self.snapshot.sanitized(),
            "unit_count": self.document.unit_count,
            "chunk_count": len(self.chunks),
            "embedding": {
                "model_ref": self.embedding_profile.model_ref,
                "dimension": self.embedding_profile.dimension,
                "profile_digest": self.embedding_profile.profile_digest,
                "mode": "verified-local-provider-required",
                "remote_provider_used": False,
            },
            "plan_digest": self.plan_digest,
            "row_data_included": False,
        }


def build_oracle_metadata_index_plan(
    *, project_id: UUID, project_slug: str, snapshot: OracleMetadataSnapshot
) -> OracleMetadataIndexPlan:
    units: list[ContentUnit] = []
    for item in snapshot.objects:
        units.extend(_ddl_units(item, len(units)))
    if not units:
        raise ValidationFailed("Oracle metadata DDL chunk uretmedi")
    manifest_document = {
        "schema": "zekam-oracle-metadata-manifest/v1",
        "project_id": str(project_id),
        "project_slug": project_slug,
        "snapshot_revision": snapshot.revision_digest,
        "database_identity_digest": snapshot.database_identity_digest,
        "objects": [item.manifest_entry() for item in snapshot.objects],
        "excluded_secret_objects": snapshot.excluded_secret_objects,
        "row_data_included": False,
    }
    manifest = canonical_json(manifest_document).encode("utf-8")
    artifact = IngestionService(default_router()).artifact_for(
        manifest,
        name=f"{project_slug}.oracle-metadata-manifest.json",
        media_type="application/vnd.zekam.oracle-metadata-manifest+json",
        now=dt.datetime.now(dt.UTC),
    )
    document = NormalizedDocument(
        document_id=f"oracle-{project_id}-{snapshot.revision_digest[-16:]}",
        artifact_digest=artifact.artifact_digest,
        source_format=SourceFormat.ORACLE_METADATA,
        units=tuple(units),
        parser_ref=ORACLE_INDEXER_VERSION,
        parser_version="1",
        parser_profile={
            "snapshot_revision": snapshot.revision_digest,
            "database_identity_digest": snapshot.database_identity_digest,
            "metadata_only": True,
            "row_data_included": False,
            "secret_scan": True,
        },
    )
    chunk_profile = ChunkProfile(
        name="oracle-ddl-v1",
        max_tokens=MAX_DDL_CHUNK_TOKENS,
        overlap_tokens=0,
        keep_code_whole=True,
    )
    embedding_profile = EmbeddingProfile(
        model_ref=REAL_EMBEDDING_MODEL_REF,
        dimension=REAL_EMBEDDING_DIMENSION,
        distance="cosine",
    )

    def stable_chunk_suffix(unit: ContentUnit) -> str:
        return digest(
            {
                "unit_id": unit.unit_id,
                "content_digest": digest_of_bytes(unit.text.encode("utf-8")),
            }
        )[-24:]

    chunks = tuple(
        Chunk(
            chunk_id=f"oracle-{project_id}-{stable_chunk_suffix(unit)}",
            document_id=document.document_id,
            text=unit.text,
            locator=unit.locator,
            kind=unit.kind,
            token_count=estimate_tokens(unit.text),
            order=index,
            profile_digest=chunk_profile.profile_digest,
        )
        for index, unit in enumerate(document.units)
    )
    return OracleMetadataIndexPlan(
        project_id=project_id,
        project_slug=project_slug,
        snapshot=snapshot,
        manifest=manifest,
        document=document,
        chunk_profile=chunk_profile,
        embedding_profile=embedding_profile,
        chunks=chunks,
    )


@dataclass(frozen=True, slots=True)
class OracleMetadataIndexResult:
    source_id: UUID
    document_id: UUID
    revision: int
    chunk_count: int
    vector_count: int
    plan: OracleMetadataIndexPlan

    def as_dict(self) -> dict[str, object]:
        return self.plan.as_dict() | {
            "source_id": str(self.source_id),
            "document_id": str(self.document_id),
            "knowledge_revision": self.revision,
            "vector_count": self.vector_count,
            "lexical_state": "ready",
            "embedding_state": "ready",
            "applied": True,
        }


def apply_oracle_metadata_index(
    plan: OracleMetadataIndexPlan,
    *,
    connection: Any,
    knowledge: Any,
    retrieval: Any,
    object_store: LocalContentAddressedStore,
    embedding_provider: EmbeddingProvider | None = None,
    embedding_policy: EmbeddingPolicy | None = None,
    now: dt.datetime | None = None,
) -> OracleMetadataIndexResult:
    """Persist a secret-safe Oracle DDL snapshot and its local vectors."""

    moment = now or dt.datetime.now(dt.UTC)
    if embedding_provider is None or embedding_policy is None:
        raise PolicyViolation("Verified embedding provider/policy olmadan indeks uygulanamaz")
    provider_profile = embedding_provider.describe()
    accepted_model_refs = {
        provider_profile.exact_model_id,
        f"openai/{provider_profile.exact_model_id}",
    }
    if (
        plan.embedding_profile.model_ref not in accepted_model_refs
        or plan.embedding_profile.dimension != provider_profile.dimension
        or plan.embedding_profile.provider_profile_digest != provider_profile.profile_digest
        or embedding_policy.expected_profile_digest != provider_profile.profile_digest
    ):
        raise PolicyViolation("Oracle index/provider profile drift; rebuild required")
    provider_profile.assert_policy(embedding_policy)
    vectors: dict[str, tuple[float, ...]] = {}
    for offset in range(0, len(plan.chunks), 8):
        chunk_batch = plan.chunks[offset : offset + 8]
        embedded = embedding_provider.embed_documents(
            tuple(chunk.text for chunk in chunk_batch), embedding_policy
        )
        if (
            len(embedded.vectors) != len(chunk_batch)
            or embedded.receipt.vector_count != len(chunk_batch)
            or embedded.receipt.dimension != provider_profile.dimension
            or embedded.receipt.profile_digest != provider_profile.profile_digest
        ):
            raise PolicyViolation("Oracle embedding batch/receipt contract drift")
        for chunk, vector in zip(chunk_batch, embedded.vectors, strict=True):
            provider_profile.validate_vector(vector)
            vectors[chunk.chunk_id] = vector
    if len(vectors) != len(plan.chunks):
        raise PolicyViolation("Oracle embedding batch eksik/duplicate sonuc uretti")
    service = IngestionService(default_router())
    artifact = service.artifact_for(
        plan.manifest,
        name=f"{plan.project_slug}.oracle-metadata-manifest.json",
        media_type="application/vnd.zekam.oracle-metadata-manifest+json",
        now=moment,
    )
    if artifact.artifact_digest != plan.document.artifact_digest:
        raise PolicyViolation("Oracle metadata manifest artifact drift")
    stored = object_store.ensure().put(plan.manifest, media_type=artifact.media_type)
    if stored.digest != artifact.content_digest:
        raise PolicyViolation("Oracle metadata manifest CAS digest uyusmazligi")
    source_slug = f"project-{plan.project_slug}-oracle-metadata"
    ingestion = service.start(
        job_id=plan.plan_digest,
        source_id=source_slug,
        artifact=artifact,
        idempotency_key=plan.plan_digest,
    )
    ingestion = service.store(ingestion)
    ingestion = ingestion.advance(IngestionStage.PARSED).advance(IngestionStage.NORMALIZED)
    ingestion = service.index(ingestion)
    pending = pending_version(
        version_id=f"{source_slug}-pending",
        source_id=source_slug,
        revision=1,
        artifact=artifact,
        content_digest=plan.document.content_digest,
        now=moment,
    )
    ingestion, _ = service.activate(ingestion, pending)
    with connection.transaction():
        artifact_id = knowledge.store_artifact(artifact)
        source_id = knowledge.register_source(source_slug, SourceFormat.ORACLE_METADATA, now=moment)
        equivalent = knowledge.equivalent_version(
            source_id,
            artifact_digest=artifact.artifact_digest,
            content_digest=plan.document.content_digest,
        )
        if equivalent is not None and equivalent[2] == "active":
            with connection.cursor() as cursor:
                cursor.execute(
                    "select d.id, count(distinct c.id), count(distinct e.id),"
                    " ep.profile_digest"
                    " from knowledge.normalized_document d"
                    " join knowledge.source_version v on v.id=d.version_id"
                    " join knowledge.document_index_profile dip on dip.document_id=d.id"
                    " join knowledge.embedding_profile ep on ep.id=dip.embedding_profile_id"
                    " left join knowledge.chunk c on c.document_id=d.id"
                    " left join knowledge.chunk_embedding e on e.chunk_id=c.id"
                    " where v.id=%s group by d.id,ep.profile_digest",
                    (equivalent[0],),
                )
                row = cursor.fetchone()
            if row is None or int(row[1]) != len(plan.chunks) or int(row[2]) != len(plan.chunks):
                raise PolicyViolation("Mevcut Oracle metadata indeksi eksik")
            if str(row[3]) != plan.embedding_profile.profile_digest:
                raise PolicyViolation(
                    "Mevcut Oracle index provider profile drift; rebuild required"
                )
            return OracleMetadataIndexResult(
                source_id=source_id,
                document_id=row[0],
                revision=equivalent[1],
                chunk_count=int(row[1]),
                vector_count=int(row[2]),
                plan=plan,
            )
        revision = knowledge.next_revision(source_id)
        version = pending_version(
            version_id=f"{source_slug}-r{revision}",
            source_id=source_slug,
            revision=revision,
            artifact=artifact,
            content_digest=plan.document.content_digest,
            now=moment,
        )
        job_id = knowledge.start_job(
            ingestion, source_id=source_id, artifact_id=artifact_id, now=moment
        )
        version_id = knowledge.store_version(version, source_id=source_id, artifact_id=artifact_id)
        document_id = knowledge.store_document(plan.document, version_id=version_id, now=moment)
        chunk_profile_id = retrieval.store_chunk_profile(plan.chunk_profile, now=moment)
        embedding_profile_id = retrieval.store_embedding_profile(plan.embedding_profile, now=moment)
        chunk_ids = retrieval.store_chunks(plan.chunks, document_id=document_id, now=moment)
        for chunk in plan.chunks:
            retrieval.store_embedding(
                chunk_ids[chunk.chunk_id],
                embedding_profile_id,
                plan.embedding_profile,
                vectors[chunk.chunk_id],
                now=moment,
            )
        retrieval.store_document_profiles(
            document_id=document_id,
            chunk_profile_id=chunk_profile_id,
            embedding_profile_id=embedding_profile_id,
            embedding_state="ready",
            now=moment,
        )
        knowledge.save_progress(job_id, ingestion, now=moment)
        previous = knowledge.active_version(source_id)
        if previous is not None and previous != version_id:
            knowledge.supersede_version(previous, version_id)
        knowledge.activate_version(version_id)
    return OracleMetadataIndexResult(
        source_id=source_id,
        document_id=document_id,
        revision=revision,
        chunk_count=len(plan.chunks),
        vector_count=len(plan.chunks),
        plan=plan,
    )
