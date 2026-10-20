"""Real SQLite and native answer projection, synthetic metadata only."""
import copy
from types import SimpleNamespace

import pytest

from zekam.application.embedded_project_rag import EmbeddedProjectRAG
from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.application.metadata_schema_answer import build_metadata_schema_result, metadata_schema_explanation
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import Locator
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex
from zekam.interfaces.cli.main import _retrieval_explanation

PROJECT = "sample"
QUESTION = "database structure, core tables and foreign keys"
PARENT = "TABLE APP.PARENT\nCOLUMN ID type=NUMBER nullable=false\nCONSTRAINT PARENT_PK kind=P columns=[ID] status=ENABLED validated=VALIDATED"
CHILD = "TABLE APP.CHILD\nCOLUMN ID type=NUMBER nullable=false\nCOLUMN PID type=NUMBER nullable=true\nCONSTRAINT CHILD_PK kind=P columns=[ID]\nCONSTRAINT CHILD_FK kind=R columns=[PID] REFERENCES PARENT referenceColumns=[ID] status=ENABLED validated=VALIDATED"


def record(cid, table, text, *, project=PROJECT, document=False):
    path = "AKTIF_GOREV.md" if document else "oracle/table/" + table
    return KnowledgeIndexRecord(chunk_id=cid, project_id=project, source_revision="rev-1",
        source_path=path, source_digest=digest(path),
        locator=Locator(relative_path=path) if document else Locator(relative_path=path, object_name="APP." + table + ":TABLE"),
        text=text, content_digest=digest_of_bytes(text.encode()), chunk_order=0,
        vector=(1.0, 0.0, 0.0, 0.0), vector_dimension=4)


def publish(index, records, project=PROJECT):
    return index.build_generation(tuple(records), project_id=project, source_revision="rev-1",
        tree_digest=digest("tree"), source_manifest_digest=digest("manifest"),
        embedding_profile_digest=digest("embedding"), provider_profile_digest=digest("provider"),
        created_at="2026-10-20T00:00:00Z")


@pytest.fixture
def seeded(tmp_path):
    with SQLiteKnowledgeIndex(tmp_path / "knowledge.sqlite3", create=True, dimension=4) as index:
        rows = [record("parent", "PARENT", PARENT), record("child", "CHILD", CHILD),
                record("doc", "FAKE", "database structure core tables: future task plan TABLE APP.FAKE", document=True)]
        generation = publish(index, rows)
        yield index, generation


def test_candidates_only_pinned_project_metadata(seeded):
    index, generation = seeded
    result = index.metadata_schema_candidates(PROJECT, generation_digest=generation.generation_digest)
    assert set(result["chunk_ids"]) == {"parent", "child"}
    other = publish(index, [record("other", "OTHER", "TABLE APP.OTHER", project="other")], project="other")
    assert set(index.metadata_schema_candidates(PROJECT, generation_digest=generation.generation_digest)["chunk_ids"]) == {"parent", "child"}
    with pytest.raises(PolicyViolation):
        index.metadata_schema_candidates(PROJECT, generation_digest=other.generation_digest)


@pytest.mark.parametrize("limit", [0, 4097, True, "4"])
def test_invalid_candidate_limit(seeded, limit):
    index, generation = seeded
    with pytest.raises(ValidationFailed):
        index.metadata_schema_candidates(PROJECT, generation_digest=generation.generation_digest, limit=limit)


def test_candidate_cap_is_honest(seeded):
    index, generation = seeded
    result = index.metadata_schema_candidates(PROJECT, generation_digest=generation.generation_digest, limit=1)
    assert len(result["chunk_ids"]) == 1 and result["total_chunks"] == 2 and result["truncated"]


def payload(seeded):
    index, generation = seeded
    return build_metadata_schema_result(index, QUESTION, project_id=PROJECT, generation=generation)


def test_sqlite_result_citations_reopen_exactly(seeded):
    index, generation = seeded
    result = payload(seeded)
    assert result["state"] == "answered"
    assert metadata_schema_explanation(result)
    ids = tuple(result["used_chunk_ids"])
    views = index.views(PROJECT, ids, generation_digest=generation.generation_digest)
    identities = index.source_identities(PROJECT, ids, generation_digest=generation.generation_digest)
    for citation in result["citations"]:
        cid = citation["chunk_id"]
        assert citation["source_ref"] == identities[cid]["source_ref"]
        assert citation["content_digest"] == digest_of_bytes(views[cid].text.encode())
        assert citation["locator"] == views[cid].locator.as_dict()
        assert citation["source_ref"].startswith("oracle/table/")
    assert "doc" not in ids


def forbidden(*args, **kwargs):
    raise AssertionError("metadata query must not call provider")


def test_schema_route_does_not_call_embedding_or_generic_documents(seeded):
    index, generation = seeded
    provider = SimpleNamespace(describe=forbidden, health=forbidden, embed=forbidden)
    rag = EmbeddedProjectRAG(index, provider, SimpleNamespace(expected_profile_digest=digest("provider")))
    result = rag.query(QUESTION, project_id=PROJECT, expected_source_revision="rev-1", expected_tree_digest=generation.tree_digest)
    assert result["searched_channels"] == ["database-metadata"]
    assert result["query_intent"] == "database-schema"
    assert "AKTIF_GOREV.md" not in str(result)
    with pytest.raises(PolicyViolation):
        rag.query(QUESTION, project_id=PROJECT, expected_source_revision="wrong", expected_tree_digest=generation.tree_digest)


def cli(result, projects=None):
    projects = projects or ["sample"]
    return _retrieval_explanation({"results": [{"project_ref": name, "status": "answered", "result": copy.deepcopy(result)} for name in projects], "project_refs": projects})


def test_cli_uses_validated_text_not_document_excerpt(seeded):
    result = payload(seeded)
    result["answer_excerpt"] = "unrelated task plan"
    text = cli(result)
    assert text and "APP.CHILD(PID) -> APP.PARENT(ID)" in text
    assert "unrelated task plan" not in text
    multiple = cli(result, ["sample", "other"])
    assert "sample: " in multiple and "other: " in multiple


@pytest.mark.parametrize("kind", ["stale", "insufficient", "tamper"])
def test_cli_invalid_metadata_never_falls_back(seeded, kind):
    result = payload(seeded)
    result["answer_excerpt"] = "unrelated task plan"
    if kind == "stale": result["index_freshness"] = "stale"
    elif kind == "insufficient": result["metadata_schema_summary"]["status"] = "insufficient"
    else: result["metadata_schema_summary"]["text"] += " fake"
    assert cli(result) is None


def test_cli_normal_excerpt_unchanged():
    assert cli({"answer_excerpt": "normal source excerpt"}) == "normal source excerpt"


def test_native_answer_admission_accepts_schema_description(seeded):
    from zekam.application.natural_request_answer import answer_natural_request
    from zekam.interfaces.cli.main import _single_natural_request_retrieval

    result = payload(seeded)
    envelope = _single_natural_request_retrieval(PROJECT, QUESTION, result, authorize_remote_query=False)
    answer = answer_natural_request(envelope, _retrieval_explanation(envelope))
    assert answer["proposed_explanation_accepted"] is True
    assert "APP.CHILD(PID) -> APP.PARENT(ID)" in answer["answer"]
