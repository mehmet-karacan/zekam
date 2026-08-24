"""P13-T01..T06 bellek sozlesmesi ve promotion kapisi testleri."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.application.memory_service import (
    Mem0Adapter,
    NativeMemoryEngine,
    PromotionGate,
    ReviewDecision,
    observation_digest,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory import (
    HygieneFinding,
    MemoryCandidate,
    MemoryClass,
    MemoryEvidence,
    MemoryKey,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemoryState,
    RetentionPolicy,
    SyncState,
    supersede,
)
from zekam.domain.retrieval import FusedHit

NOW = dt.datetime(2026, 8, 21, tzinfo=dt.UTC)
EVIDENCE = (MemoryEvidence(kind="test", reference="tests/x.py", digest_value=digest("e")),)


def _key(scope: MemoryScope = MemoryScope.PROJECT, project: str = "zekam") -> MemoryKey:
    return MemoryKey(
        scope=scope,
        realm_ref="varsayilan",
        project_ref=project,
        work_ref="W-1",
        run_ref="R-1",
        agent_ref="A-1",
    )


def _record(**kwargs: object) -> MemoryRecord:
    defaults: dict[str, object] = {
        "memory_id": "m1",
        "key": _key(),
        "memory_class": MemoryClass.EPISODIC,
        "content": "Migration 0013 pgvector HNSW indeksini ekledi",
        "state": MemoryState.ACTIVE,
        "revision": 1,
        "created_at": NOW,
        "evidence": EVIDENCE,
    }
    defaults.update(kwargs)
    return MemoryRecord(**defaults)  # type: ignore[arg-type]


def _candidate(**kwargs: object) -> MemoryCandidate:
    defaults: dict[str, object] = {
        "candidate_id": "c1",
        "key": _key(),
        "memory_class": MemoryClass.EPISODIC,
        "content": "Gozlenen davranis",
        "author_ref": "agent-a",
        "observed_at": NOW,
        "evidence": EVIDENCE,
    }
    defaults.update(kwargs)
    return MemoryCandidate(**defaults)  # type: ignore[arg-type]


# -- T01: port ve sozlesme ----------------------------------------------------


def test_kapsam_kendi_referansini_ister() -> None:
    with pytest.raises(ValidationFailed):
        MemoryKey(scope=MemoryScope.PROJECT, realm_ref="r")
    with pytest.raises(ValidationFailed):
        MemoryKey(scope=MemoryScope.WORK_ITEM, realm_ref="r", project_ref="p")
    assert MemoryKey(scope=MemoryScope.GLOBAL_USER, realm_ref="r").project_ref is None


def test_gecici_kapsamlar_kalici_bellek_degildir() -> None:
    assert _key(MemoryScope.AGENT).is_ephemeral is True
    assert _key(MemoryScope.RUN).is_ephemeral is True
    assert _key(MemoryScope.PROJECT).is_ephemeral is False


def test_bellek_authority_veremez() -> None:
    with pytest.raises(PolicyViolation):
        _record(grants_authority=True)
    assert _record().body()["grants_authority"] is False


def test_bellek_secret_tasiyamaz() -> None:
    with pytest.raises(PolicyViolation):
        _record(content="api_key=AKIA1234567890 kullanildi")
    with pytest.raises(PolicyViolation):
        _candidate(content="parola degeri: abc")


def test_kanitsiz_kayit_aktif_olamaz() -> None:
    with pytest.raises(PolicyViolation):
        _record(evidence=())


def test_gecerlilik_araligi_dogrulanir() -> None:
    with pytest.raises(ValidationFailed):
        _record(valid_from=NOW, valid_until=NOW - dt.timedelta(days=1))
    record = _record(valid_from=NOW, valid_until=NOW + dt.timedelta(days=1))
    assert record.is_valid_at(NOW) is True
    assert record.is_valid_at(NOW + dt.timedelta(days=2)) is False


# -- T03: promotion kapisi ----------------------------------------------------


def test_ham_model_ciktisi_dogrudan_aktif_olamaz() -> None:
    """Semantic bilgi review olmadan aktiflesemez."""

    candidate = _candidate(memory_class=MemoryClass.SEMANTIC)
    allowed, reason = PromotionGate().evaluate(candidate, None)
    assert allowed is False
    assert "review" in reason
    with pytest.raises(PolicyViolation):
        NativeMemoryEngine().write(candidate, now=NOW)


def test_review_yazarla_ayni_kimlik_olamaz() -> None:
    candidate = _candidate(memory_class=MemoryClass.PROCEDURAL, author_ref="agent-a")
    decision = ReviewDecision(approved=True, reviewer_ref="agent-a", reason="ok")
    allowed, reason = PromotionGate().evaluate(candidate, decision)
    assert allowed is False
    assert "ayni kimlik" in reason


def test_bagimsiz_review_ile_aktiflesir() -> None:
    candidate = _candidate(memory_class=MemoryClass.SEMANTIC, author_ref="agent-a")
    decision = ReviewDecision(approved=True, reviewer_ref="reviewer-b", reason="dogrulandi")
    record = NativeMemoryEngine().write(candidate, now=NOW, decision=decision)
    assert record.state is MemoryState.ACTIVE
    assert record.reviewed_by == "reviewer-b"


def test_reddedilen_review_aktiflestirmez() -> None:
    candidate = _candidate(memory_class=MemoryClass.SEMANTIC)
    decision = ReviewDecision(approved=False, reviewer_ref="reviewer-b", reason="kanit zayif")
    with pytest.raises(PolicyViolation):
        NativeMemoryEngine().write(candidate, now=NOW, decision=decision)


def test_kanitsiz_aday_gecemez() -> None:
    allowed, reason = PromotionGate().evaluate(_candidate(evidence=()), None)
    assert allowed is False
    assert "kanitsiz" in reason


def test_failure_dersi_iki_gozlem_ister() -> None:
    decision = ReviewDecision(approved=True, reviewer_ref="reviewer-b", reason="ok")
    tek = _candidate(memory_class=MemoryClass.FAILURE, occurrence_key="k1", observation_count=1)
    allowed, reason = PromotionGate().evaluate(tek, decision)
    assert allowed is False
    assert "gozlem" in reason

    cift = _candidate(memory_class=MemoryClass.FAILURE, occurrence_key="k1", observation_count=2)
    assert PromotionGate().evaluate(cift, decision)[0] is True


def test_failure_adayi_occurrence_key_ister() -> None:
    with pytest.raises(ValidationFailed):
        _candidate(memory_class=MemoryClass.FAILURE)


def test_gecici_kapsam_kalici_bellek_uretemez() -> None:
    candidate = _candidate(key=_key(MemoryScope.AGENT))
    allowed, reason = PromotionGate().evaluate(candidate, None)
    assert allowed is False
    assert "gecici" in reason
    with pytest.raises(PolicyViolation):
        candidate.promote(memory_id="m", reviewed_by=None, now=NOW)


# -- supersession -------------------------------------------------------------


def test_mevcut_bilgi_sessizce_ezilmez() -> None:
    current = _record()
    retired, successor = supersede(current, "Guncellenmis bilgi", memory_id="m2", now=NOW)
    assert retired.state is MemoryState.SUPERSEDED
    assert retired.superseded_by == "m2"
    assert retired.content == current.content, "eski icerik korunmali"
    assert successor.revision == current.revision + 1
    assert successor.content == "Guncellenmis bilgi"


def test_yalniz_aktif_kayit_superseded_olur() -> None:
    archived = _record(state=MemoryState.ARCHIVED)
    with pytest.raises(PolicyViolation):
        supersede(archived, "yeni", memory_id="m2", now=NOW)


def test_superseded_kayit_halefini_bildirmeli() -> None:
    with pytest.raises(ValidationFailed):
        _record(state=MemoryState.SUPERSEDED)


# -- T04: arama ve aciklama ---------------------------------------------------


def test_arama_secim_gerekcesi_tasir() -> None:
    record = _record(entities=("pgvector",))
    query = MemoryQuery(text="pgvector", key=_key(), entities=("pgvector",))
    hits = NativeMemoryEngine().search(
        query, records=[record], lexical_hits=frozenset({"m1"}), vector_ranks={"m1": 1}, now=NOW
    )
    assert len(hits) == 1
    reasons = " ".join(hits[0].reasons)
    assert "exact metin" in reasons
    assert "FTS" in reasons
    assert "vektor sirasi" in reasons
    assert "varlik eslesmesi" in reasons


def test_cross_project_sonuc_acik_izin_ister() -> None:
    other = _record(key=_key(project="baska-proje"))
    query = MemoryQuery(text="migration", key=_key(project="zekam"))
    assert NativeMemoryEngine().search(query, records=[other], now=NOW) == ()

    allowed = MemoryQuery(text="migration", key=_key(project="zekam"), allow_cross_project=True)
    assert len(NativeMemoryEngine().search(allowed, records=[other], now=NOW)) == 1


def test_agent_scratchpad_aramada_gorunmez() -> None:
    scratch = _record(key=_key(MemoryScope.AGENT), state=MemoryState.CANDIDATE)
    query = MemoryQuery(text="migration", key=_key())
    assert NativeMemoryEngine().search(query, records=[scratch], now=NOW) == ()


def test_baska_realm_gorunmez() -> None:
    other = _record(
        key=MemoryKey(scope=MemoryScope.PROJECT, realm_ref="baska", project_ref="zekam")
    )
    query = MemoryQuery(text="migration", key=_key())
    assert NativeMemoryEngine().search(query, records=[other], now=NOW) == ()


def test_temporal_sorgu_gecerliligi_uygular() -> None:
    expired = _record(
        valid_from=NOW - dt.timedelta(days=10), valid_until=NOW - dt.timedelta(days=1)
    )
    query = MemoryQuery(text="migration", key=_key(), at=NOW)
    assert NativeMemoryEngine().search(query, records=[expired], now=NOW) == ()


def test_ortak_core_uygunsuz_yuksek_sirali_adaylari_once_filtreler() -> None:
    allowed = _record(memory_id="allowed", content="izinli sonuc")
    foreign = tuple(
        _record(memory_id=f"foreign-{index}", key=_key(project="baska-proje"))
        for index in range(20)
    )
    vector_ranks = {
        **{record.memory_id: index for index, record in enumerate(foreign, start=1)},
        "allowed": 21,
    }
    query = MemoryQuery(text="vektor sorgusu", key=_key(), limit=1)

    hits, trace = NativeMemoryEngine().search_with_trace(
        query, records=(*foreign, allowed), vector_ranks=vector_ranks, now=NOW
    )

    assert [hit.record.memory_id for hit in hits] == ["allowed"]
    assert trace.source_type == "memory"
    assert trace.per_channel["dense"] == 1
    assert "ortak RRF" in " ".join(hits[0].reasons)


def test_work_item_bellegi_kardes_ise_sizmaz() -> None:
    sibling_key = MemoryKey(
        scope=MemoryScope.WORK_ITEM,
        realm_ref="varsayilan",
        project_ref="zekam",
        work_ref="W-2",
    )
    sibling = _record(key=sibling_key)
    query = MemoryQuery(text="Migration", key=_key(MemoryScope.WORK_ITEM))
    assert NativeMemoryEngine().search(query, records=[sibling], now=NOW) == ()


def test_memory_reranker_hatasi_ortak_fusion_sirasina_doner() -> None:
    def broken(query: str, hits: tuple[FusedHit, ...]) -> tuple[FusedHit, ...]:
        raise RuntimeError("reranker unavailable")

    record = _record(memory_id="m-rerank")
    query = MemoryQuery(text="Migration", key=_key())
    hits, trace = NativeMemoryEngine(reranker=broken).search_with_trace(
        query, records=[record], lexical_hits=frozenset({record.memory_id}), now=NOW
    )
    assert [hit.record.memory_id for hit in hits] == ["m-rerank"]
    assert trace.reranker_failed is True


def test_exact_memory_sonucu_reranker_ile_asagi_dusmez() -> None:
    exact = _record(memory_id="exact", content="SKYRSM-5661 task detayi")
    dense_only = _record(memory_id="dense", content="baska bir kayit")

    def reverse(query: str, hits: tuple[FusedHit, ...]) -> tuple[FusedHit, ...]:
        return tuple(reversed(hits))

    query = MemoryQuery(text="SKYRSM-5661", key=_key())
    hits, trace = NativeMemoryEngine(reranker=reverse).search_with_trace(
        query,
        records=[exact, dense_only],
        vector_ranks={"dense": 1, "exact": 2},
        now=NOW,
    )
    assert [hit.record.memory_id for hit in hits] == ["exact", "dense"]
    assert trace.reranker_used is True
    assert set(trace.as_dict()) == {
        "source_type",
        "identifiers",
        "per_channel",
        "fused_count",
        "after_dedupe",
        "reranker_used",
        "reranker_failed",
        "dropped_for_budget",
    }


# -- T05: hijyen --------------------------------------------------------------


def test_hijyen_duplicate_ve_conflict_bulur() -> None:
    first = _record(memory_id="m1", content="Retry stratejisi kullanilir")
    duplicate = _record(memory_id="m2", content="Retry stratejisi kullanilir")
    conflicting = _record(memory_id="m3", content="Retry stratejisi kullanilmaz")
    report = NativeMemoryEngine().hygiene([first, duplicate, conflicting], now=NOW)
    assert "m2" in report.of_kind(HygieneFinding.DUPLICATE)
    assert report.of_kind(HygieneFinding.CONFLICT)


def test_hijyen_otomatik_silmez() -> None:
    report = NativeMemoryEngine().hygiene([_record()], now=NOW)
    assert report.deleted == 0
    assert report.as_dict()["deleted"] == 0
    with pytest.raises(PolicyViolation):
        type(report)(findings=(), scanned=1, deleted=1)


def test_hijyen_stale_ve_kullanilmayan_kaydi_isaretler() -> None:
    stale = _record(
        memory_id="m1",
        valid_from=NOW - dt.timedelta(days=10),
        valid_until=NOW - dt.timedelta(days=1),
    )
    unused = _record(memory_id="m2", last_used_at=NOW - dt.timedelta(days=400))
    report = NativeMemoryEngine().hygiene([stale, unused], now=NOW)
    assert "m1" in report.of_kind(HygieneFinding.STALE)
    assert "m2" in report.of_kind(HygieneFinding.UNUSED)


def test_saklama_suresi_dolan_kayit_review_ister() -> None:
    engine = NativeMemoryEngine(retention=RetentionPolicy(days=30))
    old = _record(created_at=NOW - dt.timedelta(days=60))
    report = engine.hygiene([old], now=NOW)
    assert report.of_kind(HygieneFinding.RETENTION_REVIEW)


def test_kaynak_surumu_celiskisi_isaretlenir() -> None:
    citation = MemoryEvidence(kind="citation", reference="rev-1", digest_value=digest("c"))
    record = _record(evidence=(citation,))
    report = NativeMemoryEngine().hygiene([record], now=NOW, source_revision="rev-2")
    assert report.of_kind(HygieneFinding.SOURCE_VERSION_CONFLICT)


# -- T06: Mem0 adapteri -------------------------------------------------------


def test_native_kayit_otoritedir() -> None:
    record = _record()
    adapter = Mem0Adapter(engine_ref="mem0-oss", push=lambda item: digest("baska"))
    status = adapter.sync(record)
    assert status.state is SyncState.DRIFTED
    assert status.authority == "native"
    assert adapter.resolve(status, record) is record


def test_senkron_hatasi_gorunur_kalir() -> None:
    def broken(record: MemoryRecord) -> str:
        raise ConnectionError("mem0 erisilemedi")

    status = Mem0Adapter(engine_ref="mem0-oss", push=broken).sync(_record())
    assert status.state is SyncState.FAILED
    assert "senkron hatasi" in status.detail


def test_yapilandirilmamis_adapter_pending_dondurur() -> None:
    status = Mem0Adapter(engine_ref="mem0-oss").sync(_record())
    assert status.state is SyncState.PENDING


def test_esit_digest_synced_olur() -> None:
    record = _record()
    status = Mem0Adapter(engine_ref="mem0-oss", push=lambda item: item.record_digest).sync(record)
    assert status.state is SyncState.SYNCED
    assert status.external_digest == record.record_digest


def test_aktif_olmayan_kayit_senkronlanmaz() -> None:
    status = Mem0Adapter(engine_ref="mem0-oss").sync(_record(state=MemoryState.ARCHIVED))
    assert status.state is SyncState.NOT_SYNCED


def test_baska_kayda_ait_durum_reddedilir() -> None:
    adapter = Mem0Adapter(engine_ref="mem0-oss", push=lambda item: item.record_digest)
    status = adapter.sync(_record(memory_id="m1"))
    with pytest.raises(ValidationFailed):
        adapter.resolve(status, _record(memory_id="m2", content="baska icerik"))


def test_gozlem_digesti_kararlidir() -> None:
    first = observation_digest("  Ayni Gozlem  ", author_ref="a")
    second = observation_digest("ayni gozlem", author_ref="a")
    assert first == second
    assert first != observation_digest("ayni gozlem", author_ref="b")
