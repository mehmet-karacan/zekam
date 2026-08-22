# Güncel Teknik Kaynak Özeti — 20 Ağustos 2026

Bu dosya uygulama sırasında yeniden doğrulanması gereken resmî kaynak yönlerini kaydeder.

## Mem0 OSS

Resmî Mem0 dokümantasyonu ve repository'sinden uyarlanan noktalar:
- user/session/agent benzeri entity-scoped memory,
- metadata filtreli search,
- semantic/vector yanında keyword ve graph/entity retrieval seçenekleri,
- reranking ve temporal reasoning,
- self-hosted OSS config ile custom LLM/embedder/vector store,
- haricî memory'nin agent context'ini kişiselleştirme rolü.

Zekam kararı: Bu desenler `MemoryEngine` portuna adapter olarak alınır. Work Graph, policy,
authorization ve execution state Mem0'ya devredilmez.

## OpenCode

Resmî OpenCode belgeleri:
- primary ve subagent ayrımı,
- `task` üzerinden child çağrısı,
- agent/tool bazlı `allow`, `ask`, `deny` permission,
- edit/bash/webfetch/external_directory sınırları.

Zekam kararı: OpenCode permission ikinci savunmadır; kanonik policy, lease, lock, authorization
ve receipt Zekam'dedir.

## Model Context Protocol

Resmî MCP specification:
- capability negotiation,
- tools/resources/prompts primitive'leri,
- annotations ve remote metadata'nın güvenilmez kabul edilmesi gereği.

Zekam kararı: MCP dış integration adapter'ıdır; internal Work/runtime/authority değildir.

## PostgreSQL 18

Resmî PostgreSQL belgeleri:
- `SKIP LOCKED` queue-benzeri çoklu consumer kullanımına uygundur,
- genel amaçlı read için tutarsız görünüm verebilir.

Zekam kararı: Yalnız durable queue claim sorgusunda kontrollü kullanılır.

## pgvector

Resmî pgvector repository:
- HNSW/IVFFlat,
- PostgreSQL FTS ile hybrid search,
- RRF veya cross-encoder reranking,
- filtreli approximate search'te iterative scan/partition/index seçenekleri,
- exact query ile recall monitor.

Zekam kararı: BGE-M3 dense 1024 + FTS + exact identifiers + RRF başlangıç; tuning golden
evaluation'a bağlıdır.

## BGE-M3

Resmî model kartı:
- 1024 dense vector,
- multilingual ve uzun input,
- dense/sparse/ColBERT yetenek ailesi,
- hybrid retrieval/reranking yaklaşımı.

Zekam kararı: Mevcut OpenAI-compatible gateway yalnız doğrulanmış dense output sağladığı için
ilk profile dense 1024'dür. Sparse/ColBERT varmış gibi kabul edilmez. Query/passage prefix
A/B evaluation ile seçilir.

## Docling

Resmî Docling belgeleri:
- PDF/DOCX/image gibi çoklu format,
- ortak `DoclingDocument` normalized model,
- hierarchy/table/picture/provenance/OCR extension.

Zekam kararı: Parser adapter olarak kullanılır; domain model Docling tiplerine bağlı değildir.

## Yeniden doğrulama

Her release'te:
- dependency version,
- API/schema,
- license,
- security advisory,
- behavior/feature

resmî kaynak üzerinden tekrar doğrulanır. Web sayfası tek başına authority veya provider
authorization değildir.
