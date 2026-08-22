# Context Vault — Referans Analizi

## Değerlendirme özeti

Context Vault, hedef sistemin **Knowledge Plane**’i olmalıdır: kod, veri tabanı metadatası, belge, görsel/OCR, talep, defect, iş, karar, araştırma ve doğrulanmış öğrenimleri sürümlü, kaynak gösterilebilir ve yeniden indekslenebilir biçimde hazırlayan servis.

İki durum ayrı tutulmalıdır:

1. **Repository’deki mevcut kod:** çalışan fakat sınırlı bir document-RAG prototipi.
2. **Paylaşılan aktif görev:** henüz pushlanmamış olsa da kabul edilmiş hedef kapsam; analizde yapılmış sayılan mimari yön.

Yeni sistem mevcut monoliti büyütmemeli. Aktif görevdeki iyi kararları temiz bounded context’lerle uygulamalı ve Context Vault’ı orchestration/Work Graph sahibi yapmamalıdır.

## Mevcut kodun doğrulanan sorunları

- FastAPI endpoint, parser, chunker ve retrieval davranışı büyük ölçüde `main.py` içindedir.
- DOCX yalnız düz paragrafları; PDF basit text extraction’ı işler. Başlık, tablo, sayfa, bounding box, symbol ve satır konumu korunmaz.
- Chunking yaklaşık 500 karakter + overlap’tir; token veya yapıya duyarlı değildir.
- Upload senkrondur; geçici dosya işlem sonunda silinir. Orijinal ve normalize artifact kalıcı saklanmaz.
- PostgreSQL + pgvector kullanılsa da veri modeli yalnız Project, Document ve Chunk düzeyindedir; version, profile, job, event ve citation tabloları yoktur.
- Retrieval dense cosine + geniş `ILIKE` kurtarma yoludur; gerçek FTS, exact identifier index, RRF ve reranker yoktur.
- Sabit `TOP_K=3` ve global similarity threshold kullanılır; no-hit belge sorusu ile günlük sohbet doğru ayrılmaz.
- Redis, MinIO ve Celery compose’da görünür; ingestion hattına gerçek bağları eksik veya doğrulanmamıştır.
- README, context-summary, checklist ve active-task belgeleri birbirleriyle ve kodla çelişir. Kaynak kodu + test + migration + runtime receipt gerçeğin ölçütü olmalıdır.
- Kök ve `document-rag-platform/` altında yinelenen iskeletler vardır; temiz repository’de tek kanonik uygulama kökü bulunmalıdır.

## Alınacak hedef yetenekler

### 1. Sürümlü source ve artifact modeli

- `source/document`, `source_version`, `source_file`, `artifact`, `ingestion_job`, `ingestion_event` kayıtları.
- Dosya checksum’u veya Git commit SHA ile immutable source revision.
- Orijinal içerik, normalize JSON/Markdown, page image, OCR JSON ve thumbnail için object storage.
- Yeni version tamamen parse/embed/index edilmeden aktif version’ı değiştirmeme.
- Parser, chunker, OCR veya embedding değişiminde eski version’a dönülebilen re-index.

### 2. Ortak normalize içerik

Parser doğrudan chunk üretmemeli. Önce aşağıdaki türleri taşıyan kayıpsız bir ara model üretmelidir:

```text
NormalizedSource
ContentUnit
Hierarchy
SourceLocator
```

Zorunlu locator/metadata örnekleri:

- DOCX: heading path, block order, table/cell bilgisi.
- PDF: page, reading order, bounding box.
- Kod: repository/ref/commit, relative path, line range, symbol/signature.
- DB: engine, schema, object type/name, DDL revision, dependency.
- Work/research: project, work item, revision, evidence/authority sınıfı.

### 3. Parser ve scanner registry

- DOCX/PDF/TXT/Markdown/image/OCR/code/config/SQL için ayrı adaptörler.
- MIME + magic-byte doğrulama; timeout ve çıktı bütçesi.
- Repository URL, archive ve yalnız izinli local root taraması.
- Kod ingestion sırasında build, test, package install, hook, submodule veya script çalıştırmama.
- `.contextvaultignore`, `.gitignore`, sistem güvenlik ignore listesi ve include/exclude sırası.
- Tree-sitter/AST destekli symbol chunking; PL/SQL package/procedure/function/trigger farkındalığı.
- Değişmeyen content hash için incremental re-index.

### 4. Yapıya duyarlı chunking

Başlangıç profili ölçümle ayarlanmak üzere yaklaşık şu sınırları kullanabilir:

```text
hedef: 600 token
minimum: 250 token
maksimum: 900 token
kontrollü overlap: yaklaşık %12
parent context: en fazla 2400 token
```

Kurallar:

- Başlık bağlamını embedding text’e ekle; ham içeriği değiştirme.
- Tabloyu hücre ortasında, kodu sembol/statement ortasında bölme.
- Parent–child ve previous/next ilişkisi tut.
- Büyük tablo segmentlerinde header’ı; büyük kod fonksiyonlarında signature/enclosing symbol’ü tekrar et.
- Chunk kimliği source revision, locator, parser/chunker profile ve content hash’e bağlı olsun.

## 1024 boyutlu vektör ve retrieval kararı

### İlk sürüm

- Mevcut `BAAI/bge-m3` dense 1024 boyutlu profil korunmalı.
- BGE-M3 model kartı query instruction’ın zorunlu olmadığını belirtir. Mevcut Türkçe query/passage prefix’leri varsayılan olmaktan çıkarılmalı; açık/kapalı A/B evaluation ile karar verilmelidir.
- Model 8192 token desteklese de 8192-token chunk kullanılmamalı; retrieval granularity ve context maliyeti ayrı optimize edilmelidir.
- Gateway yalnız dense vector döndürüyorsa sparse/ColBERT çıktısı varmış gibi davranılmamalı.

### Hybrid retrieval sırası

1. **Authoritative/exact route:** Work ID, defect no, request no, DB object, error code, document/version ve diğer exact kayıtlar.
2. **Identifier/path/symbol route:** class, method, package, table, column, file path, alias ve trigram/fuzzy eşleşme.
3. **Lexical route:** PostgreSQL `tsvector`/GIN; teknik tokenlar için `simple`, doğal dil için test edilmiş ikinci profil.
4. **Dense route:** proje ve aktif source version filtreli pgvector cosine adayları.
5. **Fusion:** ham skorları toplamak yerine Reciprocal Rank Fusion.
6. **Rerank:** yalnız benchmark’ta net katkı sağlıyorsa feature flag ile.
7. **Context expansion:** parent/neighbor/header/signature; dedupe ve token bütçesi.
8. **Evidence packaging:** benzersiz source label, locator, revision, snippet ve skor açıklaması.

### PostgreSQL/pgvector notları

- Approximate HNSW filtreleri index scan sonrasında uygulanabildiğinden proje/source filter’ı recall’ı düşürebilir.
- Proje sayısı ve veri dağılımına göre list partition, partial index veya ayrı fiziksel embedding tabloları değerlendirilmelidir.
- pgvector iterative scans, `ef_search` ve candidate sayıları golden dataset ile kalibre edilmelidir.
- Farklı embedding boyutları veya uyumsuz profile’lar aynı index kolonda karıştırılmamalıdır.

### Profil ve re-index anahtarları

Her embedding şu kimliklere bağlı olmalıdır:

```text
provider + model + dimension + distance
query/passage prefix policy
parser profile + chunker profile
normalization schema
source revision + content hash
profile version + config digest
```

Re-index tetikleyicileri:

- source revision değişimi,
- parser/chunker/normalize şema değişimi,
- OCR provider/config değişimi,
- embedding/reranker profile değişimi,
- sensitivity/ignore policy değişimi.

## Nelerin vektörleneceği

Her şeyin ham hâli değil, güvenli ve sürümlü **retrieval projection**’ı vektörlenmelidir:

- Kod symbol/signature/body segmentleri ve mimari belgeler.
- Oracle/PostgreSQL schema, table, column, constraint, procedure ve dependency açıklamaları; satır verisi değil.
- Talep/defect/iş/karar revision özetleri ve kanıt referansları; state için Work Graph yine yetkilidir.
- Araştırma claim ve doğrulanmış sentezleri; raw provider konuşması değil.
- Doküman, PDF, tablo, OCR ve görsel açıklamaları.
- Test failure pattern, verified root cause ve çözüm kayıtları; secret veya tam log dump değil.
- Aktif skill metadata ve kullanım rehberi; skill code’u yalnız izin verilen source locator üzerinden.

Aşağıdakiler vektörlenmez:

- Secret, token, password, private key, connection string ve credential-bearing URI.
- `.env`, sertifika, kişisel path veya hassas üretim satır verisi.
- Aktif lease/owner token/authorization token.
- Ham chain-of-thought veya doğrulanmamış model varsayımı.

## Kanonik ve derived ayrımı

| Kanonik PostgreSQL kayıtları | Rebuild edilebilir bilgi projection’ları |
|---|---|
| Project, Work, Intent, Decision, Plan, Run, Checkpoint, Claim, Receipt, Source Version, Citation | Chunk, embedding, FTS, alias/symbol index, retrieval cache, Markdown rapor/index, dashboard read model |

Context Vault yalnız sağdaki bilgi indekslerinin ve source/ingestion yaşam döngüsünün sahibidir. Work state’i veya execution authority üretmez.

## Minimum veri modeli

- `sources`, `source_versions`, `source_files`
- `artifacts`, `normalized_sources`, `content_units`
- `chunks`, `chunk_relations`, `identifiers`
- `embedding_profiles`, `chunk_embeddings`
- `ingestion_jobs`, `ingestion_events`
- `retrieval_queries`, `retrieval_candidates`, `citations`
- `evaluation_datasets`, `evaluation_runs`, `evaluation_metrics`
- project/source/authority/revision/freshness metadata’sı

Raw/normalize büyük içerik object storage’da; identity, checksum, locator ve lifecycle PostgreSQL’de tutulmalıdır.

## Güvenlik ve kalite kapıları

- Secret scan/redaction embedding çağrısından önce.
- Archive traversal, zip bomb, symlink escape, arbitrary local path ve oversized parser koruması.
- Source code hiçbir koşulda ingestion sırasında çalıştırılmaz.
- Prompt içindeki belge talimatı güvenilmeyen içeriktir.
- Golden dataset; DOCX table, PDF page, OCR, code symbol, PL/SQL, exact identifier, paraphrase, multi-source, no-answer, contradiction ve injection vakalarını içerir.
- Recall@K, MRR, nDCG, citation accuracy/coverage, no-answer hatası, p50/p95 latency, cache hit ve embedding call sayısı ölçülür.
- “Tamamlandı” işareti ancak migration, test, eval ve uçtan uca receipt ile verilir.

## Al / Yeniden Tasarla / Alma

| Karar | İçerik |
|---|---|
| **Al** | PostgreSQL+pgvector, BGE-M3 1024 dense başlangıcı, aktif görevdeki versioned ingestion, immutable artifacts, normalized content, parser/chunker registry, hybrid retrieval, citations, eval, incremental code/OCR ingestion |
| **Yeniden tasarla** | Temiz bounded context, tek kanonik kök, Postgres partition/index stratejisi, model prefix policy, async jobs, source/authority metadata ve Control Plane API entegrasyonu |
| **Alma** | Monolitik `main.py`, senkron upload, karakter chunking, global threshold, top-3, `ILIKE` ana ranking, user-supplied embedding instruction, çelişkili status belgeleri, kullanılmayan compose servisleri |

## Hedef sistemdeki rolü

```text
Control Plane
  ├─ source/work/research kimlikleri ve yetkili revision’lar
  └─ retrieval isteği
          ↓
Context Vault Knowledge API
  ├─ ingest / refresh / re-index
  ├─ exact + lexical + dense + fusion
  ├─ citation / provenance
  └─ eval / debug
          ↓
Context Compiler
```

## Güncel teknik referanslar

- [BAAI/bge-m3 model kartı](https://huggingface.co/BAAI/bge-m3)
- [pgvector](https://github.com/pgvector/pgvector)
- [PostgreSQL Full Text Search](https://www.postgresql.org/docs/current/textsearch.html)
- [PostgreSQL pg_trgm](https://www.postgresql.org/docs/current/pgtrgm.html)
- [Docling desteklenen formatlar](https://docling-project.github.io/docling/usage/supported_formats/)
- [Docling OCR](https://docling-project.github.io/docling/concepts/OCR/)
