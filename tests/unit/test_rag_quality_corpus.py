"""ZEKAM-RAG-PERFORMANCE-CORRECTNESS-001: RAG kalite degerlendirme corpus'u.

Kapsam (AKTIF_GOREV.md §5 "Test verisi ve gerçekçilik" [K10]):

* Mevcut ``GoldenCase`` boş relevant set kabul etmiyor; olmayan nesne / yetersiz
  kanıt sorularını no-answer (negative) vaka olarak destekleyen uyumlu bir
  ek tür ve ayrı evaluator eklenir. ``GoldenCase`` ve ``evaluate`` geriye
  uyumlu kalir.
* En az 80 açık etiketli örnekten oluşan bir başlangıç corpus'u; tek nesne/exact,
  Türkçe/İngilizce semantic, çoklu kaynak/ilişki ve no-answer/çelişkili kanıt
  kategorilerinde en az 20'şer örnek. Tune/holdout ayrılır.

NOT: Bu corpus örnekleri SENTETİKTİR. Zekam bağlamına özgü veya gerçek proje koduna
ait yanıt anahtarı değildir; kategorileri deterministik şekilde kaplamak ve
evaluator sözleşmesini provider'sız ölçmek için türetilmiştir. Gerçek proje cevap
anahtarı ancak kaynak ve revision ile doğrulanabilir; bu corpus o iddiayı taşımaz.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from zekam.application.retrieval_service import (
    GoldenCase,
    NegativeCase,
    QualityEvaluation,
    QualityRunResult,
    evaluate,
    evaluate_quality,
)

# -- kategori sabitleri ------------------------------------------------------
CAT_EXACT = "single-object-exact"
CAT_SEMANTIC = "semantic-explanation"
CAT_RELATIONSHIP = "multi-source-relationship"
CAT_NEGATIVE = "no-answer-negative"
ALL_CATEGORIES = (CAT_EXACT, CAT_SEMANTIC, CAT_RELATIONSHIP, CAT_NEGATIVE)

# -- sentetik "corpus'ta var olan nesneler" : token -> chunk id --------------
# Yalnızca pozitif kaselerin referansları burada yer alır. Negatif kaseler bu
# token'lardan hiçbirini içermemelidir, böylece deterministik run "nesne yok"
# durumunu doğru şekilde abstain eder.
KNOWN = {
    "app.musteri": "chunk-app-musteri",
    "app.siparis": "chunk-app-siparis",
    "app.fatura": "chunk-app-fatura",
    "app.stok": "chunk-app-stok",
    "saglikyaniti": "chunk-saglikyaniti",
    "kurallari_uygula": "chunk-kurallari-uygula",
    "gpu_user.log_report_creation": "chunk-gpu-log-report-creation",
    "gpu_user.usr_kullanici": "chunk-gpu-usr-kullanici",
    "sky.is_emri": "chunk-sky-is-emri",
    "odi.rekabet_mapping": "chunk-odi-rekabet-mapping",
    "adr-0006": "chunk-adr-0006",
    "adr-0012": "chunk-adr-0012",
    "4711": "chunk-defekt-4711",
    "4722": "chunk-defekt-4722",
}


@dataclass(frozen=True)
class CorpusItem:
    """Tek corpus kaydi: sorgu + beklenen sonuç + kategori."""

    query: str
    category: str
    relevant: frozenset[str] = frozenset()
    negative: bool = False
    reason: str = ""


def _p(q: str, cat: str, *chunks: str) -> CorpusItem:
    return CorpusItem(query=q, category=cat, relevant=frozenset(chunks), negative=False)


def _n(q: str, reason: str = "") -> CorpusItem:
    return CorpusItem(query=q, category=CAT_NEGATIVE, negative=True, reason=reason)


# -- pozitif: tek nesne / exact (>=20) ---------------------------------------
_EXACT = [
    _p("app.musteri tablosu nerede", CAT_EXACT, "chunk-app-musteri"),
    _p("GPU_USER.LOG_REPORT_CREATION nesnesini bul", CAT_EXACT, "chunk-gpu-log-report-creation"),
    _p("saglikyaniti modulunu bul", CAT_EXACT, "chunk-saglikyaniti"),
    _p("kurallari_uygula fonksiyonunu bul", CAT_EXACT, "chunk-kurallari-uygula"),
    _p("ADR-0006 dosyasini ac", CAT_EXACT, "chunk-adr-0006"),
    _p("#4711 defektini bul", CAT_EXACT, "chunk-defekt-4711"),
    _p("app.siparis tablosunun yolunu bul", CAT_EXACT, "chunk-app-siparis"),
    _p("app.fatura nesnesini ara", CAT_EXACT, "chunk-app-fatura"),
    _p("app.stok tablosu nerede tanimli", CAT_EXACT, "chunk-app-stok"),
    _p("GPU_USER.USR_KULLANICI tablosunu getir", CAT_EXACT, "chunk-gpu-usr-kullanici"),
    _p("SKY.Is_Emri nesnesinin konumu", CAT_EXACT, "chunk-sky-is-emri"),
    _p("ODI.Rekabet_Mapping mapping dosyasini bul", CAT_EXACT, "chunk-odi-rekabet-mapping"),
    _p("ADR-0012 kararini ac", CAT_EXACT, "chunk-adr-0012"),
    _p("#4722 hata kaydinin dosyasini bul", CAT_EXACT, "chunk-defekt-4722"),
    _p("app.stok stok karti tablosu", CAT_EXACT, "chunk-app-stok"),
    _p("saglikyaniti http handler konumu", CAT_EXACT, "chunk-saglikyaniti"),
    _p("kurallari_uygula policy kurali", CAT_EXACT, "chunk-kurallari-uygula"),
    _p("GPU_USER.LOG_REPORT_CREATION DDL'si", CAT_EXACT, "chunk-gpu-log-report-creation"),
    _p("app.musteri modeli", CAT_EXACT, "chunk-app-musteri"),
    _p("ADR-0006 md kaynak dosyasi", CAT_EXACT, "chunk-adr-0006"),
    _p("SKY.Is_Emri state makinesi", CAT_EXACT, "chunk-sky-is-emri"),
]

# -- pozitif: semantic / açıklama, Türkçe + İngilizce (>=20) -----------------
_SEMANTIC = [
    _p("app.musteri neden toplu insert kullanir", CAT_SEMANTIC, "chunk-app-musteri"),
    _p("why does app.siparis use lazy loading", CAT_SEMANTIC, "chunk-app-siparis"),
    _p("GPU_USER.LOG_REPORT_CREATION nasil calisir", CAT_SEMANTIC, "chunk-gpu-log-report-creation"),
    _p("how does saglikyaniti validate requests", CAT_SEMANTIC, "chunk-saglikyaniti"),
    _p("app.fatura ne ise yarar acikla", CAT_SEMANTIC, "chunk-app-fatura"),
    _p("what is the purpose of kurallari_uygula", CAT_SEMANTIC, "chunk-kurallari-uygula"),
    _p("ADR-0006 neden idempotent dosya kullanir", CAT_SEMANTIC, "chunk-adr-0006"),
    _p("explain how app.stok refreshes inventory", CAT_SEMANTIC, "chunk-app-stok"),
    _p("SKY.Is_Emri state makinesinin mantigi nedir", CAT_SEMANTIC, "chunk-sky-is-emri"),
    _p("why does GPU_USER.USR_KULLANICI hash its columns", CAT_SEMANTIC, "chunk-gpu-usr-kullanici"),
    _p("ODI.Rekabet_Mapping akisi nasil isler", CAT_SEMANTIC, "chunk-odi-rekabet-mapping"),
    _p("app.siparis hangi durumlarda iptal olur anlat", CAT_SEMANTIC, "chunk-app-siparis"),
    _p("what does ADR-0012 decide and why", CAT_SEMANTIC, "chunk-adr-0012"),
    _p("kurallari_uygula kural sirasini acikla", CAT_SEMANTIC, "chunk-kurallari-uygula"),
    _p("explain the retry policy of app.fatura", CAT_SEMANTIC, "chunk-app-fatura"),
    _p("saglikyaniti fallback davranisi nedir", CAT_SEMANTIC, "chunk-saglikyaniti"),
    _p(
        "GPU_USER.LOG_REPORT_CREATION rapor birlesme sebebi",
        CAT_SEMANTIC,
        "chunk-gpu-log-report-creation",
    ),
    _p("how is #4711 fixed in app.musteri", CAT_SEMANTIC, "chunk-defekt-4711", "chunk-app-musteri"),
    _p("what is the semantic of SKY.Is_Emri priority", CAT_SEMANTIC, "chunk-sky-is-emri"),
    _p("why kod app.stok uses checksum", CAT_SEMANTIC, "chunk-app-stok"),
    _p("explain ADR-0006 in detail", CAT_SEMANTIC, "chunk-adr-0006"),
]

# -- pozitif: çoklu kaynak / ilişki (>=20) ------------------------------------
_RELATIONSHIP = [
    _p(
        "app.musteri ve app.siparis arasindaki iliski",
        CAT_RELATIONSHIP,
        "chunk-app-musteri",
        "chunk-app-siparis",
    ),
    _p(
        "does app.fatura depend on app.siparis",
        CAT_RELATIONSHIP,
        "chunk-app-fatura",
        "chunk-app-siparis",
    ),
    _p(
        "saglikyaniti kurallari_uygula cagirir mi",
        CAT_RELATIONSHIP,
        "chunk-saglikyaniti",
        "chunk-kurallari-uygula",
    ),
    _p(
        "GPU_USER.LOG_REPORT_CREATION ve GPU_USER.USR_KULLANICI iliskisi",
        CAT_RELATIONSHIP,
        "chunk-gpu-log-report-creation",
        "chunk-gpu-usr-kullanici",
    ),
    _p(
        "app.musteri ile app.stok arasindaki bagimlilik",
        CAT_RELATIONSHIP,
        "chunk-app-musteri",
        "chunk-app-stok",
    ),
    _p(
        "does SKY.Is_Emri reference app.siparis",
        CAT_RELATIONSHIP,
        "chunk-sky-is-emri",
        "chunk-app-siparis",
    ),
    _p(
        "app.fatura app.musteri kullanir mi",
        CAT_RELATIONSHIP,
        "chunk-app-fatura",
        "chunk-app-musteri",
    ),
    _p(
        "kurallari_uygula ve saglikyaniti call chain",
        CAT_RELATIONSHIP,
        "chunk-kurallari-uygula",
        "chunk-saglikyaniti",
    ),
    _p(
        "ODI.Rekabet_Mapping GPU_USER.LOG_REPORT_CREATION hangi fonksiyonu cagirir",
        CAT_RELATIONSHIP,
        "chunk-odi-rekabet-mapping",
        "chunk-gpu-log-report-creation",
    ),
    _p(
        "ADR-0012 ile ADR-0006 karar iliskisi",
        CAT_RELATIONSHIP,
        "chunk-adr-0012",
        "chunk-adr-0006",
    ),
    _p(
        "app.siparis ve app.stok arasindaki fark",
        CAT_RELATIONSHIP,
        "chunk-app-siparis",
        "chunk-app-stok",
    ),
    _p(
        "SKY.Is_Emri ile app.fatura baglantisi",
        CAT_RELATIONSHIP,
        "chunk-sky-is-emri",
        "chunk-app-fatura",
    ),
    _p(
        "#4711 ve #4722 ayni koku nedenini paylasiyor mu",
        CAT_RELATIONSHIP,
        "chunk-defekt-4711",
        "chunk-defekt-4722",
    ),
    _p(
        "does saglikyaniti call kurallari_uygula before validation",
        CAT_RELATIONSHIP,
        "chunk-saglikyaniti",
        "chunk-kurallari-uygula",
    ),
    _p(
        "app.musteri, app.siparis, app.fatura zinciri",
        CAT_RELATIONSHIP,
        "chunk-app-musteri",
        "chunk-app-siparis",
        "chunk-app-fatura",
    ),
    _p(
        "GPU_USER.USR_KULLANICI SKY.Is_Emri'ne nasil baglanir",
        CAT_RELATIONSHIP,
        "chunk-gpu-usr-kullanici",
        "chunk-sky-is-emri",
    ),
    _p(
        "relationship between app.stok and app.fatura",
        CAT_RELATIONSHIP,
        "chunk-app-stok",
        "chunk-app-fatura",
    ),
    _p(
        "ODI.Rekabet_Mapping ve SKY.Is_Emri ortak tablo kullanir mi",
        CAT_RELATIONSHIP,
        "chunk-odi-rekabet-mapping",
        "chunk-sky-is-emri",
    ),
    _p("ADR-0006 hangi servisleri kullanan", CAT_RELATIONSHIP, "chunk-adr-0006"),
    _p(
        "app.siparis'ten app.stok'a dependency",
        CAT_RELATIONSHIP,
        "chunk-app-siparis",
        "chunk-app-stok",
    ),
    _p(
        "does kurallari_uygula depend on saglikyaniti",
        CAT_RELATIONSHIP,
        "chunk-kurallari-uygula",
        "chunk-saglikyaniti",
    ),
]

# -- negatif: no-answer / olmayan nesne / çelişkili kanıt (>=20) --------------
# Bu sorgular korpusta bulunmayan nesneleri/raporları sorar; doğru sonuç abstain'dir.
_NEGATIVE = [
    _n("oracle kamu-uretim-2019 rapor dosyasi nerede", "nesne korpusta yok"),
    _n("petkim_karne modulu var mi", "nesne korpusta yok"),
    _n("varlik_yonetim_ekhat tablosu nerede", "nesne korpusta yok"),
    _n("odt_eski_personel_karti nesnesini bul", "nesne korpusta yok"),
    _n("kuyruk_is_izleme sureci hangi tabloyu kullanir", "nesne korpusta yok"),
    _n("cehre_detay_2018 raporu hangi klasorde", "nesne korpusta yok"),
    _n("muhasebe_alim_2017 plan dosyasi nerede", "nesne korpusta yok"),
    _n("why is GPU_LOG_ITM_2019 empty", "nesne korpusta yok"),
    _n("oda_kasa_2016 bakiyesi nerede hesaplaniyor", "nesne korpusta yok"),
    _n("IMAGE_LKM_2015 tablosunu bul", "nesne korpusta yok"),
    _n("ADT kutuphanesi hem synchronous hem async mi", "çelişkili/yetersiz kanıt"),
    _n("bgt_kontur_2014 kaynak dosyasini getir", "nesne korpusta yok"),
    _n("satis_plan_2013 nesnesi var mi", "nesne korpusta yok"),
    _n("does ODE mapper 2012 exist in this project", "nesne korpusta yok"),
    _n("urun_fiyat_2011 raporu hangi sunucuda", "nesne korpusta yok"),
    _n("kontrat_bekiyor_2010 kaydi nerede", "nesne korpusta yok"),
    _n("PHP legacy 2009 modulu referansi", "nesne korpusta yok"),
    _n("bicim_donusum_2008 tablosu", "nesne korpusta yok"),
    _n("paket_ice_aktar_2007 hata kodu nerede", "nesne korpusta yok"),
    _n("eski_odeme_2006 duzeltmesi hangi branch", "nesne korpusta yok"),
    _n("santral_sayac_2005 raporu var mi", "nesne korpusta yok"),
]

#: Tam corpus; kategoriler deterministik olarak iç içe geçirilir, böylece
#: tune/holdout ayrımı (index % 4) her dört kategoriden de örnek düşürür.
CORPUS: tuple[CorpusItem, ...] = tuple(
    item
    for bucket in zip(_EXACT, _SEMANTIC, _RELATIONSHIP, _NEGATIVE, strict=True)
    for item in bucket
)


def split_tune_holdout(
    corpus: tuple[CorpusItem, ...],
) -> tuple[tuple[CorpusItem, ...], tuple[CorpusItem, ...]]:
    """Tune/holdout ayrımı: her kategorinin son 5 örneği holdout'a gider.

    Her kategoride 21 örnek olduğundan holdout 4×5=20, tune 4×16=64 örnek
    taşır; her iki küme de dört kategoriyi de kapsar (CORPUS-2).
    """
    per_category: dict[str, list[CorpusItem]] = {cat: [] for cat in ALL_CATEGORIES}
    for item in corpus:
        per_category[item.category].append(item)
    tune: list[CorpusItem] = []
    holdout: list[CorpusItem] = []
    for cat in ALL_CATEGORIES:
        items = per_category[cat]
        holdout.extend(items[-5:])
        tune.extend(items[:-5])
    return tuple(tune), tuple(holdout)


# -- deterministik, provider'sız run -----------------------------------------
def make_run(known: dict[str, str] | None = None) -> Callable[..., QualityRunResult]:
    """Provider'sız deterministik run: tanınan nesne var ise answered, yoksa abstain.

    Bir sorgu korpusta bulunan bir (veya daha çok) nesne token'ı içeriyorsa
    answered=True döner ve o chunk'ları sıralar. Hiçbir tanınan nesne yoksa
    (no-answer/varlık-yok senaryosu) answered=False döner => doğru abstain.
    """

    known = dict(KNOWN if known is None else known)

    def run(query: str) -> QualityRunResult:
        lowered = query.casefold()
        matched: list[str] = []
        for token, chunk_id in known.items():
            if token in lowered and chunk_id not in matched:
                matched.append(chunk_id)
        if matched:
            return QualityRunResult(
                ranked_ids=tuple(matched), answered=True, citations=tuple(matched)
            )
        return QualityRunResult(ranked_ids=(), answered=False, citations=())

    return run


def make_tuple_run() -> Callable[..., tuple[str, ...]]:
    """Yalnızca sıralama dönen, eski ``evaluate`` sözleşmesine uygun run.

    Pozitif kaseyi doğrudan ``evaluate`` ile sınamak için; negatif bilgi taşımaz.
    """

    def run(query: str) -> tuple[str, ...]:
        lowered = query.casefold()
        matched: list[str] = []
        for token, chunk_id in KNOWN.items():
            if token in lowered and chunk_id not in matched:
                matched.append(chunk_id)
        return tuple(matched)

    return run


def make_fabricating_run() -> Callable[..., QualityRunResult]:
    """Her sorguya (negatifler dahil) desteksiz answered dönen kötü sistem."""

    def run(query: str) -> QualityRunResult:
        del query
        return QualityRunResult(
            ranked_ids=("chunk-guvenli-1",), answered=True, citations=("chunk-guvenli-1",)
        )

    return run


def make_always_abstain_run() -> Callable[..., QualityRunResult]:
    """Hiçbir soruya cevap vermeyen (her zaman abstain) sistem."""

    def run(query: str) -> QualityRunResult:
        del query
        return QualityRunResult(ranked_ids=(), answered=False, citations=())

    return run


def items_to_cases(items: tuple[CorpusItem, ...]) -> tuple[GoldenCase, ...]:
    return tuple(GoldenCase(query=item.query, relevant_ids=item.relevant) for item in items)


def items_to_negatives(items: tuple[CorpusItem, ...]) -> tuple[NegativeCase, ...]:
    return tuple(
        NegativeCase(query=item.query, reason=item.reason)
        for item in items
        if item.negative
    )


# -- NEG-1: negative vaka temsil edilebilir ve sessizce atılmaz ----------------
def test_neg1_negative_case_representable_and_measured() -> None:
    """NEG-1: no-answer vaka ``NegativeCase`` ile temsil edilir, pozitif analogu
    ``GoldenCase``'in boş-set reddinden farklıdır, ve skorlamada kaybolmaz."""
    # Empty-set GoldenCase HÂLÂ reddedilir (backward compatible, NEG-3).
    empty_rejected = False
    try:
        GoldenCase(query="q", relevant_ids=frozenset())
    except Exception:
        empty_rejected = True
    assert empty_rejected is True

    # NegativeCase ise boş relevant set anlamlıdır; reddedilmez.
    negative = NegativeCase(query="oracle kamu-uretim-2019 rapor dosyasi nerede", reason="yok")
    assert negative.query
    # Boş sorgu yine reddedilir (açık invariant korunur).
    rejected = False
    try:
        NegativeCase(query="  ")
    except Exception:
        rejected = True
    assert rejected is True

    positives = items_to_cases((_EXACT[0], _SEMANTIC[0]))
    negatives = items_to_negatives(tuple(_NEGATIVE[:2]))
    result = evaluate_quality(positives, negatives, run=make_run(), k=10)
    assert isinstance(result, QualityEvaluation)
    # Negatif sayısı korunur => atılmadılar.
    assert result.negative_count == 2
    # İkisi de doğru abstain edilmiş => negatif precision 1.0.
    assert result.negative_precision == 1.0
    # Hem pozitif hem negatif sayılar raporlanır.
    blob = result.as_dict()
    assert blob["recall_at_k"] == 1.0
    assert blob["negative_precision"] == 1.0
    assert blob["negative_count"] == 2


# -- NEG-2: no-answer sorusuna desteksiz cevap cezalandırılır ------------------
def test_neg2_unsupported_answer_on_no_answer_is_penalized() -> None:
    """NEG-2: korpusta olmayan nesne / yetersiz kanıt sorgusuna sistem yalnızca
    abstain ederse doğru; answered/üretilmiş cevap dönerse negatif precision düşer."""
    negatives = items_to_negatives(tuple(_NEGATIVE[:10]))

    # Doğru sistem: abstain eder => negative_precision == 1.0.
    good = evaluate_quality((), negatives, run=make_run(), k=10)
    assert good.negative_precision == 1.0
    assert good.negative_false_positive == 0

    # Kötü sistem: desteksiz answered döner => her negatif yanlış pozitif.
    fabricating = evaluate_quality((), negatives, run=make_fabricating_run(), k=10)
    assert fabricating.negative_precision == 0.0
    assert fabricating.negative_false_positive == 10

    # Kalite skoru sıfırlanır: negatif tarafta sıfır => oynanamaz.
    assert fabricating.quality_score == 0.0

    # "Hep abstain" stratejisi de oynanamaz: pozitif MRR zorunludur.
    positives = items_to_cases(tuple(_EXACT[:5]))
    abstain_all = evaluate_quality(positives, negatives, run=make_always_abstain_run(), k=10)
    # Negatif precision tamam (hep abstain) ama pozitifler hiç bulunamadı => MRR 0.
    assert abstain_all.negative_precision == 1.0
    assert abstain_all.positive.mrr == 0.0
    assert abstain_all.quality_score == 0.0


# -- NEG-3: mevcut GoldenCase/evaluate/tests geriye uyumlu ---------------------
def test_neg3_backward_compatibility() -> None:
    """NEG-3: ``GoldenCase`` boş-set reddi ve ``evaluate`` sözleşmesi değişmez;
    yeni türler mevcut tüketicileri bozmaz."""
    # GoldenCase boş-set reddi korunur (test_bos_golden_kume_reddedilir).
    rejected = False
    try:
        GoldenCase(query="q", relevant_ids=frozenset())
    except Exception:
        rejected = True
    assert rejected is True

    # ``evaluate`` eski imzasıyla (tuple run) çalışmaya devam eder.
    established = items_to_cases(tuple(_EXACT[:10]))
    result = evaluate(established, run=make_tuple_run(), k=10)
    assert result.recall_at_k == 1.0
    assert result.case_count == 10

    # ``evaluate_quality`` pozitif-only çağrısı mevcut ``evaluate`` sayılarıyla eşleşir.
    quality = evaluate_quality(established, (), run=make_run(), k=10)
    assert quality.positive.recall_at_k == pytest.approx(result.recall_at_k)
    assert quality.positive.mrr == pytest.approx(result.mrr)
    assert quality.positive.ndcg_at_k == pytest.approx(result.ndcg_at_k)
    # Boş politika: ne positive ne negative yoksa hata fırlar.
    rejected = False
    try:
        evaluate_quality((), (), run=make_run(), k=10)
    except Exception:
        rejected = True
    assert rejected is True


# -- CORPUS-1: corpus deterministik olarak yüklenir ve değerlendirilir ---------
def test_corpus1_loads_and_evaluates_deterministically() -> None:
    """CORPUS-1: 80+ corpus provider'sız, deterministik değerlendirilir; hem
    pozitif hem negatif kalite sayıları raporlanır."""
    assert len(CORPUS) >= 80
    positives = items_to_cases(tuple(item for item in CORPUS if not item.negative))
    negatives = items_to_negatives(CORPUS)

    run = make_run()
    first = evaluate_quality(positives, negatives, run=run, k=10)
    second = evaluate_quality(positives, negatives, run=run, k=10)
    # Determinizm: iki kez aynı sorgu & run => birebir aynı sonuç.
    assert first.as_dict() == second.as_dict()

    blob = first.as_dict()
    # Pozitif sayılar raporlanır.
    assert "recall_at_k" in blob and "mrr" in blob and "ndcg_at_k" in blob
    # Negatif sayılar raporlanır (≥20).
    assert first.negative_count >= 20
    assert blob["negative_count"] >= 20
    assert 0.0 <= first.quality_score <= 1.0
    # Pozitiflerin tamamı bulundu (iyi sistem) => recall/mrr 1.0.
    assert first.positive.recall_at_k == 1.0
    assert first.negative_precision == 1.0
    assert first.quality_score == pytest.approx(1.0)


# -- CORPUS-2: tune/holdout ayrık ve dört kategoriyi kapsar --------------------
def test_corpus2_tune_holdout_disjoint_and_full_coverage() -> None:
    """CORPUS-2: tune/holdout kümeleri sorgu bazında ayrık ve dört kategori de
    her iki kümede de kapsanır."""
    assert len(CORPUS) >= 80

    tune_items, holdout_items = split_tune_holdout(CORPUS)
    tune_set = frozenset(item.query for item in tune_items)
    holdout_set = frozenset(item.query for item in holdout_items)

    # Ayrıklık.
    assert tune_set.isdisjoint(holdout_set)

    # Dört kategori corpus'ta en az 20'şer.
    for category in ALL_CATEGORIES:
        count = sum(1 for item in CORPUS if item.category == category)
        assert count >= 20, f"{category} en az 20 olmali, {count} var"

    # Holdout dört kategoriyi de kapsar.
    holdout_categories = {item.category for item in holdout_items}
    assert holdout_categories >= set(ALL_CATEGORIES), (
        f"holdout kategori kaplamis: {sorted(holdout_categories)}"
    )

    # Tune de dört kategoriyi de kapsar.
    tune_categories = {item.category for item in tune_items}
    assert tune_categories >= set(ALL_CATEGORIES)
    assert len(tune_set) >= 60
    assert len(holdout_set) >= 20
