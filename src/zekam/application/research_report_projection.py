"""Kanonik DB research raporlarinin authority-tasimayan Markdown projection'i."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from zekam.application.home import HomeLayout
from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.research import ResearchReport
from zekam.infrastructure.postgres.research_repository import ResearchRepository

PROJECTION_SCHEMA = "zekam-research-report-projection/v1"
_SAFE_REPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validate_report_id(value: object) -> str:
    report_id = str(value)
    if not _SAFE_REPORT_ID.fullmatch(report_id) or ".." in report_id:
        raise ValidationFailed("Projection report_id path-safe olmali")
    return report_id


@dataclass(frozen=True, slots=True)
class ResearchReportProjection:
    path: Path
    report_digest: str
    content_digest: str
    created: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PROJECTION_SCHEMA,
            "path": str(self.path),
            "report_digest": self.report_digest,
            "content_digest": self.content_digest,
            "created": self.created,
            "read_only_projection": True,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class StoredResearchReport:
    record_id: UUID
    projection: ResearchReportProjection


def persist_research_report(
    repository: ResearchRepository,
    layout: HomeLayout,
    project_id: str,
    question_id: UUID,
    report: ResearchReport,
    *,
    now: dt.datetime,
) -> StoredResearchReport:
    """DB authority kaydindan sonra rebuild edilebilir projection uretir."""

    record_id = repository.store_report(question_id, report, now=now)
    document = repository.report_document(record_id)
    projection = materialize_research_report(layout, project_id, document)
    return StoredResearchReport(record_id, projection)


def render_research_report(document: Mapping[str, Any]) -> bytes:
    """Digest'i dogrulanmis raporu byte-stable Markdown olarak render eder."""

    body = {key: value for key, value in document.items() if key != "report_digest"}
    report_digest = str(document.get("report_digest", ""))
    parse_digest(report_digest)
    if digest(body) != report_digest:
        raise PolicyViolation("Projection rapor digest'i govdeyle eslesmiyor")
    report_id = _validate_report_id(body.get("report_id", ""))
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2)
    text = (
        "---\n"
        f"schema: {PROJECTION_SCHEMA}\n"
        f"report_id: {report_id}\n"
        f"report_digest: {report_digest}\n"
        "read_only_projection: true\n"
        "grants_authority: false\n"
        "---\n\n"
        f"# Research Report {report_id}\n\n"
        f"Status: {body.get('status', 'unknown')}\n\n"
        "This file is a derived projection. PostgreSQL is canonical.\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n"
    )
    if scan_text(text, relative_path=f"raporlar/arastirma-{report_id}.md"):
        raise PolicyViolation("Research report projection secret taramasini gecemedi")
    return text.encode("utf-8")


def materialize_research_report(
    layout: HomeLayout, project_id: str, document: Mapping[str, Any]
) -> ResearchReportProjection:
    """Projection'i project raporlar alanina atomik ve idempotent yazar."""

    payload = render_research_report(document)
    report_id = _validate_report_id(document["report_id"])
    project_root = layout.ensure_project(project_id)
    target = project_root / "raporlar" / f"arastirma-{report_id}.md"
    content_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if target.is_file() and target.read_bytes() == payload:
        return ResearchReportProjection(
            target, str(document["report_digest"]), content_digest, False
        )
    handle, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".part")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return ResearchReportProjection(target, str(document["report_digest"]), content_digest, True)


def projection_path(layout: HomeLayout, project_id: str, report_id: str) -> Path:
    report_id = _validate_report_id(report_id)
    return layout.project_root(project_id) / "raporlar" / f"arastirma-{report_id}.md"
