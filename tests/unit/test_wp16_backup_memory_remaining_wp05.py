"""Disjoint WP16 branch probes for backup, projection, embedding, and continuity."""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from zekam.application import memory_continuity as memory
from zekam.application.continuity_projection import (
    ACTIVE_WORK_PROJECTION_REF,
    ProjectionReleaseSnapshot,
)
from zekam.application.memory_continuity import (
    ContinuityReceiptKind,
    ContinuityReceiptPlan,
    MemoryContinuityService,
)
from zekam.application.memory_upgrade import canonical_projection_source_digest
from zekam.application.obsidian_projection import build_obsidian_projection
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.context_continuity import (
    AuthorityLevel,
    Checkpoint,
    ContextCandidate,
    ContextCandidateKind,
    ContextManifest,
    ContextOmission,
    ContextSelection,
    ContinuitySnapshot,
    EvidenceReference,
    FinalizedHandoff,
    JournalEntry,
    OmittedReason,
    TargetRouteBinding,
    compile_context,
    validate_resume,
    verify_journal,
)
from zekam.domain.errors import (
    ConfigurationError,
    NotFound,
    PolicyViolation,
    ValidationFailed,
)
from zekam.domain.markdown_projection import (
    ObsidianNoteKind,
    ObsidianProfile,
    ObsidianProjectionBundle,
    ObsidianProjectionRecord,
    ProjectionRecord,
    ProjectionSourceRef,
)
from zekam.domain.security import DataClassification
from zekam.domain.session_continuity import (
    CompactionReceipt,
    CompactionStatus,
    TruthClass,
)
from zekam.domain.session_continuity import (
    DataClassification as ContinuityClassification,
)
from zekam.infrastructure import local_backup
from zekam.infrastructure.embedding import infinity_bge as bge
from zekam.infrastructure.storage.obsidian_projection_store import (
    LocalObsidianProjectionStore,
)

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
PROJECT = UUID("00000000-0000-0000-0000-000000000905")


def _candidate(**changes: object) -> ContextCandidate:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "authority": AuthorityLevel.VERIFIED,
        "observed_at": NOW,
        "source_revision": "revision-1",
        "content_digest": digest("content"),
        "token_count": 2,
    }
    values.update(changes)
    return ContextCandidate(**values)  # type: ignore[arg-type]


def _selection() -> ContextSelection:
    candidate = _candidate()
    return ContextSelection(
        candidate.candidate_id,
        candidate.content_digest,
        candidate.token_count,
        candidate.score(NOW),
        "selected",
        candidate_digest=candidate.candidate_digest,
        authority=candidate.authority,
    )


def _checkpoint() -> Checkpoint:
    return Checkpoint(
        "checkpoint-1",
        "project-1",
        "work-1",
        "plan-1",
        "revision-1",
        ("step-1",),
        ("step-1",),
        (),
        (("step-1", digest("result")),),
        digest("context"),
        digest("journal"),
        "continue",
        NOW,
    )


def _snapshot(checkpoint: Checkpoint | None = None) -> ContinuitySnapshot:
    selected = checkpoint or _checkpoint()
    return ContinuitySnapshot(
        "project-1",
        "work-1",
        selected.checkpoint_digest,
        digest("journal"),
        digest("context"),
        "revision-1",
        ("checkpoint",),
        ("continue",),
        (EvidenceReference("test", "test-1", digest("evidence")),),
        NOW,
    )


def _handoff(**changes: object) -> FinalizedHandoff:
    values: dict[str, object] = {
        "from_client": "codex",
        "to_client": "codex",
        "from_model_ref": "model-a",
        "to_model_ref": "model-a",
        "snapshot_digest": _snapshot().snapshot_digest,
        "checkpoint_digest": _checkpoint().checkpoint_digest,
        "source_revision": "revision-1",
        "created_at": NOW,
    }
    values.update(changes)
    return FinalizedHandoff(**values)  # type: ignore[arg-type]


def test_context_remaining_rejection_manifest_and_compile_matrix() -> None:
    expired = _candidate(valid_until=NOW + dt.timedelta(seconds=1))
    assert (
        expired.rejection(NOW + dt.timedelta(seconds=1), AuthorityLevel.UNTRUSTED)
        is OmittedReason.STALE
    )
    assert _candidate().rejection(NOW, AuthorityLevel.UNTRUSTED) is None
    with pytest.raises(ValidationFailed, match="negatif"):
        ContextOmission("candidate", OmittedReason.BUDGET, -1)

    selection = _selection()
    base = ContextManifest(2, (selection,), (), digest("set"), NOW)
    for changes, error in (
        ({"grants_authority": True}, PolicyViolation),
        ({"token_budget": 1}, ValidationFailed),
        ({"recipe_id": "recipe"}, ValidationFailed),
        ({"compiler_version": 3}, ValidationFailed),
        ({"compiler_version": 2}, ValidationFailed),
        ({"scoring_policy_digest": digest("policy")}, ValidationFailed),
    ):
        with pytest.raises(error):
            replace(base, **changes)

    typed = _candidate(kind=ContextCandidateKind.SYSTEM_POLICY)
    with pytest.raises(PolicyViolation, match="recipe"):
        compile_context(
            (typed,), token_budget=2, minimum_authority=AuthorityLevel.OBSERVED, now=NOW
        )
    with pytest.raises(ValidationFailed, match="tekil"):
        compile_context(
            (_candidate(), _candidate()),
            token_budget=4,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
        )
    required = _candidate(required=True, token_count=3)
    with pytest.raises(PolicyViolation, match="sigmiyor"):
        compile_context(
            (required,), token_budget=2, minimum_authority=AuthorityLevel.OBSERVED, now=NOW
        )
    optional = _candidate(token_count=3)
    result = compile_context(
        (optional,), token_budget=2, minimum_authority=AuthorityLevel.OBSERVED, now=NOW
    )
    assert result.omitted[0].reason is OmittedReason.BUDGET
    with pytest.raises(ValidationFailed, match="budget"):
        compile_context((), token_budget=0, minimum_authority=AuthorityLevel.OBSERVED, now=NOW)


def test_context_remaining_journal_checkpoint_snapshot_route_guards() -> None:
    with pytest.raises(ValidationFailed, match="pozitif"):
        JournalEntry(0, "work-1", "event", digest("payload"), None, False, NOW)
    entry = JournalEntry(1, "work-1", "event", digest("payload"), None, False, NOW)
    assert verify_journal((entry,), entry.entry_digest) == entry.entry_digest
    with pytest.raises(ValidationFailed, match="bos"):
        verify_journal(())
    with pytest.raises(ValidationFailed, match="mismatch"):
        verify_journal((entry,), digest("wrong"))

    checkpoint = _checkpoint()
    for changes, error in (
        ({"grants_authority": True}, PolicyViolation),
        ({"plan_steps": ("step-1", "step-1")}, ValidationFailed),
        ({"pending_steps": ("step-1",)}, ValidationFailed),
        ({"step_results": ()}, ValidationFailed),
    ):
        with pytest.raises(error):
            replace(checkpoint, **changes)

    snapshot = _snapshot(checkpoint)
    for changes in (
        {"grants_authority": True},
        {"carries_active_lease": True},
        {"approval_inherited": True},
    ):
        with pytest.raises(PolicyViolation):
            replace(cast(Any, snapshot), **changes)
    with pytest.raises(ValidationFailed, match="bounded"):
        replace(snapshot, first_reads=())

    route = TargetRouteBinding(uuid4(), digest("route"), "model-a", NOW + dt.timedelta(1), NOW)
    assert route.target_model_ref == "model-a"
    with pytest.raises(ValidationFailed, match="timezone"):
        replace(route, valid_until=route.valid_until.replace(tzinfo=None))
    with pytest.raises(PolicyViolation, match="fresh"):
        replace(route, observed_at=route.valid_until)


def test_context_remaining_handoff_and_resume_guards() -> None:
    base = _handoff()
    assert base.cross_client_ready and not base.legacy_limited
    for changes in (
        {"transcript_included": True},
        {"grants_authority": True},
        {"carries_active_lease": True},
        {"approval_inherited": True},
        {"reacquire_required": False},
    ):
        with pytest.raises(PolicyViolation):
            _handoff(**changes)
    for key in ("unsupported_capabilities", "unsupported_permissions", "required_replan_items"):
        with pytest.raises(ValidationFailed, match="kanonik"):
            _handoff(**{key: ("z", "a")})
    with pytest.raises(ValidationFailed, match="timezone"):
        _handoff(target_route_valid_until=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationFailed, match="freshness"):
        _handoff(target_route_valid_until=NOW + dt.timedelta(1), target_route_fresh=False)
    with pytest.raises(ValidationFailed, match="expiry"):
        _handoff(target_route_fresh=True)
    extended = _handoff(
        source_client_capability_digest=digest("source-capability"),
        target_client_capability_digest=digest("target-capability"),
        source_client_permission_digest=digest("source-permission"),
        target_client_permission_digest=digest("target-permission"),
        target_route_decision_id=uuid4(),
        target_route_decision_digest=digest("route"),
        target_route_valid_until=NOW + dt.timedelta(1),
        target_route_fresh=True,
    )
    assert extended.handoff_digest != base.handoff_digest

    checkpoint = _checkpoint()
    snapshot = _snapshot(checkpoint)
    cross = _handoff(from_client="codex", to_client="claude")
    with pytest.raises(PolicyViolation, match="Cross-client"):
        validate_resume(cross, snapshot, checkpoint, current_source_revision="revision-1")
    for handoff_changes, message, error in (
        ({"snapshot_digest": digest("wrong")}, "snapshot", ValidationFailed),
        ({"checkpoint_digest": digest("wrong")}, "checkpoint", ValidationFailed),
        ({"source_revision": "revision-2"}, "source revision", PolicyViolation),
    ):
        with pytest.raises(error, match=message):
            validate_resume(
                replace(cast(Any, base), **handoff_changes),
                snapshot,
                checkpoint,
                current_source_revision="revision-1",
            )
    drifted_snapshot = replace(snapshot, checkpoint_digest=digest("other"))
    with pytest.raises(ValidationFailed, match="Continuity checkpoint"):
        validate_resume(
            replace(base, snapshot_digest=drifted_snapshot.snapshot_digest),
            drifted_snapshot,
            checkpoint,
            current_source_revision="revision-1",
        )


class _InfinityTransport:
    def __init__(self, catalog: Any = None, embedding: Any = None, health: Any = None) -> None:
        self.catalog = catalog
        self.embedding = embedding
        self.health = health

    def get(self, url: str, *, timeout_seconds: float) -> Any:
        del timeout_seconds
        return self.health if url.endswith("/health") else self.catalog

    def post(self, url: str, payload: bytes, *, timeout_seconds: float) -> Any:
        del url, payload, timeout_seconds
        return self.embedding


def _bge_cache(tmp_path: Path) -> tuple[Path, bge.LocalBGEConfiguration]:
    root = tmp_path / "models--BAAI--bge-m3"
    revision = "1" * 40
    snapshot = root / "snapshots" / revision
    (root / "refs").mkdir(parents=True)
    snapshot.mkdir(parents=True)
    (root / "refs" / "main").write_text(revision)
    payload = b"weights"
    name = __import__("hashlib").sha256(payload).hexdigest()
    (snapshot / name).write_bytes(payload)
    (snapshot / "pytorch_model.bin").symlink_to(name)
    for filename in ("tokenizer.json", "sentencepiece.bpe.model", "config.json"):
        (snapshot / filename).write_bytes(b"{}")
    return root, bge.LocalBGEConfiguration("http://127.0.0.1:7997", root)


def _catalog(**changes: object) -> dict[str, Any]:
    model: dict[str, Any] = {
        "id": "BAAI/bge-m3",
        "object": "model",
        "owned_by": "infinity",
        "capabilities": ["embed"],
        "backend": "torch",
        "stats": {"batch_size": 8},
    }
    model.update(changes)
    return {"data": [model], "object": "list"}


def test_bge_exact_json_transport_and_configuration_remaining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert bge._exact_json(b'{"a":1}') == {"a": 1}
    with pytest.raises(ConfigurationError, match="duplicate"):
        bge._exact_json(b'{"a":1,"a":2}')
    with pytest.raises(ConfigurationError, match="strict"):
        bge._exact_json(b"\xff")

    _, config = _bge_cache(tmp_path)
    for changes in (
        {"model_cache_root": Path("relative")},
        {"exact_model_id": "other"},
        {"dimension": 3},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"max_batch_size": True},
        {"max_batch_size": 0},
        {"compute_dtype": "bfloat16"},
    ):
        with pytest.raises(ConfigurationError):
            replace(config, **changes)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert bge.default_mac_bge_configuration().model_cache_root.is_absolute()


class _Response:
    def __init__(self, status: int, payload: bytes, declared: str | None = None) -> None:
        self.status = status
        self.payload = payload
        self.headers = {"Content-Length": declared} if declared is not None else {}

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _: int) -> bytes:
        return self.payload


def test_bge_loopback_transport_status_size_and_provider_failures() -> None:
    transport = bge.LoopbackInfinityJsonTransport()
    valid_response = _Response(200, b'{"ok":true}')
    transport._opener = cast(Any, SimpleNamespace(open=lambda *a, **k: valid_response))
    assert transport.get("http://127.0.0.1", timeout_seconds=1) == {"ok": True}
    for response, message in (
        (_Response(500, b"{}"), "status"),
        (_Response(200, b"{}", str(bge._MAX_RESPONSE_BYTES + 1)), "oversized"),
        (_Response(200, b"x" * (bge._MAX_RESPONSE_BYTES + 1)), "oversized"),
    ):
        transport._opener = cast(
            Any, SimpleNamespace(open=lambda *a, response=response, **k: response)
        )
        with pytest.raises(ConfigurationError, match=message):
            transport.get("http://127.0.0.1", timeout_seconds=1)

    for exc, error in (
        (urllib.error.HTTPError("x", 302, "redirect", cast(Any, {}), None), PolicyViolation),
        (urllib.error.HTTPError("x", 500, "failure", cast(Any, {}), None), ConfigurationError),
        (urllib.error.URLError("offline"), ConfigurationError),
    ):

        def fail(*args: object, exc: Exception = exc, **kwargs: object) -> Any:
            raise exc

        transport._opener = cast(Any, SimpleNamespace(open=fail))
        with pytest.raises(error):
            transport.post("http://127.0.0.1", b"{}", timeout_seconds=1)


def test_bge_discovery_remaining_cache_and_catalog_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = _bge_cache(tmp_path)
    valid = _catalog()
    discovery = bge.discover_local_bge(config, _InfinityTransport(valid))
    assert discovery.backend == "torch"

    catalogs: tuple[Any, ...] = (
        [],
        {"data": [], "object": "list"},
        {"data": ["bad"], "object": "list"},
        _catalog(id="other"),
        _catalog(stats={"batch_size": 0}),
    )
    for catalog in catalogs:
        with pytest.raises(ConfigurationError):
            bge.discover_local_bge(config, _InfinityTransport(catalog))

    (root / "refs" / "main").write_text("not-a-revision")
    with pytest.raises(ConfigurationError, match="commit"):
        bge.discover_local_bge(config, _InfinityTransport(valid))
    (root / "refs" / "main").write_text("1" * 40)
    (root / "snapshots" / ("1" * 40) / "pytorch_model.bin").unlink()
    with pytest.raises(ConfigurationError, match="eksik"):
        bge.discover_local_bge(config, _InfinityTransport(valid))

    outside = tmp_path / "outside"
    outside.write_bytes(b"x")
    snapshot = root / "snapshots" / ("1" * 40)
    (snapshot / "pytorch_model.bin").symlink_to(outside)
    with pytest.raises(ConfigurationError, match="cache disinda"):
        bge.discover_local_bge(config, _InfinityTransport(valid))


def test_bge_discovery_remaining_identity_guards(tmp_path: Path) -> None:
    valid = _catalog()
    root, config = _bge_cache(tmp_path / "root")
    revision = "1" * 40

    root.rename(tmp_path / "real-root")
    root.symlink_to(tmp_path / "real-root", target_is_directory=True)
    with pytest.raises(ConfigurationError, match="directory"):
        bge.discover_local_bge(config, _InfinityTransport(valid))

    root, config = _bge_cache(tmp_path / "ref")
    ref = root / "refs" / "main"
    ref.unlink()
    ref.symlink_to(tmp_path / "outside-ref")
    with pytest.raises(ConfigurationError, match="refs/main"):
        bge.discover_local_bge(config, _InfinityTransport(valid))

    root, config = _bge_cache(tmp_path / "snapshot")
    target = tmp_path / "snapshot-target"
    target.mkdir()
    (root / "snapshots" / revision).rename(target / revision)
    (root / "snapshots" / revision).symlink_to(target / revision, target_is_directory=True)
    with pytest.raises(ConfigurationError, match="snapshot"):
        bge.discover_local_bge(config, _InfinityTransport(valid))

    root, config = _bge_cache(tmp_path / "weight")
    snapshot = root / "snapshots" / revision
    weight = snapshot / "pytorch_model.bin"
    weight.unlink()
    weight.write_bytes(b"not-content-addressed")
    with pytest.raises(ConfigurationError, match="content-address"):
        bge.discover_local_bge(config, _InfinityTransport(valid))

    directory = snapshot / "directory"
    directory.mkdir()
    with pytest.raises(ConfigurationError, match="regular file"):
        bge._resolved_model_file(snapshot, root, "directory")


def _discovery() -> bge.LocalBGEDiscovery:
    return bge.LocalBGEDiscovery(
        "1" * 40,
        digest("model"),
        digest("tokenizer"),
        digest("preprocessor"),
        digest("provider"),
        digest("runtime"),
        "darwin-arm64:mps",
        "torch",
        8,
    )


def _embedding(rows: Any, **changes: object) -> dict[str, Any]:
    value: dict[str, Any] = {
        "object": "list",
        "data": rows,
        "model": "BAAI/bge-m3",
        "usage": {},
        "id": "response",
        "created": 1,
    }
    value.update(changes)
    return value


def test_bge_vector_and_health_remaining_fail_closed(tmp_path: Path) -> None:
    config = bge.LocalBGEConfiguration("http://127.0.0.1:7997", tmp_path)
    documents: tuple[Any, ...] = (
        [],
        _embedding([], object="wrong"),
        _embedding([]),
        _embedding([{"object": "wrong", "embedding": [1.0] * 1024, "index": 0}]),
        _embedding([{"object": "embedding", "embedding": [0.0] * 1024, "index": 0}]),
    )
    for document in documents:
        provider = bge.LocalInfinityBGEProvider(
            config, _discovery(), _InfinityTransport(embedding=document)
        )
        with pytest.raises((ConfigurationError, ValidationFailed)):
            provider._vectors(("text",))
    provider = bge.LocalInfinityBGEProvider(config, _discovery(), _InfinityTransport(health=[]))
    assert provider.health().degraded_state is not None


def test_bge_probe_classification_determinism_and_semantic_guards(tmp_path: Path) -> None:
    config = bge.LocalBGEConfiguration("http://127.0.0.1:7997", tmp_path)
    provider = bge.LocalInfinityBGEProvider(config, _discovery(), _InfinityTransport())
    fixture = SimpleNamespace(classification=DataClassification.SECRET)
    with pytest.raises(PolicyViolation, match="classification"):
        provider.probe(cast(Any, fixture))

    fixture = SimpleNamespace(
        classification=DataClassification.PUBLIC,
        query="query",
        positive_passage="positive",
        negative_passage="negative",
        source_refs=("source",),
        source_digests=(digest("source"),),
    )
    cast(Any, provider)._vectors = cast(
        Any,
        lambda texts: (
            ((1.0, 0.0), (0.0, 1.0), (1.0, 0.0), (0.0, 1.0)) if len(texts) == 4 else ((0.0, 1.0),)
        ),
    )
    with pytest.raises(ValidationFailed, match="determinism"):
        provider.probe(cast(Any, fixture))
    cast(Any, provider)._vectors = cast(
        Any,
        lambda texts: tuple((1.0, 0.0) for _ in texts),
    )
    with pytest.raises(ValidationFailed, match="semantic margin"):
        provider.probe(cast(Any, fixture))


def _projection_bundle(entity: str = "work-1") -> ObsidianProjectionBundle:
    record = ProjectionRecord(
        "work",
        entity,
        "Title",
        "active",
        "Safe projection",
        (ProjectionSourceRef("work", entity, "revision-1", digest(entity)),),
    )
    typed = ObsidianProjectionRecord(
        record,
        ObsidianNoteKind.WORK,
        "realm",
        PROJECT,
        TruthClass.REPO_FACT,
        ContinuityClassification.PUBLIC,
        NOW,
    )
    return build_obsidian_projection(
        (typed,),
        project_id=PROJECT,
        profile=ObsidianProfile.PUBLIC_SAFE,
        policy_digest=digest("policy"),
    )


def _published_store(
    tmp_path: Path,
) -> tuple[LocalObsidianProjectionStore, ObsidianProjectionBundle, Path]:
    store = LocalObsidianProjectionStore(tmp_path / "store")
    bundle = _projection_bundle()
    store.publish(store.stage(bundle))
    profile = store._profile_root("realm", PROJECT, ObsidianProfile.PUBLIC_SAFE, create=False)
    return store, bundle, profile


def test_obsidian_remaining_path_profile_and_stage_guards(tmp_path: Path) -> None:
    store = LocalObsidianProjectionStore(tmp_path / "store")
    with pytest.raises(ValidationFailed, match="UUID"):
        store._profile_root(
            "realm", cast(Any, "not-uuid"), ObsidianProfile.PUBLIC_SAFE, create=True
        )
    with pytest.raises(NotFound):
        store._profile_root("realm", PROJECT, ObsidianProfile.PUBLIC_SAFE, create=False)
    bad_root = LocalObsidianProjectionStore(tmp_path / "file")
    bad_root.root.write_text("not-directory")
    with pytest.raises(PolicyViolation):
        bad_root._profile_root("realm", PROJECT, ObsidianProfile.PUBLIC_SAFE, create=False)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "parent").symlink_to(tmp_path)
    with pytest.raises(PolicyViolation, match="parent"):
        store._write_file(staging, "parent/file", b"x")
    with pytest.raises(PolicyViolation, match="regular"):
        cast(
            Any,
            __import__(
                "zekam.infrastructure.storage.obsidian_projection_store", fromlist=["_regular"]
            ),
        )._regular(tmp_path / "missing")

    store.root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(NotFound, match="realm"):
        store._profile_root("realm", PROJECT, ObsidianProfile.PUBLIC_SAFE, create=False)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("missing", NotFound),
        ("oversize", PolicyViolation),
        ("json", ValidationFailed),
        ("keys", ValidationFailed),
        ("schema", ValidationFailed),
        ("realm", PolicyViolation),
        ("project", PolicyViolation),
        ("store", PolicyViolation),
        ("generation", ValidationFailed),
        ("projection", PolicyViolation),
        ("stale", PolicyViolation),
    ],
)
def test_obsidian_current_pointer_remaining_tamper_matrix(
    tmp_path: Path, mutation: str, error: type[Exception]
) -> None:
    store, bundle, profile = _published_store(tmp_path)
    pointer = profile / "CURRENT.json"
    document = json.loads(pointer.read_text())
    if mutation == "missing":
        pointer.unlink()
    elif mutation == "oversize":
        pointer.write_bytes(b"x" * (16 * 1024 + 1))
    elif mutation == "json":
        pointer.write_text("{")
    elif mutation == "keys":
        document["extra"] = True
        pointer.write_bytes(canonical_bytes(document))
    elif mutation == "schema":
        document["schema"] = "wrong"
        pointer.write_bytes(canonical_bytes(document))
    elif mutation == "realm":
        document["realm"] = "other"
        pointer.write_bytes(canonical_bytes(document))
    elif mutation == "project":
        document["project_id"] = str(uuid4())
        pointer.write_bytes(canonical_bytes(document))
    elif mutation == "store":
        document["store_identity_digest"] = digest("other")
        pointer.write_bytes(canonical_bytes(document))
    elif mutation == "generation":
        document["generation"] = "bad"
        pointer.write_bytes(canonical_bytes(document))
    elif mutation == "projection":
        document["projection_digest"] = digest("other")
        pointer.write_bytes(canonical_bytes(document))
    with pytest.raises(error):
        store.verify_current(
            "realm",
            PROJECT,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=(
                digest("other") if mutation == "stale" else bundle.projection_digest
            ),
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )


def _generation_documents(
    profile: Path, bundle: ObsidianProjectionBundle
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    root = profile / "generations" / bundle.projection_digest.removeprefix("sha256:")
    manifest = json.loads((root / "_META" / "manifest.json").read_text())
    receipt = json.loads((root / "_META" / "projection-receipt.json").read_text())
    manifest.pop("manifest_digest")
    receipt.pop("receipt_digest")
    return root, manifest, receipt


def _write_generation_documents(
    root: Path, manifest: dict[str, Any], receipt: dict[str, Any]
) -> tuple[str, str]:
    manifest_digest = digest(manifest)
    receipt["manifest_digest"] = manifest_digest
    receipt_digest = digest(receipt)
    (root / "_META" / "manifest.json").write_bytes(
        canonical_bytes({**manifest, "manifest_digest": manifest_digest})
    )
    (root / "_META" / "projection-receipt.json").write_bytes(
        canonical_bytes({**receipt, "receipt_digest": receipt_digest})
    )
    return manifest_digest, receipt_digest


@pytest.mark.parametrize(
    "mutation",
    [
        "nonobject",
        "schema",
        "digest",
        "files",
        "exclusions",
        "bad-exclusion",
        "file-count",
        "row",
        "media",
        "file-digest",
    ],
)
def test_obsidian_generation_remaining_tamper_matrix(tmp_path: Path, mutation: str) -> None:
    store, bundle, profile = _published_store(tmp_path)
    root, manifest, receipt = _generation_documents(profile, bundle)
    expected_manifest = bundle.manifest_digest
    expected_receipt = bundle.receipt_digest
    if mutation == "nonobject":
        (root / "_META" / "manifest.json").write_text("[]")
    elif mutation == "schema":
        manifest["extra"] = True
        expected_manifest, expected_receipt = _write_generation_documents(root, manifest, receipt)
    elif mutation == "digest":
        raw = json.loads((root / "_META" / "manifest.json").read_text())
        raw["manifest_digest"] = digest("wrong")
        (root / "_META" / "manifest.json").write_bytes(canonical_bytes(raw))
    elif mutation == "files":
        manifest["files"] = "bad"
        expected_manifest, expected_receipt = _write_generation_documents(root, manifest, receipt)
    elif mutation == "exclusions":
        manifest["exclusions"] = "bad"
        expected_manifest, expected_receipt = _write_generation_documents(root, manifest, receipt)
    elif mutation == "bad-exclusion":
        manifest["exclusions"] = [{"record_digest": digest("record"), "reason_code": "wrong"}]
        expected_manifest, expected_receipt = _write_generation_documents(root, manifest, receipt)
    elif mutation == "file-count":
        receipt["file_count"] = 99
        expected_manifest, expected_receipt = _write_generation_documents(root, manifest, receipt)
    elif mutation == "row":
        manifest["files"] = [{"relative_path": "bad"}]
        receipt["file_count"] = 1
        expected_manifest, expected_receipt = _write_generation_documents(root, manifest, receipt)
    elif mutation == "media":
        manifest["files"][0]["media_type"] = "application/octet-stream"
        expected_manifest, expected_receipt = _write_generation_documents(root, manifest, receipt)
    elif mutation == "file-digest":
        manifest["files"][0]["content_digest"] = digest("wrong")
        expected_manifest, expected_receipt = _write_generation_documents(root, manifest, receipt)
    with pytest.raises((ValidationFailed, PolicyViolation)):
        store._verify_generation(
            root,
            expected_realm_slug="realm",
            expected_project_id=PROJECT,
            expected_profile=ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=expected_manifest,
            expected_receipt_digest=expected_receipt,
        )


def test_obsidian_generation_root_size_and_publish_symlink_guards(tmp_path: Path) -> None:
    store = LocalObsidianProjectionStore(tmp_path / "store")
    bundle = _projection_bundle()
    staged = store.stage(bundle)
    profile = store._profile_root("realm", PROJECT, ObsidianProfile.PUBLIC_SAFE, create=False)
    (profile / "generations").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PolicyViolation, match="generations"):
        store.publish(staged)
    with pytest.raises(PolicyViolation, match="generation"):
        store._verify_generation(
            tmp_path / "missing",
            expected_realm_slug="realm",
            expected_project_id=PROJECT,
            expected_profile=ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )


def test_obsidian_stable_vault_removes_old_managed_file(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    generation = tmp_path / "generation"
    stable = profile / "GUNCEL_BELLEK"
    stable.mkdir(parents=True)
    generation.mkdir()
    obsolete = stable / "obsolete.md"
    obsolete.write_text("old")
    marker = stable / ".zekam-managed-files.json"
    marker.write_bytes(
        canonical_bytes(
            {
                "schema": "zekam-obsidian-stable-vault/v1",
                "generation": "old",
                "files": ["obsolete.md"],
                "grants_authority": False,
            }
        )
    )
    result = LocalObsidianProjectionStore._publish_stable_vault(profile, generation)
    assert result == stable and not obsolete.exists()
    marker_document = json.loads(marker.read_text())
    marker_document["files"] = ["missing.md"]
    marker.write_bytes(canonical_bytes(marker_document))
    assert LocalObsidianProjectionStore._publish_stable_vault(profile, generation) == stable


def test_obsidian_stable_vault_rejects_symlink_parent(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    stable = profile / "GUNCEL_BELLEK"
    stable.mkdir(parents=True)
    (stable / "nested").symlink_to(tmp_path, target_is_directory=True)
    generation = tmp_path / "generation"
    (generation / "nested").mkdir(parents=True)
    (generation / "nested" / "file.md").write_text("safe")
    with pytest.raises(PolicyViolation, match="symlink"):
        LocalObsidianProjectionStore._publish_stable_vault(profile, generation)


def test_obsidian_pointer_manifest_binding_and_receipt_size(tmp_path: Path) -> None:
    store, bundle, profile = _published_store(tmp_path)
    pointer_path = profile / "CURRENT.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["manifest_digest"] = digest("wrong")
    pointer_path.write_bytes(canonical_bytes(pointer))
    with pytest.raises(PolicyViolation, match="manifest/receipt binding"):
        store.verify_current(
            "realm",
            PROJECT,
            ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )

    store, bundle, profile = _published_store(tmp_path / "oversized")
    root = profile / "generations" / bundle.projection_digest.removeprefix("sha256:")
    (root / "_META" / "projection-receipt.json").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(PolicyViolation, match="receipt bounded"):
        store._verify_generation(
            root,
            expected_realm_slug="realm",
            expected_project_id=PROJECT,
            expected_profile=ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )


def test_obsidian_valid_exclusion_and_file_size_guards(tmp_path: Path) -> None:
    store, bundle, profile = _published_store(tmp_path)
    root, manifest, receipt = _generation_documents(profile, bundle)
    manifest["exclusions"] = [
        {"record_digest": digest("record"), "reason_code": "record-oversized"}
    ]
    manifest_digest, receipt_digest = _write_generation_documents(root, manifest, receipt)
    assert store._verify_generation(
        root,
        expected_realm_slug="realm",
        expected_project_id=PROJECT,
        expected_profile=ObsidianProfile.PUBLIC_SAFE,
        expected_projection_digest=bundle.projection_digest,
        expected_manifest_digest=manifest_digest,
        expected_receipt_digest=receipt_digest,
    )["file_count"] == len(bundle.files)

    root, manifest, receipt = _generation_documents(profile, bundle)
    row = manifest["files"][0]
    target = root / row["relative_path"]
    target.write_bytes(b"x" * (1024 * 1024 + 1))
    row["content_digest"] = digest_of_bytes(target.read_bytes())
    manifest_digest, receipt_digest = _write_generation_documents(root, manifest, receipt)
    with pytest.raises(PolicyViolation, match="file bounded"):
        store._verify_generation(
            root,
            expected_realm_slug="realm",
            expected_project_id=PROJECT,
            expected_profile=ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=manifest_digest,
            expected_receipt_digest=receipt_digest,
        )


def test_obsidian_provenance_drift_after_valid_schema(tmp_path: Path) -> None:
    store, bundle, profile = _published_store(tmp_path)
    root, manifest, receipt = _generation_documents(profile, bundle)
    manifest["schema"] = "wrong"
    manifest_digest, receipt_digest = _write_generation_documents(root, manifest, receipt)
    with pytest.raises(PolicyViolation, match="provenance"):
        store._verify_generation(
            root,
            expected_realm_slug="realm",
            expected_project_id=PROJECT,
            expected_profile=ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=manifest_digest,
            expected_receipt_digest=receipt_digest,
        )

    store, bundle, profile = _published_store(tmp_path / "sizes")
    root = profile / "generations" / bundle.projection_digest.removeprefix("sha256:")
    (root / "_META" / "manifest.json").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(PolicyViolation, match="manifest bounded"):
        store._verify_generation(
            root,
            expected_realm_slug="realm",
            expected_project_id=PROJECT,
            expected_profile=ObsidianProfile.PUBLIC_SAFE,
            expected_projection_digest=bundle.projection_digest,
            expected_manifest_digest=bundle.manifest_digest,
            expected_receipt_digest=bundle.receipt_digest,
        )


def _compaction_receipt() -> CompactionReceipt:
    ids = tuple(uuid4() for _ in range(5))
    return CompactionReceipt(
        receipt_id=ids[0],
        realm_id=ids[1],
        project_id=ids[2],
        work_item_id=ids[3],
        run_id=ids[4],
        session_id="session",
        client_id="codex",
        pre_compaction_event_digest=digest("pre"),
        checkpoint_draft_digest=digest("draft"),
        outbox_ref="outbox",
        outbox_payload_digest=digest("outbox"),
        worker_result_digest=None,
        checkpoint_ref=None,
        checkpoint_digest=None,
        post_compaction_event_digest=None,
        rehydration_receipt_digest=None,
        status=CompactionStatus.PREPARED,
        created_at=NOW,
        completed_at=None,
    )


class _MemoryRepository:
    def __init__(self) -> None:
        self.connection = SimpleNamespace(transaction=lambda: nullcontext())


def _raw_plan(kind: ContinuityReceiptKind, receipt: Any) -> ContinuityReceiptPlan:
    return ContinuityReceiptPlan(
        kind,
        receipt,
        digest("receipt"),
        "key",
        "resource",
        digest("source"),
        digest("policy"),
        digest("migration"),
        digest("context"),
        digest("effect"),
        digest("plan"),
    )


def test_memory_plan_and_store_remaining_type_matrix() -> None:
    receipt = _compaction_receipt()
    close = ContinuityReceiptPlan.create(
        kind=ContinuityReceiptKind.CLOSE,
        receipt=cast(Any, receipt),
        receipt_digest=receipt.receipt_digest,
        idempotency_key="key",
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
        context_digest=digest("context"),
        release_snapshot_digest=digest("release"),
    )
    assert close.body()["schema"].endswith("/v2")
    with pytest.raises(PolicyViolation, match="Close plan"):
        ContinuityReceiptPlan.create(
            kind=ContinuityReceiptKind.CLOSE,
            receipt=cast(Any, receipt),
            receipt_digest=receipt.receipt_digest,
            idempotency_key="key",
            source_digest=digest("source"),
            policy_digest=digest("policy"),
            migration_digest=digest("migration"),
            context_digest=digest("context"),
        )
    with pytest.raises(PolicyViolation, match="Yalniz close"):
        ContinuityReceiptPlan.create(
            kind=ContinuityReceiptKind.COMPACTION,
            receipt=receipt,
            receipt_digest=receipt.receipt_digest,
            idempotency_key="key",
            source_digest=digest("source"),
            policy_digest=digest("policy"),
            migration_digest=digest("migration"),
            context_digest=digest("context"),
            release_snapshot_digest=digest("release"),
        )

    service = MemoryContinuityService(cast(Any, _MemoryRepository()), cast(Any, object()))
    for kind, message in (
        (ContinuityReceiptKind.HYDRATION, "Hydration"),
        (ContinuityReceiptKind.CLOSE, "Close"),
        (ContinuityReceiptKind.COMPACTION, "Compaction"),
    ):
        with pytest.raises(PolicyViolation, match=message):
            service._store(_raw_plan(kind, object()))


def test_memory_reader_contracts_and_idempotency_remaining() -> None:
    for name, method in (
        ("read_projection_release_snapshot", "_release_snapshot"),
        ("read_hydration_inventory", "_hydration_inventory"),
    ):
        repository = _MemoryRepository()
        service = MemoryContinuityService(cast(Any, repository), cast(Any, object()))
        target = getattr(service, method)
        args: tuple[Any, ...] = (_compaction_receipt(),) if method == "_release_snapshot" else ()
        kwargs = (
            {}
            if args
            else {
                "project_id": uuid4(),
                "work_item_id": uuid4(),
                "run_id": uuid4(),
                "session_id": "session",
                "client_id": "codex",
            }
        )
        with pytest.raises(PolicyViolation, match="repository"):
            target(*args, **kwargs)
        setattr(repository, name, lambda **ignored: object())
        with pytest.raises(PolicyViolation, match="contract"):
            target(*args, **kwargs)
    for value in ("", "bad key", "x" * 161):
        with pytest.raises(ValidationFailed):
            memory._idempotency_key(value)


def test_memory_prepare_close_and_apply_kind_guards() -> None:
    repository = _MemoryRepository()
    service = MemoryContinuityService(cast(Any, repository), cast(Any, object()))
    closed = SimpleNamespace(status=SimpleNamespace(value="closed"))
    with pytest.raises(PolicyViolation, match="Closed Work"):
        service.prepare_close(cast(Any, closed), idempotency_key="key")
    open_receipt = SimpleNamespace(status=SimpleNamespace(value="prepared"))
    with pytest.raises(PolicyViolation, match="repository"):
        service.prepare_close(cast(Any, open_receipt), idempotency_key="key")

    for kind, message in (
        (ContinuityReceiptKind.HYDRATION, "Hydration"),
        (ContinuityReceiptKind.CLOSE, "Close"),
    ):
        plan = SimpleNamespace(
            kind=kind,
            receipt=object(),
            assert_integrity=lambda: None,
        )
        with pytest.raises(PolicyViolation, match=message):
            service.apply(cast(Any, plan), authorization_id=uuid4(), now=NOW)

    cast(Any, repository).store_compaction_receipt = lambda receipt, idempotency_key: True
    assert service._store(_raw_plan(ContinuityReceiptKind.COMPACTION, _compaction_receipt()))


def test_memory_apply_rejects_unconsumed_authorization() -> None:
    receipt = _compaction_receipt()
    plan = SimpleNamespace(
        kind=ContinuityReceiptKind.COMPACTION,
        receipt=receipt,
        receipt_digest=receipt.receipt_digest,
        plan_digest=digest("plan"),
        effect_digest=digest("effect"),
        resource="continuity:compaction:receipt",
        assert_integrity=lambda: None,
    )
    authorization = SimpleNamespace(
        rejection_reason=lambda now: None,
        realm_id=receipt.realm_id,
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=SimpleNamespace(
            covers_effect=lambda effect: True,
            covers_resource=lambda resource: True,
        ),
    )
    repository = _MemoryRepository()
    store = SimpleNamespace(
        get=lambda authorization_id: authorization,
        consume=lambda *args, **kwargs: SimpleNamespace(consumed=False, reason="stale"),
    )
    service = MemoryContinuityService(cast(Any, repository), cast(Any, store))
    with pytest.raises(Exception, match="tuketilemedi"):
        service.apply(cast(Any, plan), authorization_id=uuid4(), now=NOW)


def _release_snapshot() -> ProjectionReleaseSnapshot:
    project_id, work_item_id = uuid4(), uuid4()
    work_digest = digest("work")
    database_digest = digest(
        {
            "project_id": str(project_id),
            "work_item_id": str(work_item_id),
            "work_revision": 1,
            "work_state": "completed",
            "work_record_digest": work_digest,
        }
    )
    source_tree = digest("tree")
    projection_source = canonical_projection_source_digest(
        source_head="revision",
        source_tree_digest=source_tree,
        migration_head=1,
        database_revision_digest=database_digest,
    )
    return ProjectionReleaseSnapshot(
        project_id,
        work_item_id,
        1,
        "completed",
        work_digest,
        "revision",
        source_tree,
        1,
        database_digest,
        ACTIVE_WORK_PROJECTION_REF,
        digest("receipt"),
        digest("projection"),
        projection_source,
        True,
        (),
        None,
    )


def test_memory_release_reader_and_hydration_current_matrix() -> None:
    snapshot = _release_snapshot()
    repository = _MemoryRepository()
    cast(Any, repository).read_projection_release_snapshot = lambda **kwargs: snapshot
    service = MemoryContinuityService(cast(Any, repository), cast(Any, object()))
    receipt = SimpleNamespace(
        project_id=snapshot.project_id,
        work_item_id=snapshot.work_item_id,
        run_id=uuid4(),
        session_id="session",
        client_id="codex",
    )
    assert service._release_snapshot(cast(Any, receipt)) is snapshot

    identity = {
        "realm_id": uuid4(),
        "project_id": uuid4(),
        "work_item_id": uuid4(),
        "run_id": uuid4(),
        "session_id": "session",
        "client_id": "codex",
    }
    bindings = {
        "source_digest": digest("source"),
        "policy_digest": digest("policy"),
        "migration_digest": digest("migration"),
        "context_digest": digest("context"),
        "inventory_digest": digest("inventory"),
        "plan_ref": "plan",
        "checkpoint_ref": "checkpoint",
        "hydration_event_digest": digest("event"),
        "projection_refs": (),
        "freshness": (),
    }
    receipt_values = SimpleNamespace(
        **identity,
        **bindings,
        fresh=True,
        complete=True,
        required_selections=(),
        optional_selections=(),
    )
    inventory = SimpleNamespace(**identity, **bindings, entries=())
    MemoryContinuityService._assert_hydration_current(
        cast(Any, receipt_values), cast(Any, inventory)
    )
    drifted_identity = SimpleNamespace(**{**vars(receipt_values), "session_id": "other"})
    with pytest.raises(PolicyViolation, match="replan"):
        MemoryContinuityService._assert_hydration_current(
            cast(Any, drifted_identity), cast(Any, inventory)
        )
    drifted_projection = SimpleNamespace(
        **identity,
        **{**bindings, "projection_refs": (SimpleNamespace(),)},
        fresh=True,
        complete=True,
        required_selections=(),
        optional_selections=(),
    )
    with pytest.raises(PolicyViolation, match="projection"):
        MemoryContinuityService._assert_hydration_current(
            cast(Any, drifted_projection), cast(Any, inventory)
        )
    selected = SimpleNamespace(ref="selected")
    selected_receipt = SimpleNamespace(
        **identity,
        **bindings,
        fresh=True,
        complete=True,
        required_selections=(selected,),
        optional_selections=(),
    )
    with pytest.raises(PolicyViolation, match="selected entry"):
        MemoryContinuityService._assert_hydration_current(
            cast(Any, selected_receipt), cast(Any, inventory)
        )
    required_entry = SimpleNamespace(
        ref="required", required=True, classification=ContinuityClassification.PUBLIC
    )
    required_inventory = SimpleNamespace(**identity, **bindings, entries=(required_entry,))
    with pytest.raises(PolicyViolation, match="required inventory"):
        MemoryContinuityService._assert_hydration_current(
            cast(Any, receipt_values), cast(Any, required_inventory)
        )


def test_backup_remaining_copy_sources_directories_and_manifest_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"data")
    source.chmod(0o600)
    original = source.stat()
    calls = 0

    real_regular = local_backup._regular

    def changed_without_recursion(path: Path) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = real_regular(path)
        if calls == 2:
            values = list(result)
            values[6] = original.st_size + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(local_backup, "_regular", changed_without_recursion)
    with pytest.raises(PolicyViolation, match="changed"):
        local_backup._copy_regular(source, tmp_path / "target", 0o600)
    monkeypatch.setattr(local_backup, "_regular", real_regular)

    home = tmp_path / "home"
    home.mkdir()
    (home / "artifacts").write_text("not-tree")
    with pytest.raises(PolicyViolation, match="tree"):
        local_backup._sources(home)
    (home / "artifacts").unlink()
    (home / "artifacts").mkdir()
    (home / "artifacts" / "keep").write_text("x")
    (home / "artifacts" / "skip-wal").write_text("x")
    sources = local_backup._sources(home)
    assert [item[0] for item in sources] == ["artifacts/keep"]
    (home / "artifacts").chmod(0o722)
    with pytest.raises(PolicyViolation, match="directory"):
        local_backup._directories(home, sources)

    root = tmp_path / "bundle"
    root.mkdir(mode=0o700)
    manifest = root / "MANIFEST.json"
    for document, message in (
        ({"schema": "wrong"}, "schema"),
        (
            {
                "schema": local_backup.BUNDLE_SCHEMA,
                "directories": "bad",
                "entries": [],
                "file_count": 0,
                "total_bytes": 0,
                "grants_authority": False,
                "manifest_digest": digest("bad"),
            },
            "digest",
        ),
    ):
        if manifest.exists():
            manifest.chmod(0o600)
        manifest.write_bytes(canonical_bytes(document))
        manifest.chmod(0o400)
        with pytest.raises(ValidationFailed, match=message):
            local_backup.verify_bundle(root)


def _backup_document(
    directories: object,
    entries: object,
    *,
    file_count: int = 0,
    total_bytes: int = 0,
    authority: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": local_backup.BUNDLE_SCHEMA,
        "directories": directories,
        "entries": entries,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "grants_authority": authority,
    }
    return {**body, "manifest_digest": digest(body)}


def _write_backup_manifest(root: Path, document: dict[str, Any]) -> None:
    path = root / "MANIFEST.json"
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(canonical_bytes(document))
    path.chmod(0o400)


def test_backup_snapshot_bounds_and_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(b"x")
    source.chmod(0o600)
    monkeypatch.setattr(local_backup, "MAX_DATABASE_BYTES", 0)
    with pytest.raises(PolicyViolation, match="exceeds bound"):
        local_backup._snapshot_database(source, tmp_path / "target.db", 0o600)

    monkeypatch.setattr(local_backup, "MAX_DATABASE_BYTES", 1024)
    fake = SimpleNamespace(
        backup=lambda target: None,
        commit=lambda: None,
        execute=lambda sql: SimpleNamespace(fetchone=lambda: None),
        close=lambda: None,
    )
    monkeypatch.setattr("zekam.infrastructure.local_backup.sqlite3.connect", lambda *a, **k: fake)
    with pytest.raises(PolicyViolation, match="not integral"):
        local_backup._snapshot_database(source, tmp_path / "target.db", 0o600)

    regular = local_backup._regular
    calls = 0

    def oversized_target(path: Path) -> Any:
        nonlocal calls
        calls += 1
        result = regular(source)
        return SimpleNamespace(st_size=2048) if calls > 1 else result

    fake.execute = lambda sql: SimpleNamespace(fetchone=lambda: ("ok",))

    def fake_connect(target: object, **kwargs: object) -> Any:
        if isinstance(target, Path):
            target.write_bytes(b"sqlite")
        return fake

    monkeypatch.setattr("zekam.infrastructure.local_backup.sqlite3.connect", fake_connect)
    monkeypatch.setattr(local_backup, "_regular", oversized_target)
    with pytest.raises(PolicyViolation, match="snapshot exceeds"):
        local_backup._snapshot_database(source, tmp_path / "target-two.db", 0o600)


def test_backup_source_count_and_create_preconditions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "artifacts").mkdir(parents=True)
    (home / "artifacts" / "one").write_text("x")
    monkeypatch.setattr(local_backup, "MAX_FILES", 0)
    with pytest.raises(PolicyViolation, match="file count"):
        local_backup._sources(home)
    monkeypatch.setattr(local_backup, "MAX_FILES", 100_000)

    services = SimpleNamespace(status=lambda: {"all_ready": True})
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PolicyViolation, match="mandatory file"):
        local_backup.create_bundle(cast(Any, services), empty, tmp_path / "bundle-one")
    monkeypatch.setattr(local_backup, "_REQUIRED_DATABASES", frozenset())
    monkeypatch.setattr(local_backup, "_REQUIRED_FILES", frozenset())
    with pytest.raises(PolicyViolation, match="mandatory directory"):
        local_backup.create_bundle(cast(Any, services), empty, tmp_path / "bundle-two")


@pytest.mark.parametrize(
    ("directories", "entries", "message"),
    [
        ("bad", [], "directory census"),
        ([{"bad": True}], [], "directory schema"),
        ([{"path": "directory", "mode": 0o722}], [], "directory mode"),
        ([], "bad", "entry census"),
        ([], [{"bad": True}], "entry schema"),
        (
            [],
            [
                {
                    "path": "config.yaml",
                    "kind": "bad",
                    "mode": 0o600,
                    "size_bytes": 1,
                    "sha256": "a" * 64,
                }
            ],
            "entry kind",
        ),
        (
            [],
            [
                {
                    "path": "config.yaml",
                    "kind": "file",
                    "mode": 0o622,
                    "size_bytes": 1,
                    "sha256": "a" * 64,
                }
            ],
            "entry mode",
        ),
        (
            [],
            [
                {
                    "path": "config.yaml",
                    "kind": "file",
                    "mode": 0o600,
                    "size_bytes": -1,
                    "sha256": "a" * 64,
                }
            ],
            "entry size",
        ),
        (
            [],
            [
                {
                    "path": "config.yaml",
                    "kind": "file",
                    "mode": 0o600,
                    "size_bytes": 1,
                    "sha256": "bad",
                }
            ],
            "entry digest",
        ),
    ],
)
def test_backup_verify_remaining_schema_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directories: object,
    entries: object,
    message: str,
) -> None:
    root = tmp_path / "bundle"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(local_backup, "_REQUIRED_DIRECTORIES", frozenset())
    monkeypatch.setattr(local_backup, "_REQUIRED_DATABASES", frozenset())
    monkeypatch.setattr(local_backup, "_REQUIRED_FILES", frozenset())
    _write_backup_manifest(root, _backup_document(directories, entries))
    with pytest.raises(ValidationFailed, match=message):
        local_backup.verify_bundle(root)


def test_backup_verify_root_manifest_and_directory_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bundle"
    root.mkdir(mode=0o755)
    _write_backup_manifest(root, _backup_document([], []))
    with pytest.raises(PolicyViolation, match="root identity"):
        local_backup.verify_bundle(root)
    root.chmod(0o700)
    (root / "MANIFEST.json").chmod(0o600)
    with pytest.raises(PolicyViolation, match="manifest mode"):
        local_backup.verify_bundle(root)
    (root / "MANIFEST.json").chmod(0o400)

    monkeypatch.setattr(local_backup, "_REQUIRED_DIRECTORIES", frozenset())
    monkeypatch.setattr(local_backup, "_REQUIRED_DATABASES", frozenset())
    monkeypatch.setattr(local_backup, "_REQUIRED_FILES", frozenset())
    directory = root / "directory"
    directory.mkdir(mode=0o700)
    _write_backup_manifest(root, _backup_document([{"path": "directory", "mode": 0o700}], []))
    directory.chmod(0o755)
    with pytest.raises(PolicyViolation, match="directory drift"):
        local_backup.verify_bundle(root)


def test_backup_verify_unsorted_and_required_directory_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bundle"
    root.mkdir(mode=0o700)
    for name in ("a", "z"):
        (root / name).mkdir(mode=0o700)
    monkeypatch.setattr(local_backup, "_REQUIRED_DIRECTORIES", frozenset())
    _write_backup_manifest(
        root,
        _backup_document([{"path": "z", "mode": 0o700}, {"path": "a", "mode": 0o700}], []),
    )
    with pytest.raises(PolicyViolation, match="directory census drift"):
        local_backup.verify_bundle(root)

    for name in ("a", "z"):
        (root / name).rmdir()
    monkeypatch.setattr(local_backup, "_REQUIRED_DIRECTORIES", frozenset({"required"}))
    _write_backup_manifest(root, _backup_document([], []))
    with pytest.raises(PolicyViolation, match="mandatory directory"):
        local_backup.verify_bundle(root)


def test_backup_verify_empty_census_totals_authority_and_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bundle"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(local_backup, "_REQUIRED_DIRECTORIES", frozenset())
    monkeypatch.setattr(local_backup, "_REQUIRED_DATABASES", frozenset())
    monkeypatch.setattr(local_backup, "_REQUIRED_FILES", frozenset())
    _write_backup_manifest(root, _backup_document([], []))
    assert local_backup.verify_bundle(root)["file_count"] == 0

    extra = root / "extra"
    extra.write_text("x")
    with pytest.raises(PolicyViolation, match="file census"):
        local_backup.verify_bundle(root)
    extra.unlink()

    for document, message in (
        (_backup_document([], [], file_count=1), "totals"),
        (_backup_document([], [], total_bytes=1), "totals"),
        (_backup_document([], [], authority=True), "grant authority"),
    ):
        _write_backup_manifest(root, document)
        with pytest.raises(PolicyViolation, match=message):
            local_backup.verify_bundle(root)


def test_backup_create_aggregate_bound_and_directory_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("x")
    config.chmod(0o600)
    monkeypatch.setattr(local_backup, "_REQUIRED_DATABASES", frozenset())
    monkeypatch.setattr(local_backup, "_REQUIRED_FILES", frozenset())
    monkeypatch.setattr(local_backup, "_REQUIRED_DIRECTORIES", frozenset())
    monkeypatch.setattr(local_backup, "MAX_TOTAL_BYTES", 0)
    services = SimpleNamespace(status=lambda: {"all_ready": True})
    with pytest.raises(PolicyViolation, match="aggregate"):
        local_backup.create_bundle(cast(Any, services), home, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()

    root = tmp_path / "verified"
    root.mkdir(mode=0o700)
    unlisted = root / "directory"
    unlisted.mkdir()
    _write_backup_manifest(root, _backup_document([], []))
    with pytest.raises(PolicyViolation, match="directory census"):
        local_backup.verify_bundle(root)
