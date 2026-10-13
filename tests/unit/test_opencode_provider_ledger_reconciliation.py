"""Offline reconciliation gate for document embedding effects."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import EffectClaim, FailureCategory
from zekam.infrastructure.local_file_security import restrict_private_tree
from zekam.infrastructure.opencode_provider_ledger import SQLiteProviderLedgerHost, _work

pytestmark = pytest.mark.unit


def _host(tmp_path: Path) -> SQLiteProviderLedgerHost:
    private = tmp_path / "private"
    private.mkdir()
    restrict_private_tree(private)
    return SQLiteProviderLedgerHost(private / "provider.sqlite3", uuid4())


def _claim(
    host: SQLiteProviderLedgerHost,
    project_id: UUID,
    *,
    effect: str = "same-effect",
    operation: str = "provider-contract:opencode-embedding-document-abc",
) -> EffectClaim:
    work = _work(host.realm_id, project_id)
    host.register(work)
    return host.claim_effect(
        work,
        operation=operation,
        effect_digest=digest(effect),
        authorization_digest=digest("authorization"),
        idempotency_key=digest(str(work.job.id)),
        resources=parse_requests(write=("provider:embedding",)),
        adapter_digest=digest("adapter"),
    )


def test_receiptless_document_claim_blocks_same_project_effect(tmp_path: Path) -> None:
    host = _host(tmp_path)
    project = uuid4()
    first = _claim(host, project)
    with pytest.raises(PolicyViolation, match="uzlastirilmali"):
        _claim(host, project)
    assert host.receipt_for_claim(first.id) is None
    _claim(host, project, effect="different-effect")
    _claim(host, uuid4())


def test_completed_document_claim_blocks_recall_but_failed_receipt_can_retry(
    tmp_path: Path,
) -> None:
    host = _host(tmp_path)
    project = uuid4()
    success = _claim(host, project, effect="completed")
    host.record_success(
        success,
        result_digest=digest("result"),
        adapter_evidence_digest=digest("evidence"),
    )
    with pytest.raises(PolicyViolation, match="uzlastirilmali"):
        _claim(host, project, effect="completed")
    failed = _claim(host, project, effect="failed")
    host.record_failure(
        failed,
        category=FailureCategory.PROVIDER,
        failure_digest=digest("failure"),
    )
    _claim(host, project, effect="failed")


def test_probe_claims_are_not_document_reconciliation_gate(tmp_path: Path) -> None:
    host = _host(tmp_path)
    project = uuid4()
    _claim(host, project, operation="provider-contract:opencode-embedding-probe-abc")
    _claim(host, project, operation="provider-contract:opencode-embedding-probe-abc")
