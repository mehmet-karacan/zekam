"""P09 alan ciktilarinin kanonik JSON schema'lariyla tutarliligi.

Schema ve kod ayni sozlesmeyi anlatmalidir: zorunlu alanlar eksik olamaz, kodun
urettigi alanlar schema'da tanimsiz olamaz.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from zekam.application.intake_service import IntakeService
from zekam.domain.canonical import digest
from zekam.domain.research import (
    Citation,
    CitationVerification,
    Finding,
    ReportStatus,
    ResearchBudget,
    ResearchQuestion,
    ResearchReport,
    SourceKind,
    SourcePolicy,
    SourceSnapshot,
    derive_plan_candidate,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
CONTENT = digest("content")


def _schema(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
    return document


def _assert_conforms(document: dict[str, Any], schema_name: str) -> None:
    schema = _schema(schema_name)
    assert set(schema["required"]) <= set(document), schema_name
    assert set(document) <= set(schema["properties"]), schema_name


def _report() -> ResearchReport:
    finding = Finding(
        finding_id="f1",
        claim="iterative scan acilmali",
        citations=(Citation(snapshot_id="s1", locator_detail="line 1-5", content_digest=CONTENT),),
        confidence="high",
    )
    return ResearchReport(
        report_id="r1",
        question_id="q1",
        question_digest=digest("q"),
        findings=(finding,),
        unresolved_conflicts=(),
        non_success_results=(),
        verification=CitationVerification(verifier_ref="v", verified_finding_ids=("f1",)),
        snapshots=(
            SourceSnapshot(
                snapshot_id="s1",
                kind=SourceKind.FILE,
                locator="docs/RAG.md",
                content_digest=CONTENT,
                captured_at=NOW,
            ),
        ),
        status=ReportStatus.ANSWERED,
    )


def test_intake_resolution_schema_ile_uyumlu() -> None:
    outcome = IntakeService().resolve("gpu projesini arastir", now=NOW, projects=())
    document = outcome.resolution.body()
    _assert_conforms(document, "intake-resolution.schema.json")
    assert document["grants_authority"] is False


def test_research_question_schema_ile_uyumlu() -> None:
    question = ResearchQuestion(
        question_id="q1",
        question="soru",
        project_ref="zekam",
        work_ref="w",
        intent_digest=digest("i"),
        source_revision="rev",
        policy=SourcePolicy(allowed_kinds=frozenset({SourceKind.FILE}), project_scope="zekam"),
        budget=ResearchBudget(max_tokens=10, max_cost_units=1, max_seconds=10),
        created_at=NOW,
    )
    _assert_conforms(question.body(), "research-question.schema.json")


def test_research_report_schema_ile_uyumlu() -> None:
    _assert_conforms(_report().body(), "research-report.schema.json")


def test_plan_candidate_schema_ile_uyumlu() -> None:
    candidate = derive_plan_candidate(
        _report(),
        candidate_id="pc1",
        work_ref="w",
        source_revision="rev",
        proposed_steps=("adim",),
        writable_resources=("path:zekam:docs/RAG.md",),
        acceptance=("kabul",),
        rollback="revert",
        risk="low",
    )
    document = candidate.body()
    _assert_conforms(document, "plan-candidate.schema.json")
    assert document["requires_authorization"] is True


@pytest.mark.parametrize(
    "name",
    [
        "intake-resolution.schema.json",
        "research-question.schema.json",
        "research-report.schema.json",
        "plan-candidate.schema.json",
    ],
)
def test_schemalar_ek_alana_izin_vermez(name: str) -> None:
    assert _schema(name)["additionalProperties"] is False
