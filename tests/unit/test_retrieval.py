"""P12-T01..T06 hibrit retrieval sozlesmesi testleri."""

from __future__ import annotations

import pytest

from zekam.application.retrieval_service import (
    ChunkView,
    EvaluationResult,
    GoldenCase,
    RetrievalService,
    RetrievalTrace,
    evaluate,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import ContentUnit, Locator, UnitKind
from zekam.domain.retrieval import (
    AnswerState,
    Chunk,
    ChunkProfile,
    Citation,
    EmbeddingProfile,
    FusedHit,
    RetrievalAnswer,
    RetrievalChannel,
    ScoredHit,
    bge_m3_profile,
    chunk_units,
    dedupe,
    estimate_tokens,
    expand_parents,
    extract_identifiers,
    reciprocal_rank_fusion,
    requires_reindex,
)

CONTENT = digest("content")


def _unit(text: str, kind: UnitKind = UnitKind.PARAGRAPH, order: int = 0) -> ContentUnit:
    return ContentUnit(
        unit_id=f"u{order}",
        kind=kind,
        text=text,
        locator=Locator(block_index=order),
        order=order,
    )


# -- T01: chunker -------------------------------------------------------------


def test_profil_sinirlari_dogrulanir() -> None:
    with pytest.raises(ValidationFailed):
        ChunkProfile(name="p", max_tokens=0)
    with pytest.raises(ValidationFailed):
        ChunkProfile(name="p", max_tokens=100, overlap_tokens=100)
    with pytest.raises(ValidationFailed):
        ChunkProfile(name="", max_tokens=100)


def test_kod_ve_tablo_butun_kalir() -> None:
    profile = ChunkProfile(name="p", max_tokens=10, overlap_tokens=2)
    units = (
        _unit("bir iki uc dort bes alti yedi sekiz dokuz on onbir", UnitKind.CODE, 0),
        _unit("a b c d e f g h i j k l m n o p", UnitKind.TABLE, 1),
    )
    chunks = chunk_units(units, document_id="d", profile=profile)
    assert [chunk.kind for chunk in chunks] == [UnitKind.CODE, UnitKind.TABLE]
    assert chunks[0].text == units[0].text, "kod blogu bolunmemeli"
    assert all(chunk.parent_id is None for chunk in chunks)


def test_baslik_altindaki_paragraflar_birlestirilir() -> None:
    profile = ChunkProfile(name="p", max_tokens=50, overlap_tokens=5)
    units = (
        _unit("Ust baslik", UnitKind.HEADING, 0),
        _unit("birinci paragraf", UnitKind.PARAGRAPH, 1),
        _unit("ikinci paragraf", UnitKind.PARAGRAPH, 2),
    )
    chunks = chunk_units(units, document_id="d", profile=profile)
    assert len(chunks) == 2
    assert chunks[0].kind is UnitKind.HEADING
    assert "birinci paragraf" in chunks[1].text
    assert "ikinci paragraf" in chunks[1].text


def test_buyuk_birim_parent_child_uretir() -> None:
    profile = ChunkProfile(name="p", max_tokens=5, overlap_tokens=1)
    long_text = " ".join(f"kelime{index}" for index in range(30))
    chunks = chunk_units((_unit(long_text),), document_id="d", profile=profile)
    parent = chunks[0]
    children = [chunk for chunk in chunks if chunk.parent_id == parent.chunk_id]
    assert parent.parent_id is None
    assert len(children) >= 2
    assert all(child.locator == parent.locator for child in children)


def test_chunk_locatoru_korur() -> None:
    chunks = chunk_units(
        (_unit("metin"),), document_id="d", profile=ChunkProfile(name="p", max_tokens=100)
    )
    assert chunks[0].locator.block_index == 0
    with pytest.raises(ValidationFailed):
        Chunk(
            chunk_id="c",
            document_id="d",
            text="metin",
            locator=Locator(),
            kind=UnitKind.PARAGRAPH,
            token_count=1,
            order=0,
        )


def test_profil_degisikligi_digesti_degistirir() -> None:
    first = ChunkProfile(name="p", max_tokens=100)
    second = ChunkProfile(name="p", max_tokens=200)
    assert first.profile_digest != second.profile_digest


# -- T02: embedding profili ---------------------------------------------------


def test_bge_m3_profili_1024_cosine() -> None:
    profile = bge_m3_profile()
    assert profile.dimension == 1024
    assert profile.distance == "cosine"
    assert profile.model_ref == "openai/BAAI/bge-m3"


def test_boyut_ve_sonluluk_dogrulanir() -> None:
    profile = EmbeddingProfile(model_ref="m", dimension=3)
    profile.validate_vector((0.1, 0.2, 0.3))
    with pytest.raises(ValidationFailed):
        profile.validate_vector((0.1, 0.2))
    with pytest.raises(ValidationFailed):
        profile.validate_vector((0.1, float("nan"), 0.3))
    with pytest.raises(ValidationFailed):
        profile.validate_vector((0.1, float("inf"), 0.3))


def test_prefix_degisikligi_reindex_gerektirir() -> None:
    current = bge_m3_profile(query_prefix="query: ")
    incoming = bge_m3_profile(query_prefix="soru: ")
    assert requires_reindex(current, incoming) is True
    assert requires_reindex(current, bge_m3_profile(query_prefix="query: ")) is False


def test_farkli_boyut_ayri_profildir() -> None:
    assert requires_reindex(
        bge_m3_profile(), EmbeddingProfile(model_ref="openai/BAAI/bge-m3", dimension=512)
    )


# -- T03: exact identifier ----------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("ZEKAM-P12-T01 nasil calisir", ("ZEKAM-P12-T01",)),
        ("app.musteri tablosu", ("app.musteri",)),
        ("#4711 defekti", ("#4711",)),
        ("123456 numarali kayit", ("123456",)),
        ("genel bir soru", ()),
    ],
)
def test_exact_kimlik_cikarimi(query: str, expected: tuple[str, ...]) -> None:
    assert extract_identifiers(query) == expected


def test_token_tahmini_deterministik() -> None:
    assert estimate_tokens("bir iki uc") == 3
    assert estimate_tokens("a.b, c") == 5


# -- T04: RRF -----------------------------------------------------------------


def _hit(chunk_id: str, channel: RetrievalChannel, rank: int, score: float = 1.0) -> ScoredHit:
    return ScoredHit(chunk_id=chunk_id, channel=channel, rank=rank, raw_score=score)


def test_rrf_ham_skorlari_toplamaz() -> None:
    """Dense mesafe 0.01, lexical rank 12.0 olsa bile yalniz sira kullanilir."""

    channels = {
        RetrievalChannel.DENSE: (_hit("a", RetrievalChannel.DENSE, 1, 0.01),),
        RetrievalChannel.LEXICAL: (_hit("b", RetrievalChannel.LEXICAL, 1, 12.0),),
    }
    fused = reciprocal_rank_fusion(channels)
    assert {item.chunk_id for item in fused} == {"a", "b"}
    assert fused[0].score == pytest.approx(fused[1].score)


def test_iki_kanalda_gorunen_sonuc_one_cikar() -> None:
    channels = {
        RetrievalChannel.DENSE: (
            _hit("a", RetrievalChannel.DENSE, 1),
            _hit("b", RetrievalChannel.DENSE, 2),
        ),
        RetrievalChannel.LEXICAL: (_hit("b", RetrievalChannel.LEXICAL, 1),),
    }
    fused = reciprocal_rank_fusion(channels)
    assert fused[0].chunk_id == "b"
    assert set(fused[0].channels) == {RetrievalChannel.DENSE, RetrievalChannel.LEXICAL}


def test_exact_eslesme_dusuk_dense_skorla_elenmez() -> None:
    channels = {
        RetrievalChannel.EXACT: (_hit("kimlik", RetrievalChannel.EXACT, 1),),
        RetrievalChannel.DENSE: (
            _hit("alakasiz-1", RetrievalChannel.DENSE, 1),
            _hit("alakasiz-2", RetrievalChannel.DENSE, 2),
            _hit("kimlik", RetrievalChannel.DENSE, 20),
        ),
    }
    fused = reciprocal_rank_fusion(channels, exact_ids=frozenset({"kimlik"}))
    assert fused[0].chunk_id == "kimlik"
    assert fused[0].exact_match is True


def test_ayni_kanalda_tekrar_eden_sira_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        reciprocal_rank_fusion(
            {
                RetrievalChannel.DENSE: (
                    _hit("a", RetrievalChannel.DENSE, 1),
                    _hit("b", RetrievalChannel.DENSE, 1),
                )
            }
        )


def test_rrf_deterministiktir() -> None:
    channels = {
        RetrievalChannel.DENSE: (
            _hit("b", RetrievalChannel.DENSE, 1),
            _hit("a", RetrievalChannel.DENSE, 1 + 1),
        )
    }
    first = reciprocal_rank_fusion(channels)
    second = reciprocal_rank_fusion(channels)
    assert [item.chunk_id for item in first] == [item.chunk_id for item in second]


# -- T05: dedupe, expansion, reranker fallback --------------------------------


def test_ayni_icerik_iki_kez_baglama_girmez() -> None:
    hits = (
        FusedHit("a", 0.9, (RetrievalChannel.DENSE,)),
        FusedHit("b", 0.8, (RetrievalChannel.DENSE,)),
    )
    unique = dedupe(hits, content_digests={"a": "ayni", "b": "ayni"})
    assert [item.chunk_id for item in unique] == ["a"]


def test_ebeveyn_genisletmesi_tekrar_uretmez() -> None:
    hits = (
        FusedHit("cocuk-1", 0.9, (RetrievalChannel.DENSE,)),
        FusedHit("cocuk-2", 0.8, (RetrievalChannel.DENSE,)),
    )
    ordered = expand_parents(hits, parents={"cocuk-1": "ebeveyn", "cocuk-2": "ebeveyn"})
    assert ordered == ("ebeveyn", "cocuk-1", "cocuk-2")


class _Backend:
    def __init__(self, dense: tuple[ScoredHit, ...], lexical: tuple[ScoredHit, ...] = ()) -> None:
        self._dense = dense
        self._lexical = lexical

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
        return tuple(
            _hit(item, RetrievalChannel.EXACT, index)
            for index, item in enumerate(identifiers, start=1)
        )

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        return self._lexical

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        return self._dense


def test_reranker_hatasi_sonucu_kaybetmez() -> None:
    def broken(query: str, hits: tuple[FusedHit, ...]) -> tuple[FusedHit, ...]:
        raise RuntimeError("saglayici cokti")

    backend = _Backend((_hit("a", RetrievalChannel.DENSE, 1),))
    service = RetrievalService(backend, reranker=broken)
    hits, trace = service.search("genel soru")
    assert [item.chunk_id for item in hits] == ["a"]
    assert trace.reranker_failed is True
    assert "reranker basarisiz" in " ".join(trace.as_lines())


def test_reranker_sonuc_dusurursse_guvenilmez_sayilir() -> None:
    def lossy(query: str, hits: tuple[FusedHit, ...]) -> tuple[FusedHit, ...]:
        return hits[:1]

    backend = _Backend((_hit("a", RetrievalChannel.DENSE, 1), _hit("b", RetrievalChannel.DENSE, 2)))
    hits, trace = RetrievalService(backend, reranker=lossy).search("genel soru")
    assert len(hits) == 2
    assert trace.reranker_failed is True


def test_calisan_reranker_uygulanir() -> None:
    def reverse(query: str, hits: tuple[FusedHit, ...]) -> tuple[FusedHit, ...]:
        return tuple(reversed(hits))

    backend = _Backend((_hit("a", RetrievalChannel.DENSE, 1), _hit("b", RetrievalChannel.DENSE, 2)))
    hits, trace = RetrievalService(backend, reranker=reverse).search("genel soru")
    assert [item.chunk_id for item in hits] == ["b", "a"]
    assert trace.reranker_used is True


# -- T06: citation, abstain, aciklama -----------------------------------------


def _view(chunk_id: str, text: str = "kisa metin") -> ChunkView:
    return ChunkView(
        chunk_id=chunk_id,
        document_id="d1",
        text=text,
        locator=Locator(page=1),
        content_digest=digest(chunk_id),
    )


def test_sonuc_yoksa_no_hit_abstain() -> None:
    service = RetrievalService(_Backend(()))
    hits, trace = service.search("hicbir seye uymayan sorgu")
    answer = service.build_answer(
        "hicbir seye uymayan sorgu", hits, trace, views={}, token_budget=100
    )
    assert answer.state is AnswerState.ABSTAINED_NO_HIT
    assert answer.citations == ()
    assert answer.is_answered is False


def test_butce_yetmezse_dusuk_kanit_abstain() -> None:
    backend = _Backend((_hit("a", RetrievalChannel.DENSE, 1),))
    service = RetrievalService(backend)
    hits, trace = service.search("soru")
    answer = service.build_answer(
        "soru",
        hits,
        trace,
        views={"a": _view("a", "cok " * 200)},
        token_budget=5,
    )
    assert answer.state is AnswerState.ABSTAINED_LOW_EVIDENCE
    assert "token butcesi" in " ".join(answer.explanation)


def test_cevap_citation_ve_aciklama_tasir() -> None:
    backend = _Backend((_hit("a", RetrievalChannel.DENSE, 1),))
    service = RetrievalService(backend)
    hits, trace = service.search("app.musteri tablosu")
    answer = service.build_answer(
        "app.musteri tablosu", hits, trace, views={"a": _view("a")}, token_budget=1000
    )
    assert answer.is_answered is True
    assert answer.citations[0].locator.page == 1
    assert answer.as_dict()["grants_authority"] is False
    assert any("exact kimlik" in line for line in answer.explanation)


def test_kanitsiz_cevap_uretilemez() -> None:
    with pytest.raises(ValidationFailed):
        RetrievalAnswer(
            query_digest=digest("q"),
            state=AnswerState.ANSWERED,
            citations=(),
            used_chunk_ids=(),
            token_budget=10,
            tokens_used=0,
        )


def test_abstain_citation_tasiyamaz() -> None:
    citation = Citation(
        chunk_id="a", document_id="d", locator=Locator(page=1), content_digest=CONTENT
    )
    with pytest.raises(ValidationFailed):
        RetrievalAnswer(
            query_digest=digest("q"),
            state=AnswerState.ABSTAINED_NO_HIT,
            citations=(citation,),
            used_chunk_ids=(),
            token_budget=10,
            tokens_used=0,
        )


def test_baglam_token_butcesini_asamaz() -> None:
    with pytest.raises(PolicyViolation):
        RetrievalAnswer(
            query_digest=digest("q"),
            state=AnswerState.ABSTAINED_NO_HIT,
            citations=(),
            used_chunk_ids=(),
            token_budget=10,
            tokens_used=11,
        )


def test_retrieval_sonucu_authority_veremez() -> None:
    with pytest.raises(PolicyViolation):
        RetrievalAnswer(
            query_digest=digest("q"),
            state=AnswerState.ABSTAINED_NO_HIT,
            citations=(),
            used_chunk_ids=(),
            token_budget=10,
            tokens_used=0,
            grants_authority=True,
        )


def test_citation_locatorsuz_olamaz() -> None:
    with pytest.raises(ValidationFailed):
        Citation(chunk_id="a", document_id="d", locator=Locator(), content_digest=CONTENT)


# -- degerlendirme ------------------------------------------------------------


def test_golden_metrikleri_hesaplanir() -> None:
    cases = (
        GoldenCase(query="q1", relevant_ids=frozenset({"a"})),
        GoldenCase(query="q2", relevant_ids=frozenset({"b"})),
    )
    result = evaluate(cases, run=lambda q: ("a",) if q == "q1" else ("x", "b"), k=5)
    assert result.recall_at_k == pytest.approx(1.0)
    assert result.mrr == pytest.approx((1.0 + 0.5) / 2)
    assert 0.0 < result.ndcg_at_k <= 1.0
    assert result.case_count == 2


def test_iyilesme_gerileme_olmadan_olcusur() -> None:
    baseline = EvaluationResult(recall_at_k=0.5, mrr=0.4, ndcg_at_k=0.45, k=5, case_count=2)
    better = EvaluationResult(recall_at_k=0.7, mrr=0.4, ndcg_at_k=0.5, k=5, case_count=2)
    mixed = EvaluationResult(recall_at_k=0.9, mrr=0.1, ndcg_at_k=0.5, k=5, case_count=2)
    assert better.improves_on(baseline) is True
    assert mixed.improves_on(baseline) is False, "bir metrik gerilerse iyilesme sayilmaz"
    assert baseline.improves_on(baseline) is False


def test_bos_golden_kume_reddedilir() -> None:
    with pytest.raises(ValidationFailed):
        evaluate((), run=lambda q: ())
    with pytest.raises(ValidationFailed):
        GoldenCase(query="q", relevant_ids=frozenset())


def test_trace_aciklamasi_kanallari_gosterir() -> None:
    trace = RetrievalTrace(
        identifiers=("ZEKAM-P12",),
        per_channel={"dense": 3, "lexical": 2},
        fused_count=4,
        after_dedupe=3,
        reranker_used=False,
        reranker_failed=False,
    )
    lines = " ".join(trace.as_lines())
    assert "ZEKAM-P12" in lines
    assert "dense=3" in lines
    assert "fusion sonrasi: 4" in lines
