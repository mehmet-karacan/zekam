"""Independent fixed Git identity batching tests; actual project remains read-only."""

from __future__ import annotations

import os
from collections import Counter
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity_startup import ROOT, SOURCE_REF

from zekam.application.local_continuity_source_plan import ContinuitySourceRecipe
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure import local_continuity_source_plan as implementation
from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource

BATCH = ("rev-parse", "--show-toplevel", "--verify", "HEAD^{commit}")
HEAD = "a" * 40


@pytest.fixture
def adapter() -> BoundedContinuitySource:
    recipe = ContinuitySourceRecipe(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        (SOURCE_REF,),
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    )
    return BoundedContinuitySource(ROOT, recipe)


def _wire(head: str = HEAD) -> bytes:
    return f"{ROOT}\n{head}\n".encode()


def test_actual_combined_observation_matches_two_separate_read_only_observations(
    adapter: BoundedContinuitySource, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (ROOT / SOURCE_REF).read_bytes()
    # The old two-call recipe is comparison evidence only, not newly allowed API scope.
    original = adapter._git
    environment = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
    }

    def baseline(*arguments: str) -> bytes:
        code, stdout, stderr = implementation._bounded_git_process(
            [
                "git",
                "--no-optional-locks",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.excludesFile={os.devnull}",
                "-c",
                "protocol.ext.allow=never",
                "-C",
                str(ROOT),
                *arguments,
            ],
            environment,
            None,
        )
        assert code == 0 and stderr == b""
        return stdout

    expected_root = baseline("rev-parse", "--show-toplevel")
    expected_head = baseline("rev-parse", "--verify", "HEAD^{commit}")
    calls: list[tuple[str, ...]] = []

    def observe(arguments: tuple[str, ...], **kwargs: Any) -> bytes:
        calls.append(arguments)
        return original(arguments, **kwargs)

    monkeypatch.setattr(adapter, "_git", observe)
    assert adapter._head() == expected_head.decode("ascii").removesuffix("\n")
    assert calls == [BATCH]
    assert expected_root == str(ROOT).encode() + b"\n"
    assert (ROOT / SOURCE_REF).read_bytes() == before


@pytest.mark.parametrize("head", ["a" * 40, "0123456789abcdef" * 4])
def test_canonical_sha1_and_sha256_head_are_exact(
    adapter: BoundedContinuitySource, monkeypatch: pytest.MonkeyPatch, head: str
) -> None:
    calls: list[tuple[str, ...]] = []

    def observe(arguments: tuple[str, ...]) -> bytes:
        calls.append(arguments)
        return _wire(head)

    monkeypatch.setattr(adapter, "_git", observe)
    assert adapter._head() == head
    assert calls == [BATCH]


@pytest.mark.parametrize(
    "wire",
    [
        b"",
        b"\n",
        str(ROOT).encode() + b"\n",
        HEAD.encode() + b"\n",
        _wire()[:-1],
        _wire() + b"\n",
        _wire() + b"extra\n",
        b"extra\n" + _wire(),
        _wire().replace(b"\n", b"\r\n"),
        _wire().replace(b"\n", b"\x00", 1),
        _wire().replace(b"akilli-kasa", b"different-root"),
        _wire().replace(b"/akilli-kasa\n", b"/akilli-kasa/\n"),
        b"\xff\n" + HEAD.encode() + b"\n",
        _wire(""),
        _wire("a" * 39),
        _wire("a" * 41),
        _wire("a" * 63),
        _wire("a" * 65),
        _wire("A" * 40),
        _wire("g" * 40),
        _wire(" " + HEAD),
        _wire(HEAD + " "),
        _wire(HEAD + "\t"),
        _wire("a" * 39 + "\x00"),
        _wire("a" * 39 + "é"),
        _wire("a" * 39 + "\u2028"),
        _wire("a" * 39 + "\u200b"),
        _wire(HEAD + "\n" + HEAD),
        _wire("a" * 20000),
    ],
)
def test_malformed_partial_extra_and_noncanonical_identity_rejected(
    adapter: BoundedContinuitySource, monkeypatch: pytest.MonkeyPatch, wire: bytes
) -> None:
    monkeypatch.setattr(adapter, "_git", lambda *args, **kwargs: wire)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        adapter._head()


@pytest.mark.parametrize("identity", ["_root", "_git_layout"])
@pytest.mark.parametrize("after_observation", [False, True])
def test_root_and_git_identity_drift_never_accepted(
    adapter: BoundedContinuitySource,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
    after_observation: bool,
) -> None:
    original = getattr(adapter, identity)
    calls: list[tuple[str, ...]] = []
    seen = 0

    def changed() -> Any:
        nonlocal seen
        seen += 1
        if not after_observation or seen > 1:
            return ("changed-identity",)
        return original()

    def observe(arguments: tuple[str, ...]) -> bytes:
        calls.append(arguments)
        return _wire()

    monkeypatch.setattr(adapter, identity, changed)
    monkeypatch.setattr(adapter, "_git", observe)
    with pytest.raises(PolicyViolation):
        adapter._head()
    assert calls == ([BATCH] if after_observation else [])


def test_complete_capture_repetition_preserved_and_git_count_reduced(
    adapter: BoundedContinuitySource, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_git, original_capture = adapter._git, adapter._capture_once
    calls: list[tuple[str, ...]] = []
    capture_calls = 0

    def observe(arguments: tuple[str, ...], **kwargs: Any) -> bytes:
        calls.append(arguments)
        return original_git(arguments, **kwargs)

    def capture() -> Any:
        nonlocal capture_calls
        capture_calls += 1
        return original_capture()

    monkeypatch.setattr(adapter, "_git", observe)
    monkeypatch.setattr(adapter, "_capture_once", capture)
    plan = adapter.capture()
    assert len(plan.files) == 1 and plan.files[0].relative_path == SOURCE_REF
    assert capture_calls == 2
    assert Counter(calls) == {
        BATCH: 4,
        ("ls-files", "--error-unmatch", "--", SOURCE_REF): 2,
        ("check-ignore", "--no-index", "--stdin", "-z"): 2,
    }


def test_head_change_between_capture_passes_still_rejects(
    adapter: BoundedContinuitySource, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = adapter._git
    observations = 0

    def observe(arguments: tuple[str, ...], **kwargs: Any) -> bytes:
        nonlocal observations
        if arguments == BATCH:
            observations += 1
            return _wire("a" * 40 if observations <= 2 else "b" * 40)
        return original(arguments, **kwargs)

    monkeypatch.setattr(adapter, "_git", observe)
    with pytest.raises(PolicyViolation, match="between complete"):
        adapter.capture()
    assert observations == 4


def test_fixed_batch_keeps_clean_environment_and_bounded_runner(
    adapter: BoundedContinuitySource, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[list[str], dict[str, str], bytes | None]] = []

    def run(command: list[str], environment: dict[str, str], data: bytes | None) -> Any:
        recorded.append((command, environment, data))
        return 0, _wire(), b""

    monkeypatch.setenv("GIT_DIR", "/foreign/repository")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "99")
    monkeypatch.setattr(implementation, "_bounded_git_process", run)
    assert adapter._head() == HEAD
    assert len(recorded) == 1
    command, environment, data = recorded[0]
    assert command[-4:] == list(BATCH)
    assert command[:2] == ["git", "--no-optional-locks"]
    for option in (
        f"core.hooksPath={os.devnull}",
        "core.fsmonitor=false",
        f"core.excludesFile={os.devnull}",
        "protocol.ext.allow=never",
    ):
        assert option in command
    assert environment == {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
    }
    assert data is None


@pytest.mark.parametrize(
    "arguments",
    [
        ("rev-parse", "--show-toplevel"),
        ("rev-parse", "--verify", "HEAD^{commit}"),
        (*BATCH, "--all"),
        ("rev-parse", "--show-toplevel", "--verify", "HEAD"),
        ("rev-parse", "--verify", "--show-toplevel", "HEAD^{commit}"),
        ("rev-parse", "--show-toplevel", "--verify", "foreign^{commit}"),
        ("rev-parse", "--show-toplevel", "--verify", "HEAD^{tree}"),
    ],
)
def test_batch_does_not_authorize_general_git_arguments(
    adapter: BoundedContinuitySource, monkeypatch: pytest.MonkeyPatch, arguments: Any
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Invalid fixed command must not reach process runner")

    monkeypatch.setattr(implementation, "_bounded_git_process", forbidden)
    with pytest.raises(ValidationFailed):
        adapter._git(arguments)


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "output-cap"])
def test_batch_process_failure_is_typed_and_never_returns_identity(
    adapter: BoundedContinuitySource, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def run(*args: Any, **kwargs: Any) -> Any:
        if failure == "timeout":
            raise PolicyViolation("Source fixed Git observation timed out")
        if failure == "nonzero":
            return 128, _wire(), b"failure"
        return 0, _wire(), b"x" * 16385

    monkeypatch.setattr(implementation, "_bounded_git_process", run)
    with pytest.raises(PolicyViolation):
        adapter._head()


@pytest.mark.parametrize("wire", [None, True, 1, "text", bytearray(_wire()), [_wire()]])
def test_nonbytes_process_identity_is_typed_rejection(
    adapter: BoundedContinuitySource, monkeypatch: pytest.MonkeyPatch, wire: Any
) -> None:
    monkeypatch.setattr(adapter, "_git", lambda *args, **kwargs: wire)
    with pytest.raises(PolicyViolation):
        adapter._head()
