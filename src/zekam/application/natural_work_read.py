"""Explicit natural-language Work reads; no model, mutation or authority transfer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from zekam.application.operational_store import OperationalUnitOfWork
from zekam.application.run_readback import execution_guard, run_document
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.intake import normalize_text

_REFERENCE = r"(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[A-Za-z][A-Za-z0-9_]*-[0-9]+|[0-9]{1,18})"
_MARKER = r"(?:work|görev|gorev|iş\s+kaydı|is\s+kaydi)"
_AFTER = re.compile(rf"(?<!\w){_MARKER}\s+(?:#\s*)?({_REFERENCE})(?![\w-])", re.I)
_BEFORE = re.compile(
    rf"(?<![\w-])({_REFERENCE})\s+(?:numaralı|numarali)\s+(?:{_MARKER}|iş|is)"
    r"(?:n[ıiuü]n|[ıiuü]n|[ıiuü]|[ea]|n[ıiuü]|[dt][ae]n?)?(?!\w)",
    re.I,
)
_READ = re.compile(r"(?<!\w)(?:goster|listele|detay|durum|gecmis|bagimlilik|oncul|checkpoint)")
_MUTATION = re.compile(
    r"(?<!\w)(?:olustur|ekle|sil|kaldir|kapat|tamamla|baslat|degistir|gecir|guncelle|kaydet)"
)
_BOUND_JOB_LIMIT = 50


@dataclass(frozen=True, slots=True)
class WorkReadRequest:
    reference: str | None
    operation: str
    clarification: str | None = None


def explicit_work_references(question: str) -> tuple[str, ...]:
    """Extract record references only beside explicit Work markers."""
    return tuple(
        dict.fromkeys(
            match.group(1) for pattern in (_AFTER, _BEFORE) for match in pattern.finditer(question)
        )
    )


def work_scope_text(question: str) -> str:
    """A Work external key is not a project alias or Jira scope signal."""
    spans = {match.span(1) for pattern in (_AFTER, _BEFORE) for match in pattern.finditer(question)}
    for start, end in sorted(spans, reverse=True):
        question = question[:start] + " " * (end - start) + question[end:]
    return question


def parse_work_read(question: str) -> WorkReadRequest | None:
    """Only explicit record references match; Jira/code history stays in its own route."""
    if not isinstance(question, str) or len(question) > 8192:
        raise ValidationFailed("Work sorusu bounded metin olmali")
    normalized = normalize_text(question)
    if not _READ.search(normalized):
        return None
    references = explicit_work_references(question)
    if not references:
        return None
    if len(references) != 1:
        return WorkReadRequest(None, "clarification", "Hangi tek is kaydini inceleyelim?")
    if _MUTATION.search(normalized):
        return WorkReadRequest(
            references[0],
            "clarification",
            "Okuma ve degisiklik istegi birlikte geldi; once hangi islemi yapalim?",
        )
    operations = [
        name
        for name, pattern in (
            ("history", r"(?<!\w)gecmis"),
            ("dependencies", r"(?<!\w)(?:bagimlilik|oncul)"),
            ("checkpoints", r"(?<!\w)checkpoint"),
        )
        if re.search(pattern, normalized)
    ]
    if len(operations) > 1:
        return WorkReadRequest(
            references[0],
            "clarification",
            "Is kaydinin gecmisi, bagimliliklari veya checkpoint'lerinden hangisini gosterelim?",
        )
    return WorkReadRequest(references[0], operations[0] if operations else "show")


def read_work_request(
    uow: OperationalUnitOfWork, request: WorkReadRequest, *, project_ref: str
) -> dict[str, Any]:
    """Read one exact project/Work snapshot with fixed nested bounds."""
    if (
        request.operation not in {"show", "history", "dependencies", "checkpoints"}
        or not request.reference
    ):
        raise ValidationFailed("Work read istegi exact kayit ve izinli islem ister")
    project = uow.resolve_project(project_ref)
    work = uow.resolve_work(request.reference, project_id=project.id)
    bound_jobs = uow.list_bound_jobs_limited(
        work_item_ids=(work.id,),
        limit=_BOUND_JOB_LIMIT + 1,
    )
    execution = execution_guard(bound_jobs)
    body: dict[str, Any] = {
        "schema": "zekam-natural-work-read/v1",
        "status": "reported",
        "operation": request.operation,
        "project_ref": project.slug,
        "work": {
            "id": work.id,
            "external_number": work.external_number,
            "title": work.title,
            "state": work.state,
            "revision": work.revision,
            "summary": work.summary,
            "acceptance_criteria": list(work.acceptance_criteria),
            "evidence_digest": work.evidence_digest,
            "dependencies": uow.work_dependency_summaries((work.id,))[work.id],
        },
        "bounded": True,
        "bound_jobs": list(bound_jobs[:_BOUND_JOB_LIMIT]),
        "bound_jobs_truncated": len(bound_jobs) > _BOUND_JOB_LIMIT,
        "bound_jobs_scope": "exact-work",
        "execution": execution,
        "read_only": True,
        "grants_authority": False,
        "provider_calls": 0,
        "index_triggered": False,
    }
    if request.operation == "history":
        rows = uow.list_work_events(work.id, limit=101)
        body.update(events=list(rows[-100:]), truncated=len(rows) > 100, event_limit=100)
    elif request.operation == "dependencies":
        rows = uow.list_work_dependencies(work.id, limit=101)
        body.update(dependencies=list(rows[:100]), truncated=len(rows) > 100, dependency_limit=100)
    elif request.operation == "show":
        runs = uow.list_runs(work.id, limit=6)
        body.update(
            runs=[run_document(run) for run in runs[:5]],
            runs_truncated=len(runs) > 5,
            run_limit=5,
        )
    elif request.operation == "checkpoints":
        runs = uow.list_runs(work.id, limit=6)
        results = []
        for run in runs[:5]:
            checkpoints = uow.list_checkpoints(run.id, limit=6)
            results.append(
                {
                    "run_id": run.id,
                    "status": run.status,
                    "terminal_receipt_digest": run.terminal_receipt_digest,
                    "source_snapshot_id": run.source_snapshot_id,
                    "config_revision_id": run.config_revision_id,
                    "fingerprints": run_document(run)["fingerprints"],
                    "checkpoints": [
                        {
                            "id": row.id,
                            "sequence": row.sequence,
                            "checkpoint_digest": row.checkpoint_digest,
                            "source_snapshot_id": row.source_snapshot_id,
                        }
                        for row in checkpoints[:5]
                    ],
                    "checkpoints_truncated": len(checkpoints) > 5,
                }
            )
        body.update(runs=results, runs_truncated=len(runs) > 5, run_limit=5, checkpoint_limit=5)
    body["bound_job_limit"] = _BOUND_JOB_LIMIT
    return body | {"report_digest": digest(body)}
