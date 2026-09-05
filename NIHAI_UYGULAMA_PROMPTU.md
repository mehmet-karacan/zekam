# ZEKAM — Nihai, Model Bağımsız Uygulama Görevi

## 0. Bu görevin statüsü

**SUPERSEDED BASELINE:** Bu belge tarihsel nihai paket kapsamını korur; yaşayan görev veya
mimari authority değildir. Güncel ve bağlayıcı görev `AKTIF_GOREV.md` dosyasıdır. Özellikle
bu belgedeki server-veritabanı, migration, vector-store ve bootstrap kararları
`AKTIF_GOREV.md` K-001..K-014 ile değiştirilmiştir ve uygulanmaz.

Her model, ajan, geliştirici ve CLI bu belgeyi aynı anlamla uygular. Modelin kendi alışkanlığı,
provider özelliği, context window'u veya kişisel prompt dosyası ürün kurallarını değiştiremez.

## 1. Ürün kimliği

```text
Ürün: Zekam
Repository: zekam
Python paketi: zekam
CLI: zekam
Kullanıcı veri kökü: ZEKAM_HOME
Mimari: Modüler monolit + ayrı CLI/API/scheduler/worker process'leri
Kanonik durum: CPython SQLite operational schema v1
Vector: SQLite FTS5 + sqlite-vec; BGE-M3 dense 1024 cosine
```

Ürün kimliği yalnız `mimari/ZEKAM_KIMLIK_SOZLESMESI.md` içindeki Zekam yüzeyidir.
Package, CLI, environment, home, schema ve DB kimliği için uyumluluk alias'ı üretilmez.

## 2. Nihai hedef

Kullanıcı kısa doğal dil komutlarıyla şunları yapabilmelidir:

```text
"gpu projesindeki 123 numarali defectin kok nedenini arastir"
"bugun hangi islerimiz var"
"oracle paketlerinin postgresql karsiliklarini analiz et ve planla"
"bu akademik makaleyi projeyle karsilastir, uygun kisimlari uygula"
"codex kotasi yuzde 40 altindaysa claude'a, o da yuzde 30 altindaysa kurum ici modele gec"
"bu gece arastir, sabah karar verebilecegim raporu hazirla"
"nerede kaldik"
```

Zekam bu komutları proje, Work Graph, research, plan, agent harness, model routing, Knowledge
Plane, memory, verification ve receipt zincirine dönüştürmelidir. Model/CLI değişince context
kaybolmamalı; aynı iş iki kez veya iki builder tarafından alınmamalı; sonuçlar kanıtlı ve
yeniden başlatılabilir olmalıdır.

## 3. Başlamadan önce zorunlu okuma ve doğrulama

Her yeni oturum:

1. `00_BASLA.md` ve `DEVAM_PROTOKOLU.md` dosyalarını uygula.
2. `PROJE_MANIFESTI.yaml`, bağlayıcı `AKTIF_GOREV.md`, generated
   `AKTIF_GOREV.yaml` ve iş grafiğini oku.
3. Repository HEAD, dirty state, local schema state ve test baseline'ını doğrula.
4. Paket doğrulayıcısını çalıştır.
5. Tamamlanmış iddiasını kod/test/receipt olmadan kabul etme.
6. Aktif work item veya recovery-required claim varsa duplicate iş başlatma.
7. Aktif iş için gerekli bounded context'i derle.
8. Uygulama ve test planını exact logical resources ile kaydet.
9. Agentic işte minimum bir gerçek subagent atamadan yürütmeye başlama.
10. Kod mutation'ini bagli exact gercek source rootunda yap; proje kopyasi veya worktree uretme.

## 4. Değiştirilemez mimari ilkeler

### 4.1 Mimari modelden daha kalıcıdır

- Core hiçbir model sağlayıcısına, CLI'a veya tek bir agent framework'üne bağlanmaz.
- Codex, Claude Code, OpenCode ve kurum içi modeller adapter/client'tır.
- MCP dış tool/resource/prompt keşfi için adapter sınırıdır; Zekam'nin state veya authority
  sistemi değildir.
- A2A gibi federasyon protokolleri ileride dış ajan iletişimi için eklenebilir; internal
  durable runtime'ın yerine geçmez.
- Provider sonucu kanonik gerçek, görev durumu veya authorization değildir.

### 4.2 Tek kanonik Work sistemi

- Proje, talep, defect, task, subtask, decision ve research kayıtlarının tek otoritesi Work
  Graph'tır.
- Orchestration run/queue yalnız execution attempt'i temsil eder.
- Vector, FTS, dashboard, Markdown projection veya memory Work Graph'ın yerine geçmez.
- “Nerede kaldık?” önce exact Work/Run/Checkpoint kayıtlarından yanıtlanır.

### 4.3 Control, execution, knowledge ve memory sınırları

```text
Control Plane
  project registry, alias, Work Graph, Intent, Decision, Plan, policy, authorization

Execution Plane
  DAG, queue, lease, fencing, lock, claim, receipt, checkpoint, verifier, sandbox

Knowledge Plane
  source, artifact, normalized content, code/document/DB index, retrieval, citation

Memory & Learning Plane
  episodic/semantic/procedural/failure memory, hygiene, lessons, skills

Experience & Operations Plane
  CLI, API, MCP adapter, scheduler, reports, dashboard, telemetry
```

Bir düzlem diğerinin otoritesini sahiplenemez.

### 4.4 Kanonik ve derived veri ayrımı

PostgreSQL kanonik veya runtime ledger kayıtlarını tutar. pgvector/FTS tabloları, Markdown
index'ler, dashboard materialized view'ları ve graph görünümü yeniden üretilebilir derived
veridir. Derived kayıp olduğunda state kaybolmaz; rebuild action üretilir.

### 4.5 Local-first ve no-copy

- Core kodu Git repository'de sürümlenir.
- Kullanıcı state'i `ZEKAM_HOME` altında kalır.
- Haricî project source kendi dizininde kalır ve exact binding ile okunur/yazilir.
- Mutation yalniz bagli gercek source rootunda yapilir; kopya, mirror veya worktree uretilmez.
- Portable kayıtlarda absolute path veya aktif lease taşınmaz.
- Secret değerleri repository veya backup'a girmez.

## 5. Agent harness — zorunlu gerçek çalışma modeli

Sadece prompt zinciri veya birkaç subprocess çağrısı “harness” sayılmaz. Aşağıdaki katmanlar
kod ve test ile uygulanmalıdır:

```text
Request Intake
→ Project/Work Resolver
→ Context Compiler
→ Work Classifier
→ Route Planner
→ Policy/Capability/Authorization Gate
→ Model Decision
→ Tool/Secret/Outbound Gate
→ Task DAG
→ Durable Queue + Admission
→ Lease + Fencing + Logical Locks
→ Execution Host + Sandbox
→ Claim-before-effect
→ Worker/Subagent Dispatch
→ Checkpoint/Continuity
→ Agent Result Normalizer/Envelope
→ Independent Verifier
→ Terminal Receipt
→ Work Completion/Projection
→ Runtime Observation/Learning
```

### 5.1 Prepare/apply ayrımı

- `prepare` salt okunur; provider call, secret resolution, network veya mutation yapmaz.
- Plan exact input/source/policy/model/resource digest'lerine bağlanır.
- `apply`, plan drift olmadığını yeniden doğrular.
- Mutation ve outbound effect exact authorization gerektirir.
- Bir authorization planın dışındaki path, tool, network, DB veya provider kapsamını açmaz.

### 5.2 Subagent politikası

```yaml
deterministic_operations:
  minimum_subagents: 0

agentic_operations:
  minimum_subagents: 1
  coordinator_counts_as_subagent: false
  fixed_global_maximum: null
```

Agentic iş; araştırma, analiz, mimari karar, kod üretimi, defect çözümü, plan sentezi,
değerlendirme veya benzeri belirsiz işi kapsar.

- Koordinatör planlar ve fan-in yapar; child işi kendisi yapıp subagent kullanmış sayılmaz.
- Sabit maksimum yoktur. Paralellik hazır DAG düğümleri, logical resource çakışmaları,
  worker kapasitesi, token/cost/latency bütçesi, quota ve data classification ile hesaplanır.
- Tek anlamlı alt problem için bir subagent yeterli olabilir.
- Aynı işi birden fazla builder'a verme.
- Aynı yazılabilir resource üzerinde en fazla bir builder.
- Yüksek/kritik riskte builder'dan farklı verifier zorunlu.
- Her child strict result envelope döndürür; free-text authoritative sonuç değildir.
- `partial`, `failed`, `blocked`, `recovery-required` ve `abstained` kaybolmaz.

### 5.3 Queue, lease, fencing ve lock

PostgreSQL durable queue uygulanmalıdır. Claim seçiminde queue-benzeri tablo için kontrollü
`FOR UPDATE SKIP LOCKED` kullanılabilir; genel domain okumasında kullanma.

- Enqueue, job ve outbox event'i aynı transaction'da yazar.
- Claim fencing token'ı artırır.
- Heartbeat, complete, fail ve lock release owner digest + lease + fence eşleşmesi ister.
- Süresi dolmuş eski worker sonuç yayımlayamaz.
- Logical lock örnekleri:
  - `project:<id>`
  - `work:<project>:<work>`
  - `path:<project>:<relative-path>`
  - `db-object:<project>:<kind>:<name>`
  - `artifact:<project>:<id>`
  - `model-benchmark:<model-id>:<suite-id>`
- Parent/child path conflict'i tanınır.
- Project write lock aynı projenin task/path write lock'larıyla çakışır.

### 5.4 Effect claim ve receipt

Network, filesystem write, DB mutation, provider effect, Git commit/push veya dış sistem
değişikliği önce durable Effect Claim ister.

```text
claim mevcut + terminal receipt yok
→ recovery-required
→ otomatik retry yok
```

Receipt; plan, step, resource, authorization, execution identity, queue attempt, fence,
result/failure digest ve adapter evidence ile eşleşir. “Agent başarılı dedi” receipt değildir.

### 5.5 Sandbox

- Entegre source exact bagli gercek proje kokudur; mutation dogrudan bu kokte yapilir.
- Her yazilabilir logical resource ayni anda yalniz bir builder tarafindan degistirilir.
- Exact relative path allowlist.
- Traversal/symlink escape reddi.
- Network default-deny; exact host/operation allowlist.
- Shell yerine typed tool/capability tercih edilir.
- Test/verifier bagli gercek source tree'yi işlem sonunda yeniden doğrular.
- Direct-source değişiklik, allowlist + revision drift + changed-path doğrulamasından geçirilir.

## 6. Proje ve Work Graph

### 6.1 Proje entegrasyonu

`zekam project add` veya doğal dil eşdeğeri:

1. Git/source root'u read-only keşfeder.
2. Portable project ID üretir.
3. Alias, slug, display name ve fingerprint kaydeder.
4. Source binding/revision oluşturur.
5. Framework, version, language, module, DB, build, test, quality ve security capability
   profile çıkarır.
6. Secret/sensitive dosyaları profile ve index dışında bırakır.
7. Project-specific benchmark suite üretir.
8. İlk source/knowledge index planını hazırlar.
9. Proje köküne Zekam dosyası yazmaz.

### 6.2 Doğal dil çözümleme

- Exact ID ve alias önce gelir.
- “gpu projesi” gibi referanslar candidate listesiyle çözülür.
- Belirsiz iki proje varsa mutation yapmadan seçim ister.
- “bunu” gibi anaphora bounded conversation subject veya current Work'ten çözülür; yoksa
  konu uydurulmaz.
- Talep/defect numarası exact lookup ile aranır; semantic similarity numarayı değiştiremez.

### 6.3 Work yaşam döngüsü

Tipler:

```text
request, defect, task, subtask, decision, research, idea, maintenance
```

Durumlar:

```text
proposed, ready, active, blocked, verification, completed, cancelled, archived
```

Her revision immutable event üretir. `completed` için acceptance evidence gerekir. Reopen
yeni görünür revision'dır. Dependency ve parent graph acyclic olmalıdır.

## 7. Model inventory, test, routing ve quota

### 7.1 Envanter

`modeller/KANONIK_MODEL_ENVANTERI.yaml` içindeki 20 Model ID bağımsız yönetim nesnesidir.
Aynı backend/model adı farklı Model ID veya protokolle geliyorsa birleştirme.

Aktif envanter secret/endpoint değeri taşımaz:

```text
model_id
access_name
backend_model
provider_protocol
declared_mode/category
cost evidence
endpoint_ref
credential_ref
source digest
```

Ham teknik endpoint bilgisi yalnız Git-ignore edilmiş yerel referansta kalır.

### 7.2 Health ve contract testleri

Her tip kendi sentetik fixture'ını kullanır:

- chat/code: response, JSON schema, tools, context, Türkçe, timeout/cancel
- embedding: dimension, finite values, determinism, batch/single, query/passage behavior
- reranker: pair/list contract, monotonic relevance, timeout
- Whisper: audio format, Türkçe transcript, WER/CER
- guardrail: label/schema, safe/unsafe, FP/FN
- VL: gerçek image input ve grounded response
- bütün modeller: secret/prompt injection, error sanitization, latency

Health başarı yetenek kanıtı değildir; yalnız benchmark eligibility sağlar.

### 7.3 Benchmark

- En az 5 confidence-safe repetition.
- Tested model ile verifier model/identity farklıdır.
- General suite ve project-specific micro suite ayrıdır.
- Sonuç prompt/response/source text değil, metric/provenance taşır.
- Her trial: parse, format, evidence, verifier, timeout, failure category, quality,
  reliability, latency, input/output token, retry, human correction, estimated/actual cost.
- Aggregate mean/median/p95/variance ve verifier-approved cost hesaplar.
- Tek unsafe trial yüksek ortalama ile gizlenmez.
- Provider call'dan önce durable claim; duplicate plan ek maliyet üretmez.
- Başarısız model quarantine/cooldown'a girer.
- Inventory/source/suite/policy değişince eski sonuç stale olur.

### 7.4 Routing

Hard gate sırası:

```text
enabled
→ health current/passed
→ workload/client/modality support
→ project benchmark current/passed
→ data locality/security
→ context/tool/structured-output requirement
→ independent verifier exclusion
→ latency/money/token budget
→ quota pool
```

Qualified adaylar quality, reliability, project specialization, observed success, latency,
token efficiency, cost ve human correction ile puanlanır. Karar bütün evidence digests ve
rejected reasons taşır.

### 7.5 Quota fallback

Kullanıcı policy'si:

```text
Codex trusted remaining ratio < 0.40
→ Claude adaylarını değerlendir

Claude trusted remaining ratio < 0.30
→ kurum içi/OpenCode model adaylarını değerlendir
```

- Kalan oran resmi client observation veya güvenilir local telemetry ile okunmuyorsa
  `unknown` kaydet; tahmin etme.
- Quota provider markasına değil execution path/quota pool'a bağlıdır.
- Fallback Work/Plan/Checkpoint'i taşır; transcript taşımaz.
- Fallback model capability ve data security gate'lerini yine geçmelidir.
- Modelin büyük/ünlü olması otomatik öncelik vermez.
- Limit yok diye düşük kalite modeli kritik işe seçme.

### 7.6 Model tartışması/fusion

Kullanıcı “Kimi ve Opus tartışsın” benzeri talep verdiğinde:

- Ortak evidence packet ve question digest kullan.
- En fazla 2 tur, 10 dakika ve açık token/cost bütçesi.
- Her katılımcı bağımsız finding/objection verir.
- Synthesizer uzlaşıyı ve çelişkiyi ayrı kaydeder.
- Direct contradiction verifier veya insan review olmadan çözülmüş sayılmaz.
- Tartışma authority veya mutation approval üretmez.

## 8. Knowledge Plane ve RAG

`bilgi/` sözleşmeleri ve Context Vault aktif görev kapsamı uygulanmalıdır.

### 8.1 Ingestion

Kaynaklar:

```text
DOCX, PDF, TXT, Markdown
PNG, JPEG, TIFF
ZIP/TAR archive
Git repository URL/ref
izinli local directory
Oracle/PostgreSQL metadata
Work/research/decision/memory projection'lari
```

Pipeline:

```text
validate/safety scan
→ immutable original artifact
→ versioned ingestion job
→ parser router
→ normalized content
→ structure-aware/token-aware chunk
→ embedding/FTS/identifier index
→ atomic activation
```

Parser doğrudan vector üretmez. Normalize model heading, paragraph, list, table, code,
formula, image, caption, OCR, file header, symbol ve configuration birimlerini ve locator
bilgisini taşır.

### 8.2 İlk embedding profili

```text
model_ref: openai/BAAI/bge-m3
dimension: 1024
distance: cosine
query_prefix: versioned/configured
passage_prefix: versioned/configured
```

Query/passage prefix varsayımı A/B golden evaluation ile seçilir. Farklı prefix/config aynı
profile ID altında karıştırılmaz. Parser/chunker/profile değişirse re-index gerekir.

### 8.3 Retrieval

Sıra:

```text
exact project/work/defect/document ID
→ path/symbol/database object exact
→ alias/trigram
→ PostgreSQL FTS
→ BGE-M3 dense 1024
→ RRF
→ opsiyonel reranker
→ content/provenance dedupe
→ parent/neighbor expansion
→ token-budgeted evidence packet
```

- Ham dense ve lexical skorları kalibrasyonsuz toplama.
- HNSW filtreli recall ölç; iterative scan/partial index/partition kararını evaluation ile ver.
- Exact identifier düşük dense skor nedeniyle elenmez.
- No-hit document question günlük sohbet sayılmaz.
- Context maximum chunk sabitiyle değil token/evidence budget ile oluşturulur.
- Retrieval sonucu authority veya current Work state olamaz.

### 8.4 Citation

- PDF: page ve mümkünse bounding box
- DOCX: heading path ve block/table locator; uydurma page yok
- OCR: page, bbox, confidence
- Code: repository, relative path, symbol, line range, revision
- DB: connection logical ref, schema/object/kind/revision; satır verisi varsayılan dışı
- Research: source snapshot, locator, content digest

Cevap yeterli evidence yoksa abstain/no-answer üretir.

### 8.5 Repository ve DB güvenliği

- Ingestion sırasında build/test/hook/package install/submodule/LFS otomatik çalıştırma.
- `.gitignore`, `.contextvaultignore` ve system deny list uygula.
- Secret/private key/env/binary/archive bomb skip/fail policy.
- Allowed root ve canonical path enforcement.
- Oracle/PostgreSQL için varsayılan metadata-only; row read ayrı policy/authorization ister.

## 9. Profesyonel bellek ve Mem0 uyarlaması

Zekam kendi `MemoryEngine` portuna sahip olmalıdır. İki adapter:

```text
NativePostgresMemoryEngine   — zorunlu ve kanonik üretim tabanı
Mem0OssMemoryEngine          — opsiyonel, self-hosted adapter
```

Mem0 bir bağımlılık veya authority değildir. Kullanıcı/ajan/run scope, vector arama, metadata
filter, temporal/entity signals ve consolidation desenleri uyarlanır; Work/Run/Policy state'i
Mem0'ya devredilmez.

### 9.1 Bellek sınıfları

- `working`: bounded aktif context, kısa ömür
- `episodic`: ne oldu, hangi work/run/evidence
- `semantic`: doğrulanmış proje bilgisi
- `procedural`: kanıtlı yöntem ve runbook
- `preference`: kullanıcı tercihi/policy olmayan davranış ayarı
- `failure`: başarısız yaklaşım, kök neden, tekrar önleme

### 9.2 Scope ve izolasyon

```text
global/user
project
work-item
run
agent
```

Cross-project sonuç açık multi-project scope olmadan seçilemez. Agent-private scratchpad
durable memory değildir. Secret/credential/private reasoning memory'ye girmez.

### 9.3 Yazma yaşam döngüsü

```text
observation
→ candidate
→ dedupe/conflict check
→ evidence validation
→ independent review/verifier
→ active
→ superseded/revoked/archived
```

Ham model cümlesi doğrudan semantic/procedural memory olamaz. Mevcut bilgi sessizce overwrite
edilmez; revision ve supersession relation oluşur. Source drift memory'yi stale yapar.

### 9.4 Retrieval ve hygiene

Memory retrieval:

```text
exact identity/tag
+ PostgreSQL FTS
+ BGE-M3 vector
+ entity/relation
+ temporal validity/recency
+ authority/freshness
```

Context Compiler seçimin nedenini açıklar. Hygiene duplicate, conflict, stale, unused,
retention-review ve source-version conflict'i raporlar; otomatik silmez.

### 9.5 Failure learning ve skill

Aynı failure occurrence key tekrar ederse:

1. Failed run/command/evidence kaydet.
2. Verified root cause olmadan ders üretme.
3. En az iki bağımsız observation veya kritik tek olay için explicit review.
4. Learning candidate oluştur.
5. Hedefi test/eval/guidance/skill olarak seç.
6. Ayrı evaluator ve verifier fixture üzerinde ölçsün.
7. Skill activation exact approval ve rollback planı ister.
8. Skill kendi kendini aktif registry'ye yazamaz.

## 10. Research-to-delivery akışı

### 10.1 Research

Canonical research DAG başlangıç rolleri:

```text
researcher
architecture-reviewer / domain-reviewer
critic / counter-evidence-researcher
synthesizer
citation-verifier
```

İlk bağımsız roller paralel olabilir. Her finding source snapshot/revision/locator/digest
taşır. Çelişkiler compatible, scope/terminology, stale-source, evidence-gap veya direct
contradiction olarak sınıflandırılır.

### 10.2 Prompt zinciri desteği

Kullanıcının istediği zincir desteklenir:

```text
Model A: araştırma sorusu ve araştırma promptu üretir
Model B: kanıtlı araştırma yapar
Model C: araştırmayı karar ve uygulanabilir plana dönüştürür
Verifier: evidence, scope ve acceptance'i bağımsız doğrular
```

Her aşama ayrı artifact/result digest taşır. Bir modelin output'u sonraki aşamada untrusted
input'tur; talimat/authority olarak yürütülmez.

### 10.3 Implementation

Verified research/decision'dan:

1. Exact file/resource/effect planı oluştur.
2. Risk, acceptance, test ve rollback belirt.
3. Authorization al.
4. Builder bagli gercek proje dosyasinda uygular; kopya veya worktree kullanmaz.
5. Zekam testleri bağımsız yeniden çalıştırır.
6. Verifier patch/evidence'i doğrular.
7. Patch/receipt/continuity üret.
8. Kullanıcı policy'sine göre local commit oluştur.
9. Push yalnız açık talep ve authorization ile.

## 11. Secret Broker, authorization ve onay ergonomisi

### 11.1 Secret Broker

Model ve subagent yalnız logical `SecretRef` görür:

```text
provider_ref
realm/project scope
purpose
allowed operation
expiry
version
```

Broker value'yu yalnız authorized adapter çağrısı anında process memory'de çözer. Değer:
prompt, environment dump, command argument, log, exception, vector, artifact, report,
benchmark veya backup'a yazılmaz.

### 11.2 Risk-temelli approval

Otomatik/read-only:

- exact status/history/list
- authorized source read
- local index/retrieval
- dry-run/plan
- derived projection/rebuild (user data değiştirmiyorsa)

Exact one-shot approval:

- network/provider call
- secret resolution
- user data/memory/skill mutation
- integrated project mutation
- DB write veya migration
- Git commit/push
- destructive/irreversible effect
- high/critical risk

Kullanıcı “bu planı uygula” diyerek exact effect'i açıkça yetkilendirmişse child step'lerde
aynı şey tekrar tekrar sorulmaz. Scope/source/policy drift olursa yeni onay gerekir.

## 12. Scheduler, gelen belgeler ve raporlar

Scheduler sohbet process'inden bağımsız, durable job ve idempotency key ile çalışır.

Zorunlu işler:

- `gelen-belgeler` watcher
- project incremental scan
- model health/quarantine/cooldown
- benchmark staleness
- memory hygiene
- claim-without-receipt/recovery scan
- stale index/profile scan
- academic document comparison
- gece research/analysis jobs
- günlük genel ve proje raporu
- backup/restore verification
- retention review

Sabah raporu:

```text
dun tamamlanan/failed/blocked işler
aktif lease ve recovery-required
subagent/model/client dagilimi
okunan kaynaklar ve evidence
token, cost, latency, quota observations
model health/benchmark degisimi
memory/skill adaylari
retrieval/index sorunlari
security/policy olaylari
bugun onerilen next actions
```

## 13. CLI, API ve dashboard

Minimum CLI:

```text
zekam doctor
zekam init
zekam project add|list|show|resume|scan|rebind
zekam ask "<dogal dil>"
zekam work list|show|history|next|resume
zekam research start|dispatch|status|show|import
zekam plan show|approve|apply
zekam run status|cancel|recover
zekam model inventory|health|benchmark|decide|report
zekam knowledge ingest|index|search|explain|reindex
zekam memory list|search|explain|hygiene|review
zekam skill list|candidate|evaluate|activate|deprecate|retire
zekam scheduler list|run|pause|resume
zekam report today|project|models|system
zekam backup|restore|verify
```

API aynı application service'i kullanır; CLI ayrı ürün kuralı yazmaz. Dashboard önce read-only
projection'dır: Work, run, queue, agents, models, knowledge, memory, scheduler ve reports.
Daha sonra graph/sinaps görünümü eklenebilir; derived kalır.

## 14. Dizin ve sahiplik

Source repository:

```text
src/zekam/
tests/
migrations/
schemas/
config/
docs/
scripts/
```

Kullanıcı verisi:

```text
ZEKAM_HOME/
  global/
    modeller/
    politikalar/
    bellek/
    raporlar/
    runtime/
  projeler/<project-id>/
    proje.json
    baglantilar/
    talepler/
    defectler/
    isler/
    arastirmalar/
    kararlar/
    planlar/
    bilgi/
    bellek/
    artifacts/
    runtime/
    raporlar/
  gelen-belgeler/
  worktrees/
  sandboxlar/
  kilitler/
  secrets/
```

Human-facing klasör adları Türkçe olabilir; internal package/schema/table adları teknik
tutarlılık için İngilizce kalabilir.

## 15. Uygulama iş grafiği

`kalite/UYGULAMA_IS_GRAFIGI.yaml` kanonik başlangıç backlog'udur. Faz numarası bitiş sınırı
değildir. Bağımlılıkları çözerek bütün task'ları ve Global DoD'yi tamamla.

Özet fazlar:

```text
0  Baseline ve bootstrap
1  PostgreSQL canonical persistence ve realm
2  Project registry, alias, binding, capability
3  Work Graph, Intent, Decision, Plan
4  Policy, Secret Broker, exact authorization
5  AgentHarness, queue, lease, fencing, lock, claim/receipt, verifier
6  Model inventory, health, modality contracts
7  Benchmark, routing, quota, deliberation
8  Context compiler, checkpoint, continuity
9  Natural language intake ve evidence research
10 Sandbox implementation delivery ve client adapters
11 Knowledge ingestion, versioning, artifacts, OCR, code, DB
12 Hybrid retrieval, reranking, citation, evaluation
13 Native memory ve Mem0 adapter
14 Learning, skills ve measured loop
15 Scheduler, gelen belgeler ve sabah raporu
16 CLI, API, MCP, dashboard, observability
17 Hardening, DR, release ve kimlik bütünlüğü
```

Bir fazın dokümanı tamamlandı diye kod tamamlanmış sayılmaz. Her task acceptance ve test
evidence'i ister.

## 16. Commit ve Git politikası

Her anlamlı, doğrulanmış ve bağımsız değişiklik küçük bir local commit olabilir.

Başlık:

```text
<tur>: <kisa emir cumlesi>
```

İzinli türler:

```text
ozellik, duzeltme, yeniden-duzenleme, test, belge,
altyapi, guvenlik, performans, gecis, bakim
```

Başlık ve gövde Türkçe anlam taşır, **yalnız ASCII karakter** kullanır. Gövde:

```text
Neden:
- ...

Degisiklik:
- ...

Kanit:
- ...

Risk:
- ...

Geri donus:
- ...
```

“update”, “fix stuff”, anlamsız ID veya yalnız İngilizce mesaj kabul edilmez. Commit ancak
ilgili test/acceptance geçince yapılır. Push default deny'dır.

## 17. Kalite ve stop kuralları

### 17.1 Zorunlu kapılar

- format/lint/type
- unit/integration/property/concurrency
- PostgreSQL acceptance ve migration drift
- security/path/secret/prompt-injection
- retrieval/memory/model benchmark
- CLI/API contract ve E2E
- dependency audit, SBOM ve dead code
- backup/restore/recovery
- documentation-code consistency

### 17.2 Ne zaman durabilirsin?

Yalnız:

1. Global DoD ve release verification tamamlandı, veya
2. Haricî ve çözülemeyen bir blocker var.

Blocker raporu şu alanları taşır:

```text
work/task/step
başarısız exact komut veya adapter operation
sanitized hata category/digest
tekrarlanabilir kanıt
denenen güvenli yaklaşımlar
neden devam edilemediği
gerekli haricî aksiyon
son güvenli checkpoint
next safe action
```

Token/context azaldı, iş uzun, model limiti var veya başka model daha uygun gibi durumlarda
bırakma; checkpoint üret, fallback route seç ve devam et.

### 17.3 Yasak sahte tamamlanma

- Boş klasörü implemented sayma.
- Interface/port yazıp adapter'ı yokken capability tamamlandı deme.
- Compose'da servis adı var diye entegrasyon çalışıyor deme.
- Mock-only route'u production kabul etme.
- Markdown checklist'i test kanıtı yerine kullanma.
- Agent output'unu bağımsız verifier yerine kullanma.
- İlk 10 PR bitti diye nihai görevi bitirme.
- Kapsamı “sonraki faz” diyerek bu görevin dışına atma.

## 18. Her oturumun zorunlu teslimi

Oturum sonunda kullanıcıya ve sonraki modele:

```text
aktif Work ve task
değişen dosyalar
uygulanan karar
subagent sonuçları
çalıştırılan test/eval ve sonuç
claim/receipt/recovery durumu
commit kimliği (varsa)
bilinen risk/blocker
bir sonraki exact safe action
```

sun. Kayıtları kanonik store ve continuity ile eşleştir.

## 19. Nihai teslim

Global DoD tamamlandığında:

1. Tüm migration/test/eval/security/DR kapılarını temiz ortamda yeniden çalıştır.
2. En az bir gerçek proje, bir defect, bir araştırma→plan→implementation akışı, bir model
   fallback'i, bir Knowledge Plane ingestion ve bir crash-recovery E2E kanıtla.
3. 20 Model ID health/benchmark durum raporu üret.
4. Memory ve skill promotion negatif testlerini çalıştır.
5. Secret leakage taraması yap.
6. Documentation-code consistency raporu üret.
7. Release artifact, checksum, SBOM, backup ve rollback planı oluştur.
8. `zekam report system` final raporunu üret.
9. Work Graph'ta yalnız kanıtlı item'ları tamamla.
10. Tekil Zekam kimliği doğrulanmadan release yayınlama.

Bu noktaya kadar çalışma kesintisiz devam protokolünün kapsamındadır.
