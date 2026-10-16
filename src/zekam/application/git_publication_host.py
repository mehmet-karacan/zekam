"""Native host composition from a real admitted evidence pair to exact Git push."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from zekam.application.git_publication import GitPublicationPlan, GitPublicationService
from zekam.application.git_publication_bundle import _ledger, read_publication_admission
from zekam.application.git_publication_review import read_publication_review
from zekam.application.git_publication_test_source import read_publication_test_source
from zekam.application.git_publication_verifier_producer import (
    _proof_matches,
    read_verifier_producer_result,
)
from zekam.application.git_publication_wire import publication_plan_from_body
from zekam.application.project_rag_runtime import resolve_project_source
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.git.publication import BoundGitPushTransport
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


def publication_from_admission(home: Path, reference: str) -> GitPublicationPlan:
    """Resolve canonical identifiers locally; exported Markdown/JSON is not authority."""
    source = read_publication_admission(home, reference)
    return publication_plan_from_body(source["bundle"]["plan"])


def build_admitted_publication(home: Path, reference: str):
    """Compose the native source validator, actual authority ledger and bound transport."""
    source = read_publication_admission(home, reference)
    plan = publication_plan_from_body(source["bundle"]["plan"])
    _trust, ledger = _ledger(home, plan)
    with SQLiteOperationalStore(home / "state/operational.db").unit_of_work() as uow:
        project = uow.resolve_project(plan.binding.project_id)
    root = resolve_project_source(home, project.slug)

    def validate_evidence(current_plan):
        current = read_publication_admission(home, source["job_id"])
        if current != source or current_plan != plan:
            raise PolicyViolation("Publication native admission/plan changed before push")
        verification = read_verifier_producer_result(
            home, source["bundle"]["verification_producer_job_id"]
        )
        review = read_publication_review(home, verification["envelope"]["source_job_id"])
        proof = read_publication_test_source(home, review["test_job_id"], plan=current_plan)
        _proof_matches(proof, review, current_plan.binding)

    return plan, GitPublicationService(home, ledger, BoundGitPushTransport(root), validate_evidence)


def apply_admitted_publication(
    home: Path, *, reference: str, authorization_id: UUID, plan_digest: str
):
    """An admission is not approval: the existing exact authorization is still consumed."""
    plan, service = build_admitted_publication(home, reference)
    if plan.plan_digest != plan_digest:
        raise PolicyViolation("Publication native exact approved plan digest drift")
    return service.apply(plan, authorization_id=authorization_id)
