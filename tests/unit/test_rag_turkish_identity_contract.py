"""Turkce ek/kesme isareti exact kimlik sozlesmesi (RAG26-R01)."""

from __future__ import annotations

import pytest

from zekam.domain.retrieval import extract_identifiers


@pytest.mark.unit
@pytest.mark.parametrize(
    "query",
    [
        "gpu-fusion backend'inde hangi Spring Batch job'lari tanimli?",
        "Oracle'da ayrilmis kelime kolon adi girilirse ne olur?",
        "gpu-fusion backend’inde hangi Spring Batch job’lari tanimli?",
        "it's the user's job and the team's plan",
    ],
)
def test_word_internal_apostrophe_is_not_a_quoted_phrase(query: str) -> None:
    assert extract_identifiers(query) == ()


@pytest.mark.unit
def test_real_single_and_double_quoted_identifiers_are_kept() -> None:
    assert "HakedisHesaplamaJob" in extract_identifiers("'HakedisHesaplamaJob' nedir?")
    assert "BatchJobController" in extract_identifiers('"BatchJobController" endpointleri')
    assert "job ile sonuc" in extract_identifiers("'job ile sonuc' ifadesini ara")


@pytest.mark.unit
def test_quoted_phrase_still_works_next_to_suffix_apostrophes() -> None:
    query = "backend'inde 'HakedisOlusturmaJob' ve job'lari nedir?"
    identifiers = extract_identifiers(query)
    assert "HakedisOlusturmaJob" in identifiers
    assert not any(item.startswith("inde") for item in identifiers)


@pytest.mark.unit
@pytest.mark.parametrize("identifier", ["SCHEMA.PCK_X", "A_B", "src/X.java", "#123", "SKYRSM-5659"])
def test_exact_identifiers_are_preserved(identifier: str) -> None:
    assert identifier in extract_identifiers(f"{identifier} nerede kullaniliyor?")


@pytest.mark.unit
def test_adjacent_quoted_phrases_do_not_capture_the_gap_between_them() -> None:
    identifiers = extract_identifiers("'JobA', 'JobB' ve 'a 'b' c' nedir?")
    assert "JobA" in identifiers
    assert "JobB" in identifiers
    assert ", " not in identifiers
    assert "a " not in identifiers
    assert " c" not in identifiers
