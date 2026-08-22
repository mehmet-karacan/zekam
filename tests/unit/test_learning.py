"""P14-T01..T06 ogrenme dongusu ve skill yasam dongusu testleri."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.learning import (
    MINIMUM_SKILL_TRIALS,
    ContextEffectiveness,
    FailureOccurrence,
    IterationBudget,
    IterationOutcome,
    LearningCandidate,
    LearningTarget,
    RootCause,
    RouteFeedback,
    Skill,
    SkillEvaluation,
    SkillFixture,
    SkillState,
    StopReason,
    deduplicate_skills,
    distinct_observations,
    evaluate_loop,
    promote_learning,
)

NOW = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)


def _occurrence(evidence: str, run: str = "run-1") -> FailureOccurrence:
    return FailureOccurrence(
        occurrence_key="migration-drift",
        evidence_digest=digest(evidence),
        run_ref=run,
        observed_at=NOW,
        failure_category="adapter",
    )


def _root_cause() -> RootCause:
    return RootCause(
        statement="Checksum ledger'i temizlenmeden migration yeniden uygulandi",
        verified_by="verifier-b",
        evidence_digest=digest("kanit"),
    )


def _candidate(**kwargs: object) -> LearningCandidate:
    defaults: dict[str, object] = {
        "candidate_id": "l1",
        "occurrence_key": "migration-drift",
        "occurrences": (_occurrence("e1"), _occurrence("e2", "run-2")),
        "target": LearningTarget.TEST,
        "proposal": "Drift senaryosu icin regresyon testi ekle",
        "author_ref": "agent-a",
        "root_cause": _root_cause(),
    }
    defaults.update(kwargs)
    return LearningCandidate(**defaults)  # type: ignore[arg-type]


# -- T01: occurrence ve cift sayim ---------------------------------------------


def test_ayni_kanit_iki_kez_sayilmaz() -> None:
    """Ayni digest farkli run'lardan gelse bile tek gozlemdir."""

    occurrences = (_occurrence("ayni", "run-1"), _occurrence("ayni", "run-2"))
    assert distinct_observations(occurrences) == 1
    assert _candidate(occurrences=occurrences).observation_count == 1


def test_farkli_kanit_ayri_sayilir() -> None:
    assert _candidate().observation_count == 2


def test_gozlemler_ayni_occurrence_keye_ait_olmali() -> None:
    other = FailureOccurrence(
        occurrence_key="baska",
        evidence_digest=digest("e"),
        run_ref="r",
        observed_at=NOW,
        failure_category="adapter",
    )
    with pytest.raises(ValidationFailed):
        _candidate(occurrences=(_occurrence("e1"), other))


def test_gozlemsiz_aday_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        _candidate(occurrences=())


# -- T02: ders promosyonu ------------------------------------------------------


def test_kok_neden_olmadan_ders_uretilmez() -> None:
    ready, reason = _candidate(root_cause=None).readiness()
    assert ready is False
    assert "kok neden" in reason


def test_tek_gozlem_kritik_degilse_yetmez() -> None:
    ready, reason = _candidate(occurrences=(_occurrence("tek"),)).readiness()
    assert ready is False
    assert "gozlem" in reason


def test_kritik_tek_olay_ders_uretebilir() -> None:
    ready, _ = _candidate(occurrences=(_occurrence("tek"),), critical=True).readiness()
    assert ready is True


def test_iki_gozlem_ve_kok_neden_yeterli() -> None:
    ready, reason = _candidate().readiness()
    assert ready is True
    assert "dogrulandi" in reason


def test_verifier_yazarla_ayni_kimlik_olamaz() -> None:
    with pytest.raises(PolicyViolation):
        promote_learning(_candidate(author_ref="agent-a"), verifier_ref="agent-a")


def test_bagimsiz_verifier_karari_uretir() -> None:
    promotion = promote_learning(_candidate(), verifier_ref="verifier-b")
    assert promotion.approved is True
    assert promotion.target is LearningTarget.TEST
    assert promotion.verifier_ref == "verifier-b"


def test_hazir_olmayan_aday_onaylanmaz() -> None:
    promotion = promote_learning(_candidate(root_cause=None), verifier_ref="verifier-b")
    assert promotion.approved is False
    assert "kok neden" in promotion.reason


@pytest.mark.parametrize("target", list(LearningTarget))
def test_dort_hedef_de_desteklenir(target: LearningTarget) -> None:
    promotion = promote_learning(_candidate(target=target), verifier_ref="verifier-b")
    assert promotion.target is target


# -- T03: skill degerlendirmesi ------------------------------------------------


def _fixture() -> SkillFixture:
    return SkillFixture(fixture_id="f1", version="1", content_digest=digest("f"))


def _evaluation(**kwargs: object) -> SkillEvaluation:
    defaults: dict[str, object] = {
        "skill_id": "s1",
        "fixtures": (_fixture(),),
        "trials": 10,
        "successes": 9,
        "evaluator_ref": "evaluator-a",
        "verifier_ref": "verifier-b",
        "baseline_success_rate": 0.5,
    }
    defaults.update(kwargs)
    return SkillEvaluation(**defaults)  # type: ignore[arg-type]


def test_degerlendirme_en_az_bes_deneme_ister() -> None:
    with pytest.raises(ValidationFailed):
        _evaluation(trials=MINIMUM_SKILL_TRIALS - 1, successes=1)


def test_degerlendiren_ve_dogrulayan_ayri_olmali() -> None:
    with pytest.raises(PolicyViolation):
        _evaluation(evaluator_ref="ayni", verifier_ref="ayni")


def test_fixture_zorunlu() -> None:
    with pytest.raises(ValidationFailed):
        _evaluation(fixtures=())


def test_basari_orani_ve_iyilesme_hesaplanir() -> None:
    evaluation = _evaluation()
    assert evaluation.success_rate == pytest.approx(0.9)
    assert evaluation.improves is True
    assert _evaluation(successes=3, baseline_success_rate=0.5).improves is False


def test_ayni_govdeli_skill_adaylari_tekillestirilir() -> None:
    body = digest("govde")
    first = Skill(
        skill_id="s1",
        name="ilk",
        body_digest=body,
        state=SkillState.CANDIDATE,
        revision=1,
        author_ref="a",
    )
    second = Skill(
        skill_id="s2",
        name="ikinci",
        body_digest=body,
        state=SkillState.CANDIDATE,
        revision=1,
        author_ref="a",
    )
    assert [item.skill_id for item in deduplicate_skills((first, second))] == ["s1"]


# -- T04: skill yasam dongusu --------------------------------------------------


def _skill(**kwargs: object) -> Skill:
    defaults: dict[str, object] = {
        "skill_id": "s1",
        "name": "drift-kontrolu",
        "body_digest": digest("govde"),
        "state": SkillState.CANDIDATE,
        "revision": 1,
        "author_ref": "agent-a",
    }
    defaults.update(kwargs)
    return Skill(**defaults)  # type: ignore[arg-type]


def test_skill_kendi_kendini_aktive_edemez() -> None:
    with pytest.raises(PolicyViolation):
        _skill(self_promoted=True)


def test_olculmemis_skill_aktif_olamaz() -> None:
    with pytest.raises(PolicyViolation):
        _skill(state=SkillState.ACTIVE)


def test_aktivasyon_exact_approval_ister() -> None:
    with pytest.raises(AuthorizationRequired):
        _skill(state=SkillState.ACTIVE, evaluation_digest=digest("d"), rollback_plan="geri al")


def test_approval_yazarla_ayni_kimlik_olamaz() -> None:
    with pytest.raises(PolicyViolation):
        _skill(
            state=SkillState.ACTIVE,
            evaluation_digest=digest("d"),
            approved_by="agent-a",
            rollback_plan="geri al",
        )


def test_aktivasyon_rollback_plani_ister() -> None:
    with pytest.raises(PolicyViolation):
        _skill(
            state=SkillState.ACTIVE,
            evaluation_digest=digest("d"),
            approved_by="onaylayan-b",
        )


def test_baseline_gecmeyen_skill_aktive_edilemez() -> None:
    weak = _evaluation(successes=3, baseline_success_rate=0.5)
    with pytest.raises(PolicyViolation):
        _skill().activate(weak, approved_by="onaylayan-b", rollback_plan="geri al")


def test_baska_skille_ait_degerlendirme_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        _skill().activate(
            _evaluation(skill_id="s2"), approved_by="onaylayan-b", rollback_plan="geri al"
        )


def test_tam_kapiyla_aktivasyon_calisir() -> None:
    active = _skill().activate(
        _evaluation(), approved_by="onaylayan-b", rollback_plan="registry'den kaldir"
    )
    assert active.state is SkillState.ACTIVE
    assert active.approved_by == "onaylayan-b"
    assert active.rollback_plan == "registry'den kaldir"


def test_deprecate_ve_retire_akisi() -> None:
    active = _skill().activate(_evaluation(), approved_by="onaylayan-b", rollback_plan="geri al")
    deprecated = active.deprecate("yerine yeni surum geldi")
    assert deprecated.state is SkillState.DEPRECATED
    assert deprecated.retire().state is SkillState.RETIRED
    with pytest.raises(PolicyViolation):
        _skill().deprecate("aday deprecate edilemez")


# -- T05: olculu dongu ---------------------------------------------------------


def _outcome(
    iteration: int, score: float, cost: int = 10, verified: bool = True
) -> IterationOutcome:
    return IterationOutcome(iteration=iteration, score=score, cost_units=cost, verified=verified)


def test_hedefe_ulasinca_durur() -> None:
    decision = evaluate_loop((_outcome(1, 0.95),), IterationBudget(), goal_score=0.9)
    assert decision.should_continue is False
    assert decision.reason is StopReason.GOAL_REACHED


def test_dogrulanmamis_basari_hedefi_kapatmaz() -> None:
    decision = evaluate_loop(
        (_outcome(1, 0.95, verified=False),), IterationBudget(), goal_score=0.9
    )
    assert decision.should_continue is True


def test_iterasyon_butcesi_durdurur() -> None:
    budget = IterationBudget(max_iterations=2)
    outcomes = (_outcome(1, 0.1), _outcome(2, 0.5))
    decision = evaluate_loop(outcomes, budget, goal_score=0.9)
    assert decision.reason is StopReason.ITERATION_BUDGET


def test_maliyet_butcesi_durdurur() -> None:
    budget = IterationBudget(max_iterations=5, max_cost_units=15)
    decision = evaluate_loop((_outcome(1, 0.1, cost=20),), budget, goal_score=0.9)
    assert decision.reason is StopReason.COST_BUDGET
    assert decision.spent_cost == 20


def test_ilerleme_yoksa_durur() -> None:
    budget = IterationBudget(max_iterations=8, stall_limit=2)
    outcomes = (_outcome(1, 0.5), _outcome(2, 0.5), _outcome(3, 0.4))
    decision = evaluate_loop(outcomes, budget, goal_score=0.9)
    assert decision.reason is StopReason.NO_PROGRESS
    assert decision.best_score == pytest.approx(0.5)


def test_ilerleme_varsa_devam_eder() -> None:
    budget = IterationBudget(max_iterations=8, stall_limit=2)
    outcomes = (_outcome(1, 0.3), _outcome(2, 0.5), _outcome(3, 0.7))
    decision = evaluate_loop(outcomes, budget, goal_score=0.9)
    assert decision.should_continue is True
    assert decision.reason is None


def test_blocker_donguyu_durdurur() -> None:
    decision = evaluate_loop((_outcome(1, 0.1),), IterationBudget(), goal_score=0.9, blocked=True)
    assert decision.reason is StopReason.BLOCKED


def test_sinirsiz_iterasyon_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        IterationBudget(max_iterations=100)
    with pytest.raises(ValidationFailed):
        IterationBudget(max_cost_units=0)


# -- T06: baglam etkinligi -----------------------------------------------------


def _effectiveness(**kwargs: object) -> ContextEffectiveness:
    defaults: dict[str, object] = {
        "manifest_digest": digest("m"),
        "token_cost": 2000,
        "evidence_used": 6,
        "verified_success": True,
    }
    defaults.update(kwargs)
    return ContextEffectiveness(**defaults)  # type: ignore[arg-type]


def test_etkinlik_olcumu_authority_veremez() -> None:
    with pytest.raises(PolicyViolation):
        _effectiveness(grants_authority=True)
    assert _effectiveness().as_dict()["grants_authority"] is False


def test_kanit_yogunlugu_hesaplanir() -> None:
    assert _effectiveness().evidence_per_kilotoken == pytest.approx(3.0)


def test_route_geri_bildirimi_metrik_uretir() -> None:
    feedback = RouteFeedback(
        samples=(
            _effectiveness(),
            _effectiveness(verified_success=False, token_cost=4000, evidence_used=2),
        )
    )
    assert feedback.verified_success_rate == pytest.approx(0.5)
    assert feedback.mean_token_cost == pytest.approx(3000.0)
    assert feedback.as_dict()["grants_authority"] is False


def test_bos_geri_bildirim_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        RouteFeedback(samples=())
