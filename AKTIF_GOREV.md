# Zekam Aktif Görev Projection'ı

> Bu dosya insan okunur projection'dır. Zekam runtime kurulduktan sonra kanonik durum
> PostgreSQL Work Graph'tır. Çelişkide `AKTIF_GOREV.yaml`, Work Graph ve receipt kayıtları
> birlikte doğrulanır.

## Aktif iş

| Alan | Değer |
|---|---|
| Work ID | `ZEKAM-BOOTSTRAP-001` |
| Durum | `blocked` (1 DoD kriteri gerçek model sağlayıcı girdisi bekliyor) |
| Amaç | Zekam'yi boş repository'den Global Definition of Done tamamlanana kadar uygulamak |
| Aktif task | `ZEKAM-DOD-025` (canlı test öncesi yerel hazırlık tamam) |
| Sonraki güvenli adım | Endpoint/credential ve Whisper fixture değerleri mevcut olduğunda policy adayını kalıcılaştır; 10 exact one-shot authorization ile canlı contract testlerini son kapıda çalıştır |
| Recovery required | Hayır |
| Yetki | Verilmemiş; her effect exact plan ve policy'ye tabidir |

## Faz durumu

| Faz | Ad | Durum |
|---|---|---|
| `ZEKAM-P00` | Baseline ve bootstrap | `completed` |
| `ZEKAM-P01` | Kanonik persistence ve realm | `completed` |
| `ZEKAM-P02` | Project registry ve capability | `completed` |
| `ZEKAM-P03` | Work Graph, Intent, Decision, Plan | `completed` |
| `ZEKAM-P04` | Governance, Secret Broker, authorization | `completed` |
| `ZEKAM-P05` | AgentHarness ve runtime | `completed` |
| `ZEKAM-P06` | Model inventory ve health | `completed` |
| `ZEKAM-P07` | Benchmark, routing, quota, deliberation | `completed` |
| `ZEKAM-P08` | Context compiler ve continuity | `completed` |
| `ZEKAM-P09` | Doğal dil ve kanıtlı araştırma | `completed` |
| `ZEKAM-P10` | Sandbox teslim ve istemci adaptörleri | `completed` |
| `ZEKAM-P11` | Knowledge ingestion ve artifact | `completed` |
| `ZEKAM-P12` | Hibrit retrieval, citation, eval | `completed` |
| `ZEKAM-P13` | Native memory ve Mem0 adaptörü | `completed` |
| `ZEKAM-P14` | Öğrenme, skill ve ölçülü döngü | `completed` |
| `ZEKAM-P15` | Scheduler, gelen belgeler, raporlar | `completed` |
| `ZEKAM-P16` | CLI, API, MCP, dashboard | `completed` |
| `ZEKAM-P17` | Hardening, DR, release | `completed` |

## Faz 0 teslimi

- `src/zekam/` altında domain / application / infrastructure / interfaces katmanları.
- Kalite kapıları: `ruff format`, `ruff check`, `mypy --strict`, `pytest`.
- PostgreSQL 18 + pgvector 0.8.6 compose baseline'ı ve gerçek sunucuya karşı kabul testleri.
- `ZEKAM_HOME` yerleşimi, sahiplik sınıfları ve core/user-data ayrımı.
- `zekam` CLI ilk sürümü: `--version`, `init`, `doctor`.

Kanıt: `.zekam/phases/ZEKAM-P00-kanit.json`, `.zekam/evidence/`, `.zekam/checkpoints/`.

## Faz 1 teslimi

- Kanonik JSON ve SHA-256 digest kütüphanesi (`zekam-canonical-json/v1`), determinizm ve
  `NaN`/naive-datetime/bilinmeyen-tip reddi property testleriyle doğrulandı.
- UUIDv7 kimlikleri, portable slug doğrulaması ve absolute-path reddi.
- Realm, actor, client ve execution identity alan modelleri; cross-realm ilişki reddi.
- On bir bounded-context PostgreSQL schema'sı, `zekam_app` uygulama rolü, forward-only
  migration altyapısı (checksum, drift, advisory lock, geri alma dosyaları).
- Append-only `core.revision` hash zinciri ve immutable `core.event` kaydı; UPDATE/DELETE
  hem yetki hem trigger ile reddediliyor.
- Row-level security ile realm yalıtımı: seçilmemiş realm hiçbir satırı göremez.
- İçerik adresli yerel nesne deposu (atomik yazma, digest doğrulama) ve yedek manifesti.

Kanıt: `.zekam/phases/ZEKAM-P01-kanit.json`, `.zekam/evidence/ZEKAM-P01-*.json`.

## Faz 2 teslimi

- Realm kapsamlı proje kayıt defteri, alias ve portable kimlik; kanonik kayıtta
  absolute path yok, makineye özel yol ayrı ve export dışı tabloda.
- Doğal dil resolver: exact kimlik → slug → alias → trigram benzerliği. Belirsizlikte
  hiçbir mutation yapılmaz, aday listesi döner ve kullanıcı seçer.
- Salt okunur source binding, `rebind` ve append-only source revision gözlemi.
  `git` yalnız allowlist'teki salt okunur alt komutlarla çağrılır.
- Güvenli kaynak keşfi: `.gitignore` + `.zekamignore` + sistem deny list, symlink
  izlenmez, secret içeren dosya indekse girmez, secret değeri hiçbir rapora yazılmaz.
- Deterministik capability profiler: dil, build sistemi, framework, test çerçevesi,
  veritabanı, kalite/güvenlik aracı, CI ve container — her tespit kanıt yolu taşır.
- Entegrasyon yaşam döngüsü ve tek "sonraki güvenli aksiyon" raporu.

Kanıt: `.zekam/phases/ZEKAM-P02-kanit.json`, `.zekam/evidence/ZEKAM-P02-*.json`.

## Faz 3 teslimi

- Work Item, kapalı durum makinesi ve **kanıtsız `completed` reddi**: kural hem alan
  modelinde hem veritabanı constraint'inde; uygulama katmanı atlansa bile geçmez.
- Her değişiklik `core.revision` hash zincirine ekleniyor; `work history`
  `chain_valid` ile bağımsız doğrulama veriyor.
- Optimistic concurrency: aynı kaydı iki yazar güncellerse yalnız biri kazanır.
- İlişki grafiği; `depends-on` ve `parent-of` döngüleri veritabanı trigger'ı ile,
  cross-project/cross-realm ilişki bileşik foreign key ile reddediliyor.
- Intent ve Decision append-only revision kayıtları (alternatif, kriter, gerekçe, kanıt).
- Task Plan DAG'ı, topolojik sıra, exact `effect_digest`, drift tespiti ve
  `grants_authority = false` garantisi.
- `bugün hangi işlerimiz var`, `sıradaki iş` ve `nerede kaldık` yanıtları yalnız
  kanonik Work Graph'tan.

Kanıt: `.zekam/phases/ZEKAM-P03-kanit.json`, `.zekam/evidence/ZEKAM-P03-*.json`.

## Faz 4 teslimi

- Sürümlü policy ve capability registry. **Yetenek beyanı yetki değildir**; policy'nin
  izin vermesi de exact authorization yerine geçmez — ikisi de negatif testle doğrulandı.
- Risk seviyesi effect, veri sınıfı, blast radius, geri alınabilirlik ve yıkıcılıktan
  türetilir. `EffectRequest` üzerinde risk alanı **yok**: istemci kendi beyanıyla riski
  düşüremez.
- Hard gate zinciri `capability → policy → risk → scope → authorization`, ilk reddedende
  durur ve her karar denetim kaydına yazılır.
- `SecretValue`: `repr`, `str`, f-string, `%`-format, log ve exception maskeli;
  JSON/pickle/hash hata veriyor; `reveal()` görünür bir işaret.
- Secret sızıntı taraması: dört şemadaki bütün metin ve jsonb kolonları tarandı, değer
  hiçbir yerde yok — locator var, değer yok.
- Exact one-shot authorization: atomik tüketim, replay/expiry/revoke/scope reddi.
  Terminal durum geri alınamıyor, digest değiştirilemiyor, kapsam genişletilemiyor,
  kayıt silinemiyor — hepsi veritabanı trigger'ı ile.
- Provider Gate: `prepare` ağ ve secret'a dokunmuyor; `secret` ve `local-only` veri
  dışarı çıkamıyor; `restricted` gözden geçirilmiş disclosure istiyor.
- Append-only denetim ledgeri; her karar actor, yetki ve kanıt digest'i ile izlenebilir.

Kanıt: `.zekam/phases/ZEKAM-P04-kanit.json`, `.zekam/evidence/ZEKAM-P04-*.json`.

## Faz 5 teslimi

- **prepare/apply ayrımı**: `prepare` hiçbir satır yazmıyor, yetki tüketmiyor —
  bir test tüm runtime ve authorization tablolarının satır sayısını önce/sonra
  karşılaştırıyor. `apply` drift'i yeniden doğruluyor; stale hazırlık yetkiyi
  tüketmiyor.
- **Route planner**: sabit maksimum yok; paralellik yedi sınırın en küçüğü.
  `direct`/`single`/`sequential`/`parallel`/`blocked`/`recovery` kararlarının hepsi
  ayrı testle doğrulandı.
- **Durable queue**: aynı idempotency key ile ikinci enqueue yeni job üretmiyor,
  enqueue ile outbox olayı aynı transaction'da, claim `for update skip locked` ile.
- **Lease ve fencing**: owner token saklanmıyor (yalnız digest); eski fence ile
  `complete` ve `heartbeat` reddediliyor.
- **Logical lock**: çakışma kuralları veritabanı trigger'ında; parent/child path,
  project kilidi ve farklı proje senaryoları test edildi.
- **Claim-before-effect**: ikinci claim ve ikinci receipt veritabanında reddediliyor;
  bekleyen claim varsa iş `completed` olamıyor; crash-after-claim `recovery-required`
  oluyor ve sessiz retry `PolicyViolation` veriyor.
- **Agent Result Envelope**: strict şema; serbest metin ve bilinmeyen alan reddediliyor.
  Fan-in başarısızlığı yutmuyor. Agentic iş en az bir subagent istiyor, koordinatör
  sayılmıyor. Yüksek riskte kendi işini doğrulama reddediliyor.

**20 worker yarış testi**: aynı job'a koşan 20 bağlantıdan tek biri claim aldı;
20 farklı job'a koşan 20 worker hepsini tekrarsız paylaştı.

Kanıt: `.zekam/phases/ZEKAM-P05-kanit.json`, `.zekam/evidence/ZEKAM-P05-*.json`.

## Faz 6 teslimi

- **20 Model ID** kanonik store'a aktarıldı; aynı backend adını paylaşan iki kayıt
  ayrı yönetim nesnesi olarak duruyor.
- **19 teknik profil farkı görünür**: profili olmayan kayıt `verification_note`
  taşıyor ve raporda ayrı başlıkta listeleniyor.
- Ham endpoint/credential envantere **giremiyor**: URL, IP, `Bearer`, `sk-`, AWS
  anahtarı ve uzun opak token desenleri alan modelinde ve veritabanı
  constraint'inde reddediliyor.
- Altı modalite için ayrı sentetik probe ve şekil doğrulaması. Prompt/yanıt içeriği
  saklanmıyor; secret canary yansıması `secret-echo` ile yakalanıyor.
- Sözleşme kontrolleri: doğrulanmayan yetenek `capabilities_verified` listesine
  girmiyor; en son kontrol kazanıyor; bütün beklenen sözleşmeler doğrulanmadan
  `contract-passed`'e terfi yok.
- İki ardışık başarısızlık karantina, cooldown dolunca otomatik salıverme, envanter
  ve policy digest değişiminde staleness.
- Günlük rapor: Türkçe Markdown ve JSON aynı `evidence_digest`'e bağlı, ikisinde de
  secret ve ham endpoint yok.

**Veri kalitesi bulgusu:** `openai/QuantTrio/Qwen3-VL-30B-A3B-Instruct-AWQ` kaydında
`declared_category: multimodal_generation` ile `declared_mode: completion` çelişiyor.
Sessizce çözmek yerine `modality-conflict` olarak raporlanıyor; probe çağrı şeklini
belirleyen modu esas alıyor.

Kanıt: `.zekam/phases/ZEKAM-P06-kanit.json`, `.zekam/evidence/ZEKAM-P06-*.json`.

## Faz 7 teslimi

> Bu faz ayrı bir istemci oturumunda yazıldı; bu oturumda bağımsız olarak
> denetlendi ve doğrulandı (commit `7d0920d`).

- Beş sürümlü ve secret-free fixture artifact'ı; allow-root, canonical path, digest ve
  local/remote eligibility kontrolleri fail-closed.
- Capability profile digest'ine bağlı project benchmark suite ve her fixture için beş
  tekrar zorunluluğu.
- Gerçek child-process tested-model adapteri ve tested modelden farklı bağımsız verifier;
  25 trial, 25 canonical verifier sonucu ve 50 ayrı claim/terminal receipt E2E kanıtı.
- Exact fixture × repetition matrisi, deterministik mean/median/p95/variance aggregate'i
  ve tek unsafe trial'ı gizlemeyen rejection gate'i.
- Kanonik inventory, health, policy, capability, fixture registry, suite, runtime ve quota
  evidence digest'lerine bağlı açıklanabilir Model Decision.
- Trusted Codex `%40` / Claude `%30` fallback, unknown-no-guess ve iki tur/600 saniye/
  token-cost-evidence bütçeli authority-free deliberation.

Kanıt: `.zekam/phases/ZEKAM-P07-kanit.json`, `.zekam/evidence/ZEKAM-P07-*.json`.

## Faz 8 teslimi

> Kodu ayrı bir istemci oturumunda yazıldı; bu oturumda denetlendi, tamamlandı
> (belge, projeksiyon, kanıt) ve doğrulandı.

- **Context manifest**: deterministik seçim; `required` taşması fail-closed;
  her dışlama açık gerekçe taşıyor (`budget-exhausted`, `stale`,
  `insufficient-authority`, `superseded`).
- **WorkJournal**: append-only hash zinciri; ekleme, silme, sıra değiştirme, içerik
  değiştirme ve budama denemelerinin beşi de yakalanıyor.
- **Checkpoint zorunluluğu**: plan adımları completed/pending arasında exact partition;
  `meaningful_step` işaretli job, checkpoint'i olmadan `completed` olamıyor — **veritabanı
  trigger'ı**, uygulama katmanı atlansa bile geçmiyor.
- **Handoff**: transcript, authority, lease, approval ve absolute path taşıyamıyor;
  beş bayrak birlikte veritabanı constraint'i ile zorlanıyor.
- **Cross-client resume**: Codex → Claude → OpenCode geçişi transcript olmadan ve
  yetki devralmadan çalışıyor; yeni worker Work/lease/authorization durumunu yeniden
  ediniyor.

Kanıt: `.zekam/phases/ZEKAM-P08-kanit.json`, `.zekam/evidence/ZEKAM-P08-*.json`.

## Faz 9 teslimi

- **Intake**: research / project-change / status / idea / ambiguous dört sınıfı ayrılıyor;
  ipucu yoksa niyet **tahmin edilmiyor**, çakışan ipucunda seçim isteniyor.
- **Exact identifier önceliği**: `ZEKAM-P09-T01`, `#123`, `123 numarali defect` metinden
  çıkarılıyor ve semantic benzerlik bunu değiştiremiyor; kanonik kayıtta yoksa
  `identifier-unknown` olarak görünür kalıyor.
- **Anaphora sınırı**: işaret zamiri yalnız taze ve bounded konuyla çözülüyor; konu yoksa
  veya bayatsa konu uydurulmuyor.
- **Proje çözümü**: adaylar kanonik registry'den kuruluyor; iki aday varsa mutation
  başlamadan seçim isteniyor (`zekam ask` çıkış kodu 5).
- **Soru scope ve bütçe**: project/work/intent scope'una bağlı; source revision veya intent
  digest değişince stale. En fazla iki tur, 600 saniye; HTTPS exact host allowlist olmadan
  etkinleşmiyor.
- **Source snapshot**: file / repository / https / import provenance ve digest korunuyor;
  absolute path, traversal ve query string hem Python'da hem check constraint'inde
  reddediliyor.
- **Research DAG**: koordinatör subagent sayılmıyor ve dispatcher'a hiç verilmiyor; bağımsız
  ilk üç rol paralel grupta; döngü ve rol uyuşmazlığı reddediliyor.
- **Çelişki ve verifier**: direct contradiction yalnız verifier veya insan review ile
  çözülüyor; unresolved kaldığı sürece rapor `answered` olamıyor (veritabanı constraint'i).
  Citation verifier kimliği araştırmacılarla aynı olamıyor (trigger).
- **Fan-in**: non-success child sonucu yutulamıyor; kanıt yetersizse abstain üretiliyor.
- **Plan candidate**: yalnız answered rapordan türüyor (trigger) ve daima
  `requires_authorization = true`, `approval_inherited = false`, `grants_authority = false`.

Kanıt: `.zekam/phases/ZEKAM-P09-kanit.json`, `.zekam/evidence/ZEKAM-P09-*.json`.

## Faz 10 teslimi

- **Detached worktree**: entegre kaynak main tree read-only ve bu kapatılamaz
  (`main_tree_read_only=False` → `PolicyViolation`). HEAD ve tree parmak izi işlem
  öncesi/sonrası karşılaştırılıyor; gerçek `git worktree` ile doğrulandı.
- **Path allowlist**: boş allowlist reddediliyor; önek eşlemesiyle kaçılamıyor
  (`docs` izinliyken `docs-gizli/` değil); absolute path, traversal ve symlink
  kaçışı worktree kökünde çözülerek yakalanıyor.
- **Network**: default-deny; izin exact host **ve** exact operasyon listesi istiyor.
- **Typed runner**: shell yok (`shell=False`), zorunlu timeout, ortam allowlist'i
  (çağıran sürecin ortamı devralınmıyor), çıktı bayt sınırı, ham çıktı yerine digest.
- **Teslim**: drift → `git apply --check` → bağımsız test → verifier. Builder ile
  verifier aynı kimlik olamıyor; yalnız `applied` teslim receipt'e uygun.
- **İstemci adaptörleri**: Codex / Claude Code / OpenCode exact çalıştırılabilir
  dosya ve açık yetenek beyanı ile çağrılıyor; beyan edilmeyen yetenek varsayılmıyor.
  Strict JSON envelope; bozuk JSON, bilinmeyen outcome ve liste payload sessizce
  kabul edilmiyor. Komut satırı talimat metni değil digest taşıyor.
- **Commit/push kapısı**: ASCII-only Türkçe mesaj, zorunlu bölümler, anlamsız başlık
  reddi, secret ve kişisel path reddi, `Merge`/`Revert` controlled exception. Push
  varsayılan olarak reddediliyor; force push hiçbir koşulda otomatik izinli değil.

**Düzeltilen iki gerçek hata:** (1) argv metakarakter kuralı fazla genişti ve
`python -c "import os; ..."` gibi meşru argümanları engelliyordu — kural
çalıştırılabilir alanına daraltıldı, satır sonu her alanda yasak kaldı.
(2) `git apply --check` girdisi text modunda veriliyordu; Windows satır sonu çevrimi
yamayı bozup check'i daima başarısız yapıyordu — girdi bayta çevrildi.

Kanıt: `.zekam/phases/ZEKAM-P10-kanit.json`, `.zekam/evidence/ZEKAM-P10-*.json`.

## Faz 11 teslimi

- **Değişmez artifact**: orijinal kaynak içerik digest'iyle saklanır; update/delete
  trigger ile reddedilir. Orijinal ad absolute path veya traversal taşıyamaz.
- **Aşamalı ingestion**: `validated → stored → parsed → normalized → indexed →
  activated`. Atlama alan katmanında, geri alma veritabanı trigger'ında reddediliyor.
  Her aşama kalıcılaştırılıyor; aynı `idempotency_key` ikinci iş yaratmıyor.
- **Atomik aktivasyon**: tamamlanmamış ingestion aktif sürüm üretemiyor (alan +
  trigger). Bir kaynağın aynı anda yalnız bir aktif sürümü var (partial unique index).
- **Normalize içerik**: parser doğrudan vector üretmiyor; locator taşıyan içerik
  birimi üretiyor. Locator'sız birim veritabanına yazılamıyor, OCR birimi confidence
  istiyor, DOCX için uydurma sayfa numarası üretilmiyor, bilinmeyen format sessizce
  metin sayılmıyor.
- **Güvenli tarama**: deny list, `.git`/`node_modules` atlaması, symlink, ikili dosya,
  izinli kök dışı ve arşiv traversal/zip bomb fail-closed. Her karar gerekçe taşıyor.
- **Kod ve DB**: Python sembolleri AST ile çıkarılıyor — **kod çalıştırılmıyor**
  (test dosya yazan kaynakla bunu doğruluyor). PL/SQL nesneleri metadata olarak
  çıkarılıyor; satır verisi varsayılan olarak alınmıyor.

**Düzeltilen hata:** PL/SQL regex'i `create package body app.paket` ifadesinde `body`
kelimesini nesne adı sanıyordu; `package body` iki kelimelik tür olarak tanımlandı.

**Bakım iyileştirmesi:** migration down testleri sabit sürüm numarası yazdığı için her
yeni migration'da kırılıyordu; hedefler artık keşif sonucundan türetiliyor.

Kanıt: `.zekam/phases/ZEKAM-P11-kanit.json`, `.zekam/evidence/ZEKAM-P11-*.json`.

## Faz 12 teslimi

- **Chunker**: başlık altındaki paragraflar birleşiyor; tablo ve kod blokları bütün
  kalıyor; büyük birim parent-child üretiyor ve locator korunuyor. Locator'sız chunk
  hem alanda hem check constraint'inde reddediliyor.
- **Embedding profili**: BGE-M3 1024 cosine. `NaN`/`Inf` ve boyut uyuşmazlığı
  indekslenmiyor; farklı prefix ayrı profil; profil digest uyuşmazlığı trigger ile
  reddediliyor (sessiz profil karışması retrieval'i bozar ve fark edilmesi zordur).
- **Üç kanal**: exact identifier (trigram), lexical (FTS `simple` sözlüğü — kök
  bulma teknik kimliği bozar), dense (pgvector HNSW cosine).
- **RRF**: ham dense mesafesi ile `ts_rank` **toplanmıyor**; yalnız sıra kullanılıyor.
  İki kanalda görünen öne çıkıyor, exact eşleşme düşük dense skorla elenemiyor,
  sıralama deterministik.
- **Dayanıklılık**: reranker hata verir veya sonuç düşürürse fusion sırasına
  dönülüyor; aynı içerik iki kez bağlama girmiyor; çocuk seçilirse ebeveyn de alınıyor.
- **Citation ve abstain**: `answered` / `abstained-no-hit` / `abstained-low-evidence`.
  Kanıtsız cevap üretilemiyor, abstain citation taşıyamıyor, bağlam token bütçesini
  aşamıyor. Her cevap kanal ve eleme açıklaması taşıyor.
- **Değerlendirme**: Recall@k, MRR, nDCG@k. İyileşme ancak hiçbir metrik gerilemeden
  kabul ediliyor.

**Düzeltilen iki hata:** (1) `#4711` kimliği kırpılıyordu — `` `#` öncesinde sınır
oluşturmaz; lookbehind'a çevrildi. (2) Embedding trigger'ı `vector_dims()`
bulamıyordu — `search_path`'e `public` eklendi (vector tipi eklenti şemasında yaşar).

Kanıt: `.zekam/phases/ZEKAM-P12-kanit.json`, `.zekam/evidence/ZEKAM-P12-*.json`.

## Faz 13 teslimi

- **Bellek otorite değil**: `grants_authority` alanda ve check constraint'inde
  `false`. Work, policy ve run durumu belleğe devredilmiyor.
- **Kapsam izolasyonu**: `run` ve `agent` geçici — kalıcı bellek üretemiyor ve
  aramada görünmüyor (alan + `record_scope_persistent`). Cross-project açık izin
  istiyor; farklı realm hiçbir koşulda görünmüyor.
- **Promotion kapısı**: ham model çıktısı doğrudan aktif olamıyor. Kanıt zorunlu;
  `semantic`/`procedural`/`failure` bağımsız review istiyor ve review yazarla aynı
  kimlik olamıyor. Failure dersi en az iki bağımsız gözlem istiyor.
- **Supersession**: eski içerik korunuyor, `supersedes` ilişkisi kuruluyor. İçerik,
  sınıf ve kapsam değiştirilemiyor — sütun düzeyi UPDATE yetkisi bunları vermiyor
  **ve** trigger reddediyor. Kayıt silinemiyor.
- **Hibrit arama**: exact/FTS/vektör/varlık/zaman bileşenleri; her sonuç seçim
  gerekçesi taşıyor, gerekçesiz sonuç döndürülmüyor.
- **Hijyen**: duplicate, conflict, stale, unused, retention-review ve source-version
  çelişkisi salt okunur raporlanıyor; otomatik silme yok.
- **Mem0 adaptörü**: opsiyonel ve otorite değil. Drift durumunda native kayıt
  geçerli; senkron hatası native kaydı etkilemiyor.

**Düzeltilen üç şey:** (1) Çelişki sezgisi olumsuzluk ekini kelimeden çıkarmaya
çalışıyordu ve hiçbir çelişkiyi yakalamıyordu — açık olumlu/olumsuz fiil çifti
tablosuna çevrildi. (2) Bir kayıt oluşturulduğu anda supersede edilince sıfır
uzunlukta geçerlilik aralığı oluşuyordu; artık açık hata veriyor. (3) Repository
logical proje referansı yerine UUID döndürüyordu ve kapsam kontrolünü bozuyordu.

Kanıt: `.zekam/phases/ZEKAM-P13-kanit.json`, `.zekam/evidence/ZEKAM-P13-*.json`.

## Faz 14 teslimi

- **Çift sayım engellendi**: iki farklı run aynı `evidence_digest` üretiyorsa tek
  gözlemdir — hem alanda hem `occurrence_evidence_unique` constraint'inde.
- **Ders kanıttan türer**: doğrulanmış kök neden olmadan ders üretilmiyor; kök
  neden üçlüsü ya birlikte doldurulur ya hiç. En az iki bağımsız gözlem; tek olay
  ancak açıkça `critical` işaretliyse yeter. Ders verifier'ı yazarla aynı olamıyor.
- **Skill kendi kendini aktive edemiyor** (`self_promoted` alanı + constraint).
  Aktivasyon dört kapıyı birden istiyor: ölçüm, baseline'ı geçme (trigger),
  bağımsız onay ve boş olmayan rollback planı.
- **Değerlendirme**: en az beş deneme; değerlendiren ve doğrulayan ayrı kimlik;
  aynı gövdeli adaylar tekilleştiriliyor.
- **Ölçülü döngü**: goal-reached / iteration-budget / cost-budget / no-progress /
  blocked. **Doğrulanmamış başarı hedefi kapatmıyor** — "model başarılı dedi"
  yeterli değil.
- **Bağlam etkinliği**: token maliyeti, kanıt yoğunluğu ve doğrulanmış başarı oranı
  route kararına giriyor ama `grants_authority` daima `false`.

Kanıt: `.zekam/phases/ZEKAM-P14-kanit.json`, `.zekam/evidence/ZEKAM-P14-*.json`.

## Faz 15 teslimi

- **Sohbetten bağımsız**: zamanlama tanımı kalıcı; süreç yeniden başladığında
  duraklatılmış iş duraklatılmış, iptal edilmiş iş iptal edilmiş kalıyor.
- **Idempotency**: anahtar iş + planlanan an (UTC) + payload digest'inden türüyor;
  aynı tetikleme iki kez iş üretmiyor (alan + unique constraint). Aynı mutlak an
  farklı timezone gösterimiyle verilse bile anahtar aynı.
- **Kaçırılan çalışma sessizce yutulmuyor**: `run-once` tek telafi çalıştırıyor,
  `skip-visible` atlıyor ama kaç çalışma kaçırıldığını raporluyor.
- **Overlap**: bir tanımın aynı anda tek aktif çalışması olabiliyor (partial unique
  index); terminal çalışma bitiş zamanı istiyor.
- **Gelen belgeler**: hâlâ yazılan dosya ingest edilmiyor (5 sn sessizlik),
  aynı içerik ikinci kez işlenmiyor, **birden fazla hedefte tahmin edilmiyor —
  seçim isteniyor**. Belge yolu portable olmak zorunda.
- **Gece işleri**: bounded bütçe; **kota bilinmiyorsa çalışmıyor** — kalan oran
  tahmin edilmiyor.
- **Günlük rapor**: on bölüm zorunlu (alan + constraint); boş bölüm "kayıt yok"
  yazıyor; rapor authority taşımıyor; aynı gün ve kapsam için ikinci rapor yok.
- **Olaylar**: her scheduler olayı `next_safe_action` bildirmek zorunda.

Kanıt: `.zekam/phases/ZEKAM-P15-kanit.json`, `.zekam/evidence/ZEKAM-P15-*.json`.

## Faz 16 teslimi

- **Tek sözleşme, çok yüzey**: `CANONICAL_COMMANDS` yirmi komutu tanımlıyor;
  mutasyon yapan her komut açık `--uygula` bayrağı istemek zorunda ve bu alan
  düzeyinde zorlanıyor.
- **`zekam surface check`** sözleşme ile gerçekte kayıtlı komutları karşılaştırıyor —
  belge ile kod arasındaki sapma çıkış kodu 1 ile görünür oluyor. Şu an 20/20
  sözleşme komutu kayıtlı (toplam 55 komut).
- **Telemetri**: correlation zorunlu, içerik yasak. `prompt`, `response`, `content`,
  `body` ve secret benzeri alan adları ile PEM/Bearer/base64 değerleri ve kişisel
  path'ler **alan oluşturulurken** reddediliyor — çalışma zamanı filtresi değil.
- **Dashboard**: salt okunur, authority üretmiyor, altı projeksiyon zorunlu ve her
  kare kanonik kayda drill-down bağlantısı taşıyor.
- **Türetilmiş graf**: `derived` bayrağı kapatılamıyor; her düğüm kanonik referans
  taşıyor; kenar bilinmeyen düğüme veya kendine bağlanamıyor.
- **MCP**: authority Zekam'de kalıyor (`authority_owner` başka değer alamıyor);
  yetenekler istemciyle uzlaşılıyor, mutasyon yapan araç authorization istiyor.

Kanıt: `.zekam/phases/ZEKAM-P16-kanit.json`, `.zekam/evidence/ZEKAM-P16-*.json`.

## Faz 17 teslimi

- **Bütünleşik tehdit modeli** (`tests/security/test_threat_model.py`): prompt
  injection, secret sızıntısı, path kaçışı, network, replay ve cross-realm kapıları
  katmanlar **birlikte** denenerek doğrulanıyor. Bu suite üç gerçek açık buldu ve
  kapattı (aşağıda).
- **DR tatbikatı**: gerçek içerik adresli depo üzerinde yedek manifesti → yalıtılmış
  geri yükleme → doğrulama. Eksik artifact ve değiştirilmiş manifest yakalanıyor.
  Proje kapsülü absolute path, traversal, secret ve aktif lease taşıyamıyor.
- **Kapasite ve iptal**: kuyruk/worker backpressure kararı; iptal edilen çalışma
  terminal sonuç yayımlayamıyor.
- **SBOM ve release**: `scripts/surum_hazirligi.py` SBOM, DoD ölçümü ve rapor
  üretiyor. **`build_release()` Global DoD tamamlanmadan artifact üretmeyi
  reddediyor** — sözleşme kuralı koda gömülü.
- **Kimlik bütünlüğü**: package, CLI, environment, home, schema ve DB yüzeyi yalnız Zekam'dır.

**Bulunan ve kapatılan üç güvenlik açığı:**
1. Intake konusu `Bearer <token>` kabul ediyordu — bellek katmanı reddederken
   konu alanı geçiriyordu.
2. Telemetri değeri `PASSWORD=...` biçimindeki atamaları yakalamıyordu; yalnız alan
   *adı* kontrol ediliyordu, meşru bir ada gizlenmiş secret sızabilirdi.
3. Aynı sorun `api_key: ...` biçimi için de geçerliydi.

Kanıt: `.zekam/phases/ZEKAM-P17-kanit.json`, `.zekam/evidence/ZEKAM-P17-*.json`.

## Global DoD durumu

**82/83 passed, 1 pending, 0 failed, 0 blocked** (%98.8).

`zekam worker` süreci `ZEKAM-DOD-071` ve `ZEKAM-DOD-081`'i, genişletilmiş doctor ve
altıncı/beşinci kalite kapısı `ZEKAM-DOD-002` ve `ZEKAM-DOD-078`'i kapattı.
`ZEKAM-DOD-001` üç platformdaki temiz kurulum tatbikatıyla kapandı: Windows,
macOS (Darwin 25.6.0 arm64) ve Debian 13 konteyneri. Tatbikat iki gerçek kusur
buldu — symlink kararının `resolve()` sonrasında verilmesi ve operatör
`ZEKAM_DATABASE_*` ortamının testlere sızması — ikisi de kapatıldı.

Hiçbir kriter waiver ile kapatılmadı. Ayrıntı ve gerekli dış aksiyonlar:
[SURUM_RAPORU.md](SURUM_RAPORU.md) ve [GLOBAL_DOD_DURUM.md](GLOBAL_DOD_DURUM.md).

Açık kalan `ZEKAM-DOD-025` için JSON/multipart transport, yedi modalite adapter'ı,
exact model/endpoint locator eşlemesi, yedi SecretRef metadata kaydı, public contract
fixture registry, 7 hedefli policy adayı ve 10 ayrı one-shot authorization planı
kanonik Work plan revision 15 ile tamamlandı. Inventory/SecretRef eşleşmesi 7/7,
readiness 0/7'dir. Gerçek kanıt için yalnız endpoint/credential ve Whisper fixture
ortam değerleri ile son kapıda just-in-time policy/authorization/canlı çağrılar
bekleniyor.
`ZEKAM-DOD-035` kullanıcı kararıyla çevrimdışı ve permissive-only gerçek binary
DOCX/PDF/OCR E2E kanıtı üzerinden kapandı. Sağlayıcı kanıtı uydurulmayacak.

Değişen bağımlılık kümesi için `pip-audit` yalnız `pypi.org` kapsamlı exact network
authorization ile geçti; `No known vulnerabilities found` kanıtı
`.zekam/evidence/ZEKAM-DOD-078-pypi-audit-20260821T093000Z.json` içindedir. Gerçek
model sağlayıcısı çağrısı ve push yapılmadı.

**Release üretilmedi** çünkü `build_release()` tamamlanmamış DoD ile artifact
üretmeyi reddediyor.

## Worker teslimi

- **Sohbetten bağımsız süreç**: `zekam worker run --uygula` uzun ömürlü çalışır;
  zamanlama tanımları veritabanından okunur, tetiklemeler oraya yazılır.
- **Döngü**: kapasite → zamanlama → kuyruk → işleme. Kapasite dolduğunda iş
  alınmaz ve gerekçe döner.
- **Mutasyon kuralı**: `tick` ve `run` açık `--uygula` bayrağı istiyor; bayraksız
  `tick` salt okunur plan üretiyor. Sözleşmede kayıtlı ve `surface check` ile
  doğrulanıyor (24/24 komut).
- **Terminal durum**: işleyicisi olmayan iş `failed` oluyor (sessiz başarı yok),
  handler hatası sanitize ediliyor, terminal receipt'i olmayan claim `completed`
  engelliyor, iptal edilen iş sonuç yayımlayamıyor.
- **Zarif kapanma**: SIGINT/SIGTERM mevcut işi bitirip yeni döngüyü başlatmıyor.

**Düzeltilen hata:** SchedulerGateway veritabanından gelen ham metni enum yerine
doğrudan kullanıyordu; `state is SchedulerState.ACTIVE` karşılaştırması sessizce
`False` dönüyor ve **aktif tanımlar hiç çalışmıyordu.** Gerçek veritabanına karşı
yazılan test yakaladı.

Kanıt: `tests/integration/test_worker_postgres.py` (13),
`tests/e2e/test_cli_worker.py` (5), `docs/WORKER.md`.

## Doctor ve kalite kapısı teslimi

- **Doctor kapsamı**: `runtime` kategorisi eklendi — kuyruk derinliği + recovery,
  model envanteri, policy, scheduler tanımları, istemci çalıştırılabilirleri ve
  komut yüzeyi. Kanonik kayıt okunamazsa `skipped` döner, **sahte `passed` yok**.
  Sayılar realm kapsamı olmadan okunduğu için `cross_realm` olarak işaretleniyor.
- **Kalite kapısı altıya çıktı**: biçim, lint, tip, test, bağımlılık audit
  (`pip-audit`) ve ölü kod (`vulture`). `--cevrimdisi` ağ isteyen kapıyı atlar ve
  atlamayı hem ekranda hem kanıt dosyasında **görünür** kılar.

**Düzeltilen hata:** yeni doctor kontrolleri `models.model_record` ve
`security.policy_version` tablolarını sorguluyordu; gerçek adlar
`models.model_inventory` ve `security.policy`. Kontroller sessizce `skipped`
dönüyordu — yani hiç çalışmıyordu. Gerçek veritabanına bakarak yakalandı.

**Test kırılganlığı düzeltildi:** doctor testleri boş veritabanı varsayıyordu ve
tam suite'te sıraya bağlı olarak kırılıyordu; beklentiler artık veritabanının
gerçek durumundan türetiliyor.

Kanıt: `tests/integration/test_doctor_runtime_checks.py` (13),
`.zekam/evidence/ZEKAM-DOD-078-20260821T055019Z.json` (altı kapı da geçti).

## Kural

Bu dosyayı el ile “tamamlandı” yapmak işi tamamlamaz. Tamamlanma; test, verifier, claim/receipt
ve kanonik Work revision kanıtıyla üretilmelidir.
