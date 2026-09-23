# Observability ve Raporlama

## Telemetry vs state

Telemetry kanonik Work/Run/Receipt değildir. OpenTelemetry trace/metric/log kaybı ürün state'ini
kaybettirmez. Gözlem/reporting projection'ları kanonik state değildir.

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
