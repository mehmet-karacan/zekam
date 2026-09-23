---
schema: zekam-active-task/v2
task_id: ZEKAM-CONTEXT-GRAPH-001
status: APPROVED_ACTIVE_TASK
title: Zekam Context Graph ve Graph-Aware Retrieval Entegrasyonu
created_at: 2026-09-21T23:13:00+03:00
baseline_repository: mehmet-karacan/zekam
baseline_branch: main
baseline_head: 68b4833ff959185e5155a28680658ccbd96dd997
push_authorized: false
legacy_postgresql_data_import: FORBIDDEN
postgresql_runtime_dependency: FORBIDDEN
docker_required_for_zekam_core: false
---

# Zekam Context Graph ve Graph-Aware Retrieval Entegrasyonu

## 1. Hedef

Zekam’ın mevcut project RAG sistemine, Graft projesindeki başarılı context-graph prensiplerinden yararlanan ancak Zekam’ın kendi güvenlik, generation, SQLite, provenance ve rollback kurallarına uyan yerli bir **Context Graph Engine** ekle.

Bu görev Graft paketini dependency olarak kurmaz ve Graft’ın file-based graph cache’ini authority yapmaz.

Hedef sonuç:

```text
Exact + FTS5 + sqlite-vec
        |
        v
       RRF
        |
        v
Graph-aware structural reranker
        |
        v
Distinct-file context selection
        |
        v
Bounded context pack
```

Ayrıca:

- code outline,
- blast radius,
- repo map,
- graph freshness

yüzeyleri oluşturulur.

## 2. Değişmez Tasarım Kararları

1. Mevcut knowledge index ve RAG baseline çalışmaya devam edecek.
2. Graph ayrı ve rebuildable bir SQLite projection olacak.
3. `knowledge.sqlite3` ilk sürümde graph tablolarıyla migrate edilmeyecek.
4. Graph authority değildir.
5. Graph provider-free oluşturulabilmelidir.
6. İlk extractor Python stdlib `ast` olacaktır.
7. İlk sürüm yeni runtime Tree-sitter dependency eklemeyecektir.
8. Tree-sitter daha sonra adapter olarak eklenebilir.
9. İlk sürümde `RetrievalChannel` enum’una `GRAPH` ekleme.
10. Mevcut `RetrievalService.reranker` extension point’ini kullan.
11. Graph stale/unavailable/corrupt ise baseline RAG davranışı korunmalıdır.
12. Exact identifier sonucu graph nedeniyle düşürülemez.
13. `contains` dependency traversal veya PageRank edge setine giremez.
14. Line number kalıcı symbol identity olamaz.
15. Human annotation generated içerikten ayrı tutulmalıdır.
16. Naive one-hop expansion varsayılan kapalıdır.
17. Benchmark iyileşme göstermeden graph reranker default-on olamaz.
18. Push yapılmayacak.

## 3. Ön Koşullar

Uygulamadan önce:

1. `00_BASLA.md` protokolünü uygula.
2. Repository HEAD’in baseline’dan ilerlediğini görürsen stale plan üret ve kapsamı yeniden bağla.
3. `python scripts/paket_dogrula.py` çalıştır.
4. Mevcut project RAG baseline testlerini belirle.
5. Graph ile değişecek logical resource setini çıkar.
6. Uygulama planı, test planı ve rollback planı üret.
7. İş agentic ise gerçek subagent kullan.

## 4. Yeni Dosyalar

### Domain

`src/zekam/domain/code_graph.py`

Aşağıdaki immutable contract’ları tanımla:

- `GraphNodeKind`
- `GraphRelation`
- `GraphConfidence`
- `GraphFile`
- `GraphSymbol`
- `GraphEdge`
- `GraphGeneration`
- `GraphImpactHit`

V1 relation set:

- contains
- imports
- calls
- references
- extends

### Application

`src/zekam/application/code_graph.py`

- `CodeGraphPort`
- `CodeGraphExtractor`
- `CodeGraphBuildPlan`
- graph generation/build orchestration

`src/zekam/application/code_graph_python.py`

- Python AST extractor
- file/symbol/raw-edge üretimi
- deterministic qualified names
- body digest

`src/zekam/application/code_graph_ranking.py`

- graph seed mapping
- Personalized PageRank
- dependency edge allowlist
- distinct-file selection
- graph reranker

`src/zekam/application/code_graph_query.py`

- find
- outline
- impact
- map
- freshness

### Infrastructure

`src/zekam/infrastructure/sqlite/code_graph.py`

SQLite schema ve repository:

- metadata
- graph_generation
- current_graph_generation
- graph_file
- graph_symbol
- graph_edge
- graph_file_fts
- graph_chunk_link
- graph_annotation

Aynı security posture mevcut `SQLiteKnowledgeIndex` ile uyumlu olmalı:

- absolute private path,
- symlink rejection,
- single writer,
- read-only immutable mode,
- integrity checks,
- atomic generation publication.

## 5. Graph Store Konumu

Aşağıdaki logical layout kullan:

```text
ZEKAM_HOME/
  knowledge-index/
    graph/
      <project-slug>/
        code-graph.sqlite3
```

Absolute path kanonik kayda yazılmayacak.

## 6. Generation Contract

Graph generation en az şunlara bağlıdır:

- project_id
- source_revision
- tree_digest
- source_manifest_digest
- extractor_profile_digest
- deterministic graph content

State:

- building
- ready
- superseded

Yeni generation tamamen doğrulanmadan `current_graph_generation` pointer’ı değiştirilmez.

## 7. Python AST Extraction

V1’de destekle:

- module
- class
- function
- async function
- method
- nested function
- import
- from import
- inheritance
- local resolvable call
- references

Confidence:

- extracted
- inferred
- external
- unresolved

Parser syntax error durumunda bütün graph generation sessizce başarılı sayılmayacak. Dosya parse state ve hata sayısı kaydedilecek; acceptance policy’ye göre generation fail veya degraded olabilir.

## 8. Incremental Reuse

File cache identity:

```text
relative_path + content_digest + extractor_profile_digest
```

Aynıysa tekrar parse etme.

Symbol semantic reuse:

```text
stable_symbol_identity + body_digest + semantic_profile_digest
```

Aynıysa summary/crux yeniden üretme.

V1 structural graph LLM çağırmayacak.

## 9. Retrieval Entegrasyonu

Mevcut `RetrievalService` protocol’ünü kırma.

`ProjectGraphReranker` oluştur.

Kullanım şartı:

- graph project_id == knowledge project_id
- source_revision eşit
- tree_digest eşit
- graph state ready

Şart sağlanmıyorsa:

- reranker devre dışı,
- baseline sonucu korunur,
- trace graph state’i bildirir.

## 10. PageRank

Personalized PageRank dependency edge seti:

- calls
- references
- imports
- extends

`contains` hariç.

Başlangıç parametreleri config/constant olabilir:

- alpha: 0.25
- iterations: 25

Ancak default-on kabulü benchmark ile verilecek; parametreleri mutlak doğru varsayma.

## 11. Distinct-file Selection

Bounded context’te aynı dosyanın sibling hit’leri diğer relevant dosyaları boğmamalı.

Kural:

1. original top hit korunur,
2. distinct-file leader’lar önce gelir,
3. sibling hit’ler sonraki turlarda gelir,
4. exact-match hit düşürülemez.

## 12. Whole-file Lexical Prior

`graph_file_fts` ile file path + symbols + body için FTS5 index kur.

İlk sürümde bu sinyal benchmark flag arkasında tutulabilir.

Chunk FTS’nin yerine geçmez.

## 13. One-hop Expansion

İlk release default kapalı.

Yalnız benchmark:

- Recall@10 iyileştiriyor,
- MRR/nDCG gerilemiyor,
- token budget kabul sınırında,
- false-positive inflation kontrollü

ise açılabilir.

## 14. CLI

`project` komut grubu altında ekle:

```text
zekam project graph plan <alias> --json
zekam project graph build <alias> --plan-digest <digest> --uygula --json
zekam project graph status <alias> --json
zekam project graph check <alias> --json
zekam project graph find <alias> "<query>" --json
zekam project graph outline <alias> <relative-file> --json
zekam project graph impact <alias> <symbol> --json
zekam project graph map <alias> --json
```

`plan` mutation yapmaz.

`build` exact plan digest ister.

## 15. MCP / Agent Tool Set

En fazla şu beş graph tool’u expose et:

- `zekam_code_find`
- `zekam_code_outline`
- `zekam_code_impact`
- `zekam_code_map`
- `zekam_code_freshness`

Tool sayısını gereksiz büyütme.

## 16. Mevcut Dosya Entegrasyonları

### `src/zekam/application/embedded_project_rag.py`

- optional graph reranker composition
- stale/unavailable graph trace
- baseline fallback

### `src/zekam/application/project_rag_query.py`

- searched graph state
- graph freshness
- reranker used flag
- fallback reason

### `src/zekam/application/retrieval_service.py`

İlk sürümde protocol kırma.

Gerekirse yalnız backward-compatible trace metadata ekle.

### `src/zekam/application/project_knowledge_index.py`

Aynı verified source discovery’den graph planın güvenli yararlanabilmesini sağla.

Knowledge indexing behavior’ını değiştirme.

### `src/zekam/interfaces/cli/project.py`

Graph subcommand registration.

### `src/zekam/interfaces/cli/mcp.py`

Bounded tool exposure.

### `docs/ZEKAM_YETKINLIK_ENVANTERI.md`

İlk durumda:

`Project Code/Context Graph = partial`

olarak ekle.

`ready` ancak benchmark + end-to-end agent kabulünden sonra.

### `README.md`

Kısa graph kullanımı.

## 17. Benchmark

Baseline’ı değiştirmeden önce ölç.

Varyantlar:

1. Exact + FTS
2. Exact + FTS + Vector
3. Baseline + file diversity
4. Baseline + graph rerank
5. Baseline + graph rerank + file FTS
6. Baseline + graph rerank + bounded one-hop

Metrikler:

- Recall@1
- Recall@5
- Recall@10
- MRR
- nDCG@10
- distinct-file coverage
- tokens/context
- p50/p95 query latency
- cold graph build
- one-file-change rebuild
- exact-match preservation

Acceptance:

- exact behavior regression yok,
- kritik retrieval metriği gerilemiyor,
- en az bir kalite metriğinde gerçek iyileşme,
- p95 kabul sınırı içinde,
- token budget kötüleşmiyor veya ölçülmüş net fayda var.

## 18. Zorunlu Testler

Yeni unit test aileleri:

- code graph domain
- Python extractor
- SQLite graph store
- graph generation atomicity
- incremental reuse
- PageRank
- file diversity
- impact traversal
- graph/knowledge generation binding
- stale fallback
- CLI plan/apply
- MCP tools

Negatif testler:

- corrupt graph
- source tree drift
- same-size file edit
- symlink path
- duplicate symbol
- cyclic graph
- unresolved call
- concurrent writer
- stale plan digest
- wrong project graph
- knowledge generation mismatch

## 19. Rollback

Graph entegrasyonu mevcut RAG’den bağımsız olmalı.

Rollback:

1. graph reranker kapat,
2. graph MCP tools kaldır,
3. baseline project RAG’e dön,
4. derived `code-graph.sqlite3` silinebilir,
5. knowledge/operational DB değişmeden kalır.

Rollback için source migration veya data restore gerekmesi tasarım hatası sayılır.

## 20. Tree-sitter Fazı

V1 tamamlanmadan Tree-sitter dependency ekleme.

V2 adapter sonrası:

- Python parity
- TS/JS/Java/Go
- parser version fingerprint
- optional dependency group

değerlendir.

## 21. Oracle PL/SQL Fazı

PL/SQL desteği ayrı acceptance campaign’idir.

Aşağıdaki relation’ları hedefle:

- contains
- calls_procedure
- calls_function
- reads_table
- writes_table
- uses_sequence
- uses_synonym
- trigger_on
- depends_on_package
- executes_dynamic_sql

Grammar production-ready sayılmadan önce gerçek kullanıcı corpus’unda error-rate ve edge correctness ölç.

Dynamic SQL’den kesin dependency uydurma.

## 22. Concept Graph ve Semantic Enrichment

Bu görevde structural foundation tamamlandıktan sonra uygulanabilir.

Semantic alanlar:

- summary
- crux
- summary_state
- semantic_profile_digest

Provider çağrısı structural graph için zorunlu olamaz.

Human annotation generated summary’den ayrı tutulur.

## 23. Global Definition of Done Ek Kapıları

Bu görev tamamlandı denebilmesi için:

- mevcut project RAG testleri geçer,
- yeni graph testleri geçer,
- ruff geçer,
- strict mypy geçer,
- package validation geçer,
- security/secret checks geçer,
- benchmark raporu üretilir,
- graph-off baseline regression testi geçer,
- independent verifier sonucu bağlanır,
- push yapılmaz.

## 24. Kapsam Dışı

- Graft npm dependency kurulumu
- Trail Brain cloud entegrasyonu
- Graft telemetry
- Graft viewer/UI
- Neo4j veya server graph DB
- existing knowledge DB’yi graph authority yapmak
- embedding’i kaldırmak
- graph’tan authorization üretmek
- PL/SQL’i corpus doğrulaması olmadan ready ilan etmek
- mevcut Jira aktif görevinin çıktısını bu işle karıştırmak

## 25. Kaynak Araştırma Referansı

Bu aktif görev şu araştırmanın teknik kararlarını uygular:

`ZEKAM_GRAFT_ENTEGRASYON_RAPORU.md`

Araştırma referansları:

- Graft README
- Graft issue #117
- Graft issue #186
- Graft issue #257
- Graft graph types/traverse/graphrank/MCP kaynakları
- Tree-sitter incremental parsing dokümantasyonu
- SQLite FTS5/BM25 dokümantasyonu
- SQLite recursive CTE dokümantasyonu
- Oracle PL/SQL Tree-sitter grammar araştırması

## 26. İlk Safe Action

Repository protokolünü çalıştır; baseline HEAD ve active task projection durumunu doğrula; ardından yalnız G0/G1 için exact plan üret.

İlk mutation:

`domain + application contracts + SQLite graph schema + tests`

olmalıdır.

Retrieval davranışını aynı commit/adımda değiştirme.

Structural graph acceptance geçtikten sonra ayrı step’te graph reranker’ı bağla.
