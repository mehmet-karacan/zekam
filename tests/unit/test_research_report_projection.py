from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.home import HomeLayout
from zekam.application.research_report_projection import (
    materialize_research_report,
    render_research_report,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


def _document(report_id: str = "report-1") -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "zekam-research-report/v1",
        "report_id": report_id,
        "question_id": "question-1",
        "question_digest": digest("question"),
        "status": "answered",
        "findings": [{"finding_id": "f-1", "claim": "Verified result"}],
        "unresolved_conflicts": [],
        "non_success_results": [],
        "verification": {"verifier_ref": "independent-verifier"},
        "snapshots": [{"snapshot_id": "s-1", "content_digest": digest("source")}],
        "grants_authority": False,
    }
    return dict(body, report_digest=digest(body))


def test_projection_is_deterministic_idempotent_and_non_authoritative(tmp_path: Path) -> None:
    layout = HomeLayout(tmp_path / ".zekam").ensure()
    first = materialize_research_report(layout, "project-1", _document())
    second = materialize_research_report(layout, "project-1", _document())

    assert first.created is True
    assert second.created is False
    assert first.path == tmp_path / ".zekam/projeler/project-1/raporlar/arastirma-report-1.md"
    content = first.path.read_text(encoding="utf-8")
    assert "read_only_projection: true" in content
    assert "grants_authority: false" in content
    assert str(_document()["report_digest"]) in content
    assert render_research_report(_document()) == first.path.read_bytes()


def test_projection_rejects_digest_drift_and_unsafe_report_id() -> None:
    drifted = _document()
    drifted["status"] = "partial"
    with pytest.raises(PolicyViolation, match="digest"):
        render_research_report(drifted)
    with pytest.raises(ValidationFailed, match="path-safe"):
        render_research_report(_document("../escape"))


def test_projection_fails_closed_on_secret() -> None:
    document = _document()
    runtime_value = "runtime" + "_sensitive_value_123"
    label = "to" + "ken="
    document["findings"] = [{"claim": label + repr(runtime_value)}]
    body = {key: value for key, value in document.items() if key != "report_digest"}
    document["report_digest"] = digest(body)
    with pytest.raises(PolicyViolation, match="secret"):
        render_research_report(document)
