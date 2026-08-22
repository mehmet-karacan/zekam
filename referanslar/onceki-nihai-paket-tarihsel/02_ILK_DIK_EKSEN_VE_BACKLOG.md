# Z Control Plane — İlk Dikey Eksen ve Uygulanabilir Backlog

## 1. İlk ürün dilimi

İlk uçtan uca dilim şu kullanıcı komutunu güvenli ve modelden bağımsız biçimde çalıştırmalıdır:

```bash
zctl ask "gpu projesindeki 123 numaralı defectin kök nedenini araştır"
```

Bu dilim source mutation yapmaz. Ama aşağıdaki kalıcı çekirdeğin gerçekten çalıştığını kanıtlar:

- project/alias registry;
- exact defect lookup;
- Work + Intent revision;
- Context Manifest;
- minimum bir researcher subagent;
- model adapter abstraction;
- queue, lease, fencing ve checkpoint;
- strict Agent Result Envelope;
- evidence ve Türkçe research report;
- continuity ve başka model/CLI ile resume;
- idempotency ve failure/recovery.

## 2. Uçtan uca acceptance senaryosu

### Ön koşul

```bash
zctl project add /salt-okunur/gpu-fusion \
  --slug gpu-fusion \
  --alias gpu \
  --display-name "GPU Fusion"

zctl work create \
  --project gpu-fusion \
  --type defect \
  --external-id 123 \
  --title "Rapor ekranında yanlış toplam"
```

### Kullanıcı çağrısı

```bash
zctl ask "gpu projesindeki 123 numaralı defectin kök nedenini araştır"
```

### Zorunlu davranış

1. `gpu` yalnız `gpu-fusion` projesine çözülür.
2. `123`, Work Graph’ta exact external ID ile bulunur.
3. Var olan defect duplicate edilmez.
4. Yeni Intent revision oluşturulur.
5. Read-only research TaskPlan hazırlanır.
6. En az bir gerçek researcher subagent atanır; koordinatör subagent sayılmaz.
7. Model seçimi hard eligibility ve sürümlü policy ile yapılır.
8. Queue claim, lease ve fencing token üretilir.
9. Source binding yalnız read olarak açılır.
10. Child strict JSON result envelope döndürür.
11. En az bir evidence referansı yoksa `completed` kabul edilmez.
12. Koordinatör Türkçe rapor ve next-safe-action üretir.
13. Checkpoint ve finalized continuity packet yazılır.
14. Aynı idempotency key ikinci run/effect üretmez.
15. Başka adapter/model `zctl resume` ile aynı state’i okur.

### Terminal çıktı örneği

```text
İş: DEFECT-123
Proje: GPU Fusion
Durum: completed | partial | blocked | recovery-required
Araştırma run: <run-id>
Kullanılan subagent: <execution-identity>
Kanıtlar: <count>
Rapor: projeler/gpu-fusion/defectler/123/raporlar/<report-id>.md
Bir sonraki güvenli adım: <action>
```

## 3. PR sırası

Her PR küçük, geri alınabilir ve tek bir acceptance kapısına bağlıdır.

---

## PR-001 — Repository, kalite kapıları ve ADR’ler

### Kapsam

- `pyproject.toml`
- package scaffold
- Ruff, type checker, pytest, coverage
- dependency direction architecture tests
- canonical JSON/digest utility
- UTC clock/ID portları
- `zctl --version` ve `zctl doctor`
- PostgreSQL/object store compose
- ADR-001..009
- CI

### Testler

```text
canonical JSON aynı semantic input için aynı digest
unknown JSON field fail-closed
UTC timestamp normalization
architecture dependency direction
settings secret value repr içinde görünmüyor
migration boş başlangıç upgrade/downgrade
zctl doctor dependency status
```

### Kapı

- gerçek model çağrısı yok;
- domain framework import etmiyor;
- bütün kalite kapıları geçiyor.

---

## PR-002 — Project Registry ve salt-okunur source binding

### Kapsam

- `core.project`, `project_alias`, `source_binding`
- project add/list/show
- alias normalization ve ambiguity
- source root fingerprint
- relative path/no-copy contract
- readable `proje.md` projection

### Negatif testler

- aynı alias iki aktif project’e bağlanınca otomatik seçim yok;
- symlink root escape reddi;
- physical path public project record’a yazılamıyor;
- source root’a write probe reddediliyor;
- başka realm project’i okunamıyor.

### Kapı

`zctl project add` source’a hiçbir dosya yazmadan project’i kaydeder.

---

## PR-003 — Work Graph, Intent ve resume

### Kapsam

- Work item/revision/relation/event
- exact external ID
- Intent head + append-only revisions
- work create/query/history
- `zctl today`
- `zctl resume`
- Turkish readable projections

### Negatif testler

- cycle relation reddi;
- stale revision update reddi;
- completed item evidence olmadan kapanmıyor;
- cross-project relation reddi;
- Markdown projection silinse resume çalışıyor;
- vector/index olmadan exact defect lookup çalışıyor.

### Kapı

`gpu + 123` exact project/work identity’sine çözülür.

---

## PR-004 — Runtime contract ve PostgreSQL queue

### Kapsam

- TaskPlan/Step DAG
- queue item/attempt/lease
- `FOR UPDATE SKIP LOCKED` claim
- fencing token
- heartbeat
- logical resource locks
- checkpoint
- idempotency key
- fake worker host

### Recovery testleri

1. İki worker aynı queue item’ı claim edemez.
2. Expired lease yeni fencing token ile alınır.
3. Eski worker stale fence ile complete edemez.
4. Parent/child path write lock çakışır.
5. Read/read lock paralel çalışır.
6. Aynı idempotency key duplicate TaskPlan oluşturmaz.
7. Worker crash sonrası read-only step requeue olur.
8. Pending non-read claim varsa automatic retry olmaz.

### Kapı

Fake adapter ile tek subagent run’ı durable olarak tamamlanır ve resume edilir.

---

## PR-005 — Effect Ledger, Result Envelope ve Verification

### Kapsam

- Validation Gate
- Effect Claim/Receipt
- Agent Result Envelope v1 JSON Schema
- result parser/normalizer
- coordinator fan-in
- verifier identity
- workflow receipt

### Negatif testler

- unknown result field;
- tampered result digest;
- raw secret/prompt/absolute path;
- non-read completed result without claim/receipt;
- `partial` result completed fan-in yapamıyor;
- worker ve verifier aynı identity;
- duplicate step/attempt envelope;
- receipt scope result scope ile uyuşmuyor.

### Kapı

Terminal durum yalnız doğrulanmış envelope + receipt zincirinden türetilir.

---

## PR-006 — Model inventory, quota pool ve fake benchmark

### Kapsam

- provider/client/model/model revision
- capability record
- health/quarantine/cooldown
- project workload benchmark suite
- benchmark trial + aggregate
- quota pool/observation
- deterministic Model Decision Service
- fake models: good/slow/cheap/failing/quota-low

### Testler

- health-failed model dışlanır;
- benchmark floor altı dışlanır;
- context/modality/workload uyumsuz model dışlanır;
- remote forbidden data dışlanır;
- quota `remaining=null` iken yüzde uydurulmaz;
- trusted `%39` Codex observation policy `%40` altında ise fallback;
- verifier builder model exclusion;
- aynı input/policy aynı assignment digest.

### Kapı

Model kararı hiçbir gerçek provider çağrısı yapmadan açıklanabilir sonuç verir.

---

## PR-007 — OpenCode adapter ve model keşfi

### Kapsam

- executable resolution via explicit config
- credential-free model discovery
- model inventory import plan
- synthetic health probe
- noninteractive structured execution
- output/event normalization
- timeout/cancel
- usage observation
- Turkish model health/capability report

### Güvenlik

- OpenCode config: edit/shell/network/external-directory/skill varsayılan deny;
- child’a yalnız bounded input artifact verilir;
- raw auth/config dosyası okunmaz;
- model listesi provider permission sayılmaz;
- malformed/free-text output completed sayılmaz.

### Kapı

En az bir çalışan kurum modeli inventory → health → benchmark → eligible zincirini geçer. Çalışmayan model quarantine olur; görev sistemi bloklanmaz.

---

## PR-008 — Codex ve Claude adapter’ları

### Kapsam

- OpenCode ile aynı adapter contract
- explicit execution path
- structured JSON/event parser
- permission/sandbox flags
- cancellation/deadline
- usage/quota observation
- fallback chain

### Testler

- executable yok → unavailable;
- auth yok → blocked, secret echo yok;
- timeout → sanitized failure;
- malformed output → adapter-error;
- quota observation path-specific quota pool’a yazılır;
- running step ortasında transcript migration yapılmaz;
- checkpoint sonrası model değişimi başarılı.

### Kapı

Bir adapter devre dışıyken Work/Run state kaybolmadan başka adapter ile devam edilir.

---

## PR-009 — Natural-language intake ve Context Compiler

### Kapsam

- `zctl ask`
- request sanitization
- project alias + exact work resolver
- ambiguity/choice-required
- Work/Intent capture plan
- Context Manifest
- Continuity Packet
- token budget ve omitted reasons

### Negatif testler

- “bu projede” ama active/matched project yok;
- “bunu araştır” ama subject yok;
- secret assignment içeren request;
- iki project aynı alias;
- zorunlu context budget’ı aşıyor;
- stale Work/source revision;
- context’e authorization token veya secret eklenmesi.

### Kapı

Hedef kullanıcı komutu typed ve digest-bound Research TaskPlan’a dönüşür.

---

## PR-010 — Evidence Research dikey dilimi

### Kapsam

- ResearchQuestion
- SourcePlan
- project source reader
- source snapshot/digest
- one researcher subagent
- claim/evidence
- contradiction classification
- citation verification
- Turkish synthesis/report
- Work/Run checkpoint ve receipt

### Araştırma akışı

```text
capture
→ scope
→ source plan
→ researcher subagent
→ coordinator counter-evidence pass
→ synthesis
→ citation verification
→ report
→ finalized handoff
```

### Testler

- minimum 1 subagent enforcement;
- source revision drift;
- evidence locator/digest mismatch;
- duplicate source double-count edilmiyor;
- required evidence missing → partial;
- direct contradiction görünür kalıyor;
- prompt injection içeren source talimatı çalıştırılmıyor;
- report authority vermiyor.

### Kapı

İlk kullanıcı komutu uçtan uca başarıyla çalışır.

## 4. PR-010 sonrası ilk release acceptance

Release adayı `0.1.0-alpha.1` aşağıdaki komutlarla doğrulanır:

```bash
zctl doctor
zctl project list
zctl work query --project gpu-fusion --external-id 123
zctl ask "gpu projesindeki 123 numaralı defectin kök nedenini araştır"
zctl runtime status <run-id>
zctl resume --project gpu-fusion --work <work-id>
zctl report show <report-id>
```

### Ölçülebilir kalite kapıları

- exact project/work resolution: `%100` fixture doğruluğu;
- duplicate Work/Run: `0`;
- stale fence publish: `0` kabul;
- source root mutation: `0`;
- raw secret/public path leakage: `0`;
- mandatory evidence coverage: `%100`;
- crash/recovery acceptance: tüm recovery fixture’ları geçer;
- model change resume: en az iki adapter/fake profile arasında geçer;
- agentic run subagent sayısı: `>=1`;
- kanıtsız `completed`: `0`.

## 5. İkinci dikey dilim — araştırmadan uygulamaya

İlk release tamamlandıktan sonra:

```bash
zctl ask "DEFECT-123 araştırma raporuna göre uygulanabilir planı hazırla"
zctl plan show <plan-id>
zctl plan authorize <plan-id> --exact-digest <sha256>
zctl run apply <plan-id>
```

Akış:

1. Research report source/evidence digest’e bağlanır.
2. Decision revision oluşturulur.
3. Exact file/resource allowlist’li Plan Revision oluşur.
4. User one-shot authorization verir.
5. Tek builder subagent detached worktree’de değişiklik yapar.
6. Core doğrulama komutlarını shell-free/allowlist ile tekrar çalıştırır.
7. Ayrı verifier sonucu kontrol eder.
8. Patch artifact ve terminal receipt yazılır.
9. Source’a apply ayrı effect olarak yürütülür.
10. Commit/push yapılmaz.

## 6. Uygulamayı durduracak kapılar

Aşağıdaki durumlarda sonraki PR/faza geçilmez:

- kanonik state ile Markdown/vector karışmışsa;
- source root’a test dışı write varsa;
- result envelope strict değilse;
- queue lease/fencing recovery testleri geçmiyorsa;
- non-read effect receipt’siz tamamlanabiliyorsa;
- model adapter domain katmanına sızmışsa;
- quota yüzdesi tahmin edilip gerçek gibi kaydediliyorsa;
- secret prompt/log/vector içinde görülebiliyorsa;
- agentic run subagent olmadan başlayabiliyorsa;
- aynı writable scope iki builder’a atanabiliyorsa;
- `completed` status evidence/verifier olmadan oluşabiliyorsa.

## 7. İlk sprint görev listesi

### Sprint A — 001 ve 002

- [ ] repository scaffold
- [ ] quality/architecture gates
- [ ] PostgreSQL compose/migration
- [ ] `zctl doctor`
- [ ] project table/alias/binding
- [ ] source no-write tests
- [ ] ADR’ler

### Sprint B — 003 ve 004

- [ ] Work Graph/Intent
- [ ] exact defect lookup
- [ ] today/resume
- [ ] TaskPlan/DAG
- [ ] queue/lease/fencing/locks/checkpoint
- [ ] fake worker

### Sprint C — 005 ve 006

- [ ] effect ledger
- [ ] result envelope/fan-in
- [ ] independent verifier
- [ ] model inventory/health/benchmark/quota/assignment

### Sprint D — 007–009

- [ ] OpenCode adapter
- [ ] Codex/Claude adapter contract
- [ ] natural-language intake
- [ ] Context Manifest/Continuity Packet

### Sprint E — 010

- [ ] research records
- [ ] one-subagent execution
- [ ] evidence/citation/contradiction
- [ ] Turkish report
- [ ] full vertical acceptance

## 8. Tamamlanma makbuzu şablonu

Her PR şu receipt’i üretir:

```yaml
change_id:
work_item_id:
plan_revision:
source_revision_before:
source_revision_after:
changed_paths: []
architecture_tests: []
unit_tests: []
integration_tests: []
security_tests: []
recovery_tests: []
failed_or_skipped_tests: []
subagent_results: []
verifier_result:
effect_claim:
effect_receipt:
artifacts: []
known_limitations: []
next_exact_action:
```

Bu receipt olmadan PR tamamlanmış raporlanmaz.
