"""One-hundred-case bounded Akilli Kasa RAG golden registry."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AkilliKasaGoldenCase:
    case_id: str
    category: str
    query: str
    expected_source: str | None
    project_scope: str = "akilli-kasa"
    stale: bool = False


ADR6 = "belgeler/kararlar/ADR-0006-idempotent-dosya-ice-aktarma.md"
ADR5 = "belgeler/kararlar/ADR-0005-parasal-tutarlarda-decimal-kullanimi.md"
ADR10 = "belgeler/kararlar/ADR-0010-frontend-backend-api-siniri.md"
HEALTH = "src/akilli_kasa/api/saglik.py"
HEALTH_TEST = "tests/entegrasyon/test_saglik.py"
RULE_TEST = "tests/birim/test_kurallar.py"


def _cases(
    category: str,
    values: tuple[tuple[str, str | None], ...],
    *,
    project_scope: str = "akilli-kasa",
    stale: bool = False,
) -> tuple[AkilliKasaGoldenCase, ...]:
    return tuple(
        AkilliKasaGoldenCase(
            case_id=f"{category}-{index:02d}",
            category=category,
            query=query,
            expected_source=source,
            project_scope=project_scope,
            stale=stale,
        )
        for index, (query, source) in enumerate(values, start=1)
    )


EXACT = _cases(
    "exact-identifier",
    (
        (ADR6, ADR6),
        ('"Idempotent dosya ice aktarma"', ADR6),
        ("ADR-0005", ADR5),
        ("4217", ADR5),
        ("ADR-0010", ADR10),
        ("SaglikYaniti", HEALTH),
        ("response_model", HEALTH),
        ("ayarlari_al", HEALTH),
        ("test_uygulama_import_edilir", HEALTH_TEST),
        ("test_saglik_endpointi", HEALTH_TEST),
        ("test_oncelik_stop_ve_audit", RULE_TEST),
        ("KuralHedefi", RULE_TEST),
        ("kurallari_uygula", RULE_TEST),
        ("stop_processing", RULE_TEST),
        ("kaynak_turu", RULE_TEST),
        ("raporlama_disi", RULE_TEST),
        ("kategori_id", RULE_TEST),
        ("kural_id", RULE_TEST),
        ("uygulama_adi", HEALTH),
        ("status_code", HEALTH_TEST),
    ),
)

TURKISH_SEMANTIC = _cases(
    "turkish-semantic",
    (
        ("Ayni dosya ikinci kez gelirse veri bozulmasi nasil onlenir?", ADR6),
        ("Dosya tekrarlarini belirlemek icin hangi ozet kullanilir?", ADR6),
        ("Ice aktarma kayitlari neden tek islemde yazilir?", ADR6),
        ("Yeniden isleme hangi durumda acik olabilir?", ADR6),
        ("Yalniz dosya adina guvenmek neden reddedildi?", ADR6),
        ("Finansal hesaplarda kayan nokta neden kabul edilmez?", ADR5),
        ("Para tutarlari Python tarafinda hangi tiptedir?", ADR5),
        ("Veritabaninda parasal deger icin hangi tip secildi?", ADR5),
        ("Para birimi kodlari hangi standarda uyar?", ADR5),
        ("API sinirinda tutarlar nasil ele alinir?", ADR5),
        ("Is mantigi arayuzde mi sunucuda mi kalir?", ADR10),
        ("Kullanici arayuzu backend ile hangi protokol uzerinden konusur?", ADR10),
        ("ORM nesneleri neden dogrudan cevap olarak donmez?", ADR10),
        ("Arayuz surumlenirken domain hesaplari nerede tutulur?", ADR10),
        ("Saglik cevabi hangi uc bilgiyi tasir?", HEALTH),
        ("Saglik endpointi hangi uygulama adini dondurur?", HEALTH_TEST),
        ("Saglik rotasinin basarili HTTP kodu nedir?", HEALTH_TEST),
        ("Stop processing olan kural sonraki kategori kuralini nasil etkiler?", RULE_TEST),
        ("Kural audit kaydi hangi kural kimligini bekler?", RULE_TEST),
        ("Whatsapp kaynak turu raporlama disi sonucunu nasil dogurur?", RULE_TEST),
    ),
)

ENGLISH_TECHNICAL = _cases(
    "english-technical",
    (
        ("How are duplicate file imports prevented?", ADR6),
        ("Why must import records be written atomically?", ADR6),
        ("What digest identifies repeated source files?", ADR6),
        ("Why is a filename alone insufficient for idempotency?", ADR6),
        ("Which numeric types preserve exact monetary arithmetic?", ADR5),
        ("Why is binary floating point rejected for money?", ADR5),
        ("Which currency code standard is required?", ADR5),
        ("Where are API monetary amounts validated?", ADR5),
        ("Where should domain calculations remain?", ADR10),
        ("What versioned interface does the UI consume?", ADR10),
        ("Why are ORM objects not returned directly?", ADR10),
        ("Which schema layer shapes API responses?", ADR10),
        ("What fields are present in the health response?", HEALTH),
        ("Which route is exercised by the health integration test?", HEALTH_TEST),
        ("How does stop processing affect the later category rule?", RULE_TEST),
    ),
)

CODE_OBJECT = _cases(
    "code-object",
    (
        ("SaglikYaniti.durum", HEALTH),
        ("SaglikYaniti.uygulama", HEALTH),
        ("SaglikYaniti.surum", HEALTH),
        ("router.get response_model", HEALTH),
        ("def saglik ayarlari_al", HEALTH),
        ("TestClient app title Akilli Kasa", HEALTH_TEST),
        ("GET /api/v1/saglik", HEALTH_TEST),
        ("yanit.json durum saglikli", HEALTH_TEST),
        ("KuralHedefi Ornek Market", RULE_TEST),
        ("oncelik 20 kategori_ata", RULE_TEST),
        ("oncelik 10 raporlama_disi", RULE_TEST),
        ("stop_processing True", RULE_TEST),
        ("kosullar aciklama_icerir", RULE_TEST),
        ("eylemler kategori_ata", RULE_TEST),
        ("audit kural_id equals 1", RULE_TEST),
    ),
)

SCOPE = _cases(
    "project-scope",
    tuple(
        (query, None)
        for query in (
            "ADR-0006",
            "SaglikYaniti",
            "test_saglik_endpointi",
            "KuralHedefi",
            "Decimal Numeric",
            "idempotent dosya ice aktarma",
            "frontend backend API siniri",
            "stop_processing",
            "ISO 4217",
            "SHA-256 tekrar engelleme",
        )
    ),
    project_scope="other-project",
)

STALE = _cases(
    "stale-superseded",
    tuple(
        (query, None)
        for query in (
            "ADR-0006 tekrar engelleme",
            "ADR-0005 Decimal",
            "ADR-0010 REST API",
            "SaglikYaniti",
            "test_saglik_endpointi",
            "KuralHedefi",
            "kurallari_uygula",
            "stop_processing",
            "SHA-256",
            "Pydantic semalari",
        )
    ),
    stale=True,
)

NO_ANSWER = _cases(
    "no-answer-adversarial",
    tuple(
        (query, None)
        for query in (
            "Kuantum kasa anahtar dagitimi nasil yapiliyor?",
            "Mars serasinda hangi sulama protokolu kullaniliyor?",
            "Kullanici banka sifreleri hangi algoritmayla saklaniyor?",
            "Belgeler blockchaine hangi ag uzerinden yukleniyor?",
            "Bitcoin fiyat tahmini hangi modelle hesaplaniyor?",
            "Mobil uygulama magazasi hesabi kime ait?",
            "Calisanlarin tibbi kayitlari nasil siniflandiriliyor?",
            "Uretim banka bakiyesinin bugunku kesin degeri nedir?",
            "Parola degerini donduren gizli sinif hangisidir?",
            "Musteri tablosunu silen SQL komutu nerede onaylandi?",
        )
    ),
)

GOLDEN_CASES = (
    EXACT + TURKISH_SEMANTIC + ENGLISH_TECHNICAL + CODE_OBJECT + SCOPE + STALE + NO_ANSWER
)

if len(GOLDEN_CASES) != 100 or len({case.case_id for case in GOLDEN_CASES}) != 100:
    raise RuntimeError("WP-07 Akilli Kasa golden registry exact 100 unique case olmali")
