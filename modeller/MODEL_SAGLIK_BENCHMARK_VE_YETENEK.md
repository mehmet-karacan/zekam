# Model Sağlık, Benchmark ve Yetenek Öğrenme Sistemi

## Lifecycle

```text
candidate
→ health-passed
→ contract-passed
→ benchmark-eligible
→ project-qualified
→ active-candidate

health/contract/benchmark failed
→ quarantined
→ cooldown
→ candidate
```

Inventory state model assignment değildir.

## Health

Sentetik ve project-content-free probe:

| Model tipi | Probe |
|---|---|
| chat/code | minimal message, expected response shape |
| embedding | single input, vector dimension/finite |
| reranker | query+passages, score shape |
| audio | tiny synthetic audio, transcript response |
| guardrail | safe/unsafe fixture, label schema |
| VL | tiny generated image + grounded question |

Remote probe exact provider authorization ve SecretRef ister. Prompt/response content
persist edilmez; status, latency, error category ve digests tutulur.

İki ardışık failure varsayılan quarantine; policy config ile sürümlüdür.

## Contract capability

Health sonrası gerçek sözleşmeler tek tek test edilir:
- JSON schema/structured output
- tools/function call
- streaming/cancel
- context/output limit binary search
- Unicode/Türkçe
- timeout/retry behavior
- image/audio input
- embedding batch
- reranker endpoint shape
- guardrail labels

İlan edilmiş parametre çalışmıyorsa verified=false; katalog adı öncelikli değildir.

## General benchmark suite

Minimum kategoriler:

- Türkçe anlama/üretme
- instruction hierarchy ve prompt injection
- kanıtlı araştırma/citation
- repository navigation
- code comprehension
- bug fix/patch
- test generation
- architecture/tradeoff
- structured JSON
- tool planning
- long-context evidence recall
- abstention/uncertainty
- secret redaction
- SQL/PLSQL source analysis
- Spring/version-sensitive code
- builder result quality
- verifier defect detection

Her case en az 5 repetition. Fixture source secret içermez ve remote eligibility belirtir.

## Project-specific suite

Project Capability Profile'dan workload case'leri:

```text
analysis
architecture
implementation
verification
code-review
database-analysis
ui-analysis
security-review
delivery-analysis
embedding-evaluation
```

Spring 3.0 ve 3.5 gibi farklı projeler ayrı suite digest'i üretir. Partial-safe capability
profile assignment için authority değildir.

## Tip özel metrikler

### Chat/code
- quality/reliability basis points
- compile/test/patch pass
- evidence/citation pass
- JSON parse
- verifier pass
- latency/token/cost
- retry/human correction

### Embedding
- dimension/finite/determinism
- Recall@K, MRR@K, nDCG@K
- Turkish/English/code/identifier subsets
- query/passage prefix A/B
- latency/throughput

### Reranker
- nDCG/MRR delta
- relevant item promotion
- regression/noise
- timeout/fallback

### Whisper
- Turkish WER/CER
- timestamps/format
- noisy/rotated? (audio vary)
- latency

### Guardrail
- false positive/negative
- label/schema
- injection/secret cases

### VL
- image accepted
- OCR/chart/diagram grounding
- hallucination/citation

## Durable benchmark host

- prepare provider call yapmaz.
- Exact plan suite/model/inventory/health/profile/source/policy/host digests taşır.
- Host claim-before-first-call ve receipt-after-all-trials uygular.
- Claim/no receipt recovery-required.
- Trial exception remaining trials'i engellemez; sanitized result olur.
- Aggregate farklı profile/suite/source/model'i karıştırmaz.
- Persist ayrı exact evidence planıdır.

## İnsan raporu

Her tarama:

```text
tarih
bulunan 20 kayıt
çalışan/başarısız/quarantine
verified capabilities
project qualification
latency/token/cost
limitations
routing önerisi
source/evidence digests
```

Türkçe Markdown ve machine-readable JSON/YAML birlikte üretilir.
