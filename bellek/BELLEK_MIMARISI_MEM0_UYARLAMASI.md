# Zekam Bellek Mimarisi ve Mem0 Uyarlaması

## Karar

Zekam bellek altyapısı provider-neutral `MemoryEngine` portu üzerinden uygulanır.

```text
NativePostgresMemoryEngine — zorunlu, üretim için kanonik ve tam özellikli
Mem0OssMemoryEngine        — opsiyonel self-hosted adapter
InMemoryMemoryEngine       — yalnız test
```

Mem0'dan alınacak desenler:
- user/agent/run scoped memory,
- metadata filtreli semantic search,
- graph/entity ilişkileri,
- temporal validity,
- consolidation/deduplication,
- reranking ve hybrid retrieval,
- agent-specific memory.

Alınmayacak davranış:
- Work/Run/Policy/Authorization state'ini haricî memory'ye devretmek,
- ham transcript'i sınırsız kalıcılaştırmak,
- model cümlesini otomatik “gerçek” yapmak,
- geçmişi overwrite etmek,
- secret veya private reasoning saklamak.

## Bellek katmanları

### Working memory
Aktif run için bounded ve kısa ömürlü context. Checkpoint ile ilişkili ancak kanonik Work
state değildir. TTL ve token budget taşır.

### Episodic memory
Bir işte ne denendi, hangi sonuç/kanıt oluştu, hangi model/tool kullanıldı. Work/Run/Evidence
referansına bağlıdır.

### Semantic memory
Doğrulanmış proje bilgisi: teknoloji, davranış, domain kuralı, DB object ilişkisi. Source
revision, evidence ve validity taşır.

### Procedural memory
Kanıtlanmış yöntem, runbook, test stratejisi, recovery adımı. En az bağımsız review ve
başarılı fixture gerektirir.

### Preference memory
Kullanıcının açıklanmış tercihleri: rapor dili, commit stili, çalışma şekli. Güvenlik policy
yerine geçmez ve kullanıcı tarafından geri alınabilir.

### Failure memory
Başarısız yaklaşım, root cause, ortam, hata category/digest, çözüm veya kaçınma kuralı.
Tekrarlı aynı probleme tekrar düşmeyi azaltır.

## Scope

```text
global-user
project:<id>
work-item:<id>
run:<id>
agent:<logical-id>
```

Her record realm/user scope taşır. Varsayılan query tek project ve current user kapsamındadır.
Multi-project yalnız explicit scope ve policy ile.

## Memory record

```text
memory_id
class
scope
subject/entity keys
content/summary digest
evidence refs
source revisions
valid_from/valid_until
confidence
status
revision
supersedes/conflicts
created_by/reviewed_by
usage_count/last_used_at
retention_review_at
embedding profile
record digest
```

Public record absolute path, credential, raw prompt/output veya private reasoning içermez.

## Authority sınırı

Memory:
- current Work state belirleyemez,
- plan/approval/lease veremez,
- source revision yerine geçemez,
- model assignment zorlayamaz,
- policy restriction gevşetemez.

Context Compiler memory'yi yardımcı evidence adayı olarak seçer ve source/validity kontrolü
yapar.

## Native PostgreSQL tasarım yönü

Önerilen tablolar:

```text
memory.memory_head
memory.memory_revision
memory.memory_evidence
memory.memory_relation
memory.memory_embedding
memory.memory_usage
memory.memory_candidate
memory.memory_review
memory.hygiene_run
memory.hygiene_finding
memory.external_sync
```

- Head current revision'ı işaretler.
- Revision append-only.
- Embedding profile ayrı.
- Relation typed entity/causal/duplicate/conflict/supersedes.
- RLS realm/user isolation.
- FTS + pgvector + relation + temporal query.
- Candidate/review transactionally promotion üretir.

## Mem0 adapter

Adapter:
- Zekam canonical memory ID/revision/digest'ini metadata olarak yollar.
- `user_id`, `agent_id`, `run_id` eşlemesini Zekam scope'tan üretir.
- Secret/source content policy'yi Zekam tarafında uygular.
- Mem0 result'ını untrusted external projection olarak parse eder.
- Native record ile digest/source uyuşmuyorsa stale/conflict yapar.
- Mem0 unavailable olduğunda native operation devam eder; `external_sync=pending|failed`.
- Haricî delete Zekam revision geçmişini silmez.
- Mem0 config/endpoint/credential yalnız SecretRef ve local config'ten gelir.

## Hybrid memory search

1. exact subject/entity/work/source ID
2. metadata/temporal filter
3. PostgreSQL FTS
4. BGE-M3 vector
5. entity/relation expansion
6. RRF veya açıklanabilir fusion
7. authority/freshness/validity rerank
8. duplicate suppression
9. token budget

Her sonuç selection reason, revision, evidence ve freshness taşır.
