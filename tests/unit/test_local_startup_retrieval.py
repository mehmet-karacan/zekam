"""Actual bounded Akilli Kasa text; fixture vectors are NOT semantic embeddings."""

from __future__ import annotations

import json
import math
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.application.local_startup_retrieval import LocalStartupRetrieval
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed
from zekam.domain.knowledge import Locator
from zekam.domain.retrieval import RetrievalChannel, ScoredHit
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex

ROOT = Path("/Users/mkaracan/Projeler/akilli-kasa")
HEALTH = "src/akilli_kasa/api/saglik.py"
ADR = "belgeler/kararlar/ADR-0006-idempotent-dosya-ice-aktarma.md"
PROJECT = "akilli-kasa"


class ObservedIndex:
    def __init__(self, index: SQLiteKnowledgeIndex) -> None:
        self.index = index
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.overrides: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            if name in {"dense", "health", "describe", "embed_query", "embed_documents"}:
                pytest.fail("Provider/dense invocation is forbidden in startup retrieval")
            self.calls.append((name, args, kwargs))
            if name in self.overrides:
                return self.overrides[name](*args, **kwargs)
            return getattr(self.index, name)(*args, **kwargs)

        return call


def _build(
    index: SQLiteKnowledgeIndex,
    records: tuple[KnowledgeIndexRecord, ...],
    tree: str,
    profile: str = "test-vectors-only-no-semantic-claim",
) -> Any:
    return index.build_generation(
        records,
        project_id=PROJECT,
        source_revision=records[0].source_revision,
        tree_digest=tree,
        source_manifest_digest=digest([(r.source_path, r.source_digest) for r in records]),
        embedding_profile_digest=digest(profile),
        provider_profile_digest=digest("test-index-only-profile"),
        created_at="2026-09-02T00:00:00Z",
    )


@pytest.fixture
def retrieval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    if not all((ROOT / path).is_file() for path in (HEALTH, ADR)):
        pytest.skip("Actual bounded read-only Akilli Kasa corpus unavailable")

    def no_network(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Startup retrieval cannot access providers/network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    records = []
    for order, path in enumerate((HEALTH, ADR)):
        payload = (ROOT / path).read_bytes()
        text = payload.decode("utf-8")
        # Deliberately nonsemantic storage fixtures; no dense channel is ever exercised.
        vector = tuple(1.0 if slot == order else 0.0 for slot in range(1024))
        records.append(
            KnowledgeIndexRecord(
                chunk_id=f"real-{order}",
                project_id=PROJECT,
                source_revision=revision,
                source_path=path,
                source_digest=digest_of_bytes(payload),
                locator=Locator(relative_path=path, line_start=1, line_end=len(text.splitlines())),
                text=text,
                content_digest=digest_of_bytes(payload),
                chunk_order=order,
                vector=vector,
            )
        )
    records.append(replace(records[0], chunk_id="real-health-duplicate", chunk_order=2))
    tree = digest([(item.source_path, item.source_digest) for item in records])
    with SQLiteKnowledgeIndex(tmp_path / "knowledge.sqlite3", create=True) as index:
        generation = _build(index, tuple(records), tree)
        observed = ObservedIndex(index)
        yield {
            "index": index,
            "observed": observed,
            "service": LocalStartupRetrieval(observed),
            "records": tuple(records),
            "generation": generation,
            "kwargs": {
                "project_id": PROJECT,
                "expected_source_revision": revision,
                "expected_tree_digest": tree,
            },
        }


def test_actual_exact_lexical_rrf_dedupe_and_resolver_required(retrieval: Any) -> None:
    result = retrieval["service"].query(HEALTH, **retrieval["kwargs"])
    assert result["state"] == "candidates-require-source-verification"
    assert result["dense"] == "not-invoked"
    assert result["provider_called"] is result["source_bytes_verified"] is False
    assert result["grants_authority"] is result["answer_generated"] is False
    assert result["resolver_required"] is True
    assert result["searched_channels"] == ["exact", "lexical"]
    assert (
        result["generation"]["provider_profile_digest"]
        == retrieval["generation"].provider_profile_digest
    )
    assert len(result["fragments"]) == 1  # same actual content indexed twice, included once
    item = result["fragments"][0]
    assert item["text"] == (ROOT / HEALTH).read_text()
    assert item["locator"]["relative_path"] == item["source_ref"] == HEALTH
    assert item["locator"]["line_start"] == 1
    assert item["exact_match"] is True
    assert set(item["ranks"]) == {"exact", "lexical"}
    assert math.isclose(item["rrf_score"], sum(1 / (60 + rank) for rank in item["ranks"].values()))
    assert result["token_count"] == len(item["text"].encode())
    for name, _, kwargs in retrieval["observed"].calls:
        if name != "generation":
            assert kwargs["generation_digest"] == retrieval["generation"].generation_digest


def test_actual_lexical_only_candidate_and_no_answer_for_low_evidence(retrieval: Any) -> None:
    found = retrieval["service"].query("durum uygulama surum", **retrieval["kwargs"])
    assert found["state"] == "candidates-require-source-verification"
    assert found["fragments"][0]["exact_match"] is False
    assert found["fragments"][0]["channels"] == ["lexical"]
    for query in ("kozmik galaksi bulunamayan", "durum kozmik galaksi uzaklik bilinmeyen"):
        result = retrieval["service"].query(query, **retrieval["kwargs"])
        assert result["state"] == "abstained-insufficient-evidence"
        assert result["fragments"] == []


def test_whole_chunk_budget_never_truncates_exact_source(retrieval: Any) -> None:
    text = (ROOT / HEALTH).read_bytes()
    short = retrieval["service"].query(HEALTH, token_budget=len(text) - 1, **retrieval["kwargs"])
    assert short["fragments"] == []
    assert short["reason"] == "budget-exhausted"
    exact = retrieval["service"].query(
        HEALTH, token_budget=len(text), limit=1, **retrieval["kwargs"]
    )
    assert exact["fragments"][0]["text"].encode() == text


@pytest.mark.parametrize(
    "field,value",
    [
        ("query", None),
        ("query", []),
        ("query", ""),
        ("query", "  "),
        ("query", "x" * 16385),
        ("query", "\x00"),
        ("project_id", None),
        ("project_id", "../foreign"),
        ("expected_source_revision", ""),
        ("expected_tree_digest", None),
        ("expected_tree_digest", "wrong"),
        ("limit", True),
        ("limit", 0),
        ("limit", 9),
        ("limit", "1"),
        ("token_budget", None),
        ("token_budget", 0),
        ("token_budget", 16385),
    ],
)
def test_invalid_request_fails_before_index_calls(retrieval: Any, field: str, value: Any) -> None:
    kwargs = {"query": HEALTH, **retrieval["kwargs"], field: value}
    with pytest.raises((ValidationFailed, PolicyViolation)):
        retrieval["service"].query(**kwargs)
    assert retrieval["observed"].calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"source_revision": "stale"},
        {"tree_digest": digest("other-tree")},
        {"state": "building"},
        {"state": "superseded"},
        {"chunk_count": 0},
    ],
)
def test_missing_or_stale_generation_is_explicitly_degraded(retrieval: Any, change: Any) -> None:
    retrieval["observed"].overrides["generation"] = lambda *_: replace(
        retrieval["generation"], **change
    )
    result = retrieval["service"].query(HEALTH, **retrieval["kwargs"])
    assert result["state"] == "abstained-index-unavailable"
    assert result["searched_channels"] == result["fragments"] == []


@pytest.mark.parametrize("method", ["generation", "exact", "lexical", "views", "source_identity"])
def test_index_timeout_does_not_return_partial_candidates(retrieval: Any, method: str) -> None:
    def timeout(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("Synthetic unavailable index; never a provider invocation")

    retrieval["observed"].overrides[method] = timeout
    result = retrieval["service"].query(HEALTH, **retrieval["kwargs"])
    assert result["state"] == "abstained-index-unavailable"
    assert result["fragments"] == []


def test_missing_generation_and_programmer_errors_are_not_conflated(retrieval: Any) -> None:
    def missing(*args: Any, **kwargs: Any) -> Any:
        raise NotFound("No generation")

    retrieval["observed"].overrides["generation"] = missing
    assert (
        retrieval["service"].query(HEALTH, **retrieval["kwargs"])["reason"]
        == "generation-unavailable"
    )

    def programmer(*args: Any, **kwargs: Any) -> Any:
        raise AttributeError("bad adapter implementation")

    retrieval["observed"].overrides["generation"] = programmer
    with pytest.raises(AttributeError):
        retrieval["service"].query(HEALTH, **retrieval["kwargs"])


def test_actual_empty_index_is_degraded_without_obscuring_corruption(
    retrieval: Any, tmp_path: Path
) -> None:
    with SQLiteKnowledgeIndex(tmp_path / "empty.sqlite3", create=True) as empty:
        result = LocalStartupRetrieval(ObservedIndex(empty)).query(HEALTH, **retrieval["kwargs"])
    assert result["reason"] == "generation-unavailable"

    def corrupt(*args: Any, **kwargs: Any) -> Any:
        raise ValidationFailed("Knowledge generation state gecersiz")

    retrieval["observed"].overrides["generation"] = corrupt
    with pytest.raises(ValidationFailed, match="state gecersiz"):
        retrieval["service"].query(HEALTH, **retrieval["kwargs"])


@pytest.mark.parametrize(
    "variant", ["missing", "foreign-key", "wrong-id", "content", "locator", "generation"]
)
def test_forged_or_missing_views_fail_closed(retrieval: Any, variant: str) -> None:
    def changed(*args: Any, **kwargs: Any) -> Any:
        views = retrieval["index"].views(*args, **kwargs)
        key = next(iter(views))
        if variant == "missing":
            views.pop(key)
        elif variant == "foreign-key":
            views["foreign"] = views[key]
        elif variant == "wrong-id":
            views[key] = replace(views[key], chunk_id="foreign")
        elif variant == "content":
            views[key] = replace(views[key], text="forged")
        elif variant == "locator":
            views[key] = replace(views[key], locator=Locator(relative_path=HEALTH))
        else:
            views[key] = replace(views[key], document_id="other-generation")
        return views

    retrieval["observed"].overrides["views"] = changed
    with pytest.raises((PolicyViolation, ValidationFailed)):
        retrieval["service"].query(HEALTH, **retrieval["kwargs"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_id", digest("foreign-project")),
        ("source_ref", ADR),
        ("source_revision", "stale"),
        ("source_digest", "broken"),
        ("content_digest", digest("foreign-content")),
    ],
)
def test_forged_source_identity_is_rejected(retrieval: Any, field: str, value: str) -> None:
    def changed(*args: Any, **kwargs: Any) -> Any:
        identity = retrieval["index"].source_identity(*args, **kwargs)
        identity[field] = value
        return identity

    retrieval["observed"].overrides["source_identity"] = changed
    with pytest.raises((PolicyViolation, ValidationFailed)):
        retrieval["service"].query(HEALTH, **retrieval["kwargs"])


@pytest.mark.parametrize(
    "variant", ["duplicate", "nan", "wrong-channel", "bool-rank", "forged-exact"]
)
def test_channel_contract_and_exact_evidence_are_independently_checked(
    retrieval: Any, variant: str
) -> None:
    first = ScoredHit("real-0", RetrievalChannel.EXACT, 1, 1.0)
    values = {
        "duplicate": (first, replace(first, rank=2)),
        "nan": (replace(first, raw_score=float("nan")),),
        "wrong-channel": (replace(first, channel=RetrievalChannel.DENSE),),
        "bool-rank": (replace(first, rank=True),),
        "forged-exact": (replace(first, chunk_id="real-1"),),
    }
    retrieval["observed"].overrides["exact"] = lambda *args, **kwargs: values[variant]
    with pytest.raises((PolicyViolation, ValidationFailed)):
        retrieval["service"].query(HEALTH, **retrieval["kwargs"])


def test_generation_swap_is_pinned_then_rejected_without_mixed_candidates(retrieval: Any) -> None:
    old = retrieval["generation"].generation_digest

    def swap(*args: Any, **kwargs: Any) -> Any:
        hits = retrieval["index"].lexical(*args, **kwargs)
        _build(
            retrieval["index"],
            tuple(replace(r, chunk_id=r.chunk_id + "-next") for r in retrieval["records"]),
            retrieval["kwargs"]["expected_tree_digest"],
            profile="new-fixture-profile-no-semantic-claim",
        )
        return hits

    retrieval["observed"].overrides["lexical"] = swap
    result = retrieval["service"].query(HEALTH, **retrieval["kwargs"])
    assert result["state"] == "abstained-index-unavailable"
    assert result["reason"] == "generation-changed"
    assert result["fragments"] == []
    assert all(
        kwargs.get("generation_digest", old) == old for _, _, kwargs in retrieval["observed"].calls
    )


def test_restart_uses_existing_index_without_provider(retrieval: Any) -> None:
    path = retrieval["index"].path
    first = retrieval["service"].query("ADR-0006", **retrieval["kwargs"])
    with SQLiteKnowledgeIndex(path) as reopened:
        second = LocalStartupRetrieval(ObservedIndex(reopened)).query(
            "ADR-0006", **retrieval["kwargs"]
        )
    assert first == second
    assert second["fragments"][0]["source_ref"] == ADR


def test_real_process_restart_returns_same_candidates_without_network(retrieval: Any) -> None:
    expected = retrieval["service"].query("ADR-0006", **retrieval["kwargs"])
    script = """
import json, socket, sys
from pathlib import Path
from zekam.application.local_startup_retrieval import LocalStartupRetrieval
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex
def forbidden(*args, **kwargs):
    raise AssertionError('Provider/network/dense not allowed')
socket.socket.connect = forbidden
SQLiteKnowledgeIndex.dense = forbidden
request = json.load(sys.stdin)
with SQLiteKnowledgeIndex(Path(request['path'])) as index:
    before = index._connection.total_changes
    result = LocalStartupRetrieval(index).query('ADR-0006', **request['kwargs'])
    assert index._connection.total_changes == before
    print(json.dumps(result, sort_keys=True))
"""
    restarted = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"path": str(retrieval["index"].path), "kwargs": retrieval["kwargs"]}),
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert json.loads(restarted.stdout) == expected


@pytest.mark.parametrize("value", [None, [], "foreign", True])
def test_untyped_generation_rejected(retrieval: Any, value: Any) -> None:
    retrieval["observed"].overrides["generation"] = lambda *_: value
    with pytest.raises(PolicyViolation, match="typed generation"):
        retrieval["service"].query(HEALTH, **retrieval["kwargs"])


@pytest.mark.parametrize("change", [{"project_id": "foreign"}, {"chunk_count": True}])
def test_generation_scope_and_count_strict_types(retrieval: Any, change: Any) -> None:
    retrieval["observed"].overrides["generation"] = lambda *_: replace(
        retrieval["generation"], **change
    )
    with pytest.raises(PolicyViolation):
        retrieval["service"].query(HEALTH, **retrieval["kwargs"])


@pytest.mark.parametrize("value", [None, [], {}, "foreign"])
def test_wrong_channel_container_rejected(retrieval: Any, value: Any) -> None:
    retrieval["observed"].overrides["lexical"] = lambda *args, **kwargs: value
    with pytest.raises(PolicyViolation, match="bounded tuple"):
        retrieval["service"].query(HEALTH, **retrieval["kwargs"])


@pytest.mark.parametrize("method", ["exact", "lexical", "views", "source_identity"])
def test_typed_index_integrity_errors_not_obscured(retrieval: Any, method: str) -> None:
    def invalid(*args: Any, **kwargs: Any) -> Any:
        raise PolicyViolation("Immutable indexed evidence drift")

    retrieval["observed"].overrides[method] = invalid
    with pytest.raises(PolicyViolation, match="evidence drift"):
        retrieval["service"].query(HEALTH, **retrieval["kwargs"])


def test_final_generation_timeout_discards_all_prepared_candidates(retrieval: Any) -> None:
    calls = 0

    def changing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("Final generation unavailable")
        return retrieval["generation"]

    retrieval["observed"].overrides["generation"] = changing
    result = retrieval["service"].query(HEALTH, **retrieval["kwargs"])
    assert calls == 2
    assert result["state"] == "abstained-index-unavailable"
    assert result["fragments"] == []
    assert result["token_count"] == 0


def test_result_digest_and_database_read_only_query(retrieval: Any) -> None:
    before = retrieval["index"]._connection.total_changes
    result = retrieval["service"].query(HEALTH, limit=8, token_budget=16384, **retrieval["kwargs"])
    assert retrieval["index"]._connection.total_changes == before
    assert result.pop("retrieval_digest") == digest(result)
    assert result["source_bytes_verified"] is False
    assert result["resolver_required"] is True


def test_verify_fragment_replays_exact_pin_without_search_or_provider(retrieval: Any) -> None:
    item = retrieval["service"].query(HEALTH, **retrieval["kwargs"])["fragments"][0]
    retrieval["observed"].calls.clear()
    result = retrieval["service"].verify_fragment(
        **retrieval["kwargs"],
        generation_digest=item["generation_digest"],
        chunk_id=item["chunk_id"],
    )
    assert result == {key: item[key] for key in result}
    assert [name for name, _, _ in retrieval["observed"].calls] == [
        "generation",
        "views",
        "source_identity",
        "generation",
    ]
    with SQLiteKnowledgeIndex(retrieval["index"].path) as reopened:
        again = LocalStartupRetrieval(ObservedIndex(reopened)).verify_fragment(
            **retrieval["kwargs"],
            generation_digest=item["generation_digest"],
            chunk_id=item["chunk_id"],
        )
    assert again == result


@pytest.mark.parametrize(
    "change",
    [
        {"generation_digest": digest("not-current")},
        {"expected_tree_digest": digest("not-current")},
        {"expected_source_revision": "not-current"},
        {"project_id": "foreign"},
        {"chunk_id": "foreign"},
        {"generation_digest": None},
        {"chunk_id": None},
    ],
)
def test_verify_fragment_refuses_unknown_or_malformed_pin(retrieval: Any, change: Any) -> None:
    args = {
        **retrieval["kwargs"],
        "generation_digest": retrieval["generation"].generation_digest,
        "chunk_id": "real-0",
        **change,
    }
    with pytest.raises((PolicyViolation, ValidationFailed, NotFound)):
        retrieval["service"].verify_fragment(**args)


def test_verify_fragment_rejects_generation_swap_during_identity(retrieval: Any) -> None:
    def swap(*args: Any, **kwargs: Any) -> Any:
        identity = retrieval["index"].source_identity(*args, **kwargs)
        _build(
            retrieval["index"],
            tuple(replace(r, chunk_id=r.chunk_id + "-next") for r in retrieval["records"]),
            retrieval["kwargs"]["expected_tree_digest"],
            profile="new-fixture-profile-no-semantic-claim",
        )
        return identity

    retrieval["observed"].overrides["source_identity"] = swap
    with pytest.raises(PolicyViolation, match="changed during read"):
        retrieval["service"].verify_fragment(
            **retrieval["kwargs"],
            generation_digest=retrieval["generation"].generation_digest,
            chunk_id="real-0",
        )


@pytest.mark.parametrize("method", ["generation", "views", "source_identity"])
def test_verify_fragment_does_not_degrade_unavailable_persisted_evidence(
    retrieval: Any, method: str
) -> None:
    def timeout(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("Persisted index evidence unavailable")

    retrieval["observed"].overrides[method] = timeout
    with pytest.raises(TimeoutError):
        retrieval["service"].verify_fragment(
            **retrieval["kwargs"],
            generation_digest=retrieval["generation"].generation_digest,
            chunk_id="real-0",
        )
