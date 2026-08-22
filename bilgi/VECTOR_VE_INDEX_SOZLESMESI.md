# Vector ve Index Sözleşmesi

## İlk profil

```yaml
profile_id: bge-m3-dense-1024-cosine-v1
model_ref: openai/BAAI/bge-m3
dimension: 1024
distance: cosine
query_prefix: ""
passage_prefix: ""
normalization: provider-verified
```

Prefix gerçek değerleri golden A/B sonrası policy revision ile değişebilir. Değişiklik yeni
profile ID ve re-index üretir.

## Chunk identity

```text
project/source/version
relative locator
content unit/range
raw content hash
embedding text hash
parser profile
chunker profile
embedding profile
chunk digest
```

Vector text kalıcı source truth değildir. İhtiyaca göre chunk content PostgreSQL veya
normalized artifact reference ile saklanır; secret/sensitive content embed edilmez.

## PostgreSQL yönü

- `vector(1024)` active profile-specific table/partition veya strict dimension check.
- HNSW cosine index.
- FTS `tsvector` GIN.
- identifiers array GIN.
- path/symbol `pg_trgm`.
- project/source_type filters.
- index metadata current source/profile digest ile eşleşir.

Farklı dimension aynı indexed column'da karıştırılmaz. Yeni embedding model için ayrı physical
profile store/partition/table adapter planı hazırlanır.

## HNSW ve filtre

Approximate index filtering sonrası candidate kaybı olabilir. Zekam:
- exact baseline query,
- approximate query,
- Recall comparison,
- iterative scan,
- higher ef_search,
- partial index/partition

seçeneklerini corpus/filter dağılımıyla ölçer. Varsayılan sabit evrensel değildir.

## Incremental rebuild

Unchanged file/chunk:
- source/file/content hash,
- parser/chunker/profile digest

eşleşirse verified vector reuse. Source binding revision değişirse index metadata new binding'e
bağlanmadan query olmaz. Replacement stage DB integrity/count/digest kontrolünden sonra atomic
publish edilir.

## Sensitive boundary

Detector policy:
- credentials/private keys/tokens/connection strings
- personal/machine absolute paths where prohibited
- binary/dump/log/env
- generated/vendor/build

Matching value loglanmaz veya embed edilmez. Detector policy digest değişince index stale.

## Embedding provider

Local/internal route preference policy ile. Remote:
- exact provider request,
- data categories,
- retention assumptions,
- session authorization,
- SecretRef,
- source remote eligibility.

Dimension/finite/norm contract check olmadan vector persist edilmez.
