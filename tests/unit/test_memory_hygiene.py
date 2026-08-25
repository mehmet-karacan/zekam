from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from zekam.application.memory_hygiene import persist_hygiene_review_queue
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory import (
    HygieneFinding,
    HygieneReport,
    MemoryClass,
    MemoryEvidence,
    MemoryKey,
    MemoryRecord,
    MemoryScope,
    MemoryState,
)
from zekam.domain.memory_hygiene import (
    HygieneProposalAction,
    MemoryHygienePolicy,
    build_hygiene_review_queue,
)
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

NOW = dt.datetime(2026, 8, 25, 9, 0, tzinfo=dt.UTC)


def _record(memory_id: str) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        key=MemoryKey(MemoryScope.GLOBAL_USER, "realm-1"),
        memory_class=MemoryClass.WORKING,
        content=f"memory {memory_id}",
        state=MemoryState.ACTIVE,
        revision=1,
        created_at=NOW - dt.timedelta(days=10),
        evidence=(MemoryEvidence("test", "test-1", digest("evidence")),),
        author_ref="author",
    )


def _report() -> tuple[HygieneReport, tuple[MemoryRecord, ...]]:
    records = tuple(_record(f"m{index}") for index in range(1, 7))
    findings = tuple(
        (kind, record.memory_id, f"{kind} gerekcesi")
        for kind, record in zip(HygieneFinding, records, strict=True)
    )
    return HygieneReport(findings, scanned=6), records


def test_tum_hijyen_bulgulari_yalniz_review_onerisine_donusur() -> None:
    report, records = _report()
    queue = build_hygiene_review_queue(report, records, generated_at=NOW)
    assert {item.action for item in queue.proposals} == set(HygieneProposalAction)
    assert all(item.review_status == "review-required" for item in queue.proposals)
    assert all(not item.grants_authority and not item.automatic_effect for item in queue.proposals)
    assert queue.review_status == "review-required"
    assert not queue.grants_authority
    assert queue.proposals[0].review_due_at == NOW + dt.timedelta(hours=72)


def test_kuyruk_deterministik_ve_tekrar_bulgulari_tekildir() -> None:
    report, records = _report()
    duplicated = HygieneReport((*report.findings, report.findings[0]), scanned=6)
    first = build_hygiene_review_queue(duplicated, records, generated_at=NOW)
    second = build_hygiene_review_queue(duplicated, records, generated_at=NOW)
    assert first.queue_digest == second.queue_digest
    assert len(first.proposals) == 6
    assert tuple(item.proposal_digest for item in first.proposals) == tuple(
        sorted(item.proposal_digest for item in first.proposals)
    )


def test_hijyen_snapshot_temporal_driftte_degisir() -> None:
    record = _record("m1")
    report = HygieneReport(((HygieneFinding.STALE, "m1", "stale"),), scanned=1)
    first = build_hygiene_review_queue(report, (record,), generated_at=NOW)
    changed = replace(record, last_used_at=NOW - dt.timedelta(days=400))
    second = build_hygiene_review_queue(report, (changed,), generated_at=NOW)
    assert first.proposals[0].record_snapshot_digest != second.proposals[0].record_snapshot_digest
    assert first.queue_digest != second.queue_digest


def test_rapor_bilinmeyen_kayit_ve_tarama_sayisi_uyusmazligini_reddeder() -> None:
    record = _record("m1")
    with pytest.raises(ValidationFailed):
        build_hygiene_review_queue(
            HygieneReport(((HygieneFinding.STALE, "forged", "x"),), scanned=1),
            (record,),
            generated_at=NOW,
        )
    with pytest.raises(ValidationFailed):
        build_hygiene_review_queue(HygieneReport((), scanned=2), (record,), generated_at=NOW)


def test_oneri_authority_ve_otomatik_etkiye_cevrilemez() -> None:
    report, records = _report()
    proposal = build_hygiene_review_queue(report, records, generated_at=NOW).proposals[0]
    with pytest.raises(PolicyViolation):
        replace(proposal, grants_authority=True)
    with pytest.raises(PolicyViolation):
        replace(proposal, automatic_effect=True)
    with pytest.raises(PolicyViolation):
        replace(proposal, review_status="approved")


def test_nested_oneri_tamperi_persistence_oncesi_reddedilir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    report, records = _report()
    queue = build_hygiene_review_queue(report, records, generated_at=NOW)
    object.__setattr__(queue.proposals[0], "detail", "forged reason")
    store = LocalContentAddressedStore(tmp_path).ensure()
    with pytest.raises(ValidationFailed):
        persist_hygiene_review_queue(queue, store)
    assert not tuple((tmp_path / "sha256").rglob("*.bin"))


def test_nested_snapshot_tamperi_persistence_oncesi_reddedilir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    report, records = _report()
    queue = build_hygiene_review_queue(report, records, generated_at=NOW)
    object.__setattr__(queue.proposals[0], "record_snapshot_digest", digest("forged"))
    store = LocalContentAddressedStore(tmp_path).ensure()
    with pytest.raises(ValidationFailed):
        persist_hygiene_review_queue(queue, store)
    assert not tuple((tmp_path / "sha256").rglob("*.bin"))


def test_policy_otomatik_silme_ve_birlestirmeyi_acmaz() -> None:
    policy = MemoryHygienePolicy(review_slo_hours=24, policy_version="2")
    assert policy.as_dict()["automatic_delete"] is False
    assert policy.as_dict()["automatic_merge"] is False
    with pytest.raises(ValidationFailed):
        MemoryHygienePolicy(review_slo_hours=0)


def test_kuyruk_casa_read_after_write_ile_kalici_yazilir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    report, records = _report()
    queue = build_hygiene_review_queue(report, records, generated_at=NOW)
    store = LocalContentAddressedStore(tmp_path).ensure()
    stored = persist_hygiene_review_queue(queue, store)
    assert stored.queue_digest == queue.queue_digest
    assert store.get(stored.object_digest) == queue.to_bytes()
    assert stored.review_status == "review-required"
    assert not stored.grants_authority
