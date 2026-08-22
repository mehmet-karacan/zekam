"""P09-T01 dogal dil intake testleri."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.application.intake_service import IntakeService, build_candidates
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.intake import (
    AmbiguityKind,
    ConversationSubject,
    IntakeRequest,
    MatchKind,
    ProjectCandidate,
    RequestClass,
    extract_identifiers,
    normalize_text,
    resolve_intake,
)
from zekam.domain.project import Project, ProjectAlias
from zekam.domain.realm import Realm

NOW = dt.datetime(2026, 8, 20, 12, tzinfo=dt.UTC)


def _request(text: str, **kwargs: object) -> IntakeRequest:
    return IntakeRequest(text=text, received_at=NOW, **kwargs)  # type: ignore[arg-type]


def _candidate(ref: str, kind: MatchKind = MatchKind.EXACT_ID) -> ProjectCandidate:
    return ProjectCandidate(
        project_ref=ref, display_name=ref.upper(), match_kind=kind, matched_on=ref
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gpu projesindeki 123 numarali defectin kok nedenini arastir", RequestClass.RESEARCH),
        ("gpu projesine yeni bir migration ekle", RequestClass.PROJECT_CHANGE),
        ("bugun hangi islerimiz var, listele", RequestClass.STATUS),
        ("bir fikrim var: retrieval'i ayirsak olabilir mi", RequestClass.IDEA),
        ("investigate the root cause in gpu", RequestClass.RESEARCH),
    ],
)
def test_siniflandirma_dort_sinifi_ayirir(text: str, expected: RequestClass) -> None:
    resolution = resolve_intake(_request(text), candidates=(_candidate("gpu"),))
    assert resolution.request_class is expected
    assert not resolution.requires_clarification
    assert resolution.grants_authority is False


def test_niyet_ipucu_yoksa_tahmin_edilmez() -> None:
    resolution = resolve_intake(_request("gpu"), candidates=(_candidate("gpu"),))
    assert resolution.request_class is RequestClass.AMBIGUOUS
    assert resolution.ambiguities[0].kind is AmbiguityKind.NO_INTENT_CUE
    assert resolution.may_start_work is False


def test_cakisan_niyet_ipuclari_secim_ister() -> None:
    resolution = resolve_intake(
        _request("gpu projesini arastir ve duzelt"), candidates=(_candidate("gpu"),)
    )
    assert resolution.request_class is RequestClass.AMBIGUOUS
    ambiguity = resolution.ambiguities[0]
    assert ambiguity.kind is AmbiguityKind.MULTIPLE_INTENTS
    assert set(ambiguity.options) == {"research", "project-change"}


def test_exact_kimlik_semantik_benzerlikle_degismez() -> None:
    text = "ZEKAM-P09-T01 icin 4711 numarali defecti arastir"
    identifiers = extract_identifiers(text)
    assert [item.value for item in identifiers] == ["ZEKAM-P09-T01", "4711"]
    resolution = resolve_intake(_request(text), candidates=(_candidate("zekam"),))
    assert resolution.work_ref == "ZEKAM-P09-T01"
    assert [item.value for item in resolution.exact_identifiers] == ["ZEKAM-P09-T01", "4711"]


def test_bilinmeyen_exact_kimlik_gorunur_kalir() -> None:
    resolution = resolve_intake(
        _request("9999 numarali defecti arastir"),
        candidates=(_candidate("zekam"),),
        known_identifiers=frozenset({"123"}),
    )
    assert resolution.ambiguities[0].kind is AmbiguityKind.IDENTIFIER_UNKNOWN
    assert resolution.ambiguities[0].options == ("9999",)


def test_anaphora_bounded_konu_olmadan_cozulmez() -> None:
    resolution = resolve_intake(_request("bunu arastir"), candidates=(_candidate("zekam"),))
    assert resolution.anaphora_present is True
    assert resolution.subject_used is None
    assert resolution.ambiguities[0].kind is AmbiguityKind.ANAPHORA_UNRESOLVED


def test_anaphora_taze_konu_ile_cozulur() -> None:
    subject = ConversationSubject(
        subject="pgvector HNSW filtreli recall",
        captured_at=NOW - dt.timedelta(minutes=5),
        project_ref="zekam",
    )
    resolution = resolve_intake(
        _request("bunu arastir", subject=subject), candidates=(_candidate("zekam"),)
    )
    assert resolution.subject_used == "pgvector HNSW filtreli recall"
    assert resolution.request_class is RequestClass.RESEARCH


def test_bayat_konu_anaphorayi_cozmez() -> None:
    subject = ConversationSubject(
        subject="eski konu", captured_at=NOW - dt.timedelta(hours=12), project_ref="zekam"
    )
    resolution = resolve_intake(
        _request("bunu arastir", subject=subject), candidates=(_candidate("zekam"),)
    )
    assert resolution.subject_used is None
    assert resolution.ambiguities[0].kind is AmbiguityKind.ANAPHORA_UNRESOLVED


def test_iki_proje_adayinda_secim_istenir() -> None:
    resolution = resolve_intake(
        _request("bu projeyi arastir"),
        candidates=(_candidate("gpu-alpha"), _candidate("gpu-beta")),
    )
    ambiguity = next(
        item for item in resolution.ambiguities if item.kind is AmbiguityKind.PROJECT_AMBIGUOUS
    )
    assert ambiguity.options == ("gpu-alpha", "gpu-beta")
    assert resolution.project_ref is None
    assert resolution.may_start_work is False


def test_exact_aday_fuzzy_adayi_bastirir() -> None:
    resolution = resolve_intake(
        _request("gpu projesini arastir"),
        candidates=(
            _candidate("gpu", MatchKind.EXACT_ID),
            _candidate("gpu-benzeri", MatchKind.NORMALIZED_ALIAS),
        ),
    )
    assert resolution.project_ref == "gpu"
    assert resolution.may_start_work is True


def test_proje_cozulemezse_mutation_baslamaz() -> None:
    resolution = resolve_intake(_request("bu isi arastir"), candidates=())
    kinds = {item.kind for item in resolution.ambiguities}
    assert AmbiguityKind.PROJECT_UNRESOLVED in kinds
    assert resolution.may_start_work is False


def test_status_istegi_proje_zorunlu_degil() -> None:
    resolution = resolve_intake(_request("bugun hangi islerimiz var"), candidates=())
    assert resolution.request_class is RequestClass.STATUS
    assert resolution.may_start_work is True


def test_intake_authority_veremez() -> None:
    resolution = resolve_intake(_request("gpu projesini arastir"), candidates=(_candidate("gpu"),))
    assert resolution.body()["grants_authority"] is False
    with pytest.raises(PolicyViolation):
        type(resolution)(
            request_class=resolution.request_class,
            request_digest=resolution.request_digest,
            matched_cues=resolution.matched_cues,
            exact_identifiers=resolution.exact_identifiers,
            project_ref=resolution.project_ref,
            project_candidates=resolution.project_candidates,
            work_ref=resolution.work_ref,
            subject_used=None,
            anaphora_present=False,
            grants_authority=True,
        )


def test_konu_secret_tasiyamaz() -> None:
    with pytest.raises(PolicyViolation):
        ConversationSubject(subject="api_key rotasyonu", captured_at=NOW)


def test_bounded_sinirlar_zorlanir() -> None:
    with pytest.raises(ValidationFailed):
        IntakeRequest(text="a" * 5000, received_at=NOW)
    with pytest.raises(ValidationFailed):
        IntakeRequest(text="test", received_at=dt.datetime(2026, 8, 20))


def test_turkce_normalizasyonu_aksani_duseltir() -> None:
    assert normalize_text("GPU Projesindeki İŞLER") == "gpu projesindeki isler"


def test_aday_kurulumu_alias_ve_slug_esler() -> None:
    realm = Realm.create(slug="test-realm", display_name="Test")
    project = Project.create(realm=realm, slug="gpu-hizlandirma", display_name="GPU")
    alias = ProjectAlias.create(project=project, alias="gpu")
    candidates = build_candidates(
        "gpu projesini arastir", projects=(project,), aliases={project.slug: (alias,)}
    )
    assert [item.project_ref for item in candidates] == ["gpu-hizlandirma"]
    assert candidates[0].match_kind is MatchKind.EXACT_ALIAS


def test_servis_netlestirme_sorusu_uretir() -> None:
    outcome = IntakeService().resolve("bunu arastir", now=NOW, projects=())
    assert outcome.may_start_work is False
    kinds = {item.kind for item in outcome.clarifications}
    assert AmbiguityKind.ANAPHORA_UNRESOLVED in kinds
    assert all(item.question for item in outcome.clarifications)
