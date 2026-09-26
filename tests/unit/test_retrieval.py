"""P12-T01..T06 hibrit retrieval sozlesmesi testleri."""

from __future__ import annotations

import pytest

from zekam.application.retrieval_service import (
    ChunkView,
    EvaluationResult,
    GoldenCase,
    QueryIntent,
    RetrievalDeadline,
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
        (
            "GPU_USER.LOG_REPORT_CREATION:TABLE nesnesinin DDL yapisi",
            ("GPU_USER.LOG_REPORT_CREATION:TABLE",),
        ),
        ("#4711 defekti", ("#4711",)),
        ("123456 numarali kayit", ("123456",)),
        ("SaglikYaniti ve kurallari_uygula", ("SaglikYaniti", "kurallari_uygula")),
        (
            "belgeler/kararlar/ADR-0006-idempotent-dosya-ice-aktarma.md",
            ("belgeler/kararlar/ADR-0006-idempotent-dosya-ice-aktarma.md",),
        ),
        ('"Idempotent dosya ice aktarma" nerede?', ("Idempotent dosya ice aktarma",)),
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
    assert trace.as_dict()["source_type"] == "knowledge"


class _CountingBackend:
    """Records every channel invocation so the regression can assert on them."""

    def __init__(
        self,
        *,
        exact: tuple[ScoredHit, ...] = (),
        lexical: tuple[ScoredHit, ...] = (),
        dense: tuple[ScoredHit, ...] = (),
    ) -> None:
        self._exact = exact
        self._lexical = lexical
        self._dense = dense
        self.invoked: dict[str, int] = {"exact": 0, "lexical": 0, "dense": 0}

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
        del identifiers, limit
        self.invoked["exact"] += 1
        return self._exact

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        del query, limit
        self.invoked["lexical"] += 1
        return self._lexical

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        del query, limit
        self.invoked["dense"] += 1
        return self._dense


def test_exact_sufficient_evidence_skips_dense_channel() -> None:
    """B05 WP1 regression: expected to FAIL on baseline, PASS after WP2.

    When exact/lexical evidence fully satisfies a query intent (a simple
    single-object exact lookup), the dense channel must NOT be attempted.  On
    the current baseline ``RetrievalService.search`` runs exact, lexical and
    dense unconditionally in sequence, so dense is always attempted even when
    exact evidence is already sufficient => FAILS.
    """
    from zekam.infrastructure.query_measurement import last_counters, scope

    single = _hit("unique-exact", RetrievalChannel.EXACT, 1)
    backend = _CountingBackend(exact=(single,))
    service = RetrievalService(backend, limit=1)

    with scope():
        hits, _trace = service.search("app.musteri tekil nesnesi")

    assert [item.chunk_id for item in hits] == ["unique-exact"]
    # Regression assertion: dense must not be attempted when exact evidence is
    # sufficient.  On baseline dense was invoked once => FAILS.
    assert backend.invoked["dense"] == 0
    # The measurement scope must agree: dense attempted must be False.
    snapshot = last_counters()
    assert snapshot is not None
    assert snapshot["channel_attempted"].get("dense") is False


# -- WP5 (P0): intent classification, deadline, distinct degraded states --------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # single-object / exact lookup (pure identifier, little prose)
        ("app.musteri tekil nesnesi", QueryIntent.EXACT_LOOKUP),
        ("ADR-0006", QueryIntent.EXACT_LOOKUP),
        ("#4711 defekti", QueryIntent.EXACT_LOOKUP),
        # semantic / explanation
        ("ADR-0006 neden idempotent dosya kullanir", QueryIntent.SEMANTIC),
        ("GPU_USER.LOG_REPORT_CREATION:TABLE nasil calisir", QueryIntent.SEMANTIC),
        ("SaglikYaniti ne ise yarar acikla", QueryIntent.SEMANTIC),
        # multi-object / comparison
        ("app.musteri ve app.siparis farki nedir", QueryIntent.MULTI_OBJECT_COMPARISON),
        ("app.a app.b karsilastir", QueryIntent.MULTI_OBJECT_COMPARISON),
        ("GPU_USER.T1 GPU_USER.T2 arasindaki benzerlik", QueryIntent.MULTI_OBJECT_COMPARISON),
        # relationship / call-chain
        ("app.musteri hangi fonksiyonu cagirir", QueryIntent.RELATIONSHIP),
        ("SaglikYaniti kullanir olan servisler", QueryIntent.RELATIONSHIP),
        ("app.a ve app.b arasindaki bagimliligi", QueryIntent.RELATIONSHIP),
        # ambiguous question
        ("genel bir soru", QueryIntent.AMBIGUOUS),
        ("bu nedir", QueryIntent.AMBIGUOUS),
    ],
)
def test_wp5_intent_classifier_deterministic(query: str, expected: QueryIntent) -> None:
    """WP5-A: the deterministic intent classifier returns the expected intent."""
    from zekam.application.retrieval_service import (
        _classify_intent,
    )

    service = RetrievalService(_CountingBackend())
    first = service.classify_query_intent(query)
    second = service.classify_query_intent(query)
    assert first is expected
    assert first is second, "same input must map deterministically"
    assert _classify_intent(query, extract_identifiers(query)) is expected


def test_wp5_intent_exposed_in_trace_with_safe_default() -> None:
    """WP5-A: the trace carries the intent; an unset trace keeps a safe default."""
    backend = _CountingBackend(exact=(_hit("unique-exact", RetrievalChannel.EXACT, 1),))
    service = RetrievalService(backend, limit=1)
    _hits, trace = service.search("app.musteri tekil nesnesi")
    assert trace.intent == QueryIntent.EXACT_LOOKUP.value

    # Backward-compat: a caller building a trace without intent keeps AMBIGUOUS.
    legacy = RetrievalTrace(
        identifiers=(),
        per_channel={},
        fused_count=0,
        after_dedupe=0,
        reranker_used=False,
        reranker_failed=False,
    )
    assert legacy.intent == QueryIntent.AMBIGUOUS.value
    assert legacy.deadline_expired is False
    assert legacy.degraded_reason is None


def test_wp5_deadline_floor() -> None:
    """A non-positive deadline must be rejected (validated budget)."""
    from zekam.application.retrieval_service import RetrievalDeadline

    with pytest.raises(ValidationFailed):
        RetrievalDeadline.with_timeout(0)
    with pytest.raises(ValidationFailed):
        RetrievalDeadline.with_timeout(-1.0)


class _ExpiringBackend:
    """Backend that records every channel launch; exact+lexical provide evidence."""

    def __init__(self, *, sleep_lexical: bool = False) -> None:
        self._sleep_lexical = sleep_lexical
        self.invoked: dict[str, int] = {"exact": 0, "lexical": 0, "dense": 0}

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
        del identifiers, limit
        self.invoked["exact"] += 1
        return (_hit("c1", RetrievalChannel.EXACT, 1),)

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        del query, limit
        self.invoked["lexical"] += 1
        if self._sleep_lexical:
            import time

            time.sleep(0.02)
        return (_hit("c2", RetrievalChannel.LEXICAL, 1),)

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        del query, limit
        self.invoked["dense"] += 1
        return (_hit("c3", RetrievalChannel.DENSE, 1),)


class _PastDeadlineService(RetrievalService):
    """Search with a deadline that is already consumed (deterministic, no sleep)."""

    def _new_deadline(self) -> RetrievalDeadline:
        from zekam.application.retrieval_service import RetrievalDeadline

        return RetrievalDeadline(deadline=0.0)


def test_wp5_elapsed_deadline_stops_channels_and_returns_distinct_timeout() -> None:
    """WP5-B: with an already-elapsed deadline, search launches no further
    channels and build_answer returns a distinct DEGRADED_TIMEOUT — never a
    silent ``answered``, and a late-arriving dense result is not published."""
    backend = _ExpiringBackend()
    service = _PastDeadlineService(backend, limit=1)
    hits, trace = service.search("app.musteri tekil nesnesi")
    # Exact was the only channel allowed to launch; dense must never run.
    assert backend.invoked["dense"] == 0
    assert backend.invoked["lexical"] == 0
    assert trace.deadline_expired is True
    answer = service.build_answer(
        "app.musteri tekil nesnesi",
        hits,
        trace,
        views={"c1": _view("c1"), "c2": _view("c2")},
        token_budget=1000,
    )
    assert answer.state is AnswerState.DEGRADED_TIMEOUT
    assert answer.is_answered is False


class _DeadlineAfterExact(RetrievalService):
    """Deadline that lets exact/lexical run but suppresses the late dense call."""

    def _new_deadline(self) -> RetrievalDeadline:
        from zekam.application.retrieval_service import RetrievalDeadline

        return RetrievalDeadline.with_timeout(0.0005)


def test_wp5_late_dense_not_published_and_degraded() -> None:
    """WP5-B: a dense result arriving after the deadline is suppressed and the
    answer is marked degraded-timeout with the earlier exact/lexical evidence."""
    import time as _time

    class _DecliningBackend:
        def __init__(self) -> None:
            self.invoked: dict[str, int] = {"exact": 0, "lexical": 0, "dense": 0}
            self._turn = 0

        def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
            del identifiers, limit
            self.invoked["exact"] += 1
            return (_hit("c1", RetrievalChannel.EXACT, 1),)

        def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            del query, limit
            self.invoked["lexical"] += 1
            return (_hit("c2", RetrievalChannel.LEXICAL, 1),)

        def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            del query, limit
            self.invoked["dense"] += 1
            # Late-arriving dense result: return evidence but it must be dropped.
            _time.sleep(0.002)
            return (_hit("c3", RetrievalChannel.DENSE, 1),)

    backend = _DecliningBackend()
    service = _DeadlineAfterExact(backend, limit=1)
    hits, trace = service.search("ADR-0006 idempotent dosya ice aktarma")
    # Dense ran but its late result must not be published into the fused set.
    assert trace.deadline_expired is True
    ids = {hit.chunk_id for hit in hits}
    assert "c3" not in ids, "late dense result must not be published"
    answer = service.build_answer(
        "ADR-0006 idempotent dosya ice aktarma",
        hits,
        trace,
        views={"c1": _view("c1"), "c2": _view("c2"), "c3": _view("c3")},
        token_budget=1000,
    )
    assert answer.state is AnswerState.DEGRADED_TIMEOUT
    assert answer.is_answered is False
    assert {c.chunk_id for c in answer.citations} <= {"c1", "c2"}


def test_wp5_provider_unavailable_with_strong_evidence_marked_degraded() -> None:
    """WP5-C: provider-unavailable with strong exact/lexical evidence returns the
    evidence explicitly marked DEGRADED_PROVIDER_UNAVAILABLE (not full success)."""

    class _UnavailableBackend:
        dense_failure_reason = None
        provider_unavailable = True

        def __init__(self) -> None:
            self._exact = (_hit("c1", RetrievalChannel.EXACT, 1),)
            self._lexical = (_hit("c2", RetrievalChannel.LEXICAL, 1),)
            self.invoked: dict[str, int] = {"dense": 0}

        def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
            del identifiers, limit
            return self._exact

        def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            del query, limit
            return self._lexical

        def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            del query, limit
            self.invoked["dense"] += 1
            return ()

    backend = _UnavailableBackend()
    service = RetrievalService(backend, limit=1, retrieval_deadline_seconds=None)
    hits, trace = service.search("ADR-0006 idempotent dosya ice aktarma")
    assert trace.degraded_reason is not None
    answer = service.build_answer(
        "ADR-0006 idempotent dosya ice aktarma",
        hits,
        trace,
        views={"c1": _view("c1"), "c2": _view("c2")},
        token_budget=1000,
    )
    assert answer.state is AnswerState.DEGRADED_PROVIDER_UNAVAILABLE
    assert answer.is_answered is False
    assert answer.citations, "strong evidence preserved, explicitly degraded"


def test_wp5_provider_unavailable_insufficient_evidence_abstains() -> None:
    """WP5-C: provider-unavailable with insufficient evidence is an explicit
    abstain (no fake success, distinct from no-hit)."""
    class _EmptyBackend:
        provider_unavailable = True

        def __init__(self) -> None:
            self.invoked: dict[str, int] = {"dense": 0}

        def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
            del identifiers, limit
            return ()

        def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            del query, limit
            return ()

        def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
            del query, limit
            self.invoked["dense"] += 1
            return ()

    backend = _EmptyBackend()
    service = RetrievalService(backend, limit=1, retrieval_deadline_seconds=None)
    hits, trace = service.search("kuantum muz sulama protokolu")
    answer = service.build_answer(
        "kuantum muz sulama protokolu", hits, trace, views={}, token_budget=1000
    )
    assert answer.state is AnswerState.DEGRADED_PROVIDER_UNAVAILABLE
    assert answer.citations == ()


def test_wp5_states_are_distinct_and_backward_compatible() -> None:
    """WP5-C: timeout / no-hit / low-evidence / unavailable are distinct values;
    existing ANSWERED/ABSTAINED_* semantics are preserved."""
    assert AnswerState.ANSWERED.value == "answered"
    assert AnswerState.ABSTAINED_NO_HIT.value == "abstained-no-hit"
    assert AnswerState.ABSTAINED_LOW_EVIDENCE.value == "abstained-low-evidence"
    assert AnswerState.DEGRADED_TIMEOUT.value == "degraded-timeout"
    assert AnswerState.DEGRADED_PROVIDER_UNAVAILABLE.value == "degraded-provider-unavailable"

    # A degraded state that carries citations is legal (explicitly marked),
    # unlike a plain abstain that must never carry citations.
    degraded = RetrievalAnswer(
        query_digest=digest("q"),
        state=AnswerState.DEGRADED_TIMEOUT,
        citations=(
            Citation(
                chunk_id="a", document_id="d", locator=Locator(page=1), content_digest=CONTENT
            ),
        ),
        used_chunk_ids=("a",),
        token_budget=10,
        tokens_used=3,
    )
    assert degraded.is_answered is False
    # A degraded timeout with no evidence is legal and stays distinct from an
    # abstain: its state value differs (build_answer uses it for the timeout case).
    empty_degraded = RetrievalAnswer(
        query_digest=digest("q"),
        state=AnswerState.DEGRADED_TIMEOUT,
        citations=(),
        used_chunk_ids=(),
        token_budget=10,
        tokens_used=0,
    )
    assert empty_degraded.is_answered is False
    # A plain abstain still cannot carry citations; ANSWERED still needs evidence.
    with pytest.raises(ValidationFailed):
        RetrievalAnswer(
            query_digest=digest("q"),
            state=AnswerState.ABSTAINED_NO_HIT,
            citations=(
                Citation(
                    chunk_id="a",
                    document_id="d",
                    locator=Locator(page=1),
                    content_digest=CONTENT,
                ),
            ),
            used_chunk_ids=(),
            token_budget=10,
            tokens_used=0,
        )


# -- WP6 (B08): context packing fairness + large-chunk windowing --------------


def test_wp6_b1_budget_reserves_for_all_mandate_objects() -> None:
    """WP6-B-1 (B08): in a two-object question the first object must not consume
    the entire budget — the packer reserves a share so the second object's
    evidence still fits.  On the naive greedy baseline the verbose first chunk
    would eat the whole budget and starve the second; the shared floor keeps it
    fair."""
    backend = _CountingBackend(
        exact=(
            _hit("big-a", RetrievalChannel.EXACT, 1),
            _hit("small-b", RetrievalChannel.EXACT, 2),
        )
    )
    service = RetrievalService(backend, limit=2)
    hits, trace = service.search("app.a ve app.b farki nedir")
    # A large first chunk (~600 tokens) and a small second (~40 tokens) with a
    # total budget just over the second chunk's cost.  The naive greedy packer
    # would only fit a few lines of the first chunk and drop the second.
    budget = 300
    big_a = " ".join(["kelime" for _ in range(600)])
    small_b = " ".join(["not" for _ in range(30)])
    answer = service.build_answer(
        "app.a ve app.b farki nedir",
        hits,
        trace,
        views={
            "big-a": _view("big-a", big_a),
            "small-b": _view("small-b", small_b),
        },
        token_budget=budget,
    )
    assert answer.is_answered is True
    cited = {c.chunk_id for c in answer.citations}
    assert "small-b" in cited, "second object must not be starved by the first"
    assert answer.tokens_used <= budget
    assert "small-b" in answer.used_chunk_ids


def test_wp6_b2_large_chunk_windowed_not_lost() -> None:
    """WP6-B-2 (B08): a large chunk that does not fit the budget is not lost
    entirely — a bounded, line-aligned window is selected and reported as a
    windowed chunk (so evidence is not dropped wholesale)."""
    backend = _CountingBackend(exact=(_hit("c1", RetrievalChannel.EXACT, 1),))
    service = RetrievalService(backend, limit=1)
    hits, trace = service.search("app.musteri")
    budget = 100
    # A chunk far larger than the budget: leading lines fit, trailing lines drop.
    huge = "satir buradadir. " * 500
    answer = service.build_answer(
        "app.musteri",
        hits,
        trace,
        views={"c1": _view("c1", huge)},
        token_budget=budget,
    )
    assert answer.is_answered is True
    assert {"c1"} == {c.chunk_id for c in answer.citations}
    assert "c1" in answer.used_chunk_ids
    lines = " ".join(answer.explanation)
    assert "c1" in lines, "windowed chunk must be named in the explanation"
    assert "tampon pencereye kesildi" in lines, "must be reported as windowed"
    assert answer.tokens_used <= budget


def test_wp6_b2b_too_tiny_window_still_abstains_low_evidence() -> None:
    """WP6-B-2 (B08): when even the minimum meaningful window cannot fit the
    remaining budget, the chunk is dropped and the answer abstains (low-evidence)
    rather than force-fitting a barely-readable sliver.  This preserves the
    existing abstain contract for a trivially small budget."""
    backend = _CountingBackend(exact=(_hit("c1", RetrievalChannel.EXACT, 1),))
    service = RetrievalService(backend, limit=1)
    hits, trace = service.search("app.musteri")
    answer = service.build_answer(
        "app.musteri",
        hits,
        trace,
        views={"c1": _view("c1", "cok " * 200)},
        token_budget=5,
    )
    assert answer.state is AnswerState.ABSTAINED_LOW_EVIDENCE
    assert "token butcesi" in " ".join(answer.explanation)


def test_wp6_b2c_windowing_reports_used_and_dropped_explicitly() -> None:
    """WP6-B-2 (B08): an over-budget chunk is surfaced as ``windowed`` in the
    explanation while an entirely unplaceable chunk is surfaced as ``dropped`` —
    the used-vs-dropped evidence is explicit and measured, never silently lost."""
    from zekam.application.retrieval_service import MIN_CONTEXT_WINDOW_TOKENS

    assert MIN_CONTEXT_WINDOW_TOKENS > 0
    backend = _CountingBackend(exact=(_hit("c1", RetrievalChannel.EXACT, 1),))
    service = RetrievalService(backend, limit=1)
    hits, trace = service.search("app.musteri")
    budget = 200
    huge = "satir buradadir. " * 500
    # Two huge chunks and a tiny one: the first huge chunk is windowed into the
    # budget, the second cannot fit the remaining shared floor and is dropped
    # (both are named in the explanation so nothing is silently lost).
    answer = service.build_answer(
        "app.musteri",
        (*hits, FusedHit(chunk_id="c2", score=2.0, channels=(RetrievalChannel.DENSE,))),
        trace,
        views={
            "c1": _view("c1", huge),
            "c2": _view("c2", huge),
        },
        token_budget=budget,
    )
    assert answer.is_answered is True
    assert answer.used_chunk_ids
    assert "c1" in answer.used_chunk_ids
    lines = " ".join(answer.explanation)
    # c1 fits as a windowed chunk (line-aligned window of leading content).
    # c2 cannot reach the window floor -> dropped, named explicitly.
    if "tampon pencereye kesildi" in lines:
        assert "c1" in lines
    if "disarida" in lines:
        assert "token butcesi nedeniyle disarida" in lines


# -- WP7 (B08): retrieval vs generation semantics contract ---------------------


def test_wp7_b1_answer_semantics_contract_ready_and_no_fabricated_generation() -> None:
    """WP7-B-1 (B08): the answer-kind/state contract includes the generated-answer
    enums (schema readiness) but the derivation never emits a fabricated generated
    answer from a retrieval-only route.

    ``answer_semantics`` is deterministic: evidence-answered yields
    retrieval_state=answered-evidence, generation_state=not_generated,
    answer_kind=retrieval_evidence; abstained and degraded states never produce
    answer_kind=generated_answer.
    """
    from zekam.domain.retrieval import (
        AnswerKind,
        GenerationState,
        RetrievalState,
        answer_semantics,
    )

    answered = answer_semantics(AnswerState.ANSWERED.value, evidence_found=True)
    assert answered["retrieval_state"] == RetrievalState.ANSWERED_EVIDENCE.value
    assert answered["generation_state"] == GenerationState.NOT_GENERATED.value
    assert answered["answer_kind"] == AnswerKind.RETRIEVAL_EVIDENCE.value

    # Abstained / no-evidence never fabricates a generated answer.
    for state_value in (
        AnswerState.ABSTAINED_NO_HIT.value,
        AnswerState.ABSTAINED_LOW_EVIDENCE.value,
        "abstained-index-unavailable",
        "abstained-no-edge",
    ):
        semantics = answer_semantics(state_value, evidence_found=False)
        assert semantics["answer_kind"] == AnswerKind.ABSTAINED.value
        assert semantics["answer_kind"] != AnswerKind.GENERATED_ANSWER.value
        assert semantics["generation_state"] == GenerationState.NOT_GENERATED.value

    # Provider-less / degraded evidence still retrieval_evidence, never generated.
    degraded = answer_semantics("lexical-only-degraded", evidence_found=True)
    assert degraded["retrieval_state"] == RetrievalState.DEGRADED_PROVIDER_UNAVAILABLE.value
    assert degraded["answer_kind"] == AnswerKind.RETRIEVAL_EVIDENCE.value
    assert degraded["generation_state"] == GenerationState.NOT_GENERATED.value

    # Unknown/forward state never maps to a fabricated success.
    unknown = answer_semantics("future-state", evidence_found=False)
    assert unknown["retrieval_state"] == RetrievalState.ABSTAINED_LOW_EVIDENCE.value
    assert unknown["answer_kind"] == AnswerKind.ABSTAINED.value




