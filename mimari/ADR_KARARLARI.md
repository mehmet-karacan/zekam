# Zekam Başlangıç ADR Kararları

## ADR-001 — Modüler monolit

**Karar:** Tek domain/application çekirdeği, ayrı process'ler.  
**Neden:** Dağıtık transaction ve çift state karmaşasını erken aşamada önler.  
**Review trigger:** Tek process sınırının ölçülmüş throughput/SLA'yı karşılamaması.

## ADR-002 — PostgreSQL 18 kanonik store

**Karar:** Work, runtime ledger, model evidence, research, memory ve scheduler PostgreSQL'de.  
**Neden:** Transaction, constraint, RLS, queue claim ve audit tek yerde.  
**Review trigger:** Kanıtlanmış ölçek veya regülasyon zorunluluğu.

## ADR-003 — pgvector aynı veri katmanında

**Karar:** İlk vector store PostgreSQL+pgvector.  
**Neden:** Source/revision/filter ile transactionally consistent metadata.  
**Review trigger:** Golden eval ve operasyon ölçümleriyle yetersizlik.

## ADR-004 — Agent harness core özelliğidir

**Karar:** DAG/queue/lease/fence/lock/claim/receipt/checkpoint/result/verifier ayrı framework'e
bırakılmaz.  
**Neden:** Model ve CLI geçici; durable execution sözleşmesi kalıcıdır.

## ADR-005 — Agentic iş minimum bir subagent

**Karar:** Koordinatörden ayrı en az bir child. Sabit maksimum yok.  
**Neden:** İş bölümü ve doğrulanabilir child sonucu sağlarken küçük işi gereksiz çoğaltmaz.

## ADR-006 — Tek builder

**Karar:** Aynı yazılabilir logical resource'a bir builder.  
**Neden:** Çakışma, duplicate effect ve merge belirsizliğini önler.

## ADR-007 — Provider-neutral model gateway

**Karar:** Model ID, client, provider route, quota pool ve secret reference ayrıdır.  
**Neden:** Alias/model/protokol aynı olmayabilir ve zamanla değişir.

## ADR-008 — BGE-M3 1024 ilk embedding profili

**Karar:** Mevcut kurum içi BGE-M3 dense 1024 korunur; profile version/digest zorunludur.  
**Neden:** İlk güvenilir baseline ve mevcut altyapı. Sparse/ColBERT ayrıca doğrulanır.

## ADR-009 — MemoryEngine portu

**Karar:** Native PostgreSQL engine zorunlu, Mem0 OSS opsiyonel adapter.  
**Neden:** Memory vendor lock-in ve ikinci authority oluşturmaz.

## ADR-010 — Exact authorization ve one-shot effect

**Karar:** Read-only otomatik olabilir; yan etki exact plan, scope ve expiry ile yetkilendirilir.  
**Neden:** Her child step'te gereksiz onay olmadan güvenlik.

## ADR-011 — External source no-write

**Karar:** Mutation registry'de bagli gercek source rootunda ve tek-writer kilidiyla yapilir;
kopya, mirror veya detached worktree uretilmez.
**Neden:** Projeleri korur ve rollback/verification sağlar.

## ADR-012 — Commit Türkçe anlamlı ASCII

**Karar:** Başlık ve gövde Türkçe anlam taşır, yalnız ASCII kullanır.  
**Neden:** Kurumsal okunabilirlik ve platform uyumu.

## ADR-013 — Zekam tekil kanonik kimliktir

**Karar:** Package, CLI, environment, home, schema ve DB yüzeyinde yalnız Zekam kullanılır.
**Neden:** Birden fazla ürün namespace'i ve sessiz fallback oluşmasını önler.
