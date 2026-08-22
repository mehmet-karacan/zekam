# Hybrid Retrieval, Reranking ve Citation

## Niyet çözümleme

Önce query intent:
- status/history → Work Graph
- exact ID/number → exact lookup
- code/path/symbol → code exact+hybrid
- DB object → metadata exact+dependency
- document/content → knowledge hybrid
- memory/how-we-solved → reviewed memory
- multi-project → explicit scope

Status sorgusu vector ile yanıtlanmaz.

## Candidate kanalları

1. exact identifier/project/work/document/object
2. path/symbol exact/trigram
3. alias/entity
4. PostgreSQL FTS
5. dense BGE-M3 1024
6. dependency/relation expansion

Her kanal ayrı rank listesi üretir.

## Fusion

Başlangıç RRF:

```text
rrf_score = Σ 1 / (rrf_k + rank_channel)
```

Config sürümlü ve evaluation-bound'dır. Raw cosine/ts_rank doğrudan toplanmaz. Exact match
boost açık reason code taşır.

## Reranker

Port:
- Noop
- kurum içi BGE reranker
- future cross-encoder

Reranker:
- fusion top-N üzerinde,
- timeout/failure'da fusion fallback,
- model ID/health/profile/authorization evidence,
- source data remote eligibility,
- final rank ve score ayrı.

## Dedupe ve expansion

- exact content hash duplicate → bir canonical evidence weight
- aynı source farklı revision → duplicate değil, version conflict olabilir
- selected child → parent/neighbor/header expansion
- table rows → header
- code body → signature/file header
- token budget ve repeated content suppression

## No-answer

Üç davranış:
- deterministic chit-chat
- evidence sufficient
- document/project question but insufficient evidence

Dense threshold tek karar değildir. Evidence sufficiency exact/lexical/dense/reranker, source
freshness, coverage ve contradiction'dan policy ile hesaplanır. Yetersizse açık abstention.

## Citation packet

```text
label
source/project/document identity
source revision
locator
snippet/content digest
retrieval channel/rank
reranker rank
evidence role
```

LLM'e `[S1]` gibi bounded evidence blocks gider. Cevap citation label'larını kullanır; answer
service label→canonical citation mapping'i doğrular. Kaynakta olmayan label reddedilir.

## Citation tipleri

- PDF page/bbox
- DOCX heading path/block/table cell
- OCR page/bbox/confidence
- code repo/path/symbol/line/commit
- DB logical connection/schema/object/kind/revision
- research URL/source snapshot/locator/digest
- Work exact item/revision/event.

## Explain

`zekam knowledge explain`:
- intent
- selected channels
- filters
- candidate ranks/scores
- RRF contributions
- reranker
- dedupe/expansion
- omitted by budget/stale
- final context/citations
- profile/policy digests.

## Evaluation

- Recall@1/3/5/10
- MRR@10
- nDCG@10
- identifier recall
- citation accuracy/coverage
- no-answer FP/FN
- latency p50/p95
- token per verified answer
- HNSW exact-vs-approx recall.

Index tuning yalnız bu sonuçlarla yapılır.
