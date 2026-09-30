---
schema: zekam-active-task/v2
task_id: ZEKAM-RAG-ROUTER-QUALITY-002
status: APPROVED_ACTIVE_TASK
title: Zekam RAG Router Kanit Kalitesi ve Istemci Guvenilirligi Iyilestirmesi
created_at: 2026-09-29T22:29:51+03:00
baseline_repository: mehmet-karacan/zekam
baseline_branch: main
baseline_head: f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7
baseline_commit_subject: "performans: RAG kalan dogrulama kapilarini kapat ve canli provider olcumu ekle"
baseline_is_fixed_revision: true
legacy_postgresql_data_import: FORBIDDEN
postgresql_runtime_dependency: FORBIDDEN
docker_required_for_zekam_core: false
ui_surface: FORBIDDEN
push_authorized: false
runtime_test_evidence_at_task_creation: NOT_EXECUTED
---

# AKTIF_GOREV.md

## 1. Amaç, teslim ve kapsam

Zekamın mevcut RAG/router/istemci hattını, aşağıdaki kanıtlanmış hatalar ve açık doğrulama maddeleri üzerinden **gerçek kod + test + önce/sonra kanıt** ile iyileştir. Yalnız rapor yazmak teslim değildir. Yeni platform veya ikinci orchestration/RAG frameworkü kurma. SQLite/FTS5/sqlite-vec, mevcut RRF, runtime admission, Work Graph ve gerçek source binding korunacak.

Temel hedef: **doğru scope → doğru bilgi katmanı → cevabı taşıyan bounded kanıt → dürüst ve yararlı cevap**. Genel soru proje RAGına gitmez. Basit proje bilgi sorusu ayrı model-router child gerektirmez. Mutation, yetkili source fallback ve gerçek agentic çalışma kendi mevcut plan/claim/receipt/verifier kapılarına tabidir.

Bu dosya, kullanıcının iyileştirme isteğine ait uygulanacak kapsamı tanımlar. Headerdaki `APPROVED_ACTIVE_TASK`, mevcut v2 parserın sabit contract değeridir; tek başına operasyonel plan onayı, canlı provider yetkisi, Work Graph statei, claim, lease veya başarı receiptı üretmez. Bu teslim sırasında kullanıcının repositorysindeki aktif dosya değiştirilmedi. Gerçek kaynak kökünde bu görev seçildiğinde başlangıç protokolü, task digesti ve mevcut işlerle reconciliation uygulanacak. Bitmemiş eski Global DoD kapıları bu metinle silinmez veya completed sayılmaz. [K18–K22]

### Hazırlıkta yapılan / yapılmayan

29 Eylül 2026da GitHubın `f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7` revisionı ve kullanıcının `arastirma_prompt.md` / `bulgu_defteri.md` dosyaları incelendi. Birkaç regex/pencere koşulu izole Python karşı örneğiyle yeniden üretildi. **Gerçek Zekam pytest, kullanıcının Windows kurulumu, GPU kaynakları, canlı model veya remote embedding testleri burada çalıştırılmadı.** Front matterdaki NOT_EXECUTED bunun içindir; eski committe yazan test sayıları yeni koşu değildir.

### Kapsam dışı

Yeni UI/dashboard/TUI, PostgreSQL/Redis/Docker zorunluluğu, toplu veri temizliği, proje yeniden kurma, tüm Git/Jira geçmişini kör vektörleştirme, provider/model IDlerini tahmin etme, her soruyu pahalı modele yollama, sınırsız top-k/context artışı, güvenlik eşiklerini gevşetme, mastera doğrudan commit/push, kullanıcı içeriklerini silme veya reindexi sorgunun gizli effecti yapmak yok.

## 2. Başlangıç ve otorite protokolü

Önce `AGENTS.md`, `00_BASLA.md`, mevcut aktif görev, `GLOBAL_DEFINITION_OF_DONE.md`, `DEVAM_PROTOKOLU.md` ve mevcut generated projectionı oku. Bağlı **gerçek** source rootta olduğunu doğrula. Registryde bağlı başka GPU/SKY/Akış projelerine yazma. Fallback, source read ve bütün testler de mevcut root/permission kurallarına bağlıdır; proje kopyası/mirror/detached worktree ile bu kuralı aşma.

Yerelde bir kez şu gerçeklikleri kaydet: `git status --short`, `git rev-parse --show-toplevel`, `git rev-parse HEAD`, imported `zekam.__file__`, Python/OpenCode/SQLite sürümleri, active-task digesti ve aktif work/plan revision. Mevcut repository komutlarını `--help` ve source üzerinden doğrula; olmayan sync/generator komutunu uydurma. Gerçek `ActiveTaskContract.load(Path('AKTIF_GOREV.md'))` APIı headerı doğrular. YAML projectionı elle digest yazarak üretme; mevcut yetkili üretim yolunu kullan. [K19]

Baseline ilerlemişse yalnız ilgili dosyaları/diffleri inceleyip bu taskı actual revisiona kontrollü taşı; branch reset/stash/checkout yapma. User dirty değişiklikleri ile bu task patchlerini ayrı kaydet. Bu metinde çözüldü denen bir mekanizma yerel HEADde yoksa önce farkı açıkla; varsayımı gerçek davranışa göre düzelt. Önceki taskta zaten çözülmüş query full-scan/cache/CAS/bulk-citation alanlarını tekrar bozma. [K07–K09]

Her mutation için gerçek Work Item/plan, claim ve gerekiyorsa lease alınır; meaningful step checkpointi, terminal receipt ve bağımsız verification olmadan iş kapanmaz. Claim var receipt yoksa recovery-required; silent retry yok. Bu metindeki RAG26-Rxx yalnız tasarım IDleridir, yeni UUIDleri veya tamamlanma durumlarını uydurma.

Güvenli yerel kod/test bu kapsam içindedir. Şunlar ayrıca exact plan ve izin ister: canlı query embedding, provider qualification, source/document embedding/reindex, canlı model kampanyası, kullanıcı-genel client/config/agent dosyalarına dağıtım ve herhangi push/destructive effect. Gerekli izin eksikse yalnız ilgili effect BLOCKED_AUTHORIZATION kalır; bağımsız güvenli testleri çalıştır, bütün sistemi tamamlandı diye sunma.

## 2A. Yerel reconciliation ve uygulama durumu (yerel dogrulama, 2026-09-30)

Bu gorev metni GitHub `f75c6a3` revisionina gore yazildi (baseline degismez). Yerelde dogrulanan farklar:

- Yerel `main` baseline'in uzerinde ilerlemistir (13 radar/arastirma commit'i ve bu gorevin ilk kesitleri pushlandi). R00 bunu kayda gecirir.
- Onceki yasayan gorev `ZEKAM-EVIDENCE-DRIVEN-ENGINEERING-EVOLUTION-001` (SHA-256 `9fb789564987597ec579702765d4e5b0c67b7665ac1bf6f2e898a8ab929a4b60`) exact Markdown ve projection ile `docs/archive/tasks/` altina alindi; bitmemis kapsami silinmez, arsivdeki metinde kalir ve Global DoD pending maddeleri degismeden tasinir. Gecis oncesi operational Work Graph'ta bu gorev icin acik Work Item yoktu; ilgisiz eski `active` madde ("UI ve dashboard yuzeylerini kaldir") oldugu gibi birakildi.
- Yerel Windows kalite kosusu: ruff temiz; mypy 429 hata; pytest 310 failed + 79 error (pwd, macOS-only sandbox, symlink ayricaligi, POSIX yol varsayimi). Bu hatalar RAG gorevinden onceki durumdur; "onceden gecen" sayilmaz, R00 baseline'inda ayri kaydedilir.
- R11 canli kampanya: `zekam model campaign plan` `blocked-catalog-scope-drift`; `Kimi-K2.7-Code` config-only. Katalog drift kapanmadan R11 BLOCKED kalir.

### Uygulama durumu (kanit: pushlanmis commit + bagimsiz verifier)

| Item | Durum | Kanit |
|---|---|---|
| R00 baseline | Kismi | Gecis, arsiv ve yerel kalite kosusu kaydi bu bolumde; kaynak-root/CLI surum kaydi ayri receipt bekler |
| R01 Turkce kesme | Tamam | `3eb7b59`, 12 test, verifier GECTI |
| R02 profil kimligi | Tamam | `1d080c0`; kimlik probe vektorlerinden ayrildi (1024 boyut jitter'da eski 100/100, yeni 0/100 degisim); gpu-fusion taze probe'ta dense=20 dogrulandi; 4 indeksin yeniden kurulumu suruyor/kuruldu |
| R03 alinti penceresi | Tamam | `bef8308`, `a86c376`; sorgu-farkindalikli satir penceresi, sinif anotasyonlari, cok parcali `answer_excerpts`, cikti tarafinda gizli deger maskesi |
| R04 kod/config kapsami | Kismi-ileri | `19bc265`, `347b9e3`, `6268af0`, `9ed461d`, `a86c376`: paketleme, referans tanim genisletme, komsu chunk, listeleme; gpu-fusion Q1 5/5, Q2 10/10 sirali, Q3 4/4. Q10 (config) BLOCKED_BY_POLICY: `application-*.yaml` sifre kalibi nedeniyle indekslenmez; dislama nedeninin sonuca yazilmasi acik |
| R05 on-karar | Kismi | `60e2cbc`, `403347d`: port/yil Jira degil, UI etiketi mutation degil, issue degisiklik sorusu `project-history`; client hook'ta tek preflight ve sayac olcumu acik |
| R06 istemci tuketimi | Kismi | `245ecba` ve sonrasi: sablon (gereksiz route yok, state tablosu, answer_excerpts, clarification); kullanici-genel ~/.config/opencode dagitimi ayri onay bekler (BLOCKED_AUTHORIZATION) |
| R07 Git gecmisi | Tamam | `bf8ee54`: `zekam project history`, 24 test, verifier bulgulari duzeltildi; gpu-fusion SKYRSM-5659 -> `d026d692` ve `3139ed25` |
| R08 model admission | Kismi | `825a34b`: `model_context_admission` + doctor `runtime.opencode-model-limits` (12 modelde limit eksik raporlanir); kullanici opencode.json'una limit yazilmasi ayri onay bekler |
| R09 offline kabul | Kismi | `403347d`: `rag_router_probe_v1.json` (kaynak dogrulamali) + 50 test; bagimsiz kabul dogrulamasi GECTI (kosullu) ve riskleri kaydetti; ablation/latency raporu repo disinda |
| R10 bakim | Kismi | gpu-fusion, akis, schema-transform-platform yeniden kuruldu; sky-microservis kurulumu suruyor; ai-db-change-analyzer, zekam, sky-spring-ui kaynak bagi olmadigi icin indekslenmedi |
| R11 canli kampanya | BLOCKED_AUTHORIZATION | `zekam model campaign plan` blocked-catalog-scope-drift; ayri exact plan ve kullanici onayi gerekir |

Bu tablo durum ozetidir; terminal receipt yerine gecmez. Bagimsiz kabul dogrulamasinin acik riskleri: kisa belirsiz soruda netlestirme yalniz oneri olarak sunulur; kimliksiz sorularda zayif lexical eslesme + dense ile `answered` donme (Q10/uzun anlamsiz soru) yanlis-pozitif riski; Q5/Q6 icin dogru dosya citation'da ama alinti penceresi ilgili satirlari kacirabiliyor. Kapsam onerisi: R00-R05 cekirdek teslimdir, R06-R11 cekirdek DoD'ye bagli degildir.

## 3. Kanıtlı bulgular ve dikkat edilmesi gereken düzeltmeler

- **Kesin parser hatası:** `retrieval.py:45,54–71` tek tırnaklı phrase regexi, Q1de `inde hangi Spring Batch job` kimliği üretiyor. [K02]
- **Kesin pencere sorunu:** `_select_window` ve `_build_excerpt` query-aware değil; seçilmiş chunkın başı tekrar kısaltılıyor. Doğru dosyaya gelmek doğru cevabı kanıtlamaz. [K03,K04]
- **Dense kapalı gözlemi:** Ledgerda 10/10 dense=0/profile-stale. Kod güvenli biçimde incompatible denseyi kapatıyor. Hatanın asıl kaynağı saved index mi, gerçek profile change mi, cached profile restore mu yerelde ayrıştırılacak. [K06,K08,K09; BF38–39]
- **Yanlış varsayımı uygulama:** `remote_provider_used=True`, query embedding gerçekten yapıldı demek değildir. Cache-hit yolunda aynı alan true ve probe_call_count=0. [K09]
- **CLI suçlu değil:** Explicit `--project` metinsel çıkarımdan önce gelir. Bu korunacak. Dokuz DeepSeek testinde prompt kesilmiş; model başarısızlığı diye sayılmayacak. [K10; BF64–68]
- **Router zaten deterministik:** İlk karar fonksiyonu provider-free. Sorunlar gereksiz invocation/child ve eksik katman/intent ayrımı; yeniden LLM router kurma. [K11,K14,K15]
- **Yeni ek karşı örnekler:** 9001 portu Jira adayı; ekle ekranında sorusu mutation; üretici `lexical-only-degraded` ile coordinatorın state dili uyuşmuyor; ilk üç citation her enumerationın coverageını garanti etmiyor. [K12–K14]
- **Test metodolojisi:** Q5 CLOB record-id/400, Q6 reserved-word/ORA-00904; bunlar diakritik çifti değil. Aynı Jira keyine iki commit bağlanması conflict/superseded testi değil. [BF24–26,BF32–34]

Diğer bulgular ve güven düzeyleri `ZEKAM_ARASTIRMA_RAPORU.md` içinde. Bu ek rapor yoksa bu görevin Q/T matrisi ve kaynak referansları uygulamak için yeterlidir; ek raporu ayrı authority sayma.

## 4. Hedef davranış / bütçe sözleşmesi

### Deterministik karar ve knowledge layer

Scope önceliği: explicit parametre → kullanıcı tarafından seçilmiş aktif scope → exact kullanıcı sorusundaki registry/alias/Jira ipucu → tek netleştirme. Harness/system wrapperı ve retrieval metni scope belirlemez. Explicit proje ile issue key conflictinde sessiz proje değiştirme yok.

| Soru sınıfı | Seçilecek katman / davranış | Model-router child |
|---|---|---|
| Konuşma / genel kavram | Proje corpusunu atla; güncel genel araştırma gerekiyorsa mevcut ayrı yol | 0 |
| Tanım, sınıf, endpoint, job | Exact proje + code/symbol kanıtı | 0 |
| Port / Spring profile | Exact proje + config | 0 |
| Issue durumu | Exact issue + yetkili Jira | 0 |
| Issue kapsamında ne değişti | Exact proje/ref + bounded Git history/diff | 0 |
| Belirsiz iş/alias/akış | Bir netleştirme; tüm proje araması yok | 0 |
| Açık iki-proje karşılaştırması | Yalnız iki izinli scope; ayrı provenance | Ancak gerçek agentic role selection gerekirse |
| Gerçek uygulama/mutation/source fallback | Mevcut exact plan, route, claim, receipt/verifier | Mevcut policyye göre |
| İndeks yok / incompatible | Açık reason + gerçek bakım planı; otomatik reindex yok | Otomatik değil |

### Context ve latency

Mevcut `EmbeddedProjectRAG.query` default **1200 tahmini evidence token** bütçesi korunur; query-aware pencerelerle Q1/Q2/Q3/Q10un doğruluğu artırılır. Route anotasyonu hedef ≤200 tahmini token. Genel/sohbet querylerinde project retrieval, doctor ve model-router child=0. Yeterli exact sorguda query embedding=0; uygun warm semantic sorguda en fazla1, warm qualification tekrarları0. [K06]

Saf ön-karar warm p95≤25ms hedef; soğuk CLI importu ayrı. Aynı offline reference ortamda orchestration p95 ve route/evidence tokenlarında ≥%30 iyileşme hedefi; baseline çok küçükse overhead/counter gerekçesi ver. Hedef tutmazsa gerçek ölçümü yaz, eşiği sessiz düşürme. Default1200 tahmin ile gerçek provider tokenizer sayıları ayrı alanlar. Full model context bütçesi system+tools+history+query+evidence+output reserve içerir.

Citationın işaretlediği bytes, seçilmiş window ve modelin gördüğü metin aynı olmalı. Kaynağı değiştirerek alıntı üretme; sınırlı kanıttan repoda kesin yok veya tüm step/joblar bunlar sonucu çıkarma. Work durumu, authority ve canlı deployment durumu RAGdan türetilmez.

## 5. Sıralı Work Itemlar

Sıra: **R00 → R01 → R02 → R03 → R04 → R05 → R06 → R07 → R08 → R09 → gerekli/onaylı R10 → onaylı R11**. Aynı merkezi dosyalara dokunan işler tek-yazar ve sıralıdır. Bağımsız salt-okunur verifier/fixture hazırlığı yalnız gerçek disjoint scope ve mevcut protokol izin veriyorsa paralel olabilir.

Ortak tamamlanma kanıtı her item için: actual source SHA/diff → failing-before/passing-after test → sanitized counters → rollback doğrulaması → bağımsız verification → gerçek terminal receipt. Mock, fake provider veya saf fonksiyon testi canlı model başarısı değildir. Dosya yolları yeni/önerilen olarak işaretlenmişse önce mevcut eşdeğeri ara; varsa onu genişlet, gereksiz ikinci modül oluşturma.

### R00 — Başlangıç, sürüm reconciliationı ve ölçüm tabanı

- **Work Item ID:** `RAG26-R00`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Gerçek source root, installed CLI ve GitHub baseline arasındaki ilişkiyi doğrula. Eski AKTIF_GOREV kapsamındaki tamamlanan/bekleyen işleri gerçek Work Graph ve test kanıtlarıyla eşleştir. Claude ledgerındaki geçersiz koşuları ayır.
- **Bağımlılık:** Yok; diğer bütün işlerden önce.
- **Değişecek / okunacak dosyalar:** Mevcut AGENTS.md, 00_BASLA.md, AKTIF_GOREV.md, GLOBAL_DEFINITION_OF_DONE.md, DEVAM_PROTOKOLU.md (okuma); src/zekam/application/active_task_contract.py; yeni tests/fixtures/rag_router_probe_v1.json; proje dışı sanitizerlı evidence dizini.
- **Uygulama:** Önce yalnız read-only baseline. İçe aktarılan zekam.__file__, git HEAD, CLI/OpenCode sürümü, binding/project ID, generation/profile kimlikleri, source dirty durumu, izinli model IDleri ve effective config alan adlarını kaydet; secret değerlerini kaydetme. Q1–Q10 kaynaklarını gerçek GPU rootunda ve local committe doğrula. Ham kayıt bulunamazsa original-ledger-only diye bırak. Mevcut taskı completed sayma; devam/recovery şartlarını uygula.
- **Testler:** ActiveTaskContract.load; mevcut paket doğrulama yolu; fixture identity/provenance şeması; kullanıcı dirty dosyalarının korunması; Q1–Q10 gold local validation.
- **Ölçülebilir kabul:** Baseline raporu ve source→test→work-item matrisi var; tüm 14 probede revision/locator ya verified ya açık pending. Geçersiz dokuz DeepSeek denemesi kalite skoruna girmez. Henüz runtime/test PASS uydurulmaz.
- **Rollback:** Yalnız bu adıma ait yeni test/manifest ekini geri al; kullanıcı kaynaklarını, mevcut task tarihçesini ve operational DB yi sıfırlama.
- **Bağımsız verifier:** Builderdan farklı execution_identity ile baseline/authority ve fixture-provenance incelemesi.
- **Risk / effect:** low; local read ve fixture mutation; canlı effect yok.
- **Kanıt bağları:** [K10,K18–K22; BF3–15,BF61–70]
### R01 — Türkçe kesme işareti ve exact kimlik düzeltmesi

- **Work Item ID:** `RAG26-R01`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Q1deki yanlış quoted phrase hatasını en küçük güvenli kod değişikliğiyle kaldır.
- **Bağımlılık:** R00
- **Değişecek / okunacak dosyalar:** src/zekam/domain/retrieval.py; yeni tests/unit/test_rag_turkish_identity_contract.py; gerekiyorsa aynı normalizer için küçük ortak domain modülü (yeni olduğu açık yazılacak).
- **Uygulama:** Kelime içi ASCII/curly apostropheyi tırnak başlangıcı sayma. Gerçek dengeli alıntıları, boşluk içeren bilinçli phrasesı, #123, issue, dot/underscore/path/CamelCase kimliklerini koru. Türkçe harf normalizasyonunu exact kaynak/simge kimliğine uygulama; lexical shadow form ayrı kalsın. Unmatched quote ve bounded query input davranışı testli olsun. Stemming frameworkü ekleme.
- **Testler:** T01–T04; Q1 ve Q1-TR-CURLY; dengeli gerçek tek/çift tırnak, farklı kapanış, possessive İngilizce, Oracle'da, backend'inde ... job'lari; quoted phrase içindeki teknik kimlik suppression regresyonu.
- **Ölçülebilir kabul:** Yanlış inde hangi Spring Batch job kimliği 0; gerçek quoted/path/key kimlikleri değişmez; bütün mevcut exact contract testleri geçer. Bu item tek başına Q1 tam cevap başarısı diye raporlanmaz.
- **Rollback:** Bu küçük parser patchini ve yeni normalizer kullanımını feature/policy revisionıyla geri al; indeks/generation verisini silme.
- **Bağımsız verifier:** Bağımsız test çalıştıran verifier; en az bir aynı anlama gelmeyen negatif çiftle over-normalization kontrolü.
- **Risk / effect:** low; parser davranışı merkezi olduğu için geniş regression zorunlu.
- **Kanıt bağları:** [K02]
### R02 — Profil kimliği, qualification cache ve dense admission

- **Work Item ID:** `RAG26-R02`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Aynı embedding uzayında cold/warm/new-process profil kimliğinin tutarlılığını koru; gerçek uyumsuzlukta gereksiz provider çağrısını kes.
- **Bağımlılık:** R01; R00 baseline zorunlu.
- **Değişecek / okunacak dosyalar:** src/zekam/application/project_rag_runtime.py; src/zekam/application/embedded_project_rag.py; mevcut embedding profile/provider/cache modülleri importtan doğrulanacak; yeni tests/unit/test_rag_profile_roundtrip_contract.py.
- **Uygulama:** Önce persisted generation, knowledge binding, cold provider, cache record ve restored profile alanlarını secret-free karşılaştır. Restore edilen tam immutable profilin digestini cached.profile_digest ile doğrula; farklıysa güvenli explicit drift. Eksik cache alanını tahmin ederek kabul etme. Profile equalityyi sahte digest atanarak düzeltme. Statik incompatibilityyi uzaktan qualificationdan önce tespit et; bilinemeyen profil için yalnız yetkili qualification. Eski güvenli cache/single-flight/query-state-CAS davranışını koru. Source stale, model-space mismatch, auth/provider unavailable ayrı reason code olsun.
- **Testler:** T11–T14,T28,T30; cold/warm/durable-cache/new-process restore; değişen endpoint/model/dimension/prefix/tokenizer/normalization; legacy cache; timeout; corrupted cache; mismatchte 0 embed_query. Spy/fake sonuçları yalnız mekanik test kabulü.
- **Ölçülebilir kabul:** Aynı accepted immutable profil round-trip digest eşit; gerçek farklı uzay fail-closed. Önceden bilinen mismatch için query embedding 0, avoidable qualification 0. Sağlıklı fixtureda dense>0, gerçek semantic kalite ayrıca R11. remote_provider_used ile gerçek request sayısı karıştırılmıyor.
- **Rollback:** Cache şema/normalizer sürümüyle eski güvenli yola dön; eski active generation korunur. Migration rollback ve cache quarantine sahiplik kanıtlı; toplu kullanıcı verisi silme yok.
- **Bağımsız verifier:** Bağımsız provider/profile ve güvenlik verifierı; cache-hit ve cold yolunu ayrı çalıştırır.
- **Risk / effect:** medium-high; vector-space integrity. Uzak qualification ve reindex bu itemda otomatik çalışmaz.
- **Kanıt bağları:** [K06,K08,K09]
### R03 — Query-aware kanıt penceresi ve exact locator

- **Work Item ID:** `RAG26-R03`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Doğru chunk/dosya bulunduğunda kullanıcıya import bloğu değil cevabı taşıyan metin gitsin.
- **Bağımlılık:** R02
- **Değişecek / okunacak dosyalar:** src/zekam/application/retrieval_service.py; src/zekam/application/embedded_project_rag.py; src/zekam/domain/retrieval.py; gerçek citation/context consumer modülleri call graph üzerinden; yeni tests/unit/test_rag_answer_span_contract.py.
- **Uygulama:** _select_window ve _build_excerptin ortak bir seçilmiş EvidenceWindow sözleşmesini kullanmasını sağla. Query intent + teknik kimlik + kod/config anchorlarından satır bazlı ilgili aralık seç; metadata/preambleye küçük bağlam ver ama bütçeyi yemesine izin verme. Prompt metni ile citation window aynı bytes olsun. Birden fazla aralık için ayrı locator; omitted aralıklar açık. Full chunk digest, window digest ve source digest farklı alanlar. Sadece first used chunkın ilk 500 karakterini tekrar kesen ikinci tüketim yolunu kaldır. Mevcut v1 string alanını uyumlu tut; yeni bounded evidence text/locators additive veya açık sürümlü.
- **Testler:** T15–T17,T22; uzun import öncesi/sonrası endpoint, flow ve validasyon; çok uzun tek satır; CRLF, Unicode; aynı dosyadan iki pencere; bütçe 0/47/48/1200; yanlış locator/digest adversarial testleri.
- **Ölçülebilir kabul:** Q3ün endpoint gövdeleri ve Q6nın doğrulama gövdesi seçilen metinde; synthetic prefix failure kapanmış. Window line range ve bytes %100 eşleşir; toplam tahmini evidence token ≤1200. Evidence yeterli değilse partial/low-evidence; yanlış tam cevap yok.
- **Rollback:** Sözleşme alanlarını geriye uyumlu bırakıp yeni selectorı kapat; locator güvenliğini geri alma. Eski indexi yeniden üretmek varsayılan ihtiyaç değil.
- **Bağımsız verifier:** Bağımsız citation verifierı; window bytesını pinned source ile yeniden karşılaştırır.
- **Risk / effect:** medium; offset/provenance hatası kritik, token azaltma uğruna kaynak bozulmaz.
- **Kanıt bağları:** [K03,K04,K07]
### R04 — Kod/config amacı, enumeration coverage ve bounded ilişki genişletme

- **Work Item ID:** `RAG26-R04`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Q1/Q2/Q3/Q10u aynı genel similarity aramasına bırakmadan doğru kapsam ve kanıt türüyle cevaplat.
- **Bağımlılık:** R03
- **Değişecek / okunacak dosyalar:** src/zekam/application/retrieval_service.py; src/zekam/application/embedded_project_rag.py; mevcut project_knowledge_index ve code_graph_ranking entegrasyonları yerelde doğrulanacak; yeni tests/unit/test_rag_code_config_coverage.py.
- **Uygulama:** Enumeration, ordered-flow, endpoint-list, config-lookup niyetlerini mevcut classifierı genişleterek ayır. Parser/symbol inventory varsa onu yeniden kullan. Tanımı import/referanstan ayır. Job→Flow geçişini gerçek symbol/file referansıyla en fazla 2 edge hop / 3 ek chunk sınırında genişlet; tüm proje graph keşfi yok. Kaynakta ilişki yoksa uydurma. Config dosyasındaki port/profil anahtarlarını yapı koruyarak seç. Python coverage ve FTS query aynı sürümlü lexical shadow normalization kullansın; exact source identityye dokunma. CSS/DTO/SQL tiplerini hardcode blacklist etme; query-aware relevance ve gerektiğinde soft ranking uygula. RRF korunur.
- **Testler:** Q1,Q2,Q3,Q10 ve varyantlar; T04,T14–T20; declaration-vs-import, incomplete enumerations, absent flow edge, true SQL question, ı/i ve diakritik çiftleri; metadata index update/invalidation.
- **Ölçülebilir kabul:** Verified GPU fixtureında 5/5 job, doğru 10-step sıra, 4/4 POST endpoint, port ve 5/5 profil. Tanım coverage tamamlanmadan tümü ifadesi yok. Q5/Q6 eşdeğer varyantları aynı iddiaları doğru kaynaktan destekler. Düzelme sadece dense skoru düşürülerek sağlanmaz.
- **Rollback:** Amaç adapterları/expansion featurelarını sürümle kapat; fallback güvenli eski genel retrieval. Yeni lexical index gerekiyorsa ayrı generation rollback; doküman kaynakları silinmez.
- **Bağımsız verifier:** Bağımsız code/config verifierı; gerçek job flow zincirini ve config override sınırını kontrol eder.
- **Risk / effect:** medium; dillere özel adapter ve false-negative riski. GPU isimleri production kurallarına hardcode edilmez.
- **Kanıt bağları:** [K05,K07,E03; BF21–30]
### R05 — Ucuz ön-karar, açık scope ve bilgi katmanı seçimi

- **Work Item ID:** `RAG26-R05`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Genel soruyu proje aramasından ayır; açık projeyi koru; alias/key/config/kod/geçmiş katmanını deterministik seç.
- **Bağımlılık:** R04
- **Değişecek / okunacak dosyalar:** src/zekam/application/request_routing.py; src/zekam/interfaces/cli/main.py; mevcut route CLI ve Jira resolverı; config/project_families.yaml yalnız mevcut şema/authorityye uygun gerekirse; yeni tests/unit/test_request_preflight_contract.py.
- **Uygulama:** Mevcut routerı ikinci bir frameworkle değiştirme. Ucuz RequestPreflight kararı önerilen yeni internal sözleşmedir: exact_query_digest, explicit_project, scope_provenance, intent, knowledge_layers, requires_project_retrieval, requires_model_router, clarification ve reason_codes. Dış CLI adlarını helpte varmış gibi uydurma. Açık parametre üstünlüğünü koru; wrapper/system talimatlarından proje çıkarma. Sayı tek başına Jira olmasın: issue ipucu veya sıkı kısa-reference grameri; GPU 1234 / SKY 4321 davranışı korunsun. ekle ekranında salt-okunur, gerçekten ekle mutation. Belirsiz ailede bütün üyeler taranmasın. Açık karşılaştırmada sadece istenen izinli projeler.
- **Testler:** T05–T10,T29,T32; Q10-NUMBER, Q5-TR; GPU/SKY alias mevcut resolver regresyonları; unknown alias, iki eşleşme, explicit-scope/issue-conflict, genel Spring Batch nedir, harmanlanmış wrapper.
- **Ölçülebilir kabul:** Genel/sohbet fixturesında 0 project retrieval ve 0 model-router child. Tek-projeli read-only fixturesında child router 0. Port/yıl Jira olmaz; UI etiketi mutation olmaz. Saf in-process warm p95 ≤25ms (100 offline ölçüm); provider_calls=0 korunur.
- **Rollback:** Ön-kapıyı feature versionıyla eski deterministik routera yönlendir; explicit scope ve güvenlik testleri korunur. Registry config yedeği sahiplik/digest bağlı.
- **Bağımsız verifier:** Bağımsız routing/policy verifierı; authorized/unauthorized ve ambiguity çiftleriyle test.
- **Risk / effect:** medium; yanlış niyetin yetkiye dönüşmemesi mevcut admissionla güvence altında kalır.
- **Kanıt bağları:** [K10–K16]
### R06 — Coordinator/istemci tüketimi ve gereksiz araç zincirini kesme

- **Work Item ID:** `RAG26-R06`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Üretici statei, seçilmiş evidence ve gerçek consumer davranışı aynı sözleşmeye uysun.
- **Bağımlılık:** R05
- **Değişecek / okunacak dosyalar:** .opencode/agents/zekam-coordinator.md; src/zekam/application/opencode_agent_bootstrap.py; mevcut client template/hook/agent üreticileri gerçek call graphla belirlenecek; yeni tests/unit/test_rag_client_state_contract.py.
- **Uygulama:** Gerçek state enumları için tek karar tablosu oluştur. answered, lexical-only-degraded, abstained-no-hit/low-evidence/no-edge/index-unavailable, timeout ve policy refusalı ayır. Sadece statee değil evidence_found/evidence_sufficient/pinned locator bütünlüğüne bak. Yeterli lexical kanıtı açık degraded cevaba dönüştür; eksik statei success sayma. İlk üç citation sınırını budget+claim coverage ile değiştir; 1200 evidence bütçesini büyütme. Ön-kararı client hookta bir kez kullan; başarılı aynı turda doctor/route/ask/source-root döngüsünü engelle. General conversationda project araştırma yok; güncel research ve agentic işlerin gerçek yetki/subagent kuralları korunur. Coreda ikinci gizli synthesis yok. Managed olmayan kullanıcı dosyalarına dokunma; global config dağıtımı ayrı exact effect.
- **Testler:** T06,T12,T22,T24,T29,T32; fixture sonucu→client aksiyonu→final metin zinciri; legacy v1 JSON parse, stdout temizliği; üretilmiş template drift; state unknown fail-closed; loop ve refused fallback yasağı.
- **Ölçülebilir kabul:** Tüm gerçek state sınıfları tüketiliyor; Q3/Q6 bodyleri modele ulaşıyor. Tek read-only turda route≤1, ask≤1/project, gerekli değilse doctor=0, gizli ikinci model synthesis=0. İndeks/API yanıtı generated_answer değilken üretildi diye sunulmaz.
- **Rollback:** Yalnız managed-owned sectionları expected-digest karşılaştırmasıyla geri al; kullanıcının agent/config değişikliklerini ezme. Eski v1 consumer kırılmaz.
- **Bağımsız verifier:** Bağımsız client-contract verifierı; saf üretici testi değil tüketiciye kadar replay.
- **Risk / effect:** medium-high; agent politikası ve kullanıcı-genel dosyalara yayın yetkisi ayrı tutulur.
- **Kanıt bağları:** [K07,K10,K14,K17]
### R07 — Jira anahtarı → bounded Git geçmişi / değişiklik kanıtı

- **Work Item ID:** `RAG26-R07`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Q4 gibi ne değişti sorularını mevcut yetkili tarihçe/kaynak katmanından cevapla.
- **Bağımlılık:** R06
- **Değişecek / okunacak dosyalar:** Mevcut Git/source-discovery ve Jira adapterları R00/R07 okumalarında doğrulanacak; request_routing.py bilgi katmanı dispatchi; gerekiyorsa yeni src/zekam/application/project_history_query.py ve tests/unit/test_project_history_evidence_contract.py.
- **Uygulama:** Önce mevcut history yolu olup olmadığını kodla doğrula; varsa düzelt, aynı işi ikinci kez yazma. Yoksa read-only typed adapter ekle. Exact issue tokenı ile seçili yerel ref geçmişinden commit ID/subject ve ilgili bounded diff al; shell-string interpolation yok, fetch/network yok, global --all kapsamı yok. Tam SHA, parent, target ref, tree/source revision ve diff hunk locator taşı. Aynı issue iki commit birleşimdir. Shallow/eksik/truncated history, revert ve güncel HEAD etkisini açık ayır. İlk uygulamada bütün geçmişi vektörleştirme. Repo dışı Jira fetch yalnız mevcut connector/policy ve gerçekten issue verisi gerektiğinde.
- **Testler:** Q4; T19–T21,T29; iki aynı issue commit, prefix false-positive, başka repo key, shallow history, revert, binary/secret diff, injectionlike message, timeout, committe artık bulunmayan path.
- **Ölçülebilir kabul:** Local verified Q4te iki commit ve iki farklı değişiklik doğru kaynakla verilir. Varsayılan sorguda en fazla 20 matching commit, 3 ilgili diff dosyası, 2000 işlenebilir diff satırı; ortak deadline ve 1200 evidence bütçesi. Kesilen kapsam partial, yanlış tam tarihçe yok.
- **Rollback:** History dispatchini kapatıp açık unsupported/history-unavailable döndür; Git geçmişini değiştirme, git reset/clean/fetch/push yok.
- **Bağımsız verifier:** Bağımsız source-history verifierı; full SHA/diff iddiasını exact yerel hedef ref üzerinde doğrular.
- **Risk / effect:** medium; Git geçmişinde secret/PII olabileceğinden disclosure/sanitizer önemli.
- **Kanıt bağları:** [K11,K12; BF24,BF46,BF99–100]
### R08 — Model limit doctorı, güvenli harness ve boş çıktı dürüstlüğü

- **Work Item ID:** `RAG26-R08`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Kalite testini promptu kesilen, contexti taşan veya boş dönen süreçlerle karıştırma.
- **Bağımlılık:** R07
- **Değişecek / okunacak dosyalar:** Mevcut OpenCode config/inventory/diagnostics/transport modülleri import ve CLI call graphından doğrulanacak; opencode_agent_bootstrap.py dağıtım entegrasyonu; yeni tests/unit/test_model_admission_contract.py ve tests/integration/test_opencode_prompt_transport_contract.py.
- **Uygulama:** Effective merged configte model context/output alanlarını actual authorized inventoryyle eşleştir. Opaque canonical IDye provider prefixi ekleme/çıkarma; bilinmeyen limit/key fail-closed. Tam payload input+output reserve+emniyet payı sığmıyorsa provider çağrısından önce açıklamalı error. Kullanıcı configini sessiz yazma. Model JSONLde empty text/tool terminalini typed empty_output yap; model/transport/gateway sebebini ayrı kaydet. Tool araması yokken verified_actions üretme. Harness exact prompt ile schema/query/project/prompt digesti taşır; local no-network echo ve gerçek Windows executable taşımasıyla CRLF/quotes sınanır. Stdin doğru kapatılır; timeout/cancel process tree temizler. Soru sayısı process başlatma sayısı ve provider request sayısı ayrı.
- **Testler:** T23–T27,T29,T32; 32768/32000/769 sınır vakası; valid output budget; unknown limits; empty stop/reasoning-only; invalid JSON; quote/%,!,&,|; cancelled process; preflight başarısızsa kalan 9 model koşusu otomatik başlamaz.
- **Ölçülebilir kabul:** Known overflow fixtureda upstream request=0. Transport exact prompt digestini korur. 0 text/0 tool terminali PASS değildir. Fake aradım fixtureı reddedilir. Doctor bu tanıları ağ çağrısı yapmadan gösterebilir; gerektiğinde ayrı live health izni gerekir.
- **Rollback:** Yalnız managed config patchi expected-digest/backup ile geri al; bütün opencode.json üstüne yazma. Harness eski hatalı yola sessiz dönmek yerine explicit disabled kalabilir.
- **Bağımsız verifier:** Bağımsız Windows transport ve model-contract verifierı; Linux fake Windows kanıtı sayılmaz.
- **Risk / effect:** medium; credential/PII ve shell injection sınırları; kullanıcı-genel config mutationı ayrı onaylı.
- **Kanıt bağları:** [K17,E01–E03,E05; BF61–82,BF125–139]
### R09 — Offline kabul, regression/ablation ve ölçüm raporu

- **Work Item ID:** `RAG26-R09`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Hangi düzeltmenin hangi sorunu kapattığını, toplam güvenlik/kaliteyi geriletmeden kanıtla.
- **Bağımlılık:** R08
- **Değişecek / okunacak dosyalar:** Yeni tests/fixtures/rag_router_probe_v1.json; yeni offline evaluator/benchmark testleri; mevcut retrieval/routing/client regression paketleri; yetkili dokümantasyon ve proje dışı sanitized evidence artefactları.
- **Uygulama:** 14 gerçek-proje probe + T01–T32 contractlarını koştur; local gold revisionı bağla. Parser→profile→window→coverage→routing→client ablationı aynı corpusla ölç. Synthetic ve gerçek semantic embedding skorlarını ayrı tut. Content evidence digest query/profile/generation/selected windows ile kararlı; run_id ve qualification digest ayrı. 10 warm-up +100 offline ölçüm, cold CLI ve warm process ayrı; failures/timeout/abstainler raporda kalır. Cache/state concurrency ve security regression tam koşulsun. Mevcut Global DoD kapılarını atlama.
- **Testler:** Q1–Q10 +4 varyant; T01–T32; proje test/lint/type/package kuralları; mevcut gerçek-project answer-key ve scaling tests (bulunan sürümde); secret scan ve changed-path scope.
- **Ölçülebilir kabul:** Verified fixtureda Q1/2/3/4/5/6/10=7/7, Q7/Q9=2/2, Q8=1/1, varyant=4/4. Window/scope/provenance/injection contractları %100. Kanıt/route tokenları hedef ≥%30 azalır, quality gerilemez; latency hedefleri aynı ortamda açık raporlanır. Canlı sonuç yoksa ona ait alan NOT_EXECUTED.
- **Rollback:** Benchmark/evaluator değişimini geri al; eski ölçümleri silme. İyileştirme quality tradeoffu geçmezse ilgili feature önceki güvenli sürüme alınır, eşik sessiz gevşetilmez.
- **Bağımsız verifier:** Bağımsız kabul verifierı; builderın aynı process assertionı independent receipt sayılamaz.
- **Risk / effect:** medium; gold leakage, cherry-picking ve fake-live kabul riski.
- **Kanıt bağları:** [K20,K21; BF21–34]
### R10 — Ayrı onaylı indeks hazırlığı / profil geçişi

- **Work Item ID:** `RAG26-R10`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Gerekliyse gpu-fusion profil uyumunu ve zekamın kendi indeks readinessini gerçek kaynaklarla kur.
- **Bağımlılık:** R09; exact maintenance plan + kullanıcı onayı + gereken claim/lease. Onay yoksa BLOCKED_AUTHORIZATION.
- **Değişecek / okunacak dosyalar:** Mevcut index_registered_project / index plan / generation publish yolları; runtime verisi yalnız açık yetkili exact proje alanında. Üretim kaynaklarını değiştirme.
- **Uygulama:** Varsayılan yalnız bakım dry-runı. Eski/yeniprofili, local source manifestini, belge sayısı/byte/tokenı, izinli provider exact IDyi, batch/call/amount tavanını, yeni generation ve rollback generationını planla. 5709u güncel corpus boyutu sanma. Gerçek uzay uyumluysa reindex yapma. Uzak source embedding query izninden ayrı izin ister. Onay digestine bağlanmış bakım çalışır; atomik publish/CAS, interruption recovery ve idempotency testli. Eski kullanıcı indeks/generation verisi otomatik silinmez.
- **Testler:** T11–T14,T29–T31; failure-before-publish, mixed-generation refusal, interrupted claim/no receipt recovery, changed-source replay; no source modification.
- **Ölçülebilir kabul:** Onaylı planda belirtilen kapsamdan sapma 0; izin ve call budget aşımı 0; active generation/policy digest eşit; zekam için index_readable ve approved snapshot smoke. Her effectte gerçek terminal receipt; onay verilmezse yapılmadı açık ve global kapanış yok.
- **Rollback:** Yeni generation publish öncesi eski aktif kalır; sonrası uyumlu eski generation/config ikilisi kontrollü geri seçilir. Farklı profil vektörlerini aynı sorguda karıştırma; veri silme yok.
- **Bağımsız verifier:** Bağımsız maintenance/provider verifierı; source manifest, receipt ve generation eşitliğini doğrular.
- **Risk / effect:** high; remote disclosure ve kalıcı indeks mutationı. Bu belge tek başına live authorization değildir.
- **Kanıt bağları:** [K06,K08,K09,K20,K21; BF8–14,BF138–139]
### R11 — Ayrı onaylı Kimi / DeepSeek uçtan uca kampanya

- **Work Item ID:** `RAG26-R11`. Bunlar plan kimlikleridir; çalışma anındaki UUID/claim/receipt değildir.
- **Hedef:** Düzeltmeleri gerçek istemci/model zincirinde tekrar ölç; teknik offline başarıyı model başarısı diye genelleme.
- **Bağımlılık:** R09 ve gereken R10 tamam; exact live plan + kullanıcı onayı. Onay yoksa BLOCKED_AUTHORIZATION.
- **Değişecek / okunacak dosyalar:** R08 harness; R09 fixture/evaluator; kampanya manifesti ve sanitized event/receipt raporları. Gerçek model ID/limitleri yerel inventoryden sabitlenecek.
- **Uygulama:** Bölüm 7deki 30-session planını uygula. No-network transport doğrulamasından sonra model başına bir smoke; başarısız modelin kalan koşularını otomatik harcama. Her model için Q1–Q10 ve dört varyant, sabit scope/source/policy; toolsuz sahte arama, empty output ve invalid harness ayrı verdict. Kampanya boyunca reindex, doc embedding, model değiştirme ve otomatik retry yok. Ek verifier providerı gerekiyorsa plana dahil edilmeden çağırma. Sonuçları independent verificationa ver; model family/DoD kapsama sınırını açık bırak.
- **Testler:** Gerçek OpenCode process+provider receipts; 14 case/model; 2 semantic dense canary; primary Q1/2/3/10 mandatory source checks; Q7/Q9 honest negative ve Q8 clarification.
- **Ölçülebilir kabul:** Tavanlar aşılmaz. Her iki model için seven answerable/negative/clarification/variant sonuçları ayrı tabloda; tüm zorunlu cases hedefi sağlanmadan canlı kabul yok. Sahte tool claim=0 ve empty success=0. Bu mini kampanya ZEKAM-DOD-025/tüm-model/platform kabulü sayılamaz.
- **Rollback:** Kampanyayı durdur, çalışan processleri bounded kapat ve receipt reconciliation yap. Başarısız modeli global inventoryden otomatik silme/değiştirme; teşhis ve ayrı plan sun.
- **Bağımsız verifier:** Builder/kampanya çalıştırıcısından bağımsız evidence verifierı; gerçek source ve transport receipts kontrolü; remote verifier bütçesiz çağrılmaz.
- **Risk / effect:** high; ayrı remote effect onayı, model/tool-call sayısı ve secret/PII kapsamı.
- **Kanıt bağları:** [K20,K21; AP74,AP96–98; BF138–139]


## 6. Q1–Q10 kaynaklı probe ve regresyon matrisi

Aşağıdaki gold değerler **kullanıcının bulgu defterinden** alındı. GPU kaynak revisionı ve locatorları bu hazırlıkta doğrulanmadı. İlk koşuda bunları gerçek bound source root ve full SHA ile sabitle; farklı revisionda değişmiş sonuç varsa gold historysini koru ve farkı belgeleyerek yeni gold oluştur. Kaynaklar görülmeden committed expected/actual aynı string yapılarak PASS üretme.

| ID | Amaç / gereken kanıt | Kabul |
|---|---|---|
| Q1 | EtlTransferJob; HakedisOlusturmaJob; ReadHakedisJob; HakedisHesaplamaJob; KuralCalistirimiJob | Beş tanımın tamamı gerçek tanım kanıtıyla gösterilir; import kullanımı tanım sayılmaz. Liste eksikse tam liste diye sunulmaz. |
| Q2 | insertHakedisLogStart; birimHakedisOlustur; createIslemTmpTableForHakedis; createOrUpdatePrimKalemi; kesintiAggUret; islemHesaplamaPartition; clearIslemTmpTable; updateIslemTipiHakedis; updateHakedis; updateHakedisLogEnd | On adım aynı sırayla ve gerçek flow zinciri kanıtıyla verilir. Bean listesi/importlar sıra kanıtı değildir. |
| Q3 | POST /batch-job/stop; POST /batch-job/start; POST /batch-job/start-hakedis-olusturma-job; POST /batch-job/start-job-manually | Dört POST endpoint; class-level ve method-level mapping birleşimi doğrulanır. |
| Q4 | 3139ed25 → CLOB/BLOB/LONG ID için 400; d026d692 → reserved-word doğrulaması. | İki değişiklik ayrı commitlere bağlanır; aynı issue iki commit çelişki sayılmaz. Güncel HEAD etkisi ve tarihsel etki karıştırılmaz. |
| Q5 | 400 Bad Request; CLOB record-id validation | 400 davranışı doğru doğrulama kaynağıyla desteklenir. Soru kod yazma talebi sayılmaz. |
| Q6 | DDL öncesi doğrulama; ORA-00904 doğrudan kullanıcıya sızmaz | Doğrulama akışı gösterilir; unrelated import, CSS, DTO alıntısı destek sayılmaz. |
| Q7 | Kanıt bulunamadığını kapsamıyla belirt; Kuyruk adı uydurma | RAG miss tek başına repoda kesin yok demek değildir. İncelenen scope/revision ve aranan yüzeyler belirtilir; sahte arama beyanı olmaz. |
| Q8 | oluşturma akışı; hesaplama/okuma akışı; tek netleştirme sorusu veya kaynaklı ayrım | Boş cevap yerine niyet ayrımı. Kaynak okunmadıysa ayrıntılı akış uydurulmaz; netleştirme üst üste tekrarlanmaz. |
| Q9 | README yetersiz; Kurulum adımları uydurma | README üzerinden kurulum açıklanamadığı söylenir; genel Spring kurulum reçetesi kaynak bilgisi diye sunulmaz. |
| Q10 | 9001; dev, dev_new, dev-mkaracan, tt-test, tttest | Port ve beş profil kendi config kaynağıyla doğrulanır; repository varsayılanı canlı deployment portu diye genellenmez. |


Q2 tam sıra: `insertHakedisLogStart → birimHakedisOlustur → createIslemTmpTableForHakedis → createOrUpdatePrimKalemi → kesintiAggUret → islemHesaplamaPartition → clearIslemTmpTable → updateIslemTipiHakedis → updateHakedis → updateHakedisLogEnd`.

Dört ek aynı-senaryo regresyonu:

- **Q1-TR-CURLY:** `gpu-fusion backend’inde hangi Spring Batch job’ları tanımlı?`
- **Q5-TR:** `Audit tablo tanımı ekle ekranında indekslenemeyen CLOB kolon Kayıt ID seçilirse ne oluyor`
- **Q6-TR:** `Audit yapısı oluşturulurken Oracle'da ayrılmış kelime kolon adı (örn. DATE) girilirse ne olur?`
- **Q10-NUMBER:** `gpu-fusion backend portu 9001 mi; hangi Spring profilleri var?`

Q5/Q5-TR ve Q6/Q6-TR gerçek eşdeğer çiftlerdir; Q5/Q6 değil. Başlangıç kanıtı orijinallerin yazımını ASCIIleştirmiş olabileceği için asıl raw prompt varsa ayrı sakla; görünür ledger metnini raw prompt diye etiketleme.

### Zorunlu offline contractlar

| ID | Contract | Kabul |
|---|---|---|
| T01 | ASCII apostrophe in Turkish suffix | backend'inde ... job'lari yanlış quoted phrase üretmez. |
| T02 | Curly apostrophe / mixed quotes | ’ ve gerçek dengeli tek/çift tırnak farklı işlenir. |
| T03 | Real quoted identifier | "BatchJobController" ve 'HakedisHesaplamaJob' exact korunur. |
| T04 | Exact identifier preservation | SCHEMA.PCK_X, A_B, SRC/X.java, #123, SKYRSM-5659 bozulmaz. |
| T05 | Explicit project wins over harness wrapper | Sistem/harness açıklamasındaki Zekam açık gpu-fusion hedefini ezmez. |
| T06 | General / technical general | Merhaba, 2+2 ve genel Spring Batch kavram sorusu proje corpusuna gönderilmez. |
| T07 | Ambiguous project aliases | İki eşleşen kayıt veya belirsiz aile bir netleştirme üretir; tüm projeleri taramaz. |
| T08 | Explicit cross-project comparison | İki açık yetkili hedef karşılaştırılır; ilgisiz üçüncü proje yok. |
| T09 | Port/year not Jira | 9001 port ve 2026 yılı tek başına issue anahtarı değildir; GPU 1234 kısa biçimi korunur. |
| T10 | Read-only wording not mutation | ekle ekranında / oluştur butonu ne yapıyor salt okunur; gerçekten ekle/düzelt ayrı. |
| T11 | Profile cold/warm/new process identity | Aynı kabul edilmiş uzayda deserialize sonrası profile_digest eşit. |
| T12 | Real incompatible profile | Model/dimension/prefix/normalization değişiminde eski dense vector kullanılmaz. |
| T13 | Mismatch no avoidable remote calls | Yerelden bilinen mismatch için query embedding ve tekrarlı qualification sıfır. |
| T14 | Healthy dense canary | Uygun profil ve semantic fixture: dense gerçekten çalışır; forced healthy canary dense>0. |
| T15 | Query-aware source span | 500 karakterden sonra endpoint/validation var; doğru satır aralığı seçilir. |
| T16 | Multi-span exact bytes | Aynı dosyada ayrı iki bölüm ayrı locator taşır; eksiltmeler görünür, metin değiştirilmez. |
| T17 | Budget/noise | Uzun import ve CSS gürültüsü; en fazla 1200 tahmini evidence token, ilgili body korunur. |
| T18 | Missing edge | İki sembolün birlikte bulunması tek başına çağrı/sıra/bağımlılık iddiasını geçirmez. |
| T19 | Real conflict fixture | Aynı scope/revision geçerliliğinde çelişen iki kaynak: çözülmemiş çelişki açıklanır. |
| T20 | Superseded document fixture | Tarih/authority doğrulanmış eski PostgreSQL taslağı güncel SQLite kararı yerine kullanılmaz. |
| T21 | History boundaries | Shallow geçmiş / eksik commit / reverte edilmiş değişiklik açıkça partial/history olarak işaretlenir. |
| T22 | Client state contract | answered, lexical-only-degraded, abstained-no-hit/low-evidence/no-edge/index-unavailable ve timeout tümü testli. |
| T23 | Empty model response | Stop + reasoning/usage var, text/tool yok: başarısız boş çıktı; sessiz PASS yok. |
| T24 | Tool honesty | Aradım ifadesi receipt olmadan başarı sayılmaz; verified_actions sistemden üretilir. |
| T25 | Context admission | 32000 output + 769 input > 32768: provider çağrısından önce blokaj. |
| T26 | Windows prompt round trip | CRLF/LF, Türkçe, tek/çift tırnak, &, |, %, !; exact prompt digest eşleşir. |
| T27 | No-input process termination | stdin kapalı; timeout/cancel process tree cleanup; geç sonuç yayınlanmaz. |
| T28 | Stable evidence vs run identity | Aynı pinned evidence content digest sabit; run_id değişebilir; probe digest cevap hashı değildir. |
| T29 | Security boundaries | Unauthorized project, symlink/reparse escape, injection, SecretRef ve PII sızıntısı fail-closed. |
| T30 | Cache/generation races | Query/reindex CAS, failed publish, binding drift; eski doğrulama yeni generationı ezmez. |
| T31 | No-index useful response | zekam indeksi yok: yapılandırılmış missing-index ve gerçek bakım planı; otomatik reindex yok. |
| T32 | No hidden repeated calls | Aynı turda route, doctor, ask tekrarları yok; refusal alternatif komutla delinmez. |

Her T-vakası test hedefidir, bu dosya hazırlanırken geçmiş bir PASS kaydı değildir. Exact state, citations, selected body, transcript/receipt ve generated answer ayrı değerlendirilir.

### Skor ve ölçüm

Yedi answerable orijinalde (Q1,2,3,4,5,6,10) 7/7 kaynaklı doğru cevap; Q7/Q9da2/2 güvenli negatif; Q8de1/1 yararlı netleştirme; ek varyantlarda4/4 semantik eşdeğerlik hedeflenir. Q1/2/3/10 mandatorydir. Kaynak locator coverage / citation accuracy / ordered-sequence match / unsupported claim / process-honesty ayrı metriklerdir.

Healthy semantic canaryde dense>0 ve gerçek query effect kanıtı gerekir. Exact yeterli her soruda dense>0 isteme; bu tersine gereksiz maliyettir. Synthetic deterministic embeddings semantic kalite kanıtı değildir. Raw harness hatalı koşular kalite paydasından ayrılır ama pipeline reliability tablosunda başarısız/invalid kalır; hata oranı gizlenmez.

Offline her senaryo için 10 warm-up +100 ölçümlü sorgu; cold CLI ve in-process/warm multi-process ayrı. Query süresi içinde qualification, local storage, hydration, model wait ve output parsing ayrı trace spanlarıyla ölçülür; paralel süreler kör toplanmaz. p99 için yetersiz örneklem varsa p99 başarısı iddia etme. Yalnız başarılı sorguları latency tablosuna alıp timeout/abstaini gizleme.

## 7. Ayrı live plan — otomatik çalıştırılmayacak

Bu bölüm bir **önerilen exact kampanya zarfı**dır. Yerelde model/limit/fixture/scope/authorization kimlikleri sabitlenip plan digesti üretilecek. Kullanıcı onayı yoksa canlı çağrı sayısı **0**. Testte kullanılan sıradan mock/local adapter gerçek provider yerine geçmez.

### 7.1 Modeller ve değişmez girdiler

İstenen model adları `Kimi-K2.7-Code` ve `DeepSeek-V4-Flash`. Bunlar otomatik geçerli canonical Model ID sayılmaz. Gerçek authorized inventory/OpenCode catalogdan tam ID çöz; provider prefixini değiştirme. ID veya model limitleri doğrulanamazsa o modelin kampanyasını başlatma; başka modelle sessiz ikame yok. Qwen/GPT-OSS tekrar testi ayrı talep ve bütçe gerektirir.

Sabitlenecekler: local Zekam HEAD, GPU HEAD, source/binding revisionı, generation/profile/normalizer digestleri, OpenCode ve effective config sürümü, model exact ID, tool/agent policy, 14 soru ve prompt digesti, context cap, output reserve, request budget, timeout/cleanup, izinli adapterlar, ağ/disclosure kapsamı ve stop conditions.

### 7.2 Tavanlar

| Kalem | Tavan / hesap |
|---|---|
| Offline transport round-trip | Provider çağrısı0; önce bu geçer |
| Live protocol smoke | Model başına1 session; toplam2 session |
| Kalite koşusu | 2 model ×14 soru =28 session |
| Toplam OpenCode başlangıcı | 30; buna retry dahil değildir, retry izni yok |
| LLM request / smoke | En fazla2; iki model toplam4 |
| LLM request / kalite session | En fazla4; 28×4=112 |
| LLM provider request toplam | En fazla116 |
| Tool call / kalite session | En fazla3; tur başına tekrarlar sayaçta görünür |
| Query embedding | En fazla28 kalite +2 dense canary =30 |
| Qualification provider request | En fazla4, tek onaylı embedding binding için; actual plan daha düşük gerektiriyorsa düşür |
| Document/source embedding | 0; reindex R10un ayrı planıdır |
| LLM input / request | En fazla12000 model token, tüm zorunlu context dahil |
| Kalite LLM output / request | En fazla1800 model token |
| Smoke output / request | En fazla256 model token |
| Güvenlik rezervi | Effective model contextinde en az max(512 token, contextin %5i); sığmıyorsa çağırma |
| Session timeout | En fazla180sn + cleanup için10sn; gerçek mevcut runtime daha sıkıysa onu koru |
| Tekrar / replacement / gizli judge | 0; ayrı exact izin olmadan yok |

Tavanlar ihtiyaç vaadi veya fatura tahmini değildir. Actual provider limits, tool-round planı veya zorunlu agent context bu zarfla uyumsuzsa kullanıcıya plan değişikliği göster; sistem talimatlarını düşürerek veya token sayısını eksik sayarak sığdırma. Bağımsız remote verifier gerekiyorsa onun model/çağrı bütçesi ayrıca planlanmadan çalıştırılmaz.

Smoke başarısız, overflow, prompt-digest mismatch, yetki hatası, boş terminal, provider ID drift veya loop görüldüğünde ilgili modelin kalan koşularını durdur. Başarısız koşu ve harcanmış gerçek requestler bütçede kalır. Rerun kararı yeni exact plan ister.

### 7.3 Sonuç tablosu

Her satırda case/model, valid input, selected scope/layers, effective state, body citation correctness, factual verdict, process-honesty, no-answer/clarify, route/ask/doctor/tool sayıları, qualification/query/LLM request sayıları, input/output tokens, latency, error ve gerçek receipt referansı bulunur. Ham secret veya reasoning transcripti rapora konmaz. Modelin metnindeki aradım beyanı tool receiptinden bağımsız kabul edilmez.

Bu dar kampanya ürünün bütün platform/model testleri veya ZEKAM-DOD-025 yerine geçmez. İzin verilmezse core offline rapor teslim edilir ve live acceptance açık kalır; faz/global DoD bitmiş sayılmaz.

## 8. Güvenlik, veri koruma ve rollback

Kullanıcının proje dosyaları, kaynak kodu, görev kayıtları, configi, belgeleri ve runtime verisi korunur. Değişiklik yalnız bu taskın bağlı gerçek rootunda ve exact plan scopeunda. Source/history/config içinden secret/PII model contextine/loga alınmaz; prompt-injection içerikleri veri olarak kalır, scope veya tool yetkisi üretmez. Read-only yol daha fazla yetki istemek için fallback zinciri kurmaz. [K20,K21]

Rollback item bazlı küçük diff, sürümlü contract/cache/generation ve owned-config backup üzerinden yapılır. `git reset --hard`, `git clean`, toplu rm, kullanıcı configini tamamen replace, indeks silip baştan kurma ve sessiz remote reindex yok. Çalışan eski generation yalnız uyumlu profil ile kullanılabilir; yanlış uzayı zorla current etme. Rollback de effect/claim/receipt kurallarına bağlıdır.

## 9. Tamamlanma ve teslim

Her Work Item için gerçek diff, test komutu/exit code, önce/sonra kanıt, ölçüm ortamı, source/generation/model kimlikleri, verifier sonucu ve rollback bilgisi ver. Yeni docs/golden fixtures kodla tutarlı olsun. Global DoDde eksik maddeler görünür kalacak; sadece görev adını değiştirerek ertelenmeyecek.

Kullanıcıya kısa sonuç ve artefact bağlantıları ver; aynı uzun task/kodu sohbette yeniden basma. Kanıt yoksa tamamlandı deme. Commit mesajı Türkçe anlamlı ve ASCII-only; commit yetkisi mevcut protokolden doğrulanır, push bu görevde yetkili değildir.

## 10. Kaynak referansları

**[BF]** `bulgu_defteri.md`, 29 Eylül 2026; L21–30 gold soru/cevaplar, L38–54 retrieval, L61–89 model/harness, L138–139 yeniden koşu/embedding izin sınırı. **[AP]** `arastirma_prompt.md`; özellikle L69–76 ve L80–100. Yerel raw dosyalar görülmeden bu metinler gerçek request receiptı sayılmaz.

- **[K01] Sabit GitHub revision:** [https://github.com/mehmet-karacan/zekam/commit/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7](https://github.com/mehmet-karacan/zekam/commit/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7). 26 Eylül 2026 22:19:02 UTC tarihli commit; İstanbul takviminde 27 Eylül 01:19. Commit mesajındaki test sonuçları geçmiş beyanıdır, bu araştırmada yeniden çalıştırılmış sonuç değildir.
- **[K02] Teknik kimlik / tırnak ayrıştırma:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/domain/retrieval.py#L31-L72](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/domain/retrieval.py#L31-L72). _IDENTIFIER, _QUOTED_PHRASE ve extract_identifiers; Türkçe kesme işaretleri için karşı örnek.
- **[K03] Alıntının ilk kullanılan parçanın başından seçilmesi:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/embedded_project_rag.py#L73-L150](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/embedded_project_rag.py#L73-L150). MAX_ANSWER_EXCERPT_CHARS=500; _excerpt_window, _build_excerpt ve _tokens.
- **[K04] Bağlam penceresi ve token bütçesi:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/retrieval_service.py#L76-L157](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/retrieval_service.py#L76-L157). _select_window leading window; _is_pure_identifier_lookup.
- **[K05] Intent sınıflandırması ve deadline:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/retrieval_service.py#L279-L405](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/retrieval_service.py#L279-L405). _classify_intent mevcut ve deterministik; exact/semantic/relationship/comparison/ambiguous ayrımı; mevcut RetrievalDeadline.
- **[K06] Dense uyumluluk ve query kapısı:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/embedded_project_rag.py#L370-L500](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/embedded_project_rag.py#L370-L500). Dense devre dışıyken embed_query çağrılmaz; generation/policy/provider profilleri karşılaştırılır; varsayılan context bütçesi 1200.
- **[K07] Kanıt yeterlilik kapısı ve sonuç sözleşmesi:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/embedded_project_rag.py#L500-L800](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/embedded_project_rag.py#L500-L800). Kimlik desteği, lexical coverage, çoklu nesne ve ilişki kapıları; citation bulk hydration; lexical-only-degraded; retrieval-only alanları.
- **[K08] Query yürütme sırası ve kaynak tazeliği:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/project_rag_runtime.py#L2333-L2510](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/project_rag_runtime.py#L2333-L2510). Bounded freshness; state/index binding; _provider çağrısından sonra EmbeddedProjectRAG.query; observational state CAS; probe_evidence_digest ve remote_provider_used.
- **[K09] Önbellekten profil yeniden kurulması:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/project_rag_runtime.py#L1166-L1265](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/project_rag_runtime.py#L1166-L1265). Durable/in-process qualification cache; cached _profile reconstruction; cache hitte remote_provider_used=True, probe_call_count=0.
- **[K10] Açık CLI proje önceliği:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/interfaces/cli/main.py#L204-L251](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/interfaces/cli/main.py#L204-L251). --project varsa resolve_registered_project; yoksa resolve_question_project; retrieval alanı yeniden yorumlanmadan çıktıya girer.
- **[K11] Mevcut deterministik router sözleşmesi:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/request_routing.py#L1-L170](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/request_routing.py#L1-L170). Project family ve kayıtlı proje temelli çözüm; çıktı provider_calls=0, grants_authority=False.
- **[K12] Router proje eşleşmesi ve sayısal Jira adaylığı:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/request_routing.py#L287-L397](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/request_routing.py#L287-L397). Açık metinsel proje eşleşmeleri; bir dört veya daha fazla haneli sayıyla Jira adaylığının açılması.
- **[K13] Router aile ve mutation ayrımı:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/request_routing.py#L460-L600](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/request_routing.py#L460-L600). Family-all-members; bağımsız ekle/oluştur sözcüklerinin değişiklik niyetine dönüşebilmesi.
- **[K14] Coordinator talimatları / tüketici sözleşmesi:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/.opencode/agents/zekam-coordinator.md#L1-L119](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/.opencode/agents/zekam-coordinator.md#L1-L119). Her kullanıcı isteğinde route preview; RAG-first router child gerektirmez; answered için ilk üç citation; diğer durum adlarıyla uyuşmazlık riski.
- **[K15] Model seçimi yetki ve çağrı değildir:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/domain/model_routing.py#L1-L61](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/domain/model_routing.py#L1-L61). Routing nesneleri authority-free; provider çağrıları kararın dışında.
- **[K16] Layered model routing:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/layered_model_routing.py#L1-L185](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/layered_model_routing.py#L1-L185). Mevcut evidence-bound policy / qualification / context arayüzleri; ikinci bir model router kurmak için gerekçe değil.
- **[K17] OpenCode managed dağıtım yüzeyi:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/opencode_agent_bootstrap.py#L1-L57](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/opencode_agent_bootstrap.py#L1-L57). Kullanıcı-genel agent/config/plugin yerleşimi ve managed sahiplik işaretleri. Dosyanın tamamının davranışı bu aralıktan çıkarılmadı.
- **[K18] Yaşayan mevcut görev:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/AKTIF_GOREV.md#L1-L110](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/AKTIF_GOREV.md#L1-L110). zekam-active-task/v2; önceki görevin kapsamı, kaynak inceleme sınırı ve başlangıç disiplini. Eski bulgular bugünkü kodla tekrar eşleştirilmeli.
- **[K19] Aktif görev şeması:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/active_task_contract.py#L21-L225](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/src/zekam/application/active_task_contract.py#L21-L225). İzinli front matter alanları, sabit status, authority/projection ayrımı; yeni dosya bu alan kümesini kullanır.
- **[K20] Global kabul kapıları:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/GLOBAL_DEFINITION_OF_DONE.md](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/GLOBAL_DEFINITION_OF_DONE.md). Teknik, platform, sağlayıcı, güvenlik, bağımsız verification ve kanıt kuralları; birkaç başarılı soru tüm DoD yerine geçmez.
- **[K21] Devam / recovery protokolü:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/DEVAM_PROTOKOLU.md](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/DEVAM_PROTOKOLU.md). Source revision, Work Graph, claim, receipt ve checkpoint; claim var terminal receipt yoksa recovery-required.
- **[K22] Başlangıç otoriteleri:** [https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/AGENTS.md](https://github.com/mehmet-karacan/zekam/blob/f75c6a34cfbb27390ed54a7ffb978bf8d3a444d7/AGENTS.md). Gerçek kaynak kökü, yürütme ve doğrulama protokolünün üst sınırı; 00_BASLA.md ile birlikte yerelde yeniden okunacak.
- **[E01] OpenCode resmî provider dokümantasyonu:** [https://opencode.ai/docs/providers/](https://opencode.ai/docs/providers/). Custom provider için model limit.context / limit.output; gerçek kurulu sürümün şeması esas alınmalı.
- **[E02] Python subprocess resmî dokümantasyonu:** [https://docs.python.org/3/library/subprocess.html#security-considerations](https://docs.python.org/3/library/subprocess.html#security-considerations). Windows .bat/.cmd dosyaları shell=False olsa da kabuk tarafından yorumlanabilir. Tek başına shell=False düzeltme garantisi değildir.
- **[E03] SQLite FTS5 resmî dokümantasyonu:** [https://www.sqlite.org/fts5.html#unicode61_tokenizer](https://www.sqlite.org/fts5.html#unicode61_tokenizer). Unicode61, remove_diacritics ve tokenization; Porter İngilizce içindir. Uygulamanın Python yeterlilik kapısı ayrıca tutarlı olmalı.
- **[E04] Anthropic Contextual Retrieval:** [https://www.anthropic.com/engineering/contextual-retrieval](https://www.anthropic.com/engineering/contextual-retrieval). İndeksleme öncesi parçaya bağlam ekleme yaklaşımı. Bu projeye aynı başarı yüzdeleri aktarılamaz; ilk aşamada deterministik hata düzeltmelerinin yerine önerilmedi.
- **[E05] OpenCode resmî CLI dokümantasyonu:** [https://opencode.ai/docs/cli/](https://opencode.ai/docs/cli/). Headless/run yüzeyi için sürümle eşleştirilecek referans; prompt taşıma biçimi kurulu binary ile doğrulanmadan varsayılmayacak.
