from __future__ import annotations

import datetime as dt
import io
import struct
import zipfile
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import pytest

from zekam.application.context_recipe import (
    ContextRecipe,
    ContextRecipeRegistry,
    ContextRecipeRole,
)
from zekam.application.history_import import (
    HistoryImportCount,
    HistoryImportFilter,
    HistoryImportRequest,
)
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import AuthorityLevel, ContextCandidateKind
from zekam.domain.errors import ConfigurationError, NotFound, PolicyViolation, ValidationFailed
from zekam.domain.knowledge import SourceFormat
from zekam.domain.markdown_projection import (
    MarkdownProjectionFile,
    ObsidianNoteKind,
    ObsidianProjectionFile,
    ObsidianProjectionRecord,
    ProjectionExclusion,
    ProjectionRecord,
    ProjectionRelationRef,
    ProjectionSourceRef,
    obsidian_note_path,
    projection_filename,
    render_projection_record,
)
from zekam.domain.research import (
    Citation,
    CitationVerification,
    Conflict,
    ConflictKind,
    Finding,
    ResearchBudget,
    ResearchDag,
    ResearchNode,
    ResearchQuestion,
    ResearchRole,
    RoleOutcome,
    RoleResult,
    SourceKind,
    SourcePolicy,
    SourceSnapshot,
    synthesize,
)
from zekam.domain.session_continuity import DataClassification, TruthClass
from zekam.infrastructure.knowledge import document_parsers as parsers

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(int=value) for value in range(1, 10))
D = digest("evidence")


def _zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return output.getvalue()


def test_document_parser_media_zip_xml_and_docx_fail_closed() -> None:
    assert parsers.media_type_for(SourceFormat.DOCX).endswith("document")
    for name in ("../escape.xml", "/absolute.xml", "a\\b.xml"):
        with pytest.raises(PolicyViolation):
            parsers._safe_zip_name(name)
    parsers._safe_zip_name("word/document.xml")
    with pytest.raises(ValidationFailed):
        parsers._xml(b"<broken>", "test")
    with pytest.raises(ValidationFailed):
        parsers._xml(b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>', "test")
    with pytest.raises(ValidationFailed, match="DOCX"):
        parsers.DocxParser().parse(b"not-a-zip")
    external = _zip(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
            "word/_rels/document.xml.rels": (
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + b'<Relationship TargetMode="External"/>'
                + b"</Relationships>"
            ),
        }
    )
    with pytest.raises(PolicyViolation, match="external"):
        parsers.DocxParser().parse(external)
    empty = _zip(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
        }
    )
    with pytest.raises(ValidationFailed, match="icerik"):
        parsers.DocxParser().parse(empty)


def test_image_dimension_parsers_cover_valid_invalid_and_pixel_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 8 + struct.pack(">II", 20, 10)
    assert parsers._png_dimensions(png) == ((20, 10),)
    with pytest.raises(ValidationFailed):
        parsers._png_dimensions(b"bad")
    jpeg = b"\xff\xd8\xff\xc0\x00\x09\x08\x00\x0a\x00\x14\x03\x01"
    assert parsers._jpeg_dimensions(jpeg) == ((20, 10),)
    with pytest.raises(ValidationFailed):
        parsers._jpeg_dimensions(b"bad")
    with pytest.raises(ValidationFailed, match="segment"):
        parsers._jpeg_dimensions(b"\xff\xd8\xff\xe0\x00\xff")

    tiff = (
        b"II"
        + struct.pack("<H", 42)
        + struct.pack("<I", 8)
        + struct.pack("<H", 2)
        + struct.pack("<HHI", 256, 4, 1)
        + struct.pack("<I", 20)
        + struct.pack("<HHI", 257, 4, 1)
        + struct.pack("<I", 10)
        + struct.pack("<I", 0)
    )
    assert parsers._tiff_dimensions(tiff) == ((20, 10),)
    assert parsers._tiff_value(tiff, "<", 3, 2, b"\x01\x00\x00\x00") is None
    with pytest.raises(ValidationFailed):
        parsers._tiff_dimensions(b"bad")
    with pytest.raises(ValidationFailed):
        parsers._dimensions(SourceFormat.TXT, b"text")
    monkeypatch.setattr(parsers, "MAX_IMAGE_PIXELS", 1)
    with pytest.raises(PolicyViolation, match="pixel"):
        parsers._dimensions(SourceFormat.PNG, png)


def test_tesseract_parser_validation_and_tsv_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for kwargs in (
        {"source_format": SourceFormat.TXT},
        {"source_format": SourceFormat.PNG, "languages": ()},
        {"source_format": SourceFormat.PNG, "languages": ("bad-!",)},
        {"source_format": SourceFormat.PNG, "minimum_confidence": 2.0},
    ):
        with pytest.raises(ValidationFailed):
            parsers.TesseractOcrParser(**cast(Any, kwargs))
    parser = parsers.TesseractOcrParser(SourceFormat.PNG, minimum_confidence=0.5)
    monkeypatch.setattr(
        "zekam.infrastructure.knowledge.document_parsers.shutil.which", lambda _: None
    )
    with pytest.raises(ConfigurationError):
        parser._executable()
    with pytest.raises(ValidationFailed, match="kolon"):
        parser._units("bad\nvalue", ((100, 100),))
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    row = "5\t1\t1\t1\t1\t10\t20\t30\t10\t90\tHello\n"
    units = parser._units(header + row, ((100, 100),))
    assert units[0].text == "Hello" and units[0].locator.bbox == (0.1, 0.2, 0.4, 0.3)
    with pytest.raises(ValidationFailed, match="sayisal"):
        parser._units(header + row.replace("\t10\t20", "\tbad\t20"), ((100, 100),))
    with pytest.raises(ValidationFailed, match="sayfa"):
        parser._units(header + row.replace("\t1\t1\t1\t1", "\t2\t1\t1\t1"), ((100, 100),))
    with pytest.raises(ValidationFailed, match="bos"):
        parser._units(header + row.replace("\t90\t", "\t10\t"), ((100, 100),))


@pytest.mark.parametrize(
    "changes",
    [
        {"recipe_id": ""},
        {"version": 0},
        {"maximum_token_budget": 0},
        {"role": "builder"},
        {"minimum_authority": 1},
        {"required_kinds": frozenset()},
        {"allowed_kinds": frozenset({ContextCandidateKind.GENERAL})},
    ],
)
def test_context_recipe_exact_types_limits_and_forbidden_general(changes: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "recipe_id": "recipe",
        "version": 1,
        "role": ContextRecipeRole.BUILDER,
        "allowed_kinds": frozenset({ContextCandidateKind.SOURCE_DIFF}),
        "required_kinds": frozenset({ContextCandidateKind.SOURCE_DIFF}),
        "maximum_token_budget": 10,
        "per_kind_candidate_limit": 1,
        "per_kind_token_limit": 10,
        "minimum_authority": AuthorityLevel.OBSERVED,
    }
    with pytest.raises((ValidationFailed, PolicyViolation)):
        ContextRecipe(**cast(Any, values | changes))


def test_context_recipe_registry_deduplicates_and_missing_role_fails() -> None:
    recipe = ContextRecipe(
        "builder",
        1,
        ContextRecipeRole.BUILDER,
        frozenset({ContextCandidateKind.SOURCE_DIFF}),
        frozenset({ContextCandidateKind.SOURCE_DIFF}),
        10,
        1,
        10,
    )
    assert ContextRecipeRegistry((recipe,)).for_role(ContextRecipeRole.BUILDER) == recipe
    with pytest.raises(ValidationFailed, match="tekil"):
        ContextRecipeRegistry((recipe, recipe))
    with pytest.raises(NotFound):
        ContextRecipeRegistry((recipe,)).for_role(ContextRecipeRole.VERIFIER)


def test_history_import_filter_request_count_security_boundaries() -> None:
    good = HistoryImportFilter(exclude=("foo", "secret"), project_ref="project:cash")
    assert len(good.body()["exclude_digests"]) == 2
    for changes in (
        {"date_from": dt.date(2026, 2, 1), "date_to": dt.date(2026, 1, 1)},
        {"source_types": ("zip", "zip")},
        {"exclude": ("B", "a")},
        {"exclude": ("",)},
        {"project_ref": "C:\\bad"},
    ):
        with pytest.raises(ValidationFailed):
            HistoryImportFilter(**cast(Any, changes))
    request = HistoryImportRequest(
        "history", "history.zip", DataClassification.LOCAL_ONLY, D, "human", good
    )
    assert request.source_name == "history.zip"
    request_changes: tuple[dict[str, Any], ...] = (
        {"classification": DataClassification.PUBLIC},
        {"corpus_id": ""},
        {"source_name": "../history.zip"},
        {"source_name": "history.txt"},
    )
    for changes in request_changes:
        with pytest.raises((ValidationFailed, PolicyViolation)):
            replace(request, **changes)
    assert HistoryImportCount("excluded", 0).as_dict()["count"] == 0
    for args in (("", 0), ("bad", -1)):
        with pytest.raises(ValidationFailed):
            HistoryImportCount(*args)


def _projection_record(**changes: Any) -> ProjectionRecord:
    ref = ProjectionSourceRef("work", "WORK-1", "rev-1", D)
    values: dict[str, Any] = {
        "entity_type": "work",
        "entity_id": "WORK-1",
        "title": "Work",
        "status": "active",
        "summary": "Safe summary",
        "source_refs": (ref,),
    }
    values.update(changes)
    return ProjectionRecord(**values)


def test_markdown_projection_literal_relation_path_and_render_security() -> None:
    record = _projection_record()
    payload = render_projection_record(
        record, project_id="project", snapshot_digest=D, generation_digest=D
    )
    assert b"grants_authority: false" in payload
    assert projection_filename("work", "WORK-1") == "notes/work-work-1.md"
    for changes in (
        {"title": "bad\nvalue"},
        {"summary": ""},
        {"source_refs": ()},
        {"source_refs": (record.source_refs[0], record.source_refs[0])},
        {"related_entity_ids": ("B", "A")},
    ):
        with pytest.raises(ValidationFailed):
            _projection_record(**changes)
    relation = ProjectionRelationRef("r1", "outgoing", "blocks", "WORK-2", D)
    related = _projection_record(related_entity_ids=("WORK-2",), relation_refs=(relation,))
    assert b"outgoing" in render_projection_record(
        related, project_id="p", snapshot_digest=D, generation_digest=D
    )
    with pytest.raises(ValidationFailed):
        replace(relation, direction="sideways")
    with pytest.raises(ValidationFailed):
        _projection_record(relation_refs=(relation,), related_entity_ids=())
    for path in ("../x.md", "C:\\x.md", "x.txt"):
        with pytest.raises(ValidationFailed):
            MarkdownProjectionFile(path, b"x", D)


def test_obsidian_projection_record_file_and_exclusion_boundaries() -> None:
    record = ObsidianProjectionRecord(
        _projection_record(),
        ObsidianNoteKind.WORK,
        "local",
        IDS[0],
        TruthClass.REPO_FACT,
        DataClassification.INTERNAL,
        NOW,
        confidence=0.9,
    )
    assert record.identity == "work:WORK-1"
    assert obsidian_note_path(record).endswith("work-work-1.md")
    assert obsidian_note_path(
        replace(record, record=replace(record.record, status="archived"))
    ).startswith("90_ARCHIVE/")
    for changes in (
        {"project_id": "bad"},
        {"note_kind": "work"},
        {"truth_class": "canonical"},
        {"classification": "internal"},
        {"observed_at": NOW.replace(tzinfo=None)},
        {"valid_from": NOW, "valid_until": NOW},
        {"confidence": 2},
        {"supersedes": ("b", "a")},
    ):
        with pytest.raises(ValidationFailed):
            replace(record, **changes)
    good_file = ObsidianProjectionFile("notes/x.md", b"x", "text/markdown; charset=utf-8")
    assert good_file.content_digest.startswith("sha256:")
    for args in (
        ("../x.md", b"x", "text/markdown; charset=utf-8"),
        ("x.md", b"", "text/markdown; charset=utf-8"),
        ("x.md", b"x", "bad/type"),
    ):
        with pytest.raises(ValidationFailed):
            ObsidianProjectionFile(*args)
    assert ProjectionExclusion(D, "secret-pattern").reason_code == "secret-pattern"
    with pytest.raises(ValidationFailed):
        ProjectionExclusion(D, "unknown")


def _research_policy() -> SourcePolicy:
    return SourcePolicy(frozenset({SourceKind.FILE, SourceKind.REPOSITORY}), project_scope="cash")


def _question(**changes: Any) -> ResearchQuestion:
    values: dict[str, Any] = {
        "question_id": "q1",
        "question": "How is recovery verified?",
        "project_ref": "cash",
        "work_ref": "WORK-1",
        "intent_digest": D,
        "source_revision": "git:abc",
        "policy": _research_policy(),
        "budget": ResearchBudget(100, 10, 60),
        "created_at": NOW,
    }
    values.update(changes)
    return ResearchQuestion(**values)


def test_research_source_question_snapshot_security_and_scope() -> None:
    assert _research_policy().permits(SourceKind.FILE)
    https = SourcePolicy(frozenset({SourceKind.HTTPS}), frozenset({"example.com"}))
    assert https.permits(SourceKind.HTTPS, host="example.com")
    assert not https.permits(SourceKind.HTTPS, host="other.com")
    for factory in (
        lambda: SourcePolicy(frozenset()),
        lambda: SourcePolicy(frozenset({SourceKind.HTTPS})),
        lambda: SourcePolicy(frozenset({SourceKind.FILE}), frozenset({"Bad/Host"})),
        lambda: ResearchBudget(0, 1, 1),
        lambda: ResearchBudget(1, 1, 601),
        lambda: _question(question=""),
        lambda: _question(question="api_key abcdefghijklmnop"),
        lambda: _question(project_ref="other"),
        lambda: _question(created_at=NOW.replace(tzinfo=None)),
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            factory()
    snapshot = SourceSnapshot("s1", SourceKind.FILE, "docs/a.md", D, NOW)
    snapshot.assert_permitted(_research_policy())
    snapshot_factories: tuple[Any, ...] = (
        lambda: SourceSnapshot("s", SourceKind.FILE, "../a", D, NOW),
        lambda: SourceSnapshot("s", SourceKind.REPOSITORY, "src/a", D, NOW),
        lambda: SourceSnapshot("s", SourceKind.IMPORT, "import", D, NOW),
        lambda: SourceSnapshot("s", SourceKind.HTTPS, "http://x", D, NOW, host="x"),
        lambda: SourceSnapshot("s", SourceKind.HTTPS, "https://x/a?q=secret", D, NOW, host="x"),
    )
    for factory in snapshot_factories:
        with pytest.raises((ValidationFailed, PolicyViolation)):
            factory()


def test_research_finding_results_dag_conflict_and_synthesis_integrity() -> None:
    citation = Citation("s1", "line 1", D)
    finding = Finding("f1", "Evidence supports recovery", (citation,), "high")
    success = RoleResult(ResearchRole.RESEARCHER, "agent-a", RoleOutcome.SUCCESS, (finding,))
    blocked = RoleResult(ResearchRole.CRITIC, "agent-b", RoleOutcome.BLOCKED, blocker="missing")
    verification = CitationVerification("verifier", ("f1",))
    accepted, conflicts, non_success = synthesize(
        (success, blocked), conflicts=(), verification=verification
    )
    assert accepted == (finding,) and conflicts == () and non_success == (blocked,)
    with pytest.raises(ValidationFailed):
        Citation("s", "", D)
    for factory in (
        lambda: Finding("f", "", (citation,), "high"),
        lambda: Finding("f", "claim", (), "high"),
        lambda: Finding("f", "claim", (citation,), "certain"),
        lambda: RoleResult(ResearchRole.COORDINATOR, "a", RoleOutcome.SUCCESS, (finding,)),
        lambda: RoleResult(ResearchRole.RESEARCHER, "a", RoleOutcome.SUCCESS),
        lambda: RoleResult(ResearchRole.CRITIC, "a", RoleOutcome.BLOCKED),
        lambda: ResearchNode("", ResearchRole.RESEARCHER),
        lambda: ResearchNode("a", ResearchRole.RESEARCHER, ("a",)),
        lambda: CitationVerification("", ()),
        lambda: CitationVerification("v", ("f",), ("f",), ("reason",)),
    ):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            factory()
    dag = ResearchDag(
        "q1",
        (
            ResearchNode("research", ResearchRole.RESEARCHER),
            ResearchNode("verify", ResearchRole.CITATION_VERIFIER, ("research",)),
        ),
    )
    assert dag.execution_order() == ("research", "verify")
    assert dag.parallel_groups() == (("research",), ("verify",))
    with pytest.raises(ValidationFailed):
        ResearchDag("q", (ResearchNode("a", ResearchRole.RESEARCHER, ("missing",)),))
    conflict = Conflict("c", ConflictKind.DIRECT_CONTRADICTION, "f1", "f2", "different")
    assert conflict.is_unresolved
    with pytest.raises(PolicyViolation):
        replace(conflict, resolved_by="researcher")
    with pytest.raises(PolicyViolation):
        verification.assert_independent(frozenset({"verifier"}))
