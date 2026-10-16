"""Real local Git fixtures for complete unit-test publication range capture."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.unit.test_git_publication_transport import _git
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.git import publication_review as subject


@pytest.fixture
def change_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    test = "src/test/ATest.java"
    (root / "src/test").mkdir(parents=True)
    (root / test).write_bytes(b"class ATest {}\n")
    _git(root, "init")
    _git(root, "add", "src")
    _git(
        root,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "baseline",
    )
    base = _git(root, "rev-parse", "HEAD").strip()
    return SimpleNamespace(root=root, test=test, base=base)


def commit(w):
    _git(w.root, "add", "--all")
    _git(
        w.root,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "candidate",
    )
    return _git(w.root, "rev-parse", "HEAD").strip()


def capture(w, head):
    return subject.capture_review_changes(
        w.root, base_revision=w.base, head=head, allowed_paths={w.test}
    )


def test_capture_preserves_exact_blob_newlines_and_full_range(change_repo):
    w = change_repo
    (w.root / w.test).write_bytes(b"class ATest { /* extra */ }\n\n")
    head = commit(w)
    proof = capture(w, head)
    change = proof["changes"][0]
    assert change["before"] == "class ATest {}\n"
    assert change["after"].endswith("\n\n")
    assert proof["head"] == head and proof["base_revision"] == w.base
    assert change["status"] == "M" and len(change["old_blob"]) == len(head)
    assert proof["grants_authority"] is False


@pytest.mark.parametrize("change", ["unreviewed", "delete", "binary", "no-change", "invalid-utf8"])
def test_capture_refuses_changes_not_covered_by_unit_review(change_repo, change):
    w = change_repo
    if change == "unreviewed":
        (w.root / "other.txt").write_bytes(b"unreviewed\n")
    elif change == "delete":
        (w.root / w.test).unlink()
    elif change == "binary":
        (w.root / w.test).write_bytes(b"binary\0test")
    elif change == "invalid-utf8":
        (w.root / w.test).write_bytes(b"\xff\n")
    if change == "no-change":
        head = w.base
    else:
        head = commit(w)
    with pytest.raises(PolicyViolation):
        capture(w, head)


def test_capture_added_test_and_immutable_blob_tamper_rejection(change_repo):
    w = change_repo
    w.test = "src/test/NewTest.java"
    (w.root / w.test).write_bytes(b"class NewTest {}\n")
    head = commit(w)
    proof = capture(w, head)
    assert proof["changes"][0]["before"] is None
    corrupt = deepcopy(proof)
    corrupt["changes"][0]["after"] = "class Substituted {}\n"
    corrupt["change_digest"] = digest({k: v for k, v in corrupt.items() if k != "change_digest"})
    with pytest.raises(PolicyViolation, match="blob text/hash"):
        subject.validate_review_changes(corrupt, allowed_paths={w.test}, head=head)


def test_capture_ignores_inherited_git_directory_override(change_repo, monkeypatch, tmp_path):
    w = change_repo
    (w.root / w.test).write_bytes(b"class ATest { /* reviewed */ }\n")
    head = commit(w)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing-git"))
    assert capture(w, head)["head"] == head


def test_capture_rejects_unrelated_history(change_repo):
    w = change_repo
    _git(w.root, "checkout", "--orphan", "unrelated")
    (w.root / w.test).write_bytes(b"class Unrelated {}\n")
    head = commit(w)
    with pytest.raises(PolicyViolation):
        capture(w, head)
