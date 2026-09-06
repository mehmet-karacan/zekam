"""Canonical project source/Oracle metadata -> OpenCode BGE-M3 -> hybrid RAG.

The source project is never mutated. Discovery excludes ignored, binary, oversized,
unsafe and secret-bearing files. Remote disclosure requires an explicit CLI flag;
vectors are cached durably under ``ZEKAM_HOME`` so an interrupted indexing run
resumes by exact chunk, content and provider-profile identity.  Connection secrets
remain process-memory-only and are never persisted or embedded.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import struct
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from zekam.application.embedded_project_rag import EmbeddedProjectRAG
from zekam.application.embedding_provider import (
    EmbeddingBatch,
    EmbeddingPolicy,
    EmbeddingProbeFixture,
)
from zekam.application.home import HomeLayout
from zekam.application.knowledge_file_plane import ProjectProjection
from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.application.model_health_service import ProbeUnavailable
from zekam.application.model_registry import load_inventory
from zekam.application.odi11g_smart_export import (
    OdiSanitizedPlan,
    build_sanitized_odi_plan,
    load_smart_binding,
)
from zekam.application.opencode_embedding import (
    default_opencode_config_file,
    load_opencode_embedding_configuration,
)
from zekam.application.oracle_metadata_index import (
    OracleDatasource,
    OracleMetadataClient,
    OracleMetadataIndexPlan,
    build_oracle_metadata_index_plan,
    load_project_oracle_datasource,
)
from zekam.application.project_knowledge_index import ProjectIndexPlan, build_project_index_plan
from zekam.application.provider_contract_execution import PreparedProviderContractCall
from zekam.application.provider_contract_runner import (
    ProviderExecutionHost,
    RuntimeProviderContractRunner,
)
from zekam.application.request_routing import (
    RegisteredProject,
    load_project_families,
    route_request,
)
from zekam.application.source_discovery import DiscoveryReport, discover
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import validate_slug
from zekam.domain.retrieval import Chunk
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    DataClassification,
    SecretBackend,
    SecretRef,
)
from zekam.domain.work import EffectKind
from zekam.infrastructure.embedding.opencode_remote import (
    MAX_BATCH_SIZE,
    OpenCodeRemoteEmbeddingProvider,
    OpenCodeRuntimeInvocation,
    RuntimeOpenCodeEmbeddingExecutor,
)
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_file,
    restrict_private_tree,
)
from zekam.infrastructure.opencode_provider_ledger import (
    LiveProcessClient,
    SQLiteProviderLedgerHost,
    _work,
)
from zekam.infrastructure.process.capability_worker import ProcessIsolatedJsonProviderTransport
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

MODEL_ID = "openai/BAAI/bge-m3"
PROVIDER_ID = "litellm"
VECTOR_DIMENSION = 1024
VECTOR_CACHE_SCHEMA = """
pragma foreign_keys=on;
create table if not exists vector_cache(
  chunk_id text not null,
  content_digest text not null,
  provider_profile_digest text not null,
  vector_blob blob not null,
  vector_digest text not null,
  created_at text not null,
  primary key(chunk_id,content_digest,provider_profile_digest)
) strict;
"""


def _git_source_state(root: Path) -> tuple[str, str, str]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    status_digest = hashlib.sha256(status).hexdigest()
    return head, status_digest, f"{head}:status:{status_digest}"


def _registered_project(home: Path, project_slug: str) -> UUID:
    store = SQLiteOperationalStore(home / "state" / "operational.db")
    with store.unit_of_work() as uow:
        project = uow.resolve_project(project_slug)
        uow.commit()
    return UUID(project.id)


def resolve_registered_project(home: Path, reference: str) -> str:
    """Resolve a project id/slug/alias to the canonical runtime slug."""

    store = SQLiteOperationalStore(home / "state" / "operational.db")
    with store.unit_of_work() as uow:
        project = uow.resolve_project(reference)
        uow.commit()
    return project.slug


def _project_plan(
    root: Path, *, project_id: UUID, project_slug: str
) -> tuple[DiscoveryReport, ProjectIndexPlan]:
    _, _, revision = _git_source_state(root)
    discovery = discover(root)
    plan = build_project_index_plan(
        project_id=project_id,
        project_slug=project_slug,
        source_root=root,
        source_revision=revision,
        expected_tree_digest=discovery.tree_digest,
    )
    return discovery, plan


def _plan_document(
    discovery: DiscoveryReport,
    plan: ProjectIndexPlan,
    oracle_plan: OracleMetadataIndexPlan | None = None,
    odi_plan: OdiSanitizedPlan | None = None,
) -> dict[str, Any]:
    oracle_chunks = len(oracle_plan.chunks) if oracle_plan is not None else 0
    odi_chunks = len(odi_plan.chunks) if odi_plan is not None else 0
    return {
        "schema": "zekam-project-rag-plan/v1",
        "project_id": str(plan.project_id),
        "project_slug": plan.project_slug,
        "source_revision": plan.source_revision,
        "tree_digest": plan.tree_digest,
        "plan_digest": digest(
            {
                "repository_plan_digest": plan.plan_digest,
                "oracle_plan_digest": oracle_plan.plan_digest if oracle_plan is not None else None,
                "odi_plan_digest": odi_plan.plan_digest if odi_plan is not None else None,
            }
        ),
        "discovered_file_count": discovery.file_count,
        "selected_file_count": plan.selected_file_count,
        "source_chunk_count": len(plan.chunks),
        "oracle_chunk_count": oracle_chunks,
        "odi_chunk_count": odi_chunks,
        "chunk_count": len(plan.chunks) + oracle_chunks + odi_chunks,
        "skipped_secret_files": len({item.relative_path for item in discovery.secrets}),
        "secret_finding_count": len(discovery.secrets),
        "skipped_unsupported": plan.skipped_unsupported,
        "skipped_encoding": plan.skipped_encoding,
        "truncated": discovery.truncated,
        "model_id": MODEL_ID,
        "dimension": VECTOR_DIMENSION,
        "estimated_provider_calls": (
            len(plan.chunks) + oracle_chunks + odi_chunks + MAX_BATCH_SIZE - 1
        )
        // MAX_BATCH_SIZE,
        "source_access": "read-only",
        "database_access": "metadata-only" if oracle_plan is not None else "disabled",
        "odi_access": "sanitized-metadata" if odi_plan is not None else "disabled",
        "row_data_included": False,
        "secret_values_recorded": False,
    }


def _runtime_paths(home: Path, project_slug: str) -> dict[str, Path]:
    root = home.resolve(strict=True)
    issues = HomeLayout(root).verify()
    if issues:
        raise PolicyViolation(f"ZEKAM_HOME layout gecersiz: {issues[0].kind}")
    project_root = HomeLayout(root).ensure_project(project_slug)
    index_root = root / "knowledge-index" / "vector" / "opencode-bge-m3" / project_slug
    manifest_root = root / "knowledge-index" / "manifests" / project_slug
    for directory in (project_root, index_root, manifest_root):
        directory.mkdir(parents=True, exist_ok=True)
        restrict_private_tree(directory)
        if not private_directory(directory):
            raise PolicyViolation("Project RAG runtime private ACL ister")
    return {
        "home": root,
        "project_root": project_root,
        "index_root": index_root,
        "manifest_root": manifest_root,
        "index": index_root / "knowledge.sqlite3",
        "cache": index_root / "vector-cache.sqlite3",
        "ledger": project_root / "runtime" / "provider-ledger.sqlite3",
        "state": project_root / "runtime" / "rag-state.json",
    }


def _provider(
    ledger_path: Path,
    config_file: Path,
    project_id: UUID,
) -> tuple[
    OpenCodeRemoteEmbeddingProvider,
    EmbeddingPolicy,
    SQLiteProviderLedgerHost,
    dict[str, Any],
]:
    configuration = load_opencode_embedding_configuration(
        config_file,
        provider_id=PROVIDER_ID,
        selected_model_id=MODEL_ID,
        inventory=load_inventory(),
    )
    realm_id = uuid5(NAMESPACE_URL, f"zekam://local-realm/{project_id}")
    host = SQLiteProviderLedgerHost(ledger_path, realm_id)
    transport = ProcessIsolatedJsonProviderTransport()
    client = LiveProcessClient(configuration, transport)

    def invocation(prepared: PreparedProviderContractCall) -> OpenCodeRuntimeInvocation:
        work = _work(realm_id, project_id)
        host.register(work)
        secret_ref = SecretRef.create(
            realm_id=realm_id,
            name="opencode-litellm-embedding",
            provider=prepared.plan.provider_ref,
            purpose="project source and Oracle metadata embedding",
            allowed_operations=(prepared.plan.operation,),
            store_backend=SecretBackend.ENVIRONMENT,
            store_locator=configuration.credential_locator,
        )
        authorization = Authorization.issue(
            realm_id=realm_id,
            actor_id=uuid4(),
            plan_digest=prepared.plan.authorization_plan_digest,
            effect_digest=prepared.plan.effect_request.effect_digest,
            scope=AuthorizationScope(
                allowed_resources=(prepared.plan.target, prepared.plan.call_resource),
                allowed_effects=(EffectKind.PROVIDER_CALL.value,),
                provider_refs=(prepared.plan.provider_ref,),
                secret_ref_ids=(secret_ref.id,),
                data_classifications=prepared.plan.data_classifications,
            ),
            risk="critical",
            lifetime=dt.timedelta(minutes=10),
        )
        return OpenCodeRuntimeInvocation(
            RuntimeProviderContractRunner(
                host=cast(ProviderExecutionHost, host),
                work=work,
                client=client,
            ),
            secret_ref,
            authorization,
            "project-rag-runtime",
        )

    provider = OpenCodeRemoteEmbeddingProvider(
        configuration,
        RuntimeOpenCodeEmbeddingExecutor(invocation),
        dimension=VECTOR_DIMENSION,
        max_batch_size=MAX_BATCH_SIZE,
    )
    fixture = EmbeddingProbeFixture(
        query="Which component validates a project source revision?",
        positive_passage="The project index validates the source revision and tree digest.",
        negative_passage="A recipe explains how to bake a chocolate cake.",
        source_refs=("synthetic:project-index", "synthetic:recipe"),
        source_digests=(digest("project-index"), digest("recipe")),
        classification=DataClassification.PUBLIC,
    )
    probe = provider.probe(fixture)
    policy = EmbeddingPolicy(
        DataClassification.INTERNAL,
        probe.profile.profile_digest,
        remote_disclosure_authorized=True,
    )
    return (
        provider,
        policy,
        host,
        {
            "profile_digest": probe.profile.profile_digest,
            "probe_evidence_digest": probe.evidence_digest,
            "semantic_margin": probe.semantic_margin,
            "max_repeat_delta": probe.max_repeat_delta,
            "max_batch_delta": probe.max_batch_delta,
        },
    )


def _cache(path: Path) -> sqlite3.Connection:
    created = not path.exists()
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    mode = str(connection.execute("pragma journal_mode=delete").fetchone()[0]).casefold()
    if mode != "delete":
        connection.close()
        raise PolicyViolation("Vector cache DELETE journal ister")
    connection.execute("pragma synchronous=full")
    connection.executescript(VECTOR_CACHE_SCHEMA)
    if created:
        restrict_private_file(path)
    if not private_regular(path):
        connection.close()
        raise PolicyViolation("Vector cache private regular file olmali")
    return connection


def _vector_blob(vector: tuple[float, ...]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _vector_from_blob(blob: bytes) -> tuple[float, ...]:
    if len(blob) != VECTOR_DIMENSION * 4:
        raise ValidationFailed("Vector cache dimension drift")
    return tuple(float(value) for value in struct.unpack(f"<{VECTOR_DIMENSION}f", blob))


def _embed_documents_with_retry(
    provider: OpenCodeRemoteEmbeddingProvider,
    texts: tuple[str, ...],
    policy: EmbeddingPolicy,
) -> EmbeddingBatch:
    """Retry only transport-unavailable document embeddings with a strict bound."""

    for attempt in range(4):
        try:
            return provider.embed_documents(texts, policy)
        except ProbeUnavailable:
            if attempt == 3:
                raise
            time.sleep(min(2**attempt, 4))
    raise AssertionError("unreachable")


def _cached_vectors(
    connection: sqlite3.Connection,
    chunks: tuple[Chunk, ...],
    profile_digest: str,
) -> dict[str, tuple[float, ...]]:
    result: dict[str, tuple[float, ...]] = {}
    for chunk in chunks:
        content_digest = digest_of_bytes(chunk.text.encode("utf-8"))
        row = connection.execute(
            "select vector_blob,vector_digest from vector_cache where chunk_id=?"
            " and content_digest=? and provider_profile_digest=?",
            (chunk.chunk_id, content_digest, profile_digest),
        ).fetchone()
        if row is None:
            aliases = connection.execute(
                "select vector_blob,vector_digest from vector_cache where content_digest=?"
                " and provider_profile_digest=?",
                (content_digest, profile_digest),
            ).fetchall()
            if not aliases:
                continue
            verified_aliases = []
            for alias in aliases:
                alias_blob = bytes(alias["vector_blob"])
                alias_digest = str(alias["vector_digest"])
                if digest_of_bytes(alias_blob) != alias_digest:
                    raise PolicyViolation("Vector cache alias digest drift")
                verified_aliases.append((alias_digest, alias))
            # The accepted remote BGE profile permits bounded numeric jitter.
            # Select one valid prior vector deterministically; profile/content
            # equality is still mandatory and every candidate digest is checked.
            row = min(verified_aliases, key=lambda item: item[0])[1]
            connection.execute(
                "insert or ignore into vector_cache values(?,?,?,?,?,?)",
                (
                    chunk.chunk_id,
                    content_digest,
                    profile_digest,
                    bytes(row["vector_blob"]),
                    str(row["vector_digest"]),
                    dt.datetime.now(dt.UTC).isoformat(),
                ),
            )
        blob = bytes(row["vector_blob"])
        if digest_of_bytes(blob) != str(row["vector_digest"]):
            raise PolicyViolation("Vector cache digest drift")
        result[chunk.chunk_id] = _vector_from_blob(blob)
    return result


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with stage.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        restrict_private_file(stage)
        stage.replace(path)
        restrict_private_file(path)
    finally:
        stage.unlink(missing_ok=True)


def bind_project_source(home: Path, project_slug: str, source_root: Path) -> dict[str, Any]:
    """Persist this device's private, exact source-root binding for a project."""

    slug = validate_slug(project_slug)
    project_id = _registered_project(home, slug)
    root = source_root.resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor):
        raise ValidationFailed("Project source root bounded directory olmali")
    head, status_digest, revision = _git_source_state(root)
    paths = _runtime_paths(home, slug)
    document = {
        "schema": "zekam-project-local-source-binding/v1",
        "project_id": str(project_id),
        "project_slug": slug,
        "source_root": str(root),
        "source_root_digest": digest_of_bytes(str(root).encode("utf-8")),
        "git_head": head,
        "git_status_digest": f"sha256:{status_digest}",
        "source_revision": revision,
    }
    binding_path = paths["project_root"] / "baglantilar" / "source.json"
    _write_private(binding_path, (canonical_json(document) + "\n").encode("utf-8"))
    if not private_regular(binding_path):
        raise PolicyViolation("Project source binding private ACL ister")
    return document | {"binding_ref": binding_path.relative_to(paths["home"]).as_posix()}


def resolve_project_source(home: Path, project_slug: str) -> Path:
    """Resolve and validate this device's private source-root binding."""

    slug = validate_slug(project_slug)
    project_id = _registered_project(home, slug)
    paths = _runtime_paths(home, slug)
    binding_path = paths["project_root"] / "baglantilar" / "source.json"
    if not binding_path.is_file() or not private_regular(binding_path):
        raise ValidationFailed("Project local source binding bulunamadi")
    document = json.loads(binding_path.read_text(encoding="utf-8"))
    if (
        document.get("schema") != "zekam-project-local-source-binding/v1"
        or document.get("project_id") != str(project_id)
        or document.get("project_slug") != slug
    ):
        raise PolicyViolation("Project local source binding scope drift")
    root = Path(str(document.get("source_root", ""))).resolve(strict=True)
    if (
        not root.is_dir()
        or root == Path(root.anchor)
        or document.get("source_root_digest") != digest_of_bytes(str(root).encode("utf-8"))
    ):
        raise PolicyViolation("Project local source binding path drift")
    return root


def project_rag_status(home: Path, project_slug: str) -> dict[str, Any]:
    """Return durable RAG state without provider or project database calls."""

    slug = validate_slug(project_slug)
    project_id = _registered_project(home, slug)
    paths = _runtime_paths(home, slug)
    if not paths["state"].is_file() or not paths["index"].is_file():
        return {
            "schema": "zekam-project-rag-status/v1",
            "project_id": str(project_id),
            "project_slug": slug,
            "state": "unavailable",
        }
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    with SQLiteKnowledgeIndex(paths["index"], read_only=True) as index:
        generation = index.generation(str(project_id))
        integrity = index.integrity()
    if state.get("generation_digest") != generation.generation_digest:
        raise PolicyViolation("Project RAG state/generation drift")
    return {
        "schema": "zekam-project-rag-status/v1",
        "project_id": str(project_id),
        "project_slug": slug,
        "state": "ready" if integrity.get("status") == "passed" else "corrupt",
        "generation_digest": generation.generation_digest,
        "chunk_count": generation.chunk_count,
        "source_chunk_count": state.get("source_chunk_count"),
        "oracle_chunk_count": state.get("oracle_chunk_count"),
        "odi_chunk_count": state.get("odi_chunk_count", 0),
        "database_access": state.get("database_access", "unknown"),
        "odi_access": (
            "sanitized-metadata" if state.get("odi_source_digest") is not None else "disabled"
        ),
        "odi_source_digest": state.get("odi_source_digest"),
        "odi_lineage_edge_count": state.get("odi_lineage_edge_count", 0),
        "source_revision": generation.source_revision,
        "tree_digest": generation.tree_digest,
        "provider_profile_digest": generation.provider_profile_digest,
        "index_integrity": integrity,
        "row_data_included": False,
        "secret_values_recorded": False,
    }


def read_project_citation(
    home: Path,
    project_slug: str,
    chunk_id: str,
    *,
    generation_digest: str | None = None,
) -> dict[str, Any]:
    """Re-open one citation from the pinned local index and verify its identity."""

    slug = validate_slug(project_slug)
    project_id = str(_registered_project(home, slug))
    if not chunk_id or len(chunk_id.encode("utf-8")) > 512:
        raise ValidationFailed("Citation chunk id bounded olmali")
    paths = _runtime_paths(home, slug)
    if not paths["index"].is_file():
        raise ValidationFailed("Project knowledge index bulunamadi")
    with SQLiteKnowledgeIndex(paths["index"], read_only=True) as index:
        generation = index.generation(project_id)
        selected_generation = generation_digest or generation.generation_digest
        views = index.views(
            project_id,
            (chunk_id,),
            generation_digest=selected_generation,
        )
        if chunk_id not in views:
            raise PolicyViolation("Citation chunk pinned generation icinde bulunamadi")
        identity = index.source_identity(
            project_id,
            chunk_id,
            generation_digest=selected_generation,
        )
    view = views[chunk_id]
    return {
        "schema": "zekam-project-citation/v1",
        "project_id": project_id,
        "project_slug": slug,
        "generation_digest": selected_generation,
        "chunk_id": chunk_id,
        **identity,
        "locator_type": "database-object" if view.locator.object_name else "project-file",
        "locator": view.locator.as_dict(),
        "body": view.text,
        "verified": True,
    }


def resolve_question_project(home: Path, question: str) -> str:
    """Resolve only a deterministic single-project RAG route for ``zekam ask``."""

    store = SQLiteOperationalStore(home / "state" / "operational.db")
    with store.unit_of_work() as uow:
        projects = tuple(
            RegisteredProject(project.slug, uow.list_project_aliases(project.id))
            for project in uow.list_projects(include_archived=False)
        )
        uow.commit()
    route = route_request(
        question,
        catalog=load_project_families(),
        registered_projects=projects,
    )
    if route.status == "general":
        raise ValidationFailed("Genel bilgi sorusu project RAG kullanmaz; route=general-research")
    if route.status != "selected":
        raise ValidationFailed(f"Soru route karari secilemedi: {route.strategy}")
    if route.strategy != "single-project-rag" or len(route.project_refs) != 1:
        raise ValidationFailed(
            "Soru tek proje RAG degil; route preview hedeflerini fan-out kullanin"
        )
    return route.project_refs[0]


def _oracle_source_records(
    plan: OracleMetadataIndexPlan,
    *,
    source_revision: str,
    start_order: int,
    vectors: dict[str, tuple[float, ...]],
) -> tuple[KnowledgeIndexRecord, ...]:
    by_locator = {
        f"{item.owner}.{item.object_name}:{item.object_type}": item
        for item in plan.snapshot.objects
    }
    records: list[KnowledgeIndexRecord] = []
    for chunk in plan.chunks:
        locator = chunk.locator.object_name
        if locator is None or locator not in by_locator:
            raise PolicyViolation("Oracle chunk locator metadata object ile eslesmiyor")
        item = by_locator[locator]
        source_path = f"oracle/{item.object_type.casefold().replace(' ', '-')}/{item.object_name}"
        records.append(
            KnowledgeIndexRecord(
                chunk_id=_generation_chunk_id(chunk.chunk_id, source_revision),
                project_id=str(plan.project_id),
                source_revision=source_revision,
                source_path=source_path,
                source_digest=item.ddl_digest,
                locator=replace(chunk.locator, relative_path=source_path),
                text=chunk.text,
                content_digest=digest_of_bytes(chunk.text.encode("utf-8")),
                chunk_order=start_order + chunk.order,
                vector=vectors[chunk.chunk_id],
            )
        )
    return tuple(records)


def _generation_chunk_id(chunk_id: str, source_revision: str) -> str:
    """Keep immutable generations from reusing a globally unique SQLite row id."""

    return f"{chunk_id}-g{source_revision.removeprefix('sha256:')[-16:]}"


def _collect_oracle_with_receipt(
    home: Path,
    project_id: UUID,
    datasource: OracleDatasource,
) -> tuple[Any, dict[str, object]]:
    """Collect metadata behind the canonical local claim/receipt ledger."""

    runtime = SQLiteLocalRuntimeStore(home / "state" / "operational.db")
    effect = {
        "project_id": str(project_id),
        "operation": "oracle.metadata.read",
        "connection_identity_digest": datasource.connection_identity_digest,
        "schema_name": datasource.schema_name,
        "config_relative_path": datasource.config_relative_path,
        "metadata_only": True,
        "row_data_included": False,
    }
    run_nonce = uuid4().hex
    job, created = runtime.enqueue(
        idempotency_key=f"oracle-metadata:{project_id}:{run_nonce}",
        payload={"operation": "oracle.metadata.read", "effect": effect},
        max_attempts=1,
    )
    if not created:
        raise PolicyViolation("Oracle metadata job replay yeni DB effect calistiramaz")
    owner_id = f"project-rag-{run_nonce[:12]}"
    owner_token = uuid4().hex
    work = runtime.claim_next(
        owner_id=owner_id,
        owner_pid=os.getpid(),
        owner_token=owner_token,
        lease_seconds=600,
        resources=(f"oracle-metadata:{datasource.connection_identity_digest}",),
        supported_operations=("oracle.metadata.read",),
        job_id=job.id,
    )
    if work is None:
        raise PolicyViolation("Oracle metadata job claim edilemedi")
    effect_digest = digest(effect)
    claim, claim_created = runtime.claim_effect(
        work,
        operation="oracle.metadata.read",
        effect_digest=effect_digest,
        idempotency_key=f"job:{job.id}:effect:{effect_digest}",
    )
    if not claim_created:
        raise PolicyViolation("Oracle metadata effect claim replay calistirilamaz")
    try:
        snapshot = OracleMetadataClient().collect(datasource)
    except Exception as exc:
        evidence = digest(
            {
                "operation": "oracle.metadata.read",
                "status": "failed",
                "error_type": type(exc).__name__,
            }
        )
        receipt = runtime.record_receipt(
            claim,
            status="failed",
            evidence_digest=evidence,
        )
        runtime.finish(work, state="failed", evidence_digest=evidence)
        raise
    evidence = digest(
        {
            "operation": "oracle.metadata.read",
            "status": "completed",
            "snapshot_revision": snapshot.revision_digest,
            "object_count": len(snapshot.objects),
            "row_data_included": False,
        }
    )
    receipt = runtime.record_receipt(
        claim,
        status="completed",
        evidence_digest=evidence,
    )
    runtime.finish(work, state="completed", evidence_digest=evidence)
    return snapshot, {
        "job_id": job.id,
        "claim_id": claim.id,
        "claim_effect_digest": claim.effect_digest,
        "receipt_id": receipt.id,
        "receipt_status": receipt.status,
        "receipt_evidence_digest": receipt.evidence_digest,
    }


def _index(
    source_root: Path,
    home: Path,
    project_id: UUID,
    project_slug: str,
    opencode_config: Path,
    oracle_config: str | None,
    *,
    batch_size: int,
    authorize_odi_metadata: bool = False,
) -> dict[str, Any]:
    paths = _runtime_paths(home, project_slug)
    source_state_before = _git_source_state(source_root)
    discovery, plan = _project_plan(source_root, project_id=project_id, project_slug=project_slug)
    oracle_plan: OracleMetadataIndexPlan | None = None
    oracle_receipt: dict[str, object] | None = None
    if oracle_config is not None:
        datasource = load_project_oracle_datasource(source_root, oracle_config)
        snapshot, oracle_receipt = _collect_oracle_with_receipt(
            paths["home"], project_id, datasource
        )
        oracle_plan = build_oracle_metadata_index_plan(
            project_id=project_id,
            project_slug=project_slug,
            snapshot=snapshot,
        )
    odi_binding = load_smart_binding(paths["home"], project_slug)
    odi_plan: OdiSanitizedPlan | None = None
    if odi_binding is not None:
        if not authorize_odi_metadata:
            raise PolicyViolation("ODI metadata disclosure explicit authorization ister")
        odi_plan = build_sanitized_odi_plan(
            project_id=project_id,
            project_slug=project_slug,
            source=Path(str(odi_binding["source_file"])),
        )
    provider, policy, ledger, probe = _provider(paths["ledger"], opencode_config, project_id)
    profile = provider.describe()
    bound_plan = replace(
        plan,
        embedding_profile=replace(
            plan.embedding_profile,
            provider_profile_digest=profile.profile_digest,
        ),
    )
    bound_oracle_plan = (
        replace(
            oracle_plan,
            embedding_profile=replace(
                oracle_plan.embedding_profile,
                provider_profile_digest=profile.profile_digest,
            ),
        )
        if oracle_plan is not None
        else None
    )
    chunks = (
        bound_plan.chunks
        + (bound_oracle_plan.chunks if bound_oracle_plan is not None else ())
        + (odi_plan.chunks if odi_plan is not None else ())
    )
    connection = _cache(paths["cache"])
    try:
        vectors = _cached_vectors(connection, chunks, profile.profile_digest)
        cache_hits = len(vectors)
        missing = tuple(chunk for chunk in chunks if chunk.chunk_id not in vectors)
        provider_batches = 0
        for offset in range(0, len(missing), batch_size):
            batch = missing[offset : offset + batch_size]
            embedded = _embed_documents_with_retry(
                provider, tuple(chunk.text for chunk in batch), policy
            )
            if len(embedded.vectors) != len(batch):
                raise PolicyViolation("Project RAG partial embedding batch")
            connection.execute("begin immediate")
            try:
                for chunk, vector in zip(batch, embedded.vectors, strict=True):
                    profile.validate_vector(vector)
                    content_digest = digest_of_bytes(chunk.text.encode("utf-8"))
                    blob = _vector_blob(vector)
                    connection.execute(
                        "insert into vector_cache values(?,?,?,?,?,?)",
                        (
                            chunk.chunk_id,
                            content_digest,
                            profile.profile_digest,
                            blob,
                            digest_of_bytes(blob),
                            dt.datetime.now(dt.UTC).isoformat(),
                        ),
                    )
                    vectors[chunk.chunk_id] = vector
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            provider_batches += 1
            print(
                json.dumps(
                    {
                        "progress": len(vectors),
                        "total": len(chunks),
                        "provider_batches": provider_batches,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
    finally:
        connection.close()
    if len(vectors) != len(chunks):
        raise PolicyViolation("Project RAG vector cache incomplete")
    if _git_source_state(source_root) != source_state_before:
        raise PolicyViolation("Project source indexing sirasinda degisti; generation uygulanmadi")

    combined_revision = digest(
        {
            "repository_revision": bound_plan.source_revision,
            "oracle_revision": bound_oracle_plan.snapshot.revision_digest
            if bound_oracle_plan
            else None,
            "odi_source_digest": odi_plan.source_digest if odi_plan else None,
            "odi_plan_digest": odi_plan.plan_digest if odi_plan else None,
        }
    )
    combined_tree_digest = digest(
        {
            "repository_tree": bound_plan.tree_digest,
            "oracle_database": bound_oracle_plan.snapshot.database_identity_digest
            if bound_oracle_plan
            else None,
            "oracle_revision": bound_oracle_plan.snapshot.revision_digest
            if bound_oracle_plan
            else None,
            "odi_source_digest": odi_plan.source_digest if odi_plan else None,
        }
    )
    combined_manifest = canonical_json(
        {
            "schema": "zekam-project-combined-source-manifest/v1",
            "project_id": str(project_id),
            "project_slug": project_slug,
            "repository_manifest_digest": digest_of_bytes(bound_plan.manifest),
            "oracle_manifest_digest": (
                digest_of_bytes(bound_oracle_plan.manifest)
                if bound_oracle_plan is not None
                else None
            ),
            "odi_manifest_digest": digest_of_bytes(odi_plan.manifest) if odi_plan else None,
            "source_revision": combined_revision,
            "tree_digest": combined_tree_digest,
            "row_data_included": False,
            "secret_values_recorded": False,
        }
    ).encode("utf-8")
    source_digests = {
        item.relative_path: item.content_digest for item in bound_plan.discovery.files
    }
    source_records = tuple(
        KnowledgeIndexRecord(
            chunk_id=_generation_chunk_id(chunk.chunk_id, combined_revision),
            project_id=str(project_id),
            source_revision=combined_revision,
            source_path=str(chunk.locator.relative_path),
            source_digest=source_digests[str(chunk.locator.relative_path)],
            locator=chunk.locator,
            text=chunk.text,
            content_digest=digest_of_bytes(chunk.text.encode("utf-8")),
            chunk_order=chunk.order,
            vector=vectors[chunk.chunk_id],
        )
        for chunk in bound_plan.chunks
    )
    oracle_records = (
        _oracle_source_records(
            bound_oracle_plan,
            source_revision=combined_revision,
            start_order=len(source_records),
            vectors=vectors,
        )
        if bound_oracle_plan is not None
        else ()
    )
    odi_source_digest = odi_plan.source_digest if odi_plan is not None else ""
    odi_records = tuple(
        KnowledgeIndexRecord(
            chunk_id=_generation_chunk_id(chunk.chunk_id, combined_revision),
            project_id=str(project_id),
            source_revision=combined_revision,
            source_path=str(chunk.locator.relative_path),
            source_digest=odi_source_digest,
            locator=chunk.locator,
            text=chunk.text,
            content_digest=digest_of_bytes(chunk.text.encode("utf-8")),
            chunk_order=len(source_records) + len(oracle_records) + chunk.order,
            vector=vectors[chunk.chunk_id],
        )
        for chunk in (odi_plan.chunks if odi_plan is not None else ())
    )
    records = source_records + oracle_records + odi_records
    with SQLiteKnowledgeIndex(paths["index"], create=not paths["index"].exists()) as index:
        generation = index.build_generation(
            records,
            project_id=str(project_id),
            source_revision=combined_revision,
            tree_digest=combined_tree_digest,
            source_manifest_digest=digest_of_bytes(combined_manifest),
            embedding_profile_digest=bound_plan.embedding_profile.profile_digest,
            provider_profile_digest=profile.profile_digest,
            created_at=dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        )
        integrity = index.integrity()

    manifest_root = paths["manifest_root"]
    _write_private(manifest_root / "repository.json", bound_plan.manifest)
    if bound_oracle_plan is not None:
        _write_private(manifest_root / "oracle.json", bound_oracle_plan.manifest)
    if odi_plan is not None:
        _write_private(manifest_root / "odi11g.json", odi_plan.manifest)
    _write_private(manifest_root / "combined.json", combined_manifest)
    projection = ProjectProjection.create(
        project_id=str(project_id),
        slug=project_slug,
        display_name="GPU Fusion" if project_slug == "gpu-fusion" else project_slug,
        status="active",
        source_bindings=(f"source:{project_slug}",),
        technologies=(),
        database_metadata=tuple(
            item
            for item in (
                f"knowledge-index/manifests/{project_slug}/oracle.json"
                if bound_oracle_plan
                else None,
                f"knowledge-index/manifests/{project_slug}/odi11g.json" if odi_plan else None,
            )
            if item is not None
        ),
        knowledge_scopes=(f"project:{project_id}",),
        last_source_snapshot=digest({"source_revision": combined_revision}),
    )
    projection_path = KnowledgeFileStore(paths["home"]).publish_project_projection(projection)
    result = _plan_document(discovery, bound_plan, bound_oracle_plan, odi_plan) | {
        "schema": "zekam-project-rag-index/v1",
        "status": "passed",
        "repository_source_revision": bound_plan.source_revision,
        "repository_tree_digest": bound_plan.tree_digest,
        "source_revision": combined_revision,
        "tree_digest": combined_tree_digest,
        "combined_manifest_digest": digest_of_bytes(combined_manifest),
        "generation_digest": generation.generation_digest,
        "provider_profile_digest": profile.profile_digest,
        "embedding_profile_digest": bound_plan.embedding_profile.profile_digest,
        "cache_hits": cache_hits,
        "newly_embedded_chunks": len(chunks) - cache_hits,
        "provider_batches": provider_batches,
        "probe": probe,
        "ledger": ledger.summary(),
        "index_integrity": integrity,
        "index_ref": f"knowledge-index/vector/opencode-bge-m3/{project_slug}/knowledge.sqlite3",
        "project_projection_ref": projection_path.relative_to(paths["home"]).as_posix(),
        "source_mutated": False,
        "row_data_included": False,
        "remote_provider_used": True,
        "provider_call_budget": provider_batches + 2,
        "secret_values_recorded": False,
        "odi_source_digest": odi_plan.source_digest if odi_plan else None,
        "odi_chunk_count": len(odi_plan.chunks) if odi_plan else 0,
        "odi_lineage_edge_count": len(odi_plan.lineage_edges) if odi_plan else 0,
        "odi_raw_xml_embedded": False,
    }
    if bound_oracle_plan is not None:
        result |= {
            "oracle_snapshot": bound_oracle_plan.snapshot.sanitized(),
            "oracle_effect": oracle_receipt,
        }
    _write_private(paths["state"], (canonical_json(result) + "\n").encode("utf-8"))
    return result


def _query(
    source_root: Path,
    home: Path,
    project_id: UUID,
    project_slug: str,
    config_file: Path,
    query: str,
) -> dict[str, Any]:
    paths = _runtime_paths(home, project_slug)
    if not paths["state"].is_file() or not paths["index"].is_file():
        raise ValidationFailed("Project RAG index bulunamadi")
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    if state.get("project_id") != str(project_id):
        raise PolicyViolation("Project RAG state project binding drift")
    current_revision = _git_source_state(source_root)[2]
    current_tree = discover(source_root).tree_digest
    if (
        state.get("repository_source_revision") != current_revision
        or state.get("repository_tree_digest") != current_tree
    ):
        raise PolicyViolation("Project source degisti; yeniden index gerekli")
    odi_binding = load_smart_binding(home, project_slug)
    current_odi_digest = odi_binding.get("source_digest") if odi_binding else None
    if state.get("odi_source_digest") != current_odi_digest:
        raise PolicyViolation("ODI Smart Export binding degisti; yeniden index gerekli")
    provider, policy, _, probe = _provider(paths["ledger"], config_file, project_id)
    with SQLiteKnowledgeIndex(paths["index"], read_only=True) as index:
        result = EmbeddedProjectRAG(index, provider, policy).query(
            query,
            project_id=str(project_id),
            expected_source_revision=str(state["source_revision"]),
            expected_tree_digest=str(state["tree_digest"]),
        )
    return result | {
        "probe_evidence_digest": probe["probe_evidence_digest"],
        "database_freshness": (
            "disabled" if state.get("database_access") == "disabled" else "last-indexed-snapshot"
        ),
        "source_access": "read-only",
        "row_data_included": False,
        "secret_values_recorded": False,
    }


def query_registered_project(
    home: Path,
    project_slug: str,
    question: str,
    *,
    opencode_config: Path | None = None,
) -> dict[str, Any]:
    """Query a registered project through its validated local binding and active index."""

    slug = validate_slug(project_slug)
    root = resolve_project_source(home, slug)
    project_id = _registered_project(home, slug)
    config_file = (opencode_config or default_opencode_config_file()).resolve(strict=True)
    return _query(root, home.resolve(strict=True), project_id, slug, config_file, question)


def index_registered_project(
    home: Path,
    project_slug: str,
    *,
    oracle_config: str | None = None,
    opencode_config: Path | None = None,
    batch_size: int = MAX_BATCH_SIZE,
    authorize_odi_metadata: bool = False,
) -> dict[str, Any]:
    """Refresh code and Oracle metadata for a registered, locally bound project."""

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValidationFailed("Batch size 1..64 araliginda olmali")
    slug = validate_slug(project_slug)
    root = resolve_project_source(home, slug)
    project_id = _registered_project(home, slug)
    config_file = (opencode_config or default_opencode_config_file()).resolve(strict=True)
    return _index(
        root,
        home.resolve(strict=True),
        project_id,
        slug,
        config_file,
        oracle_config,
        batch_size=batch_size,
        authorize_odi_metadata=authorize_odi_metadata,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "index", "query"):
        command = subcommands.add_parser(name)
        command.add_argument("--project", type=Path, required=True)
        command.add_argument("--home", type=Path, required=True)
        command.add_argument("--slug")
        if name == "plan":
            command.add_argument("--oracle-config")
            command.add_argument("--authorize-database-metadata", action="store_true")
        if name in {"index", "query"}:
            command.add_argument(
                "--opencode-config", type=Path, default=default_opencode_config_file()
            )
        if name == "index":
            command.add_argument("--oracle-config")
            command.add_argument("--authorize-remote-source", action="store_true")
            command.add_argument("--authorize-database-metadata", action="store_true")
            command.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE)
        if name == "query":
            command.add_argument("--authorize-remote-query", action="store_true")
            command.add_argument("--question", required=True)
    args = parser.parse_args()
    source_root = args.project.resolve(strict=True)
    home = args.home.resolve(strict=True)
    project_slug = validate_slug(args.slug or source_root.name.casefold())
    project_id = _registered_project(home, project_slug)
    if args.command == "plan":
        discovery, plan = _project_plan(
            source_root, project_id=project_id, project_slug=project_slug
        )
        oracle_plan = None
        oracle_effect = None
        if args.oracle_config is not None:
            if not args.authorize_database_metadata:
                raise PolicyViolation("Database metadata plan explicit authorization ister")
            datasource = load_project_oracle_datasource(source_root, args.oracle_config)
            snapshot, oracle_effect = _collect_oracle_with_receipt(home, project_id, datasource)
            oracle_plan = build_oracle_metadata_index_plan(
                project_id=project_id,
                project_slug=project_slug,
                snapshot=snapshot,
            )
        result = _plan_document(discovery, plan, oracle_plan)
        if oracle_plan is not None:
            result |= {
                "database_access": "metadata-only-completed",
                "oracle_snapshot": oracle_plan.snapshot.sanitized(),
                "oracle_effect": oracle_effect,
                "remote_provider_used": True,
                "provider_call_budget": result["estimated_provider_calls"] + 2,
            }
    elif args.command == "index":
        if not args.authorize_remote_source:
            raise PolicyViolation("Remote source disclosure explicit authorization ister")
        if args.oracle_config is not None and not args.authorize_database_metadata:
            raise PolicyViolation("Database metadata disclosure explicit authorization ister")
        if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
            raise ValidationFailed("Batch size 1..64 araliginda olmali")
        result = _index(
            source_root,
            home,
            project_id,
            project_slug,
            args.opencode_config.resolve(strict=True),
            args.oracle_config,
            batch_size=args.batch_size,
        )
    else:
        if not args.authorize_remote_query:
            raise PolicyViolation("Remote query embedding explicit authorization ister")
        result = _query(
            source_root,
            home,
            project_id,
            project_slug,
            args.opencode_config.resolve(strict=True),
            args.question,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
