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
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from zekam.application.config import EmbeddingRoute, KnowledgeSettings, load_settings
from zekam.application.embedded_project_rag import EmbeddedProjectRAG
from zekam.application.embedding_provider import (
    EmbeddingBatch,
    EmbeddingPolicy,
    EmbeddingProbeFixture,
    EmbeddingProfile,
    EmbeddingProvider,
)
from zekam.application.embedding_routing import EmbeddingRouteCandidate, EmbeddingRouteKind
from zekam.application.home import HomeLayout
from zekam.application.knowledge_file_plane import ProjectProjection
from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.application.local_embedding_composition import build_verified_mac_embedding
from zekam.application.model_health_service import ProbeUnavailable
from zekam.application.model_registry import load_inventory
from zekam.application.odi11g_smart_export import (
    OdiSanitizedPlan,
    build_sanitized_odi_plan,
    load_smart_binding,
    smart_binding_source_current,
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
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed, ZekamError
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
from zekam.infrastructure.embedding.infinity_bge import (
    build_local_bge_provider,
    default_mac_bge_configuration,
)
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
from zekam.infrastructure.query_measurement import (
    record_probe_latency,
    record_qualification,
    record_source_freshness_scan,
)
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

MODEL_ID = "openai/BAAI/bge-m3"
PROVIDER_ID = "litellm"
VECTOR_DIMENSION = 1024

# ----------------------------------------------------------------------
# WP2 (B01): bounded provider qualification cache.
#
# A warm, already-accepted provider identity must NOT re-run the probe on a
# second authorized query.  The cache key binds the full acceptance identity:
# provider/endpoint identity, exact model id, reported model revision, dimension,
# dtype/normalization, preprocessing/prefix/tokenizer contract, fixture version
# and the relevant configuration/revision digest.  NO secret value is stored.
#
# Durability: the task requires warm behaviour across *separate CLI processes*.
# This step stores the qualification record in the project's own provider-ledger
# SQLite file (already a safe local state), so a later CLI process can reuse a
# still-fresh acceptance.  An in-process LRU is layered on top to avoid re-reading
# the ledger within one process.  (See B01 report: cross-process durability is
# achieved via the ledger; direct file-level reuse is bounded by TTL + binding.)
#
# Single-flight: concurrent qualification for the same binding collapses into one
# probe; waiters are bounded by the configured deadline.
#
# Invalidation: config/revision/policy change, TTL expiry, a differing binding
# digest, or a corrupt/incompatible record re-validates with a bounded budget --
# never unlimited wait and never silent auto-authorization.  Failure cache is
# short, bounded, never hides auth errors, and avoids probe storms.
# ----------------------------------------------------------------------
QUALIFICATION_TTL_SECONDS = 5 * 60  # start-of-attempt; NOT a guaranteed value
_QUALIFICATION_FAILURE_TTL_SECONDS = 30

_qualification_lock = threading.Lock()
# in-process identity -> _QualificationRecord (or failure marker); bounded LRU.
_qualification_inprocess: dict[str, object] = {}
_QUALIFICATION_INPROCESS_MAX = 32
_qualification_singleflight: dict[str, threading.Event] = {}


@dataclass(frozen=True, slots=True)
class _QualificationRecord:
    """Secret-free durable qualification evidence for one provider binding."""

    binding_digest: str
    qualified_at_ns: int
    expires_at_ns: int
    ttl_seconds: int
    profile_digest: str
    probe_evidence_digest: str
    semantic_margin: float
    max_repeat_delta: float
    max_batch_delta: float
    latency_ms: int
    provider_call_count: int
    model_revision_fingerprint: str
    provider_identity_digest: str
    exact_model_id: str


@dataclass(frozen=True, slots=True)
class _QualificationFailure:
    """Short-lived failure guard; never hides auth/authorization errors."""

    binding_digest: str
    recorded_at_ns: int
    error_type: str


def _qualification_key(
    configuration: object,
    knowledge: KnowledgeSettings,
    *,
    probe_revision: str,
) -> str:
    """Build the exact secret-free acceptance binding digest for the cache.

    Binds provider/endpoint identity, exact model id, dimension, distance,
    normalization/dtype, preprocessing/prefix/tokenizer contract, fixture
    version and the configuration revision.  No secret value enters the digest.
    """
    config_identity = getattr(configuration, "endpoint_identity", None)
    endpoint_digest = (
        getattr(config_identity, "identity_digest", None)
        if config_identity is not None
        else None
    )
    provider_id = getattr(configuration, "provider_id", None)
    selected_model = getattr(configuration, "selected_model_id", None)
    canonical_model = getattr(configuration, "canonical_model_id", None)
    body = {
        "schema": "zekam-project-rag-qualification-binding/v1",
        "provider_id": provider_id,
        "endpoint_identity_digest": endpoint_digest,
        "exact_model_id": selected_model,
        "canonical_model_id": canonical_model,
        "embedding_model_ref": knowledge.embedding_model_ref,
        "embedding_dimension": knowledge.embedding_dimension,
        "embedding_distance": knowledge.embedding_distance,
        "vector_dtype": "float32",
        "normalized": True,
        "preprocessing_prefix": "none",
        "tokenizer_contract": "provider-managed",
        "probe_fixture_version": "v1",
        "probe_revision": probe_revision,
    }
    return str(digest(body))


def _probe_revision(knowledge: KnowledgeSettings) -> str:
    """Version the probe fixture/contract so a fixture change invalidates easily."""
    return str(knowledge.embedding_profile_id)


def _now_ns() -> int:
    return time.monotonic_ns()


def _qualification_ledger_store() -> tuple[type, str]:
    """Return the (already-imported) ledger host/factory contract."""
    return SQLiteProviderLedgerHost, "provider-ledger"


def _read_durable_qualification(
    ledger_path: Path, binding_digest: str, *, now_ns: int
) -> _QualificationRecord | None:
    """Best-effort read of a still-fresh qualification from the durable ledger.

    Uses only secret-free identity columns.  A corrupt/missing record returns
    None so the caller re-validates with a bounded budget.  Never auto-approves.
    """
    try:
        if not ledger_path.is_file() or not private_regular(ledger_path):
            return None
        import sqlite3 as _s3

        connection = _s3.connect(f"{ledger_path.as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            connection.row_factory = _s3.Row
            row = connection.execute(
                "select binding_digest,qualified_at_ns,expires_at_ns,ttl_seconds,"
                " profile_digest,probe_evidence_digest,semantic_margin,max_repeat_delta,"
                " max_batch_delta,latency_ms,provider_call_count,model_revision_fingerprint,"
                " provider_identity_digest,exact_model_id"
                " from project_rag_qualification where binding_digest=?",
                (binding_digest,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        expires = int(row["expires_at_ns"])
        if expires <= now_ns:
            return None
        return _QualificationRecord(
            binding_digest=str(row["binding_digest"]),
            qualified_at_ns=int(row["qualified_at_ns"]),
            expires_at_ns=expires,
            ttl_seconds=int(row["ttl_seconds"]),
            profile_digest=str(row["profile_digest"]),
            probe_evidence_digest=str(row["probe_evidence_digest"]),
            semantic_margin=float(row["semantic_margin"]),
            max_repeat_delta=float(row["max_repeat_delta"]),
            max_batch_delta=float(row["max_batch_delta"]),
            latency_ms=int(row["latency_ms"]),
            provider_call_count=int(row["provider_call_count"]),
            model_revision_fingerprint=str(row["model_revision_fingerprint"]),
            provider_identity_digest=str(row["provider_identity_digest"]),
            exact_model_id=str(row["exact_model_id"]),
        )
    except Exception:
        # Corrupt/unreadable ledger: never treat as valid; force bounded re-probe.
        return None


def _write_durable_qualification(ledger_path: Path, record: _QualificationRecord) -> None:
    """Persist a fresh acceptance to the project ledger (best-effort)."""
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3 as _s3

        connection = _s3.connect(ledger_path, timeout=5)
        try:
            connection.execute(
                "create table if not exists project_rag_qualification("
                " binding_digest text primary key,"
                " qualified_at_ns integer not null,"
                " expires_at_ns integer not null,"
                " ttl_seconds integer not null,"
                " profile_digest text not null,"
                " probe_evidence_digest text not null,"
                " semantic_margin real not null,"
                " max_repeat_delta real not null,"
                " max_batch_delta real not null,"
                " latency_ms integer not null,"
                " provider_call_count integer not null,"
                " model_revision_fingerprint text not null,"
                " provider_identity_digest text not null,"
                " exact_model_id text not null"
                ") strict"
            )
            connection.execute(
                "insert or replace into project_rag_qualification values("
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.binding_digest,
                    record.qualified_at_ns,
                    record.expires_at_ns,
                    record.ttl_seconds,
                    record.profile_digest,
                    record.probe_evidence_digest,
                    record.semantic_margin,
                    record.max_repeat_delta,
                    record.max_batch_delta,
                    record.latency_ms,
                    record.provider_call_count,
                    record.model_revision_fingerprint,
                    record.provider_identity_digest,
                    record.exact_model_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
    except Exception:
        # Persistence is best-effort; an in-process record still holds for this run.
        return


def _inprocess_qualification(
    binding_digest: str, *, now_ns: int
) -> _QualificationRecord | _QualificationFailure | None:
    with _qualification_lock:
        entry = _qualification_inprocess.get(binding_digest)
        if isinstance(entry, _QualificationRecord):
            if entry.expires_at_ns > now_ns:
                return entry
            _qualification_inprocess.pop(binding_digest, None)
        elif isinstance(entry, _QualificationFailure):
            if now_ns - entry.recorded_at_ns < _QUALIFICATION_FAILURE_TTL_SECONDS * 1_000_000_000:
                return entry
            _qualification_inprocess.pop(binding_digest, None)
        return None


def _cache_inprocess_qualification(binding_digest: str, entry: object) -> None:
    with _qualification_lock:
        if len(_qualification_inprocess) >= _QUALIFICATION_INPROCESS_MAX:
            _qualification_inprocess.pop(next(iter(_qualification_inprocess)), None)
        _qualification_inprocess[binding_digest] = entry


def _should_revalidate_from_failure(failure: _QualificationFailure, *, now_ns: int) -> bool:
    return now_ns - failure.recorded_at_ns >= _QUALIFICATION_FAILURE_TTL_SECONDS * 1_000_000_000


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


def _directory_source_state(root: Path) -> tuple[str, str, str]:
    discovery = discover(root)
    if discovery.truncated:
        raise ValidationFailed("Project directory source snapshot truncated")
    identity = discovery.tree_digest.removeprefix("sha256:")
    return "", identity, f"directory:{discovery.tree_digest}"


def _has_git_marker(root: Path) -> bool:
    """Detect Git metadata without following a possibly broken link."""

    try:
        os.lstat(root / ".git")
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValidationFailed("Project Git source marker denetlenemedi") from exc
    return True


def classify_project_source(root: Path) -> str:
    """Classify a project root from its lexical Git marker."""

    return "git" if _has_git_marker(root) else "directory"


def _git_source_state(root: Path) -> tuple[str, str, str]:
    """Return a stable source identity for Git roots and ordinary directories.

    Project registration explicitly accepts both source kinds.  Git repositories
    require a committed HEAD, while ordinary directories use the same bounded,
    secret-filtered discovery digest as the index plan.
    """

    try:
        repository = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        repository = None
    if repository is None or repository.returncode != 0:
        if _has_git_marker(root):
            raise ValidationFailed("Project Git source identity probe basarisiz")
        return _directory_source_state(root)

    try:
        repository_root = Path(repository.stdout.strip()).resolve(strict=True)
    except OSError:
        raise ValidationFailed("Project Git source root cozumlenemedi") from None
    if repository_root != root.resolve(strict=True):
        return _directory_source_state(root)
    try:
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="ascii",
            errors="replace",
            timeout=10,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValidationFailed("Project Git source identity probe basarisiz") from None
    if head_result.returncode != 0:
        raise ValidationFailed("Project Git source committed HEAD ister")
    if status_result.returncode != 0:
        raise ValidationFailed("Project Git source status okunamadi")
    head = head_result.stdout.strip()
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise ValidationFailed("Project Git source HEAD kimligi gecersiz")
    status_digest = hashlib.sha256(status_result.stdout).hexdigest()
    return head, status_digest, f"{head}:status:{status_digest}"


def _parse_git_modified_paths(status_output: bytes) -> tuple[tuple[str, str], ...]:
    """Extract (path, change-kind) pairs from ``git status --porcelain=v1 -z`` bytes.

    Git ``-z`` output uses NUL-delimited records ``XY <path>`` for rename/copy
    records (``X<space>Y<space>old<space>new``) and ``XY path`` otherwise.  We only
    care about *which* working-tree paths changed and whether the change is a
    deletion, so a bounded, order-preserving parse suffices.  Malformed records are
    ignored conservatively (an unparsed dirty path cannot make a false "current").
    """
    records: list[tuple[str, str]] = []
    for raw in status_output.split(b"\x00"):
        if not raw:
            continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        x, y = text[:1], text[1:2]
        rest = text[2:]
        if rest.startswith(" "):
            rest = rest[1:]
        path = rest.strip()
        if not path:
            continue
        if "\n" in path:
            path = path.split("\n", 1)[0]
        change = "deleted" if (x + y) in {"D ", " D", "DD", "AD"} else "changed"
        records.append((path, change))
    return tuple(records)


def _git_source_freshness_signal(
    root: Path,
    *,
    indexed_digests: dict[str, str],
) -> dict[str, Any]:
    """Bounded, content-aware source freshness for the query path (WP3-B).

    This replaces the per-query ``discover().tree_digest`` full re-scan.  For a Git
    root it reuses HEAD + ``git status --porcelain`` to learn *which* indexed paths
    changed, then hashes the on-disk content of exactly those changed, still-present
    files.  The returned dict carries ``head`` and, for every changed tracked/untracked
    file that belongs to the indexed plan, the current content digest together with the
    indexed content digest.  Callers compare them so two states sharing the same
    ``git status`` text but different file content are NOT treated as identical.

    Return value keys:
    * ``head``                      current Git HEAD (when readable).
    * ``changed_content_digests``   {relative_path: current_content_digest} for changed
                                     indexed, present, non-symlink files.
    * ``indexed_content_digests``   the same paths mapped to their indexed content digest.
    * ``changed_paths``             the sub-list of changed indexed paths.
    * ``clean``                     True when no indexed path changed (git status shows
                                     none of the indexed paths dirty).
    * ``unknown``                   True when we could NOT bound the signal and must NOT
                                     claim "current" (watcher/journal gap, deleted /
                                     permission-changed / symlink / junction source, or an
                                     unbounded dirty set).
    """
    record_source_freshness_scan()
    try:
        head, _, _ = _git_source_state(root)
    except (OSError, subprocess.SubprocessError, ZekamError):
        return {"unknown": True}
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {"unknown": True}
    if status.returncode != 0:
        return {"unknown": True}
    changed = _parse_git_modified_paths(status.stdout)
    if len(changed) > _MAX_DIRTY_FRESHNESS_PATHS:
        return {"head": head, "unknown": True}
    rooted = root.resolve(strict=True)
    current_digests: dict[str, str] = {}
    changed_paths: list[str] = []
    for relative, _kind in sorted(changed):
        if relative not in indexed_digests:
            continue
        changed_paths.append(relative)
        candidate = rooted.joinpath(*relative.split("/")) if relative else rooted
        try:
            resolved = candidate.resolve(strict=True)
            if resolved != candidate or candidate.is_symlink():
                return {"head": head, "unknown": True}
            candidate.relative_to(rooted)
        except (OSError, ValueError):
            return {"head": head, "unknown": True}
        try:
            payload = candidate.read_bytes()
        except OSError:
            return {"head": head, "unknown": True}
        current_digests[relative] = digest_of_bytes(payload)
    clean = not changed_paths
    return {
        "head": head,
        "clean": clean,
        "changed_paths": tuple(changed_paths),
        "changed_content_digests": current_digests,
    }


def _indexed_source_paths(state: dict[str, Any]) -> frozenset[str]:
    """Return the set of indexed source relative paths recorded at index time."""
    paths = frozenset(_indexed_source_digests(state))
    return paths


def _indexed_source_digests(state: dict[str, Any]) -> dict[str, str]:
    """Return {relative_path: content_digest} captured for the indexed plan."""
    manifest = state.get("source_files")
    if isinstance(manifest, list):
        digests: dict[str, str] = {}
        for item in manifest:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            content = item.get("content_digest")
            if isinstance(path, str) and isinstance(content, str) and path:
                digests[path] = content
        if digests:
            return digests
    # No per-file record: freshness can only use a cheap HEAD+status comparison and
    # can never claim per-file content equality.
    return {}


def _source_freshness_for_query(
    state: dict[str, Any],
    source_root: Path | None,
) -> tuple[list[str], dict[str, Any]]:
    """Evaluate query-path source freshness with explicit states (WP3-B).

    Returns ``(stale_reasons, observations)``.  Content/unknown findings add stale
    reasons so ``index_freshness`` is not declared current.  No TTL or size/mtime
    equality is ever presented as cryptographic content equality.

    For atomic Git roots the per-query full ``discover().tree_digest`` re-scan is
    replaced by a bounded HEAD + ``git status`` content-aware check (see
    ``_git_source_freshness_signal``).  For ordinary directory roots there is no
    journal, so the recorded content-based ``directory:`` revision is compared to
    the current one; a mismatch is a content change and never a silent "current".
    """
    if source_root is None:
        return ["project-source-binding-unavailable"], {"source_freshness": "unavailable"}
    try:
        kind = classify_project_source(source_root)
    except ZekamError:
        return ["project-source-freshness-unknown"], {"source_freshness": "unknown"}
    if kind == "git":
        indexed_digests = _indexed_source_digests(state)
        try:
            observations = _git_source_freshness_signal(
                source_root, indexed_digests=indexed_digests
            )
        except (OSError, subprocess.SubprocessError, ZekamError):
            observations = {"unknown": True}
        reasons: list[str] = []
        if observations.get("unknown"):
            reasons.append("project-source-freshness-unknown")
        else:
            declared_revision = state.get("repository_source_revision")
            current_head = observations.get("head")
            if current_head and declared_revision:
                declared_head = str(declared_revision).split(":")[0]
                if current_head != declared_head:
                    reasons.append("project-source-revision-stale")
            # Content-aware: a changed indexed file whose current digest differs
            # from what was indexed is a content change even if the git status
            # text is the same (the project-source-content-stale reason).
            for path, current_digest in observations.get(
                "changed_content_digests", {}
            ).items():
                indexed_digest = indexed_digests.get(path)
                if indexed_digest is not None and current_digest != indexed_digest:
                    reasons.append("project-source-content-stale")
                    break
        return reasons, observations
    return _directory_freshness_for_query(state, source_root)


def _directory_freshness_for_query(
    state: dict[str, Any],
    source_root: Path,
) -> tuple[list[str], dict[str, Any]]:
    """Freshness for ordinary (non-Git) directory roots.

    A directory source has no commit/status journal, so the only content-correct
    signal is the recorded ``directory:<tree_digest>`` revision vs the current
    one.  ``_git_source_state`` on a non-git root returns this digests directly
    (based on a full tree digest), which is content-based -- never TTL/size/mtime.
    A mismatch is reported as stale.
    """
    record_source_freshness_scan()
    try:
        _, status_digest, current_revision = _git_source_state(source_root)
    except (OSError, subprocess.SubprocessError, ZekamError):
        return ["project-source-freshness-unknown"], {"source_freshness": "unknown"}
    declared_revision = state.get("repository_source_revision")
    declared_tree = state.get("repository_tree_digest")
    reasons: list[str] = []
    if declared_revision != current_revision:
        reasons.append("project-source-revision-stale")
    # For a directory root ``head`` is empty and the status digest carries the
    # content-based tree digest; compare it against the recorded tree so a tree
    # change is reported even when head is an empty string.
    if status_digest:
        current_tree = (
            f"sha256:{status_digest}"
            if not status_digest.startswith("sha256:")
            else status_digest
        )
        if declared_tree and declared_tree != current_tree:
            reasons.append("project-source-tree-stale")
    return reasons, {"directory_revision": current_revision}


_MAX_DIRTY_FRESHNESS_PATHS = 512


_LOCAL_QUALIFICATION_FIXTURE = EmbeddingProbeFixture(
    query="Which component validates a project source revision?",
    positive_passage="The project index validates the source revision and tree digest.",
    negative_passage="A recipe explains how to bake a chocolate cake.",
    source_refs=("synthetic:project-index", "synthetic:recipe"),
    source_digests=(digest("project-index"), digest("recipe")),
    classification=DataClassification.PUBLIC,
)


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
    root: Path,
    *,
    project_id: UUID,
    project_slug: str,
    knowledge: KnowledgeSettings | None = None,
    embedding_candidates: tuple[EmbeddingRouteCandidate, ...] = (),
    allow_remote_source: bool = False,
) -> tuple[DiscoveryReport, ProjectIndexPlan]:
    _, _, revision = _git_source_state(root)
    discovery = discover(root)
    plan = build_project_index_plan(
        project_id=project_id,
        project_slug=project_slug,
        source_root=root,
        source_revision=revision,
        expected_tree_digest=discovery.tree_digest,
        embedding_candidates=embedding_candidates,
        allow_remote_source=allow_remote_source,
        local_model_ref=(knowledge.embedding_model_ref if knowledge is not None else MODEL_ID),
        local_dimension=(
            knowledge.embedding_dimension if knowledge is not None else VECTOR_DIMENSION
        ),
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
        "model_id": plan.embedding_profile.model_ref,
        "dimension": plan.embedding_profile.dimension,
        "embedding_route": plan.embedding_route.sanitized(),
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
        if not private_directory(directory):
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


def _existing_runtime_paths(home: Path, project_slug: str) -> dict[str, Path]:
    """Resolve existing RAG paths without mutating ACLs or creating directories."""

    root = home.resolve(strict=True)
    layout = HomeLayout(root)
    issues = layout.verify()
    if issues:
        raise PolicyViolation(f"ZEKAM_HOME layout gecersiz: {issues[0].kind}")
    # Keep the lexical path so the scoped validator can detect junction/symlink aliases.
    project_root = root / "projeler" / project_slug
    index_root = root / "knowledge-index" / "vector" / "opencode-bge-m3" / project_slug
    manifest_root = root / "knowledge-index" / "manifests" / project_slug
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


def _scoped_path_is_private(
    root: Path, path: Path, *, directory: bool
) -> bool:
    """Reject missing, escaped, symlinked or reparse-backed scoped RAG paths."""

    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return False
    if resolved != path:
        return False
    return private_directory(path) if directory else private_regular(path)


def _rag_scope_is_private(paths: dict[str, Path]) -> bool:
    root = paths["home"]
    return all(
        _scoped_path_is_private(root, paths[name], directory=True)
        for name in ("project_root", "index_root", "manifest_root")
    ) and all(
        _scoped_path_is_private(root, paths[name], directory=False)
        for name in ("state", "index")
    )


@dataclass(frozen=True, slots=True)
class _EmbeddingBinding:
    provider: EmbeddingProvider
    policy: EmbeddingPolicy
    ledger: dict[str, Any]
    probe: dict[str, Any]
    route: EmbeddingRoute
    remote_provider_used: bool
    probe_call_count: int


@dataclass(frozen=True, slots=True)
class _LocalQueryBinding:
    """Typed local-route query binding (provider, profile, policy)."""

    provider: EmbeddingProvider
    profile: EmbeddingProfile
    policy: EmbeddingPolicy


class _DenseDisabledProvider:
    """Sentinel provider for exact/lexical-only reads; every call is a defect."""


def _lexical_only_binding(
    provider_profile_digest: str,
    route: EmbeddingRoute,
    reason: str,
) -> _EmbeddingBinding:
    evidence = digest(
        {
            "schema": "zekam-project-query-dense-disabled/v1",
            "provider_profile_digest": provider_profile_digest,
            "route": route.value,
            "reason": reason,
        }
    )
    return _EmbeddingBinding(
        provider=cast(EmbeddingProvider, _DenseDisabledProvider()),
        policy=EmbeddingPolicy(DataClassification.INTERNAL, provider_profile_digest),
        ledger={
            "schema": "zekam-local-provider-ledger-summary/v1",
            "provider_calls": 0,
            "durable_remote_effects": 0,
        },
        probe={
            "profile_digest": provider_profile_digest,
            "probe_evidence_digest": evidence,
            "route": route.value,
            "degraded_reason": reason,
        },
        route=route,
        remote_provider_used=False,
        probe_call_count=0,
    )


def _local_query_binding(
    home: Path,
    ledger_path: Path,
    project_id: UUID,
    knowledge: KnowledgeSettings,
) -> _EmbeddingBinding:
    """Local-route query binding WITHOUT full corpus plan chunking (WP3-A).

    Previously the query path called ``_project_plan`` to build the whole corpus
    plan/chunks and fed them into ``build_verified_mac_embedding`` for
    qualification.  WP3 removes that: the accepted, bounded synthetic fixture
    identity (same contract used by the remote canonical probe) is reused, so a
    normal answer query does no corpus chunking / document embedding / indexing.
    The local provider is rebuilt per query (no remote/external call; the Mac BGE
    model runs loopback), and its fingerprint must still match the canonical
    dimension so qualification stays exact.
    """
    if knowledge.embedding_route is not EmbeddingRoute.LOCAL:
        raise ConfigurationError("Local query binding route drift")
    _assert_canonical_knowledge_profile(knowledge)
    if _runtime_platform() != "darwin":
        raise PolicyViolation("Local BGE embedding route yalniz macOS cihazinda desteklenir")
    record_qualification()
    del home, ledger_path, project_id
    local = _build_local_query_embedding()
    accepted_model_refs = {
        local.profile.exact_model_id,
        f"openai/{local.profile.exact_model_id}",
    }
    if (
        knowledge.embedding_model_ref not in accepted_model_refs
        or local.profile.dimension != knowledge.embedding_dimension
    ):
        raise ConfigurationError("Local provider canonical embedding profile drift")
    return _EmbeddingBinding(
        provider=local.provider,
        policy=local.policy,
        ledger={
            "schema": "zekam-local-provider-ledger-summary/v1",
            "provider_calls": 0,
            "durable_remote_effects": 0,
        },
        probe={
            "profile_digest": local.profile.profile_digest,
            "probe_evidence_digest": local.profile.probe_evidence_digest,
            "semantic_margin": "verified",
            "route": "local",
            "query_path": "fixture",
        },
        route=knowledge.embedding_route,
        remote_provider_used=False,
        probe_call_count=2,
    )


def _build_local_query_embedding() -> _LocalQueryBinding:
    """Build and verify the local BGE binding from the bounded synthetic fixture.

    Uses :data:`_LOCAL_QUALIFICATION_FIXTURE` (a fixed, project-independent source
    probe pair) so the query path never needs real project chunks.  Mirrors the
    verified local binding shape (provider, profile, policy).
    """
    provider = build_local_bge_provider(default_mac_bge_configuration())
    result = provider.probe(_LOCAL_QUALIFICATION_FIXTURE)
    policy = EmbeddingPolicy(DataClassification.LOCAL_ONLY, result.profile.profile_digest)
    if not provider.health().healthy:
        raise ValidationFailed("Verified local embedding provider health gecemedi")

    return _LocalQueryBinding(
        provider=provider,
        profile=result.profile,
        policy=policy,
    )


def project_embedding_route(home: Path) -> EmbeddingRoute:
    """Resolve the explicit, secret-free project embedding route."""

    return load_settings(home=home.resolve(strict=True)).knowledge.embedding_route


def _assert_canonical_knowledge_profile(knowledge: KnowledgeSettings) -> None:
    if (
        knowledge.embedding_profile_id != "bge-m3-dense-v1"
        or knowledge.embedding_dimension != VECTOR_DIMENSION
        or knowledge.embedding_distance != "cosine"
    ):
        raise ConfigurationError("Project RAG canonical embedding profili desteklenmiyor")


def _knowledge_binding(knowledge: KnowledgeSettings) -> dict[str, object]:
    """Return the exact secret-free config identity that owns an index."""

    return {
        "embedding_profile_id": knowledge.embedding_profile_id,
        "embedding_route": knowledge.embedding_route.value,
        "embedding_model_ref": knowledge.embedding_model_ref,
        "embedding_dimension": knowledge.embedding_dimension,
        "embedding_distance": knowledge.embedding_distance,
        "remote_provider_id": (
            knowledge.remote_provider_id
            if knowledge.embedding_route is EmbeddingRoute.REMOTE
            else None
        ),
    }


def _knowledge_binding_digest(knowledge: KnowledgeSettings) -> str:
    return str(digest(_knowledge_binding(knowledge)))


def _runtime_platform() -> str:
    """Keep platform policy runtime-evaluated for cross-OS package builds."""

    return str(sys.platform)


def _provider(
    home: Path,
    ledger_path: Path,
    config_file: Path | None,
    project_id: UUID,
    chunks: tuple[Chunk, ...],
    knowledge: KnowledgeSettings,
    *,
    remote_authorized: bool,
) -> _EmbeddingBinding:
    _assert_canonical_knowledge_profile(knowledge)
    if knowledge.embedding_route is EmbeddingRoute.LOCAL:
        if _runtime_platform() != "darwin":
            raise PolicyViolation("Local BGE embedding route yalniz macOS cihazinda desteklenir")
        record_qualification()
        local = build_verified_mac_embedding(chunks)
        accepted_model_refs = {
            local.profile.exact_model_id,
            f"openai/{local.profile.exact_model_id}",
        }
        if (
            knowledge.embedding_model_ref not in accepted_model_refs
            or local.profile.dimension != knowledge.embedding_dimension
        ):
            raise ConfigurationError("Local provider canonical embedding profile drift")
        return _EmbeddingBinding(
            provider=local.provider,
            policy=local.policy,
            ledger={
                "schema": "zekam-local-provider-ledger-summary/v1",
                "provider_calls": 0,
                "durable_remote_effects": 0,
            },
            probe={
                "profile_digest": local.profile.profile_digest,
                "probe_evidence_digest": local.profile.probe_evidence_digest,
                "semantic_margin": "verified",
                "route": "local",
            },
            route=knowledge.embedding_route,
            remote_provider_used=False,
            probe_call_count=2,
        )
    if not remote_authorized:
        raise PolicyViolation("Remote embedding explicit authorization ister")
    if config_file is None:
        raise ConfigurationError("Remote embedding OpenCode config ister")
    configuration = load_opencode_embedding_configuration(
        config_file,
        provider_id=knowledge.remote_provider_id,
        selected_model_id=knowledge.embedding_model_ref,
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

    fixture = EmbeddingProbeFixture(
        query="Which component validates a project source revision?",
        positive_passage="The project index validates the source revision and tree digest.",
        negative_passage="A recipe explains how to bake a chocolate cake.",
        source_refs=("synthetic:project-index", "synthetic:recipe"),
        source_digests=(digest("project-index"), digest("recipe")),
        classification=DataClassification.PUBLIC,
    )

    provider = OpenCodeRemoteEmbeddingProvider(
        configuration,
        RuntimeOpenCodeEmbeddingExecutor(invocation),
        dimension=knowledge.embedding_dimension,
        max_batch_size=MAX_BATCH_SIZE,
    )
    # ----- WP2 (B01): bounded qualification cache -------------------------
    # A warm, already-accepted provider identity must not re-run the probe.  The
    # binding digest covers provider/endpoint identity, model, dimension, dtype/
    # normalization, preprocessing/prefix/tokenizer contract, fixture version and
    # the config revision; a config/revision/policy change invalidates it
    # independently of TTL.
    binding_digest = _qualification_key(
        configuration,
        knowledge,
        probe_revision=_probe_revision(knowledge),
    )
    now_ns = _now_ns()
    cached: _QualificationRecord | _QualificationFailure | None = _inprocess_qualification(
        binding_digest, now_ns=now_ns
    )
    if cached is None:
        # Cross-process reuse: fall back to the durable project ledger.  A still
        # -fresh, binding-matching record lets a separate CLI process reuse an
        # acceptance without a new probe.
        cached = _read_durable_qualification(ledger_path, binding_digest, now_ns=now_ns)
    if isinstance(cached, _QualificationRecord):
        # Reconstruct the binding from the accepted, still-fresh evidence.  No new
        # probe and no new authorization: the acceptance is already proven.
        policy = EmbeddingPolicy(
            DataClassification.INTERNAL,
            cached.profile_digest,
            remote_disclosure_authorized=True,
        )
        return _EmbeddingBinding(
            provider=provider,
            policy=policy,
            ledger=host.summary(),
            probe={
                "profile_digest": cached.profile_digest,
                "probe_evidence_digest": cached.probe_evidence_digest,
                "semantic_margin": cached.semantic_margin,
                "max_repeat_delta": cached.max_repeat_delta,
                "max_batch_delta": cached.max_batch_delta,
                "latency_ms": cached.latency_ms,
                "route": "remote",
                "qualified_from_cache": True,
            },
            route=knowledge.embedding_route,
            remote_provider_used=True,
            probe_call_count=0,
        )
    if isinstance(cached, _QualificationFailure) and not _should_revalidate_from_failure(
        cached, now_ns=now_ns
    ):
        # A short, bounded failure window prevents probe storms while never
        # hiding the underlying auth/authorization error, which the caller
        # still surfaces when it actually reaches this point.
        raise PolicyViolation("Remote embedding qualification unavailable (recent failure)")

    # Single-flight: concurrent qualification for the same binding collapses into
    # one probe; waiters are bounded by the accepted deadline below.
    event = _qualification_singleflight.get(binding_digest)
    if event is not None:
        deadline = time.monotonic() + QUALIFICATION_TTL_SECONDS
        while not event.wait(0.5):
            if time.monotonic() >= deadline:
                raise PolicyViolation("Remote embedding qualification single-flight deadline asti")
        return _provider(
            home,
            ledger_path,
            config_file,
            project_id,
            chunks,
            knowledge,
            remote_authorized=True,
        )
    event = threading.Event()
    _qualification_singleflight[binding_digest] = event
    try:
        probe = provider.probe(fixture)
        # WP1 (B01): this probe (two remote _vectors calls + the later dense
        # embed_query) re-ran on every authorized query before WP2.  The counter
        # is diagnostic only and never enters a semantic/authority digest.
        record_qualification()
        record_probe_latency(probe.latency_ms)
        record = _QualificationRecord(
            binding_digest=binding_digest,
            qualified_at_ns=_now_ns(),
            expires_at_ns=_now_ns() + QUALIFICATION_TTL_SECONDS * 1_000_000_000,
            ttl_seconds=QUALIFICATION_TTL_SECONDS,
            profile_digest=probe.profile.profile_digest,
            probe_evidence_digest=probe.evidence_digest,
            semantic_margin=probe.semantic_margin,
            max_repeat_delta=probe.max_repeat_delta,
            max_batch_delta=probe.max_batch_delta,
            latency_ms=probe.latency_ms,
            provider_call_count=probe.provider_call_count,
            model_revision_fingerprint=probe.profile.model_revision_fingerprint,
            provider_identity_digest=probe.profile.provider_identity_digest,
            exact_model_id=probe.profile.exact_model_id,
        )
        _write_durable_qualification(ledger_path, record)
        _cache_inprocess_qualification(binding_digest, record)
        policy = EmbeddingPolicy(
            DataClassification.INTERNAL,
            probe.profile.profile_digest,
            remote_disclosure_authorized=True,
        )
        return _EmbeddingBinding(
            provider=provider,
            policy=policy,
            ledger=host.summary(),
            probe={
                "profile_digest": probe.profile.profile_digest,
                "probe_evidence_digest": probe.evidence_digest,
                "semantic_margin": probe.semantic_margin,
                "max_repeat_delta": probe.max_repeat_delta,
                "max_batch_delta": probe.max_batch_delta,
                "latency_ms": probe.latency_ms,
                "route": "remote",
            },
            route=knowledge.embedding_route,
            remote_provider_used=True,
            probe_call_count=probe.provider_call_count,
        )
    except Exception as exc:
        if not isinstance(exc, (PolicyViolation, ConfigurationError, ValidationFailed)):
            failure = _QualificationFailure(
                binding_digest=binding_digest,
                recorded_at_ns=_now_ns(),
                error_type=type(exc).__name__,
            )
            _cache_inprocess_qualification(binding_digest, failure)
        raise
    finally:
        _qualification_singleflight.pop(binding_digest, None)


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


def _emit_index_progress(*, completed: int, total: int, provider_batches: int) -> None:
    """Keep progress as JSONL on stderr so stdout remains one final JSON document."""

    print(
        json.dumps(
            {
                "progress": completed,
                "total": total,
                "provider_batches": provider_batches,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )


def _vector_blob(vector: tuple[float, ...]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _vector_from_blob(blob: bytes) -> tuple[float, ...]:
    if len(blob) != VECTOR_DIMENSION * 4:
        raise ValidationFailed("Vector cache dimension drift")
    return tuple(float(value) for value in struct.unpack(f"<{VECTOR_DIMENSION}f", blob))


def _embed_documents_with_retry(
    provider: EmbeddingProvider,
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


def _state_cas_identity(state: dict[str, Any]) -> tuple[object, object, object]:
    """The fields that pin a resume/query state to one authoritative index generation.

    ``_index`` advances ``generation_digest``/``source_revision``/``tree_digest`` when
    it publishes a newer generation.  A concurrent stale query writer that re-reads
    state before a newer index publication must not clobber it; comparing exactly
    these generation-pinning fields detects the concurrent advance.
    """
    return (
        state.get("generation_digest"),
        state.get("source_revision"),
        state.get("tree_digest"),
    )


@contextmanager
def _rag_state_write_lock(path: Path) -> Iterator[None]:
    """Serialize rag-state.json writers with a private, non-following lock.

    Both ``_index`` and ``_query`` write the same ``rag-state.json``.  The lock keeps
    their read-modify-write sequences from interleaving in one process while the
    compare-and-set still protects against a cross-process advance.
    """
    import importlib

    lock_path = Path(str(path) + ".writer.lock")
    if lock_path.is_symlink():
        raise ConfigurationError("RAG state writer lock symlink olamaz")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    acquired = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        restrict_private_file(lock_path)
        identity = lock_path.lstat()
        opened = os.fstat(descriptor)
        if not private_regular(lock_path) or (identity.st_dev, identity.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise ConfigurationError("RAG state writer lock identity/ACL drift")
        deadline = time.monotonic() + 5.0
        while True:
            try:
                if os.name == "nt":
                    msvcrt = importlib.import_module("msvcrt")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl = importlib.import_module("fcntl")
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise PolicyViolation(
                        "RAG state writer already active; concurrent reindex in progress"
                    ) from exc
                time.sleep(0.01)
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_rag_state_cas(
    path: Path,
    *,
    expected_identity: tuple[object, object, object],
    payload: bytes,
) -> bool:
    """Compare-and-set write of ``rag-state.json`` under a writer lock.

    Re-reads the current on-disk state inside the lock and only commits if its
    generation-pinning identity still matches ``expected_identity``.  If a
    concurrent reindex advanced the generation before this write, the stale write
    is skipped (returns ``False``) so the newer authoritative state is preserved.
    Returns ``True`` when the write committed, ``False`` when skipped.
    """
    with _rag_state_write_lock(path):
        try:
            current_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PolicyViolation("RAG state CAS okuma hatasi") from exc
        try:
            current = json.loads(current_text)
        except json.JSONDecodeError as exc:
            raise PolicyViolation("RAG state CAS exact JSON olmali") from exc
        if not isinstance(current, dict):
            raise PolicyViolation("RAG state CAS nesne olmali")
        if _state_cas_identity(current) != expected_identity:
            # Concurrent generation advance: never clobber the newer state.
            return False
        _write_private(path, payload)
        return True


def build_project_source_binding_plan(
    home: Path, project_slug: str, source_root: Path
) -> dict[str, Any]:
    """Validate and identify an exact device-local source binding without writing."""

    slug = validate_slug(project_slug)
    project_id = _registered_project(home, slug)
    root = source_root.resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor):
        raise ValidationFailed("Project source root bounded directory olmali")
    head, status_digest, revision = _git_source_state(root)
    source_kind = "git" if head else "directory"
    body = {
        "schema": "zekam-project-local-source-binding/v1",
        "project_id": str(project_id),
        "project_slug": slug,
        "source_kind": source_kind,
        "source_root": str(root),
        "source_root_digest": digest_of_bytes(str(root).encode("utf-8")),
        "git_head": head or None,
        "git_status_digest": f"sha256:{status_digest}" if head else None,
        "source_revision": revision,
    }
    return body | {
        "schema": "zekam-project-local-source-binding-plan/v1",
        "binding_schema": body["schema"],
        "plan_digest": digest(body),
        "apply": False,
    }


def bind_project_source(home: Path, project_slug: str, source_root: Path) -> dict[str, Any]:
    """Persist this device's private, exact source-root binding for a project."""

    plan = build_project_source_binding_plan(home, project_slug, source_root)
    document = {
        key: value
        for key, value in plan.items()
        if key not in {"binding_schema", "plan_digest", "apply"}
    }
    document["schema"] = plan["binding_schema"]
    slug = str(document["project_slug"])
    paths = _runtime_paths(home, slug)
    binding_path = paths["project_root"] / "baglantilar" / "source.json"
    _write_private(binding_path, (canonical_json(document) + "\n").encode("utf-8"))
    if not private_regular(binding_path):
        raise PolicyViolation("Project source binding private ACL ister")
    return document | {
        "binding_ref": binding_path.relative_to(paths["home"]).as_posix(),
        "plan_digest": plan["plan_digest"],
        "apply": True,
    }


def resolve_project_source(home: Path, project_slug: str) -> Path:
    """Resolve and validate this device's private source-root binding."""

    slug = validate_slug(project_slug)
    project_id = _registered_project(home, slug)
    paths = _existing_runtime_paths(home, slug)
    binding_path = paths["project_root"] / "baglantilar" / "source.json"
    if not (
        _scoped_path_is_private(paths["home"], paths["project_root"], directory=True)
        and _scoped_path_is_private(paths["home"], binding_path.parent, directory=True)
        and _scoped_path_is_private(paths["home"], binding_path, directory=False)
    ):
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
    paths = _existing_runtime_paths(home, slug)
    if not paths["state"].is_file() or not paths["index"].is_file():
        return {
            "schema": "zekam-project-rag-status/v1",
            "project_id": str(project_id),
            "project_slug": slug,
            "state": "unavailable",
            "index_readable": False,
            "provider_readiness": "unknown",
            "query_verified_at": None,
            "query_ready": False,
            "query_available": False,
        }
    if not _rag_scope_is_private(paths):
        return {
            "schema": "zekam-project-rag-status/v1",
            "project_id": str(project_id),
            "project_slug": slug,
            "state": "blocked",
            "index_readable": False,
            "provider_readiness": "unknown",
            "query_verified_at": None,
            "query_ready": False,
            "query_available": False,
            "blocked_reason": "knowledge-scope-acl-drift",
            "retryable": False,
            "provider_calls": 0,
        }
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    with SQLiteKnowledgeIndex(paths["index"], read_only=True) as index:
        generation = index.generation(str(project_id))
        integrity = index.readiness(str(project_id))
    if state.get("generation_digest") != generation.generation_digest:
        raise PolicyViolation("Project RAG state/generation drift")
    index_readable = integrity.get("status") == "passed"
    current_knowledge = load_settings(home=paths["home"]).knowledge
    try:
        _assert_canonical_knowledge_profile(current_knowledge)
    except ConfigurationError:
        profile_binding_current = False
    else:
        profile_binding_current = state.get(
            "knowledge_binding_digest"
        ) == _knowledge_binding_digest(current_knowledge)
    return {
        "schema": "zekam-project-rag-status/v1",
        "project_id": str(project_id),
        "project_slug": slug,
        # Keep the v1 state contract stable for existing consumers.  The
        # readiness fields below make explicit that this only proves the local
        # index is readable; it is not a successful provider query receipt.
        "state": (
            "ready"
            if index_readable and profile_binding_current
            else "index-rebuild-required"
            if index_readable
            else "corrupt"
        ),
        "index_readable": index_readable,
        "index_rebuild_required": index_readable and not profile_binding_current,
        "provider_readiness": "unknown",
        "query_verified_at": state.get("query_verified_at"),
        "query_ready": False,
        "query_available": index_readable,
        "query_mode": "current-or-snapshot" if index_readable else "blocked",
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
        "embedding_profile_id": state.get("embedding_profile_id"),
        "embedding_route": state.get("embedding_route"),
        "knowledge_binding_digest": state.get("knowledge_binding_digest"),
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
    paths = _existing_runtime_paths(home, slug)
    if not _rag_scope_is_private(paths):
        raise PolicyViolation("Project RAG scoped ACL/identity drift")
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
    generation_scope_digest: str,
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
                chunk_id=_generation_chunk_id(chunk.chunk_id, generation_scope_digest),
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


def _generation_chunk_scope_digest(
    *,
    project_id: str,
    source_revision: str,
    tree_digest: str,
    source_manifest_digest: str,
    embedding_profile_digest: str,
    provider_profile_digest: str,
    chunk_fingerprints: tuple[tuple[str, str, str], ...],
) -> str:
    """Bind globally unique chunk ids to every input that can create a generation."""

    return digest(
        {
            "schema": "zekam-project-chunk-generation-scope/v1",
            "project_id": project_id,
            "source_revision": source_revision,
            "tree_digest": tree_digest,
            "source_manifest_digest": source_manifest_digest,
            "embedding_profile_digest": embedding_profile_digest,
            "provider_profile_digest": provider_profile_digest,
            "chunks": [
                {
                    "id": chunk_id,
                    "content_digest": content_digest,
                    "vector_digest": vector_digest,
                }
                for chunk_id, content_digest, vector_digest in chunk_fingerprints
            ],
        }
    )


def _generation_chunk_id(chunk_id: str, generation_scope_digest: str) -> str:
    """Keep immutable generations from reusing a globally unique SQLite row id."""

    return f"{chunk_id}-g{generation_scope_digest.removeprefix('sha256:')[-16:]}"


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
    opencode_config: Path | None,
    oracle_config: str | None,
    *,
    batch_size: int,
    authorize_odi_metadata: bool = False,
    authorize_remote_source: bool = False,
) -> dict[str, Any]:
    paths = _runtime_paths(home, project_slug)
    knowledge = load_settings(home=paths["home"]).knowledge
    _assert_canonical_knowledge_profile(knowledge)
    source_state_before = _git_source_state(source_root)
    discovery, plan = _project_plan(
        source_root,
        project_id=project_id,
        project_slug=project_slug,
        knowledge=knowledge,
    )
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
    binding = _provider(
        paths["home"],
        paths["ledger"],
        opencode_config,
        project_id,
        plan.chunks,
        knowledge,
        remote_authorized=authorize_remote_source,
    )
    provider = binding.provider
    policy = binding.policy
    profile = provider.describe()
    if binding.route is EmbeddingRoute.REMOTE:
        candidate = EmbeddingRouteCandidate(
            model_ref=profile.exact_model_id,
            dimension=profile.dimension,
            qualified=True,
            health_fresh=True,
            verified=True,
            latency_ms=float(binding.probe["latency_ms"]),
            semantic_margin=float(binding.probe["semantic_margin"]),
            qualification_evidence_digest=profile.profile_digest,
            probe_evidence_digest=profile.probe_evidence_digest,
        )
        discovery, plan = _project_plan(
            source_root,
            project_id=project_id,
            project_slug=project_slug,
            knowledge=knowledge,
            embedding_candidates=(candidate,),
            allow_remote_source=True,
        )
        if plan.embedding_route.kind is not EmbeddingRouteKind.QUALIFIED_REMOTE:
            raise PolicyViolation("Remote embedding route plan/effect drift")
    elif plan.embedding_route.kind is not EmbeddingRouteKind.LOCAL_PROVIDER:
        raise PolicyViolation("Local embedding route plan/effect drift")
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
            _emit_index_progress(
                completed=len(vectors),
                total=len(chunks),
                provider_batches=provider_batches,
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
    combined_manifest_digest = digest_of_bytes(combined_manifest)
    generation_chunk_scope_digest = _generation_chunk_scope_digest(
        project_id=str(project_id),
        source_revision=combined_revision,
        tree_digest=combined_tree_digest,
        source_manifest_digest=combined_manifest_digest,
        embedding_profile_digest=bound_plan.embedding_profile.profile_digest,
        provider_profile_digest=profile.profile_digest,
        chunk_fingerprints=tuple(
            (
                chunk.chunk_id,
                digest_of_bytes(chunk.text.encode("utf-8")),
                digest_of_bytes(_vector_blob(vectors[chunk.chunk_id])),
            )
            for chunk in chunks
        ),
    )
    source_digests = {
        item.relative_path: item.content_digest for item in bound_plan.discovery.files
    }
    source_records = tuple(
        KnowledgeIndexRecord(
            chunk_id=_generation_chunk_id(chunk.chunk_id, generation_chunk_scope_digest),
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
            generation_scope_digest=generation_chunk_scope_digest,
            start_order=len(source_records),
            vectors=vectors,
        )
        if bound_oracle_plan is not None
        else ()
    )
    odi_source_digest = odi_plan.source_digest if odi_plan is not None else ""
    odi_records = tuple(
        KnowledgeIndexRecord(
            chunk_id=_generation_chunk_id(chunk.chunk_id, generation_chunk_scope_digest),
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
            source_manifest_digest=combined_manifest_digest,
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
        "source_files": [
            {"path": item.relative_path, "content_digest": item.content_digest}
            for item in bound_plan.discovery.files
        ],
        "combined_manifest_digest": digest_of_bytes(combined_manifest),
        "generation_digest": generation.generation_digest,
        "provider_profile_digest": profile.profile_digest,
        "embedding_profile_digest": bound_plan.embedding_profile.profile_digest,
        "embedding_profile_id": knowledge.embedding_profile_id,
        "knowledge_binding_digest": _knowledge_binding_digest(knowledge),
        "cache_hits": cache_hits,
        "newly_embedded_chunks": len(chunks) - cache_hits,
        "provider_batches": provider_batches,
        "probe": binding.probe,
        "ledger": binding.ledger,
        "index_integrity": integrity,
        "index_ref": f"knowledge-index/vector/opencode-bge-m3/{project_slug}/knowledge.sqlite3",
        "project_projection_ref": projection_path.relative_to(paths["home"]).as_posix(),
        "source_mutated": False,
        "row_data_included": False,
        "embedding_route": binding.route.value,
        "remote_provider_used": binding.remote_provider_used,
        "provider_call_budget": provider_batches + binding.probe_call_count,
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
    # Authoritative index publish is serialized under the same writer lock that the
    # query path's compare-and-set uses, so a concurrent query CAS can never
    # interleave with (or clobber) this newer generation's state write.
    with _rag_state_write_lock(paths["state"]):
        _write_private(paths["state"], (canonical_json(result) + "\n").encode("utf-8"))
    return result


def _query(
    source_root: Path | None,
    home: Path,
    project_id: UUID,
    project_slug: str,
    config_file: Path | None,
    query: str,
    *,
    authorize_remote_query: bool = False,
) -> dict[str, Any]:
    paths = _existing_runtime_paths(home, project_slug)
    if not paths["state"].is_file() or not paths["index"].is_file():
        raise ValidationFailed("Project RAG index bulunamadi")
    if not _rag_scope_is_private(paths):
        raise PolicyViolation("Project RAG scoped ACL/identity drift")
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    if state.get("project_id") != str(project_id):
        raise PolicyViolation("Project RAG state project binding drift")
    stale_reasons: list[str] = []
    # WP3-B: content-aware freshness without a per-query full ``discover`` tree
    # re-scan.  Uses HEAD + git status to bound *which* indexed paths changed and
    # compares changed-file on-disk content against the indexed content digest, so
    # same-status-different-content is caught and deleted/inaccessible/ambiguous
    # sources produce an explicit unknown/stale state rather than a silent "current".
    if source_root is None:
        stale_reasons.append("project-source-binding-unavailable")
    else:
        try:
            stale_reasons_, _ = _source_freshness_for_query(state, source_root)
        except ZekamError:
            stale_reasons_ = ["project-source-freshness-unavailable"]
        stale_reasons.extend(stale_reasons_)
    odi_binding = load_smart_binding(home, project_slug, verify_source=False)
    current_odi_digest = odi_binding.get("source_digest") if odi_binding else None
    if state.get("odi_source_digest") != current_odi_digest:
        stale_reasons.append("odi-smart-export-stale")
    elif odi_binding is not None and not smart_binding_source_current(odi_binding):
        stale_reasons.append("odi-smart-export-source-stale")
    knowledge = load_settings(home=paths["home"]).knowledge
    canonical_profile = True
    try:
        _assert_canonical_knowledge_profile(knowledge)
    except ConfigurationError:
        canonical_profile = False
        stale_reasons.append("embedding-config-unsupported")
    profile_binding_current = (
        canonical_profile
        and state.get("knowledge_binding_digest") == _knowledge_binding_digest(knowledge)
    )
    if not profile_binding_current and "embedding-config-unsupported" not in stale_reasons:
        stale_reasons.append("embedding-config-stale")
    with SQLiteKnowledgeIndex(paths["index"], read_only=True) as index:
        generation = index.generation(str(project_id))
        if (
            state.get("generation_digest") != generation.generation_digest
            or state.get("source_revision") != generation.source_revision
            or state.get("tree_digest") != generation.tree_digest
        ):
            raise PolicyViolation("Project RAG state/index generation binding drift")
        dense_disabled_reason: str | None = None
        if not profile_binding_current:
            dense_disabled_reason = "embedding-config-stale"
            binding = _lexical_only_binding(
                generation.provider_profile_digest,
                knowledge.embedding_route,
                dense_disabled_reason,
            )
        elif knowledge.embedding_route is EmbeddingRoute.REMOTE and not authorize_remote_query:
            if not stale_reasons:
                raise PolicyViolation("Remote query embedding explicit authorization ister")
            dense_disabled_reason = "remote-query-not-authorized"
            binding = _lexical_only_binding(
                generation.provider_profile_digest,
                knowledge.embedding_route,
                dense_disabled_reason,
            )
        else:
            try:
                # WP3-A: the query path must NOT run full corpus plan chunking /
                # document embedding / Oracle metadata / reindex.  Local-route
                # qualification uses the accepted, bounded synthetic fixture
                # identity (same contract as remote) and never asks the local
                # provider to build a fresh corpus plan from source.
                if knowledge.embedding_route is EmbeddingRoute.LOCAL:
                    binding = _local_query_binding(
                        paths["home"],
                        paths["ledger"],
                        project_id,
                        knowledge,
                    )
                else:
                    binding = _provider(
                        paths["home"],
                        paths["ledger"],
                        config_file,
                        project_id,
                        (),
                        knowledge,
                        remote_authorized=authorize_remote_query,
                    )
            except (OSError, subprocess.SubprocessError, ZekamError) as exc:
                dense_disabled_reason = f"embedding-provider-unavailable:{type(exc).__name__}"
                binding = _lexical_only_binding(
                    generation.provider_profile_digest,
                    knowledge.embedding_route,
                    dense_disabled_reason,
                )
        result = EmbeddedProjectRAG(index, binding.provider, binding.policy).query(
            query,
            project_id=str(project_id),
            expected_source_revision=str(state["source_revision"]),
            expected_tree_digest=str(state["tree_digest"]),
            dense_disabled_reason=dense_disabled_reason,
        )
    stale_reasons = list(dict.fromkeys([*result.get("stale_reasons", ()), *stale_reasons]))
    result["index_freshness"] = "stale" if stale_reasons else "current"
    result["stale_reasons"] = stale_reasons
    result["reindex_recommended"] = bool(stale_reasons)
    result["retrieval_digest"] = digest(
        {key: value for key, value in result.items() if key != "retrieval_digest"}
    )
    provider_profile_digest = (
        generation.provider_profile_digest
        if dense_disabled_reason is not None
        else binding.provider.describe().profile_digest
    )
    query_contract_verified = (
        not stale_reasons
        and dense_disabled_reason is None
        and "dense" in result.get("searched_channels", ())
        and result.get("degraded_reason") is None
        and result.get("provider_profile_digest") == provider_profile_digest
        and result.get("generation_digest") == state.get("generation_digest")
    )
    query_verified_at = state.get("query_verified_at")
    if query_contract_verified:
        query_verified_at = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        state["query_verified_at"] = query_verified_at
        state["query_verification"] = {
            "embedding_route": binding.route.value,
            "provider_profile_digest": provider_profile_digest,
            "source_revision": state["source_revision"],
            "retrieval_digest": result.get("retrieval_digest"),
        }
        # Revision-bound compare-and-set: only advance the observational
        # query_verification fields if the on-disk state still pins the SAME index
        # generation the query verified against.  A concurrent reindex that
        # published a newer generation must not be clobbered by this stale query
        # write.  A skipped write only loses an observational timestamp; the
        # authoritative generation state is preserved.
        _write_rag_state_cas(
            paths["state"],
            expected_identity=_state_cas_identity(state),
            payload=(canonical_json(state) + "\n").encode("utf-8"),
        )
    return result | {
        "probe_evidence_digest": binding.probe["probe_evidence_digest"],
        "embedding_route": binding.route.value,
        "remote_provider_used": binding.remote_provider_used,
        "query_verified_at": query_verified_at,
        "query_verification_recorded": query_contract_verified,
        "database_freshness": (
            "disabled" if state.get("database_access") == "disabled" else "last-indexed-snapshot"
        ),
        "snapshot_only": bool(stale_reasons),
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
    authorize_remote_query: bool = False,
) -> dict[str, Any]:
    """Query a registered project through its validated local binding and active index."""

    slug = validate_slug(project_slug)
    project_id = _registered_project(home, slug)
    resolved_home = home.resolve(strict=True)
    source_binding = _existing_runtime_paths(resolved_home, slug)["project_root"] / (
        "baglantilar/source.json"
    )
    if not source_binding.exists() and not source_binding.is_symlink():
        root = None
    else:
        try:
            root = resolve_project_source(resolved_home, slug)
        except FileNotFoundError:
            root = None
    config_file = (
        (opencode_config or default_opencode_config_file()).resolve(strict=False)
        if authorize_remote_query
        else None
    )
    return _query(
        root,
        resolved_home,
        project_id,
        slug,
        config_file,
        question,
        authorize_remote_query=authorize_remote_query,
    )


def index_registered_project(
    home: Path,
    project_slug: str,
    *,
    oracle_config: str | None = None,
    opencode_config: Path | None = None,
    batch_size: int = MAX_BATCH_SIZE,
    authorize_odi_metadata: bool = False,
    authorize_remote_source: bool = False,
) -> dict[str, Any]:
    """Refresh code and Oracle metadata for a registered, locally bound project."""

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValidationFailed("Batch size 1..64 araliginda olmali")
    slug = validate_slug(project_slug)
    root = resolve_project_source(home, slug)
    project_id = _registered_project(home, slug)
    resolved_home = home.resolve(strict=True)
    route = project_embedding_route(resolved_home)
    config_file = (
        (opencode_config or default_opencode_config_file()).resolve(strict=True)
        if route is EmbeddingRoute.REMOTE
        else None
    )
    return _index(
        root,
        resolved_home,
        project_id,
        slug,
        config_file,
        oracle_config,
        batch_size=batch_size,
        authorize_odi_metadata=authorize_odi_metadata,
        authorize_remote_source=authorize_remote_source,
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
        route = project_embedding_route(home)
        if route is EmbeddingRoute.REMOTE and not args.authorize_remote_source:
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
            (
                args.opencode_config.resolve(strict=True)
                if route is EmbeddingRoute.REMOTE
                else None
            ),
            args.oracle_config,
            batch_size=args.batch_size,
            authorize_remote_source=args.authorize_remote_source,
        )
    else:
        route = project_embedding_route(home)
        result = _query(
            source_root,
            home,
            project_id,
            project_slug,
            (
                args.opencode_config.resolve(strict=False)
                if route is EmbeddingRoute.REMOTE and args.authorize_remote_query
                else None
            ),
            args.question,
            authorize_remote_query=args.authorize_remote_query,
        )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
