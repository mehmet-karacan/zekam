# AKTİF GÖREV — Zekam Ölçümlü Loop ve Graph Yürütme Düzlemi

## 0. Bu dosyanın kullanım ve handoff kuralı

Bu dosya önce dış görev girdisi olarak `../zekam-girdi/AKTIF_GOREV.md` konumundan okunur.
Ajan, görevi güncel kaynak kodu ve kanonik çalışma durumu ile doğruladıktan sonra ancak
problem yoksa repo kökündeki `AKTIF_GOREV.md` üzerine kontrollü olarak kopyalar.

```text
<workspace>/
├── zekam/
│   └── AKTIF_GOREV.md          ← doğrulama sonrası yaşayan aktif görev
└── zekam-girdi/
    └── AKTIF_GOREV.md          ← dışarıdan gelen görev girdisi
```

Kurallar:

1. Önce `../zekam-girdi/AKTIF_GOREV.md` okunur.
2. `git status --short --branch`, branch, HEAD, son commitler, migration head, doctor,
   lease, claim, checkpoint, receipt, recovery ve kanonik Work durumu doğrulanır.
3. Önceki aktif görev gerçekten kapanmadan bu görev kök görevin üzerine yazılmaz.
4. Görev güncel HEAD, mimari sözleşmeler, güvenlik politikaları veya kanonik Work state ile
   çelişiyorsa körlemesine kopyalanmaz; problem raporlanır ve mevcut kök görev korunur.
5. Problem yoksa dış görev dosyası repo kökündeki `AKTIF_GOREV.md` üzerine kopyalanır.
6. Kopyalamadan sonra kökteki `AKTIF_GOREV.md` yaşayan ve güncel aktif görev kaydıdır.
7. `zekam-girdi` yalnız giriş/handoff alanıdır; ilerleme oradaki dosyada tutulmaz.
8. `AKTIF_GOREV.yaml` veya kanonik PostgreSQL Work state ile drift oluşturulmaz. Markdown
   hiçbir zaman Work/Plan/Run/Authorization otoritesi değildir.
9. Kullanıcıya ait alakasız değişiklikler korunur; `stash`, `reset --hard`, `clean`, history
   rewrite veya toplu geri alma yapılmaz.
10. Kullanıcı açıkça istemedikçe commit, push, PR, merge veya GitHub workflow tetikleyici
    değişikliği yapılmaz.

---

## 1. Görev kimliği ve doğrulanmış başlangıç durumu

| Alan | Değer |
|---|---|
| Görev | Zekam Ölçümlü Loop ve Graph Yürütme Düzlemi |
| Durum | Uygulamaya hazır görev spesifikasyonu |
| Öncelik | P0 + P1; P2 yalnız ölçümle gerekirse |
| Analiz tarihi | 2026-08-28 |
| İncelenen GitHub `main` HEAD | `be5e1a7390f759577d670095c20706804d2077c7` |
| Önceki görev başlangıç HEAD’i | `0be98185f4393e0f56db46164939b9873214123e` |
| Önceki başlangıçtan sonraki commit | 8 |
| Git’te görülen en yeni migration | `0065_projection_close_checkpoint_v2_compat` |
| Kanonik otorite | PostgreSQL Work/Runtime/Memory/Model tabloları |
| Loop runtime temeli | `runtime.loop_*` + `BoundedLoopExecutor` |
| Graph temeli | `TaskPlan` DAG + `RoutePlanner` + `AgentGraphRoot` |
| Ölçüm temeli | `learning.evaluate_loop` + model/context experiment altyapısı |
| CI | Yalnız manuel `workflow_dispatch`; otomatik PR/push açılmayacak |
| Uzak model çağrıları | Varsayılan kapalı |
| GitHub yazma | Ayrı açık kullanıcı yetkisi olmadan yasak |

### 1.1 Önceki görevin Git kaynak kodu doğrulaması

Önceki “Memory Learning Loop ve Obsidian Projection” görevinin kaynak kodu tarafında
önemli teslimatlar gerçekten eklenmiştir:

- lifecycle event → candidate compiler orchestration,
- durable compiler watermark/job yolu,
- Codex ikinci lifecycle harness’i,
- OpenCode/Codex cross-harness continuity,
- private/public güvenlik filtreli Obsidian projection,
- projection-aware close ve release kapıları,
- high-risk autofill preview/fill/submit guard,
- closure/doctor uyumluluk düzeltmeleri,
- migration `0057`–`0065` aralığı,
- Codex/Claude/OpenCode için idempotent managed client instruction bootstrap,
- immutable Obsidian generation'dan watcher-friendly sabit `GUNCEL_BELLEK` kasası.

Son `main` commit'i bu iki kullanıcı-deneyimi katmanını eklemiştir. Commit risk notuna
göre lifecycle runtime bootstrap ve otomatik projection publish hâlâ sonraki güvenli adım
olarak görülmektedir. Bu görev mevcut bootstrap/store kodunu yeniden yazmayacak; gereken
yerde receipt-bound runtime composition ve ölçümlü projection görünümü olarak genişletecektir.

Bu görev, tamamlanmış parçaları yeniden yazmayacaktır.

### 1.2 Önceki görevin kapanışında görülen kanonik drift

GitHub’daki kaynak kodu güncel olsa da repo kökündeki `AKTIF_GOREV.yaml` hâlâ daha eski:

- source HEAD `b8d970c...`,
- migration head `55`,
- eski Memory Continuity Work kimliği,
- eski projection digest ve next-safe-action

bilgilerini taşımaktadır. Git yalnız kaynak kodunu gösterir; yerel PostgreSQL’in gerçekten
`0065`’e yükseltildiğini, yeni Work/Run/receipt zincirinin kapandığını veya projection’ın
yeniden üretildiğini kanıtlamaz.

**Bu nedenle bu yeni görevin ilk zorunlu kapısı şudur:**

```text
önceki source implementation
+ güncel migration head
+ kanonik Work/Plan/Run/receipt
+ root Markdown/YAML projection
+ doctor/close sonucu
```

birbiriyle uzlaştırılmadan yeni görev kanonik aktif iş olarak başlatılamaz.

### 1.3 Yeniden tabanlama kuralı

Uygulama başladığında HEAD yukarıdaki SHA’dan ilerideyse görev iptal edilmez. Ajan:

1. Güncel HEAD ve migration head’i kaydeder.
2. Bu dosyadaki her açığı güncel kod üzerinde yeniden doğrular.
3. Artık çözülmüş maddeleri tekrar uygulamaz.
4. Yeni çakışma veya kapsam farkını görünür `baseline-drift` kanıtı olarak kaydeder.
5. Önerilen dosya/migration numaralarını güncel HEAD’e göre yeniden sıralar.

---

## 2. Misyon

Zekam’ın mevcut bounded-loop, learning, benchmark, Work DAG, route planner, worker,
checkpoint, claim/receipt ve context continuity parçalarını tek bir kanıtlanabilir
**ölçümlü yürütme düzleminde** birleştir.

Hedef daha fazla ajan veya daha uzun `while true` çalıştırmak değildir. Hedef:

```text
ölçülebilir hedef
→ uygun yürütme biçimini seç
→ bir tur/graph dalgası çalıştır
→ dış ve sabit ölçümle sonucu değerlendir
→ yalnız anlamlı yeni bilgi ve ilerlemeyi taşı
→ tekrar, durgunluk, metrik kandırma ve riskte dur
→ kanıtlı sonucu kabul et veya geri al
```

döngüsünü kurmaktır.

Temel ayrım:

```text
Loop  = aynı kanonik artifact/hedef üzerinde ölçümlü iyileştirme turları
Graph = farklı işleri gerçek dependency ve typed artifact akışıyla doğru sırada yürütme
```

Bir graph node’u kendi içinde bounded loop olabilir. Loop ve graph birbirinin alternatifi
olmak zorunda değildir; farklı sorunları çözerler.

---

## 3. Kaynak altyazıdan alınan ve Zekam’a uyarlanan ilkeler

Kaynak metnin ana tezi şudur: ölçüm, tur arası durum aktarımı ve durma şartı yoksa loop
yoktur; yalnız pahalı tekrar vardır.

### 3.1 Aynen korunacak ilkeler

1. **Her tur model için yeni bir hayattır.** İlerlemeyi modelin hafızasına değil, dışarıdaki
   kanonik checkpoint/progress packet taşır.
2. **Aynı işi tekrar çağırmak loop değildir.** Yeni tur; yeni kanıt, yeni hipotez veya
   ölçülmüş ilerleme taşımak zorundadır.
3. **Ölçüm yoksa loop yoktur.** “İlerliyorum” diyen model beyanı ölçüm sayılmaz.
4. **Geçti/kaldı tek başına yön göstermez.** Mümkün olduğunda sayı, yön, delta ve hangi
   boyutta iyileşme/regresyon olduğu taşınır.
5. **Ölçmek yapmaktan ucuz ve güvenilir olmalıdır.** Aksi durumda önce ölçüm altyapısı
   iyileştirilir; loop başlatılmaz.
6. **Üreten ile ölçen ayrılır.** Builder kendi çıktısını tek başına puanlayamaz.
7. **Validator’ın sınavı sabitlenir.** Builder test, fixture, metric code veya threshold’u
   kolaylaştırarak başarı üretemez.
8. **Ham geçmiş taşınmaz.** Başarısız denemenin tam çıktısı değil, kısa sonuç, kök neden,
   metric delta, patch/evidence digest ve sonraki odak taşınır.
9. **Graph yalnız gerçek decomposition varsa kurulur.** Tek artifact için gereksiz graph,
   yeni nesil mikroservis karmaşıklığıdır.
10. **Paralellik ajan sayısı değildir.** Gerçekten aynı anda çalışabilen bağımsız node’ların
    zaman aralıkları ve kaynak çakışmasızlığı kanıtlanır.
11. **Yaratıcı çeşitlilik loop değildir.** Birden fazla bağımsız aday üretip seçmek
    `tournament` biçimidir; aynı fikri kendi kendine cilalamak değildir.
12. **Geri alınamaz effect loop gövdesi olmaz.** Deploy, migration apply, para/kripto,
    dış mesaj, submit ve benzeri işler queue + exact authorization + insan gate ister.
13. **Durma şartı zorunludur.** Hedef, attempt, token, cost, deadline, plateau, tekrar,
    oscillation, risk veya manual-review sınırlarından biri loop’u kapatır.
14. **No-op en kolay kazanç olamaz.** Hiçbir şey değiştirmemek ilerleme sayılmaz.
15. **Scaffolding kalıcı dogma değildir.** Özet, ikinci kontrol, fallback veya ek prompt
    katmanı paired ablation ile değer üretmiyorsa deprecation adayı olur.
16. **Model + program birlikte çalışır.** Program sıra, bütçe, veri akışı ve sınırı zorlar;
    model bounded karar/üretim yapar.

### 3.2 Aynen alınmayacak veya sınırlandırılacak noktalar

- Çok sayıda loop’u yalnız token bol diye sınırsız başlatmak.
- Token/maliyet bütçesini abonelik hissiyatına göre varsaymak.
- Modelin kendi yazdığı testi kendi başarısının tek ölçütü yapmak.
- Aynı model veya aynı execution identity’yi builder ve verifier olarak kullanmak.
- Graph/loop seçimini yalnız “iş zor” sezgisine bırakmak.
- Creative output’ta modelin kendi önceki adayını tekrar tekrar savunmasına izin vermek.
- Başarısız attempt’in tam transcript/log’unu sonraki prompta yığmak.
- Geri alma için kullanıcı değişikliklerini silmek veya blanket `git reset` kullanmak.
- Riskli effect’i loop tekrar politikasına sokmak.
- Metrik yükseldi diye güvenlik, doğruluk veya reliability hard guard’ını düşürmek.
- Loop/graph altyapısının kendi policy veya validator sınırlarını otomatik gevşetmesi.

---

## 4. Güncel Zekam teşhisi

### 4.1 Güçlü ve korunması gereken mevcut loop temeli

Mevcut `LoopPolicy` ve PostgreSQL runtime aşağıdakileri zaten zorlamaktadır:

- exact Work/Plan/Step/Assignment/Context binding,
- ayrı validator assignment,
- max attempt, token, cost ve deadline,
- required evidence delta,
- forbidden effect sınıfları,
- exact predecessor attempt,
- aynı semantic request için yeni kanıt olmadan tekrar reddi,
- result ve validator receipt zorunluluğu,
- effect varsa claim + terminal receipt,
- exception sonrası sessiz retry yerine manual-review,
- append-only attempt/checkpoint/terminal ledger.

Bunlar korunacak; yeni bir paralel “loop engine” yazılmayacaktır.

### 4.2 Güçlü ve korunması gereken graph temeli

Mevcut yapı:

- `TaskPlan.steps[*].depends_on` ile kanonik DAG,
- cycle reddi,
- ready-step hesabı,
- logical resource conflict kontrolü,
- worker/quota/token/cost/provider/policy kapasitesinin minimumuna göre paralellik,
- recovery önceliği,
- `direct/single/sequential/parallel/blocked/recovery` route kararları,
- AgentGraph root/spawn/message kayıtları,
- queue/lease/fencing/claim/receipt/recovery

özelliklerine sahiptir.

Yeni graph düzlemi ikinci bir dependency store kurmayacak; TaskPlan ve RoutePlanner üzerine
ölçümlü topology selection ve graph execution evidence ekleyecektir.

### 4.3 Mevcut ölçüm parçaları

Zekam’da iki değerli fakat ayrı kalan ölçüm hattı vardır:

1. `learning.evaluate_loop`
   - iteration score,
   - cost,
   - verified flag,
   - goal,
   - stall limit,
   - no-progress stop.

2. Model/context experiment altyapısı
   - paired trial,
   - minimum tekrar,
   - quality/reliability/latency/token/cost,
   - independent verifier,
   - no-regression gates,
   - ContextAblationProfile,
   - baseline/candidate karşılaştırması.

Bu iki yapı canonical runtime loop ile birleştirilmeli; üçüncü bir ölçüm modeli eklenmemelidir.

### 4.4 Kapanması gereken gerçek açıklar

#### A-01 — Runtime loop yalnız terminal outcome görüyor, yönlü metric vector görmüyor

Canonical loop sonucu `retryable-failure/passed/blocked/manual-review` ve kullanım
sayılarını saklıyor. Neden ilerlediği, hangi metriğin ne kadar değiştiği, hangi guard’ın
regrese olduğu ve marjinal değerin maliyete oranı canonical checkpoint’te yoktur.

#### A-02 — Learning score döngüsü ile runtime loop iki ayrı dünya

`learning.evaluate_loop` score/stall bilir; `runtime.loop_*` ise bu kararı kullanmaz.
Aynı ürün içinde iki farklı loop semantiği uzun vadede drift üretir.

#### A-03 — Yeni evidence retry admission sağlar ama model-visible ilerleme paketine bağlı değildir

Bir evidence ID’nin kayıtlı olması, sonraki model çağrısının bu kanıttan türetilmiş kısa
sonuç, metric delta ve sonraki odak bilgisini gerçekten gördüğünü kanıtlamaz.

#### A-04 — Semantic request digest prompt yeniden yazılarak aşılabilir

Mevcut digest prompt/context/action’a bağlıdır. Aynı hipotez farklı cümleyle yazılırsa,
aynı problem tekrar yaşanabilir. Stable objective, artifact, hypothesis, patch ve failure
signature kimlikleri eksiktir.

#### A-05 — Graph mı loop mu direct mi kararı için suitability gate yok

RoutePlanner hazır planı nasıl çalıştıracağını seçer; fakat planın baştan graph olması
gerekip gerekmediğini, creative tournament mı, bounded loop mu, queue/human gate mi
olacağını ölçümlü biçimde seçmez.

#### A-06 — Graph koordinasyon maliyeti ölçülmüyor

Edge sayısı, mesaj/token overhead’i, dependency wait, critical path, gerçek overlap,
parallel efficiency ve fan-in maliyeti route kararına geri beslenmiyor.

#### A-07 — Validator spec digest var; validator asset freeze eksik

Test/eval fixture’ları, metric implementation, threshold ve ilgili dosyaların tam content
manifest’i builder’ın yazma scope’undan bağımsız biçimde dondurulmalıdır.

#### A-08 — Tournament ayrı bir first-class pattern değil

Creative çeşitlilik üretip bağımsız seçme akışı loop/graph içine yanlış biçimde
sıkıştırılabilir.

#### A-09 — Forbidden effect policy configurable; global fail-closed invariant yeterince güçlü değil

Deploy, migration apply, external message ve benzeri effect’lerin loop body’ye yanlış
policy ile alınması kesin olarak engellenmelidir.

#### A-10 — Tek attempt executor var; durable multi-attempt orchestration üretim yolu net değil

`BoundedLoopExecutor` tek admitted attempt’i effect → validator → ledger sırasıyla kapatır.
Bir sonraki attempt’in durable job olarak yalnız ölçüm kararından sonra enqueue edilmesi,
crash sonrası devamı, pause/cancel ve progress hydration için tek production orchestration
yolu gereklidir.

#### A-11 — Scaffolding ablation mevcut experiment altyapısına bağlanmamış

Model/context ablation vardır; ancak loop summary, retry hint, second checker, fallback,
extra critic veya graph coordinator katmanlarının değerini düzenli ölçen ürün yolu yoktur.

#### A-12 — Önceki aktif görev projection’ı kanonik olarak uzlaştırılmamış görünüyor

Yeni iş başlamadan source implementation ile root Markdown/YAML projection ve DB Work
state eşleştirilmelidir.

#### A-13 — Sabit `GUNCEL_BELLEK` yolu var; otomatik runtime publish bağı ayrıca kanıtlanmalı

Projection store doğrulanmış immutable generation'ı sabit `GUNCEL_BELLEK` dizinine
materialize edebilmektedir. Yeni loop/graph görünümü ikinci bir vault/store veya yeni bir
"current" pointer tasarlamamalıdır. Bunun yerine mevcut stable-vault yolunu kullanmalı;
projection build → publish → verification → receipt zincirinin runtime/close akışında ne
zaman otomatik çalışacağı exact policy ve idempotency ile bağlanmalıdır.

---

## 5. Değiştirilemez mimari kararlar

### K-01 — Yeni bir paralel loop motoru kurulmayacak

`LoopPolicy`, `PostgresLoopPolicyRepository`, `BoundedLoopExecutor`, `learning.evaluate_loop`
ve model/context experiment ortak generic ölçüm sözleşmesine bağlanacaktır.

### K-02 — Model beyanı progress değildir

Producer modelin “ilerledim”, “düzelttim” veya “buldum” ifadesi ölçüm kaynağı olamaz.
Progress yalnız dış evidence ile üretilir.

Ölçüm güven sırası:

```text
1. deterministic test / static analysis / benchmark / runtime telemetry
2. immutable rule-based evaluator
3. bağımsız model verifier
4. insan review
5. producer self-report → yalnız observation, progress değil
```

### K-03 — Tek scalar skor zorunlu ve yeterli değildir

Bir objective birden fazla metric taşır. Her metric:

- `maximize`,
- `minimize`,
- `target`,
- `range`

yönlerinden birine ve `primary`, `hard-guard`, `secondary`, `cost` rollerinden birine
sahiptir.

Progress için:

- bütün hard guard’lar tolerance içinde kalmalı,
- en az bir primary metric minimum anlamlı delta kadar iyileşmeli veya hedefe ulaşmalı,
- invalid/NaN/eksik ölçüm progress sayılmamalı,
- scalar “value” yalnız sıralama/UI yardımı olabilir; tek kabul kapısı olamaz.

### K-04 — Yeni evidence ile ölçülmüş ilerleme farklı şeylerdir

Yeni diagnosis veya araştırma evidence’i retry admission sağlayabilir; fakat metric
iyileşmesi sayılmaz. Diagnostic turlar sınırlı patience kullanır.

### K-05 — Attempt 2+ progress packet olmadan başlayamaz

Her yeni attempt, predecessor outcome’dan deterministik üretilmiş bounded
`LoopProgressPacket` taşır. Tam transcript veya bütün geçmiş taşınmaz.

### K-06 — Rephrase novelty değildir

Prompt cümlesi değişse bile aynı:

- objective,
- artifact,
- hypothesis,
- patch,
- failure signature,
- validator diagnosis

tekrarı canonical fingerprint ile yakalanır.

### K-07 — Topology policy ile seçilir

Desteklenen first-class pattern’ler:

```text
direct
single-pass
tournament
bounded-loop
graph
queue-human-review
blocked
```

### K-08 — Graph kanonik TaskPlan DAG’dır

Yeni graph store, yeni dependency gerçeği veya TaskPlan’a paralel plan modeli kurulmaz.
AgentGraph runtime spawn/message kanıtıdır; Work/TaskPlan otoritesinin yerine geçmez.

### K-09 — Loop graph node’u olabilir

Graph node execution mode `direct`, `bounded-loop`, `tournament` veya `human-gate`
olabilir. Bütün graph’ı baştan sona tek loop olarak tekrar çalıştırmak varsayılan değildir.

### K-10 — Geri alınamaz effect loop body olamaz

Aşağıdakiler fail-closed biçimde `queue-human-review` ister:

- deploy/release,
- migration apply,
- git push/merge,
- dış mesaj/mail gönderimi,
- form submit,
- para/kripto/ödeme,
- imza, CAPTCHA, MFA/OTP,
- destructive delete,
- policy/authorization gevşetme.

### K-11 — Validator asset’leri immutable binding ister

Validator assignment yetmez. Test/eval/fixture/metric/threshold dosyaları content digest
manifest’iyle bağlanır. Builder aynı loop policy altında bunları değiştiremez.

### K-12 — Bir worker handler içinde `while true` yok

Her attempt ayrı durable job/attempt/claim/receipt zinciridir. Sonraki attempt yalnız
validator ve progress decision’dan sonra enqueue edilir.

### K-13 — Ham geçmiş değil, bounded state taşınır

Progress packet token budget’i aşamaz; kaynaklar digest/reference olarak taşınır.

### K-14 — No-op ve metric gaming başarı değildir

Artifact/patch değişmemiş ve hedefe zaten ulaşılmamışsa progress yoktur. Testi veya
threshold’u kolaylaştırmak invalidates evaluation.

### K-15 — Scaffolding yalnız kanıtla kalır

Ablation baseline’i geçmeyen ek katman otomatik silinmez; deprecation candidate üretir ve
review ister.

### K-16 — CI ve GitHub politikası değişmeyecek

GitHub Actions `workflow_dispatch` olarak manuel kalır. Kullanıcı açıkça istemedikçe:

- `pull_request`/`push` trigger eklenmez,
- workflow çalıştırılmaz,
- commit/push/PR oluşturulmaz.

Yerel kalite, PostgreSQL ve bağımsız verifier kapıları yine zorunludur.

### K-17 — Mevcut client bootstrap ve `GUNCEL_BELLEK` yeniden kullanılacak

Global Codex/Claude/OpenCode managed instruction bootstrap ile Obsidian stable-vault
materialization mevcut ortak giriş ve insan-görünüm katmanıdır. Loop/graph görevi yeni
client talimat dosyaları, yeni vault kökü veya alternatif current-pointer üretmez. Gerekli
genişletmeler mevcut managed section ve projection receipt sözleşmesi üzerinden additive
yapılır.

---

## 6. Hedef mimari

```text
User / Work Intent
        │
        ▼
Objective + Measurement Contract
        │
        ▼
Topology Suitability Gate
        │
        ├── direct / single-pass
        ├── tournament
        ├── bounded-loop
        ├── graph
        └── queue-human-review

Bounded loop node:

Objective + baseline
        │
        ▼
Durable attempt job
        │
        ▼
Builder effect (bounded/reversible)
        │
        ▼
Frozen external measurement
        │
        ▼
Metric vector + verifier receipt
        │
        ▼
Progress decision
        │
        ├── target reached → passed
        ├── meaningful progress → next progress packet + enqueue
        ├── diagnostic delta → bounded diagnostic retry
        ├── plateau/repeat/oscillation → blocked/manual review
        └── budget/risk → terminal

Graph:

TaskPlan DAG
  ├── node A [direct]
  ├── node B [bounded-loop]
  ├── node C [tournament]
  └── fan-in verifier/human gate
        │
        ▼
Graph execution receipt
(critical path, overlap, wait, cost, result evidence)
```

---

## 7. Yeni veya genişletilecek domain sözleşmeleri

### 7.1 `OptimizationObjective`

Zorunlu alanlar:

```text
objective_id
realm_id
project_id
work_item_id
plan_id
step_id
artifact_ref
artifact_baseline_digest
measurement_plan_digest
validator_asset_manifest_digest
metric_specs[]
max_attempts
max_tokens
max_cost_micros
deadline
reversibility_class
created_at
objective_digest
grants_authority=false
```

Objective prompt metnine değil, kanonik iş/artifact/measurement kimliğine bağlıdır.

### 7.2 `MetricSpec`

```text
metric_id
name
unit
direction: maximize|minimize|target|range
role: primary|hard-guard|secondary|cost
source_kind
target_value|min_value|max_value
minimum_meaningful_delta
regression_tolerance
aggregation
spec_digest
```

### 7.3 `MeasurementEvidence`

```text
metric_id
value
evidence_ref
evidence_digest
source_revision
measured_at
measurement_identity
verifier_identity
producer_self_report=false
```

Secret veya raw output yerine değer + bounded metadata + digest saklanır.

### 7.4 `ProgressVector`

```text
baseline_values
previous_values
current_values
deltas
hard_guard_results
primary_progress_results
target_results
value_per_cost
progress_state: improved|target-reached|plateau|regressed|invalid
progress_digest
```

### 7.5 `LoopProgressPacket`

Attempt 2+ için MUST context parçasıdır:

```text
objective_digest
artifact_before_digest
artifact_after_digest
predecessor_attempt_id
attempt_ordinal
previous_metric_vector
current_metric_vector
metric_deltas
accepted_hypothesis_digest
rejected_hypothesis_digests[]
patch_digest
failure_signature
validator_diagnosis_ref/digest
new_evidence_refs[]
remaining_attempt/token/cost/time budget
next_allowed_focus
forbidden_retries[]
packet_digest
grants_authority=false
```

Packet yalnız gereken kısa metni taşır; full log/transcript/response taşımaz.

### 7.6 `AttemptNoveltyFingerprint`

```text
objective_digest
artifact_digest
hypothesis_digest
patch_digest
failure_signature
action_semantics_digest
novelty_digest
```

Aynı objective altında prompt rephrase ile aynı fingerprint tekrar kullanılamaz.

### 7.7 `LoopSuitabilityAssessment`

```text
measurement_available
measurement_source_tier
measurement_estimated_cost
action_estimated_cost
measurement_to_action_ratio
reversible
idempotent_or_receipt_bound
creative_diversity_goal
human_judgment_required
distinct_deliverable_count
dependency_edge_count
expected_coordination_cost
recommended_pattern
reason_codes[]
assessment_digest
```

Bilinmeyen kritik alan varsa loop/graph varsayılmaz; `single-pass`, `blocked` veya
`queue-human-review` seçilir.

### 7.8 `ExecutionTopologyDecision`

```text
pattern
objective_digest
plan_digest
node_modes
parallelism_ceiling
estimated_calls
estimated_tokens
estimated_cost
estimated_coordination_overhead
required_human_gates
reason_codes
decision_digest
grants_authority=false
```

### 7.9 `ValidatorAssetManifest`

```text
validator_spec_digest
fixture_refs + content_digests
metric_code_refs + content_digests
threshold_policy_digest
allowed_read_resources
forbidden_builder_write_resources
source_revision
manifest_digest
```

Validator asset değişikliği yeni plan/policy revision gerektirir; mevcut attempt içinde
sessizce değiştirilemez.

### 7.10 `GraphExecutionReceipt`

```text
graph_root_id
plan_digest
node_receipts[]
edge_wait_durations
critical_path
max_observed_concurrency
parallel_overlap_duration
parallel_efficiency
coordination_input/output_tokens
coordination_cost
fan_in_result_digest
terminal_state
receipt_digest
```

### 7.11 `TournamentPlan`

```text
candidate_count
candidate_assignments[]
shared_objective_digest
candidate_context_digest
candidate_isolation=true
selector_assignment_id
selector_spec_digest
human_final_gate
budget
plan_digest
```

Adaylar birbirlerinin üretimini görmez; selector builder’larla aynı kimlik değildir.

---

## 8. Yürütme biçimi seçim tablosu

| Durum | Seçim | Gerekçe |
|---|---|---|
| Tek işlem, açık kabul, retry gereksiz | `direct` | En düşük overhead |
| Tek bounded üretim, bir kez çalıştırma | `single-pass` | Loop maliyeti gereksiz |
| Aynı artifact, objective ölçüm ucuz, effect reversible | `bounded-loop` | Ölçümlü iterasyon anlamlı |
| Yaratıcı/çeşitli aday üretimi ve sonradan seçim | `tournament` | Self-polishing yerine çeşitlilik |
| En az iki farklı deliverable/dependency/fan-in | `graph` | Gerçek veri ve sıra bağımlılığı |
| Geri alınamaz/high-risk effect | `queue-human-review` | Loop retry yasak |
| Ölçüm yok veya ölçüm yapmaktan pahalı | `single-pass` / `blocked` | Önce ölçüm altyapısı |
| Tek artifact için yalnız ajan sayısı artırma | Graph reddi | Pahalı kopya üretimi |

Graph admission için en az biri kanıtlanmalıdır:

- farklı typed output üreten en az iki node,
- gerçek dependency edge,
- anlamlı parallel-ready bağımsız iş,
- ayrı doğrulama/fan-in ihtiyacı.

Yalnız “iş zor” veya “çok ajan daha iyi olabilir” gerekçesi graph için yeterli değildir.

---

## 9. Work package’ları

## WP-00 — Önceki görevi kanonik olarak uzlaştır

### Amaç

Önceki source implementation ile DB/Work/projection durumunu eşleştir.

### Yapılacaklar

1. Güncel branch/HEAD ve migration head’i doğrula.
2. Local DB current revision’ı oku; Git’teki migration head ile karşılaştır.
3. Önceki Memory Learning/Obsidian task’ına ait Work/Plan/Run/step/checkpoint/receipt’i bul.
4. Açık claim, stale lease, pending/recovery job veya incomplete close varsa önce recovery yap.
5. Root `AKTIF_GOREV.yaml` ve generated Markdown projection’ı kanonik state’ten yeniden üret.
6. Previous source implementation gerçekten kabul kapılarını geçmediyse yeni task’a geçme.
7. Önceki task kapanış receipt’ini kaydet; sonra bu dosyayı kök aktif görev yap.

### Kabul

- Root YAML güncel source/migration/DB digest taşır.
- `completed` ile pending next action çelişmez.
- Git’teki commit varlığı tek başına tamamlanma kanıtı sayılmaz.

---

## WP-01 — Generic ölçüm ve objective çekirdeğini birleştir

### Amaç

`learning.evaluate_loop`, runtime LoopPolicy ve model/context experiment metriklerini ortak
bir domain çekirdeğine bağla.

### Yapılacaklar

1. Generic metric direction/role/evidence/progress sözleşmelerini oluştur.
2. `learning.IterationOutcome/LoopDecision` davranışını backward-compatible adapter ile bu
   çekirdeğe taşı.
3. Model/context experiment quality/reliability/latency/token/cost metric’lerini aynı generic
   yapıyı kullanacak şekilde bağla.
4. Runtime loop v1’i bozma; additive v2 schema/model ekle.
5. Binary test objective için `0→1` geçişini yönlü metric olarak destekle.
6. Hard guard regresyonunu scalar skor yükselse bile reddet.

### Kabul

- Üç ayrı loop/experiment değerlendirme semantiği kalmaz.
- Eski testler adapter üzerinden geçmeye devam eder.
- NaN/Inf/eksik ölçüm fail-closed olur.

---

## WP-02 — Measured LoopPolicy v2 ve progress checkpoint

### Amaç

Her canonical loop attempt sonucunu metric vector ve progress decision ile kapat.

### Yapılacaklar

1. Loop policy’yi objective/measurement/validator asset digest’leriyle bağla.
2. `LoopValidation` v2’ye metric evidence refs, vector digest ve progress state ekle.
3. `runtime.loop_attempt_outcome` ve `runtime.loop_checkpoint` için additive v2 tablolar veya
   revision-safe kolonlar ekle.
4. `complete_loop_attempt_v2` result + verifier + metric evidence + effect receipt’i atomik
   doğrulasın.
5. `retryable-failure` tek başına devam kararı vermesin; progress evaluator karar versin.
6. New diagnosis retry hakkı ile measured improvement’ı ayrı tut.

### Kabul

- Her attempt’in “neden devam/dur” cevabı metric/evidence ile açıklanır.
- Producer self-report progress source olarak kabul edilmez.

---

## WP-03 — Deterministik `LoopProgressPacket` ve bounded hydration

### Amaç

Turlar arasında bütün geçmişi değil, gerekli state’i güvenli biçimde taşı.

### Yapılacaklar

1. Predecessor checkpoint’ten deterministic packet compiler yaz.
2. Attempt 2+ context recipe’sinde packet’i MUST yap.
3. Packet token boyutunu policy ile sınırla.
4. Full logs, raw transcript, complete patch ve önceki response’ları context’e alma.
5. Kaynaklar reference + digest olarak taşınsın.
6. Packet `next_allowed_focus` ve `forbidden_retries` taşısın.
7. Packet source/plan/policy/validator drift’inde stale sayılsın.

### Kabul

- Aynı model yeni turda predecessor metric/failure/next focus bilgisini gerçekten görür.
- Packet olmadan ikinci attempt admission reddedilir.
- Context büyüklüğü attempt sayısıyla lineer olarak şişmez.

---

## WP-04 — Rephrase-proof tekrar, stagnation ve oscillation kapıları

### Amaç

Aynı gecenin üç kez yaşanmasını prompt değiştirerek aşılması mümkün olmayacak şekilde
engelle.

### Yapılacaklar

1. Stable objective/artifact/hypothesis/patch/failure fingerprint üret.
2. Aynı hypothesis + aynı failure signature tekrarını reddet.
3. Aynı patch digest’i yeniden uygulamayı reddet.
4. Diagnostic evidence için sınırlı patience tanımla.
5. Plateau, regression ve A↔B oscillation detector ekle.
6. No-op attempt’i progress sayma.
7. Repeated hypothesis terminal reason’larını first-class yap.

### Yeni stop reason adayları

```text
no-progress
repeated-hypothesis
repeated-patch
repeated-failure-signature
oscillation
metric-regression
invalid-measurement
validator-drift
risk-escalation
```

### Kabul

- Prompt rephrase duplicate guard’ı aşamaz.
- İki farklı UUID aynı semantik giriş için novelty üretmez.
- Plateau policy limitinde loop otomatik ve kanıtlı durur.

---

## WP-05 — Validator asset freeze ve reward-hacking savunması

### Amaç

Builder’ın sınavı kolaylaştırarak loop’u geçmesini engelle.

### Yapılacaklar

1. Test/eval fixture/metric/threshold dosyalarından immutable manifest üret.
2. Builder logical write resources bu manifestteki asset’leri kapsayamaz.
3. TDD gerekiyorsa ayrı `test-author` veya verifier phase’i testleri önce üretip dondursun.
4. Test/fixture değişikliği yeni Plan/Validator revision gerektirsin.
5. Metric code digest değişirse mevcut attempt invalid olsun.
6. Builder/verifier model ve execution identity ayrımı yüksek riskte zorunlu kalsın.
7. No-op veya threshold loosening için negatif test ekle.

### Kabul

- Builder testi kolaylaştırarak passed üretemez.
- Validator asset drift attempt’i kapatır ve replan ister.

---

## WP-06 — Durable multi-attempt loop orchestrator

### Amaç

Tek attempt executor’ı mevcut worker/queue üzerinde crash-safe uzun loop’a dönüştür.

### Yapılacaklar

1. Yeni paralel daemon kurma; mevcut worker composition’a explicit loop-attempt handler ekle.
2. Her queue job tam bir attempt çalıştırır; handler içinde sonsuz döngü olmaz.
3. Sıra:

```text
admit
→ bind dispatch
→ builder effect
→ canonical result receipt
→ frozen measurement
→ verifier receipt
→ progress evaluation
→ checkpoint
→ terminal veya next-attempt enqueue
```

4. Next attempt enqueue aynı transaction/outbox/idempotency sözleşmesine bağlı olsun.
5. Crash after claim/effect, receipt replay ve recovery-required senaryolarını uygula.
6. Pause/cancel/drain sonrası yeni attempt açma.
7. Remaining quota/token/cost/deadline her attempt öncesi yeniden doğrulansın.
8. Remote model route yalnız mevcut provider policy ve exact authorization ile çalışsın.

### Kabul

- Worker yeniden başlasa loop duplicate attempt üretmez.
- Claim + no receipt sessiz retry olmaz.
- Terminal loop yeniden enqueue edilmez.

---

## WP-07 — Execution topology suitability planner

### Amaç

Plan yürütülmeden önce direct/loop/tournament/graph/queue-human-review seçimini kanıtlı yap.

### Yapılacaklar

1. `LoopSuitabilityAssessment` üret.
2. Measurement/action cost ratio ve measurement source tier hesapla.
3. Reversibility ve high-risk effect gate uygula.
4. Distinct deliverable, dependency ve expected coordination overhead ölç.
5. Creative diversity goal’da tournament seç.
6. Topology decision authority vermesin; TaskPlan revision’a bağlansın.
7. Ambiguous/unknown critical field’de fail-closed davran.

### Kabul

- Tek artifact ve measurable objective için gereksiz graph reddedilir.
- Ölçümü olmayan iş loop’a alınmaz.
- Geri alınamaz iş human gate’e gider.

---

## WP-08 — Mevcut TaskPlan/RoutePlanner’a graph execution evidence ekle

### Amaç

Graph’ın gerçekten paralel ve değerli olduğunu dışarıdan ölç.

### Yapılacaklar

1. TaskPlan dependency gerçeğini değiştirme.
2. Node execution mode metadata’sını additive olarak ekle.
3. Ready/independent node’ların actual start/end interval’larını kaydet.
4. Critical path, dependency wait ve resource wait hesapla.
5. Max observed concurrency ve overlap duration üret.
6. Coordination token/cost/message sayısını ölç.
7. Fan-in failure hiçbir child hatasını yutmasın.
8. Graph overhead beklenen değeri aşıyorsa future topology feedback üret.

### Paralellik tanımı

```text
parallelism = aynı zaman aralığında active olan,
              dependency’si tamamlanmış,
              logical-resource conflict taşımayan node sayısı
```

Ajan sayısı tek başına paralellik değildir.

### Kabul

- “30 ajan vardı” değil, gerçek overlap/critical-path kanıtı raporlanır.
- Sequential graph yanlışlıkla parallel raporlanmaz.

---

## WP-09 — Tournament pattern

### Amaç

Creative veya alternatif üretim işlerini yanlış iterative loop’tan ayır.

### Yapılacaklar

1. N bağımsız candidate assignment oluştur.
2. Candidate’lar birbirlerinin çıktısını görmesin.
3. Ortak objective/context/constraints binding kullan.
4. Selector ayrı assignment/model/execution identity olsun.
5. Qualitative selection gerekiyorsa human final gate destekle.
6. Candidate sayısı, token/cost ve deadline bounded olsun.
7. Tournament sonucu active decision/skill/policy’ye otomatik terfi etmesin.

### Kabul

- Creative thumbnail/metin alternatifi loop yerine tournament’a route edilir.
- Kendi fikrini tekrar puanlayan producer selector olamaz.

---

## WP-10 — Loop-owned reversible change set

### Amaç

İyileşmeyen attempt’i yalnız kendi değişikliklerini geri alarak temizle.

### Yapılacaklar

1. Attempt başlamadan exact source/tree ve allowed path snapshot al.
2. Değişen dosyaları loop-owned change set olarak kaydet.
3. Patch digest ve changed resource list’i checkpoint’e bağla.
4. Passed/meaningful improvement durumunda candidate change set korunabilir.
5. Regression/invalid durumunda yalnız loop-owned inverse patch uygulanabilir.
6. User-owned veya başlangıçta dirty dosyalara blanket reset uygulanmaz.
7. Commit ancak test/verifier/policy geçtikten ve kullanıcı yetkisi varsa yapılır.

### Kabul

- Başarısız attempt kullanıcı değişikliğini silemez.
- `git reset --hard`, `clean`, force checkout rollback değildir.

---

## WP-11 — Scaffolding ablation ve deprecation candidate

### Amaç

Bugünkü model eksiklerini kapatan fakat ileride yük olacak katmanları ölç.

### Değerlendirilecek feature örnekleri

- attempt summary,
- second checker,
- critic node,
- fallback parser,
- retry hint,
- extra context section,
- graph coordinator,
- reranker,
- duplicate guard’ın belirli heuristic katmanları.

### Yapılacaklar

1. Mevcut `ContextAblationProfile` ve paired trial altyapısını genişlet.
2. Baseline ve candidate aynı fixture/repetition/verifier ile çalışsın.
3. Quality/reliability düşmeden latency/token/cost değeri ölçülsün.
4. Fark üretmeyen scaffolding yalnız deprecation candidate olsun.
5. Otomatik kod/policy silme yapılmasın; review + rollback planı zorunlu olsun.

### Kabul

- “Bu katman artık gereksiz” kararı anekdotla değil paired evidence ile üretilir.

---

## WP-12 — Observability, CLI ve Obsidian projection

### Amaç

Loop/graph durumunu model self-report’u yerine dışarıdan okunabilir yap.

### CLI önerileri

```text
zekam loop assess
zekam loop plan
zekam loop status
zekam loop attempts
zekam loop progress
zekam loop stop
zekam topology decide
zekam graph status
zekam graph critical-path
zekam tournament status
zekam experiment ablate
```

Varsayılan bütün plan/assess/status komutları salt okunur olmalıdır. Mutation `--uygula` ve
exact authorization ister.

### Observatory/Obsidian görünümü

Mevcut immutable generation store ve sabit `GUNCEL_BELLEK` kasası kullanılacaktır. Yeni
bir vault yolu veya paralel projection store kurulmaz. Otomatik publish ancak current
source/objective/progress digest'lerine bağlı projection receipt ile yapılır.

- objective ve metric yönleri,
- baseline/current/target,
- attempt timeline,
- patch/hypothesis/failure signatures,
- stop reason,
- token/cost/deadline,
- graph critical path ve overlap,
- tournament candidates/selector,
- scaffolding ablation sonucu,
- source/validator/progress digest’leri.

Raw prompt/response/transcript gösterilmez.

---

## WP-13 — Security, threat model ve operasyon runbook

### Tehditler

- prompt rephrase ile duplicate bypass,
- forged metric evidence,
- producer self-scoring,
- validator asset mutation,
- threshold loosening,
- no-op reward gaming,
- repeated patch/hypothesis,
- oscillation,
- context poisoning,
- raw history/token explosion,
- graph coordination denial-of-service,
- false parallelism,
- cross-resource concurrent writes,
- irreversible effect retry,
- stale source/plan/policy/validator binding,
- quota/cost overrun,
- child failure fan-in’de yutulması,
- rollback’in kullanıcı dosyasını silmesi.

### Runbook’lar

- loop plateau/manual-review,
- invalid measurement,
- validator drift,
- graph deadlock/blocker,
- crash after effect,
- pause/drain/cancel,
- quota fallback,
- loop-owned rollback,
- stale progress packet,
- previous task projection reconciliation.

---

## 10. Ölçüm ve progress karar kuralları

### 10.1 Progress

Bir attempt yalnız şu durumda `improved` olur:

```text
all hard guards within tolerance
AND
(at least one primary metric improved by minimum meaningful delta
 OR all required targets reached)
AND
measurement evidence valid and externally bound
AND
no validator asset drift
AND
no prohibited effect/recovery gap
```

### 10.2 Binary test

Binary test sonucu yalnız:

```text
fail (0) → pass (1)
```

delta’sında progress sayılır. `pass → pass` yeni progress değildir; ek objective gerekiyorsa
ayrı metric tanımlanır.

### 10.3 Diagnosis

Yeni kök neden/diagnosis:

- retry admission sağlayabilir,
- `new-failure-diagnosis` evidence olur,
- metric improvement değildir,
- aynı diagnosis tekrar kullanılamaz,
- policy’nin diagnostic patience sınırına tabidir.

### 10.4 Maliyet değeri

Her attempt için en az:

```text
marginal_primary_gain
actual_tokens
actual_cost
elapsed_time
context_tokens
coordination_cost
```

izlenir. Minimum value-per-cost sınırı policy’ye bağlıdır; tek başına quality düşürmek için
kullanılmaz.

### 10.5 Ölçüm maliyeti

Loop suitability için:

```text
measurement_to_action_ratio = estimated_measurement_cost / estimated_action_cost
```

hesaplanır. Ratio policy sınırını aşıyor, measurement güvenilir değil veya action geri
alınamıyorsa loop reddedilir.

---

## 11. Progress packet içerik ve eleme politikası

### MUST include

- objective/artifact kimliği,
- son attempt sonucu,
- metric vector ve delta,
- hard guard regressions,
- kısa verifier diagnosis,
- accepted/rejected hypothesis digest’leri,
- patch/failure signature,
- remaining budget,
- next allowed focus,
- kaynak/evidence reference’ları.

### MUST NOT include

- ham transcript,
- bütün model response’ları,
- tam test log’u,
- tam patch body,
- önceki bütün promptlar,
- secret/credential/PII,
- private reasoning,
- gereksiz graph mesaj geçmişi.

### Boyut kuralı

Packet ayrı token bütçesi taşır. Bütçeye sığmayan optional evidence açık omission reason ile
çıkarılır. Required state sığmıyorsa fail-closed olur; sessiz truncate edilmez.

---

## 12. Durma matrisi

| Durum | Terminal/karar |
|---|---|
| Hedef doğrulandı | `passed` |
| Hard guard regrese | `blocked` veya rollback + bounded retry |
| Attempt/token/cost/deadline doldu | `budget-exhausted` |
| Stall limit boyunca anlamlı delta yok | `no-progress` |
| Aynı hypothesis/failure/patch tekrarlandı | `blocked` |
| A↔B oscillation | `manual-review` |
| Ölçüm invalid/forged/stale | `manual-review` |
| Validator asset drift | `replan-required` |
| Risk seviyesi yükseldi | `manual-review` |
| Geri alınamaz effect gerekiyor | `queue-human-review` |
| Claim var, receipt yok | `recovery-required` |
| Kullanıcı pause/cancel | `cancelled/paused`; yeni attempt yok |

Harness’in kendi timeout’u canonical stop reason yerine geçmez; yalnız son güvenlik ağıdır.

---

## 13. Dosya ve entegrasyon rehberi

Güncel HEAD yeniden incelendikten sonra en küçük doğru değişiklik yapılmalıdır. Önerilen
yerleşim:

### Domain

- `src/zekam/domain/optimization.py`
  - generic metric/objective/evidence/progress sözleşmeleri.
- `src/zekam/domain/loop_progress.py`
  - progress packet, novelty, stagnation/oscillation.
- `src/zekam/domain/execution_topology.py`
  - pattern ve suitability/decision contract’ları.
- `src/zekam/domain/loop_policy.py`
  - additive v2 binding; v1 backward compatibility.
- `src/zekam/domain/learning.py`
  - duplicate score evaluator’ı shared optimization çekirdeğine adapter yap.
- `src/zekam/domain/model_context_experiment.py`
  - generic metric + scaffolding ablation reuse.
- `src/zekam/domain/agent_graph.py`
  - yalnız runtime evidence gerekiyorsa genişlet; TaskPlan dependency gerçeğini kopyalama.

### Application

- `src/zekam/application/loop_service.py`
  - v2 validation/progress closure.
- `src/zekam/application/loop_orchestrator.py`
  - one-job-per-attempt durable orchestration.
- `src/zekam/application/loop_progress_compiler.py`
  - bounded packet compiler.
- `src/zekam/application/topology_planner.py`
  - direct/loop/tournament/graph/human gate seçimi.
- `src/zekam/application/route_planner.py`
  - topology decision ve graph feedback entegrasyonu.
- `src/zekam/application/context_recipe.py`
  - attempt 2+ progress packet MUST.
- `src/zekam/application/worker.py`
  - explicit loop-attempt handler/composition; sonsuz handler yok.
- `src/zekam/application/observatory.py`
  - metric/attempt/critical-path projection.
- `src/zekam/application/client_instruction_bootstrap.py`
  - yeni dosya üretmeden mevcut managed section'a yalnız gerekli bounded komutları ekle.
- `src/zekam/infrastructure/storage/obsidian_projection_store.py`
  - mevcut immutable generation + `GUNCEL_BELLEK` publish yolunu kullan; ikinci store kurma.

### PostgreSQL

- mevcut `loop_policy_repository.py` genişletilir,
- ölçüm/progress/topology için additive repository eklenebilir,
- current head’te next migration `0066` görünmektedir; uygulama anında migration discovery ile
  next sequential number yeniden belirlenmelidir,
- mevcut v1 tablolar drop/overwrite edilmez,
- append-only/RLS/realm/idempotency/trigger kuralları korunur.

### Schemas

Önerilen yeni JSON schema’lar:

```text
optimization-objective.schema.json
metric-spec.schema.json
measurement-evidence.schema.json
progress-vector.schema.json
loop-progress-packet.schema.json
loop-validation-v2.schema.json
loop-suitability-assessment.schema.json
execution-topology-decision.schema.json
validator-asset-manifest.schema.json
graph-execution-receipt.schema.json
tournament-plan.schema.json
```

### CLI

- plan/status varsayılan dry-run/read-only,
- mutation `--uygula`, exact scope ve authorization,
- JSON output strict ve schema-bound,
- free-text authoritative değildir.

---

## 14. Test ve kabul matrisi

### 14.1 Domain/unit

1. Maximize/minimize/target/range metric delta doğru hesaplanır.
2. Hard guard regression scalar value artsa da reddedilir.
3. NaN/Inf/eksik metric invalid olur.
4. Producer self-report progress sayılmaz.
5. Yeni diagnosis retry sağlar ama improvement sayılmaz.
6. Prompt rephrase aynı hypothesis fingerprint’ini aşamaz.
7. Aynı patch digest tekrar kullanılamaz.
8. No-op progress sayılmaz.
9. Plateau stall limitinde durur.
10. A↔B oscillation yakalanır.
11. Packet required alanları ve token budget’i doğrulanır.
12. Full history packet’e giremez.
13. Suitability measurement/action ratio hesaplar.
14. Reversible false ise loop seçilmez.
15. Creative diversity goal tournament seçer.
16. Tek artifact için gereksiz graph reddedilir.
17. Graph node loop mode taşıyabilir.
18. Validator asset manifest drift’i fail-closed olur.

### 14.2 PostgreSQL/integration

1. Attempt 2+ progress packet olmadan admission reddedilir.
2. Same objective/hypothesis farklı prompt digest ile tekrar reddedilir.
3. Metric evidence başka realm/work/plan’dan bağlanamaz.
4. Result/verifier/measurement receipt cardinality exact olur.
5. Builder ve validator execution identity aynı olamaz.
6. Validator fixture değiştirilirse current attempt kapanamaz.
7. Next-attempt enqueue idempotenttir.
8. Terminal loop yeni job üretmez.
9. Budget concurrency yarışında sınır aşılmaz.
10. Crash after effect → recovery-required.
11. Claim + receipt replay canonical sonucu döndürür.
12. Loop-owned inverse patch kullanıcı dosyasına dokunmaz.
13. Graph critical path deterministic hesaplanır.
14. Actual overlap olmadan parallel receipt üretilemez.
15. Child failure fan-in’de yutulamaz.
16. RLS/cross-realm negative testleri geçer.
17. Fresh DB + upgrade DB migration geçer.
18. SQLite full measured-loop authority’ye sessiz düşmez.

### 14.3 E2E

1. Basit deterministic bug fix: fail→pass ve loop terminal passed.
2. Aynı bug üç kez aynı hypothesis ile çözülmeye çalışılır: ikinci duplicate bloklanır.
3. Benchmark climb: improve olan patch korunur; regression yalnız loop-owned patch ile geri alınır.
4. Creative aday işi tournament’a gider.
5. Migration/deploy/external message loop’a alınmaz; human queue oluşur.
6. Üç bağımsız step graph’ta gerçek overlap ile çalışır.
7. Dependency’li step predecessor bitmeden başlamaz.
8. Graph overhead yüksekse future topology feedback simpler pattern önerir.
9. Worker restart sonrası next attempt duplicate olmaz.
10. Progress packet sayesinde yeni harness predecessor state’i görür.
11. Context attempt sayısıyla sınırsız büyümez.
12. Scaffolding with/without paired experiment kanıt üretir.
13. Observatory/Obsidian raw transcript/secret içermez.
14. `zekam close` güncel source/projection/receipt ile kapanır.

### 14.4 Security

- forged metric value/evidence,
- prompt injection in diagnosis,
- threshold loosening,
- builder writes tests/fixtures,
- self-verifier,
- same model/execution identity on critical work,
- path traversal/symlink escape,
- secret/PII/raw transcript,
- cross-realm evidence,
- stale plan/policy/source/validator,
- irreversible effect retry,
- budget overflow,
- graph resource conflict,
- fake parallelism,
- user-file rollback damage,
- no-op reward gaming.

### 14.5 CI politikası

Aşağıdakiler yerelde zorunludur:

- package validator,
- protocol/schema generation check,
- ruff format/check,
- mypy,
- unit/integration/E2E/security,
- fresh/upgrade PostgreSQL,
- package build/install/smoke,
- bağımsız verifier.

GitHub Actions tarafında mevcut `workflow_dispatch` korunur. Otomatik `push` veya
`pull_request` tetikleyicisi eklenmez. Kullanıcı ayrıca isterse manuel workflow çalıştırılır.

---

## 15. Rollout

### Gate 0 — Önceki görev reconciliation

- DB migration ve source HEAD eşleşir.
- Eski Work/Run/receipt kapanır.
- Root projections güncellenir.

### Gate 1 — Shadow metric recording

- Mevcut loop davranışı değişmeden metric/evidence/progress vector kaydedilir.
- Karar hâlâ v1 path’ten gelir; v1/v2 farkı gözlemlenir.

### Gate 2 — Progress packet enforcement

- Attempt 2+ packet zorunlu olur.
- Duplicate/plateau detector shadow’dan enforced’a geçer.

### Gate 3 — Measured continuation

- Next attempt yalnız v2 progress decision ile enqueue edilir.
- No-progress ve invalid measurement terminal olur.

### Gate 4 — Topology planner advisory

- Direct/loop/tournament/graph/human önerisi üretilir; mevcut planla karşılaştırılır.
- Yanlış yönlendirme oranı ölçülür.

### Gate 5 — Topology enforcement

- Passing eval ve bağımsız verifier sonrası topology decision admission’a bağlanır.

### Gate 6 — Graph evidence ve tournament

- Critical path/overlap/overhead kaydı ve tournament selector devreye girer.

### Gate 7 — Scaffolding ablation

- Değer üretmeyen katmanlar yalnız deprecation candidate olur.

---

## 16. Rollback ve recovery

- Feature mode `disabled/shadow/enforced` olmalı; enforced geçiş exact authorization ister.
- V1 loop path additive migration süresince korunur.
- V2 decision problemi olursa new-attempt enqueue pause edilir; mevcut append-only evidence
  silinmez.
- Progress packet projection yeniden üretilebilir.
- Topology planner enforced’dan advisory/shadow’a alınabilir.
- Graph/tournament yeni Work gerçeği yaratmaz; TaskPlan korunur.
- Loop-owned patch inverse receipt ile geri alınır; blanket Git rollback yapılmaz.
- Validator asset manifest eski geçerli revision’a dönebilir; history rewrite yapılmaz.
- Claim var, terminal receipt yoksa recovery-required; sessiz retry yok.
- DB rollback rehearsal bounded ve ayrı kanıtlıdır.

---

## 17. Tamamlanma ölçütleri

Görev yalnız aşağıdakilerin tümü sağlandığında tamamlanır:

- [ ] Önceki Memory Learning/Obsidian implementation kanonik DB ve root projection ile uzlaştırıldı.
- [ ] Yeni aktif görev kanonik Work/Plan/Run olarak kaydedildi.
- [ ] Generic objective/metric/evidence/progress çekirdeği oluşturuldu.
- [ ] `learning.evaluate_loop`, runtime LoopPolicy ve model/context experiment aynı çekirdeği kullanıyor.
- [ ] Runtime loop v1 backward compatibility korunuyor.
- [ ] Loop validation v2 directional metric vector taşıyor.
- [ ] Hard guard no-regression enforced.
- [ ] Producer self-report progress sayılmıyor.
- [ ] Attempt 2+ bounded progress packet olmadan başlayamıyor.
- [ ] Full history/transcript context’e yığılmıyor.
- [ ] Prompt rephrase aynı hypothesis/patch/failure tekrarını aşamıyor.
- [ ] No-op, plateau ve oscillation stop kapıları geçiyor.
- [ ] Validator asset manifest immutable ve builder write scope dışında.
- [ ] Durable worker bir job = bir attempt kuralıyla çalışıyor.
- [ ] Next-attempt enqueue idempotent ve crash-safe.
- [ ] Direct/single/tournament/loop/graph/human topology planner mevcut.
- [ ] Tek artifact için gereksiz graph reddediliyor.
- [ ] Geri alınamaz effect queue-human-review’a yönleniyor.
- [ ] Graph gerçek critical path/overlap/coordination evidence üretiyor.
- [ ] Fake parallelism raporlanamıyor.
- [ ] Tournament candidate isolation ve independent selector geçiyor.
- [ ] İyileşmeyen attempt yalnız loop-owned patch’i geri alıyor.
- [ ] Scaffolding ablation paired evidence üretiyor.
- [ ] Observatory/Obsidian metric ve stop reason görünümü üretiyor.
- [ ] Projection mevcut immutable generation store ve sabit `GUNCEL_BELLEK` yolunu kullanıyor.
- [ ] Otomatik stable-vault publish varsa exact projection receipt/idempotency ile bağlı.
- [ ] Secret/PII/raw transcript hiçbir projection/evidence’e sızmıyor.
- [ ] Fresh DB, upgrade DB, full local quality ve security testleri geçiyor.
- [ ] Bağımsız verifier builder’dan farklı model ve execution identity ile onaylıyor.
- [ ] CI manuel `workflow_dispatch` olarak kaldı; otomatik tetikleyici eklenmedi.
- [ ] Kullanıcı açıkça istemedikçe commit/push/PR oluşturulmadı.
- [ ] `zekam close` güncel projection/receipt ile işi güvenli kapattı.

---

## 18. Yasak kısa yollar

- Yeni ve mevcut loop semantiğini paralel iki ayrı engine olarak bırakmak.
- `while true` ile worker handler içinde dönmek.
- Modelin “ilerledim” beyanını score yapmak.
- Sadece passed/failed taşıyıp metric yönünü saklamamak.
- Bütün geçmişi her attempt’e yüklemek.
- Prompt değişti diye aynı hypothesis’i yeni saymak.
- New evidence’i otomatik improvement saymak.
- Builder’a test/fixture/threshold değiştirme izni vermek.
- Tek artifact için ajan sayısını artırıp graph demek.
- Creative işi self-scoring loop’a sokmak.
- Deploy/migration/message/submit effect’ini loop içinde retry etmek.
- Graph paralelliğini ajan sayısıyla raporlamak.
- Child failure’ı fan-in’de yutmak.
- İyileşmeyen attempt için kullanıcı worktree’sini resetlemek.
- Ablation olmadan “artık gereksiz” diye koruma katmanını silmek.
- Mevcut client bootstrap veya `GUNCEL_BELLEK` yanında ikinci bir bootstrap/vault yolu kurmak.
- Otomatik GitHub CI açmak.
- Kullanıcı onayı olmadan commit/push/PR oluşturmak.

---

## 19. Uygulayıcı ajanın çalışma protokolü

1. Gerçek repo kökünü doğrula.
2. `AGENTS.md`, `00_BASLA.md`, `DEVAM_PROTOKOLU.md`, manifest ve policy dosyalarını oku.
3. Git/HEAD/migration/doctor/lease/claim/receipt/recovery durumunu çıkar.
4. Önce `../zekam-girdi/AKTIF_GOREV.md` dosyasını doğrula.
5. Önceki aktif görevin source implementation + canonical DB/projection close parity’sini tamamla.
6. Problem yoksa bu dosyayı kök `AKTIF_GOREV.md` üzerine kopyala ve yaşayan aktif görev yap.
7. Kanonik Work/Intent/Plan oluştur; Markdown’dan authority türetme.
8. Güncel kod üzerinde gap analysis yap; çözülmüş parçaları yeniden yazma.
9. Yeni paralel loop/graph store kurma; mevcut `LoopPolicy`, `TaskPlan`, `RoutePlanner`,
   `learning` ve experiment altyapısını konsolide et.
10. Önce contract/schema/test, sonra en küçük additive implementation yap.
11. Her migration için fresh, upgrade ve bounded rollback rehearsal yap.
12. Her effect için claim/receipt/idempotency/recovery kurallarına uy.
13. Uzak model/provider çağrılarını varsayılan kapalı tut.
14. Local testleri ve bağımsız verifier’ı çalıştır.
15. GitHub CI manual kalsın; kullanıcı açıkça istemedikçe workflow tetikleme/değiştirme.
16. Kullanıcı dosyalarını koru; blanket reset/clean/stash yapma.
17. Kök `AKTIF_GOREV.md` görev ilerledikçe güncel tutulur; DB/YAML projection ile drift
    görünür ve kapanışta uzlaştırılır.
18. Sonuçta değişen dosya, schema/migration, test, metric, projection, receipt, risk ve
    rollback kanıtlarını raporla.
19. `zekam close --project-key zekam` ile kapanışı tamamla.
20. Açık kullanıcı yetkisi olmadan commit/push/PR oluşturma.

---

## 20. Beklenen teslimatlar

1. Önceki görev parity/reconciliation receipt’i.
2. Generic optimization objective/metric/evidence/progress domain’i.
3. Measured LoopPolicy v2 ve PostgreSQL persistence.
4. LoopProgressPacket compiler ve context integration.
5. Rephrase-proof novelty/stagnation/oscillation gate’leri.
6. ValidatorAssetManifest ve builder write isolation.
7. Durable one-attempt-per-job loop orchestrator.
8. ExecutionTopology suitability planner.
9. TaskPlan/RoutePlanner graph execution evidence.
10. Tournament pattern ve independent selector.
11. Loop-owned reversible change-set/rollback receipt’i.
12. Scaffolding ablation campaign entegrasyonu.
13. CLI/Observatory/Obsidian projections ve mevcut `GUNCEL_BELLEK` publish entegrasyonu.
14. Unit/integration/E2E/security testleri.
15. Architecture/threat model/runbook belgeleri.
16. Bağımsız verification evidence.
17. Güncel root `AKTIF_GOREV.md` + kanonik Work/YAML projection + close receipt.
18. CI’nin manuel kaldığı ve push yapılmadığına ilişkin sonuç kaydı.

---

## 21. Kısa uygulama promptu

```text
Zekam repo kökünde çalış. AGENTS.md, 00_BASLA.md ve DEVAM_PROTOKOLU.md başlangıç
kurallarını uygula; git/HEAD/migration/doctor/lease/claim/receipt/recovery durumunu
doğrula. Önce ../zekam-girdi/AKTIF_GOREV.md dosyasını oku. Önceki Memory
Learning/Obsidian görevinin kaynak kodu main’de uygulanmış olsa da root AKTIF_GOREV.yaml
eski source/migration bilgisi taşıyorsa önce kanonik PostgreSQL Work/Run/receipt ve root
projection parity’sini tamamla. Problem yoksa dış görev dosyasını kontrollü biçimde kök
AKTIF_GOREV.md üzerine kopyala ve bundan sonra kökteki dosyayı yaşayan/güncel aktif görev
olarak tut; problem varsa mevcut görevin üzerine yazma.

Yeni paralel bir loop veya graph engine kurma. Mevcut LoopPolicy/runtime.loop_*,
BoundedLoopExecutor, learning.evaluate_loop, model/context experiment, TaskPlan DAG,
RoutePlanner, AgentGraph ve worker/queue altyapısını konsolide ederek ölçümlü yürütme
düzlemini uygula. Her loop stable objective, dış ve immutable directional metric vector,
frozen validator assets, rephrase-proof hypothesis/patch/failure fingerprint, bounded
LoopProgressPacket, no-op/plateau/oscillation stop kapıları ve one-job-per-attempt durable
orchestration kullansın. Yeni evidence retry hakkı verebilir ama ölçülmüş progress sayılmasın.

İş için önce topology suitability üret: tek işte direct/single, ölçümü ucuz ve reversible
aynı artifact’ta bounded-loop, yaratıcı çeşitlilikte tournament, gerçek farklı deliverable
ve dependency’de graph, geri alınamaz/high-risk effect’te queue-human-review seç. Graph
TaskPlan gerçeğini kopyalamasın; gerçek critical path, wait, overlap, coordination cost ve
fan-in receipt’i üretsin. Builder test/fixture/metric/threshold asset’lerini değiştiremesin;
regression yalnız loop-owned inverse patch ile geri alınsın, kullanıcı değişiklikleri
korunsun. Scaffolding yalnız paired ablation kanıtıyla deprecation adayı olsun. Mevcut
managed client instruction bootstrap ve immutable generationdan üretilen `GUNCEL_BELLEK`
yolunu yeniden kullan; ikinci bootstrap/vault/store kurma, otomatik publish'i exact
projection receipt ve idempotency'ye bağla.

PostgreSQL tek otorite, model self-report authority/progress değil, raw transcript/secret/PII
yasak, remote calls varsayılan kapalı olsun. Tüm yerel quality, fresh/upgrade DB,
unit/integration/E2E/security testlerini ve builder’dan farklı model+execution identity’ye
sahip bağımsız verifier’ı çalıştır. GitHub Actions workflow_dispatch olarak manuel kalsın;
ben açıkça istemedikçe otomatik CI tetikleyicisi, commit, push veya PR oluşturma. Sonunda
root task/projection’ı kanonik state ile uzlaştırıp zekam close ile receipt-bound kapanış yap.
```
