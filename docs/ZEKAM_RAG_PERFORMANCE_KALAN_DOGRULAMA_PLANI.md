# ZEKAM-RAG-PERFORMANCE-CORRECTNESS-001 — Kalan Doğrulama Planı

- **Görev:** Zekam RAG Gecikmesi, Kanıt Kalitesi ve Uçtan Uca Yanıt Hattının Düzeltilmesi
- **Authority:** `AKTIF_GOREV.md` (ZEKAM-RAG-PERFORMANCE-CORRECTNESS-001)
- **Bu planın amacı:** Görevin 7. bölümündeki kapanış kriterlerinden henüz işaretlenmemiş üç doğrulama/ölçüm kapısını kapatmak veya neden kapatılamadığını kanıtlı raporlamak.
- **Bağlı kaynak revision:** `7af3854c03c29b7b7e50cf2ac903840020150e29` (görev commit'i, `origin/main` ile eşit)

## Henüz açık olan kapanış kriterleri

| Kriter | AKTIF_GOREV.md referansı | Durum başlangıçta | Çözüm yolu |
|---|---|---|---|
| 50.000 chunk ölçeği | §5 "1.000, 10.000, yaklaşık 20.000 ve 50.000 chunk" | `NOT_EXECUTED` | WP9 — yerel provider-free benchmark |
| Gerçek proje semantic answer-key | §5 "Gerçek proje cevap anahtarını kaynak ve revision ile doğrula" | `NOT_EXECUTED` | WP10 — Zekam kaynağından AST/regex ile gerçek identifier + source konumlu corpus |
| Canlı provider qualification/embedding ölçümü | §5 "Gerçek semantic kalite için onaylı/cached gerçek embedding corpus'u veya izinli gerçek provider koşusu" | `NOT_EXECUTED` | WP11 — test altyapısı + açık uzak provider izniyle çalıştır |

## WP9 — 50.000 chunk scaling benchmark

### Hedef
`SQLiteKnowledgeIndex` üzerinde gerçek 50.000 chunk ile provider-free scaling ölçümü yapmak; generation üst sınırının (`MAX_RECORDS_PER_GENERATION == 50_000`) güvenle ulaşılabildiğini, counter contract'ın korunduğunu ve determinizmin bozulmadığını kanıtlamak.

### Sınır ve varsayım
- 250.000 iddiası yok; 50k mevcut üst sınır.
- Build süresi referans makinede 20k için ~31 s olduğundan 50k için ~75-90 s beklenir.
- Test, zaman bütçesini aşan yavaş ortamlarda `ZEKAM_SCALING_50K=0` ile atlanabilir; "fake size" üretilmez.
- Latency ölçümleri yalnızca raporlanır, bu makinede tekrarlanabilir p95 iddiası yapılmaz.

### Çıktılar
- `tests/unit/test_rag_scaling_benchmark.py` içinde yeni `test_scale_50k_reports_when_built`.
- `docs/ZEKAM_RAG_PERFORMANCE_BENCHMARK.json` içinde `sizes_built` listesi güncellenir.

## WP10 — Gerçek proje semantic answer-key evaluation

### Hedef
Sentetik corpus'un yerine, **Zekam kaynağının kendi dosyalarından** çıkarılmış gerçek teknik identifier'ları (fonksiyon/sınıf/decorator/değişken) ve bunların gerçek source path + line range bilgisini kullanan bir evaluation corpus'u oluşturmak. Bu corpus:

- source tree'deki gerçek nesneleri sorar,
- beklenen cevabı source file path + line range + symbol name olarak taşır,
- retrieval-only çalışır; üretilmiş cevap yok,
- `source_revision` (Git HEAD) ile doğrulanabilir.

### Yöntem
1. `git rev-parse HEAD` ile kanonik source revision al.
2. `src/zekam/application/*.py` dosyalarını AST ile tarayıp tanımlanan top-level function/class/decorator isimlerini çıkar.
3. Her symbol için `relative_path`, `line_start`, `line_end`, `symbol_name` kaydet.
4. Seçilmiş ~30 symbol üzerinden doğal dil + exact sorgular üret (örn. "`project_rag_runtime._query` ne yapar?").
5. Retrieval çalıştır; dönen citation'ların `relative_path` ve `line_range` beklenen source konumla kesişiyorsa başarılı say.
6. No-answer/yanlış scope sızıntısı durumlarını da raporla.

### Çıktılar
- Yeni test: `tests/unit/test_rag_real_project_answer_keys.py`.
- `docs/ZEKAM_RAG_PERFORMANCE_BENCHMARK.json` içinde `real_project_keys` bölümü.

## WP11 — Canlı provider latency/correctness ölçümü

### Hedef
Gerçek uzak provider (OpenCode/litellm) üzerinden qualification probe ve query embedding çağrılarının latency'sini ve call-count'unu ölçmek. Bu ölçüm:

- açık uzak provider authorization gerektirir,
- maliyet/token bütçesi sınırlı tutulur (~5-10 farklı query),
- effect/receipt sınırından geçer,
- `workspace-write-no-network` permission profile'ını ihlal etmemek için açık onaylı çalıştırılır.

### Sınır
- AKTIF_GOREV.md §2: "Gerçek provider çağrısı hâlâ mevcut effect ve receipt sınırından geçecek."
- Mevcut `zekam doctor` permission profile `workspace-write-no-network`; canlı ölçüm ağ kullanımı içerir. Bu nedenle test `pytest.mark.skipif(not ZEKAM_LIVE_PROVIDER_MEASURE)` ile varsayılan atlanır.
- Kullanıcı açık "canlı provider ölçümünü çalıştır" talimatı verirse ve maliyet/onay açıkça kabul edilirse çalıştırılır; aksi halde `NOT_EXECUTED` olarak işaretlenir.

### Çıktılar
- Yeni test: `tests/integration/test_live_provider_rag_latency.py`.
- `docs/ZEKAM_RAG_PERFORMANCE_BENCHMARK.json` içinde `live_provider` bölümü (`executed: true/false`).

## Sıra ve bağımlılıklar

```text
WP9  (yerel, 50k)        -- bağımsız
WP10 (yerel, answer-key) -- bağımsız
WP11 (uzak provider)     -- WP9/WP10'dan bağımsız; yalnızca açık yetkiyle çalışır
```

WP9 ve WP10 paralel olarak başlatılabilir. WP11 için önce izin kararı alınır, sonra çalıştırılır veya `NOT_EXECUTED` kalır.

## Tamamlanma tanımı

Her WP için aşağıdaki kanıtlardan en az biri üretilir:

1. Kod/test değişikliği + geçen test sonucu (terminal exit code, sample count).
2. Veya açık `NOT_EXECUTED` kararı ile neden (izin, maliyet, ortam) ve güvenli next action.
3. Rapor ve makine okunur benchmark JSON güncellenir.
4. Paket doğrulayıcı ve regresyon testleri yeniden çalıştırılır.
5. Gerekirse bağımsız verifier ve commit/push.

## Riskler

- 50k build çok yavaş olabilir; bu durumda `skip` + raporlanmış bütçe aşımı.
- Gerçek proje answer-key corpus'u, source tree değişince stale olur; test her çalıştığında HEAD'den yeniden üretilmeli.
- Canlı provider ölçümü, ağ/izin hatası nedeniyle başarısız olabilir; bu bir regresyon değil, raporlanmış sınırdır.
