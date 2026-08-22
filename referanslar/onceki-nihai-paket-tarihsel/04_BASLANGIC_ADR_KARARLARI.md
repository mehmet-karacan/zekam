# Z Control Plane — Başlangıç ADR Kararları

Bu dosya uygulama repository’sinde ayrı ADR dosyalarına bölünecek başlangıç kararlarını taşır. Her ADR ilk PR’da `docs/adr/` altına alınmalı; karar değişirse eski kayıt silinmemeli, yeni ADR ile supersede edilmelidir.

---

## ADR-001 — Modüler monolit ile başla

**Durum:** Accepted by architecture research  
**Karar:** İlk ürün tek repository, tek logical application ve aynı kod tabanından çalışan CLI/API/scheduler/worker süreçleri olacaktır.

**Gerekçe:** Domain sınırları henüz olgunlaşmadan mikroservis; dağıtık transaction, ikinci queue, observability ve deployment maliyetini artırır. Kalıcı ayrım process değil bounded context ve portlarla sağlanabilir.

**Sonuçlar:**

- Domain ve application portları net olacaktır.
- Worker ayrı process olabilir, fakat aynı schema/contract kullanır.
- Daha sonra servis ayrıştırması event/ownership kanıtıyla yapılabilir.

**Doğrulama:** Architecture import testleri ve module ownership testleri.

---

## ADR-002 — PostgreSQL kanonik durumun tek sahibidir

**Durum:** Accepted  
**Karar:** Work, intent, plan, authorization, runtime, claim/receipt, model kararları, research ve lifecycle state PostgreSQL’de tutulacaktır.

**Gerekçe:** Model, sohbet, Markdown, vector index veya Redis üzerinden resume güvenli değildir. Transaction, constraint, append-only revision ve RLS aynı sınırda gereklidir.

**Sonuçlar:**

- pgvector aynı veritabanında projection olarak kullanılabilir.
- Redis yalnız wake-up/cache olur.
- Markdown/dashboard silinse de status/resume çalışır.

**Doğrulama:** Projection’lar kaldırılarak Work/Run resume integration testi.

---

## ADR-003 — İç runtime kendi tipli sözleşmesini kullanır

**Durum:** Accepted  
**Karar:** DAG, lease, fencing, lock, checkpoint, claim/receipt ve result envelope ürün çekirdeğinin internal contract’ıdır. Bir agent framework, MCP veya A2A bunu sahiplenmez.

**Gerekçe:** Haricî protokoller ve framework’ler hızlı değişir; authority ve recovery ürünün temel değeridir.

**Sonuçlar:**

- MCP tool/resource adapter’ıdır.
- A2A ileride haricî bağımsız ajan federasyonu için kullanılabilir.
- Model/CLI adapter’ları internal request/result contract’a normalize edilir.

**Doğrulama:** Fake adapter ile bütün runtime acceptance testlerinin haricî framework olmadan geçmesi.

---

## ADR-004 — Agentic işte minimum bir subagent, dinamik concurrency

**Durum:** Accepted  
**Karar:** Ana koordinatör subagent sayılmaz. Agentic çalışma en az bir subagent kullanır. Deterministik exact işlem sıfır subagent kullanabilir. Sabit global maksimum yoktur; concurrency run başına hesaplanır.

**Gerekçe:** Her işte iki ajan gerektirmek küçük işleri gereksiz pahalı yapar; maksimum ajan kullanmak çakışma ve token israfı üretir. Buna karşılık agentic işin en az bir child execution ile ayrılması sonuç attribution ve continuity sağlar.

**Sonuçlar:**

- Standart araştırma: coordinator + 1 researcher.
- Mutation: tek builder; risk gerektirirse ayrı verifier.
- Paralellik yalnız ayrık, lock-çakışmasız DAG node’larında.
- Worker pool limiti operasyonel kapasitedir, semantic ajan maksimumu değildir.

**Doğrulama:** Agentic run’ın child olmadan başlamasını reddeden policy testi; deterministik status query’nin model çağrısı yapmaması.

---

## ADR-005 — Source no-write ve detached worktree

**Durum:** Accepted  
**Karar:** Kayıtlı proje root’u salt-okunur binding’dir. Mutating work her attempt için `Z_HOME/calisma-alanlari/` altında detached worktree/sandbox’da yürür.

**Gerekçe:** Paralel ajan, yanlış proje ve plan dışı mutation riskini source root’tan ayırmak gerekir.

**Sonuçlar:**

- Exact relative path allowlist.
- Symlink/traversal reddi.
- Source revision apply öncesi yeniden doğrulanır.
- Commit/push ayrı approval ister.

**Doğrulama:** Source tree digest’in plan/apply sürecinde değişmediğini kanıtlayan integration/security testleri.

---

## ADR-006 — PostgreSQL queue, lease, fencing ve effect ledger

**Durum:** Accepted  
**Karar:** Durable queue PostgreSQL tablolarıdır. Worker claim’i `FOR UPDATE SKIP LOCKED`; ownership lease ve monoton fencing token ile korunur. Write/network effect, claim ve terminal receipt kullanır.

**Gerekçe:** Notification veya broker mesajı result authority değildir. Crash, replay ve stale worker için durable kanıt gerekir.

**Sonuçlar:**

- `LISTEN/NOTIFY` veya Redis yalnız wake-up olabilir.
- Read-only idempotent attempt yeniden alınabilir.
- Claim var, receipt yoksa `recovery-required`.
- Sessiz retry yoktur.

**Doğrulama:** Concurrent claim, stale fence, pending claim recovery ve idempotent replay testleri.

---

## ADR-007 — Model kararını quota pool ve kanıta bağla

**Durum:** Accepted  
**Karar:** Model seçimi önce hard eligibility, sonra sürümlü puanlama ile yapılır. Provider markası yerine execution path’e bağlı quota pool kullanılır.

**Gerekçe:** Aynı provider’ın subscription CLI, Agent SDK ve API yolları farklı limit/fiyat davranışı taşıyabilir. “Codex %40” gibi eşikler yalnız güvenilir observation varsa uygulanmalıdır.

**Sonuçlar:**

- Model inventory, health, benchmark ve runtime observations ayrı kayıtlar.
- Kalan yüzde bilinmiyorsa uydurulmaz.
- Fallback safe checkpoint sınırında olur.
- Verifier builder model/identity exclusion uygular.

**Doğrulama:** Fake quota/health/benchmark fixture’larıyla deterministic assignment golden testleri.

---

## ADR-008 — Knowledge Plane sürümlü hybrid retrieval’dır

**Durum:** Accepted  
**Karar:** İlk semantic profil BGE-M3 dense 1024/cosine olarak korunur. Retrieval exact ID/path/symbol, alias/trigram, PostgreSQL FTS, dense vector, RRF ve opsiyonel reranker’dan oluşur.

**Gerekçe:** Dense-only retrieval defect numarası, class/method, DB object ve path sorgularında güvenilir değildir. Kaynak/parsing/profile değişimi stale index oluşturmalıdır.

**Sonuçlar:**

- Source/version/artifact/normalized-content korunur.
- Embedding profile sürümlüdür.
- Query/passage prefix varsayılmaz; A/B test edilir.
- HNSW filtering recall golden set ile ölçülür.
- Vector Work status veya authority belirlemez.

**Doğrulama:** Exact/lexical/semantic/no-answer/citation eval suite ve controlled re-index testleri.

---

## ADR-009 — SecretRef ve SecretBroker

**Durum:** Accepted  
**Karar:** Core ve model yalnız SecretRef görür. Secret değeri dar scope/TTL ile adapter sınırında enjekte edilir.

**Gerekçe:** Prompt, log, artifact metadata, vector ve handoff secret için güvenli depolar değildir.

**Sonuçlar:**

- Local OS keychain/encrypted store ve enterprise Vault adapter.
- Existing CLI OAuth session CLI’a aittir.
- Secret public serialization ve exception text’inde görünmez.
- Mümkünse single-use response wrapping/dynamic lease.

**Doğrulama:** Secret canary’nin prompt/log/DB projection/artifact/vector/continuity içinde bulunmadığı security testleri.

---

## ADR-010 — Risk-temelli approval, exact one-shot authorization

**Durum:** Accepted  
**Karar:** Exact read/projection otomatik; önceden kapsamlanmış read-only schedule preauthorized; mutation/network/secret/commit/push tek kullanımlık exact authorization ister.

**Gerekçe:** Her adımda onay kullanıcıyı yorar; geniş veya kalıcı onay güvenliği bozar.

**Sonuçlar:**

- Authorization plan/effect/scope/policy/expiry’ye digest-bound.
- Apply anında atomik tüketilir.
- Plan revision değişirse authorization stale olur.
- Reopen/yeni effect yeni authorization ister.

**Doğrulama:** stale/replayed/revoked/expired/mismatched scope concurrency testleri.

---

## ADR-011 — Context ve continuity authority taşımaz

**Durum:** Accepted  
**Karar:** Context Manifest ve Continuity Packet exact kayıtlardan üretilen bounded projection’lardır; active lease, authorization token veya secret taşımaz.

**Gerekçe:** Model değişiminde transcript transferi hem kırılgan hem fazla tokenlıdır. Öte yandan handoff’un yetki taşıması replay riskidir.

**Sonuçlar:**

- Soft/hard boyut limiti.
- Required kayıt budget’a sığmazsa fail-closed.
- Work/run/source revision değişince stale.
- Resume fresh authorization/lease alır.

**Doğrulama:** Tamper/stale/size/authority-field negatif testleri.

---

## ADR-012 — Self-learning kontrollü aday yaşam döngüsüdür

**Durum:** Accepted  
**Karar:** Sistem hata ve tekrarları learning candidate olarak kaydeder; aktif memory/skill/policy’yi otomatik değiştirmez.

**Gerekçe:** Bir model gözlemi kalıcı kural için güvenilir değildir; kötü öğrenim tekrarları büyütür.

**Sonuçlar:**

- En az iki bağımsız gözlem.
- Ayrı evaluator/verifier.
- Project fixture ve metric.
- Exact registry mutation planı ve rollback.

**Doğrulama:** Tek gözlem, self-promotion, proposer=evaluator/verifier ve failed evaluation negatif testleri.

## ADR uygulama kuralı

Her ADR repository’de şu metadata’yı taşımalıdır:

```yaml
status: proposed | accepted | superseded | deprecated
owners: []
decision_date:
review_triggers: []
related_work_items: []
related_tests: []
supersedes: null
superseded_by: null
```

Bir acceptance testi olmayan ADR, yalnız yönlendirici karar olarak kalır; “uygulandı” sayılmaz.
