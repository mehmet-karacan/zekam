---
schema: zekam-active-task/v2
task_id: ZEKAM-RAG-PERFORMANCE-CORRECTNESS-001
status: APPROVED_ACTIVE_TASK
title: Zekam RAG Gecikmesi, Kanit Kalitesi ve Uctan Uca Yanit Hattinin Duzeltilmesi
created_at: 2026-09-25T00:00:00+03:00
baseline_repository: mehmet-karacan/zekam
baseline_branch: main
baseline_head: c3ad4c6abf2596cf633f0e95d52c8cd96c18000b
baseline_commit_subject: "bakim: paket dogrulama raporunu commit sonrasi yenile"
baseline_is_fixed_revision: true
legacy_postgresql_data_import: FORBIDDEN
postgresql_runtime_dependency: FORBIDDEN
docker_required_for_zekam_core: false
ui_surface: FORBIDDEN
push_authorized: false
runtime_test_evidence_at_task_creation: NOT_EXECUTED
---

# AKTIF_GOREV.md

## 1. Görev: rapor yazmakla kalma, çalışan düzeltmeyi uygula

Zekam repository'sinde kıdemli performans mühendisi, retrieval mühendisi ve güvenilirlik odaklı uygulama geliştiricisi olarak çalış.

Kullanıcının problemi şudur: Sistem genel olarak yavaş hissediliyor; RAG bazen doğru bilgiyi bulmuyor, bazen çok geç dönüyor, bazen de beklenen nitelikte bir cevap vermiyor. Amaç yalnız birkaç ayarı değiştirmek değil; bu üç şikâyeti ayrı ayrı ölçmek, gerçek nedenlerini düzeltmek ve tekrar oluşmalarını testlerle engellemektir.

Bu dosya uygulanacak görev promptudur. Yalnız inceleme, öneri, mimari şema veya dokümantasyon üretmek tamamlanmış teslim değildir. Aşağıdaki kapsam içinde gerçek kod değişikliklerini, regresyon testlerini ve önce/sonra ölçümünü gerçekleştir. Mevcut çalışan özellikleri koru; ilgisiz bir yeniden yazım veya yeni platform kurma projesine dönüşme.

“Kusursuz” hedefini ölçülebilir olarak ele al: doğru proje ve revision'dan doğrulanabilir kanıt; kontrollü gecikme; açık hata/degraded durumu; veri ve yetki izolasyonu; kaynak tüketimi sınırları; testle korunan davranış. Ölçülmemiş bir sisteme “kusursuz”, “üretime hazır” veya “X kat hızlandı” deme.

### İncelemenin sınırı

Bu görev 25 Eylül 2026 tarihinde GitHub üzerinden sabit revision'ın kaynak kodu incelenerek hazırlanmıştır. Kullanıcının çalışan kurulumunda profil, benchmark, gerçek provider çağrısı veya repository testleri bu hazırlık sırasında çalıştırılmamıştır. Aşağıdaki gecikme mekanizmaları kaynakta görülmüştür; kullanıcının toplam beklemesindeki payları henüz ölçülmemiştir.

Buna tek istisna, B06'daki yuvarlama davranışına ait küçük ve bağımsız sentetik aritmetik karşı örnektir. Bu örnek Zekam entegrasyon testi veya gerçek provider ölçümü değildir.

Son HEAD `c3ad4c6` yalnız `VALIDATION_RESULT.json` güncellemesidir. Hemen önceki `1dfd760` context/memory/skills/orchestration değişiklikleri içerir. Dolayısıyla bütün sorunları “son commit bozdu” diye etiketleme. Yeni değişikliklerin startup/context etkisini ve daha önceden var olan RAG sorgu hattını ayrı değerlendir. [K01, K02]

## 2. Başlangıç ve yetki sınırları

Önce tek seferlik, bounded başlangıç kontrolü yap:

```text
1. git status --short
2. git rev-parse HEAD
3. git show -s --format='%H%n%P%n%s%n%ci' HEAD
4. git cat-file -e c3ad4c6abf2596cf633f0e95d52c8cd96c18000b^{commit}
5. Mevcutsa AGENTS.md, ardından 00_BASLA.md, DEVAM_PROTOKOLU.md,
   GLOBAL_DEFINITION_OF_DONE.md, PROJE_MANIFESTI.yaml ve bu AKTIF_GOREV.md.
6. Mevcut görev/projection üretme ve doğrulama komutlarını repository'den doğrula.
7. python scripts/paket_dogrula.py
```

Başlangıç doğrulamasının sonucunu ve önceden var olan hataları kaydet. Validator'ın ürettiği dosya değişikliklerini kullanıcı değişikliklerinden ayır. Eksik bağımlılık veya platform desteğini test başarısı gibi gösterme.

HEAD baseline'ın ilerisine geçmişse ilgili farkları incele, bu görevi yeni yerleşime kontrollü taşı ve gerçek uygulama SHA'sını raporla. Baseline'ı sessizce değiştirme; kullanıcının branch'ini geri alma. İlgisiz/diverged geçmişte güvenli ilişki kurulamazsa mutasyon yapma, erişilebilen analiz ve uygulanabilir patch planını teslim et. Otomatik reset, checkout, stash, worktree veya proje kopyası üretme.

`AKTIF_GOREV.md` tek aktif görev kaynağıdır. Önceki görevin tamamlanmış işlerini bozma veya yeniden başlatma. Eski görev metninin tarihçesini koru. `AKTIF_GOREV.yaml` türetilmiş projection'dır; elle sahte digest yazma, repository'nin mevcut üreticisini kullan. Bu dosyanın front matter alanları mevcut `ActiveTaskContract` şemasına göre seçilmiştir. [K18]

Değiştirilemez sınırlar:

- Kullanıcının projeleri, kaynak dosyaları, belgeleri, görev kayıtları, yerel veritabanları ve kişisel içerikleri korunacak. “Performans temizliği” adıyla silme, truncate, eski veri importu veya tüm sistemi yeniden kurma yapılmayacak.
- Memory, RAG, context, cache, skill, model önerisi ve telemetry yetki kaynağı değildir. Mevcut plan/authorization/claim/receipt, project/realm scope, source digest, SecretRef ve single-writer kontrolleri korunacak.
- Bu görev kod geliştirme ve güvenli yerel test kapsamıdır. Uzak query embedding yetkisi, kaynakların indekslenmek üzere dışarı gönderilmesi veya model sentezi yetkisi yerine geçmez. Her işlem kendi mevcut açık yetkilendirmesini gerektirir.
- Cache hit, geçmiş authorization'ı yeniden kullanma veya yeni uzak effect yetkisi verme gerekçesi olmayacak. Gerçek provider çağrısı hâlâ mevcut effect ve receipt sınırından geçecek.
- SQLite + FTS5 + sqlite-vec ve mevcut Python/CLI mimarisi esas alınacak. Yeni PostgreSQL/Redis/Docker zorunluluğu, ikinci RAG framework'ü, kontrolsüz GraphRAG veya UI/dashboard/TUI eklenmeyecek.
- Her sorguyu daha pahalı modele yönlendirme, token bütçesini sınırsız artırma, tüm aramaları aynı anda başlatma veya güvenlik eşiklerini topluca düşürme çözüm değildir.
- Commit/push ve agent çalıştırmaları mevcut protokole tabidir. Push yetkisi yoktur. Bağımsız verifier kullanılacaksa mevcut yetkili agent/worker hattı ve tek-yazar ilkesi korunur; uzak agent yetkisi yoksa bağımsız yerel test/inceleme kanıtı kullanılır ve bu sınır raporlanır.

## 3. Kaynakta doğrulanmış bulgular

Aşağıdaki bulguları mevcut HEAD'de yeniden doğrula. Bir davranış daha yeni revision'da düzelmişse tekrar uygulama; hangi testle doğrulandığını kaydet. Kanıt numaraları son bölümde sabit commitli kaynaklara bağlıdır.

### B01 — Soru başına provider qualification tekrar ediliyor

`project_rag_runtime._query()` provider bağını yeniden kuruyor. `_provider()` uzak yolda yeni `OpenCodeRemoteEmbeddingProvider` oluşturup koşulsuz `provider.probe(fixture)` çalıştırıyor. `probe()` iki ayrı `_vectors()` çağrısı yapıyor. Ardından asıl `embed_query()` bir çağrı daha yapıyor. Başarılı ve dense etkin uzak sorguda bu yol iki ön test + bir gerçek query embedding çağrısı demektir. [K03, K04, K05]

Her effect ayrıca mevcut ledger/claim/receipt ve process transport maliyetlerinden geçiyor. Sağlık kontrolünün kendisini yanlış teşhis etme: bu uzak adapter'ın `health()` metodu profile bakıyor; ek HTTP yapan asıl davranış burada `probe()`dur. [K05, K14]

### B02 — Sorgu yolunda proje taraması ve yerel yolda indeks planlama var

`_query()` canlı source revision ve `discover(source_root).tree_digest` hesaplıyor. Yerel embedding yolunda ayrıca `_project_plan()` çağırıp chunk planını oluşturuyor; o da Git/source discovery adımlarına giriyor. `_provider()` yerelde `build_verified_mac_embedding(chunks)` ile kaynak parçalarından yeniden qualification fixture hazırlıyor. [K03, K04, K06]

Bu, soruya cevap vermek için gerekli retrieval işinden ayrı bir maliyettir. Ancak freshness kontrolünü kaldırıp eski veriye “current” demek kabul edilmez.

### B03 — Her indeks açılışında veri büyüklüğüne bağlı kontrol var

Her `_query()` yeni `SQLiteKnowledgeIndex(..., read_only=True)` açıyor. Constructor `_validate_schema()` çağırıyor; burada `PRAGMA quick_check` ve `PRAGMA foreign_key_check` çalışıyor. İsimde “quick” geçmesi sabit maliyet anlamına gelmez: SQLite dokümantasyonu `quick_check` için O(N) davranışını belirtir. [K03, K07, E01]

Bu kontrolleri basitçe silme. Güvenilir generation admission/bakım sınırına ve aynı doğrulanmış dosya kimliğinin yeniden kullanımına ayır.

### B04 — Exact aramada pahalı metin taraması; citation hydration'da tekrar var

`SQLiteKnowledgeIndex.exact()` her identifier için `json_extract`, `lower`, `instr(lower(body), ...)` gibi ifadeler kullanıyor. Mevcut scope indeksi aday kapsamını sınırlar; identifier'ın kendisi için eşitlik/posting indeksi yerine generation içindeki metni değerlendirmek zorunda kalan bir yol var. İlk identifier `limit` kadar sonuç doldurursa daha sonraki identifier'lara geçmeden dönülebiliyor. Etkiyi gerçek sorgu planı ve SQL sayaçlarıyla ölç. [K08]

`EmbeddedProjectRAG.query()` önce `views()` alıyor, sonra her citation için ayrı `source_identity()` çağırıyor. Bu yol ek SQL, digest ve read-boundary kontrolleri üretiyor. Mevcut doğrulamayı koruyarak toplu hydrate etmek mümkündür. [K08, K09]

Önemli karşı bulgu: dense arama zaten `vec0`, `embedding MATCH`, `k` ve project/generation partition filtreleri kullanıyor. “Vektör indeksi yok, önce vektör DB ekleyelim” teşhisi doğru değildir. [K07, K08, E02, E03]

### B05 — Kanallar sırayla ve dense ihtiyaç değerlendirilmeden çalıştırılıyor

`RetrievalService.search()` sırasıyla exact, lexical ve dense çağırıyor. Kesin, yeterli kanıtın bulunduğu basit bir soruda dahi dense açık ise query embedding bekleniyor. Trace sayılar içeriyor ama bu sınıfta aşama süreleri ve ortak query deadline'ı yok. Alt process katmanında timeout/cancellation bulunması, uçtan uca bütçenin zaten yönetildiği anlamına gelmez. [K10, K14]

### B06 — Tolerans içinde kabul edilen vektör farkı profil kimliğini değiştirebilir

Uzak `probe()` normalize vektörleri `round(value * 1000)` ile yuvarlayıp digest'e katıyor. Bu fingerprint, `model_revision_fingerprint` ve `profile_id` üzerinden kalıcı profile identity'ye giriyor. Mevcut profile identity zaten `verified_at` ve probe freshness metadata'sını dışarıda tutuyor; sorun timestamp değil, bu sayısal fingerprint'in süreksizliği. [K05, K11]

Sentetik karşı örnek:

```text
u = (0.00049, sqrt(1 - 0.00049²), 0, ..., 0)  # 1024 boyut, norm 1
v = (0.00051, sqrt(1 - 0.00051²), 0, ..., 0)  # 1024 boyut, norm 1
max_delta ≈ 0.00002 < 0.0005
cosine ≈ 0.9999999998 > 0.99999
round(u[0] * 1000) = 0
round(v[0] * 1000) = 1
```

Dolayısıyla sayısal toleransların içinde kalmak fingerprint eşitliğini garanti etmez. İki probe çalışması kendi içinde başarılı olsa bile birbirinden farklı profile identity üretebilir; generation ile profile karşılaştırması dense kanalını stale/degraded yoluna götürebilir. Bunun kullanıcının gerçek provider'ında gerçekleştiği henüz ölçülmedi. [K05, K09, K11]

### B07 — Bütün teknik adların tek chunk'ta bulunması zorunlu

`_supports_all_identifiers()` ve sonraki hit filtresi bütün technical identifier'ları aynı parçada arıyor. A nesnesini bir dosya, B nesnesini başka dosya açıklıyorsa; karşılaştırma, çağrı zinciri veya birden fazla kaynağa yayılan ilişki sorusu bu filtre yüzünden elenebilir. [K09]

Çözüm global olarak identifier kontrolünü kaldırmak değildir. Tek nesne sorusu ile çoklu nesne/ilişki sorusunun kanıt sözleşmesi ayrılmalıdır.

### B08 — Retrieval başarısı ile üretilmiş cevap birbirine karışıyor

İncelenen `ask` hattı `query_registered_project()` sonucunu döndürüyor. Normal CLI çıktısında `answer_excerpt` yazdırılıyor; bu alan ilk kullanılan chunk'ın ilk 500 karakteri. Bu akışta kaynakları sentezleyen bir LLM çağrısı yok. `answered` burada retrieval/evidence başarısını ifade ediyor; kullanıcıya tam bir üretilmiş cevap verildiğini kanıtlamıyor. Başka client/agent tüketicileri ayrıca sentez yapıyorsa onları ayrı izle. [K03, K09, K12]

Varsayılan 1200 token bütçesinde parçalar bütün hâlinde sığmıyorsa atılıyor. Uygun kanıtı taşımayan 500 karakterlik ilk kesit, diğer citation'ların içerikleri elde edilmiş olsa bile kullanıcıya yetersiz sonuç gösterebilir. [K09, K10]

### B09 — Genel yavaşlık için ayrıca doğrulanacak noktalar

`workspace_resume.build_resume_packet()` içinde `list_projects`, `list_work`, proje bazında alias okumaları ve sonradan uygulanan liste sınırları var. Yeni navigation alanları skill/knowledge erişimi ekliyor; `_active_skill_refs()` record listesini aldıktan sonra sınırlandırıyor. Alt repository metotlarının gerçek limitlerini, bağlantı yaşam döngüsünü ve bu çağrıların hook sıklığını ölçmeden bunları kesin darboğaz ilan etme. Memory ID'lerinin skill ref olarak kullanılmasının doğruluğunu da kontrol et. [K13]

CLI giriş modülü birçok alt komutu import ediyor. Import maliyeti, context'in tekrarlı enjekte edilmesi, ledger/ACL/disk maliyeti, provider cleanup beklemesi ve `rag-state.json` üzerindeki eşzamanlı yazımlar ayrı inceleme adaylarıdır; henüz kullanıcı ortamında kanıtlanmış nedenler değildir. [K03, K12, K13, K14]

### B10 — Var olan sağlam parçaları yeniden icat etme

Canonical indeksleme yolunda eksik chunk'ları işleyen durable vector cache, batch işleme, generation bağlama ve atomik aktivasyon zaten var. Bunları yok sayıp sıfırdan cache/index altyapısı yazma. Capability envanteri büyük ölçek performans doğrulamasını ayrıca açık bırakıyor; işlev testleri başarıyla geçse de hız/kalite kabulü ayrıca ölçülmeli. [K15, K16]

## 4. Uygulama iş paketleri

### WP1 / P0 — Ölçüm ve yeniden üretim

Önce mevcut davranışa test ekle, sonra düzelt. Erişilebilen gerçek kurulum ve temiz geçici test HOME'u için şu yolları ayrı izle:

```text
CLI process başlangıcı/import
  -> project/realm/authorization çözümleme
  -> source freshness
  -> config ve provider binding / qualification
  -> index open / validation
  -> exact / lexical / query embedding / dense
  -> fusion / evidence selection / hydration
  -> context packing
  -> client'e sonuç iletimi
  -> varsa yetkili model sentezi / ilk token / son token
```

Mevcut diagnostic trace altyapısını genişlet; ikinci bir observability framework'ü kurma. Monotonic clock ile en az şu alanları kaydet:

- `request_id`, gerçek route, selected project, generation/profile kimlikleri ve her aşamanın süresi.
- Qualification çağrıları, query embedding çağrıları ve synthesis çağrıları ayrı sayaçlar; cache hit/miss, single-flight beklemesi, provider queue/transport/process süreleri erişilebildiği ölçüde ayrı.
- SQL sayısı, file traversal sayısı, indeks açılışı ve deep-validation sayısı, candidate/final citation sayısı, bütçe nedeniyle atılan kanıt sayısı, bytes/tokens ve peak memory.
- Kanal başına `attempted/completed/skipped/failed/timeout`, fallback nedeni, freshness durumu ve cancellation sonucu. “Dense açık” ile “dense gerçekten başarıyla çalıştı” ayrılacak.

Ham query/source text, credentials, token'lar, bağlantı adreslerindeki secrets ve ham provider yanıtlarını loglama. Trace için güvenli kimlik/digest kullan. Süre/sayaç gibi değişken alanları semantic identity veya authority digest'e kazara katma; mevcut sözleşmeyi sürümlü ve uyumlu genişlet.

Provider'sız mock ölçümleriyle gerçek provider ölçümlerini aynı performans sayısı altında toplama. Model token süresini RAG retrieval süresi diye raporlama. Core hattında generation yoksa TTFT `not_applicable` olacak, sıfır milisaniye gibi gösterilmeyecek.

### WP2 / P0 — Qualification, profil kararlılığı ve query embedding cache

`_provider()` içindeki oluşturma/qualification ile sorgu kullanımını ayır. Aynı doğrulanmış provider kimliği için her soruda iki probe tekrarlanmasın.

Mevcut yerel storage/adapter yapısına uygun, bounded bir qualification kaydı tasarla. Kaydı en az provider/endpoint identity, exact model ID, gerçek bilinen model revision, boyut, dtype/normalization, preprocessing/prefix/tokenizer sözleşmesi, fixture/version, ilgili policy/config revision ve uygun scope'a bağla. Secret değeri saklama. Credential/izin değişimini secret'ın kendisini kaydetmeden mevcut version/identity mekanizmasıyla ele al.

Qualification kanıtını yeni çağrının yetkisiyle karıştırma. Kabul edilmiş, süresi dolmamış kanıt ile provider nesnesini yeniden kurmak mümkün olmalı. CLI her çağrıda yeni process açtığından yalnız Python global dict/LRU ile çözüm tamamlanmış sayılmaz: ayrı CLI process'leri arasındaki sıcak davranışı da doğrula. Mevcut güvenli local state yeterliyse yeni daemon ekleme.

Qualification TTL'si yapılandırılabilir olsun; başlangıç denemesi olarak 5 dakika değerlendirilebilir. Bu bir ölçülmüş doğru değer veya süresiz güven garantisi değildir. Config/revision/policy değişimi TTL'den bağımsız invalidation yapmalı. Süresi dolmuş kayıt, bozuk cache veya uyumsuz binding durumunda yeniden doğrulama bütçeye tabi olacak; süresiz bekleme veya otomatik authorization olmayacak. Qualification süresinin dolması ile indeksin semantik olarak uyumsuz olması ayrı durumlar olacak.

Aynı scope/provider için eşzamanlı qualification ihtiyacını tek uçuşta birleştir. Bekleyen çağrılar kendi deadline'ına tabi olsun. Başarısızlık cache'i kısa ve bounded olsun; auth hatası gizlenmesin, sürekli probe fırtınası oluşmasın.

B06'yı kalıcı düzelt:

1. Yuvarlanmış vektör hash eşitliğini “toleranslı uyumluluk” yerine kullanma.
2. Onaylı/indexte kullanılan profile'a bağlı sabit referans probe vektörleri ve sürümlü compatibility değerlendirmesi kullan; gerçek model revision bilgisi varsa onu esas al.
3. Yeni probe'u yalnız kendi tekrarıyla değil kabul edilmiş referansla da karşılaştır. Referansı her kabulde kaydırarak kümülatif drift'i normalleştirme.
4. Sayısal jitter ile gerçek model/embedding uzayı değişimini ayır. Uyumluluk kanıtlanmadan eski profile digest'ini yeni vektöre yapıştırma.
5. Model, endpoint, boyut, prefix/tokenizer veya gerçek semantik uzay değişiminde güvenli reddet/reindex gereksinimini koru. Eski ve yeni uzayları aynı generation'da karıştırma.
6. Mevcut indekslere geçişi açık migration/qualification planıyla yap; toplu kör reindex'i ön koşul hâline getirme.

Ayrı bir query embedding cache ekle/uyarla. Anahtarı query'nin güvenli fingerprint'i, embedding amacı (`query`), profile/space kimliği, preprocessing ve authorization/data scope'u kapsasın. Query embedding ile doküman embedding cache'ini amacı yok sayarak birleştirme. Sonuç cache'i kullanılacaksa ayrıca project, generation, freshness/evidence policy ve context bütçesi bağlanmalı; ilk aşamada sonuç cache'i zorunlu değildir.

Hedef sayaç sözleşmesi: aynı kabul edilmiş sıcak binding'de qualification için 0 çağrı; benzersiz semantic query için en fazla 1 query embedding; geçerli query cache hit'inde 0 query embedding. Exact fast path güvenle yeterli ise uzak provider'a hiç gitmemeli. İlk cold qualification ayrı raporlanmalı.

### WP3 / P0 — Query yolundan indeksleme işini çıkar; freshness'ı doğru tut

Normal soru cevaplama `_project_plan`, bütün corpus'u chunk'lama, doküman embedding'i, Oracle metadata toplama, ODI parsing veya reindex çalıştırmayacak. Yerel query provider'ının qualification için bütün proje planını istemesini kaldır; kabul edilmiş bounded qualification fixture/kimliğini kullan.

Freshness için kayıtlı source manifest, Git revision, mevcut change detection ve generation metadata'sını kullan. Git HEAD tek başına yeterli değildir: dirty/untracked dosyalar ve commit olmadan içerik değişiklikleri hesaba katılacak. Özellikle aynı `git status` metniyle dosya içeriğinin değişebileceğini test et.

Güvenilir incremental invalidation varsa yalnız değişen dosyaların digest'lerini yenile. Watcher/change journal yoksa ya da taşma/kesinti olduysa sessizce “current” üretme. Metadata-only karşılaştırmasının kanıtlayamadığı durumda `last-indexed-snapshot`, `freshness-unknown` veya mevcut eşdeğer açık durumu kullan; strict canlı doğrulama ihtiyacını ayrı ve bounded yola taşı.

Güncel kaynak hakkında iddia üretmeden önce kullanılan citation'ların gerekli source/content doğrulamasını gerçekleştir. TTL, sadece dosya boyutu veya mtime eşitliğini kriptografik içerik eşitliği gibi sunma. Silinmiş/izinleri değişmiş/symlink veya junction olmuş kaynağı eski cache üzerinden yetkisizce döndürme.

Yeni generation yayınlanmasıyla cache invalidation tek bir tutarlı kimliğe bağlansın. Sorgu başında generation pinle; kanal aramaları ve hydration boyunca farklı generation'ları karıştırma. Sorgu, indeks güncelleme işi bitene kadar gereksiz kuyruğa girmesin; ama mevcut immutable/offline checkpoint ve single-writer sözleşmesi ihlal edilmesin.

### WP4 / P0 — SQLite okuma yolu ve exact lookup

Şema sürümü/scope kontrolü, güvenilir snapshot admission, deep integrity ve seçilen citation doğrulamasını farklı sorumluluklara ayır.

Aynı güvenilir dosya/generation kimliği için her sorguda `quick_check`/`foreign_key_check` tekrarı olmasın. Bunu yaparken mevcut file identity, ACL, sidecar ve read-boundary korumalarını koru. Deep check'i yeni generation yayınlama, ilk güvenilir kabul, dosya kimliği değişimi, recovery ve açık audit sınırlarında sürdür. Değişmiş/bozuk/kanıtsız dosyaya cache nedeniyle geçerli muamelesi yapma.

Sadece bir boolean `validated=True` veya dosya yolu anahtarlı süresiz cache yeterli değildir. Güvenilir generation/file identity, schema/engine version, verification evidence ve invalidation birbirine bağlı olacak. Böyle bir güvenli kabul kanıtı yoksa strict doğrulama yolu kalacak; güvenlik atlanarak hedef tutulmuş sayılmayacak.

`immutable=1` salt-okunur açılışını canlı mutasyon yapılan dosyaya körlemesine yayma. WAL/journal'ı okuyucu adına silme/checkpoint etme. Mevcut SQLite sürümü/journal safety politikasını koru; tüm veritabanlarına topluca WAL veya `synchronous=off` uygulama. [E01, E04]

Exact arama için proje/generation scope'lu normalize identifier/object/path lookup veya posting indeksi kullan. Normal form şemasını indekslemede üret; her candidate body üzerinde tekrar `lower/json_extract/instr` çalıştırmayı ana yol olmaktan çıkar. Tam nesne eşleşmesi, path eşleşmesi ve metin içi mention farklı kanıt türleri olsun. Kısmi substring'i gerçek exact eşleşme diye puanlama.

Birden fazla identifier varsa ilk adın limit'i tüketip diğerlerini aç bırakmasını engelle: bounded per-identifier aday bütçesi ve birleştirme uygula. Son sıralamada deterministik davranış ve toplam aday üst sınırı korunacak.

FTS expression ve tokenization sözleşmesini gözden geçir. Python `\w+`/casefold ile `unicode61` aynı davranışı garanti etmez. Türkçe İ/ı/ş/ğ, snake_case, CamelCase, qualified package/procedure adları, slash/dot içeren path'ler ve hata kodları için ortak ve test edilmiş normalizasyon uygula. Orijinal identifier'ı ve citation metnini değiştirme. Bütün kelimeleri OR'lamak yerine query intent/önemli terim ayrımını bounded biçimde iyileştir; gerçek recall ölçmeden bütün kelimeleri AND'e çevirme. [E05]

Dense tarafta var olan `vec0 MATCH + k + project/generation partition` yolunu koru. Scalar distance ile bütün vektörleri Python'a taşıyan bir yola gerileme. Vektör motoru veya quantization değişimini ancak bu katmanın gerçekten baskın olduğu ölçülür ve kalite korunursa ayrı sınırlı deney olarak değerlendir.

Citation views + source identity + locator + content doğrulamasını toplu okuma içinde birleştir. Aynı metni aynı pinned request içinde gereksiz tekrar hash'leme; güvenli doğrulama sonucunu request-local kullan. Tüm corpus'u hydrate etme. SQLite connection'ını thread'ler arasında kontrolsüz paylaşma; snapshot ve connection yaşam döngüsü açık olsun.

### WP5 / P0 — Sorgu bütçesi, kontrollü kanal seçimi ve hata davranışı

İlk sürümde pahalı bir LLM planner zorunlu kılmadan basit query intent ayrımı kur: tek nesne/exact lookup; semantic açıklama; çoklu nesne/karşılaştırma; ilişki/çağrı zinciri; belirsiz soru.

Exact/lexical sonuç, o intent'in bütün kanıt ihtiyacını karşılıyorsa evidence gate'den sonra erken dön. Bir isim bulunmasını sorunun bütünü cevaplandı sanma. Açıklama/ilişki sorusunu yalnız exact hit var diye kısa kesme.

Semantic soru dense gerektiriyorsa query embedding ile bağımsız yerel lexical işi, güvenli connection/worker sınırlarında ve sınırlı concurrency ile örtüştürülebilir. Her şeyi sınırsız parallel yapma. Mevcut framework'e uygun minimum değişikliği tercih et.

Tek monotonic uçtan uca retrieval deadline'ı oluştur; discovery/binding, qualification, transport, SQL, reranker, fallback ve cancellation kalan bütçeyi paylaşsın. Her alt katmanın kendi süresini baştan başlatmasıyla toplam bekleme büyümesin. Alt worker'daki mevcut hard timeout, process-tree cleanup ve late-result suppression korunacak. 10 saniyelik cancellation grace'in retrieval tail latency'ye etkisini ayrıca ölç; kısaltma ancak cleanup güvenliği test edilerek yapılabilir. [K14]

Retry yalnız mevcut sınıflandırmanın izin verdiği geçici hatalarda, kalan deadline ve effect/receipt sözleşmesiyle bounded olsun. Auth/policy/dimension hatasını retry etme. Timeout sonrasında gelen sonucu yayınlama. İptal edilmiş görev process/connection/lock sızdırmasın.

Degraded dönüş mevcut kanıt kalitesini düşürmeden yapılacak: provider yoksa güçlü exact/lexical kanıt dönebilir; kanıt yetersizse açık abstain. Boş cevap, “None”, sessiz genel model cevabı veya sahte başarı dönme. No-hit, low-evidence, unavailable, timeout, stale snapshot ve authorization denied anlamlarını karıştırma.

### WP6 / P0 — Çoklu kaynak kanıtı ve context kalitesi

Tek nesne sorularında mevcut sıkı identifier/source doğrulamasını koru. Çoklu nesne veya karşılaştırmada doğrulanmış kanıt kümesi, gerekli nesneleri birlikte kapsayabilsin; hepsinin tek chunk'ta bulunması şartı kalksın.

İlişki iddiası için yalnız A ve B'nin ayrı ayrı bulunması yeterli değildir. Çağrı, veri akışı veya bağımlılık söyleniyorsa bunu destekleyen source/call-site/metadata edge kanıtı da bulunmalı. Sadece isim varlığından edge uydurma. Graph kullanılacaksa mevcut graph/source revision bağını kontrol et, yalnız ilgili intent'te bounded expansion uygula; varsayılan graph-off politikasını bütün sorular için açma.

Dense top-2 margin'i yorumlamadan önce aynı içerik/near-duplicate adayların etkisini ölç. Sabit 0.49/0.04/0.50 eşiklerini keyfî değiştirme; ayrılmış değerlendirme kümesinde intent bazında kalibre et. Kaliteyi metrik uğruna no-answer davranışını gevşeterek artırma.

Context packing'de:

- En ilgili ve zorunlu kanıta bütçe ayır; iki nesneli soruda yalnız ilk nesne bütün bütçeyi tüketmesin.
- Büyük chunk'ı bütçeye sığmıyor diye tamamen kaybetmek yerine yapı/satır/symbol sınırıyla ilgili pencere seç; gerektiğinde komşu parça/parent bilgisini bounded biçimde ekle.
- Gösterilen excerpt'in locator ve digest ilişkisini doğru belirt. Tam kaynak digest'i ile türetilmiş excerpt digest'ini karıştırma; keyfî kesilmiş metne yanlış satır aralığı ekleme.
- Model context kapasitesi, output rezervi ve güvenlik/authority bağlamı dikkate alınsın. 1200 token'ı körlemesine büyük sayıya yükseltmek yerine kullanılan/atılan kanıtı açık ölç.
- Kaynak içerikleri untrusted data olarak paketlensin; dosya içindeki “önceki talimatları yok say” gibi metinlerin tool veya authorization üretmesine izin verme.

### WP7 / P1 — Retrieval çıktısı ile model cevabını ayır

Önce bütün gerçek tüketicileri takip et: CLI `ask`, project query komutu, ilgili client hook/MCP/agent context kullanımı. Var olmayan bir HTTP/UI katmanı uydurma. Retrieval çağrısından sonra client zaten bir model çalıştırıyorsa ikinci bir gizli model sentezi ekleme.

İki açık çalışma biçimi sağla:

**Retrieval-only / agent context:** Doğrulanmış, bounded evidence packet döndür. Seçilmiş parçaların kullanılabilir metinleri, citation kimlikleri, provenance, score/selection trace ve freshness bilgileri mevcut consumer'a ulaşsın. Yalnız ilk 500 karakteri “cevap” olarak sunma. Bu yol generation model çağrısı yapmaz ve bunu açık belirtir.

**Açıkça yetkilendirilmiş cevap üretimi:** CLI'da gerçekten sentez isteniyorsa mevcut model gateway/routing/authorization/claim/receipt hattıyla tek bounded synthesis çağrısı yap. Yeni doğrudan HTTP istemcisi ve ikinci credentials yönetimi ekleme. Bu yeni çalışma biçiminin option/şema adlarını mevcut CLI sözleşmesiyle uyumlu tasarla; bu dosyada önerilen adları zaten mevcut komutlar gibi kullanma. Sentez izni query embedding izninden ayrı ele alınacak.

Üretilmiş cevap yalnız sağlanan kanıtlara dayanmalı; kullanılan kaynakları göstermeli, karşılaştırmada iki tarafın kanıtını taşımalı, kaynakta olmayan bilgiyi tamamlıyormuş gibi yazmamalı. Çelişkili/yetersiz kanıtta belirsizliği belirtmeli. Citation ID/locator doğruluğunu deterministik kontrol et; cümlelerin gerçekten desteklenmesini ayrı groundedness değerlendirmesiyle ölç. Sadece geçerli citation ID olması doğruluk kanıtı değildir.

`retrieval_state`, `generation_state`, `answer_kind`, `evidence_found` ve mevcut `answered` anlamı ayrılacak. V1 tüketicileri sessizce kırma; gerekli yeni contract için sürümlü/uyumlu geçiş ve test sağla. Provider'sız/izinsiz durumda üretilmiş cevap varmış gibi davranma.

Normal `--json` çıktısı tek geçerli JSON document olarak kalacak. Streaming desteklenecekse yalnız açık opt-in, sürümlü JSONL/event sözleşmesiyle; stdout'a progress/log karıştırma. İlk evidence hazır olma, ilk token ve tamamlanma sürelerini ayrı ölç. Hız için yalnız cevabın ekrana parça parça yazılmasını iyileştirmek, yavaş retrieval'ı çözmüş sayılmaz.

### WP8 / P1 — Genel uygulama gecikmesi, resume ve indeks bakım yolu

B09'daki noktaları profile et. `resume` ve yaygın hook'lar için SQL sınırlarının gerçekten repository katmanında uygulandığını doğrula; Python'da sonuçları sonradan kesmek tüm veri okunmasını engellemiyorsa bounded sorgu ekle. Alias/note/skill erişiminde N+1 ve gereksiz schema/DB açılışlarını azalt. Memory kayıtlarını skill kimliği diye sunan yanlış eşlemeyi varsa düzelt. Hataları sessizce yutup boş context verme yerine sanitised diagnostics üret.

CLI `--help`/`--version` ve basit sorgu komutlarının cold import maliyetini ölç. Ağır ve ilgisiz alt modülleri ancak ölçüm haklı çıkarıyorsa lazy composition ile ayır; help/command registration/authorization registry/Windows davranışını bozma. Örneğin mevcut interpreter ile `python -X importtime -c "import zekam.interfaces.cli.main"` kullanılabilir; tam uygulama import sonucunu provider latency ile karıştırma.

Session/context injection sayacını ekle. Aynı session'da aynı resume/context paketini her tool/hook turunda yeniden ekleme; değişen revision/checkpoint ve mevcut context bütçesiyle bağlı kullan. Doctor/full audit'in yanlışlıkla sık çağrılan yola girdiğini trace doğrularsa onu ayır; sadece dosya adı veya import gördüğün için çalıştığını varsayma.

Canonical `_index` yolundaki durable vector cache'i koru. Cache anahtarındaki chunk identity'nin alakasız commit veya aynı içeriğin yeniden planlanması nedeniyle gereksiz misses üretip üretmediğini test et. Uyumlu embedding uzayında değişmemiş içerik gereksiz re-embed edilmesin; locator/generation kimlikleri ayrı ve doğru güncellensin. Batch limitleri ve provider kapasitesine bağlı bounded concurrency kullan; aynı SQLite yazıcısını paralel worker'lara kontrolsüz açma.

İndeks yayınlanırken mevcut generation çalışır kalmalı veya mevcut read contract güvenli ve açık unavailable davranışı vermeli; hiçbir sorgu kısmi build görmemeli. Rag state/generation/manifest güncellemelerinin crash ve race davranışını test et. `_query()` başarılı verification sonrası tüm `rag-state.json` belgesini yeniden yazıyorsa eşzamanlı reindex'in yeni state'ini ezmesini engelle: revision-bound compare-and-set veya ayrı observational kayıt kullan. Telemetry/counter yazımını authoritative generation güncellemesiyle karıştırma.

Disk büyümesini generation/cache retention ölçümüyle raporla. Bu görev, kullanıcı verisini veya eski generation'ları otomatik silme izni değildir; gerekiyorsa ayrı dry-run cleanup planı üret, varsayılan olarak çalıştırma.

## 5. Test ve ölçüm sözleşmesi

### Test verisi ve gerçekçilik

Mevcut testleri genişlet. Gerekirse en az 80 açık etiketli örnekten oluşan bir başlangıç corpus'u kur: tek nesne/exact, Türkçe/İngilizce semantic, çoklu kaynak/ilişki ve no-answer/çelişkili kanıt kategorilerinde en az 20'şer örnek. Tune ve holdout kümelerini ayır. Örnekler sentetikse bunu belirt; gerçek proje cevap anahtarını kaynak ve revision ile doğrula.

Mevcut `GoldenCase` boş relevant set kabul etmiyor. Negative/no-answer vakaları desteklemek için mevcut evaluator'a uyumlu ek tür veya ayrı evaluator ekle; olmayan nesne sorularını test dışına itme. [K10]

Performansı 1.000, 10.000, yaklaşık 20.000 ve 50.000 chunk ile veya mevcut ortamın belgelenmiş sınırlarına göre ölç. 50.000 mevcut generation üst sınırıdır; bu görev kapsamında 250.000 destekleniyor iddiası üretme. Kod yorumundaki yaklaşık 20,5 bin örneğini kullanıcının güncel ölçülmüş corpus büyüklüğü gibi sunma. [K07]

Cold CLI process, aynı process warm ve farklı CLI process'lerinde warm qualification/cache ayrı senaryolar olacak. Tekrarlanan ve benzersiz sorular; 1/4/8 eşzamanlı istek; query sırasında indeks yayını; provider slow/down; stale source ve büyük yerel work/memory geçmişi kapsanacak.

Provider'sız performans testinde gerçek SQLite/FTS/vec0 yolunu kullan; embedding'i deterministik fixture ile değiştirmen semantik kalite kanıtı oluşturmaz. Gerçek semantic kalite için onaylı/cached gerçek embedding corpus'u veya izinli gerçek provider koşusu ayrı gerekir. Uzak çağrı bütçesi/izni yoksa bu koşuyu `NOT_EXECUTED` işaretle; mock sonucuyla yerini doldurma.

### Zorunlu regresyon matrisi

| Vaka | Beklenen kanıt |
|---|---|
| Yeterli tek-nesne exact cevap | Kanıt doğrulandıktan sonra 0 qualification / 0 query embedding; aynı doğru kaynak |
| Warm benzersiz semantic query | 0 qualification, en fazla 1 query embedding; gerçek dense çalışması trace'de görülür |
| Warm tekrar query | Geçerli embedding cache ile 0 query embedding; scope/profile/freshness kontrolleri korunur |
| Ayrı CLI process'lerinden aynı binding | Qualification cache gerçekten yeniden kullanılır; process-local başarıyla sınırlı kalmaz |
| Eşzamanlı cold binding | Tek qualification işi; bekleyenler bounded; authorization scope karışmaz |
| Yuvarlama sınırındaki sayısal jitter | B06 karşı örneği kabul edilmiş uzay uyumluluğunu gereksiz stale yapmaz |
| Gerçek model/endpoint/dimension/prefix değişimi | Yanlış eski vector cache kullanılmaz; açık incompatible/degraded sonuç |
| İki farklı dosyadaki nesne karşılaştırması | Her nesne için doğru citation; tek chunk zorunluluğu nedeniyle yanlış abstain olmaz |
| A ve B var ama ilişki kanıtı yok | Çağrı/bağımlılık ilişkisi uydurulmaz |
| Corpus'ta olmayan teknik nesne | False exact/unsupported generated answer üretmez |
| Büyük chunk ve küçük context bütçesi | Gerekli bölüm doğru locator ile seçilir veya açık low-evidence; yanlış tam cevap yok |
| Provider timeout/cancel | Deadline, cleanup, late-result suppression; sızıntı ve sahte receipt yok |
| Aynı dirty Git status altında dosya içeriği değişikliği | Eski kanıta yanlış current etiketi verilmez |
| Watcher kaybı, metadata-only belirsizlik, kaynak silinmesi | Freshness-unknown/snapshot veya strict doğrulama; sessiz güncel kabul yok |
| Bozuk indeks veya sidecar/file identity değişimi | Cache güvenliği bypass etmez; fail-closed davranış korunur |
| Query ile reindex/state update yarışı | Pinned generation tutarlılığı; yeni state'in eski query tarafından ezilmemesi |
| Aynı soru, farklı project/realm/izin | Cache ve citation sızıntısı yok; unauthorized provider çağrısı yok |
| Türkçe harfler, underscore/dot/path/camel adları | Normalize arama ve exact identity sözleşmesi tutarlı |
| Kaynak içine gömülü prompt injection | Retrieval data'sı authority, tool call veya uzak disclosure izni üretmez |
| `ask --json`, resume ve mevcut tüketiciler | Geriye uyumlu parse; stdout temiz; retrieval/synthesis anlamı doğru |
| Büyüyen work/memory geçmişi | Resume sorguları ve bağlantı sayısı bounded; sadece çıktı listesini kesme yok |

### Performans hedefleri: ölçülmüş sonuç değil, başlangıç kabul adayı

Aşağıdaki eşikler önceden elde edilmiş başarı iddiası değildir. Referans makine/OS, disk, Python/SQLite/sqlite-vec sürümü, corpus, source freshness modu, kullanılan model/route ve örnek sayısını kaydet. Eşik değişecekse gerekçeyi önce/sonra ölçümüyle açık yaz; sessizce gevşetme.

| Ölçüm | Başlangıç hedefi |
|---|---|
| ~20 bin chunk, warm yeterli exact/lexical core retrieval | p95 ≤ 300 ms; CLI cold import ve açık strict tam source audit hariç, bunlar ayrıca raporlanır |
| ~20 bin chunk, warm hybrid yerel orchestration/storage maliyeti | Kritik yolda haricî embedding beklemesi dışında p95 ≤ 500 ms |
| Warm semantic query | Qualification tekrarları 0; gerekli query embedding çağrısı ≤ 1 |
| Warm query/cache path | Tam proje planı/chunking/document embedding 0; aynı güvenilir index identity için tekrarlı deep validation 0 |
| Warm `resume` | Referans yerel iş/memory yükünde p95 ≤ 300 ms; gerçekten okunan satır/sorgu sayısıyla birlikte |
| Patolojik tekrar giderilen senaryolar | Kalite/güvenlik gerilemeden p95'te anlamlı düşüş; başlangıç hedefi en az %50. Baseline zaten küçükse overhead/counter kanıtı kullan |
| Tüm retrieval çağrıları | Yapılandırılmış ortak deadline + açık cleanup sınırı; gözlenen p99/timeout ayrı |
| Curated exact/scope/citation integrity vakaları | %100 beklenen deterministik davranış; “tüm gerçek dünyada %100 doğruluk” anlamına gelmez |
| Holdout retrieval kalitesi | Recall@10, MRR, nDCG@10 baseline'dan gerilemez; çoklu kaynak vakalarında belirlenen hata düzelir |
| Curated no-answer ve güvenlik vakaları | Desteksiz başarı, uydurma citation ve çapraz scope sızıntısı 0 |

Haricî provider toplam süresi için erişim olmadan evrensel saniye garantisi koyma. Yerel overhead ile provider beklemesini trace kritik yolundan ayır; paralel aşamaların sürelerini toplayıp veya toplamdan körlemesine çıkarıp yanıltıcı sayı üretme. Synthesis varsa ilk evidence, TTFT, completion, output uzunluğu ve model maliyeti ayrı raporlanacak.

p50/p95 için yeterli örnek kullan; başlangıç olarak senaryo başına en az 100 ölçümlü istek ve ayrı warm-up uygula. Güvenilir p99 iddiası için en az 1.000 ölçümlü örnek veya açıkça daha düşük güvenli tahmin etiketi kullan. Küçük örneklemden anlamlıymış gibi p99 ilan etme. Canlı provider çağrılarını bu sayıya tamamlamak için izin/maliyet sınırını aşma. Timeout, hata ve abstain isteklerini latency dağılımından sessizce çıkarma; başarı oranıyla beraber göster.

## 6. Uygulama sırası ve değişiklik disiplini

Önerilen sıra:

```text
WP1 baseline + failing regression
  -> WP2 qualification/cache/profile düzeltmesi
  -> WP3 query/indexing ayrımı
  -> WP4 storage/exact/hydration
  -> WP5 deadline ve kanallar
  -> WP6 evidence/context
  -> WP7 tüketici sözleşmesi ve izinli synthesis
  -> WP8 ölçülen startup/resume/index maintenance sorunları
  -> karşılaştırmalı benchmark + bağımsız doğrulama + paket doğrulama
```

Büyük tek patch yerine her iş paketinde sınırlı değişiklik ve odaklı test çalıştır. Mevcut test yollarını repository'den bul; hayalî komutları çalışmış gibi listeleme. Unit/integration/e2e/security/architecture ayrımını koru. Mümkünse hatayı düzelten testin baseline'da başarısız, yeni kodda başarılı olduğunu göster.

İkinci bir serbest görev listesi veya kapsamı genişleten “bilişsel mimari v3” planı üretme. Bu dosyadaki performans, correctness ve cevap hattı kapsamı yeterlidir. Temel source/authorization semantics ile çelişen optimizasyonu uygulama; yerine aynı hedefe ulaşan güvenli dar çözümü seç.

Tamamlanamayan bir WP varsa nedeni ve kalan test/dış erişim ihtiyacını açık yaz; yapılan güvenli düzeltmeleri yine teslim et. Test ortamı yok diye ölçüm sonucu uydurma veya yalnız doküman değiştirip tamamlandı deme.

## 7. Tamamlanma kriterleri ve son teslim

Görev ancak şu çıktılarla kapanabilir:

1. Her doğrulanmış bug için kod değişikliği veya “yeni HEAD'de zaten düzelmiş” test kanıtı; her hipotez için ölçülen sonuç veya açıkça doğrulanamadı notu.
2. Kaynakta tarif edilen kritik üç gecikme tekrarı için sayaç kanıtı: soru başına qualification, query'de source planlama ve aynı index identity'de deep check.
3. B06 ve B07 için deterministik regression; gerçek drift ve ilişki uydurma güvenliğinin korunduğu negatif testler.
4. Retrieval-only ve üretilmiş cevap davranışının açık ayrımı; aktif consumer'a yeterli ve bounded kanıtın gerçekten ulaştığının uçtan uca testi.
5. Aynı corpus/ortam altında önce/sonra latency, çağrı sayısı, kaynak tüketimi ve kalite tablosu. Synthetic/mock/live koşular birbirinden ayrı.
6. Mevcut güvenlik, single-writer, authority isolation, read-only index, no-UI ve JSON consumer testlerinde regresyon olmaması.
7. Geriye uyum/migration/config default'ları ile geri alma adımları. Rollback, kullanıcının kaynaklarını veya eski verilerini silmeye dayanmayacak.
8. Repository'nin kanonik validation/projection/release digest akışıyla son doğrulama. Generated raporları elle “passed” yapma; değişen authority dosyasından projection'ı gerçek üreticiyle yeniden üret.

Repository'nin mevcut doküman yerleşimine uygun tek sonuç raporu ve makine okunur benchmark kanıtı bırak. Yeni bir aktif görev adı kullanma; görev otoritesi `AKTIF_GOREV.md` olarak kalır. Çıktılarda en az gerçek HEAD, değişen dosyalar, çalıştırılan komut/exit code, before/after metrikler, maliyet/izin sınırları, kalan riskler ve rollback bulunmalı.

Son kullanıcı özetini şu sorulara cevap verecek biçimde yaz: “Neden yavaştı?”, “Neden bazen yanlış/eksik görünüyordu?”, “Ne değişti?”, “Ne kadar iyileşti ve nasıl ölçüldü?”, “Hangi doğrulama henüz yapılmadı?”.

## 8. İnceleme kaynakları

Koddaki bütün referanslar aksi belirtilmedikçe şu sabit revision içindir:

```text
https://github.com/mehmet-karacan/zekam/tree/c3ad4c6abf2596cf633f0e95d52c8cd96c18000b
```

Kaynak referansındaki satırlar GitHub kaynak dosyasının satır aralığıdır; uygulayıcı güncel HEAD'de fonksiyon adını esas alarak karşılaştırmalıdır.

| Ref | İncelenen kaynak ve bölüm |
|---|---|
| K01 | `c3ad4c6abf2596cf633f0e95d52c8cd96c18000b` commit metadata/diff; yalnız `VALIDATION_RESULT.json`; 24.09.2026 23:43:57 UTC = 25.09.2026 02:43:57 İstanbul |
| K02 | `4629e9e58f8a74bbaad1e76e362893628837c823...1dfd76059aa8492b74b09486b2649329ca4a1e79` compare dosya değişim listesi; `workspace_resume`, `context_compiler`, cognitive checks ve ilgili testler |
| K03 | `src/zekam/application/project_rag_runtime.py`, satır 1500–son; `_query`, `query_registered_project`, query verification/state yazımı |
| K04 | Aynı dosya, satır 1–270 ve 320–620; `_git_source_state`, `_project_plan`, `_provider`, binding/cache altyapısı |
| K05 | `src/zekam/infrastructure/embedding/opencode_remote.py`, satır 290–son; `_vectors`, `probe`, `_embed`, `health` |
| K06 | `src/zekam/application/local_embedding_composition.py`; source fixture seçimi ve `build_verified_mac_embedding` |
| K07 | `src/zekam/infrastructure/sqlite/knowledge_index.py`, satır 1–270 ve 300–545; constructor, schema, `_validate_schema`, read-boundary ve generation limiti |
| K08 | Aynı dosya, satır 600–960; `exact`, `lexical`, `dense`, `views`, `source_identity`, readiness |
| K09 | `src/zekam/application/embedded_project_rag.py`, satır 1–260 ve 255–son; provider/evidence gate, tek-chunk identifier filtresi, citation ve excerpt çıktısı |
| K10 | `src/zekam/application/retrieval_service.py`; sıralı search, build_answer, token budgeting, GoldenCase/evaluator |
| K11 | `src/zekam/application/embedding_provider.py`, satır 1–270; profile identity, validation ve policy sözleşmesi |
| K12 | `src/zekam/interfaces/cli/main.py`, satır 1–200 ve 210–355; eager import listesi, resume/ask ve CLI çıktı yolu |
| K13 | `src/zekam/application/workspace_resume.py`, satır 1–310; bounded projection ve ek navigation erişimleri |
| K14 | `src/zekam/infrastructure/process/capability_worker.py`, satır 1–270; deadline, cancellation grace ve process cleanup. `opencode_remote.py` satır 1–290: effect/receipt doğrulama |
| K15 | `src/zekam/application/project_rag_runtime.py`, satır 1100–1420; canonical `_index`, durable vector cache, eksik batch'ler ve generation aktivasyonu |
| K16 | `docs/ZEKAM_YETKINLIK_ENVANTERI.md`; readiness sınırları, RAG büyük ölçek performans kampanyası ve graph değerlendirmesi |
| K17 | `README.md` ve baseline `AKTIF_GOREV.md` satır 1–135; CLI-only, aktif görev, authority ve güvenlik sınırları |
| K18 | `src/zekam/application/active_task_contract.py`, satır 1–230; izin verilen front matter alanları ve task/projection sözleşmesi |

Dış teknik doğrulama kaynakları, erişim tarihi 25.09.2026:

```text
E01 — SQLite PRAGMA quick_check / integrity / foreign_key_check
https://www.sqlite.org/pragma.html#pragma_quick_check

E02 — sqlite-vec KNN MATCH + k ve distance metric sözleşmesi
https://alexgarcia.xyz/sqlite-vec/features/knn.html

E03 — sqlite-vec partition key ve metadata filtreleri
https://alexgarcia.xyz/sqlite-vec/features/vec0.html

E04 — SQLite URI immutable davranışı ve dosya değişmezliği varsayımı
https://www.sqlite.org/uri.html

E05 — SQLite FTS5 unicode61 tokenizer davranışı
https://www.sqlite.org/fts5.html
```

Bu kaynaklardaki güncel API/özellikleri repository'nin kurulu sürümü desteklemeyebilir. Uygulama öncesi pyproject/lockfile ve çalışma zamanındaki sürümleri doğrula; mevcut bağımlılıkları gerekçesiz yükseltme.
