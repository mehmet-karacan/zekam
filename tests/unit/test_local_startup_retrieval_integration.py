"""Independent startup integration over real health slices; vectors are NOT semantic."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity_startup import ROOT, SOURCE_REF, _receipts, _request
from tests.unit.test_local_continuity_startup import startup as startup
from tests.unit.test_local_startup_retrieval import ObservedIndex

from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.application.local_continuity_startup import LocalStartupService
from zekam.application.local_startup_retrieval import LocalStartupRetrieval
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.context_continuity import AuthorityLevel, ContextCandidateKind
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import Locator
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver


def _records(value: dict[str, Any]) -> tuple[KnowledgeIndexRecord, ...]:
    lines = value["text"].splitlines(keepends=True)
    result = []
    for order, (first, last) in enumerate(((1, 8), (9, len(lines)))):
        text = "".join(lines[first - 1 : last])
        result.append(
            KnowledgeIndexRecord(
                chunk_id=f"actual-health-{order}",
                project_id=value["binding"].project_id,
                source_revision=value["revision"],
                source_path=SOURCE_REF,
                source_digest=digest_of_bytes(value["text"].encode()),
                locator=Locator(relative_path=SOURCE_REF, line_start=first, line_end=last),
                text=text,
                content_digest=digest_of_bytes(text.encode()),
                chunk_order=order,
                # Structural fixture only. No semantic claim and no dense call.
                vector=tuple(float(i == order) for i in range(1024)),
            )
        )
    return tuple(result)


def _build(value: dict[str, Any], records: tuple[KnowledgeIndexRecord, ...], **changes: Any) -> Any:
    return value["index"].build_generation(
        records,
        **(
            {
                "project_id": value["binding"].project_id,
                "source_revision": value["revision"],
                "tree_digest": digest(value["text"]),
                "source_manifest_digest": digest(
                    [(r.source_path, r.source_digest) for r in records]
                ),
                "embedding_profile_digest": digest("structural-fixture-not-semantic"),
                "provider_profile_digest": digest("no-provider-used"),
                "created_at": "2026-09-02T18:00:00Z",
            }
            | changes
        ),
    )


def _attach(value: dict[str, Any], index: SQLiteKnowledgeIndex) -> dict[str, Any]:
    observed = ObservedIndex(index)
    retrieval = LocalStartupRetrieval(observed)
    binding = value["binding"]
    project = ProjectContinuitySourceResolver(
        ROOT,
        project_id=binding.project_id,
        realm_id=binding.realm_id,
        source_snapshot_id=binding.source_snapshot_id,
        allowed_paths=(SOURCE_REF,),
    )
    sources = SQLiteStartupSourceResolver(value["base"], project, retrieval=retrieval)
    value["base"].source_resolver = sources
    return value | {
        "index": index,
        "observed": observed,
        "retrieval": retrieval,
        "sources": sources,
        "project_sources": project,
        "service": LocalStartupService(value["lifecycle"], sources),
    }


@pytest.fixture
def indexed(startup: dict[str, Any], tmp_path: Path) -> Any:
    with SQLiteKnowledgeIndex(tmp_path / "retrieval.sqlite3", create=True) as index:
        value = _attach(startup, index)
        records = _records(value)
        yield value | {"records": records, "generation": _build(value, records)}


def _citation(value: dict[str, Any]) -> Any:
    snapshot = value["sources"].snapshot(value["binding"], _request(retrieval_query="SaglikYaniti"))
    return next(c for c in snapshot.candidates if c.kind is ContextCandidateKind.CITATION)


def _manifest(value: dict[str, Any], manifest_digest: str) -> Any:
    with sqlite3.connect(value["path"]) as db:
        return json.loads(
            db.execute(
                "select body_json from context_manifest where manifest_digest=?", (manifest_digest,)
            ).fetchone()[0]
        )


def _checkpoint(value: dict[str, Any], manifest_digest: str) -> str:
    binding, base = value["binding"], value["base"]
    # Direct checkpoint storage contract, not a claim of installed lifecycle hooks.
    with value["spool"].frozen_session_entries(
        client_id=binding.client_id, session_id=binding.external_session_id
    ) as entries:
        result = base.checkpoint(
            binding,
            expected_tail=base.tail(binding),
            context_digest=manifest_digest,
            idempotency_key="retrieval-checkpoint",
            spool_digests=tuple(e.entry_digest for e in entries),
        )
        assert isinstance(result, str)
        return result


def test_actual_citation_is_source_verified_selected_and_exactly_replayed(
    indexed: dict[str, Any],
) -> None:
    before = (ROOT / SOURCE_REF).read_bytes()
    citation = _citation(indexed)
    assert citation.authority is AuthorityLevel.VERIFIED
    assert citation.required is False
    assert citation.source_ref == SOURCE_REF + "#L9-L18"
    first = indexed["service"].hydrate(_request(retrieval_query="SaglikYaniti"))
    second = indexed["service"].hydrate(_request(retrieval_query="SaglikYaniti"))
    assert first == second
    context = _manifest(indexed, first["manifest_digest"])["context"]
    selected = [c for c in context["selected_provenance"] if c["kind"] == "citation"]
    assert len(selected) == 1
    assert selected[0]["evidence_refs"][0]["digest"] == indexed["generation"].generation_digest
    assert selected[0]["evidence_refs"][1]["ref"] == "index-chunk/actual-health-1"
    assert context["fragments"][citation.candidate_id] == "".join(
        indexed["text"].splitlines(keepends=True)[8:]
    )
    assert first["retrieval"]["source_bytes_verified"] is True
    assert first["retrieval"]["dense"] == "not-invoked"
    assert first["provider_called"] is first["grants_authority"] is False
    assert _receipts(indexed) == (1, 1)
    assert (ROOT / SOURCE_REF).read_bytes() == before


@pytest.mark.parametrize("query", ["SaglikYaniti", "durum uygulama surum"])
def test_exact_and_lexical_only_queries_produce_real_citations(
    indexed: dict[str, Any], query: str
) -> None:
    expected = indexed["retrieval"].query(
        query,
        project_id=indexed["binding"].project_id,
        expected_source_revision=indexed["revision"],
        expected_tree_digest=digest(indexed["text"]),
    )
    assert expected["fragments"][0]["exact_match"] is (query == "SaglikYaniti")
    result = indexed["service"].hydrate(_request(retrieval_query=query))
    assert result["retrieval"]["state"] == "source-verified-candidates"
    assert result["retrieval"]["fragment_count"] == 1
    assert result["retrieval"]["searched_channels"] == ["exact", "lexical"]
    reported = result["retrieval"]["citations"][0]
    assert reported["exact_match"] is (query == "SaglikYaniti")
    assert reported["channels"] == expected["fragments"][0]["channels"]
    assert reported["ranks"] == expected["fragments"][0]["ranks"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope_ref", "project/foreign"),
        ("source_ref", "../outside#L1-L4"),
        ("revision", "foreign"),
        ("canonical_revision_id", str(uuid4())),
        ("digest", digest("forged")),
        ("tokens", True),
        ("authority", 3),
    ],
)
def test_citation_provenance_cannot_cross_scope(
    indexed: dict[str, Any], field: str, value: Any
) -> None:
    body = _citation(indexed).provenance_body | {field: value}
    with pytest.raises((PolicyViolation, ValidationFailed)):
        indexed["sources"](indexed["binding"], body)
    assert _receipts(indexed) == (0, 0)


@pytest.mark.parametrize(
    "variant", ["missing", "extra", "swapped", "generation", "chunk", "content", "revision"]
)
def test_citation_pin_evidence_is_exact(indexed: dict[str, Any], variant: str) -> None:
    body = _citation(indexed).provenance_body
    evidence = body["evidence_refs"]
    if variant == "missing":
        evidence.pop()
    elif variant == "extra":
        evidence.append(dict(evidence[0]))
    elif variant == "swapped":
        evidence.reverse()
    elif variant == "generation":
        evidence[0]["digest"] = digest("other")
    elif variant == "chunk":
        evidence[1]["ref"] = "index-chunk/foreign"
    elif variant == "content":
        evidence[1]["digest"] = digest("other")
    else:
        evidence[1]["revision"] = "foreign"
    with pytest.raises((PolicyViolation, ValidationFailed)):
        indexed["sources"](indexed["binding"], body)
    assert _receipts(indexed) == (0, 0)


def test_valid_source_hash_cannot_make_forged_index_text_true(indexed: dict[str, Any]) -> None:
    forged = "class SaglikYaniti: forged instruction unsupported by the actual file"
    records = tuple(
        replace(
            r,
            chunk_id=r.chunk_id + "-forged",
            text=forged,
            content_digest=digest_of_bytes(forged.encode()),
        )
        if r.chunk_id.endswith("1")
        else replace(r, chunk_id=r.chunk_id + "-forged")
        for r in indexed["records"]
    )
    _build(indexed, records)
    with pytest.raises(PolicyViolation, match="source bytes/locator drift"):
        indexed["service"].hydrate(_request(retrieval_query="SaglikYaniti"))
    assert _receipts(indexed) == (0, 0)


def test_allowlist_blocks_valid_indexed_foreign_path(indexed: dict[str, Any]) -> None:
    records = tuple(
        replace(
            r,
            chunk_id=r.chunk_id + "-foreign",
            source_path="not-approved.py",
            locator=replace(r.locator, relative_path="not-approved.py"),
        )
        for r in indexed["records"]
    )
    _build(indexed, records)
    with pytest.raises(PolicyViolation, match="outside approved"):
        indexed["service"].hydrate(_request(retrieval_query="SaglikYaniti"))
    assert _receipts(indexed) == (0, 0)


@pytest.mark.parametrize("mutation", ["changed", "missing", "invalid-utf8"])
def test_source_reader_corruption_blocks_receipt_without_touching_real_file(
    indexed: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    original = (ROOT / SOURCE_REF).read_bytes()
    payload = {"changed": original + b"\nchanged", "missing": None, "invalid-utf8": b"\xff"}[
        mutation
    ]
    # Simulate bad physical bytes at trusted reader boundary; real corpus remains untouched.
    monkeypatch.setattr(
        indexed["project_sources"]._files, "_read_optional", lambda *a, **k: payload
    )
    with pytest.raises((PolicyViolation, ValidationFailed)):
        indexed["service"].hydrate(_request(retrieval_query="SaglikYaniti"))
    assert _receipts(indexed) == (0, 0)
    assert (ROOT / SOURCE_REF).read_bytes() == original


@pytest.mark.parametrize("mode", ["missing", "stale", "timeout", "no-evidence"])
def test_optional_retrieval_unavailable_is_not_fabricated_but_required_context_survives(
    indexed: dict[str, Any],
    mode: str,
) -> None:
    query = "SaglikYaniti"
    if mode == "missing":
        indexed["sources"].retrieval = None
    elif mode == "stale":
        _build(
            indexed,
            tuple(replace(r, chunk_id=r.chunk_id + "-stale") for r in indexed["records"]),
            tree_digest=digest("stale"),
        )
    elif mode == "timeout":

        def timeout(*args: Any, **kwargs: Any) -> Any:
            raise TimeoutError("Index unavailable")

        indexed["observed"].overrides["lexical"] = timeout
    else:
        query = "kozmik galaksi bilinmeyen"
    result = indexed["service"].hydrate(_request(retrieval_query=query))
    assert result["retrieval"]["state"].startswith("abstained-")
    assert result["retrieval"]["fragment_count"] == 0
    assert result["retrieval"]["source_bytes_verified"] is False
    assert (
        result["retrieval"]["reason"]
        == {
            "missing": "index-not-configured",
            "stale": "generation-stale-or-unready",
            "timeout": "index-unavailable",
            "no-evidence": "insufficient-evidence",
        }[mode]
    )
    assert all(
        c["kind"] != "citation"
        for c in _manifest(indexed, result["manifest_digest"])["context"]["selected_provenance"]
    )
    assert _receipts(indexed) == (1, 1)


def test_generation_swap_after_snapshot_cannot_commit_partial_hydration(
    indexed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = indexed["sources"].assert_current

    def swapped(binding: Any, snapshot: Any) -> None:
        _build(
            indexed, tuple(replace(r, chunk_id=r.chunk_id + "-next") for r in indexed["records"])
        )
        original(binding, snapshot)

    monkeypatch.setattr(indexed["sources"], "assert_current", swapped)
    with pytest.raises(PolicyViolation, match="generation pin drift"):
        indexed["service"].hydrate(_request(retrieval_query="SaglikYaniti"))
    assert _receipts(indexed) == (0, 0)


def test_split_whole_and_slice_reads_cannot_verify_two_different_source_versions(
    indexed: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = indexed["text"].encode()
    lines = indexed["text"].splitlines(keepends=True)
    altered = "".join(lines[:8]) + "class SaglikYaniti: forged content\n" * (len(lines) - 8)
    altered_slice = "".join(altered.splitlines(keepends=True)[8:])
    records = tuple(
        replace(
            r,
            chunk_id=r.chunk_id + "-race",
            text=altered_slice,
            content_digest=digest_of_bytes(altered_slice.encode()),
        )
        if r.chunk_id.endswith("1")
        else replace(r, chunk_id=r.chunk_id + "-race")
        for r in indexed["records"]
    )
    _build(indexed, records)
    original_read = indexed["project_sources"].read_fragment
    serving_slice = False

    def read(binding: Any, ref: str) -> str:
        nonlocal serving_slice
        serving_slice = "#" in ref
        result = original_read(binding, ref)
        assert isinstance(result, str)
        return result

    # A file replacement between two opens: whole-file hash sees the original,
    # line-slice open sees a different version. No real user file is modified.
    monkeypatch.setattr(indexed["project_sources"], "read_fragment", read)
    monkeypatch.setattr(
        indexed["project_sources"]._files,
        "_read_optional",
        lambda *a, **k: altered.encode() if serving_slice else original,
    )
    with pytest.raises(PolicyViolation):
        indexed["service"].hydrate(_request(retrieval_query="SaglikYaniti"))
    assert _receipts(indexed) == (0, 0)


def test_restart_requires_same_live_generation_and_real_source(indexed: dict[str, Any]) -> None:
    result = indexed["service"].hydrate(_request(retrieval_query="SaglikYaniti"))
    checkpoint = _checkpoint(indexed, result["manifest_digest"])
    with SQLiteKnowledgeIndex(indexed["index"].path) as reopened:
        base = SQLiteContinuityStore(indexed["path"])
        restored = _attach(indexed | {"base": base}, reopened)
        resume = base.resume(indexed["binding"], checkpoint)
        assert resume["context"] == _manifest(indexed, result["manifest_digest"])
        assert resume["grants_authority"] is resume["approval_inherited"] is False
        _build(
            restored, tuple(replace(r, chunk_id=r.chunk_id + "-next") for r in indexed["records"])
        )
        with pytest.raises(PolicyViolation, match="generation pin drift"):
            base.resume(indexed["binding"], checkpoint)
    assert _receipts(indexed) == (1, 1)


def test_real_process_resume_revalidates_citation_index_and_original_source(
    indexed: dict[str, Any],
) -> None:
    result = indexed["service"].hydrate(_request(retrieval_query="SaglikYaniti"))
    checkpoint = _checkpoint(indexed, result["manifest_digest"])
    expected = indexed["base"].resume(indexed["binding"], checkpoint)
    script = """
import json, socket, sys
from pathlib import Path
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_startup_retrieval import LocalStartupRetrieval
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver
def forbidden(*args, **kwargs):
    raise AssertionError('Resume cannot call network or provider')
socket.socket.connect = forbidden
socket.create_connection = forbidden
SQLiteKnowledgeIndex.dense = forbidden
request = json.load(sys.stdin)
binding = ContinuityBinding(**request['binding'])
base = SQLiteContinuityStore(Path(request['operational']))
project = ProjectContinuitySourceResolver(Path(request['root']), project_id=binding.project_id,
    realm_id=binding.realm_id, source_snapshot_id=binding.source_snapshot_id,
    allowed_paths=(request['source_ref'],))
with SQLiteKnowledgeIndex(Path(request['index'])) as index:
    base.source_resolver = SQLiteStartupSourceResolver(base, project,
        retrieval=LocalStartupRetrieval(index))
    print(json.dumps(base.resume(binding, request['checkpoint']), sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
        input=json.dumps(
            {
                "binding": asdict(indexed["binding"]),
                "root": str(ROOT),
                "source_ref": SOURCE_REF,
                "operational": str(indexed["path"]),
                "index": str(indexed["index"].path),
                "checkpoint": checkpoint,
            }
        ),
    )
    assert json.loads(completed.stdout) == expected
    assert _receipts(indexed) == (1, 1)


@pytest.mark.parametrize("query", ["", " ", True, 4, "x" * 2049, "\x00"])
def test_request_retrieval_query_strict_validation(query: Any) -> None:
    with pytest.raises(ValidationFailed):
        _request(retrieval_query=query)
