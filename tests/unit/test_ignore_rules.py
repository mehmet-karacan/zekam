"""Yoksayma kurallarinin `.gitignore` alt kumesi davranisi."""

from __future__ import annotations

import pytest

from zekam.application.ignore_rules import IgnoreMatcher, parse_rule, system_deny_matcher

pytestmark = pytest.mark.unit


def matcher(*lines: str) -> IgnoreMatcher:
    return IgnoreMatcher.from_lines(lines)


@pytest.mark.parametrize("line", ["", "   ", "# yorum", "  # girintili yorum"])
def test_comment_and_blank_lines_are_skipped(line: str) -> None:
    assert parse_rule(line) is None


def test_simple_name_matches_at_any_depth() -> None:
    rules = matcher("node_modules/")
    assert rules.is_ignored("node_modules", is_directory=True)
    assert rules.is_ignored("web/node_modules", is_directory=True)
    assert not rules.is_ignored("node_modules_yedek", is_directory=True)


def test_directory_only_rule_does_not_match_file() -> None:
    rules = matcher("build/")
    assert rules.is_ignored("build", is_directory=True)
    assert not rules.is_ignored("build", is_directory=False)


def test_anchored_rule_matches_only_at_root() -> None:
    rules = matcher("/dist")
    assert rules.is_ignored("dist")
    assert not rules.is_ignored("web/dist")


def test_glob_star_stays_within_one_segment() -> None:
    rules = matcher("*.log")
    assert rules.is_ignored("app.log")
    assert rules.is_ignored("logs/app.log")
    assert not rules.is_ignored("app.log.txt")


def test_double_star_crosses_segments() -> None:
    rules = matcher("src/**/gecici")
    assert rules.is_ignored("src/gecici")
    assert rules.is_ignored("src/a/b/gecici")
    assert not rules.is_ignored("baska/src/gecici")


def test_question_mark_matches_single_character() -> None:
    rules = matcher("dosya?.txt")
    assert rules.is_ignored("dosya1.txt")
    assert not rules.is_ignored("dosya12.txt")


def test_negation_reverses_previous_rule() -> None:
    rules = matcher("*.env", "!ornek.env")
    assert rules.is_ignored("uretim.env")
    assert not rules.is_ignored("ornek.env")


def test_last_matching_rule_wins() -> None:
    rules = matcher("!ornek.env", "*.env")
    assert rules.is_ignored("ornek.env")


def test_path_under_ignored_directory_is_ignored() -> None:
    rules = matcher("gizli/")
    assert rules.is_path_ignored("gizli/icerik.txt")
    assert not rules.is_path_ignored("acik/icerik.txt")


def test_extended_rules_are_applied_in_order() -> None:
    base = matcher("*.tmp")
    extra = matcher("!korunacak.tmp")
    combined = base.extended(extra)
    assert combined.is_ignored("gecici.tmp")
    assert not combined.is_ignored("korunacak.tmp")


def test_matcher_without_rules_ignores_nothing() -> None:
    assert not IgnoreMatcher().is_ignored("herhangi.txt")


@pytest.mark.parametrize(
    "path",
    [
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "src/__pycache__",
    ],
)
def test_system_deny_list_excludes_known_directories(path: str) -> None:
    assert system_deny_matcher().is_ignored(path, is_directory=True)


@pytest.mark.parametrize(
    "path",
    [
        "gizli.pem",
        "sunucu.key",
        "kimlik.p12",
        "id_rsa",
        ".env",
        ".env.production",
        "a.pyc",
        "tsconfig.tsbuildinfo",
    ],
)
def test_system_deny_list_excludes_credential_files(path: str) -> None:
    assert system_deny_matcher().is_ignored(path)


@pytest.mark.parametrize("path", [".env.example", ".env.sample"])
def test_example_environment_files_are_kept(path: str) -> None:
    assert not system_deny_matcher().is_ignored(path)


def test_regular_source_files_are_not_denied() -> None:
    rules = system_deny_matcher()
    for path in ("src/zekam/main.py", "README.md", "pyproject.toml"):
        assert not rules.is_ignored(path), path
