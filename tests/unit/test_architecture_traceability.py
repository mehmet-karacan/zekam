"""Source relationships are declarations, never manufactured test receipts."""

import pytest

from zekam.application.architecture_traceability import (
    build_source_traceability,
    task_package_scopes,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed


def _task(text):
    return {
        "source_digest": digest("task"),
        "requirements": [{"id": "ZKM-REQ-001"}, {"id": "ZKM-REQ-002"}],
        "capabilities": [{"id": "CAP-01"}, {"id": "CAP-02"}],
        "package_scopes": task_package_scopes(text),
    }


def test_only_explicit_input_scope_and_test_paths_link_to_source():
    task = _task(
        "### WP-02 — Native\n**Girdi/kapsam:** REQ-001/002; CAP-01/02.\n"
        "`tests/test_native.py` and `tests/test_missing.py`\n"
        "## Other section\n`tests/test_unrelated.py`\n"
    )
    report = build_source_traceability(
        task,
        [
            {
                "id": "source-native",
                "path": "tests/test_native.py",
                "digest": digest("native"),
                "tests": [{"name": "test_native", "line": 5, "run": "must-not-be-adopted"}],
            }
        ],
    )
    scope = report["package_scopes"][0]
    assert scope["requirements"] == ["ZKM-REQ-001", "ZKM-REQ-002"]
    assert scope["capabilities"] == ["CAP-01", "CAP-02"]
    assert len(report["edges"]) == 4
    assert all(edge["authority_line"] == 2 for edge in report["edges"])
    assert report["missing_test_paths"] == ["tests/test_missing.py"]
    assert scope["test_links"][0]["state"] == "test-source-found"
    assert scope["test_links"][0]["tests"] == [{"name": "test_native", "line": 5}]
    assert all(
        link["run"] is None and link["evidence"] is None and link["execution_commit"] is None
        for link in scope["test_links"]
    )
    assert report["execution_evidence_attached"] is False
    assert report["release_chain_complete"] is False and report["acceptance_verified"] is False
    assert report["grants_authority"] is False
    assert (
        digest({key: value for key, value in report.items() if key != "traceability_digest"})
        == report["traceability_digest"]
    )


def test_unmapped_requirements_and_packages_without_tests_stay_visible():
    report = build_source_traceability(_task("### WP-02 — Source\n**Girdi/kapsam:** CAP-01\n"), [])
    assert report["requirements_without_explicit_scope"] == ["ZKM-REQ-001", "ZKM-REQ-002"]
    assert report["packages_without_declared_tests"] == ["WP-02"]


def test_historical_scope_and_free_mentions_do_not_create_edges():
    task = _task(
        "REQ-001 CAP-01\n### WP-02 — Current\nREQ-002 mentioned outside input scope.\n## Ek A — Old\n### WP-99 — Old scope\n**Girdi/kapsam:** REQ-001; CAP-01\n"
    )
    report = build_source_traceability(task, [])
    assert report["edges"] == []
    assert [scope["id"] for scope in report["package_scopes"]] == ["WP-02"]


@pytest.mark.parametrize("scope", ["REQ-999", "CAP-99"])
def test_unknown_scope_identity_rejected(scope):
    with pytest.raises(ValidationFailed, match="unknown"):
        build_source_traceability(_task(f"### WP-02 — Source\n**Girdi/kapsam:** {scope}\n"), [])


def test_duplicate_package_and_unsafe_test_paths_rejected():
    with pytest.raises(ValidationFailed, match="duplicate"):
        task_package_scopes("### WP-02 — One\n### WP-02 — Two\n")
    with pytest.raises(ValidationFailed, match="path"):
        task_package_scopes("### WP-02 — One\n`tests/../outside.py`\n")
