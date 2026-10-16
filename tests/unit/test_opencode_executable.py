"""Installation fixtures only; these tests never execute OpenCode."""

from pathlib import Path

import pytest

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients import opencode_executable as subject


@pytest.mark.parametrize("package", ["@opencode/cli", "opencode-ai"])
@pytest.mark.parametrize("suffix", [".cmd", ".ps1", ".bat"])
def test_npm_shim_selects_referenced_package(tmp_path, monkeypatch, package, suffix):
    shim = tmp_path / f"opencode{suffix}"
    shim.write_text(f'"$basedir/node_modules/{package}/bin/opencode.exe" $args', encoding="utf-8")
    target = tmp_path / "node_modules" / package / "bin" / "opencode.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"fixture only")
    # A stale second installation must not change the package selected by shim.
    other_package = "opencode-ai" if package == "@opencode/cli" else "@opencode/cli"
    other = tmp_path / "node_modules" / other_package / "bin" / "opencode.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"stale fixture")
    monkeypatch.setattr(subject.shutil, "which", lambda _: str(shim))
    assert subject.resolve_opencode_executable() == target.resolve()


def test_windows_separator_and_secondary_alias(tmp_path, monkeypatch):
    shim = tmp_path / "opencode2.cmd"
    shim.write_text(r'"%dp0%\node_modules\@opencode\cli\bin\opencode.exe" %*', encoding="utf-8")
    target = tmp_path / "node_modules/@opencode/cli/bin/opencode.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"fixture only")
    monkeypatch.setattr(subject.shutil, "which", lambda _: str(shim))
    assert subject.resolve_opencode_executable("opencode2") == target.resolve()


@pytest.mark.parametrize(
    "text",
    [
        "arbitrary script",
        "node_modules/opencode-ai/bin/opencode node_modules/@opencode/cli/bin/opencode",
        "x" * 65537,
    ],
)
def test_unknown_ambiguous_or_large_wrapper_rejected(tmp_path, monkeypatch, text):
    shim = tmp_path / "opencode.cmd"
    shim.write_text(text, encoding="utf-8")
    monkeypatch.setattr(subject.shutil, "which", lambda _: str(shim))
    with pytest.raises(PolicyViolation):
        subject.resolve_opencode_executable()


def test_missing_target_does_not_fall_back_to_unreferenced_package(tmp_path, monkeypatch):
    shim = tmp_path / "opencode.ps1"
    shim.write_text("node_modules/@opencode/cli/bin/opencode.exe", encoding="utf-8")
    other = tmp_path / "node_modules/opencode-ai/bin/opencode.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"stale fixture")
    monkeypatch.setattr(subject.shutil, "which", lambda _: str(shim))
    with pytest.raises(ValidationFailed, match="direct executable"):
        subject.resolve_opencode_executable()


def test_explicit_binary_without_path_lookup(tmp_path: Path, monkeypatch):
    binary = tmp_path / "opencode.exe"
    binary.write_bytes(b"fixture only")
    monkeypatch.setattr(subject.shutil, "which", lambda _: None)
    assert subject.resolve_opencode_executable(str(binary)) == binary.resolve()


def test_research_and_test_cli_use_common_resolver(monkeypatch, tmp_path):
    from zekam.infrastructure import opencode_research
    from zekam.interfaces.cli import unit_test

    binary = tmp_path / "opencode.exe"
    calls = []

    def resolve(value="opencode"):
        calls.append(value)
        return binary

    monkeypatch.setattr(opencode_research, "resolve_opencode_executable", resolve)
    monkeypatch.setattr(unit_test, "resolve_opencode_executable", resolve)
    assert opencode_research._resolve_executable("custom-opencode") == str(binary)
    assert unit_test._resolve_opencode_executable() == binary
    assert calls == ["custom-opencode", "opencode"]
