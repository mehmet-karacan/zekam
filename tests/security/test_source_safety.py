"""Haricî kaynak agacina karsi guvenlik sinirlari.

Bu testler "yanlislikla yazma" ve "sessiz sizinti" senaryolarini negatif olarak
dogrular.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from zekam.application.source_discovery import DiscoveryPolicy, SkipReason, discover
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.git import source_reader

pytestmark = pytest.mark.security


def _write(root: Path, relative: str, body: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="\n")


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "kaynak"
    _write(root, "src/ana.py", "print(1)\n")
    _write(root, "README.md", "# kaynak\n")
    return root


def _snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return snapshot


# -- Git salt okunur allowlist ---------------------------------------------------


@pytest.mark.parametrize(
    "subcommand",
    ["commit", "push", "checkout", "clean", "reset", "fetch", "pull", "merge", "gc", "submodule"],
)
def test_write_capable_git_subcommands_are_rejected(source_tree: Path, subcommand: str) -> None:
    with pytest.raises(PolicyViolation, match="Salt okunur olmayan"):
        source_reader.run_read_only(source_tree, subcommand)


def test_empty_git_command_is_rejected(source_tree: Path) -> None:
    with pytest.raises(PolicyViolation):
        source_reader.run_read_only(source_tree)


def test_allowlist_contains_only_read_only_subcommands() -> None:
    forbidden = {"commit", "push", "checkout", "reset", "clean", "fetch", "pull", "merge", "apply"}
    assert forbidden.isdisjoint(source_reader.READ_ONLY_COMMANDS)


def test_non_repository_directory_is_not_reported_as_repository(source_tree: Path) -> None:
    # Ust dizinde depo olsa bile alt dizin depo koku sayilmaz.
    assert source_reader.is_git_repository(source_tree) is False


def test_observe_returns_none_for_non_repository(source_tree: Path) -> None:
    assert source_reader.observe(source_tree) is None


# -- Kesif kaynagi degistirmez ----------------------------------------------------


def test_discovery_leaves_source_bit_identical(source_tree: Path) -> None:
    before = _snapshot(source_tree)
    discover(source_tree)
    assert _snapshot(source_tree) == before


def test_discovery_creates_no_new_files(source_tree: Path) -> None:
    before = {path.relative_to(source_tree).as_posix() for path in source_tree.rglob("*")}
    discover(source_tree)
    after = {path.relative_to(source_tree).as_posix() for path in source_tree.rglob("*")}
    assert after == before


# -- Secret sizintisi -------------------------------------------------------------


def test_file_with_private_key_is_never_indexed(source_tree: Path) -> None:
    _write(source_tree, "gizli/anahtar.txt", "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n")
    report = discover(source_tree)
    assert "gizli/anahtar.txt" not in {item.relative_path for item in report.files}
    assert report.skipped_by(SkipReason.SECRET)


def test_pem_and_env_files_are_denied_before_reading(source_tree: Path) -> None:
    _write(source_tree, "sunucu.pem", "-----BEGIN PRIVATE KEY-----\n")
    _write(source_tree, ".env", "DB_PASSWORD=Kx7pQm2ZrT9wLb4Nc1Vd\n")
    report = discover(source_tree)
    indexed = {item.relative_path for item in report.files}
    assert "sunucu.pem" not in indexed
    assert ".env" not in indexed


def test_secret_value_never_appears_in_report(source_tree: Path) -> None:
    secret = "Kx7pQm2ZrT9wLb4Nc1Vd"
    _write(source_tree, "ayar.py", f'auth_token = "{secret}"\n')
    rendered = repr(discover(source_tree).as_dict())
    assert secret not in rendered


def test_secret_finding_reports_path_and_line(source_tree: Path) -> None:
    _write(source_tree, "ayar.py", 'x = 1\nauth_token = "Kx7pQm2ZrT9wLb4Nc1Vd"\n')
    finding = discover(source_tree).secrets[0]
    assert finding.relative_path == "ayar.py"
    assert finding.line_number == 2


# -- Sinir asimi ------------------------------------------------------------------


def test_traversal_target_is_rejected(tmp_path: Path) -> None:
    from zekam.application.source_discovery import assert_within_root

    root = tmp_path / "kok"
    root.mkdir()
    with pytest.raises(PolicyViolation):
        assert_within_root(root / ".." / "disari", root)


def test_zero_limits_produce_empty_index(source_tree: Path) -> None:
    report = discover(source_tree, policy=DiscoveryPolicy(max_total_files=0))
    assert report.files == ()
    assert report.truncated


def test_many_small_files_respect_total_byte_limit(tmp_path: Path) -> None:
    root = tmp_path / "bomba"
    root.mkdir()
    for index in range(50):
        _write(root, f"dosya-{index}.txt", "x" * 100)
    report = discover(root, policy=DiscoveryPolicy(max_total_bytes=500))
    assert report.truncated
    assert report.total_bytes <= 500
