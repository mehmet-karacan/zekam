"""Sorgu-farkindalikli kanit penceresi sozlesmesi (RAG26-R03)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from zekam.application.embedded_project_rag import (
    MAX_ANSWER_EXCERPT_CHARS,
    _build_excerpt,
    _excerpt_window,
)

_IMPORTS = "".join(f"import org.springframework.batch.core.Part{i};\n" for i in range(30))
_CONTROLLER = (
    "@RestController\n"
    '@RequestMapping("/batch-job")\n'
    "public class BatchJobController {\n"
    '    @PostMapping("/stop")\n'
    "    public void stop() {}\n"
    '    @PostMapping("/start")\n'
    "    public void start() {}\n"
    "}\n"
)
_TEXT = "package tr.gpu.controller;\n" + _IMPORTS + "\n" + _CONTROLLER


@pytest.mark.unit
def test_synthetic_prefix_is_longer_than_the_excerpt_budget() -> None:
    assert len(_IMPORTS) > MAX_ANSWER_EXCERPT_CHARS


@pytest.mark.unit
def test_query_aware_window_selects_endpoint_body_not_import_block() -> None:
    window, truncated, _ = _excerpt_window(
        _TEXT, MAX_ANSWER_EXCERPT_CHARS, "BatchJobController hangi HTTP endpoint sunar"
    )
    assert truncated is True
    assert '@PostMapping("/stop")' in window
    assert '@PostMapping("/start")' in window
    assert "import org.springframework.batch.core.Part0;" not in window
    assert len(window) <= MAX_ANSWER_EXCERPT_CHARS


@pytest.mark.unit
def test_window_is_exact_source_lines_with_reported_line_range() -> None:
    window, _, line_range = _excerpt_window(_TEXT, MAX_ANSWER_EXCERPT_CHARS, "BatchJobController")
    lines = _TEXT.splitlines(keepends=True)
    start, end = line_range
    assert "".join(lines[start - 1 : end]) == window


@pytest.mark.unit
def test_short_chunk_is_returned_unchanged() -> None:
    window, truncated, line_range = _excerpt_window("a\nb\n", 500, "anything")
    assert (window, truncated, line_range) == ("a\nb\n", False, (1, 2))


@pytest.mark.unit
def test_query_without_matches_falls_back_to_leading_window() -> None:
    window, truncated, line_range = _excerpt_window(_TEXT, 200, "zzzqqq")
    assert truncated is True
    assert window.startswith("package tr.gpu.controller;")
    assert line_range[0] == 1


@pytest.mark.unit
def test_single_overlong_line_is_hard_cut_with_range() -> None:
    window, truncated, line_range = _excerpt_window("x" * 900, 500, "x")
    assert truncated is True
    assert len(window) == 500
    assert line_range == (1, 1)


@pytest.mark.unit
def test_build_excerpt_prefers_the_chunk_whose_window_matches_the_query() -> None:
    def view(text: str) -> SimpleNamespace:
        return SimpleNamespace(text=text, content_digest="sha256:x", locator=None)

    views = {"c1": view(_IMPORTS * 2), "c2": view(_TEXT)}
    answer = SimpleNamespace(used_chunk_ids=("c1", "c2"), citations=(object(), object()))
    text, meta = _build_excerpt(answer, views, "BatchJobController endpoint")
    assert text is not None and meta is not None
    assert meta["chunk_id"] == "c2"
    assert '@PostMapping("/stop")' in text
    assert meta["excerpt_line_range"][0] > 1


def _views(**texts: str) -> dict[str, SimpleNamespace]:
    return {
        chunk_id: SimpleNamespace(
            text=text,
            content_digest=f"sha256:{chunk_id}",
            locator=SimpleNamespace(
                relative_path=f"{chunk_id}.java",
                line_start=1,
                as_dict=lambda chunk_id=chunk_id: {"relative_path": f"{chunk_id}.java"},
            ),
        )
        for chunk_id, text in texts.items()
    }


@pytest.mark.unit
def test_multi_window_excerpts_are_bounded_exact_and_ordered_by_relevance() -> None:
    from zekam.application.embedded_project_rag import (
        MAX_ANSWER_EXCERPTS,
        MAX_ANSWER_EXCERPTS_TOTAL_CHARS,
        _build_excerpts,
    )

    views = _views(
        c1="x\n" * 10,
        c2=_CONTROLLER,
        c3="BatchJobController endpoint\n" * 3,
        c4="BatchJobController endpoint listesi\n" * 3,
        c5="BatchJobController\n",
    )
    answer = SimpleNamespace(used_chunk_ids=tuple(views), citations=(object(),) * 5)
    items = _build_excerpts(answer, views, "BatchJobController endpoint")
    assert 1 <= len(items) <= MAX_ANSWER_EXCERPTS
    assert sum(len(item["text"]) for item in items) <= MAX_ANSWER_EXCERPTS_TOTAL_CHARS
    assert "c1" not in {item["chunk_id"] for item in items}
    for item in items:
        assert item["excerpt_digest"] != item["content_digest"]
        assert item["text"] in views[item["chunk_id"]].text


@pytest.mark.unit
def test_related_chunks_get_a_relevance_floor_but_unrelated_zero_match_chunks_do_not() -> None:
    from zekam.application.embedded_project_rag import _build_excerpts

    views = _views(
        head=_CONTROLLER, tail="@PostMapping\nvoid other() {}\n", noise="tamamen ilgisiz\n"
    )
    answer = SimpleNamespace(used_chunk_ids=("head", "tail", "noise"), citations=(object(),) * 3)
    plain = _build_excerpts(answer, views, "BatchJobController")
    related = _build_excerpts(answer, views, "BatchJobController", related_ids=("tail",))
    assert [item["chunk_id"] for item in plain] == ["head"]
    assert [item["chunk_id"] for item in related] == ["head", "tail"]


@pytest.mark.unit
def test_multi_window_excerpts_are_empty_without_citations() -> None:
    from zekam.application.embedded_project_rag import _build_excerpts

    answer = SimpleNamespace(used_chunk_ids=(), citations=())
    assert _build_excerpts(answer, {}, "x") == []


@pytest.mark.unit
def test_leading_annotations_of_the_type_declaration_join_the_window() -> None:
    from zekam.application.embedded_project_rag import _excerpt_window

    text = (
        "package x;\n" + "import a.B;\n" * 60 + "@RestController\n"
        '@RequestMapping("/batch-job")\n'
        "public class BatchJobController {\n"
        '    @PostMapping("/stop")\n'
        "    public void stop() {}\n"
    )
    window, _, (first, last) = _excerpt_window(text, 400, "BatchJobController stop")
    assert '@RequestMapping("/batch-job")' in window
    assert "@RestController" in window
    lines = text.splitlines(keepends=True)
    assert "".join(lines[first - 1 : last]) == window
    assert len(window) <= 400


@pytest.mark.unit
def test_annotation_extension_is_bounded() -> None:
    from zekam.application.embedded_project_rag import (
        MAX_LEADING_ANNOTATION_LINES,
        _excerpt_window,
    )

    text = ("x" * 20 + "\n") * 40 + "@A\n" * 20 + "public class Target {\n    void run() {}\n}\n"
    window, truncated, _range = _excerpt_window(text, 200, "Target")
    assert truncated is True
    assert 0 < window.count("@A") <= MAX_LEADING_ANNOTATION_LINES


@pytest.mark.unit
def test_secret_looking_source_lines_never_reach_the_excerpt() -> None:
    from zekam.application.embedded_project_rag import (
        REDACTED_LINE,
        _build_excerpt,
        _build_excerpts,
        _redact_secret_lines,
    )

    body = (
        'class GeneralUtils {\n    String anahtar = "2025_UYDURMA_DEGER_1234";\n'
        '    String api_key = "p9x7m2q4v8n6w1z3";\n    int port = 9001;\n}\n'
    )
    masked, redacted = _redact_secret_lines(body)
    assert redacted is True
    assert "2025_UYDURMA_DEGER_1234" not in masked
    assert "p9x7m2q4v8n6w1z3" not in masked
    assert masked.count(REDACTED_LINE) == 2
    assert "int port = 9001;" in masked
    views = _views(c1=body)
    answer = SimpleNamespace(used_chunk_ids=("c1",), citations=(object(),))
    text, meta = _build_excerpt(answer, views, "GeneralUtils port")
    assert meta is not None and meta["redacted"] is True
    assert "2025_UYDURMA_DEGER_1234" not in (text or "")
    items = _build_excerpts(answer, views, "GeneralUtils port")
    assert items and items[0]["redacted"] is True
    assert "p9x7m2q4v8n6w1z3" not in items[0]["text"]


@pytest.mark.unit
def test_clean_source_is_returned_unredacted() -> None:
    from zekam.application.embedded_project_rag import _redact_secret_lines

    text = "public class Ok {\n    int port = 9001;\n}\n"
    assert _redact_secret_lines(text) == (text, False)


@pytest.mark.unit
def test_short_ambiguous_question_suggests_one_clarification_from_real_citations() -> None:
    from zekam.application.embedded_project_rag import _clarification

    citations = [
        {"source_ref": "gpu-backend/src/main/java/batch/flow/hesaplama/A.java"},
        {"source_ref": "gpu-backend/src/main/java/batch/flow/olusturma/B.java"},
        {"source_ref": "gpu-backend/src/main/java/batch/flow/olusturma/C.java"},
    ]
    result = _clarification("Hakedis nasil calisir?", (), citations)
    assert result["suggested"] is True
    assert result["reason"] == "short-ambiguous-query"
    assert result["candidate_areas"] == ["flow/hesaplama", "flow/olusturma"]
    specific = _clarification(
        "HakedisHesaplamaJob hangi step'leri sirayla calistirir?", (), citations
    )
    assert specific["suggested"] is False and specific["candidate_areas"] == []
    with_identifier = _clarification("Hakedis", ("HakedisHesaplamaJob",), citations)
    assert with_identifier["suggested"] is False


@pytest.mark.unit
def test_enumeration_questions_get_more_windows_within_a_larger_fixed_total() -> None:
    from zekam.application.embedded_project_rag import (
        MAX_ANSWER_EXCERPTS,
        MAX_ANSWER_EXCERPTS_ENUMERATION,
        MAX_ANSWER_EXCERPTS_TOTAL_CHARS_ENUMERATION,
        _build_excerpts,
    )

    views = _views(**{f"c{i}": f"JobConfig{i} hangi job\n" * 20 for i in range(10)})
    answer = SimpleNamespace(used_chunk_ids=tuple(views), citations=(object(),) * 10)
    normal = _build_excerpts(answer, views, "hangi job")
    listing = _build_excerpts(answer, views, "hangi job", enumeration=True)
    assert len(normal) <= MAX_ANSWER_EXCERPTS
    assert MAX_ANSWER_EXCERPTS < len(listing) <= MAX_ANSWER_EXCERPTS_ENUMERATION
    assert sum(len(item["text"]) for item in listing) <= MAX_ANSWER_EXCERPTS_TOTAL_CHARS_ENUMERATION
