# Z — Nihai Mimari Araştırma Raporu

**Tarih:** 20 Ağustos 2026  
**Karar durumu:** Araştırma tamamlandı; sonuç yeni bir ürün/proje gerektiriyor.  
**Önerilen ürün adı:** `Z Control Plane`  
**Önerilen repository:** `z-control-plane`  
**Önerilen CLI:** `zctl`

## 1. Nihai karar

Kurulması gereken yapı; KRCN Core, ZEKAM ve Context Vault kodlarının birleştirildiği dördüncü bir monolit değildir. Sıfırdan açılacak, eski sistemlerden yalnızca doğrulanmış sözleşmeleri ve test vakalarını alan, **modelden, sağlayıcıdan ve CLI’dan bağımsız bir AI Engineering Control Plane** olmalıdır.

İlk sürüm:

- tek repository içinde **modüler monolit**,
- aynı kod tabanından çalışan ayrı API, CLI, scheduler ve worker süreçleri,
- kanonik durum için PostgreSQL,
- semantic projection için pgvector,
- ham/normalize artifact için yerel CAS veya S3 uyumlu object storage,
- geçici uyanma/sinyal için isteğe bağlı Redis,
- dış model ve CLI’lar için dar, tipli adaptörler,
- kaynak proje kökleri için salt-okunur binding,
- yazma işleri için izole detached worktree/sandbox

olarak kurulmalıdır.

Bu kararın nedeni şudur: kalıcı değer modelde değil; iş durumu, kanıt, yetki, çalışma izi, model seçimi, recovery ve doğrulama sözleşmelerindedir. Model veya CLI değişse de yeni yürütücü, kanonik kayıt ve authority taşımayan continuity paketi üzerinden aynı noktadan devam edebilmelidir.

## 2. Sistem tanımı

Z Control Plane, doğal dil hedefini aşağıdaki kanıtlı zincire dönüştürür:

```text
Doğal dil hedefi
  → proje ve iş kimliği çözümleme
  → Work + Intent revizyonu
  → risk/capability/resource analizi
  → yürütme şekli ve model kararı
  → en az bir tamamlayıcı subagent ile agentic çalışma
  → checkpoint + result envelope
  → bağımsız doğrulama
  → claim/receipt ile terminal sonuç
  → kanıtlı rapor veya exact uygulama planı
  → gerekiyorsa izole uygulama ve patch
```

Sistem dört düzleme ayrılır:

1. **Control Plane:** proje, iş, karar, plan, yetki, model, policy ve scheduler.
2. **Execution Plane:** DAG, queue, worker, lease, fencing, lock, checkpoint, claim ve receipt.
3. **Knowledge Plane:** sürümlü kaynaklar, artifact’lar, normalize içerik, hybrid retrieval, citation ve evaluation.
4. **Experience Plane:** CLI, API, Türkçe klasör/projection’lar, günlük rapor ve ileride dashboard.

## 3. Değişmez mimari ilkeler

1. PostgreSQL kanonik gerçektir; sohbet, Markdown, dashboard, embedding ve cache projection’dır.
2. `project.resume` veya “nerede kaldık?” exact Work/Run/Checkpoint/Receipt zincirinden cevaplanır.
3. Kaynak proje dizinine doğrudan yazmak yasaktır.
4. Model ve CLI yalnız yürütücüdür; yetki veya state sahibi değildir.
5. Aynı yazılabilir logical scope için aynı anda tek builder vardır.
6. Agentic çalışma en az **bir subagent** kullanır; koordinatör subagent sayılmaz.
7. Deterministik exact lookup/status/format dönüşümü agentic çalışma değildir ve sıfır subagent ile yapılabilir.
8. Sistem çapında sabit bir “maksimum ajan” yoktur. Paralellik her run için DAG bağımsızlığı, resource lock, kota, güvenlik sınıfı ve bütçeden hesaplanır.
9. Her agent sonucu strict result envelope ile döner; serbest metin “başarılı” beyanı terminal kanıt değildir.
10. Yazma veya dış dünya etkisi durable claim ve terminal receipt olmadan tamamlanmış sayılamaz.
11. Verifier, doğruladığı builder ile aynı execution identity olamaz; yüksek/kritik riskte model ailesi de ayrılmalıdır.
12. Secret değeri hiçbir prompt, log, vector, artifact metadata veya continuity paketinde bulunmaz.
13. Öğrenme, bellek veya skill tek gözlemle aktif sistemi değiştiremez.
14. Kapsam, kalite ve güvenlik eşiğini sağlayan en düşük maliyetli/uygun model seçilir; model büyüklüğü tek ölçüt değildir.
15. Daha çok context veya daha çok ajan başarı metriği değildir. Başarı; doğrulanmış sonuç, düşük tekrar, düşük token/maliyet ve güvenli recovery’dir.

## 4. Bounded-context ve sahiplik haritası

| Bağlam | Sahip olduğu kanonik kayıtlar | Sahip olmadığı alanlar |
|---|---|---|
| `projects` | Project, alias, source binding, module, capability profile, source revision | Work durumu, model skoru, chunk/vector |
| `work` | Work item, relation, event, Intent, Decision, Plan, Approval | Worker lease, vector benzerliği, provider session |
| `runtime` | Task DAG, step, attempt, queue claim, lease, fencing, logical lock, checkpoint, effect claim/receipt, verification, result envelope | İşin işsel durumu, secret değerleri |
| `models` | Provider/client/model inventory, capability, health, benchmark, quota pool, price evidence, assignment | Work authority, credential değeri |
| `research` | Question, source snapshot, claim, contradiction, citation verdict, synthesis/report | Otomatik knowledge/policy promotion |
| `knowledge` | Source/version/artifact identity, normalized-content manifest, citation, evaluation run | Work status, approval, active lease |
| `memory_skills` | Memory revision, learning candidate, skill candidate/evaluation/lifecycle | Otomatik policy veya registry mutation |
| `governance` | Policy, capability grant, one-shot authorization, provider assurance, SecretRef, audit | Secret plaintext, model output as authority |
| `operations` | Schedule, inbox item, report job, backup manifest, health event | Domain state’in yerine geçen dashboard verisi |

## 5. Kanonik veri ve projection ayrımı

### Kanonik PostgreSQL kayıtları

- realm, actor ve project kimlikleri;
- alias ve read-only source binding’ler;
- Work item/revision/relation/event;
- Intent, Decision ve Plan revision’ları;
- exact one-shot authorization;
- Run, TaskPlan, Step, Attempt ve Checkpoint;
- Queue claim, lease, fencing token ve logical resource lock;
- Effect Claim ve terminal Effect Receipt;
- Agent Result Envelope ve Verification;
- model inventory, health, benchmark, quota observation ve assignment;
- research question, source snapshot, claim, contradiction ve citation verdict;
- source/version/artifact identity ve citation;
- memory/skill lifecycle;
- schedule, audit ve backup manifest.

### Yeniden üretilebilir projection’lar

- Markdown iş/proje/rapor görünümü;
- chunk, embedding, full-text, trigram, alias ve symbol index’leri;
- retrieval cache ve context candidate skorları;
- Work/Run dashboard read-model’leri;
- günlük/genel rapor projection’ları;
- model yetenek tablolarının insan görünümü;
- search autocomplete ve graph görünümü.

Projection eksik veya bozuksa ilgili sorgu `stale/unavailable` döner; kanonik gerçeği değiştirmez.

## 6. Referanslar için Al / Yeniden Tasarla / Alma matrisi

| Referans | Al | Yeniden tasarla | Alma |
|---|---|---|---|
| KRCN Core | Work Graph, Project Capsule/no-copy, capability profile, lease/fencing/lock, claim/receipt, result envelope, continuity, model benchmark, memory/skill gate | Çok sayıdaki dosya sözleşmesini küçük bounded context ve PostgreSQL tablolarına indir; approval UX’i risk-temelli yap | Repository’yi veya devasa application katmanını topluca kopyalama; SQLite’ı çok-worker kanonik queue yapma |
| ZEKAM | Natural-language intake, revisioned Intent, evidence research, Decision/Plan digest, one-shot approval, detached worktree, path allowlist, verifier, receipt/handoff | Dikey workflow’ları ortak runtime ve ortak Work Graph üstünde kur | İkinci Work Graph, ikinci approval sistemi veya ikinci model gateway oluşturma |
| Context Vault | Sürümlü ingestion, immutable artifact, normalize içerik, parser/chunker registry, BGE-M3 1024, hybrid retrieval, citation/eval | Knowledge Plane olarak portlar üzerinden yeniden kur; active task kapsamını aşamalı taşı | Mevcut monolitik/demo backend’i ürün çekirdeği sayma; retrieval’ı görev state’i yapma |
| Avenox | Harness-first yaklaşım, intent-over-persona, scoped memory, permission/secret hub, ölçümlü loop, dinamik skill, gece işleri | İçerik fikirlerini typed contract, metric ve policy’ye çevir | Video söylemini kanıt/test olmadan ürün gereksinimi sayma; sınırsız self-modification |
| DevDan | Tek owner, builder–validator ayrımı, sandbox, typed tool security, event observability, private skill catalog, bounded deliberation | Çok-agent desenlerini dependency/resource aware scheduler’a çevir | Aynı yazma kapsamını birden fazla modele verme; her işi agent’a yaptırma |

## 7. Agent ve subagent sözleşmesi

### 7.1 Minimum kural

```yaml
agentic_work:
  min_subagents: 1
  coordinator_counts_as_subagent: false
  fixed_global_max_subagents: null
```

Bu kural “her komut iki model çağırır” anlamına gelmez. Şu işlemler agentic değildir ve subagent gerektirmez:

- exact ID/status sorgusu;
- schema doğrulama;
- digest hesaplama;
- format/projection üretimi;
- deterministik migration planlama;
- hazır kanıt üzerinden metric hesaplama.

Agentic işlerde varsayılan şekiller:

| İş tipi | Minimum şekil |
|---|---|
| Standart araştırma | Koordinatör + 1 researcher subagent |
| Derin araştırma | Researcher + gerektiğinde critic/citation-verifier |
| Kod değişikliği | Tek builder subagent; deterministic test/verifier zorunlu |
| Yüksek/kritik riskli mutation | Tek builder + ayrı verifier subagent |
| İki ayrık read-only alt problem | Bağımsız iki subagent paralel olabilir |
| Tek küçük fakat belirsiz analiz | 1 subagent; koordinatör sentezler |

### 7.2 Dinamik üst sınır

Ürün düzeyinde sabit maksimum yoktur. Run için izin verilen concurrency:

```text
min(
  bağımsız ve lock-çakışmasız hazır DAG düğümleri,
  worker kapasitesi,
  provider/quota kapasitesi,
  token-maliyet-zaman bütçesi,
  data-classification izinleri,
  proje policy limiti
)
```

Worker pool kapasitesi operasyonel emniyet limitidir; semantik “ajan maksimumu” değildir.

### 7.3 Result Envelope

Her child şu alanları taşır:

```text
result_id, project_id, work_item_id, run_id, step_id, attempt_id
role, execution_identity, model_assignment_id
input_manifest_digest, source_revision_digest
status: completed | partial | failed | blocked | recovery-required | abstained
findings[], evidence_refs[], artifact_refs[], risks[]
missing_requirements[], next_safe_actions[]
effect_claim_ref?, effect_receipt_ref?, verifier_ref?
result_digest
```

Kurallar:

- `partial`, `failed`, `blocked` ve `recovery-required` tamamlanmış sayılmaz.
- Worker non-read effect bildirdiyse claim ve receipt zorunludur.
- Result içindeki secret, raw prompt, chain-of-thought ve fiziksel path reddedilir.
- Koordinatör yalnız doğrulanmış envelope’ları fan-in yapar.

## 8. Runtime, lock ve recovery

PostgreSQL queue tablosu `FOR UPDATE SKIP LOCKED` ile birden fazla consumer tarafından claim edilebilir. `SKIP LOCKED` yalnız queue benzeri seçimde kullanılmalı; normal domain okumalarında kullanılmamalıdır.

Her claim:

- `attempt_no` artırır,
- monoton `fencing_token` üretir,
- opaque owner token’ın yalnız digest’ini saklar,
- lease expiry belirler,
- exact logical resource setine bağlanır.

Logical resource örnekleri:

```text
project:<project-id>
work:<project-id>:<work-id>
path:<project-id>:<relative-posix-path>
database:<project-id>:<logical-object>
provider:<quota-pool-id>
```

Çakışma kuralı: aynı resource’a iki read izinlidir; taraflardan biri write ise paralellik yasaktır. Project-level write lock alt task/path lock’larıyla çakışır.

Recovery:

- read-only step idempotent ise yeni attempt ile yeniden çalışabilir;
- write/network claim var ve receipt yoksa `recovery-required` olur;
- aynı idempotency key + terminal receipt yeni dış etki üretmez, kanonik receipt’i döndürür;
- stale fencing token ile result/receipt publish edilemez;
- sessiz retry yoktur.

`LISTEN/NOTIFY` veya Redis yalnız worker’ı uyandırır; gerçek queue ve sonuç PostgreSQL’dedir.

## 9. Model Control Plane ve kota fallback

### 9.1 Envanter hiyerarşisi

```text
Provider → Client/Execution Path → Model → Model Revision → Quota Pool
```

Aynı marka altındaki farklı yollar ayrı quota pool olabilir. Örneğin:

- `claude-interactive-subscription`;
- `claude-agent-sdk-credit`;
- `anthropic-api-billing`;
- `codex-agentic-plan`;
- `openai-api-project`;
- `opencode-internal-gateway`.

Bu ayrım önemlidir; kullanım ve limit davranışı execution path’e göre farklı olabilir.

### 9.2 Adaptör sözleşmesi

```text
ProviderAdapter
- discover_models()
- describe_capabilities(model_ref)
- health_probe(model_ref, synthetic_fixture)
- execute(execution_request)
- cancel(execution_ref)
- observe_usage(execution_ref)
- observe_quota(quota_pool_ref)
```

İlk adaptörler:

1. `fake` — deterministik test adaptörü.
2. `opencode_cli` — kurum modelleri için ilk gerçek adaptör.
3. `codex_cli`.
4. `claude_cli`.
5. `openai_compatible` API.
6. `openai_api` ve `anthropic_api` gerekiyorsa ayrı adapter.

### 9.3 Eligibility hard-gate’leri

Model ancak şu koşulların tamamını sağlarsa adaydır:

- enabled ve güncel health-passed;
- workload, modality, tool/structured-output ve context ihtiyacını destekler;
- proje benchmark kalite tabanını geçer;
- veri sınıfı ve local/remote policy ile uyumludur;
- provider assurance ve gerekli authorization vardır;
- quota pool kullanılabilir veya güvenilir fallback policy’si vardır;
- latency/cost hard bütçesini aşmaz;
- verifier için builder identity/model exclusion kuralını ihlal etmez.

### 9.4 Puanlama

Hard gate sonrası skor yalnız config ile hesaplanır:

```text
verified_quality
+ observed_reliability
+ project/workload_fit
+ quota_headroom
+ token_efficiency
- normalized_latency
- normalized_cost
- recent_failure_penalty
```

Ağırlıklar sürümlü policy kaydıdır. Eksik veri uydurulmaz; `unknown` kalır ve confidence düşer.

### 9.5 Kota gözlemi

`QuotaObservation`:

```text
quota_pool_id
source: provider_api | cli_json | telemetry | manual | estimated
remaining_basis_points: nullable
reset_at: nullable
confidence_basis_points
observed_at
observation_digest
```

“Codex %40 altı” veya “Claude %30 altı” yalnız güvenilir `remaining_basis_points` varsa uygulanır. CLI kalan yüzdesini sunmuyorsa sistem yüzde uydurmaz; rate-limit hatası, cooldown, manuel bütçe ve kullanım gözlemleriyle karar verir.

Fallback yalnız güvenli step/checkpoint sınırında olur. Yeni model transcript’i devralmaz; bounded Continuity Packet ve current Context Manifest alır.

## 10. Context, memory ve continuity

### 10.1 Context Compiler

Zorunlu exact kayıtlar:

- current Work item + revision;
- current Intent revision;
- current Plan/Step;
- acceptance criteria;
- constraints/prohibited actions;
- project capability profile;
- source revision;
- authority özeti;
- gerekli evidence referansları.

İsteğe bağlı adaylar:

- ilgili code/document/DB metadata;
- önceki doğrulanmış karar ve episodic run;
- proje policy’si;
- aktif ve uyumlu skill’ler.

Her compilation seçilen/dışlanan kayıtları, nedeni, token hesabını ve digest’i taşır. Bütçeye sığmayan zorunlu kayıt varsa sessiz truncation yapılmaz.

### 10.2 Continuity Packet

- authority taşımaz;
- active lease veya owner token içermez;
- goal, status, current step, completed/pending işler, kararlar, riskler, ilk okunacak exact kayıtlar ve next safe action taşır;
- soft limit 24 KiB, hard limit 32 KiB önerilir;
- source/work/run revision değişirse stale olur.

### 10.3 Bellek katmanları

| Katman | İçerik | Terfi kuralı |
|---|---|---|
| Working | Checkpoint/context manifest | Run’a bağlı, kısa ömürlü |
| Episodic | Tamamlanmış run/journal/receipt | Terminal ve doğrulanmış olmalı |
| Semantic | Onaylı bilgi/karar | Evidence + revision + approval |
| Procedural | Aktif skill sürümü | Evaluation + verifier + approval |
| Preference | Kullanıcı/proje policy | Kullanıcı revizyonu |
| Learning candidate | Tekrarlanan problem/öneri | En az iki bağımsız gözlem + verifier |

Mem0 benzeri ürünler ileride benchmark/adaptör olarak değerlendirilebilir; Work status, authority veya exact kararların kanonik sahibi olamaz.

## 11. Knowledge Plane ve 1024 boyutlu retrieval

Context Vault aktif görevindeki yön korunur: ilk profil `openai/BAAI/bge-m3`, 1024 boyut ve cosine’dır. Proje, belge, kod, DB metadata, talep, defect, araştırma ve onaylı öğrenimler ortak provenance sözleşmesine bağlanır.

Sorgu sırası:

1. project/scope resolution;
2. status/history niyetiyse doğrudan kanonik Work Graph;
3. exact ID, defect/request no, path, symbol, DB object;
4. alias ve trigram;
5. PostgreSQL full-text;
6. BGE-M3 dense 1024;
7. Reciprocal Rank Fusion;
8. opsiyonel reranker;
9. dedupe + parent/neighbor expansion;
10. token-budgeted evidence package.

Kurallar:

- BGE-M3 query instruction zorunlu kabul edilmez; mevcut prefix’ler sürümlü A/B test ile kararlaştırılır.
- Sparse/ColBERT çıktıları gateway sağlamıyorsa varmış gibi işaretlenmez.
- Embedding kaydı `source_version + parser_profile + chunker_profile + embedding_profile + policy_digest` ile bağlanır.
- Profil değişimi kontrollü re-index üretir.
- Approximate HNSW filtreli sorguların recall’ı golden dataset ile ölçülür; iterative scan, partial index veya project partition kararı ölçümle verilir.
- Vektör yalnız retrieval evidence’tır; task status, policy veya authority olamaz.

## 12. Güvenlik ve approval modeli

### 12.1 Secret Broker

Kanonik kayıt yalnız `SecretRef` taşır. Değer:

- yerel geliştirmede OS keychain veya şifreli local store;
- kurumsal kullanımda Vault benzeri adapter;
- yalnız provider/tool boundary’de child process environment, file descriptor veya HTTP client’a;
- kısa TTL, dar scope ve mümkünse tek kullanım

ile enjekte edilir. Var olan CLI OAuth oturumunun sahibi CLI’dır; Z auth dosyasını okuyup kopyalamaz.

### 12.2 Approval sınıfları

| Sınıf | Örnek |
|---|---|
| `AUTO` | Exact read-only query, digest, rebuildable projection |
| `PREAUTHORIZED` | Sınırı ve bütçesi önceden onaylı gece read-only araştırması |
| `USER_ONCE` | Source mutation, DB write, remote sensitive data, secret kullanımı, commit/push, dış API etkisi |
| `DENY` | Source root’a direct write, secret’i prompta koyma, plansız destructive production operation |

One-shot authorization şu exact kapsamı bağlar:

```text
plan digest, effects, paths/resources, tools, network hosts,
data categories, provider/quota pool, expiry, rollback ve acceptance criteria
```

### 12.3 Sandbox

- Source root read-only.
- Her mutating attempt için detached worktree.
- Exact relative path allowlist.
- Traversal/symlink escape reddi.
- Network default-deny, explicit host allowlist.
- Yüksek riskte rootless OCI sandbox; CPU/memory/time/process limitleri ve dropped capabilities.
- Provider/CLI native permission ayarı yalnız ikinci savunmadır; core policy’yi geçersiz kılamaz.

## 13. İnsan görünümü ve veri yerleşimi

```text
Z_HOME/
  projeler/
    <proje-slug>/
      proje.md
      talepler/<id>/
      defectler/<id>/
      isler/<id>/
      arastirmalar/<id>/
      kararlar/<id>/
      raporlar/
  modeller/
    envanter/
    testler/
    raporlar/
  gelen-kutusu/
  calisma-alanlari/
  raporlar/
    gunluk/
    genel/
  yedekler/
  .z/
    runtime/
    cache/
    locks/
    secret-refs/
```

Bu klasörler kullanıcı drop-zone’u veya readable projection’dır. Kanonik gerçek PostgreSQL’dedir. Kayıtlı proje kaynakları başka fiziksel dizinlerde kalır ve Z tarafından yerinde, salt-okunur bağlanır.

## 14. Protokol kararı

- İç bounded-context çağrıları: Python typed interfaces/application ports.
- Worker request/result: sürümlü internal JSON Schema/Protobuf benzeri contract.
- MCP: dış tool/resource adapter yüzeyi için kullanılabilir; Work Graph, authority veya model router değildir.
- A2A: ilk sürüm iç subagent runtime’ı değildir. İleride bağımsız/opaque haricî ajanlarla federasyon için adapter olarak eklenebilir.
- OpenTelemetry: content-free trace/metric/log sözleşmesi; prompt ve response content varsayılan olarak kaydedilmez.

Bu ayrım, hızlı değişen protokollerin ürün çekirdeğini sahiplenmesini engeller.

## 15. Aşamalı uygulama sırası

### Faz 0 — Foundation

- temiz repository, paket sınırları, ADR’ler, CI;
- PostgreSQL + object storage compose;
- architecture/import boundary testleri;
- hiçbir gerçek model çağrısı yok.

**Kabul:** `zctl doctor`, migration upgrade/downgrade, unit tests ve architecture tests geçer.

### Faz 1 — Project + Work

- project/alias/source binding;
- capability profile iskeleti;
- Work Graph, Intent ve readable projection;
- `project add`, `work create/query`, `today`, `resume`.

**Kabul:** Chat olmadan exact defect/request bulunur; farklı model/session gerektirmeden resume edilir.

### Faz 2 — Runtime çekirdeği

- DAG, queue, lease/fencing, logical locks;
- checkpoint, claim/receipt, result envelope, verification;
- fake deterministic model adapter;
- minimum 1 subagent policy.

**Kabul:** crash/recovery, duplicate claim, stale fence ve lock-conflict testleri geçer.

### Faz 3 — Model Control Plane

- inventory, health, quarantine, benchmark, quota pool ve assignment;
- OpenCode discovery/execute adapter’ı;
- aynı contract ile Codex ve Claude adapter’ları.

**Kabul:** unavailable/quota-low model güvenli checkpoint’te değiştirilir; yüzde bilinmiyorsa uydurulmaz.

### Faz 4 — Natural language + Context

- `zctl ask`;
- alias/entity resolution;
- Context Manifest ve Continuity Packet;
- “gpu projesindeki 123 defect” exact flow.

**Kabul:** başka provider/model aynı Work/Run state’ini okuyup devam eder.

### Faz 5 — Evidence Research

- question/scope/source plan;
- en az 1 researcher subagent;
- source snapshot, claim, contradiction, citation verifier ve Türkçe rapor.

**Kabul:** kaynaksız iddia terminal başarı sayılmaz; eksik sonuç partial/blocked görünür.

### Faz 6 — Sandboxed Delivery

- Decision/Plan/one-shot authorization;
- detached worktree, tek builder, deterministic validation, bağımsız verifier;
- patch ve receipt.

**Kabul:** plan dışı path, stale source, replay, failed test ve receipt’siz mutation reddedilir.

### Faz 7 — Knowledge Plane

- Context Vault active taskının sürümlü ingestion, artifact, normalize içerik ve hybrid retrieval dilimi;
- BGE-M3 1024 + FTS + identifier + RRF + citation/eval.

**Kabul:** exact defect/symbol ve semantic soru golden setinde ölçülür; stale index fail-closed davranır.

### Faz 8 — Memory, Skills ve Scheduler

- learning candidate, memory hygiene, skill lifecycle;
- inbox, gece araştırması ve sabah raporu.

**Kabul:** otomatik self-promotion yok; her scheduled run kanıt ve receipt bırakır.

### Faz 9 — API, Dashboard ve Portability

- FastAPI read/write operation yüzeyi;
- dashboard projection’ları;
- backup/restore ve capsule export/import.

**Kabul:** dashboard kapalı olsa runtime çalışır; restore tatbikatı kanıtlıdır.

## 16. İlk yürüyen dikey dilim

İlk gerçek hedef:

```bash
zctl ask "gpu projesindeki 123 numaralı defectin kök nedenini araştır"
```

Beklenen sonuç:

1. `gpu` alias’ı tek project’e çözülür.
2. `123` exact defect olarak bulunur; semantic aramayla tahmin edilmez.
3. Work + Intent revision’ı oluşur.
4. Source revision ve capability profile bağlanır.
5. Context Manifest hazırlanır.
6. En az 1 researcher subagent atanır.
7. Source read-only kalır.
8. Child strict result envelope döndürür.
9. Koordinatör kanıtlı Türkçe rapor üretir.
10. `zctl resume` başka model/CLI ile aynı noktayı gösterir.
11. Aynı idempotency key ikinci Work/Run üretmez.
12. Secret veya fiziksel path public çıktıya sızmaz.

İkinci dikey dilim, bu rapordan exact Plan ve kullanıcı onayıyla detached worktree patch’i üretir.

## 17. Riskler, varsayımlar ve ertelenenler

### En önemli riskler

- CLI’ların kalan kota yüzdesini güvenilir ve makinece okunur biçimde sunmaması.
- Provider-native permission’lara fazla güvenme.
- PostgreSQL queue’da uzun transaction veya hatalı lease tasarımı.
- HNSW filtreli retrieval recall kaybı.
- Proje alias’ının yanlış project’e çözülmesi.
- “Self-learning” adı altında policy/skill drift.
- Secret/PII’nin ingestion veya telemetry’ye sızması.
- Faz 0–2 tamamlanmadan dashboard ve gelişmiş RAG’a yönelme.

### Doğrulanacak varsayımlar

- OpenCode gateway’in model listesi, structured output, usage ve failure sinyalleri.
- Codex/Claude CLI noninteractive JSON event şemaları ve cancellation davranışı.
- Yerel BGE-M3 endpoint’inin batch, timeout, vektör boyutu ve throughput özellikleri.
- Object storage ve PostgreSQL backup/restore hedefleri.
- Gerçek proje dosya sayısı, dil dağılımı, DB metadata hacmi ve güvenlik sınıfları.

### İlk sürümden ertelenecekler

- Mikroservis ayrıştırması.
- Graph database.
- Ayrı vector database.
- A2A tabanlı iç runtime.
- Otomatik skill/policy aktivasyonu.
- Çok modellerin aynı patch üzerinde yarışması.
- Sınırsız model tartışması.
- Sparse/ColBERT inference provider kanıtlanmadan bu modların açılması.
- Zengin “beyin sinapsı” dashboard’u.

## 18. Araştırma sonucu ve uygulama kapısı

Bu çalışma yalnız araştırma raporu değildir; yeni proje kararı üretmiştir. Uygulamaya başlamak için eşlik eden dosyalar kanonik başlangıç paketidir:

- `01_Z_CONTROL_PLANE_UYGULAMA_GOREVI.md`
- `02_ILK_DIK_EKSEN_VE_BACKLOG.md`
- `03_Z_PROJECT_MANIFEST.yaml`
- `04_BASLANGIC_ADR_KARARLARI.md`

Kodlamaya Faz 0’dan başlanmalı; eski üç repository’den source file kopyalanmamalıdır. Önce sözleşme, schema, negatif test ve acceptance fixture’ları yeniden yazılmalıdır.

## 19. Araştırma yöntemi ve doğrulama notu

- Ana prompt ve beş referans analizi karşılaştırıldı.
- Context Vault’ın pushlanmamış aktif görevi mimari yön olarak yapılmış sayıldı; mevcut kodun gerçekliğiyle karıştırılmadı.
- Güncel davranışlar resmi/primary kaynaklardan kontrol edildi.
- Bu çalışma ortamında çağrılabilir Codex, Claude veya OpenCode CLI/subagent runtime bulunmadığı için gerçek haricî subagent çalıştırılamadı. Bulgular ayrı bir “mimari karşı-kanıt ve uygulanabilirlik” doğrulama turundan geçirildi. Uygulama contract’ı agentic işlerde minimum bir gerçek subagent’ı zorunlu kılar.

## 20. Birincil teknik kaynaklar

Erişim tarihi: **20 Ağustos 2026**

- Model Context Protocol, 2026-07-28 specification release: `https://blog.modelcontextprotocol.io/posts/2026-07-28/`
- A2A latest specification: `https://a2a-protocol.org/latest/specification/`
- PostgreSQL 18 `SELECT ... SKIP LOCKED`: `https://www.postgresql.org/docs/18/sql-select.html`
- PostgreSQL asynchronous notification: `https://www.postgresql.org/docs/18/libpq-notify.html`
- pgvector README: `https://github.com/pgvector/pgvector`
- BAAI BGE-M3 model card: `https://huggingface.co/BAAI/bge-m3`
- Docling supported formats: `https://docling-project.github.io/docling/usage/supported_formats/`
- Docling Document: `https://docling-project.github.io/docling/concepts/docling_document/`
- OpenCode agents: `https://opencode.ai/docs/agents/`
- OpenCode permissions: `https://opencode.ai/docs/permissions/`
- OpenAI Codex plan usage: `https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan`
- Anthropic Claude Code plan usage: `https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan`
- Anthropic Agent SDK credit: `https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan`
- HashiCorp Vault response wrapping: `https://developer.hashicorp.com/vault/docs/concepts/response-wrapping`
