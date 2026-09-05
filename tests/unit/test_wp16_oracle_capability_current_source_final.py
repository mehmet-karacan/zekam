# mypy: disable-error-code="arg-type,attr-defined,misc,no-any-return"
from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from tests.unit import test_model_capability_benchmark as cap_fixture
from tests.unit import test_project_embedding_apply_boundary as embedding_fixture

from zekam.application import model_capability_benchmark as cap
from zekam.application import oracle_metadata_index as oracle
from zekam.application.embedding_provider import (
    EmbeddingBatch,
    EmbeddingPolicy,
    EmbeddingPurpose,
    EmbeddingReceipt,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.retrieval import EmbeddingProfile as IndexEmbeddingProfile
from zekam.domain.security import DataClassification

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 9, 5, tzinfo=dt.UTC)


def _response() -> tuple[Any, cap.CapabilityVerifier, cap.CapabilityResponse]:
    registry, profile, fixtures = cap_fixture._loaded()
    task = registry.tasks[0]
    response = cap_fixture.FakeAdapter().execute(
        model_id="tested",
        task=task,
        fixture=fixtures[task.task_digest],
        profile=profile,
        turn_index=1,
        prior_response_digest=None,
        cancellation=threading.Event(),
    )
    verifier = cap.CapabilityVerifier(
        "independent", "separate", profile.evaluator_provenance_digest
    )
    response = replace(
        response,
        acceptance_evidence_digest=cap.capability_acceptance_evidence_digest(
            task, response, verifier.provenance_digest
        ),
    )
    return task, verifier, response


@pytest.mark.parametrize("case", ("artifact", "evidence", "status"))
def test_capability_verifier_rejects_exact_response_drift(case: str) -> None:
    task, verifier, response = _response()
    payload = dict(response.payload)
    if case == "artifact":
        payload["artifact_digest"] = 1
        response = replace(response, payload=payload)
    elif case == "status":
        payload["status"] = None
        response = replace(response, payload=payload)
        response = replace(
            response,
            acceptance_evidence_digest=cap.capability_acceptance_evidence_digest(
                task, response, verifier.provenance_digest
            ),
        )
    else:
        response = replace(response, acceptance_evidence_digest=digest("wrong"))
    with pytest.raises((PolicyViolation, ValidationFailed)):
        verifier.verify(tested_model_id="tested", task=task, response=response)


def test_capability_string_auc_and_duration_boundaries() -> None:
    with pytest.raises(ValidationFailed):
        cap._string_tuple(["ok", 1], "markers")
    receipt = cap.CapabilityCheckpointReceipt("done", 1, digest("artifact"), 1, 1)
    assert cap._time_weighted_progress_auc(0, (receipt,)) == 1.0
    assert cap._time_weighted_progress_auc(0, (replace(receipt, acceptance_passed=0),)) == 0.0
    with pytest.raises(ValidationFailed):
        cap._time_weighted_progress_auc(1, (replace(receipt, elapsed_ms=2),))


def _registry() -> dict[str, Any]:
    return yaml.safe_load(
        (ROOT / "config" / "model_capability_benchmark.yaml").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "case",
    (
        "document",
        "profile",
        "tasks",
        "task",
        "routes",
        "fixture",
        "binding",
        "checks",
        "check-shape",
        "check-value",
        "check-binding",
        "scenario",
    ),
)
def test_capability_registry_rejects_each_nested_contract(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    document = _registry()
    fixture_path = ROOT / str(document["tasks"][0]["fixture_source"])
    fixture = cap.json.loads(fixture_path.read_text(encoding="utf-8"))
    loaded: object = document
    if case == "document":
        loaded = []
    elif case == "profile":
        document["execution_profile"] = []
    elif case == "tasks":
        document["tasks"] = {}
    elif case == "task":
        document["tasks"][0]["extra"] = True
    elif case == "routes":
        document["tasks"][0]["route_dimensions"] = []
    elif case == "fixture":
        fixture["extra"] = True
    elif case == "binding":
        fixture["task_id"] = "other"
    elif case == "checks":
        fixture["hidden_acceptance_checks"] = []
    elif case == "check-shape":
        fixture["hidden_acceptance_checks"][0]["extra"] = True
    elif case == "check-value":
        fixture["hidden_acceptance_checks"][0]["any_of"] = []
    elif case == "check-binding":
        fixture["expected_markers"] = ["different"]
    else:
        fixture["scenario"] = ""
    monkeypatch.setattr(cap.yaml, "safe_load", lambda _text: loaded)
    monkeypatch.setattr(cap.json, "loads", lambda _text: fixture)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        cap.load_capability_registry(
            ROOT / "config" / "model_capability_benchmark.yaml", repository_root=ROOT
        )


def test_capability_registry_escape_unknown_route_and_runner_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _registry()
    document["tasks"][0]["fixture_source"] = "../outside.json"
    monkeypatch.setattr(cap.yaml, "safe_load", lambda _text: document)
    with pytest.raises(PolicyViolation):
        cap.load_capability_registry(
            ROOT / "config" / "model_capability_benchmark.yaml", repository_root=ROOT
        )
    with pytest.raises(ValidationFailed):
        cap._route_dimensions(["unknown"])
    monkeypatch.undo()
    plan = cap_fixture._plan()
    verifier = cap.CapabilityVerifier(
        "independent", "separate", plan.execution_profile.evaluator_provenance_digest
    )
    with pytest.raises(ValidationFailed):
        cap.CapabilityCohortRunner(cap_fixture.FakeAdapter(), verifier, timeout_scale=0)
    runner = cap.CapabilityCohortRunner(cap_fixture.FakeAdapter(), verifier)
    with pytest.raises(PolicyViolation):
        runner.run(replace(plan, max_parallelism=1), {})
    wrong = cap.CapabilityCohortRunner(
        cap_fixture.FakeAdapter(), replace(verifier, provenance_digest=digest("wrong"))
    )
    with pytest.raises(PolicyViolation):
        wrong.run(plan, {})
    with pytest.raises(PolicyViolation):
        runner._run_lane(
            plan,
            plan.registry.tasks[0],
            plan.model_ids[0],
            {},
            threading.Barrier(1),
            threading.Event(),
        )


class _FaultAdapter(cap_fixture.FakeAdapter):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def execute(self, **kwargs: Any) -> cap.CapabilityResponse:
        response = super().execute(**kwargs)
        names = ("forbidden",) if self.mode == "forbidden" else ("read",) * 100
        receipts = tuple(
            cap.CapabilityToolReceipt(name, digest((name, index)))
            for index, name in enumerate(names)
        )
        return replace(response, tool_receipts=receipts)


@pytest.mark.parametrize("mode", ("forbidden", "budget"))
def test_capability_lane_cancels_forbidden_and_over_budget_tools(mode: str) -> None:
    plan = cap_fixture._plan(("model-a",))
    _, _, fixtures = cap_fixture._loaded()
    verifier = cap.CapabilityVerifier(
        "independent", "separate", plan.execution_profile.evaluator_provenance_digest
    )
    cancellation = threading.Event()
    with pytest.raises(PolicyViolation):
        cap.CapabilityCohortRunner(_FaultAdapter(mode), verifier)._run_lane(
            plan,
            plan.registry.tasks[0],
            "model-a",
            fixtures,
            threading.Barrier(1),
            cancellation,
        )
    assert cancellation.is_set()


def test_capability_lane_rejects_zero_turn_runtime_profile() -> None:
    plan = cap_fixture._plan(("model-a",))
    _, _, fixtures = cap_fixture._loaded()
    object.__setattr__(plan.execution_profile, "max_model_turns", 0)
    verifier = cap.CapabilityVerifier(
        "independent", "separate", plan.execution_profile.evaluator_provenance_digest
    )
    with pytest.raises(PolicyViolation, match="response uretmedi"):
        cap.CapabilityCohortRunner(cap_fixture.FakeAdapter(), verifier)._run_lane(
            plan,
            plan.registry.tasks[0],
            "model-a",
            fixtures,
            threading.Barrier(1),
            threading.Event(),
        )


def _plan() -> Any:
    ddl = "CREATE TABLE GPU_APP.PRODUCT (ID NUMBER NOT NULL)"
    item = oracle.OracleDdlObject(
        "GPU_APP",
        "PRODUCT",
        "TABLE",
        "VALID",
        "2026-09-05T00:00:00",
        digest_of_bytes(ddl.encode()),
        ddl,
    )
    snapshot = oracle.OracleMetadataSnapshot(
        "GPU_APP", digest("connection"), digest("database"), (item,), 0
    )
    plan = oracle.build_oracle_metadata_index_plan(
        project_id=uuid4(), project_slug="gpu", snapshot=snapshot
    )
    profile = embedding_fixture._profile()
    return replace(
        plan,
        embedding_profile=IndexEmbeddingProfile(
            "openai/BAAI/bge-m3", 1024, provider_profile_digest=profile.profile_digest
        ),
    )


class _Provider:
    def __init__(self, vectors: int | None = None, receipt_count: int | None = None) -> None:
        self.profile = embedding_fixture._profile()
        self.vectors = vectors
        self.receipt_count = receipt_count

    def describe(self) -> Any:
        return self.profile

    def embed_documents(self, texts: tuple[str, ...], policy: EmbeddingPolicy) -> EmbeddingBatch:
        self.profile.assert_policy(policy)
        count = len(texts) if self.vectors is None else self.vectors
        vectors = tuple((1.0,) + (0.0,) * 1023 for _ in range(count))
        receipt_count = len(texts) if self.receipt_count is None else self.receipt_count
        receipt = EmbeddingReceipt(
            EmbeddingPurpose.DOCUMENT,
            self.profile.profile_digest,
            digest(texts),
            digest(vectors),
            max(1, receipt_count),
            1024,
            1,
            1,
        )
        if receipt_count < 1:
            object.__setattr__(receipt, "vector_count", receipt_count)
            batch = object.__new__(EmbeddingBatch)
            object.__setattr__(batch, "vectors", vectors)
            object.__setattr__(batch, "receipt", receipt)
            return batch
        return EmbeddingBatch(vectors, receipt)


def _policy(provider: _Provider) -> EmbeddingPolicy:
    return EmbeddingPolicy(DataClassification.LOCAL_ONLY, provider.profile.profile_digest)


class _Store:
    def __init__(self, drift: bool = False) -> None:
        self.drift = drift

    def ensure(self) -> _Store:
        return self

    def put(self, payload: bytes, *, media_type: str) -> Any:
        del media_type
        return SimpleNamespace(digest=digest("wrong") if self.drift else digest_of_bytes(payload))


class _Connection:
    row: Any = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def cursor(self) -> _Connection:
        return self

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, *_args: object) -> None:
        return None

    def fetchone(self) -> Any:
        return self.row


class _Knowledge:
    def __init__(self, equivalent: Any = None, previous: UUID | None = None) -> None:
        self.equivalent = equivalent
        self.previous = previous
        self.source_id, self.version_id, self.document_id = uuid4(), uuid4(), uuid4()
        self.superseded: tuple[UUID, UUID] | None = None

    def store_artifact(self, *_args: object) -> UUID:
        return uuid4()

    def register_source(self, *_args: object, **_kwargs: object) -> UUID:
        return self.source_id

    def equivalent_version(self, *_args: object, **_kwargs: object) -> Any:
        return self.equivalent

    def next_revision(self, *_args: object) -> int:
        return 2

    def start_job(self, *_args: object, **_kwargs: object) -> UUID:
        return uuid4()

    def store_version(self, *_args: object, **_kwargs: object) -> UUID:
        return self.version_id

    def store_document(self, *_args: object, **_kwargs: object) -> UUID:
        return self.document_id

    def save_progress(self, *_args: object, **_kwargs: object) -> None:
        pass

    def active_version(self, *_args: object) -> UUID | None:
        return self.previous

    def supersede_version(self, old: UUID, new: UUID) -> None:
        self.superseded = (old, new)

    def activate_version(self, *_args: object) -> None:
        pass


class _Retrieval:
    def __init__(self) -> None:
        self.embeddings = 0

    def store_chunk_profile(self, *_args: object, **_kwargs: object) -> UUID:
        return uuid4()

    def store_embedding_profile(self, *_args: object, **_kwargs: object) -> UUID:
        return uuid4()

    def store_chunks(self, chunks: tuple[Any, ...], **_kwargs: object) -> dict[str, UUID]:
        return {chunk.chunk_id: uuid4() for chunk in chunks}

    def store_embedding(self, *_args: object, **_kwargs: object) -> None:
        self.embeddings += 1

    def store_document_profiles(self, **_kwargs: object) -> None:
        pass


def _apply(plan: Any, provider: _Provider, **kwargs: Any) -> Any:
    return oracle.apply_oracle_metadata_index(
        plan,
        connection=kwargs.get("connection", _Connection()),
        knowledge=kwargs.get("knowledge", _Knowledge()),
        retrieval=kwargs.get("retrieval", _Retrieval()),
        object_store=kwargs.get("store", _Store()),
        embedding_provider=provider,
        embedding_policy=_policy(provider),
        now=NOW,
    )


def test_oracle_provider_profile_batch_manifest_and_cas_guards() -> None:
    plan = _plan()
    with pytest.raises(PolicyViolation):
        oracle.apply_oracle_metadata_index(
            plan,
            connection=object(),
            knowledge=object(),
            retrieval=object(),
            object_store=_Store(),
        )
    provider = _Provider()
    with pytest.raises(PolicyViolation):
        _apply(
            replace(
                plan,
                embedding_profile=replace(
                    plan.embedding_profile, provider_profile_digest=digest("wrong")
                ),
            ),
            provider,
        )
    for bad in (_Provider(vectors=0), _Provider(receipt_count=0)):
        with pytest.raises(PolicyViolation):
            _apply(plan, bad)
    with pytest.raises(PolicyViolation):
        _apply(replace(plan, manifest=plan.manifest + b" "), provider)
    with pytest.raises(PolicyViolation):
        _apply(plan, provider, store=_Store(drift=True))


@pytest.mark.parametrize("previous", (None, "present"))
def test_oracle_new_version_persists_vectors_and_conditionally_supersedes(
    previous: str | None,
) -> None:
    plan, provider = _plan(), _Provider()
    prior = uuid4() if previous else None
    knowledge, retrieval = _Knowledge(previous=prior), _Retrieval()
    result = _apply(plan, provider, knowledge=knowledge, retrieval=retrieval)
    assert result.revision == 2
    assert retrieval.embeddings == len(plan.chunks)
    assert knowledge.superseded == ((prior, knowledge.version_id) if prior else None)


@pytest.mark.parametrize("mode", ("missing", "profile", "valid"))
def test_oracle_active_replay_requires_complete_exact_index(mode: str) -> None:
    plan, provider = _plan(), _Provider()
    knowledge = _Knowledge(equivalent=(uuid4(), 7, "active"))
    connection = _Connection()
    connection.row = (
        None
        if mode == "missing"
        else (
            uuid4(),
            len(plan.chunks),
            len(plan.chunks),
            digest("wrong") if mode == "profile" else plan.embedding_profile.profile_digest,
        )
    )
    if mode == "valid":
        assert _apply(plan, provider, knowledge=knowledge, connection=connection).revision == 7
    else:
        with pytest.raises(PolicyViolation):
            _apply(plan, provider, knowledge=knowledge, connection=connection)
