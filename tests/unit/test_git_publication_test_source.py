"""Real temporary Git and parser fixtures; no production Maven acceptance claim."""

from dataclasses import replace
import os
from types import SimpleNamespace

import pytest

from tests.unit.test_git_publication_transport import _git
from tests.unit.test_unit_test_measurement import Env, request
from tests.unit.unit_test_runner_support import good_scenario
from zekam.application import git_publication_test_source as subject
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.git.publication import BoundGitPushTransport
from zekam.infrastructure.unit_test_runner.tree_fingerprint import (
    digest_test_tree,
    fingerprint_tree,
)


@pytest.fixture
def current_measurement(tmp_path, monkeypatch):
    env = Env(tmp_path)
    monkeypatch.setenv("PATH", env.fake.search_path + os.pathsep + os.environ["PATH"])
    root = env.root
    (root / ".gitignore").write_text("target/\n", encoding="utf-8")
    test = root / "src/test/java/p/ATest.java"
    test.parent.mkdir(parents=True)
    test.write_text("package p; class ATest {}\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "add", "pom.xml", "src", ".gitignore")
    _git(
        root,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    _git(root, "remote", "add", "origin", str(remote))
    target = BoundGitPushTransport(root).inspect(remote="origin", ref="refs/heads/main")
    req = replace(request(), source_revision=target.head)
    env.fake.set(**good_scenario())
    record = env.measure(req)
    tree = digest_test_tree(root, ("",))
    candidate = {
        "files": [list(pair) for pair in sorted(tree.items())],
        "test_tree_digest": fingerprint_tree(tree),
    }
    return SimpleNamespace(
        root=root,
        req=req,
        record=record.to_payload(),
        measured=record.binding.to_payload(),
        candidate=candidate,
        target=target,
        env=env,
    )


def inspect(world):
    return subject._inspect_measurement(
        world.root,
        request=world.req,
        record=world.record,
        measured=world.measured,
        candidate=world.candidate,
        target=world.target,
        target_modules=("",),
    )


def test_current_source_reparses_exact_reports_and_keeps_jvm_probe_unknown(current_measurement):
    report = inspect(current_measurement)
    assert report["current_source_verified"] is True
    assert report["raw_reports_verified"] is True
    assert report["runtime_jvm_reprobed"] is False
    assert report["test_tree_digest"] == current_measurement.candidate["test_tree_digest"]


@pytest.mark.parametrize(
    "path",
    [
        "target/surefire-reports/TEST-p.ATest.xml",
        "target/site/jacoco/jacoco.xml",
        "target/jacoco.exec",
        "target/classes/p/A.class",
    ],
)
def test_current_source_rejects_changed_ignored_evidence(current_measurement, path):
    (current_measurement.root / path).write_bytes(b"changed ignored artifact")
    with pytest.raises(PolicyViolation, match="digest drift"):
        inspect(current_measurement)


def test_current_source_rejects_changed_test_tree_even_when_git_ignores_it(current_measurement):
    root = current_measurement.root
    ignored = root / ".git/info/exclude"
    ignored.write_text("src/test/ignored.txt\n", encoding="utf-8")
    (root / "src/test/ignored.txt").write_text("new test resource\n", encoding="utf-8")
    assert _git(root, "status", "--porcelain") == ""
    with pytest.raises(PolicyViolation, match="test tree drift"):
        inspect(current_measurement)


def test_current_source_rejects_new_clean_commit_with_same_code(current_measurement):
    root = current_measurement.root
    _git(
        root,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "new revision",
    )
    with pytest.raises(PolicyViolation, match="HEAD/target"):
        inspect(current_measurement)


def test_current_source_rejects_class_drift_during_report_parse(current_measurement, monkeypatch):
    original = subject._parse_reports

    def parse(outputs):
        result = original(outputs)
        (current_measurement.root / "target/classes/p/A.class").write_bytes(
            b"changed after first class read"
        )
        return result

    monkeypatch.setattr(subject, "_parse_reports", parse)
    with pytest.raises(PolicyViolation, match="digest drift"):
        inspect(current_measurement)


def test_current_source_rejects_unknown_report_kind_or_traversal(current_measurement):
    for key in ("unknown:report", "surefire:../TEST-outside.xml"):
        measured = current_measurement.measured | {"report_digests": [[key, digest("report")]]}
        with pytest.raises(PolicyViolation):
            subject._raw_reports(current_measurement.root, measured, ("",))


def test_current_source_rejects_wrong_report_counters(current_measurement):
    current_measurement.record = current_measurement.record | {"test_counts": [["executed", 999]]}
    with pytest.raises(PolicyViolation, match="counts drift"):
        inspect(current_measurement)
