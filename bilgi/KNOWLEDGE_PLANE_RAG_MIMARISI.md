# Zekam Knowledge Plane ve RAG Mimarisi

## Amaç

Projeler, belgeler, kod, veritabanı metadata'sı, Work/Research/Decision kayıtları ve reviewed
memory üzerinde kaynak gösteren, sürümlü, yeniden indekslenebilir ve açıklanabilir retrieval
sağlar.

## Kanonik/derived ayrımı

Kanonik:
- source identity/revision,
- immutable artifact metadata/digest,
- normalized content/version,
- citation/provenance,
- approved knowledge record.

Derived:
- chunk,
- FTS vector,
- embedding,
- HNSW/GIN/trigram index,
- retrieval cache,
- dashboard projection.

Derived kayıp olduğunda source/normalized artifact'tan rebuild edilir.

## Kaynak modeli

```text
KnowledgeSource
SourceVersion
SourceArtifact
NormalizedSource
ContentUnit
Chunk
EmbeddingProfile
ChunkEmbedding
Citation
RetrievalRun
EvaluationRun
```

SourceVersion aktif olmadan query'ye girmez. Yeni version tamamen ingest/index/eval smoke geçer,
sonra atomik activate edilir. Önceki version rollback window boyunca korunur.

## ContentUnit

Unit türleri:

```text
heading paragraph list_item table code formula image image_caption
ocr_text page_break file_header symbol configuration database_object
```

Locator:
- page/bbox,
- heading path/block,
- relative path/line/symbol,
- schema/object/dependency,
- source snapshot/revision.

Parser doğrudan chunk üretmez. Ortak normalized JSON ve isteğe bağlı Markdown artifact üretir.

## Storage

- PostgreSQL: metadata, normalized unit (veya object refs), FTS, vector metadata, citation.
- Object storage: original bytes, normalized JSON/MD, page images, OCR artifacts, large patch/report.
- Redis: opsiyonel short cache/wakeup; state değil.

## Project scope

Varsayılan query tek project. Cross-project açık list/scope ve policy ister. Her hit project_id,
source revision, domain, authority class ve freshness taşır.

## Source drift

File/repository/DB metadata revision değişirse:
- ilgili normalized/index stale,
- query unsafe content'i kullanmaz,
- domain `blocked-stale` veya partial unavailable raporlar,
- reindex/reintegration next action üretir.

## Knowledge promotion

Research report veya model synthesis otomatik knowledge değildir. Promotion:
- evidence-complete,
- source current,
- contradiction review,
- exact normalization,
- user-data authorization

ister. Memory promotion ayrıca Memory Gate'ten geçer.

## Context assembly

Retriever hit döndürür; Context Compiler:
- Work hedefi,
- required evidence,
- authority/freshness,
- duplicate,
- token budget

ile seçim yapar. Raw source instruction untrusted data olarak işaretlenir.
