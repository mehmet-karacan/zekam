"""P17 release, Global DoD, SBOM, kapasite ve iptal testleri."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.release import (
    REQUIRED_EVIDENCE_KINDS,
    BackpressureDecision,
    CancellationRequest,
    CriterionState,
    DodAssessment,
    DodCriterion,
    EvidenceItem,
    Sbom,
    SbomEntry,
    build_release,
)

NOW = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)


def _evidence() -> tuple[EvidenceItem, ...]:
    return tuple(
        EvidenceItem(kind=kind, reference=f"tests/{kind}.py", digest_value=digest(kind))
        for kind in REQUIRED_EVIDENCE_KINDS
    )


def _criterion(**kwargs: object) -> DodCriterion:
    defaults: dict[str, object] = {
        "criterion_id": "ZEKAM-DOD-001",
        "category": "A",
        "statement": "Kriter",
        "state": CriterionState.PASSED,
        "evidence": _evidence(),
    }
    defaults.update(kwargs)
    return DodCriterion(**defaults)  # type: ignore[arg-type]


# -- T05: Global DoD -----------------------------------------------------------


def test_kriter_waiver_kabul_etmez() -> None:
    with pytest.raises(PolicyViolation):
        _criterion(waiver_allowed=True)


def test_kanitsiz_kriter_kapanamaz() -> None:
    with pytest.raises(PolicyViolation) as error:
        _criterion(evidence=())
    assert "kanit eksik" in str(error.value)


def test_eksik_kanit_turu_kapanmayi_engeller() -> None:
    partial = tuple(item for item in _evidence() if item.kind != "verifier-or-review")
    with pytest.raises(PolicyViolation) as error:
        _criterion(evidence=partial)
    assert "verifier-or-review" in str(error.value)


def test_blocked_kriter_gerekce_ister() -> None:
    with pytest.raises(ValidationFailed):
        _criterion(state=CriterionState.BLOCKED, evidence=())


def test_kanit_referansi_absolute_path_tasiyamaz() -> None:
    with pytest.raises(PolicyViolation):
        EvidenceItem(
            kind="test-or-evaluation", reference="/home/biri/test.py", digest_value=digest("x")
        )
    with pytest.raises(PolicyViolation):
        EvidenceItem(kind="test-or-evaluation", reference="C:\\test.py", digest_value=digest("x"))


def test_degerlendirme_orani_hesaplanir() -> None:
    assessment = DodAssessment(
        criteria=(
            _criterion(criterion_id="c1"),
            _criterion(criterion_id="c2", state=CriterionState.PENDING, evidence=()),
        ),
        assessed_at=NOW,
    )
    assert assessment.completion_ratio == pytest.approx(0.5)
    assert assessment.is_complete is False
    assert len(assessment.by_state(CriterionState.PENDING)) == 1


def test_tekrar_eden_kriter_kimligi_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        DodAssessment(criteria=(_criterion(), _criterion()), assessed_at=NOW)


# -- T04: SBOM ve release ------------------------------------------------------


def _sbom() -> Sbom:
    return Sbom(
        entries=(
            SbomEntry(name="psycopg", version="3.2.0", license_id="LGPL"),
            SbomEntry(name="typer", version="0.12.0", license_id="MIT"),
        ),
        generated_at=NOW,
    )


def test_bos_sbom_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        Sbom(entries=(), generated_at=NOW)


def test_tekrar_eden_bagimlilik_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        Sbom(
            entries=(SbomEntry(name="typer", version="1"), SbomEntry(name="Typer", version="2")),
            generated_at=NOW,
        )


def test_sbom_digesti_siralamadan_bagimsiz() -> None:
    first = _sbom()
    reversed_entries = Sbom(entries=tuple(reversed(first.entries)), generated_at=NOW)
    assert first.sbom_digest == reversed_entries.sbom_digest


def test_dod_tamamlanmadan_release_uretilemez() -> None:
    incomplete = DodAssessment(
        criteria=(_criterion(criterion_id="c1", state=CriterionState.PENDING, evidence=()),),
        assessed_at=NOW,
    )
    with pytest.raises(PolicyViolation) as error:
        build_release(
            name="zekam",
            version="0.1.0",
            content_digest=digest("paket"),
            sbom=_sbom(),
            assessment=incomplete,
        )
    assert "Global DoD" in str(error.value)


def test_tam_dod_ile_release_uretilir() -> None:
    complete = DodAssessment(criteria=(_criterion(criterion_id="c1"),), assessed_at=NOW)
    artifact = build_release(
        name="zekam",
        version="0.1.0",
        content_digest=digest("paket"),
        sbom=_sbom(),
        assessment=complete,
    )
    assert artifact.sbom_digest == _sbom().sbom_digest
    assert artifact.dod_digest == complete.assessment_digest
    assert artifact.signed is False


def test_surum_semantik_olmali() -> None:
    complete = DodAssessment(criteria=(_criterion(criterion_id="c1"),), assessed_at=NOW)
    with pytest.raises(ValidationFailed):
        build_release(
            name="zekam",
            version="son",
            content_digest=digest("p"),
            sbom=_sbom(),
            assessment=complete,
        )


# -- T02: kapasite ve iptal ----------------------------------------------------


def test_kuyruk_dolunca_yeni_is_kabul_edilmez() -> None:
    decision = BackpressureDecision(
        queue_depth=10, max_queue_depth=10, active_workers=1, max_workers=4
    )
    assert decision.accepts_work is False
    assert "kuyruk derinligi" in decision.reason()


def test_worker_kapasitesi_dolunca_kabul_edilmez() -> None:
    decision = BackpressureDecision(
        queue_depth=1, max_queue_depth=10, active_workers=4, max_workers=4
    )
    assert decision.accepts_work is False
    assert "worker kapasitesi" in decision.reason()


def test_kapasite_uygunsa_kabul_edilir() -> None:
    decision = BackpressureDecision(
        queue_depth=2, max_queue_depth=10, active_workers=1, max_workers=4
    )
    assert decision.accepts_work is True
    assert decision.as_dict()["accepts_work"] is True


def test_gecersiz_kapasite_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        BackpressureDecision(queue_depth=1, max_queue_depth=0, active_workers=1, max_workers=1)
    with pytest.raises(ValidationFailed):
        BackpressureDecision(queue_depth=-1, max_queue_depth=1, active_workers=0, max_workers=1)


def test_iptal_edilen_calisma_sonuc_yayimlayamaz() -> None:
    request = CancellationRequest(run_ref="run-1", requested_at=NOW, acknowledged=True)
    with pytest.raises(PolicyViolation):
        request.assert_no_result_after_cancel(result_published=True)
    request.assert_no_result_after_cancel(result_published=False)


def test_onaylanmamis_iptal_sonucu_engellemez() -> None:
    request = CancellationRequest(run_ref="run-1", requested_at=NOW, acknowledged=False)
    request.assert_no_result_after_cancel(result_published=True)
