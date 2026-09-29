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
