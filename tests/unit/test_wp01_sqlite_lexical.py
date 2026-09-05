from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.suites.wp01_sqlite_lexical import _fts_query, run


def test_fts_query_is_deterministic_and_quoted() -> None:
    expression, tokens = _fts_query("SKYRSM-5661 SKYRSM")
    assert expression == '"5661" OR "skyrsm"'
    assert tokens == {"5661", "skyrsm"}


@pytest.mark.parametrize("value", ["", "?", " "])
def test_fts_query_rejects_empty_token_set(value: str) -> None:
    with pytest.raises(ValueError):
        _fts_query(value)


def test_run_rejects_existing_root_before_loading_corpus(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir()
    with pytest.raises(ValueError, match="must not exist"):
        run(corpus_path=tmp_path / "missing", root=root, target_count=100, rebuild=False)
