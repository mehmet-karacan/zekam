---
schema: zekam-active-task/v2
task_id: ZEKAM-LOCAL-INTELLIGENCE-PLANE-001
status: APPROVED_ACTIVE_TASK
title: Zekam Yerel-İlk Polyglot Veri, Çalışan RAG, Model Laboratuvarı ve Ölçümlü Öz-İyileştirme Düzlemi
created_at: 2026-09-02T09:56:00+03:00
baseline_repository: mehmet-karacan/zekam
baseline_branch: main
baseline_head: d95cdac2713df797e42afda020ab6e8e55188031
legacy_postgresql_data_import: FORBIDDEN
postgresql_runtime_dependency: FORBIDDEN
docker_required_for_zekam_core: false
push_authorized: false
---

# AKTIF_GOREV.md

> **DURUM: ONAYLI AKTİF GÖREV**
>
> Bu dosya Zekam'ın yeni veri, RAG, model laboratuvarı, benchmark, routing, bellek ve öz-iyileştirme mimarisinin bağlayıcı uygulama görevidir. Görev kapsamı kullanıcı tarafından 2 Eylül 2026 tarihinde onaylanmıştır.
>
> Bu görev **eski PostgreSQL verisini taşımak, dışa aktarmak, okumak, dönüştürmek veya uyumluluk amacıyla yeniden kullanmak değildir**. Zekam uygulaması ve doğrulanmış domain kuralları korunur; veri katmanı sıfırdan kurulur. Sistem, PostgreSQL hiç var olmamış gibi temiz ve yerel-first biçimde bootstrap edilir.
>
> Bu görev hiçbir dış servis çağrısına, canlı benchmark kampanyasına, commit push işlemine, gizli veri aktarımına veya destructive temizliğe kendiliğinden yetki vermez. Canlı model çağrıları ve push ayrı, açık ve digest-bound kullanıcı onayı ister.

---

## 1. Görevin tek cümlelik amacı

Zekam'ı; Docker veya ayrı veritabanı servisi gerektirmeden çalışan, her veri sınıfını en uygun yerel motorda tutan, global ve proje-özel bilgisini düzenleyen, MacBook'ta yerel BGE ve Windows/OpenCode ortamında uzak embedding kullanan, gerçekten çalışan kaynaklı hibrit RAG sağlayan, kurum içi modelleri ölçümlü benchmarklarla tanıyıp yönlendiren ve yalnız güvenli sınırlar içinde kendisini sürekli iyileştirebilen bir yerel AI çalışma düzlemine dönüştür.

---

## 2. Bağlayıcı kullanıcı kararları

Aşağıdaki kararlar bu görevin kapsamı içinde tartışmaya açık değildir. Uygulayıcı model bunları sessizce değiştiremez, gevşetemez veya başka bir yoruma çekemez.

### K-001 — PostgreSQL verisi alınmayacak

- Eski PostgreSQL veritabanına bağlanılmayacak.
- `pg_dump`, `psql`, `COPY`, ETL, export/import veya veri karşılaştırması yapılmayacak.
- Eski PostgreSQL tablolarındaki project, work, run, memory, model, benchmark, routing veya retrieval kayıtları yeni sisteme taşınmayacak.
- Eski migration head'i yeni şema başlangıcı kabul edilmeyecek.
- Yeni operational store şema sürümü `1` ile başlayacak.
- Kod, test fixture'ı ve domain sözleşmesi yeniden kullanılabilir; **veri yeniden kullanılmaz**.

### K-002 — Zekam core için PostgreSQL ve Docker zorunluluğu bitecek

- Zekam'ın kurulması, başlatılması, doctor çalıştırması, proje eklemesi, work yönetmesi, RAG sorgulaması, oturum kapatması ve model registry okuması için Docker gerekmeyecek.
- Ayrı DB server, port, DB kullanıcısı, DB parolası ve container health yönetimi olmayacak.
- Zekam core'un normal çalışmasında `docker`, `docker compose`, `postgres`, `pgvector`, `psycopg` veya `psql` çağrısı yapılmayacak.

### K-003 — Tek DB her şeyi yapmayacak

Zekam en fazla aşağıdaki üç kalıcı motor sınıfını kullanacak:

1. **Operational Store:** state, work, session, run, lock, claim, receipt, model registry state ve policy activation.
2. **Knowledge Index:** exact/lexical/vector retrieval için yeniden üretilebilir yerel indeks.
3. **Analytics Store:** benchmark ve telemetri analizi için DuckDB tabanlı, yeniden üretilebilir analitik görünüm.

Markdown/Git ve content-addressed filesystem kaynak/veri alanıdır; bunlar dördüncü bir uygulama DB'si gibi kullanılmayacaktır.

### K-004 — Her proje kendi veritabanından sorumludur

- Zekam'ın operational DB seçimi, yönettiği projelerin DB seçimi değildir.
- GPU Oracle kullanabilir, başka proje PostgreSQL, Context Vault farklı bir çözüm kullanabilir.
- Zekam yalnız proje metadata'sını, bağlantı bilgisini, çalıştırma şeklini, kullanılan teknolojileri ve ilgili kaynakları bilir.
- Proje DB'si Zekam core DB'sine taşınmaz.

### K-005 — Temiz veri bootstrap yapılacak

Yeni sistem ilk çalışmada verisini aşağıdaki kaynaklardan yeniden üretecek:

- repo ve proje keşfi,
- kullanıcı tarafından onaylanmış proje binding'leri,
- global/proje Markdown bilgisi,
- güncel model keşfi,
- yeni embedding/index üretimi,
- yeni benchmark baseline'ı,
- yeni runtime olayları ve telemetri.

Eski DB'den herhangi bir kayıt bootstrap girdisi değildir.

### K-006 — Embedding provider ile storage provider bağımsızdır

- MacBook: mevcut yerel BGE embedding rotası kullanılacak ve gerçek model çağrısı yapıldığı kanıtlanacak.
- Windows/OpenCode: kurum içi uzak embedding rotası kullanılacak.
- Storage motoru embedding modelini belirlemeyecek.
- Embedding modelinin yerel veya uzak olması operational DB seçimini etkilemeyecek.

### K-007 — Sahte semantic fallback yasaktır

- Deterministik feature-hashing veya token sayacı “embedding modeli” diye adlandırılmayacak.
- Gerçek embedding üretilemiyorsa sistem açıkça `lexical-only-degraded` durumuna geçecek veya fail-closed davranacak.
- Sahte vektör üreterek dense RAG çalışıyor görüntüsü verilmeyecek.

### K-008 — Model bilgisi ve benchmark kanıtı yerelde tutulacak

OpenCode kurum içi modelleri için aşağıdaki bilgiler local olarak tutulacak:

- exact erişim kimliği,
- provider ve endpoint identity digest'i,
- local/remote niteliği,
- cihaz/ortam kapsamı,
- modality ve doğrulanmış yetenekler,
- health ve availability,
- revision/fingerprint değişimi,
- benchmark tarihçesi,
- strengths/weaknesses,
- latency, reliability ve task-specific skorlar,
- routing uygunluğu ve confidence.

### K-009 — Benchmark “çalıştı mı” testi olmayacak

Benchmark ve ürün testleri yanlış, sınır ve düşmanca girdiler içerecek. Örneğin integer beklenen alana `1` geldiğinde çalışması kadar, `"A"`, `true`, `1.5`, `null`, `""`, negatif, taşan sayı ve bozuk JSON geldiğinde kontrollü biçimde reddetmesi de zorunludur.

### K-010 — Kendisini geliştirme bounded ve kanıtlı olacak

- Sistem sorun tespit edebilir, improvement candidate üretebilir, test edebilir ve öneri sunabilir.
- Derived index/projection yeniden üretimi gibi güvenli onarımları bounded politika altında otomatik yapabilir.
- Skill, prompt, routing, memory relation veya hook değişikliği doğrudan aktif olamaz.
- Root instruction, schema, security, approval, retention, secret veya destructive policy değişikliği açık insan onayı olmadan yapılamaz.

### K-011 — `AKTIF_GOREV.md` tek görev sözleşmesidir

- Bu dosya scope ve hedeflerin tek yaşayan görev sözleşmesidir.
- `AKTIF_GOREV.yaml` bağımsız ikinci authority olarak tutulmayacak; kaldırılacak veya yalnız bu dosyadan deterministik, read-only ve generated projection olarak üretilecek.
- Operational DB, görevin kapsamını icat etmez; bu dosyanın exact digest'ine bağlı execution progress ve receipt tutar.

### K-012 — Push ayrı onay ister

- Yerel commitler mevcut commit politikasına uygun olarak üretilebilir.
- Kullanıcı açıkça istemedikçe hiçbir branch push edilmez.
- Force-push ve history rewrite yasaktır.

### K-013 — Mac-first uygulama, Windows kabulü ertelenmiştir

Kullanıcı 2 Eylül 2026 tarihinde uygulamanın mevcut MacBook üzerinde doğrudan
ilerlemesini ve Windows tarafına daha sonra dönülmesini açıkça istemiştir.

- macOS ARM64 için ölçülmüş provisional teknoloji kararlarıyla sonraki iş
  paketlerine geçilebilir.
- Windows x64 hard gate'leri kaldırılmış veya geçmiş sayılmaz; `deferred` olarak
  açık kalır ve nihai cross-platform kabulü engeller.
- Mac üzerinde tamamlanan işler `macos-accepted/windows-deferred` durumunu taşır;
  çapraz-platform `COMPLETED` iddiası üretmez.
- Windows kanıtı geldiğinde aynı fixture, manifest ve fault matrisleri ayrıca
  çalıştırılır; Mac sonucu Windows sonucu olarak yeniden kullanılmaz.
- Mac embedding/RAG uygulaması tüm kullanıcı corpus'unu indekslemez; küçük,
  kaynakları doğrulanabilir temsilî fixture ile işlevsel doğrulama yapar.
- Büyük corpus, yüksek yük ve platform stres testleri Windows aşamasına
  ertelenir; Mac temsilî sonucu ölçek kabulü sayılmaz.
- Bu karar PostgreSQL yasağını, gerçek embedding zorunluluğunu, güvenlik
  kapılarını veya push onayı sınırını gevşetmez.

### K-014 — Akıllı Kasa gerçek proje RAG kaynağıdır

Kullanıcı 2 Eylül 2026 tarihinde embedding, retrieval, citation, no-answer ve
RAG soru testlerinin tamamının `/Users/mkaracan/Projeler/akilli-kasa` gerçek
projesinden yapılmasını açıkça istemiştir.

- Akıllı Kasa repository'si Zekam tarafından salt okunur source binding olarak
  kullanılır; hiçbir dosyası değiştirilmez, resetlenmez, temizlenmez veya commit'e
  dahil edilmez.
- Mevcut dirty worktree kullanıcı çalışmasıdır ve korunur. Fixture manifesti
  kaynak HEAD'i ile seçilen dosyaların içerik digest'lerini ayrı ayrı bağlar.
- Portable operational kayıtlara mutlak Mac yolu yazılmaz; proje `project_id`,
  logical source binding ve repository-relative locator ile temsil edilir.
- Mac acceptance corpus'u tüm repository değildir. Gerçek source code, test ve
  mimari belgeden seçilen küçük, elle doğrulanabilir ve secrets/PII/gerçek finansal
  veri içermeyen temsilî fixture kullanılır.
- `.env*`, secret, credential, `veriler/`, gerçek finans belgesi, generated DB,
  binary, archive, worktree metadata ve kullanıcıya özel içerik ingestion dışında
  tutulur.
- Exact, lexical, dense, RRF, citation, abstain, restart ve sıfırdan re-index
  soruları yalnız bu bound fixture'ın gerçek içeriği ve exact locator'larıyla
  doğrulanır; Zekam repository'sinden sentetik cevap corpus'u üretilmez.
- Bu source binding Akıllı Kasa'nın kendi PostgreSQL veya runtime verisine erişim
  yetkisi vermez; yalnız izinli repository dosyaları okunur.

---

## 3. Araştırma ve baseline bilgisi

### 3.1 İncelenen güncel Zekam baseline'ı

| Alan | Değer |
|---|---|
| Repository | `mehmet-karacan/zekam` |
| Branch | `main` |
| HEAD | `d95cdac2713df797e42afda020ab6e8e55188031` |
| Son ana yön | Proje sorularını RAG-first yönlendirme |
| İnceleme biçimi | Uzak repository statik analizi; canlı DB/provider çağrısı yapılmadı |
| Eski aktif görev | PostgreSQL Work Graph'tan üretilmiş tamamlanmış projection |
| Mevcut migration ailesi | PostgreSQL için `0001..0078` |
| Mevcut SQLite durumu | Kısıtlı “minimum profile” |

### 3.2 Güncel repo teşhisi

#### Güçlü ve korunacak parçalar

- Domain katmanında kimlik, digest, idempotency, validation, policy, authorization, claim/receipt, memory, routing ve benchmark sözleşmeleri güçlüdür.
- `ZEKAM_HOME` ile source repository fiziksel olarak ayrılmıştır.
- Path traversal, symlink/reparse, duplicate JSON key, secret ref ve payload limitlerinde fail-closed yaklaşım vardır.
- Model ID'lerini birleştirmeme, exact access name koruma ve unknown değerleri tahmin etmeme ilkeleri vardır.
- Benchmark planı, fixture digest'i, independent verifier ve terminal receipt kavramları vardır.
- RAG tasarım belgelerinde exact + lexical + dense + RRF + citation + abstain ilkeleri doğrudur.
- Client lifecycle, compaction, checkpoint, memory candidate, projection ve CAS katmanlarının önemli bir kısmı yeniden kullanılabilir domain mantığı taşır.

#### Kapanması gereken temel açıklar

1. Varsayılan yapı hâlâ PostgreSQL'dir; README, manifest, mimari, doctor ve startup protokolleri PostgreSQL'i core kabul eder.
2. SQLite yalnız project/work/manual JSON vector profiline sahiptir; lifecycle, memory, benchmark, routing ve runtime yetenekleri açıkça reddedilir.
3. Ana application servisleri doğrudan `infrastructure.postgres` repository sınıflarını import eder; gerçek port/adaptör ayrımı tamamlanmamıştır.
4. Proje RAG sorgusu doğrudan PostgreSQL retrieval repository'sine bağlıdır.
5. Knowledge ingest SQLite profilinde uygulanamaz; PostgreSQL'e fallback de yoktur.
6. OpenCode uzak embedding için güvenli discovery/probe yolu vardır; fakat gerçek proje corpus indekslemesinde qualified remote route uygulanmaz.
7. “Local fallback” olarak kullanılan proje embedding yolu gerçek BGE çağrısı değil, deterministik feature-hashing'dir.
8. SQLite vector akışı kullanıcıdan hazır `--vector-json` ister; bu çalışan uçtan uca RAG değildir.
9. Model registry, benchmark, routing, health, campaign ve qualification store'ları PostgreSQL repository'lerine bağlıdır.
10. `GLOBAL_DEFINITION_OF_DONE` içindeki PostgreSQL'e bağlı tamamlandı işaretleri yeni mimari için kanıt sayılmaz; yeni sistemde yeniden doğrulanmalıdır.
11. Mevcut `AKTIF_GOREV.md`, yeni kullanıcı kararlarını temsil etmez ve yaşayan görev olarak değiştirilmelidir.

### 3.3 Ek araştırma girdileri

Bu görev aşağıdaki kaynak ailelerinden pattern uyarlamaktadır:

- Kullanıcının sağladığı ikinci beyin/Obsidian/Markdown/Git/hooks/self-healing transkripti.
- Avenox kullanıcısının 24 repository'si için 1 Eylül 2026 tarihinde üretilmiş ayrıntılı teknik raporlar.
- Güncel embedded operational DB, vector/hybrid search ve analitik motor resmi dokümantasyonu.
- Zekam'ın mevcut memory, retrieval, benchmark, routing, lifecycle, security ve quality dokümanları.

Kaynaklardan pattern alınır; ürün-spesifik teknoloji veya sınırsız otonomi doğrudan kopyalanmaz.

---

## 4. Hedef ürün tanımı

Zekam aşağıdaki beş rolü aynı çekirdekte birleştirecek, fakat veri sahipliklerini ayıracaktır:

```text
ZEKAM
│
├── 1. Yerel AI Çalışma Düzlemi
│   ├── project/work/session/run/state
│   ├── leases/locks/claims/receipts
│   └── local-first runtime
│
├── 2. İkinci Beyin ve Bilgi Alanı
│   ├── global knowledge
│   ├── project-scoped knowledge
│   ├── reports/research/ideas/decisions
│   ├── daylogs/concepts/connections
│   └── Obsidian-compatible Markdown
│
├── 3. Çalışan RAG Düzlemi
│   ├── source discovery and ingestion
│   ├── exact + lexical + dense retrieval
│   ├── RRF/rerank/dedupe/context building
│   ├── citation/no-answer
│   └── local/remote embedding profiles
│
├── 4. Model Laboratuvarı
│   ├── OpenCode model discovery
│   ├── exact model registry
│   ├── adversarial benchmark suites
│   ├── longitudinal analytics
│   └── evidence-bound routing
│
└── 5. Ölçümlü Öz-İyileştirme
    ├── failure/outcome observation
    ├── improvement candidate
    ├── offline evaluation
    ├── shadow/canary
    ├── review/approval
    └── rollback and learning
```

---

## 5. Değiştirilemez mimari invariant'lar

### I-001 — Her mutable veri sınıfının tek authority'si vardır

Aynı veri bağımsız olarak hem DB, hem YAML, hem Markdown içinde authority olarak tutulamaz.

### I-002 — Operational state retrieval sonucundan üretilemez

RAG, memory, Markdown, dashboard veya model cevabı work/run/approval state değiştiremez.

### I-003 — Model çıktısı yalnız adaydır

Model summary, decision, skill, relation, remediation veya routing önerisi otomatik authority değildir.

### I-004 — Claim-before-effect, terminal-receipt-after-effect korunur

Dış yan etki veya geri döndürülemez mutation için önce claim, sonra effect, sonra terminal receipt gerekir.

### I-005 — Timeout sonuç değildir

Timeout; yalnız beklemenin bittiğini gösterir. İş sonucu reconcile edilmeden retry veya success üretilemez.

### I-006 — Replay idempotent ve payload-bound olmalıdır

Aynı idempotency key farklı payload ile gelirse “zaten işlendi” denmez; payload drift hatası verilir.

### I-007 — Unknown veri uydurulmaz

Kaynak bulunamayan alan `unknown/missing` olur; kritik form veya raporda boş bırakılır ve neden açıklanır.

### I-008 — Citation locator ve digest zorunludur

Kanıt kullanılan her cevap exact kaynak, locator ve içerik digest'i taşır. Kanıt yoksa abstain edilir.

### I-009 — Embedding profilleri karışamaz

Model ID, boyut, normalize, distance, prefix, preprocessing veya tokenizer farklıysa ayrı profile ve ayrı index namespace gerekir.

### I-010 — Derived index silinebilir ve yeniden üretilebilir olmalıdır

Vector, lexical ve DuckDB analytics store kaynak değildir. Tamamen silinip kaynak manifestlerinden yeniden kurulabilmelidir.

### I-011 — Private/corporate/public fiziksel ve policy düzeyinde ayrılabilir

Cross-realm retrieval varsayılan kapalıdır. Kurum içi veri kişisel Git/iCloud alanına çıkamaz.

### I-012 — Hook hızlı ve deterministik olmalıdır

Session hook içinde uzun model/provider çağrısı yapılmaz. Hook durable event/outbox bırakır; worker derleme işini sonra yapar.

### I-013 — Self-healing self-authorization değildir

Sistem bozuk index/projection/cache onarabilir; root policy, schema veya approval mekanizmasını kendiliğinden değiştiremez.

### I-014 — Benchmark failure kayıtları silinmez

Timeout, parse error, invalid output, unsafe result veya grader failure sonuçları model lehine filtrelenmez.

### I-015 — Aynı tested model kendi nihai verifier'ı olamaz

Model ailesi ve execution identity bağımsızlığı risk sınıfına göre zorunludur.

### I-016 — Git transaction store değildir

Git sürüm, senkron ve public-safe bilgi geçmişidir; lock, queue, receipt, secret, raw transcript veya DB dump taşımaz.

### I-017 — No silent fallback

Storage, embedding, reranker, model, tool veya provider fallback'i açık state ve reason üretmeden devreye giremez.

### I-018 — Freshness ve provenance confidence'tan önce gelir

Stale veya kaynağı bilinmeyen yüksek-confidence bilgi current truth sayılmaz.

### I-019 — Platform farkları test edilir

macOS ARM64 ve Windows x64 davranışı aynı varsayılmaz; path, lock, process, extension, SQLite sürümü ve packaging ayrı test edilir.

### I-020 — Güvenli davranış “başarı” kadar test edilir

Sistem yanlış girdiyi reddedemiyorsa, hatada state'i koruyamıyorsa veya recovery yapamıyorsa özellik tamamlanmış sayılmaz.

---

## 6. Source-of-truth ve türetilmiş veri matrisi

| Veri sınıfı | Authority | Türetilmiş görünüm/index | Silinirse davranış |
|---|---|---|---|
| Görev scope'u | `AKTIF_GOREV.md` exact digest | Operational progress projection | Dosya yoksa apply bloklanır |
| Project/work/session/run state | Operational Store | Markdown/UI/analytics | Backup'tan restore gerekir |
| Claim/receipt/audit | Operational Store append-only | Reports/DuckDB | Silinemez; restore gerekir |
| Kullanıcı yazdığı global bilgi | `.zekam/global/**` Markdown | RAG index/Obsidian graph | Kaynak dosyadan re-index |
| Kullanıcı yazdığı proje bilgisi | `.zekam/projeler/<slug>/**` Markdown | RAG index/context pack | Kaynak dosyadan re-index |
| Proje resmi dokümanı | Projenin kendi reposu | Zekam RAG index | Repo'dan re-index |
| Ham belge/artifact | Local CAS/filesystem | normalized/chunks/index | CAS/backup'tan restore |
| Memory candidate/active state | Operational Store + evidence refs | Markdown memory projection | Projection rebuild |
| Model identity/current state | Operational Store | YAML/Markdown reports | DB snapshot/rediscovery |
| Model discovery snapshot | İmzalı/hash'li local JSON artifact | Registry reconciliation | Yeniden discovery |
| Benchmark suite/prompt/grader | Zekam source repo | Plan manifest | Git'ten geri gelir |
| Benchmark raw result | Immutable run artifact/CAS | DuckDB aggregate/report | Artifact'tan rebuild |
| Telemetry raw events | Atomic event segments + manifest | DuckDB | Segmentlerden rebuild |
| Vector/lexical index | Derived local index | — | Tam rebuild |
| DuckDB analytics | Derived | Dashboard/report | Tam rebuild |
| Secret | OS keychain/approved secret backend | Logical SecretRef | Secret backend'ten çözülür |

---

## 7. Hedef `ZEKAM_HOME` yerleşimi

Mevcut core/home ayrımı korunacak; layout `zekam-home-layout/v2` olarak sürümlenecektir. Dizin adları cross-platform sorunlarını azaltmak için ASCII tutulacaktır.

```text
~/.zekam/
│
├── layout.json
├── config.yaml
│
├── state/
│   ├── operational.db
│   ├── snapshots/
│   ├── backups/
│   └── manifests/
│
├── global/
│   ├── raporlar/
│   ├── arastirmalar/
│   ├── fikirler/
│   ├── kararlar/
│   ├── referanslar/
│   ├── gunlukler/
│   ├── kavramlar/
│   ├── baglantilar/
│   ├── bellek/
│   └── politikalar/
│
├── projeler/
│   └── <project-slug>/
│       ├── PROJECT.yaml
│       ├── baglantilar/
│       ├── raporlar/
│       ├── arastirmalar/
│       ├── fikirler/
│       ├── kararlar/
│       ├── planlar/
│       ├── gorevler/
│       ├── referanslar/
│       ├── notlar/
│       ├── bellek/
│       ├── artifacts/
│       └── runtime/
│
├── modeller/
│   ├── registry/
│   ├── discovery/
│   ├── providers/
│   ├── profiller/
│   ├── observations/
│   ├── routing/
│   └── raporlar/
│
├── benchmarklar/
│   ├── planlar/
│   ├── kosular/
│   ├── artifacts/
│   ├── baselines/
│   ├── raporlar/
│   └── quarantine/
│
├── knowledge-index/
│   ├── exact/
│   ├── lexical/
│   ├── vector/
│   ├── manifests/
│   ├── snapshots/
│   └── quarantine/
│
├── analytics/
│   ├── zekam.duckdb
│   ├── imports/
│   ├── exports/
│   └── manifests/
│
├── telemetry/
│   ├── events/
│   ├── manifests/
│   └── quarantine/
│
├── artifacts/
│   └── sha256/
│
├── runtime/
│   ├── locks/
│   ├── leases/
│   ├── outbox/
│   ├── spool/
│   ├── health/
│   ├── tmp/
│   └── recovery/
│
├── gelen-belgeler/
├── inbox/
├── archive/
├── sandboxlar/
├── yerel/
└── secrets/
```

### 7.1 Proje dizini kuralları

- `PROJECT.yaml`, proje kimliği ve source binding metadata'sının insan okunur projection'ıdır; operational store'daki project ID'yi icat etmez.
- Projenin kendi `README`, `docs`, ADR ve runbook'ları resmi ürün dokümanıdır.
- `.zekam/projeler/<slug>/` Mehmet'in o projeye ilişkin araştırma, fikir, karar geçmişi, AI çalışma notu ve çapraz bağlantı alanıdır.
- Aynı belge iki klasöre kopyalanmaz; canonical owner ve link kullanılır.
- Bir araştırma bir projeye özgüyse proje altında, bağımsızsa `global/arastirmalar` altında yaşar.
- Cross-project link açık relation metadata'sıyla kurulur.

### 7.2 Generated Markdown kuralları

Generated dosyalar front matter içinde en az şunları taşır:

```yaml
schema: zekam-generated-note/v1
generated: true
source_refs: []
source_digests: []
generated_at: "...Z"
generator_version: "..."
freshness: current
editable: false
```

İnsan düzeltmesi generated dosyaya doğrudan yazılmaz; correction/feedback kaydı oluşturulur ve projection yeniden üretilir.

---

## 8. Teknoloji seçim kapıları

Polyglot mimari, rastgele çok teknoloji eklemek anlamına gelmez. Aşağıdaki bake-off tamamlanmadan motor adı hard-code edilmez. Sonuçlar ADR, ölçüm dosyası ve test kanıtıyla bağlanır.

### 8.1 Operational Store adayları

Kısa liste:

1. SQLite tabanlı production adapter.
2. Turso Database / `pyturso` tabanlı embedded adapter.
3. Gerekirse yalnız karşılaştırma için mevcut SQLite minimum adapter.

`libSQL` yeni projenin varsayılanı olarak kabul edilmez; güncel resmi ekosistem yönü ayrıca doğrulanır.

#### Hard gate'ler

Aday aşağıdakilerin tamamını sağlamalıdır:

- ayrı server ve Docker istememeli,
- macOS ARM64 ve Windows x64 üzerinde kurulmalı,
- `uv sync`/wheel kurulumuyla reproducible olmalı,
- transaction, foreign key, unique ve check constraint davranışı kanıtlanmalı,
- idempotency ve payload-drift koruması sağlamalı,
- crash sonrası bütünlük testi geçmeli,
- backup/restore API veya güvenli snapshot yöntemi olmalı,
- aynı DB dosyasını network filesystem üzerinde kullanmaya zorlamamalı,
- secret/credential gerektirmemeli,
- lisans ve dağıtım koşulları kabul edilebilir olmalı,
- Python 3.11+ hedef matrisinde çalışmalı,
- schema migration ve integrity doctor desteği üretilebilmeli.

#### Ölçüm seti

- 10.000 project/work/event insert,
- 100.000 append-only event,
- 4 local producer üzerinden serialized write,
- concurrent read while write,
- process kill during commit,
- process kill during checkpoint/snapshot,
- disk-full simulation,
- read-only directory,
- stale/dead/same-PID lock,
- schema drift/unexpected trigger,
- DB file truncation/corruption detection,
- backup restore digest parity.

#### SQLite özel güvenlik kapısı

- Runtime SQLite sürümü `doctor` tarafından okunur.
- Çok bağlantılı WAL kullanımında 2026 WAL-reset düzeltmesini içeren güvenli sürüm/backport doğrulanmadan concurrency profili etkinleşmez.
- Güvenli sürüm kanıtlanamıyorsa tek-writer coordinator ve daha muhafazakâr journal/checkpoint profili zorunludur.
- `SQLITE_BUSY` kontrollü retry bütçesi ve terminal failure state'i taşır; sonsuz retry yoktur.
- WAL, SHM ve DB dosyası birlikte snapshot edilir veya SQLite backup API kullanılır.

#### Seçim kuralı

- Hard gate geçmeyen aday elenir.
- Kalan adaylar portability, durability, concurrency, dependency risk, packaging, observability ve bakım maliyetiyle puanlanır.
- Sonuç eşitse daha az dependency ve daha olgun recovery yolu olan aday seçilir.
- Seçim `ADR-LOCAL-STORE-001` ile kaydedilir.

### 8.2 Knowledge Index adayları

Kısa liste:

- LanceDB OSS local,
- Zvec in-process,
- SQLite FTS5 + sqlite-vec,
- Qdrant local/edge yalnız karşılaştırma profili,
- brute-force NumPy/feature hash yalnız test baseline'ı; production dense motoru değildir.

#### Hard gate'ler

- Docker/server/network gerektirmemeli,
- macOS ARM64 ve Windows x64 kurulumu geçmeli,
- persistent local index sağlamalı,
- vector dimension/profile isolation desteklemeli,
- metadata/project/realm filtrelerini candidate limitinden önce uygulamalı,
- exact identifier ve lexical kanal ayrı tutulabilmeli,
- source row ID ve citation locator'a geri bağlanabilmeli,
- atomic version activation veya generation-swap uygulanabilmeli,
- crash sonrası stale/corrupt index anlaşılabilmeli,
- source manifestten tam rebuild yapılabilmeli,
- 50.000 ve 250.000 chunk corpus'unda ölçülebilmeli,
- hidden network/provider çağrısı yapmamalı,
- library version ve index formatı manifestte tutulmalı.

#### Zekam gerçek veri bake-off'u

Aşağıdaki query sınıfları kullanılır:

- Türkçe doğal dil,
- İngilizce teknik doküman,
- Oracle PL/SQL object adı,
- dosya yolu ve class/function identifier,
- Jira/talep/defect ID,
- typo/fuzzy sorgu,
- anlamca yakın fakat kelimesi farklı sorgu,
- exact keyword ile semantic conflict,
- no-answer sorgusu,
- cross-project sızıntı denemesi.

Metrikler:

- Recall@1/5/10,
- MRR,
- nDCG@10,
- exact top-1,
- citation precision,
- no-answer precision/recall,
- stale exclusion,
- index build/update süresi,
- query p50/p95,
- disk ve memory footprint,
- crash/rebuild süresi,
- installation success rate.

Seçim `ADR-KNOWLEDGE-INDEX-001` ile kaydedilir. Production'da tek knowledge index stack seçilir; üç aday birden kalıcı dependency yapılmaz.

### 8.3 Analytics Store

DuckDB yalnız analitik ve raporlama için kullanılacaktır.

- Operational mutation DuckDB üzerinden yapılmaz.
- Birden çok process DuckDB dosyasına doğrudan yazmaz.
- Raw benchmark ve telemetry olayları immutable artifact/event segmentlerinde tutulur.
- Tek analytics writer veya batch importer DuckDB'yi günceller.
- DuckDB tamamen silinip raw event/run artifact'larından yeniden üretilebilir.
- Aynı veri operational DB ve DuckDB arasında çift authority oluşturmaz.

Seçim ve import sözleşmesi `ADR-ANALYTICS-001` ile kaydedilir.

### 8.4 Teknoloji bütçesi

- Zekam core için arka planda çalışan DB server sayısı: **0**.
- Kalıcı DB/index motoru sınıfı: **en fazla 3**.
- Redis, Celery, MinIO, PostgreSQL veya ayrı queue server varsayılan dependency değildir.
- Gereksiz “ileride lazım olur” altyapısı eklenmez.

---

## 9. Uygulama portları ve bağımlılık yönü

Application/domain katmanı hiçbir somut DB motorunu import etmeyecektir.

### 9.1 Zorunlu portlar

```text
OperationalStore
ProjectStore
WorkStore
RuntimeLedger
LeaseStore
EffectLedger
SessionStore
CheckpointStore
MemoryStore
ModelRegistryStore
BenchmarkStateStore
RoutingStateStore
ArtifactStore
KnowledgeSourceStore
ExactSearchIndex
LexicalSearchIndex
VectorSearchIndex
EmbeddingProvider
RerankerProvider
AnalyticsSink
ModelDiscoveryProvider
Clock
IdGenerator
LockProvider
BackupProvider
```

### 9.2 Port kuralları

- Portlar domain tipleri döndürür; DB cursor/row/SQL dışarı sızmaz.
- Transaction boundary application service tarafından açıkça tanımlanır.
- External provider çağrısı açık DB transaction içinde yapılmaz.
- Her adapter capability manifest'i sunar.
- Desteklenmeyen capability fail-closed ve typed error üretir.
- Production adapter ile fake/in-memory adapter aynı contract testlerini geçer.
- Adapter fallback'i composition root dışında yapılmaz.

### 9.3 Import kapısı

Final durumda aşağıdakiler sıfır olmalıdır:

- `src/zekam/application/**` altında `infrastructure.postgres` importu,
- `src/zekam/interfaces/**` altında `psycopg`/PostgreSQL repository importu,
- production runtime'da `connect(database_settings)` PostgreSQL çağrısı,
- core testlerinde çalışan PostgreSQL container fixture'ı,
- `pyproject.toml` production dependency'sinde `psycopg`,
- README/startup içinde Zekam core için Docker/PostgreSQL önkoşulu.

---

## 10. Temiz bootstrap ve legacy davranışı

### 10.1 Yeni kurulum akışı

```text
zekam init
  -> home path güvenliği
  -> layout v2 preview
  -> operational engine capability probe
  -> operational.db şema v1 atomic bootstrap
  -> artifact/CAS bootstrap
  -> knowledge index adapter probe
  -> analytics directory bootstrap
  -> local machine profile detection
  -> model/embedding provider discovery preview
  -> config + bootstrap receipt atomic publish
  -> doctor
```

### 10.2 Bootstrap atomicity

- Başarısız DB veya index bootstrap'ında `config.yaml` seçimi yayınlanmaz.
- Temp/staging dizini final dizine atomic rename ile alınır.
- Aynı bootstrap planı iki kez uygulanırsa duplicate oluşturmaz.
- Aynı idempotency key farklı plan digest'iyle gelirse reddedilir.
- Her aşamadan sonra process-kill test edilir.
- Final `bootstrap_receipt` oluşmadan sistem initialized sayılmaz.

### 10.3 Eski PostgreSQL konfigürasyonu görülürse

- Zekam PostgreSQL'e bağlanmaz.
- `doctor` `legacy-postgresql-config-detected` finding'i üretir.
- Kullanıcı verisi silinmez.
- Sistem tarafından üretilmiş eski PG runtime/config izleri preview ile karantinaya alınabilir.
- Yeni operational store ayrı path ve şema v1 ile kurulur.
- Eski DB'den import önerilmez ve import komutu yazılmaz.
- Git history eski kodu zaten koruduğu için aktif source tree içinde PostgreSQL kodu tarihsel arşiv olarak tutulmak zorunda değildir.

### 10.4 İlk veri üretimi

Temiz bootstrap sonrasında sırayla:

1. Zekam repo source snapshot'ı üretilir.
2. Kullanıcı tarafından kayıtlı project binding'leri keşfedilir.
3. Global/proje knowledge dosyaları manifestlenir.
4. Cihaz embedding provider profili doğrulanır.
5. Fresh knowledge index oluşturulur.
6. OpenCode model catalog'u secretsiz biçimde keşfedilir.
7. Model registry reconcile preview üretilir.
8. Benchmark baseline planı hazırlanır; kullanıcı onayı olmadan canlı çağrı yapılmaz.
9. Runtime/telemetry yalnız bu andan sonraki olaylarla oluşur.

---

## 11. Operational Store veri modeli

Şema eski PostgreSQL tablolarının mekanik kopyası olmayacaktır. Aşağıdaki domain ihtiyaçlarından sıfırdan oluşturulur.

### 11.1 Sistem ve schema

| Tablo ailesi | Zorunlu amaç |
|---|---|
| `system_meta` | product/layout/schema/instance kimliği |
| `schema_revision` | forward migration, checksum, applied_at |
| `config_revision` | sanitize edilmiş config digest ve activation |
| `bootstrap_receipt` | fresh init plan/result evidence |
| `artifact_ref` | CAS digest, media type, size, classification |

### 11.2 Project ve source

| Tablo ailesi | Zorunlu amaç |
|---|---|
| `project` | stable ID, slug, display name, status |
| `project_alias` | deterministik alias çözümü |
| `source_binding` | project → gerçek source root/repository |
| `source_snapshot` | HEAD/tree/content/config digest, captured_at |
| `project_capability_profile` | teknoloji ve workload profili |

### 11.3 Work ve execution

| Tablo ailesi | Zorunlu amaç |
|---|---|
| `work_item` | identity, kind, project, current state |
| `work_revision` | immutable revision, payload digest |
| `work_event` | append-only state transition event |
| `run` | execution attempt identity/status/budget |
| `run_step` | DAG step, dependency, status, evidence |
| `checkpoint` | resumable progress ve source revision |
| `lease` | owner, fence, heartbeat, expiry |
| `logical_lock` | resource, owner identity, generation |

### 11.4 Effect ve recovery

| Tablo ailesi | Zorunlu amaç |
|---|---|
| `effect_claim` | effect öncesi exact claim |
| `effect_receipt` | success/failure/unknown terminal evidence |
| `outbox` | transaction sonrası external work |
| `recovery_case` | claim var receipt yok / ambiguous effect |
| `repair_candidate` | bounded self-repair önerisi |
| `repair_receipt` | review/apply/recheck kanıtı |

### 11.5 Session ve continuity

| Tablo ailesi | Zorunlu amaç |
|---|---|
| `session` | client/device/project/work identity |
| `session_event` | typed lifecycle event |
| `continuity_checkpoint` | pre-compaction/close durable state |
| `hydration_receipt` | session start context manifest |
| `close_receipt` | session end flush/compile/handoff |
| `context_manifest` | model-visible exact fragment list |

### 11.6 Memory ve learning

| Tablo ailesi | Zorunlu amaç |
|---|---|
| `memory_candidate` | untrusted candidate + provenance |
| `memory_review` | independent evaluation |
| `memory_record` | active/superseded/revoked identity |
| `memory_revision` | immutable content revision |
| `memory_relation` | typed source/related/supersedes link |
| `memory_usage_event` | gerçekten model-visible kullanım |
| `memory_outcome` | verified task outcome ilişkisi |
| `skill_candidate` | tekrarlı workflow önerisi |
| `skill_revision` | versioned tested skill contract |

### 11.7 Model, benchmark ve routing state

| Tablo ailesi | Zorunlu amaç |
|---|---|
| `model_identity` | exact canonical ID ve access name |
| `model_revision` | observed provider fingerprint/revision |
| `model_availability` | device/client/provider scope |
| `model_health_observation` | health/probe evidence |
| `benchmark_plan` | suite/model/policy/environment digest |
| `benchmark_run` | immutable run identity/status |
| `benchmark_trial` | repetition/failure/metrics/artifact refs |
| `routing_policy_activation` | versioned policy digest/approval |
| `routing_decision` | input evidence, candidates, reasons, confidence |

### 11.8 Veri tipi kuralları

- Boolean değerler yalnız `0/1` ve CHECK ile saklanır; Python `bool` integer yerine kabul edilmez.
- Score/probability alanları `0..1` veya açık tanımlı range CHECK taşır.
- Para integer micro-unit olarak saklanır; float para kullanılmaz.
- Timestamp UTC canonical formatta saklanır ve parse edilmeden kabul edilmez.
- JSON duplicate key, non-finite sayı ve unknown field doğrulaması application boundary'de yapılır.
- Digest yalnız `sha256:<64-lower-hex>` biçimindedir.
- Completed/terminal state zorunlu evidence/receipt ister.
- Append-only kayıt UPDATE/DELETE ile değiştirilemez.
- Receipt ve audit tablolarında cascade delete yoktur.
- Foreign key enforcement her connection açılışında doğrulanır.

---

## 12. Knowledge source ve dosya düzlemi

### 12.1 Kaynak türleri

- Zekam core docs/code/config,
- kayıtlı proje repo dosyaları,
- `.zekam/global/**`,
- `.zekam/projeler/<slug>/**`,
- kullanıcı tarafından onaylanan PDF/DOCX/TXT/MD/image,
- archive ve izinli directory,
- database metadata snapshot'ları,
- benchmark/report artifacts.

### 12.2 Güven sınırı

- Belge içindeki talimat system instruction değildir.
- Repo README içindeki komut otomatik çalıştırılmaz.
- Source scan hiçbir hook/build/script yürütmez.
- Secret/PII sınıflandırması indexing öncesi yapılır.
- Remote embedding'e gidecek içerik data classification ve disclosure policy'den geçer.

### 12.3 Source manifest

Her kaynak en az aşağıdaki metadata'yı taşır:

```text
source_id
owner_scope
project_id | global
source_kind
portable_source_ref
canonical_path_or_url_digest
content_digest
source_revision
media_type
data_classification
parser_profile_digest
chunk_profile_digest
captured_at
freshness
provenance_refs
```

### 12.4 Incremental ingestion

- Source content digest değişmediyse tekrar parse/embed edilmez.
- Parser/chunker/embedding profile değiştiyse yeni generation oluşturulur.
- Yeni generation tamamen hazır olmadan active pointer değiştirilmez.
- Eski generation rollback için korunur.
- Partial generation sorguya girmez.
- Orphan CAS/index fragment doctor tarafından bulunur ve reconcile planına alınır.

---

## 13. Embedding provider düzlemi

### 13.1 Provider portu

```text
EmbeddingProvider
├── describe() -> profile capability
├── probe(public_fixture) -> evidence
├── embed_documents(batch, policy) -> vectors + receipt
├── embed_query(text, profile) -> vector + receipt
└── health() -> fresh observation
```

### 13.2 Zorunlu `EmbeddingProfile`

```text
profile_id
display_name
provider_kind: local | remote
provider_identity_digest
exact_model_id
model_revision_fingerprint
dimension
vector_dtype
normalized
distance_metric
query_prefix
passage_prefix
preprocessor_digest
tokenizer_digest
batch_policy_digest
device_scope
data_classification_allowlist
verified_at
probe_evidence_digest
```

### 13.3 MacBook local BGE rotası

- Mevcut yerel BGE runtime/path gerçek olarak keşfedilir.
- Model dosyası hash/fingerprint'i alınır; model binary Git'e eklenmez.
- Query ve passage preprocessing profile bağlanır.
- Offline public fixture üzerinde duplicate determinism, semantic margin, dimension ve non-finite kontrolü yapılır.
- Network kapalıyken corpus/query embedding E2E çalışmalıdır.
- Model eksikse “feature hash embedding”e sessiz düşülmez.

### 13.4 Windows/OpenCode uzak embedding rotası

- OpenCode config exact provider/model ID ile okunur.
- Credential yalnız SecretRef/environment locator'dan process memory'de çözülür.
- Endpoint URL/secret durable state'e yazılmaz; identity digest tutulur.
- Önce public synthetic probe, sonra policy-allowed corpus çağrısı yapılır.
- Kurum içi model kimliği prefix'i kaldırılmaz veya normalize edilerek başka kimlikle birleştirilmez.
- Timeout, redirect, invalid JSON, wrong dimension, NaN/Inf ve partial batch fail-closed'dur.

### 13.5 Cross-device index kuralı

- Mac ve Windows binary vector index'i senkronize edilmek zorunda değildir.
- Canonical source manifestleri senkron olabilir; her cihaz kendi profile'ıyla yeniden index üretir.
- İki provider'ın aynı model olduğu yalnız exact model ID ile varsayılmaz; vector parity/equivalence probe gerekir.
- Equivalence kanıtlanmazsa iki ayrı profile namespace tutulur.

### 13.6 Degraded mode

Aşağıdaki durumlar açıkça görünür olmalıdır:

- `embedding-unavailable`,
- `embedding-profile-stale`,
- `embedding-dimension-drift`,
- `remote-disclosure-not-authorized`,
- `lexical-only-degraded`,
- `index-rebuild-required`.

Degraded mode cevaplarında dense kanal kullanılmış gibi rapor verilmez.

---

## 14. Çalışan RAG düzlemi

### 14.1 Retrieval kanalları

1. **Exact:** project ID, defect/talep ID, dosya yolu, SQL object, class/function, quoted phrase.
2. **Lexical:** BM25/FTS tabanlı kelime ve teknik terim araması.
3. **Dense:** doğrulanmış embedding profiliyle semantic arama.
4. **Optional entity/relation:** project, model, decision, work ve memory ilişkileri.

### 14.2 Fusion

- Ham lexical ve vector skorları doğrudan toplanmaz.
- Reciprocal Rank Fusion varsayılan `k=60` ile uygulanır ve config digest'ine bağlanır.
- Exact identifier eşleşmesi düşük dense skor nedeniyle kaybolmaz.
- Tie-break deterministiktir.
- Dedupe content/source digest üzerinden yapılır.
- Reranker opsiyoneldir; hata veya kalite düşüşünde açıkça base fusion sırasına dönülür.

### 14.3 Scope routing

```text
query
  -> active project/work context
  -> explicit project/global hints
  -> scope policy
  -> allowed source sets
  -> exact/lexical/dense candidates
```

- Proje sorusu önce o proje scope'unda aranır.
- Global bilgi yalnız policy ve relevance ile eklenir.
- Cross-project arama explicit intent/policy olmadan yapılmaz.
- Corporate/personal realm karışımı yoktur.

### 14.4 Citation sözleşmesi

Her selected context fragment en az şunları taşır:

```text
source_id
source_ref
source_revision
content_digest
chunk_id
locator_type
locator
project_scope
retrieval_channels
rank_trace
```

Model cevabındaki citation bu identity ile doğrulanır. Modelin uydurduğu source ref kabul edilmez.

### 14.5 No-answer davranışı

- Hiç hit yoksa `abstained-no-hit`.
- Hit var fakat yeterli kanıt yoksa `abstained-low-evidence`.
- Index stale/eksikse `abstained-index-unavailable`.
- Remote embedding yok fakat lexical yeterliyse cevap `lexical-only-degraded` etiketi taşır.
- Model çağrısı öncesi evidence gate başarısızsa model hiç çağrılmaz.

### 14.6 RAG kalite kapısı

İlk release için en az 100 soruluk, gerçek Zekam/proje kullanımını temsil eden golden corpus hazırlanır:

- 20 exact identifier,
- 20 Türkçe semantic,
- 15 İngilizce teknik,
- 15 code/SQL object,
- 10 project/global scope,
- 10 stale/superseded,
- 10 no-answer/adversarial.

Zorunlu eşikler:

- exact identifier top-1: `%100`,
- citation locator validity: `%100`,
- cross-project/realm leakage: `0`,
- fabricated citation: `0`,
- no-answer setinde unsupported factual answer: `0`,
- Recall@10: en az `0.85`,
- MRR: en az `0.75`,
- nDCG@10: en az `0.80`,
- index freshness mismatch'in sessiz geçmesi: `0`.

Eşikler gerçek baseline sonrasında daha yukarı alınabilir; aşağı çekilmesi ayrı approval ister.

### 14.7 RAG dikey kabul akışı

```text
fresh home
-> project add
-> repo/global knowledge scan
-> source manifest
-> real embedding provider probe
-> parse/chunk/embed
-> exact + lexical + dense indexes
-> ask project question
-> cited answer or explicit abstain
-> change source file
-> incremental re-index
-> stale generation excluded
-> restart
-> same query continuity
```

Bu akış macOS local BGE ve Windows/OpenCode remote embedding profillerinde ayrı E2E test edilir.

### 14.8 Context Vault ilişkisi

- Context Vault ayrı projedir ve kendi storage kararına sahiptir.
- Zekam RAG, Context Vault DB'sine doğrudan bağlanmaz.
- İleride Context Vault API'si optional `KnowledgeProvider` adapter'ı olabilir.
- Zekam core RAG'i Context Vault veya onun PostgreSQL altyapısı olmadan çalışmalıdır.

---

## 15. Global ve proje-özel bilgi yaşam döngüsü

### 15.1 Bilgi türleri

- `report`
- `research`
- `idea`
- `decision`
- `reference`
- `note`
- `daylog`
- `concept`
- `connection`
- `failure`
- `lesson`
- `skill`
- `handoff`

### 15.2 Owner scope

Her kayıt tam bir owner scope taşır:

- `global-user`,
- `project:<id>`,
- `work:<id>`,
- `run:<id>`,
- `session:<id>`.

`run` ve `session` içeriği otomatik kalıcı bilgi değildir; candidate compiler üzerinden değerlendirilir.

### 15.3 Proje metadata dosyası

Her proje için `PROJECT.yaml` projection'ında en az:

```yaml
schema: zekam-project-projection/v1
project_id: "..."
slug: "..."
display_name: "..."
status: active
source_bindings: []
related_projects: []
technologies: []
database_metadata: []
important_docs: []
knowledge_scopes: []
last_source_snapshot: "..."
projection_digest: "sha256:..."
```

Project DB credential/connection secret'i bu dosyaya yazılmaz.

### 15.4 WikiLink ve relation

- WikiLink convenience view'dur; canonical relation operational store'dadır.
- Model relation önerir, doğrulanmış source refs olmadan active relation oluşmaz.
- Broken link, duplicate concept ve orphan note doctor tarafından raporlanır.
- Full vault her session'a yüklenmez; index/context compiler kullanılır.

---

## 16. Session, hook, daylog ve ikinci beyin sürekliliği

### 16.1 Ortak lifecycle event'leri

```text
SESSION_START
USER_TURN_COMMITTED
ASSISTANT_TURN_COMMITTED
TOOL_EFFECT_CLAIMED
TOOL_EFFECT_COMPLETED
CHECKPOINT_REQUESTED
PRE_COMPACTION
POST_COMPACTION
PRE_CLOSE
SESSION_CLOSED
CRASH_RECOVERED
```

### 16.2 Hook davranışı

- Hook typed, küçük ve content-bounded event yazar.
- Hook raw secret veya sınırsız transcript kopyalamaz.
- Hook provider/model çağrısı yapmaz.
- Durable spool/outbox write başarısızsa required event fail-loud olur.
- Background process spawn edilmiş olması başarı sayılmaz.
- Outbox item terminal receipt olmadan complete sayılmaz.

### 16.3 Session start

Session start şu sırayla çalışır:

1. home/layout/config/operational integrity,
2. stale lock ve recovery case kontrolü,
3. active task digest,
4. active project/work/checkpoint,
5. security/policy fragments,
6. current decisions/failures/skills,
7. relevant project/global retrieval,
8. bounded context manifest,
9. hydration receipt.

### 16.4 Pre-compaction

- Compaction öncesi durable checkpoint zorunludur.
- Pending effect, outbox veya unpersisted session delta varsa compaction ACK verilmez.
- Context özeti authority olarak değil continuity artifact olarak saklanır.
- Post-compaction golden resume testi gerekli bilgilerin korunduğunu doğrular.

### 16.5 Session close

Close pipeline:

```text
freeze session delta
-> validate source identities
-> append lifecycle events
-> produce daylog candidate
-> compile memory/decision/skill/failure candidates
-> persist checkpoint/handoff
-> refresh safe projections
-> terminal close receipt
```

Close hook uzun provider çağrısını beklemez; compile worker terminal receipt'i ayrı üretir. Required close state açıkça `pending`, `complete` veya `recovery-required` olur.

### 16.6 Daylog

Daylog en az:

- ne yapıldı,
- hangi work/run kapsamında,
- hangi kararlar alındı,
- ne başarısız oldu,
- hangi kanıtlar oluştu,
- neler kaldı,
- sonraki güvenli adım,
- source/receipt refs

alanlarını taşır. Daylog model summary'si olarak active fact değildir.

---

## 17. Memory ve skill öğrenme döngüsü

### 17.1 Memory sınıfları

- working,
- episodic,
- semantic,
- procedural,
- preference,
- failure.

### 17.2 Promotion akışı

```text
observation
-> candidate
-> source/evidence validation
-> duplicate/conflict/stale checks
-> independent review where required
-> approval/policy gate
-> active revision
-> usage/outcome monitoring
-> supersede/revoke/retire
```

### 17.3 Failure dersi

- Tek olay evrensel ders üretmez.
- Aynı failure signature için en az iki bağımsız observation veya güçlü external evidence gerekir.
- Failure card: symptom, environment, root cause, unsafe workaround, safe remediation, verification, source refs.
- Lesson ve skill source failure'lara linklenir.

### 17.4 Skill candidate

Her skill manifesti:

```text
skill_id/version
purpose/triggers
inputs/outputs
required tools
steps
checks
risks
permissions ceiling
source evidence
benchmark/evaluation
rollback/deprecation
```

- Model kendi yazdığı skill'i kendi başına global activate edemez.
- Skill mevcut tool permission üst sınırını genişletemez.
- Fake adapter ve sandbox dry-run geçmeden review'a çıkamaz.

### 17.5 Mem0

- Mem0 zorunlu core değildir.
- İleride instant/working cache veya external non-authority adapter olabilir.
- Mem0 kesintisi operational memory'yi durduramaz.
- External memory drift'te local authority geçerlidir.

---

## 18. Yerel model registry ve discovery

### 18.1 Model identity

Aşağıdakiler ayrı alanlardır ve birleştirilemez:

```text
canonical_model_id
access_name
backend_model
provider_id
provider_family
endpoint_identity_digest
modality
device_scope
location: local | remote
```

Provider prefix'i veya access name “güzelleştirmek” için kaldırılmaz. Örneğin erişim kimliği ile backend model adı aynı şey değildir.

### 18.2 Discovery snapshot

OpenCode discovery:

- config dosyasını secretsiz ve fail-closed okur,
- duplicate JSON key ve unknown field'i reddeder,
- exact enabled provider/model listesini çıkarır,
- endpoint ve credential değerini değil identity/locator digest'ini tutar,
- `new`, `unchanged`, `changed`, `missing`, `ambiguous`, `quarantined` farklarını üretir,
- snapshot immutable artifact olarak kaydedilir.

### 18.3 Revision drift

Aynı model ID'nin davranışı değişebilir. Aşağıdakiler revision şüphesi üretir:

- dimension veya modality değişimi,
- public probe fingerprint değişimi,
- response contract değişimi,
- latency/reliability dağılımında kalıcı shift,
- capability benchmark'ta anlamlı regression,
- OpenCode/provider binding digest değişimi.

Revision şüphesinde eski qualification `stale` olur; fresh benchmark olmadan routing primary olamaz.

### 18.4 Model profile raporu

Her model için local projection:

```text
identity
availability by device/client
verified modalities
health observations
benchmark task breakdown
strengths/weaknesses
latency/reliability/cost evidence
known failures
preferred/forbidden tasks
last seen
last benchmark
routing confidence
```

Unknown bilgi `unknown` kalır; internetteki pazarlama iddiasıyla otomatik doldurulmaz.

---

## 19. Benchmark laboratuvarı

### 19.1 Amaç

Kurum içi OpenCode modellerini, Mehmet'in gerçek işlerine göre ölçmek ve Zekam routing kararlarına güvenilir evidence sağlamaktır.

### 19.2 Suite aileleri

```text
benchmarks/suites/
├── sql-plsql/
├── code-repair/
├── code-review/
├── architecture/
├── rag-retrieval/
├── tool-use/
├── agentic-workflow/
├── long-context/
├── document-analysis/
├── structured-output/
├── safety-policy/
├── embedding-retrieval/
├── reranking/
└── creative-tournament/
```

### 19.3 Task manifest

Her task en az:

```yaml
schema: zekam-benchmark-task/v1
task_id: "..."
version: 1
workload: "..."
modality: "..."
prompt_ref: "..."
fixture_refs: []
hidden_key_ref: "..."
grader_ref: "..."
required_tools: []
forbidden_effects: []
data_classification: public
repetitions: 5
timeout_seconds: 120
max_input_tokens: null
max_output_tokens: null
scoring_dimensions: []
pass_thresholds: {}
```

### 19.4 Negative/adversarial fixture ilkesi

Suite'ler yalnız doğru çözümü istemez; aşağıdakileri de ölçer:

- yanıltıcı görünen ama hatasız kod,
- görünmeyen gerçek root cause,
- çelişkili instructions,
- eksik veri ve “uydurma” tuzağı,
- wrong type/boundary,
- duplicate/replay,
- tool side-effect yasağı,
- unsafe prompt injection,
- stale source,
- context overflow,
- timeout/recovery,
- malformed provider output,
- hidden answer key leakage denemesi.

### 19.5 Run manifest ve provenance

Her trial:

```text
run_id/trial_id/repetition
exact model identity and revision
client/OpenCode version
device/OS/Python
provider binding digest
prompt/fixture/grader/policy hashes
generation parameters
start/end/latency
input/output token counts
raw response artifact digest
normalized result digest
grader version and evidence
status/failure category
retry count
human correction count
```

Raw model çıktısı post-edit edilmez. Düzeltme gerekiyorsa `raw`, `normalized`, `fixed` ayrı artifact olarak tutulur.

### 19.6 “Benchmarkları çalıştır” komut akışı

```text
model discovery
-> registry reconciliation preview
-> eligible model set
-> exact suite/task selection
-> call count/token/time budget
-> plan digest
-> dry-run report
-> explicit approval
-> claims
-> isolated trials
-> graders/verifiers
-> immutable raw artifacts
-> aggregate
-> routing candidate
-> report
```

- Kullanıcı onayı öncesi hiçbir canlı provider çağrısı yapılmaz.
- Plan exact model sayısı, trial sayısı ve maksimum çağrı bütçesini göstermelidir.
- Replay aynı plan/trial için yeni maliyet üretmez.
- Timeout sonrası effect reconcile edilmeden retry yoktur.

### 19.7 Scoring

Tek bir toplam skor yeterli değildir. En az:

- correctness,
- evidence/citation,
- structured format,
- safety,
- reliability,
- latency,
- token efficiency,
- tool correctness,
- recovery,
- human correction

boyutları tutulur.

Mean, median, p95, variance, pass rate ve confidence interval raporlanır. Teknik ve yaratıcı görevler aynı metrikle zorla karşılaştırılmaz.

### 19.8 Blind evaluation

- İnsan veya judge model değerlendirmesinde model adları A/B alias'ına çevrilir.
- Aynı prompt, fixture, tool policy ve generation koşulları korunur.
- Test edilen model ile verifier aynı identity/family olamaz; istisna açık policy ister.
- Hidden key model-visible context'e girmez.

### 19.9 Benchmark integrity eşikleri

- plan/prompt/fixture/grader hash coverage: `%100`,
- failure retention: `%100`,
- hidden key leakage: `0`,
- raw output overwrite: `0`,
- unauthorized external effect: `0`,
- aynı plan replay'inde ek provider call: `0`,
- model identity ambiguity: `0`,
- score range/type violation kabulü: `0`.

---

## 20. Evidence-bound model routing

### 20.1 Capability sınıfları

| Sınıf | Kullanım | Model authority |
|---|---|---|
| `DETERMINISTIC` | parse, digest, diff, policy, stale, projection | model yok |
| `FAST_CHEAP` | bounded summary, label, düşük risk extraction | candidate only |
| `BALANCED` | relation/skill draft, routine implementation | candidate only |
| `STRONG_REASONING` | architecture, complex root cause | review-required |
| `CRITICAL_REVIEW` | security, high-risk verification | independent identity |

### 20.2 Routing girdileri

```text
task/workload
project capability profile
data classification
required tools/modalities
current model availability
fresh health
fresh task-specific benchmark
latency/token/cost budget
stability/variance
human correction rate
independence requirements
```

### 20.3 Routing formülü

Routing tek benchmark skoruna dayanmaz. Karar en az:

```text
quality
+ reliability
+ task specialization
+ current availability
+ evidence freshness
+ latency fit
+ token/cost efficiency
+ sample size confidence
- unsafe/failure penalty
- correction penalty
- staleness penalty
```

kullanır.

### 20.4 Aktivasyon güvenliği

- Benchmark sonucu doğrudan routing policy değiştirmez.
- Yeni öneri `routing-policy-candidate` olur.
- Offline replay, shadow ve gerekiyorsa canary test edilir.
- Regression yoksa review/approval ile active revision yapılır.
- Fallback model başka effect'i tekrar etmemelidir; tool result/receipt üzerinden devam eder.
- Availability unknown ise model primary seçilmez.

### 20.5 Routing acceptance

- Aynı evidence aynı karar digest'ini üretir.
- Stale benchmark karar için hard rejection veya penalty üretir.
- Removed model seçilmez.
- Embedding modeli code task'e, chat modeli embedding task'e seçilmez.
- Mac local-only task remote modele gitmez.
- Windows kurum içi task yanlış provider'a gitmez.
- Reviewer independence ihlali reddedilir.
- Karar gerekçesi candidate bazında görünürdür.

---

## 21. Analytics ve telemetri

### 21.1 Raw event formatı

Raw telemetry, küçük atomic JSONL/Parquet segmentleri ve manifestleriyle tutulur. Her event:

```text
event_id
event_type
occurred_at
project/work/run/session refs
component/adapter version
sanitized dimensions
metric values
source receipt/artifact digest
```

Raw prompt, secret veya sınırsız response telemetry'ye yazılmaz.

### 21.2 DuckDB modelleri

- model availability trend,
- benchmark trial/aggregate history,
- routing decision/outcome,
- RAG Recall/MRR/nDCG/no-answer,
- embedding latency/error/dimension drift,
- work/run success/failure/recovery,
- memory usage/effectiveness,
- context budget/freshness,
- resource/soak metrics.

### 21.3 Rebuild

`zekam analytics rebuild`:

1. manifestleri doğrular,
2. corrupt/duplicate segmentleri quarantine eder,
3. temporary DuckDB üretir,
4. row/digest reconciliation yapar,
5. atomik generation swap uygular,
6. rebuild receipt üretir.

### 21.4 Dashboard ilkesi

Dashboard projection'dır. DB veya routing authority değildir. Her metrik son refresh zamanı ve source manifest digest'i gösterir.

---

## 22. Ölçümlü öz-iyileştirme ve self-healing

### 22.1 Improvement candidate modeli

Her öneri en az:

```text
candidate_id
objective
observed_problem
failure_signature
source evidence
hypothesis
proposed change class
allowed files/resources
expected metric delta
regression guards
evaluation plan
budget/stop conditions
rollback plan
proposer identity
```

alanlarını taşır.

### 22.2 Değişiklik sınıfları

| Sınıf | Örnek | Otonomi |
|---|---|---|
| `AUTO_SAFE` | index rebuild, stale marking, cache/projection regeneration | bounded + receipt ile otomatik |
| `REVIEW_REQUIRED` | skill draft, relation proposal, prompt/routing candidate | bağımsız review |
| `HUMAN_APPROVAL_REQUIRED` | schema, hook activation, root instruction, security, retention, external effect | exact insan onayı |
| `PROHIBITED_AUTONOMOUS` | secret export, approval bypass, receipt silme, force push, history rewrite | yasak |

### 22.3 Measured improvement loop

```text
observe
-> canonicalize failure/objective identity
-> check novelty and prior attempts
-> propose bounded plan
-> freeze validator assets
-> run baseline
-> apply in isolated sandbox/worktree
-> run adversarial evaluation
-> compare metric vector
-> independent verifier
-> shadow/canary
-> approval if required
-> activate version
-> monitor
-> rollback or retain
-> learning candidate
```

### 22.4 Stop koşulları

- maximum iteration,
- wall-clock budget,
- provider call budget,
- token/cost budget,
- no-new-evidence,
- repeated hypothesis,
- guard regression,
- unsafe effect request,
- human review required,
- confidence below threshold.

### 22.5 İyileşme tanımı

Tek metriği yükseltmek iyileşme değildir. Değişiklik ancak:

- hedef metriği anlamlı artırıyorsa,
- hiçbir critical guard gerilemiyorsa,
- failure/variance artmıyorsa,
- maliyet/latency kabul sınırındaysa,
- test fixture ve grader değiştirilmemişse

kabul edilir.

### 22.6 Self-repair

```text
detect integrity gap
-> classify impact
-> freeze affected mutation path
-> reconstruct evidence
-> create repair candidate
-> deterministic/independent validation
-> approval if required
-> apply
-> terminal repair receipt
-> full contract recheck
```

“Bir daha yapma, kendini düzelt” promptu tek başına repair değildir.

---

## 23. Avenox 24 repository raporu izlenebilirlik matrisi

Aşağıdaki tablo, her rapordan alınan pattern'i ve Zekam karşılığını gösterir. Her satır uygulama sonunda en az bir code/doc/test evidence ref'iyle kapatılacaktır.

| # | Rapor | Alınan temel pattern | Zekam uyarlaması |
|---:|---|---|---|
| 1 | `01-sifirdan.md` | Şema-first, deterministik builder, provenance, malformed/recovery testleri | Bootstrap generator, schema contracts, negative-first acceptance |
| 2 | `02-avenoxbeyin.md` | Session hooks, flush/compile/daylog, atomic upgrade, doctor | Lifecycle bridge, durable spool, compiler ve continuity receipts |
| 3 | `03-avenoxskills.md` | Skill = executable contract; detect→plan→approve→execute→verify→receipt | Versioned skill registry ve permission ceiling |
| 4 | `04-avenox-hermes-notlari.md` | Failure catalog; state/memory/context/authority ayrımı; bounded QA | Failure cards, symptom search, root-cause→lesson traceability |
| 5 | `05-avenoxstatusline.md` | Düşük overhead, safe cache, malformed inputta güvenli görünüm | Local status/doctor snapshot ve fallback state |
| 6 | `06-epstein-llm-friendly.md` | Relational provenance + ayrı lexical/vector; evidence pack ve RRF | Polyglot RAG, citation bundle ve rebuildable indexes |
| 7 | `07-higgsfield-studio.md` | Verified catalog, resumable jobs, unknown model uydurmama | Model registry revision, job resume, unknown fields |
| 8 | `08-sessizkes.md` | Analysis/execute ayrımı, progress/cancel/temp cleanup, no overwrite | Ingestion/benchmark plan-run ayrımı ve atomic output |
| 9 | `09-moltpump.md` | Saga/outbox/reconciliation ve idempotent external effects | Provider/tool effect claims, outbox ve recovery |
| 10 | `10-limon-arena.md` | Immutable raw outputs, sandbox, blind/randomized evaluation | Benchmark raw artifacts, A/B alias ve isolated runner |
| 11 | `11-avenox-chatllm-notlari.md` | Adversarial fixtures, hidden keys, side-effect graders | Negative-first model benchmark suite |
| 12 | `12-avenoxai.md` | Scoped memory→context compiler→model/tool policy→receipt | Core plane separation ve provider-neutrality |
| 13 | `13-hermes-agent.md` | Canonical inbound/session routing, skill/scheduler/subagent lifecycle | Generic client adapters ve exact lifecycle contracts |
| 14 | `14-kapalicarsi.md` | Deterministik engine kuralları sahiplenir; model yalnız proposal | Self-improvement candidate ve before/after hash receipts |
| 15 | `15-AI-Ne-Kadar-Ilerledi.md` | Raw/fixed ayrımı, exact model/generation metadata, longitudinal history | Benchmark provenance ve time-series model trend |
| 16 | `16-polymarket-btc-edge-bot.md` | Paper-first, risk ledger, staleness, recorded replay, exact numeric | Live call safety, stale evidence ve replay tests |
| 17 | `17-CodexBar.md` | Provider registry, local discovery, health/quota cache | OpenCode model discovery, availability ve health snapshots |
| 18 | `18-GPT-5.6-SOL-Benchmark.md` | Task-specific graders, golden metrics, soak, technical/creative ayrımı | Zekam gerçek-workload suites ve ayrı score dimensions |
| 19 | `19-candylovable.md` | LLM authoring plane ile deterministic runtime ayrımı; versioned patch | Model proposal sandbox, fake provider ve activation gate |
| 20 | `20-aivideocreator.md` | Staged pipeline, timing/provenance, generated artifacts | Parse→chunk→embed→index generation pipeline |
| 21 | `21-oracle.md` | Secure context bundles, durable consultation job, no duplicate after timeout | Model consultation manifests, overflow fail-loud, reconciliation |
| 22 | `22-galaxy-simulation.md` | Shared lifecycle, destroy/leak tests, bounded degrade/hysteresis | Resource cleanup, fallback hysteresis ve soak gates |
| 23 | `23-sonnet5-vs-opus.md` | Aynı prompt/koşul, blind comparison, confidence ve task breakdown | Model arena fairness ve confidence-bound routing |
| 24 | `24-clawdbot.md` | Local-first assistant, SQLite/vector memory, stale/dead/same-PID lock, no duplicate effect on failover | Cross-platform local runtime, lock recovery ve idempotent scheduler |

### 23.1 Aynen alınmayacak Avenox desenleri

- Tek sınırsız vault içinde corporate/private/public veriyi karıştırmak.
- Modelin root/security/schema'yı doğrudan değiştirmesi.
- Raw transcript'i otomatik kalıcı fact yapmak.
- Model/harness adı hard-code etmek.
- Background spawn'ı completion saymak.
- Provider ürününe özel DB/servisi Zekam core'a taşımak.
- Supabase/PostgreSQL kullanımını sırf Avenox kullandı diye kopyalamak.
- Test sonucu kötü olan run'ı silmek veya post-edit ile güzelleştirmek.

---

## 24. Uygulama iş paketleri

Her iş paketi yalnız belirtilen exit gate'ler tamamlandığında kapatılır. Paket tamamlanırken bu dosyadaki checkbox'lar güncellenir ve evidence bundle üretilir.

### WP-00 — Rebaseline, görev authority ve PostgreSQL erişim yasağı

**Amaç:** Güncel HEAD'i ve değişiklikleri doğrulayıp bu görevi yaşayan sözleşme yapmak.

**İşler:**

- [x] `git status --short --branch`, HEAD, remote ve son commitler kaydedildi.
- [x] Baseline HEAD farklıysa fark analizi bu dosyaya eklendi.
- [x] Mevcut kullanıcı değişiklikleri korunup manifestlendi.
- [x] Bu `AKTIF_GOREV.md` repo köküne kontrollü biçimde alındı.
- [x] `AKTIF_GOREV.yaml` dual-authority riski kaldırıldı.
- [x] Test harness'te PostgreSQL connect/export komutlarını fail eden sentinel eklendi.
- [x] Eski DB'ye hiçbir bağlantı yapılmadığı evidence ile gösterildi.

**WP-00 baseline ve kanıt:** Baseline HEAD görevdeki değerle aynıdır; branch ve uzak
`origin/main` farkı `0/0` olarak doğrulanmıştır. Bu nedenle ek commit fark analizi gerekmedi.
Başlangıçtaki staged, unstaged ve untracked kullanıcı dosyaları içerik digest'leriyle
manifestlenmiş ve korunmuştur. Kanıt paketi:
`artifacts/acceptance/wp-00/wp00-d95cdac/`.

**Exit gate:**

- task digest operational plan'a bağlanabilir,
- no-legacy-DB-access testi geçer,
- repo kullanıcı değişiklikleri kaybolmaz.

### WP-01 — Embedded teknoloji bake-off ve ADR'ler

**Amaç:** Operational ve knowledge motorlarını gerçek Mac/Windows yükleriyle seçmek.

**İşler:**

- [x] Operational SQLite spike (macOS ARM64).
- [x] Operational Turso Database/pyturso spike (macOS ARM64).
- [x] LanceDB local RAG spike (macOS ARM64).
- [x] Zvec in-process RAG spike (macOS ARM64).
- [x] SQLite FTS5 + sqlite-vec RAG spike (macOS ARM64).
- [x] Qdrant reference spike gerekmedi; üç ana aday ölçülebilir karar üretti.
- [x] Portability, durability, quality, latency ve packaging Mac ölçümleri.
- [x] SQLite runtime sürüm/WAL güvenlik matrisi (Mac runtime).
- [x] `ADR-LOCAL-STORE-001` (macOS accepted, Windows deferred).
- [x] `ADR-KNOWLEDGE-INDEX-001` (bounded Mac fixture accepted, scale/Windows deferred).
- [x] `ADR-ANALYTICS-001` (macOS accepted, Windows deferred).

**Mac-first karar:** CPython SQLite + SQLite FTS5/sqlite-vec + DuckDB. Bu seçim
yalnız macOS uygulamasını açar; Windows x64 hard gate ve büyük-corpus stres
ölçümleri `deferred` durumundadır. Global WP-01 cross-platform exit gate'i açık
kalmaya devam eder.

**Exit gate:**

- hard gate geçmeyen aday dependency değildir,
- tek operational ve tek knowledge stack seçilmiştir,
- benchmark sonuçları hash'li artifact'tır.

### WP-02 — `ZEKAM_HOME` v2 ve atomic fresh bootstrap

**Amaç:** Docker'sız, sıfır verili, idempotent ve recoverable kurulum.

**İşler:**

- [x] Layout v2 contract ve ownership classes.
- [x] Dry-run layout/bootstrap planı.
- [x] Atomic staging→publish.
- [x] Schema v1 migration framework.
- [x] Bootstrap receipt.
- [x] Legacy PG config detection without connection.
- [x] macOS permissions ve path tests.
- [ ] Windows permissions, reparse ve path tests (deferred).
- [x] Backup/snapshot başlangıç sözleşmesi.

**Mac-first kabul:** Fresh init, ikinci init, fault injection, receipt drift,
legacy config, concurrency, quarantine recovery ve SQLite schema/migration
integrity testleri macOS üzerinde geçmiştir. Windows acceptance daha sonra aynı
fixture ile ayrıca çalıştırılacaktır.

**Exit gate:**

- `docker` ve network yokken fresh init başarılı,
- ikinci init duplicate üretmiyor,
- her injection noktasında yarım publish yok.

### WP-03 — Operational Store v1

**Amaç:** Project, work, run, session, model state ve receipts için tam yerel authority.

**İşler:**

- [x] Portlar ve transaction unit-of-work.
- [x] Şema aileleri ve constraints.
- [x] Project/alias/source snapshot.
- [x] Work revisions/events.
- [x] Run/step/checkpoint.
- [x] Config/task digest binding.
- [x] Schema fingerprint/integrity doctor.
- [x] Backup/restore parity.

**Exit gate:**

- mevcut project/work temel davranışı local store'da E2E,
- type/boundary/drift/concurrency testleri geçer,
- application katmanında somut DB importu yoktur.

### WP-04 — Yerel queue, lease, lock, claim, receipt ve recovery

**Amaç:** PostgreSQL queue/lease semantiğinin yerel ve crash-safe karşılığı.

**İşler:**

- [x] Durable local outbox/queue.
- [x] Single-writer coordinator veya seçilen motorun güvenli concurrency modeli.
- [x] Lease/fencing/heartbeat.
- [x] Stale/dead/same-PID orphan lock recovery.
- [x] Claim-before-effect ve terminal receipt.
- [x] Timeout reconciliation.
- [x] Idempotent scheduler slot.
- [x] Resource cleanup/destroy tests.

**Exit gate:**

- process kill ve restart'ta duplicate effect yok,
- claim var receipt yoksa `recovery-required`,
- stale lock data kaybı olmadan temizlenir.

### WP-05 — Knowledge file plane, project scopes ve CAS

**Amaç:** `.zekam` global/proje bilgi hiyerarşisini ve source manifests'i kurmak.

**İşler:**

- [x] Global reports/research/ideas/decisions/reference/daylog structure.
- [x] Project-specific ortak klasör contract'ı.
- [x] `PROJECT.yaml` projection.
- [x] User-authored vs generated note ayrımı.
- [x] CAS ve artifact refs.
- [x] Privacy/realm/sync profiles.
- [x] WikiLink/relation projection.
- [x] Inbox/archive lifecycle.

**Exit gate:**

- owner scope belirsiz dosya yok,
- duplicate source-of-truth yok,
- public-safe projection secret/PII scan'i geçer.

### WP-06 — Gerçek embedding provider'ları

**Amaç:** Mac local BGE ve Windows/OpenCode remote embedding'i production portuna bağlamak.

**İşler:**

- [x] Local BGE discovery/config/profile/probe.
- [x] OpenCode remote exact identity/config/probe (contract accepted; Windows live E2E deferred).
- [x] Document/query embedding batch.
- [x] Dimension/non-finite/profile drift controls.
- [x] Data classification/outbound gate.
- [x] Cross-device profile namespace.
- [x] Explicit degraded lexical-only state.
- [x] Feature-hash yolunu semantic embedding olarak kaldırma/yeniden adlandırma.

**Exit gate:**

- Mac network kapalı E2E real embedding,
- Windows approved remote embedding E2E,
- fake semantic fallback yok,
- wrong dimension/NaN/partial batch state'i bozmuyor.

**Mac kabul durumu (2 Eylül 2026):** `macos-accepted/windows-deferred`.
Gerçek loopback Infinity/BGE-M3 runtime'ı, Akıllı Kasa'dan seçilen üç salt-okunur
ADR ile semantic margin, batch/repeat kararlılığı, exact model/revision/weight
fingerprint, finite/normalized 1024-boyutlu vektör ve production provider
composition yolunda doğrulandı. Missing/timeout/partial/NaN/dimension/model/profile
drift durumları CAS veya DB mutation'ından önce fail-closed; provider yokluğunda
exact+lexical arama `lexical-only-degraded` çalışıyor. Bağımsız verifier PASS verdi.
Kanıt: `artifacts/acceptance/wp-06/wp06-d95cdac-macos/manifest.json`.
Windows/OpenCode canlı uzak-provider E2E K-013 uyarınca açık ve nihai kabulü
engellemeye devam eder. Bu kabul WP-07 hibrit RAG kapısını kapatmaz.

### WP-07 — Çalışan hibrit RAG dikey dilimi

**Amaç:** Fresh source'tan kaynaklı cevap veya doğru abstain üretmek.

**İşler:**

- [x] Source discovery/manifest.
- [x] Parse/chunk/locator.
- [x] Exact index.
- [x] Lexical index.
- [x] Vector index.
- [x] RRF/dedupe/rerank fallback.
- [x] Scope filtering.
- [x] Citation validation.
- [x] No-answer gate.
- [x] Generation activation/rebuild.
- [x] Golden RAG corpus ve metrics.

**Exit gate:**

- Bölüm 14 eşikleri geçer,
- project question cited answer verir,
- unsupported question uydurmaz,
- source değişince stale index current sayılmaz.

**Mac kabul durumu (2 Eylül 2026):** `macos-accepted/windows-deferred`.
Akıllı Kasa'nın altı salt-okunur kaynak dosyasından üretilen bounded corpus,
gerçek local BGE-M3 embedding, SQLite FTS5 + sqlite-vec exact/lexical/dense
indeksleri ve RRF tabanlı retrieval ile 100-case golden kapıdan geçti: exact
top-1 ve citation locator validity `1.00`, Recall@10 `0.8571`, MRR `0.8107`,
nDCG@10 `0.8223`; cross-project leakage, fabricated citation, unsupported factual
answer ve silent freshness mismatch `0`. Provider timeout/partial/dimension,
concurrent generation activation, body/vector/FTS corruption, rollback,
supersede, restart ve scratch rebuild adversarial olarak doğrulandı. Sorgular tek
generation digest'ine pinlenir; stale ve current citation kimlikleri karışmaz.
Bağımsız verifier PASS verdi. Kanıt:
`artifacts/acceptance/wp-07/wp07-d95cdac-macos/manifest.json`.
Windows/OpenCode canlı uzak embedding RAG E2E K-013 uyarınca deferred durumdadır;
WP-08 ve sonraki iş paketleri tamamlanmış sayılmaz.

### WP-08 — Lifecycle hooks, continuity ve Markdown ikinci beyin

**Amaç:** Oturumların başlangıç/compaction/kapanış sürekliliğini local store ve Markdown projections ile kanıtlamak.

**İşler:**

- [x] Generic lifecycle bridge.
- [ ] OpenCode/Codex/Claude adapters.
- [x] Durable local spool/outbox.
- [x] Session start hydration.
- [x] Pre-compaction checkpoint/ACK.
- [x] Close pipeline/receipt.
- [x] Daylog/handoff projection.
- [x] Context compiler/budgets.
- [x] Gap/backfill/recovery doctor.

**Mac kabul durumu (5 Eylül 2026):** `macos-accepted/windows-deferred`.
Generic bridge, durable spool, hydration, pre-compaction ACK, close/daylog/handoff,
bounded context compiler ve gap/recovery akışları restart, replay, ancestry drift,
yarım işlem ve duplicate delivery testleriyle doğrulandı. Bağımsız Mac verifier
`PASS_WP08_MAC` verdi. OpenCode'un Windows/Bun canlı adapter yolu K-013 kapsamında
deferred olduğu için birleşik adapter checkbox'ı ve global cross-client kapısı açık
kalır. Kanıt: `artifacts/acceptance/wp-08/wp08-d95cdac-macos/`.

**Exit gate:**

- restart/compaction sonrası golden resume,
- missing required hook sessiz geçmez,
- full vault preload yok,
- generated notes source refs taşır.

### WP-09 — Memory, failure catalog ve skill lifecycle

**Amaç:** Session bilgisini kontrollü candidate→review→active öğrenmeye dönüştürmek.

**İşler:**

- [x] Memory candidates/reviews/revisions/relations.
- [x] Duplicate/conflict/stale/supersession.
- [x] Failure signatures/cards.
- [x] Lesson extraction.
- [x] Skill candidates/manifests/evaluation.
- [x] Independent review ve activation.
- [x] Usage/outcome effectiveness.
- [x] Hygiene/retention proposals.

**Mac kabul durumu (5 Eylül 2026):** `macos-accepted/windows-deferred`.
Local SQLite learning store candidate→review→active zincirini, immutable revision ve
relation kayıtlarını, duplicate/conflict/stale/supersession kararlarını, failure/lesson
ve skill yaşam döngüsünü kanıtla sınırlar. Self-approval, tek-failure terfisi, bozuk
kanıt, replay drift ve doğrudan active-fact yazımı fail-closed reddedildi. Bağımsız
verifier `PASS_WP09_MAC` verdi. Kanıt:
`artifacts/acceptance/wp-09/wp09-d95cdac-macos/`.

**Exit gate:**

- raw transcript doğrudan active fact olamaz,
- tek failure evrensel skill üretmez,
- skill testsiz/self-approved aktive olmaz.

### WP-10 — Yerel model registry ve discovery

**Amaç:** Cihaz ve OpenCode model gerçekliğini local olarak güncel tutmak.

**İşler:**

- [x] Exact model identity schema.
- [ ] OpenCode secretsiz discovery snapshot.
- [x] New/removed/changed/ambiguous reconcile.
- [x] Device/client availability.
- [x] Model revision fingerprint.
- [x] Health/probe observations.
- [x] Local profile reports.
- [x] Staleness/quarantine/cooldown.

**Mac kabul durumu (5 Eylül 2026):** `macos-accepted/windows-deferred`.
Exact provider/access/model/revision kimliği, local registry reconciliation, health,
availability, quarantine/cooldown ve secretsiz profil raporları deterministic SQLite
state üzerinde doğrulandı; prefix veya benzer adlar merge edilmedi. Canlı OpenCode
discovery snapshot'ı Windows/Bun aşamasına K-013 ile ertelendi ve tahminî fixture
canlı discovery kanıtı sayılmadı. Kanıt:
`artifacts/acceptance/wp-10/wp10-d95cdac-macos/`.

**Exit gate:**

- güncel model listesi tahminsiz anlaşılır,
- prefix/ID merge yok,
- removed/stale model routing'e giremez,
- secret durable state'e sızmaz.

### WP-11 — Adversarial benchmark laboratuvarı

**Amaç:** Kurum içi modelleri gerçek görevlerde adil, tekrarlanabilir ve zorlayıcı biçimde ölçmek.

**İşler:**

- [x] Suite/task/fixture/grader schemas.
- [x] Hidden key isolation.
- [x] Sandbox runner.
- [x] Dry-run call budget.
- [x] Claims/receipts/replay.
- [x] Raw output artifacts.
- [x] Independent verifier.
- [x] Task-specific aggregates/confidence.
- [x] Blind A/B model arena.
- [x] Failure retention.

**Mac kabul durumu (5 Eylül 2026):** `macos-accepted/windows-deferred`.
On dört zorunlu task family için persistent task/fixture/hidden-key/grader kaynakları,
exact digest ve scoring-dimension sözleşmeleri üretildi. macOS sandbox runner network,
write ve undeclared-home read girişimlerini reddetti; dry-run provider çağrısı `0`,
replay yeni çağrı üretmiyor, raw başarısızlıklar immutable tutuluyor ve aggregate'lar
beş tekrar, Wilson güven aralığı ile on üç metriği doğruluyor. Bağımsız current-source
verifier WP-11'i PASS etti. Kanıt:
`artifacts/acceptance/wp-11/wp11-d95cdac-macos/`.

**Exit gate:**

- Bölüm 19 integrity eşikleri geçer,
- `"A"` yerine integer benzeri invalid cases reddedilir,
- aynı plan replay provider çağrısı üretmez.

### WP-12 — Evidence-bound routing

**Amaç:** Fresh benchmark, health, availability ve project profile'a göre model seçmek.

**İşler:**

- [x] Capability classes ve workload mapping.
- [x] Candidate hard gates.
- [x] Multi-metric scoring/confidence.
- [x] Staleness/revision invalidation.
- [x] Primary/fallback/independence.
- [x] Shadow/canary policy activation.
- [x] Decision explanation/digest.
- [x] Outcome feedback.

**Mac kabul durumu (5 Eylül 2026):** `macos-accepted/windows-deferred`.
Routing kararı exact model kimliği, registry/capability/health ve immutable benchmark
evidence epoch'una bağlandı. Stale, removed, unavailable, wrong-modality ve düşük
confidence adayları hard-gate ile dışlanıyor; primary/fallback bağımsızlığı, deterministic
replay, shadow/canary ve outcome feedback SQLite üzerinde restart sonrası korunuyor.
Bağımsız verifier WP-12'yi PASS etti. Kanıt:
`artifacts/acceptance/wp-12/wp12-d95cdac-macos/`.

**Exit gate:**

- deterministic replay,
- wrong modality/device/provider seçimi yok,
- benchmark tek başına policy değiştiremiyor,
- failover duplicate side effect üretmiyor.

### WP-13 — Analytics, observatory ve raporlar

**Amaç:** DuckDB üzerinden rebuildable benchmark/runtime/RAG görünümü.

**İşler:**

- [x] Raw event segment schema.
- [x] Single analytics writer/importer.
- [x] DuckDB models/views.
- [x] Rebuild/generation swap.
- [x] Model/RAG/runtime dashboards.
- [x] Morning/project reports.
- [x] Freshness/source manifest görünürlüğü.

**Mac kabul durumu (5 Eylül 2026):** `macos-accepted/windows-deferred`.
Typed append-only raw segmentler tek importer ile DuckDB generation'ına alınmakta;
model, RAG, runtime, context ve memory view'ları kaynak manifesti/freshness ve
`authority=false` taşımaktadır. Generation silme/rebuild aynı aggregate digest'ini
üretir, yarım generation aktif olmaz ve dashboard write authority kazanmaz. Bağımsız
verifier WP-13'ü PASS etti. Kanıt:
`artifacts/acceptance/wp-13/wp13-d95cdac-macos/`.

**Exit gate:**

- DuckDB silinip aynı aggregate digest'leriyle rebuild,
- dashboard authority değil,
- multi-process write yok.

### WP-14 — Ölçümlü öz-iyileştirme

**Amaç:** Failure/outcome'dan güvenli improvement candidate üretmek ve doğrulamak.

**İşler:**

- [x] Improvement candidate schema.
- [x] Change-class policy.
- [x] Novelty/prior-attempt identity.
- [x] Validator asset freeze.
- [x] Isolated evaluation.
- [x] Metric vector comparison.
- [x] Independent verifier.
- [x] Shadow/canary/rollback.
- [x] Learning candidate feedback.

**Mac kabul durumu (5 Eylül 2026):** `macos-accepted/windows-deferred`.
Improvement schema v3; candidate/change-class/novelty, frozen validator, isolated
beş-tekrarlı benchmark ve tüm zorunlu metrikleri immutable SQLite zincirine bağlar.
Rollout için durable pre-effect claim → runner settlement → frozen bağımsız verifier
settlement zorunludur; caller-created completed receipt, runner-only settlement,
forged verifier, regress, budget aşımı ve direct activation reddedildi. Restartlı
claim→runner→verifier→rollout ve rollback geçti. Bağımsız verifier 39/39 ve önceki
self-attested repro'nun fail-closed olduğunu doğruladı. Kanıt:
`artifacts/acceptance/wp-14/wp14-d95cdac-macos/`.

**Exit gate:**

- AUTO_SAFE dışı değişiklik kendiliğinden aktive olmaz,
- regress eden değişiklik iyileşme sayılmaz,
- stop/budget koşulları gerçekten enforcement'tır.

### WP-15 — Application rewiring ve PostgreSQL sökümü

**Amaç:** Tüm CLI/API/worker/scheduler/service composition'ı yeni port/adaptörlere geçirmek ve PG izlerini active runtime'dan kaldırmak.

**İşler:**

- [x] `PersistenceBackend.POSTGRESQL` kaldırıldı.
- [x] `src/zekam/infrastructure/postgres/**` kaldırıldı.
- [x] Eski `migrations/*.sql` active source'tan kaldırıldı.
- [x] `compose/**` Zekam core requirement olmaktan çıkarıldı/kaldırıldı.
- [x] `psycopg` dependency kaldırıldı.
- [x] CLI/API/doctor/backup/release yeni store'lara bağlandı.
- [x] PG integration testleri yeni contract tests'e dönüştürüldü.
- [x] README/AGENTS/00_BASLA/manifest/mimari/docs güncellendi.
- [x] Global DoD tüm maddeleri yeni kanıtla reset/yeniden değerlendirildi.

**Mac kabul durumu (5 Eylül 2026):** `macos-accepted/windows-deferred`.
Fresh setup ve standard doctor yalnız SQLite/local store bileşimini kullanıyor;
PostgreSQL adapter dizini, active migration/compose ağacı ve `psycopg` dependency'si
yoktur. Korunan tarihsel source dosyalarından on dokuzu wheel'e alınmaz. Fail-closed
static import auditinde public CLI/API/worker/scheduler grafiği 149 modül ve legacy
erişim bulgusu `0`; wheel inventory'de archive-only dosya, PostgreSQL adapter ve
dependency `0` bulundu. Wheel/sdist/source virtual tree exact Hatch exclusion
politikasına ve manifest digest'ine bağlıdır. Bağımsız verifier canlı effect zinciri
bulmadı. Kanıt: `artifacts/acceptance/wp-15/wp15-d95cdac-macos/`.

**Exit gate:**

- aktif kod/dokümanda Zekam core PG dependency count `0`,
- core komutları Docker'sız çalışır,
- dead-code/dependency/package manifest temizdir.

### WP-16 — Cross-platform chaos, DR ve release acceptance

**Amaç:** Sistemi sadece mutlu yolda değil gerçek arıza ve platform koşullarında kanıtlamak.

**İşler:**

- [x] macOS ARM64 acceptance.
- [ ] Windows x64 acceptance.
- [ ] Python supported version matrix.
- [x] process kill/fault injection campaign.
- [x] disk full/read-only/corrupt file.
- [x] network unavailable/provider timeout.
- [x] lock/PID reuse/concurrent writers.
- [x] backup/restore/rebuild drills.
- [x] soak/resource leak.
- [x] security/privacy/secret scan.
- [x] evidence bundle/SBOM/checksum/release report.

**Mac kabul durumu (5 Eylül 2026):** `macos-accepted/windows-deferred`.
macOS ARM64 üzerinde fault/process-kill, disk/read-only/corruption, provider/network,
lease/lock/PID/concurrent writer, backup/restore/rebuild ve bounded soak/resource
senaryoları çalıştırıldı; secret/privacy/package auditleri fail-closed geçti. Fresh
wheel ve sdist exact source manifestiyle bağımsız smoke testten geçirilmiştir. Windows
x64, supported-Python matrisi, büyük corpus ve yüksek-yük stres ölçümleri K-013 ile
deferred olduğundan WP-16 global exit gate'i ve görev `COMPLETED` durumu açık kalır.
Kanıt: `artifacts/acceptance/wp-16/wp16-d95cdac-macos/`.

**Exit gate:**

- bütün P0/P1 acceptance geçer,
- unresolved critical/high finding `0`,
- bağımsız verifier raporu vardır,
- push yapılmamıştır veya explicit user approval evidence'i vardır.

---

## 25. İlk çalışan dikey dilim

Geniş refactor başlamadan aşağıdaki uçtan uca dilim tamamlanmalıdır:

```text
1. Fresh ZEKAM_HOME
2. No Docker / no DB server / no network requirement
3. Local operational store bootstrap
4. Zekam projesini register et
5. Bir project source snapshot üret
6. Mac'te gerçek local BGE veya testte deterministic fake provider contract
7. Source parse + chunk + exact/lexical/vector index
8. Proje sorusuna exact citation'lı cevap
9. No-answer sorusuna abstain
10. Work oluştur ve checkpoint yaz
11. Session close receipt + daylog
12. Process restart
13. Continue/hydration ve aynı project state
14. OpenCode model discovery preview
15. Benchmark dry-run planı; provider call sayısı 0
```

Bu dilim tamamlanmadan bütün PostgreSQL kodu topluca silinmez. Önce replacement path kanıtlanır, sonra eski path sökülür.

---

## 26. Test stratejisi — adversarial/negative-first

### 26.1 Temel tanım

Bir özellik yalnız doğru girdide sonuç verdiğinde değil, yanlış girdide kontrollü reddettiğinde, state'i bozmadığında, yeniden başladığında toparlandığında ve kanıt ürettiğinde tamamlanmış sayılır.

### 26.2 Tip ve validation matrisi

Her typed alan için en az:

| Beklenen | Geçerli | Zorunlu invalid örnekleri |
|---|---|---|
| integer | `1` | `"A"`, `"1"`, `true`, `1.5`, `null`, `""`, `-1`, taşan sayı |
| float/range | `0.5` | `"A"`, `NaN`, `Inf`, `-0.1`, `1.1`, bool |
| digest | `sha256:...` | yanlış prefix, upper hex, kısa/uzun, whitespace |
| UUID/ID | canonical ID | boş, malformed, wrong type, duplicate |
| enum | tanımlı değer | unknown, case drift, integer |
| path | izinli canonical path | traversal, symlink, reparse, NUL, UNC/network escape |
| JSON object | exact schema | array, duplicate key, unknown field, invalid UTF-8, oversize |
| vector | finite exact dimension | zero vector, NaN/Inf, wrong dim, string item, partial batch |
| timestamp | canonical UTC | local ambiguous, invalid date, future drift where forbidden |

### 26.3 Operational tests

- schema bootstrap idempotency,
- config publish only after DB success,
- payload drift,
- optimistic concurrency,
- append-only enforcement,
- completed-without-evidence rejection,
- FK/orphan rejection,
- unexpected table/index/trigger drift,
- corrupt meta/schema version,
- concurrent setup,
- concurrent work update,
- rollback atomicity,
- backup parity.

### 26.4 Lock/queue/recovery tests

- stale lock,
- dead PID,
- same-PID orphan after container/process restart,
- PID reuse,
- heartbeat loss,
- lease expiry/fencing,
- duplicate scheduler slot,
- claim before effect crash,
- effect after claim before receipt crash,
- timeout with unknown external result,
- failover without duplicate tool effect,
- slow consumer/backpressure,
- queue poison item/quarantine.

### 26.5 RAG tests

- exact ID dominates semantic noise,
- lexical/dense score scale not directly summed,
- profile dimension drift,
- query/corpus profile mismatch,
- local BGE absent,
- remote embedding unauthorized,
- remote response wrong order/count,
- source changes during indexing,
- partial generation never active,
- stale generation excluded,
- corrupt vector index rebuild,
- deleted source tombstone,
- duplicate chunk/content,
- citation locator mismatch,
- fabricated source ref,
- no-answer hallucination,
- prompt injection in document,
- cross-project/realm leakage,
- context budget overflow.

### 26.6 Model discovery/benchmark tests

- duplicate JSON key,
- unknown OpenCode fields,
- malformed endpoint/credential locator,
- exact model missing,
- same backend with multiple access IDs,
- removed/disabled/quarantined model,
- revision fingerprint drift,
- score `"A"`, negative, >max, NaN,
- timeout/parse/unsafe trial retained,
- hidden key access attempt,
- changed prompt/fixture/grader invalidates plan,
- raw result overwrite attempt,
- same model as verifier,
- blind alias leak,
- replay creates no call,
- call budget overrun blocks run.

### 26.7 Self-improvement tests

- same hypothesis rephrased cannot bypass novelty digest,
- validator fixture change invalidates comparison,
- critical guard regression rejects candidate,
- AUTO_SAFE scope escape blocked,
- review-required direct activation blocked,
- schema/security/root change human gate,
- rollback returns prior digest,
- no terminal receipt means not activated,
- max iteration/token/time/call budget enforced.

### 26.8 Security/privacy tests

- secret in prompt/log/vector/artifact/report/backup,
- corporate data to personal sync,
- remote embedding disclosure without authorization,
- path traversal/symlink/archive bomb,
- untrusted doc instruction execution,
- hidden endpoint/credential persistence,
- permission/ACL drift,
- public-safe projection PII redaction,
- model context receives only allowed fragments.

### 26.9 Platform/package tests

- macOS ARM64 clean install,
- Windows x64 clean install,
- paths with spaces/non-ASCII,
- Windows reparse/long path/file lock,
- POSIX permissions,
- SQLite/Turso/vector library wheel availability,
- no compiler/manual DB setup requirement unless explicitly accepted,
- uninstall/reinstall with data preserved,
- core commands when Docker not installed.

### 26.10 Quality gates

- critical domain modules branch coverage: en az `%90`,
- genel branch coverage: en az `%85`,
- mutation score kritik validator/policy modüllerinde en az `%75`,
- flaky retry ile gizlenen test: `0`,
- expected failure/xpass belgesiz: `0`,
- high/critical security finding: `0`,
- testte live provider çağrısı default: `0`,
- platform acceptance pass: macOS + Windows.

---

## 27. Performans ve operasyonel ölçütler

Donanım farkları nedeniyle WP-01 her hedef cihaz için baseline çıkarır. Final regression gate:

- clean init, model/index işlerini hariç tutarak hedef cihazda `3 saniye` içinde tamamlanmalı,
- local status/active work sorguları 10k work-event fixture'da p95 `150 ms` altında olmalı,
- local serialized write p95 baseline'a göre `%20`den fazla gerilememeli,
- exact/lexical RAG query 50k chunk corpus'ta p95 `250 ms` hedeflemeli,
- vector/hybrid retrieval, query embedding süresi hariç, p95 `800 ms` hedeflemeli,
- source change sonrası incremental index tam rebuild'den ölçülebilir biçimde hızlı olmalı,
- crash recovery kullanıcı müdahalesi gereken durumları `30 saniye` içinde typed finding olarak görünür kılmalı,
- benchmark dry-run provider çağrısı yapmadan `2 saniye` hedeflemeli,
- uzun soak sonunda açık file handle/process/temp artifact büyümesi olmamalı,
- WAL/event/index dosyaları bounded maintenance politikası taşımalı.

Absolute hedef geçilemiyorsa neden, donanım ve measured baseline ADR/evidence içinde açıkça yazılır; sessizce eşik düşürülmez.

---

## 28. Security ve privacy sınırları

### 28.1 Veri sınıfları

- public,
- internal,
- confidential-corporate,
- restricted,
- secret,
- local-private.

### 28.2 Storage ve outbound

| Sınıf | Local store | Git | Remote embedding/model |
|---|---:|---:|---:|
| public | evet | policy ile | policy ile |
| internal | evet | yalnız public-safe projection | reviewed route |
| confidential-corporate | ayrı realm/root | hayır | yalnız kurum içi explicit policy |
| restricted | encrypted/restricted | hayır | çok dar explicit authorization |
| secret | secret backend | asla | credential channel dışında asla |
| local-private | local-only root | varsayılan hayır | varsayılan hayır |

### 28.3 Yüksek riskli otomasyon

Form, ödeme, vergi, sözleşme, resmi belge, iletişim veya external submit için:

- field-level provenance,
- unknown/blank behavior,
- preview,
- independent verification,
- exact submit authorization,
- final receipt

zorunludur. “Form açıldı ve alan doldu” success değildir; kaynak ve gönderim durumu ayrı state'tir.

---

## 29. Backup, restore ve yeniden üretim

### 29.1 Backup sınıfları

1. Operational DB safe snapshot.
2. User knowledge/CAS backup.
3. Config/policy/model registry projections.
4. Benchmark raw artifacts.
5. Derived index ve DuckDB varsayılan olarak backup zorunlu değil; rebuild manifesti zorunlu.

### 29.2 Restore drill

- Fresh empty home'a restore.
- Operational integrity ve row/digest parity.
- CAS missing/corrupt detection.
- Knowledge indexes sıfırdan rebuild.
- DuckDB sıfırdan rebuild.
- Session/work continuity.
- Secret değerleri backup içinde değil; logical refs yeniden çözülür.

### 29.3 Rollback

- Schema forward-fix tercih edilir.
- Feature/policy revision prior digest'e dönebilir.
- Index generation prior active pointer'a atomik dönebilir.
- Routing policy shadow/disabled yapılabilir.
- Memory overwrite edilmez; supersede/revoke revision kullanılır.
- Git force-push rollback değildir.

---

## 30. CLI ve kullanıcı deneyimi hedefleri

Aşağıdaki komut aileleri ya korunacak ya da aynı davranışı sağlayan net karşılıkla sunulacaktır:

```text
zekam init --dry-run / --apply
zekam doctor
zekam status
zekam project add/list/show/scan
zekam work create/list/show/update
zekam knowledge scan/index/rebuild/status
zekam ask
zekam session start/checkpoint/close/continue
zekam model discover/reconcile/status/report
zekam benchmark plan/run/status/report
zekam route preview/explain
zekam memory candidate/review/show/hygiene
zekam analytics rebuild/report
zekam backup create/verify/restore
zekam repair scan/plan/apply
```

Kurallar:

- mutation varsayılan dry-run,
- destructive/external effect explicit apply/authorization,
- JSON output contract versioned,
- hata mesajı component/input/reason/next-safe-action taşır,
- uzun işler progress/cancel/resume destekler,
- “başarısız” tek başına hata mesajı değildir,
- status her dependency'yi `healthy/degraded/blocked/unknown` gösterir.

---

## 31. Repository değişiklik haritası

### 31.1 Korunacak/refactor edilecek

- `src/zekam/domain/**` içindeki provider-neutral domain modelleri,
- digest/idempotency/validation/security contracts,
- `application/home.py` core/home ayrımı ve ownership yaklaşımı,
- lifecycle/context/memory/benchmark/routing domain mantığı,
- provider adapter ve secret broker sınırları,
- CAS ve generated projection mantığı,
- golden resume, property, security ve negative test fixture'ları.

### 31.2 Yeni ana yollar

Önerilen isimler implementation sırasında ADR ile kesinleştirilebilir:

```text
src/zekam/application/bootstrap/
src/zekam/application/storage/
src/zekam/application/knowledge_runtime/
src/zekam/application/model_lab/
src/zekam/application/improvement/

src/zekam/infrastructure/operational/
src/zekam/infrastructure/knowledge_index/
src/zekam/infrastructure/embeddings/
src/zekam/infrastructure/analytics/
src/zekam/infrastructure/local_runtime/

benchmarks/suites/
benchmarks/fixtures/
benchmarks/graders/
benchmarks/schemas/

docs/adr/
docs/architecture/
docs/runbooks/
```

### 31.3 Kaldırılacak/aktif kullanımdan çıkacak

- `src/zekam/infrastructure/postgres/**`,
- PostgreSQL'e özgü migration runner ve routine integrity,
- root `migrations/0001..0078` active DB yolu,
- Zekam core DB compose profili,
- `psycopg` production dependency,
- PostgreSQL/pgvector zorunluluğu yazan README/manifest/docs,
- SQLite minimum/full-continuity ayrımı,
- manual `--vector-json` production RAG yolu,
- feature-hash “embedding fallback” ifadesi,
- PG verisini yeni sisteme taşıyan herhangi bir script,
- bağımsız `AKTIF_GOREV.yaml` authority.

### 31.4 Global DoD güncellemesi

Eski PostgreSQL kanıtına dayanan `[x]` işaretleri yeni mimari için otomatik taşınmaz. Her madde:

- `preserved-and-reverified`,
- `reimplemented-and-verified`,
- `removed-by-new-architecture`,
- `pending`,
- `not-applicable`

olarak yeniden sınıflandırılır ve evidence ref taşır.

---

## 32. Evidence bundle sözleşmesi

Her WP kapanışında:

```text
artifacts/acceptance/<wp>/<run-id>/
├── manifest.json
├── environment.json
├── commands.jsonl
├── test-results/
├── metrics/
├── changed-files.json
├── source-digests.json
├── security-scan.json
├── recovery-results.json
├── verifier-report.json
└── summary.md
```

- Command arguments secret-safe/redacted olur.
- Her artifact SHA-256 manifestte yer alır.
- Builder ve verifier identity görünürdür.
- Provider çağrısı varsa exact call count/token/cost/claim/receipt yer alır.
- Assertion olmadan “geçti” yazılmaz.

---

## 33. Tamamlanma ölçütleri

Görev yalnız aşağıdakilerin tamamı sağlandığında `COMPLETED` olur.

### 33.1 PostgreSQL ve Docker'dan bağımsızlık

- [x] Eski PostgreSQL DB'ye hiçbir veri erişimi yapılmadı.
- [x] Yeni sistem schema v1 ve fresh data ile başladı.
- [x] Production importlarında `infrastructure.postgres` yok.
- [x] `psycopg`, pgvector ve PostgreSQL migration dependency'si yok.
- [x] Zekam core komutları Docker kurulu değilken çalışıyor.
- [x] README/doctor/startup PostgreSQL istemiyor.

### 33.2 Operational state

- [x] Project/work/run/session/checkpoint local authority'de çalışıyor.
- [x] Queue/lease/lock/claim/receipt/recovery E2E çalışıyor.
- [x] Concurrent writer ve crash testleri geçiyor.
- [x] Backup/restore parity kanıtlandı.
- [x] Schema drift fail-closed.

### 33.3 Knowledge ve RAG

- [x] Global/proje knowledge hierarchy kurulmuş.
- [x] Source manifests ve owner scopes eksiksiz.
- [x] Mac local BGE gerçek embedding kanıtlandı.
- [ ] Windows/OpenCode remote embedding kanıtlandı.
- [x] Profile/dimension/preprocessing isolation var.
- [x] Exact + lexical + dense + RRF çalışıyor.
- [x] Citation/no-answer eşikleri geçiyor.
- [x] Index silinip rebuild edilebiliyor.
- [x] Context Vault olmadan Zekam RAG çalışıyor.

### 33.4 İkinci beyin ve süreklilik

- [x] Session start hydration receipt var.
- [x] Pre-compaction checkpoint/ACK var.
- [x] Close/daylog/handoff receipt var.
- [x] Restart/compaction golden resume geçiyor.
- [x] Full vault preload yok.
- [x] Generated Markdown source/freshness taşıyor.

### 33.5 Model laboratuvarı

- [ ] OpenCode model discovery exact ve secret-safe.
- [x] Local model registry fresh reconciliation yapıyor.
- [x] New/removed/revision drift görünür.
- [x] Adversarial benchmark suites çalışıyor.
- [x] Raw outputs ve failures immutable.
- [x] Hidden keys modelden yalıtılmış.
- [x] Routing fresh evidence ve confidence kullanıyor.
- [x] Benchmark sonucu doğrudan policy değiştiremiyor.

### 33.6 Öz-iyileştirme

- [x] Improvement candidate ve change-class policy var.
- [x] Validator assets frozen.
- [x] Shadow/canary/rollback var.
- [x] AUTO_SAFE sınırı test edilmiş.
- [x] Root/security/schema için human gate var.
- [x] Regress eden candidate aktive olmuyor.

### 33.7 Kalite ve release

- [x] Negative/fault/property/concurrency/security testleri geçiyor.
- [ ] macOS ARM64 ve Windows x64 acceptance geçiyor.
- [x] Critical/high security finding yok.
- [ ] Full evidence bundle ve independent verifier var.
- [x] Global DoD yeni mimariye göre güncellendi.
- [x] Package manifest/SBOM/checksum/runbooks güncel.
- [x] `AKTIF_GOREV.md` final durum ve evidence refs ile güncel.
- [x] Repo temiz veya kullanıcı değişiklikleri açıkça belgeli.
- [x] Kullanıcı açıkça istemedikçe push yapılmadı.

---

## 34. Yasak kısa yollar

- Eski PostgreSQL DB'den “yalnız birkaç önemli tabloyu” almak.
- Eski migration 78'i yeni local schema başlangıcı saymak.
- SQLite minimum repository'yi isim değiştirerek full implementation ilan etmek.
- Feature hash'i BGE/semantic embedding olarak sunmak.
- Kullanıcıdan elle vector JSON isteyip RAG tamamlandı demek.
- Vector engine seçimini gerçek corpus bake-off'u olmadan yapmak.
- Üç vector motorunu birden production dependency yapmak.
- DuckDB'yi operational transactional DB yapmak.
- Binary index'i source-of-truth saymak.
- Mac ve Windows embedding profillerini aynı varsaymak.
- Model prefix/ID'lerini normalize edip birleştirmek.
- Benchmark kötü sonuçlarını silmek veya düzeltip raw gibi göstermek.
- Hidden answer key'i model context'ine vermek.
- Tek happy-path testiyle feature kapatmak.
- Wrong type/boundary/fault testlerini ertelemek.
- Hook içinde uzun provider çağrısı yapmak.
- Background spawn'ı receipt saymak.
- Timeout sonrası kör retry yapmak.
- Model summary'sini active memory/decision yapmak.
- Skill'i aynı modelin yazıp onaylaması.
- Self-healing adıyla schema/security/root instruction değiştirmek.
- Corporate/private bilgiyi public Git veya kişisel sync'e koymak.
- Git'e DB dump, raw transcript veya secret eklemek.
- User-authored dosyayı generated projection ile overwrite etmek.
- Context Vault'ı Zekam core dependency yapmak.
- Kullanıcı onayı olmadan live benchmark veya push yapmak.

---

## 35. Uygulayıcı ajan çalışma protokolü

1. Gerçek repo kökünü ve baseline HEAD'i doğrula.
2. `AGENTS.md`, `00_BASLA.md` ve bu dosyayı oku; PostgreSQL gerektiren eski başlangıç adımlarını bu görevle çelişiyorsa çalıştırma.
3. Kullanıcı değişikliklerini manifestle ve koru.
4. Eski PostgreSQL'e bağlanmayı deneme; erişim sentinel'ini etkinleştir.
5. Önce WP-00 ve WP-01'i tamamla; motor isimlerini tahminle sabitleme.
6. Her iş paketinde önce contract/negative test, sonra en küçük doğru uygulama, sonra E2E yap.
7. İlk dikey dilimi çalıştırmadan toplu silme/refactor yapma.
8. Domain invariant'larını koru; somut repository implementasyonlarını port arkasına taşı.
9. Dış çağrı, mutation ve destructive effect için plan/apply ayrımını koru.
10. Her WP sonrasında testleri bağımsız yeniden çalıştır ve evidence bundle oluştur.
11. Test, fixture, grader veya threshold değiştiyse önceki benchmarkla karşılaştırma yapma.
12. `AKTIF_GOREV.md` progress, karar ve evidence refs ile güncel tutulur.
13. Yerel commitler küçük, Türkçe anlamlı ve ASCII-only mesajlı olur.
14. Kullanıcı açıkça istemedikçe push yapma.
15. Belirsizlikte en muhafazakâr, local-first, reversible ve fail-closed davranışı seç; kararı ADR olarak yaz.

---

## 36. İlk uygulama sırası

```text
WP-00
  -> WP-01
  -> WP-02
  -> WP-03
  -> WP-04
  -> WP-05
  -> WP-06
  -> WP-07  [ilk çalışan RAG dilimi]
  -> WP-08
  -> WP-09
  -> WP-10
  -> WP-11
  -> WP-12
  -> WP-13
  -> WP-14
  -> WP-15
  -> WP-16
```

Parallel çalışma yalnız resource ownership çakışmıyorsa yapılır. Özellikle operational schema, composition root, lifecycle ve RAG activation pointer için single-builder kuralı uygulanır.

---

## 37. Nihai teslimatlar

1. Güncellenmiş `AKTIF_GOREV.md`.
2. Üç teknoloji ADR'si ve ölçüm artifact'ları.
3. `ZEKAM_HOME` layout v2.
4. Fresh operational schema v1 ve migration framework.
5. Local runtime/queue/lease/claim/receipt/recovery.
6. Global/proje knowledge hierarchy ve CAS.
7. Mac local BGE ve Windows/OpenCode remote embedding adapters.
8. Seçilmiş embedded hybrid knowledge index.
9. Çalışan source-grounded RAG ve golden evaluation.
10. Session hooks, daylog, compiler ve continuity receipts.
11. Memory/failure/skill lifecycle.
12. Local model registry/discovery.
13. Adversarial benchmark laboratuvarı.
14. Evidence-bound routing.
15. DuckDB analytics ve observatory projections.
16. Bounded self-improvement/self-repair workflow.
17. PostgreSQL/Docker core bağımlılıklarının tamamen sökülmesi.
18. Cross-platform tests, DR drills, runbooks, SBOM, checksums ve final verifier report.

---

## 38. Son başarı tanımı

Görev sonunda Mehmet herhangi bir destek servisi başlatmadan:

```text
zekam init
zekam project add ...
zekam knowledge index ...
zekam ask "..."
zekam benchmark plan --scope opencode ...
zekam session close
```

akışını çalıştırabilmelidir.

Zekam:

- kendi state'ini yerelde güvenle yönetmeli,
- global ve proje bilgisini doğru scope'ta bulmalı,
- Mac ve Windows embedding rotalarını ayırmalı,
- kaynak göstermeden cevap uydurmamalı,
- güncel kurum içi modelleri keşfedip benchmarklamalı,
- routing kararlarını güncel kanıta bağlamalı,
- hatalarını ölçüp iyileştirme adayı üretebilmeli,
- fakat hiçbir zaman kendi güvenlik ve otorite sınırlarını kendiliğinden aşmamalıdır.

**PostgreSQL geçmişi veri olarak yoktur. Yeni Zekam, kendi bilgisini ve ölçüm kanıtını temiz başlangıçtan itibaren yeniden üretir.**
