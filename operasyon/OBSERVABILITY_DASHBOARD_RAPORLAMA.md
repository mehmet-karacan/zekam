# Observability, Dashboard ve Raporlama

## Telemetry vs state

Telemetry kanonik Work/Run/Receipt değildir. OpenTelemetry trace/metric/log kaybı ürün state'ini
kaybettirmez. Dashboard derived query/projection kullanır.

## Correlation

Her event/metric:

```text
realm_id
project_id
work_item_id
plan_revision_id
run_id
step_id
job/attempt
execution_identity
model_assignment_id
request/trace/correlation_id
source revision
policy digest
```

uygun olanları taşır. Secret/raw source/model output loglanmaz.

## Metrikler

### Runtime
- queue depth/age
- claim latency
- active/expired lease
- lock conflict
- step duration
- retry/recovery
- verifier pass/fail
- receipt completion
- cancellation.

### Model
- health/quarantine
- quality/reliability
- p50/p95 latency
- input/output tokens
- cost
- quota observation
- retry/human correction
- route/fallback.

### Knowledge
- ingest stage/duration/failure
- parser/OCR
- chunks/vectors
- cache
- retrieval channel/candidate
- Recall/MRR/nDCG
- no-answer
- citation.

### Memory
- candidate/promotion/revoke
- search utility
- stale/duplicate/conflict
- selected/used tokens
- verifier correlation
- Mem0 sync.

### Security
- denied authorization
- outbound/provider
- secret resolution metadata
- path/network violations
- prompt injection detection
- audit anomaly.

## Log

Structured JSON, sanitized error category/digest. Local secure diagnostics ayrı access control.
Full prompt/source/credential default log yok.

## Dashboard minimum sayfaları

1. Genel sağlık
2. Projeler
3. Bugünkü işler / Work Graph
4. Runs/DAG/agents/queue/locks/recovery
5. Modeller/health/benchmark/quota/routing
6. Knowledge sources/index/retrieval/eval
7. Memory/learning/skills/hygiene
8. Scheduler/gece işleri/raporlar
9. Security/authorization/outbound
10. Backup/release.

İlk sürüm read-only. Mutation action dashboard'dan yapılırsa aynı application service exact
plan/approval gate'ini kullanır.

## Sinaps/graph görünümü

Derived graph:
- project/work/decision/source/citation/memory/model/agent ilişkileri,
- authority ve freshness styling,
- active runtime ownership ayrı,
- click-through canonical record.

Graph DB zorunlu değildir; PostgreSQL relation/projection ile başlanır. Graph'ta edge olması
authority kanıtı değildir.

## İnsan raporları

Türkçe ve anlaşılır:
- günlük,
- proje,
- model,
- araştırma,
- release,
- incident,
- memory hygiene.

Machine-readable JSON/YAML eşlik eder. Markdown otomatik “tamamlandı” uydurmaz.
