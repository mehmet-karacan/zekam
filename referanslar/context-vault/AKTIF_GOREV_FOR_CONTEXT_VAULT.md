# Context Vault — RAG, Kod Deposu ve Çok Modlu İçerik Altyapısı Uygulama Görevi

## Görev Kimliği

| Alan | Değer |
|---|---|
| Durum | **ONAYLI — UYGULANABİLİR AKTİF GÖREV** |
| Hedef repository | `https://github.com/mehmet-karacan/context-vault` |
| Hedef branch tabanı | `main` |
| İncelenen başlangıç commit'i | `763703ddf45e40cbe2d7b7799d95b03c77fb0e39` |
| Kanonik uygulama dizini | `document-rag-platform/` |
| Mevcut embedding modeli | `openai/BAAI/bge-m3` |
| Mevcut embedding boyutu | `1024` |
| Görev dosyasının repository içindeki yeri | Repository kökü: `AKTIF_GOREV.md` |
| Dil | Kod adları İngilizce, kullanıcı metinleri ve dokümantasyon Türkçe olabilir |

---

## 1. Görevin Amacı

Mevcut Context Vault uygulamasını, yalnızca basit Word/PDF metni parçalayıp dense vektör araması yapan bir MVP olmaktan çıkarıp aşağıdaki içerikleri güvenilir biçimde işleyebilen, kaynak gösterebilen, yeniden indekslenebilen ve modelden bağımsız devam ettirilebilen bir bilgi platformuna dönüştür:

1. Yapısal Word belgeleri.
2. Dijital ve taranmış PDF belgeleri.
3. TXT ve Markdown belgeleri.
4. PNG, JPEG ve benzeri görseller.
5. OCR gerektiren belgeler ve görseller.
6. ZIP/TAR olarak yüklenen proje kodları.
7. Git repository URL'si üzerinden alınan kod tabanları.
8. Sunucuda izin verilen kökler altındaki klasörlerin recursive taranması.
9. Çoklu belge ve çoklu kaynak üzerinde hibrit arama, reranking ve kanıta dayalı cevap üretimi.

Bu çalışma **embedding modelini değiştirme projesi değildir**. İlk uygulamada `openai/BAAI/bge-m3` ve `Vector(1024)` korunacaktır. Ana dönüşüm; ingestion, parse, chunking, metadata, retrieval, reranking, citation, değerlendirme ve yeniden indeksleme katmanlarında yapılacaktır.

---

## 2. Başlangıç ve Devam Protokolü

Bu dosyayı alan herhangi bir yapay zekâ ajanı veya geliştirici aşağıdaki sırayı uygulamalıdır:

1. Bu dosyanın tamamını oku.
2. Repository çalışma ağacını, son commit'i ve `git status` çıktısını kontrol et.
3. Gerçek durumu koddan doğrula; `context-summary.md`, `IMPLEMENTATION_CHECKLIST.md` veya eski görev özetlerini tek başına doğru kabul etme.
4. Kanonik uygulama dizini olarak `document-rag-platform/` altında çalış.
5. Kullanıcıya ait kaynak dosyaları, belgeleri, proje kodlarını ve mevcut verileri silme.
6. Büyük kapsamlı tek commit yerine aşama bazlı küçük ve geri alınabilir commit'ler üret.
7. Her aşamadan sonra testleri çalıştır, sonucu bu dosyanın **İlerleme Kaydı** bölümüne işle.
8. Tamamlanan işi tekrar yapma; dosyadaki işaretler ile gerçek kodu birlikte doğrula.
9. Yalnızca haricî ve çözülemeyen bir engel varsa dur. Eksik servis, erişim veya credential varsa engeli tam komut ve hata mesajıyla kaydet.
10. Görev kapsamını genişletme. Yeni özellikler ancak bu planda tanımlı adaptör veya extension point sınırları içinde eklenebilir.

Önerilen çalışma branch'i:

```text
feat/context-vault-ingestion-retrieval-v2
```

---

## 3. Mevcut Kodun Doğrulanmış Başlangıç Durumu

Aşağıdaki maddeler başlangıç gerçekliğidir ve uygulama sırasında yeniden doğrulanmalıdır:

- FastAPI backend ağırlıklı olarak `services/backend/src/main.py` içinde monolitik yapıdadır.
- Embedding ve chat gateway çağrıları `services/backend/src/llm.py` içindedir.
- `EMBEDDING_MODEL` ortam değişkeni varsayılan olarak `openai/BAAI/bge-m3` değerini kullanır.
- Embedding sonucu OpenAI uyumlu `/embeddings` cevabından yalnızca tek dense vektör olarak alınır.
- `Chunk.embedding` alanı `Vector(1024)` olarak tanımlıdır.
- PostgreSQL üzerinde cosine HNSW index oluşturulur.
- DOCX parser yalnızca `doc.paragraphs` içindeki düz metni birleştirir; tablo, başlık hiyerarşisi ve diğer yapılar korunmaz.
- Mevcut chunking yaklaşık `500 karakter + 50 karakter overlap` ile yapılır; token bazlı değildir.
- Sorguda dense cosine adayları ve basit `ILIKE` kelime eşleşmeleri kullanılır.
- Final bağlama en fazla `TOP_K = 3` chunk gönderilir.
- Global benzerlik eşiği kodda `0.55`, README'de farklı bir değer olarak geçmektedir.
- Kaynak bulunamadığında belge sorusu ile günlük sohbet doğru ayrılmamaktadır.
- Reranker yoktur.
- Gerçek PostgreSQL full-text search, BM25 benzeri lexical sıralama veya RRF yoktur.
- Chunk metadata'sında başlık yolu, sayfa, satır, sembol, parser sürümü ve embedding sürümü yoktur.
- Orijinal dosya MinIO'da kalıcı tutulmaz; geçici dosya işlem sonunda silinir.
- Redis, MinIO ve Celery servisleri compose içinde bulunmasına rağmen ingestion hattına tam bağlı değildir.
- Worker komutu gerçek bir `celery_app` modülüne bağlanmamış olabilir; doğrulanmalı ve düzeltilmelidir.
- Test ve evaluation klasörleri büyük ölçüde boştur.
- Repo kökünde ve `document-rag-platform/` altında yinelenen iskelet dizinler vardır. Otomatik silme yapılmayacaktır.
- Proje durum dokümanları birbiriyle ve gerçek kodla çelişmektedir.

---

## 4. Değişmeyecek Temel Kararlar

1. İlk sürümde embedding modeli `openai/BAAI/bge-m3` olarak kalır.
2. İlk aktif dense embedding profili 1024 boyutunda kalır.
3. PostgreSQL + pgvector korunur.
4. Backend FastAPI, frontend Next.js olarak kalır.
5. Uygulama bir anda mikroservislere bölünmez; modüler monolit olarak düzenlenir.
6. Belge içeriği hiçbir zaman sistem talimatı sayılmaz.
7. Kullanıcı kodu ingestion sırasında hiçbir koşulda çalıştırılmaz.
8. Repository tararken paket kurulmaz, build alınmaz, hook çalıştırılmaz ve submodule otomatik çekilmez.
9. Orijinal kaynak ve normalize edilmiş parse çıktısı korunmadan yalnızca embedding saklama yaklaşımı devam ettirilmez.
10. Model, parser veya chunker değiştiğinde kontrollü re-index zorunludur.
11. Kod içinde sabit model adı, sabit eşik ve sabit top-k değerleri bırakılmaz; tümü doğrulanmış config üzerinden yönetilir.
12. Kullanıcıya ait mevcut dosyalar ve repo içeriği otomatik silinmez.

---

## 5. Hedef Üst Seviye Mimari

```text
Kaynak
  ├─ DOCX / PDF / TXT / MD
  ├─ PNG / JPEG / TIFF
  ├─ ZIP / TAR proje paketi
  ├─ Git repository URL
  └─ İzinli yerel klasör
        ↓
Kaynak doğrulama ve güvenlik kontrolleri
        ↓
Orijinal içeriği object storage'a yazma
        ↓
Ingestion job ve durum olayları
        ↓
Parser Router
  ├─ Document Parser
  ├─ PDF Parser
  ├─ OCR Provider
  ├─ Code Repository Parser
  └─ Plain Text Parser
        ↓
Normalize Edilmiş İçerik Modeli
        ↓
İçerik türüne duyarlı Chunker Registry
        ↓
Dense Embedding + Lexical Index + Identifier Index
        ↓
PostgreSQL / pgvector
        ↓
Sorgu hazırlama
        ↓
Dense Retrieval + Lexical Retrieval + Exact Identifier Retrieval
        ↓
RRF Fusion
        ↓
Opsiyonel Reranker
        ↓
Deduplication + Komşu/Parent Genişletme
        ↓
Kanıt paketleme
        ↓
LLM cevap üretimi
        ↓
Citations + Retrieval Debug + Evaluation
```

---

## 6. Ortak Normalize Edilmiş İçerik Modeli

DOCX, PDF, görsel, OCR ve kaynak kodu aynı ingestion altyapısına bağlamak için ortak bir ara model oluşturulacaktır.

Önerilen domain modelleri:

```python
NormalizedSource
- source_id
- version_id
- source_type
- title
- language
- metadata
- units: list[ContentUnit]

ContentUnit
- unit_id
- unit_type
- text
- markdown
- order
- hierarchy
- locator
- metadata

Hierarchy
- heading_path
- parent_unit_id
- depth

SourceLocator
- page_start
- page_end
- bbox
- file_path
- line_start
- line_end
- symbol_name
- symbol_type
- block_index
```

Zorunlu `unit_type` değerleri:

```text
heading
paragraph
list_item
table
code
formula
image
image_caption
ocr_text
page_break
file_header
symbol
configuration
```

Kurallar:

- Parser doğrudan chunk üretmez; önce normalize içerik üretir.
- Normalize model kayıpsız veya yeniden üretilebilir JSON olarak saklanır.
- İnsan tarafından okunabilir Markdown temsili ayrıca üretilebilir.
- Kaynak konumu mevcutsa her içerik biriminde korunur.
- DOCX için gerçek sayfa numarası garanti edilmez; başlık yolu ve blok sırası temel citation olur. Sayfa numarası yalnız render/convert edilen sürümden üretilebilir.
- PDF ve görsellerde sayfa/bounding-box bilgisi korunur.
- Kodda dosya yolu, satır aralığı ve sembol bilgisi korunur.

---

## 7. Hedef Backend Dizin Yapısı

Mevcut kod big-bang yeniden yazılmayacak; endpoint'ler ve servisler aşamalı taşınacaktır.

```text
document-rag-platform/services/backend/src/
├── main.py
├── config.py
├── db.py
├── models.py
├── api/
│   └── v1/
│       ├── router.py
│       ├── projects.py
│       ├── documents.py
│       ├── repositories.py
│       ├── ingestion_jobs.py
│       ├── chat.py
│       └── debug.py
├── application/
│   ├── ingestion_service.py
│   ├── reindex_service.py
│   ├── retrieval_service.py
│   ├── answer_service.py
│   └── source_service.py
├── domain/
│   ├── normalized_content.py
│   ├── ingestion.py
│   ├── retrieval.py
│   ├── citations.py
│   └── ports.py
├── infrastructure/
│   ├── parsers/
│   │   ├── router.py
│   │   ├── docling_parser.py
│   │   ├── docx_parser.py
│   │   ├── pdf_parser.py
│   │   ├── plain_text_parser.py
│   │   ├── image_parser.py
│   │   └── code_parser.py
│   ├── chunkers/
│   │   ├── registry.py
│   │   ├── document_chunker.py
│   │   ├── table_chunker.py
│   │   ├── code_chunker.py
│   │   └── token_counter.py
│   ├── embeddings/
│   │   ├── openai_compatible.py
│   │   ├── profiles.py
│   │   └── cache.py
│   ├── retrieval/
│   │   ├── dense.py
│   │   ├── lexical.py
│   │   ├── identifier.py
│   │   ├── rrf.py
│   │   └── context_builder.py
│   ├── rerankers/
│   │   ├── noop.py
│   │   └── remote.py
│   ├── repositories/
│   │   ├── discovery.py
│   │   ├── git_source.py
│   │   ├── archive_source.py
│   │   ├── directory_source.py
│   │   ├── ignore_rules.py
│   │   └── language_detection.py
│   ├── ocr/
│   │   ├── base.py
│   │   ├── tesseract_provider.py
│   │   ├── docling_provider.py
│   │   └── preprocessing.py
│   └── storage/
│       ├── minio_storage.py
│       └── local_storage.py
├── workers/
│   ├── celery_app.py
│   └── ingestion_tasks.py
└── llm.py
```

`llm.py` ilk aşamada uyumluluk için kalabilir; embedding ve chat kodları adaptörlere taşındıkça ince bir facade'a dönüştürülmeli veya kontrollü biçimde kaldırılmalıdır.

---

## 8. Hedef Veri Modeli

Mevcut `projects`, `documents` ve `chunks` kayıtları korunarak Alembic migration ile genişletilecektir.

### 8.1 `documents`

Eklenecek alanlar:

```text
source_type          document | image | repository | directory | archive
origin_uri           nullable
mime_type            nullable
checksum             nullable
active_version_id    nullable
created_at
updated_at
deleted_at            nullable
```

İlk aşamada tablo adı değiştirilmez; API uyumluluğu korunur.

### 8.2 `document_versions`

```text
id
document_id
version_no
source_revision       dosya checksum'u veya Git commit SHA
status
parser_profile
chunker_profile
storage_key
normalized_artifact_id
created_at
activated_at
error_message
```

Her yeni upload, repository commit'i veya re-index sonucu ayrı version üretir. Yeni version tamamen hazır olmadan aktif version değiştirilmez.

### 8.3 `source_files`

Repository, klasör veya arşiv içindeki her dosya için:

```text
id
version_id
relative_path
language
mime_type
size_bytes
content_hash
is_binary
is_generated
is_ignored
metadata_json
```

### 8.4 `document_artifacts`

```text
id
version_id
artifact_type         original | normalized_json | normalized_md | page_image | thumbnail | ocr_json
storage_key
checksum
size_bytes
metadata_json
created_at
```

### 8.5 `ingestion_jobs`

```text
id
version_id
status                queued | running | completed | failed | cancelled
stage                 validating | storing | parsing | ocr | normalizing | chunking | embedding | indexing | activating
progress
attempt
error_code
error_message
started_at
finished_at
created_at
```

### 8.6 `ingestion_events`

Kalıcı ilerleme ve hata olayları tutulur. Redis yalnız canlı iletim katmanı olur; gerçek durum PostgreSQL'dedir.

### 8.7 `chunks`

Mevcut alanlara ek olarak:

```text
version_id
source_file_id        nullable
sequence_no
chunk_type
heading_path          JSONB veya text[]
page_start
page_end
line_start
line_end
bbox                   JSONB
symbol_name
symbol_type
token_count
content_hash
parent_chunk_id       nullable
metadata_json
search_vector         TSVECTOR
identifiers           text[]
created_at
```

### 8.8 `embedding_profiles`

```text
id
provider
model
dimension
distance_metric
query_prefix
passage_prefix
profile_version
config_hash
is_active
created_at
```

### 8.9 `chunk_embeddings`

```text
chunk_id
embedding_profile_id
embedding             Vector(1024) — ilk aktif profil
created_at
UNIQUE(chunk_id, embedding_profile_id)
```

Bu görev sırasında farklı boyutlu embedding'ler aynı indeksli kolonda karıştırılmayacaktır. İleride farklı boyut gerekiyorsa `EmbeddingStore` adaptörü arkasında ayrı fiziksel tablo/index profili oluşturulacaktır.

### 8.10 Sohbet ve citation tabloları

```text
conversations
messages
message_citations
```

`message_citations` en az şunları tutar:

```text
message_id
chunk_id
document_id
version_id
source_file_id
rank
retrieval_score
reranker_score
page_start
page_end
line_start
line_end
citation_label
```

---

# 9. Uygulama Aşamaları

Her aşama ayrı doğrulanmalı ve tamamlanmadan bir sonraki aşamanın üretim davranışı varsayılan hâle getirilmemelidir.

---

## Aşama 0 — Gerçek Durumu Sabitle ve Güvenli Başlangıç Oluştur

### Yapılacaklar

- [ ] `main` branch ve başlangıç commit'ini kaydet.
- [ ] Mevcut Docker Compose akışını çalıştır ve çalışan/çalışmayan servisleri raporla.
- [ ] Mevcut DB şemasının dump'ını veya en azından şema çıktısını al.
- [ ] En az 20 gerçek belge sorusundan başlangıç golden dataset oluştur.
- [ ] Her soru için mevcut top-10 retrieval sonuçlarını JSONL olarak kaydet.
- [ ] Mevcut cevapların ve kaynakların baseline çıktısını üret.
- [ ] `README.md`, `context-summary.md`, `IMPLEMENTATION_CHECKLIST.md` ve `active/current-tasks.md` çelişkilerini işaretle.
- [ ] Repo kökünde kanonik dizinin `document-rag-platform/` olduğunu belirten kısa root README ekle veya mevcut README'yi düzelt.
- [ ] Yinelenen kök iskelet dizinleri silme; yalnızca `docs/cleanup-candidates.md` altında listele.

### Kabul kriterleri

- [ ] Baseline retrieval dosyası repository içinde `tests/evals/baseline/` altında bulunuyor.
- [ ] En az Recall@1, Recall@3, Recall@5 ve MRR@10 hesaplayan script var.
- [ ] Uygulamanın mevcut hâli için tekrar üretilebilir başlangıç komutları yazılı.
- [ ] Hiçbir kullanıcı verisi silinmedi.

---

## Aşama 1 — Konfigürasyon ve Modüler Backend İskeleti

### Yapılacaklar

- [ ] `pydantic-settings` veya eşdeğer typed settings katmanı ekle.
- [ ] Dağınık `os.getenv` çağrılarını `config.py` altında topla.
- [ ] Proje, belge, chat ve health endpoint'lerini `api/v1` router'larına taşı.
- [ ] Domain portlarını tanımla:
  - [ ] `DocumentParser`
  - [ ] `OcrProvider`
  - [ ] `Chunker`
  - [ ] `TokenCounter`
  - [ ] `EmbeddingProvider`
  - [ ] `VectorRetriever`
  - [ ] `LexicalRetriever`
  - [ ] `Reranker`
  - [ ] `ObjectStorage`
  - [ ] `SourceScanner`
- [ ] Mevcut API response yapısını mümkün olduğunca geriye uyumlu tut.
- [ ] `llm.py` içindeki embedding ve generation sorumluluklarını adaptörlere ayır.
- [ ] Unit test iskeletini gerçek testlerle doldurmaya başla.

### Kabul kriterleri

- [ ] `main.py` uygulama oluşturma ve router bağlama dışında iş kuralı içermez.
- [ ] Eski endpoint'ler çalışmaya devam eder.
- [ ] Typed settings doğrulaması eksik zorunlu credential'da açık hata verir.
- [ ] Unit testler ve backend startup testi geçer.

---

## Aşama 2 — Sürümlü Ingestion, MinIO, Alembic ve Worker

### Yapılacaklar

- [ ] Gerçek Alembic yapılandırması oluştur.
- [ ] Bölüm 8'deki tabloları/alanları ekleyen migration'ları yaz.
- [ ] Mevcut belgeleri `document_versions.version_no = 1` olacak şekilde backfill et.
- [ ] Mevcut chunk'ları version ve embedding profile ile ilişkilendir.
- [ ] MinIO `ObjectStorage` adaptörünü uygula.
- [ ] Upload edilen orijinal dosyayı immutable object key ile MinIO'ya yaz.
- [ ] Normalize JSON ve Markdown artifact'larını MinIO'da sakla.
- [ ] `celery_app.py` ve gerçek ingestion task'larını oluştur.
- [ ] Compose içindeki worker komutunu çalışır hâle getir.
- [ ] Worker'a DB, Redis, MinIO, embedding gateway ve gerekli model ayarlarını geçir.
- [ ] Upload endpoint'ini senkron tam işleme yerine job oluşturacak biçimde dönüştür.
- [ ] Geçiş süresince senkron mod için feature flag bırak.
- [ ] Job retry, idempotency ve stage transition kontrolü ekle.
- [ ] `GET /ingestion-jobs/{job_id}` ve event endpoint'i ekle.

### Object key standardı

```text
projects/{project_id}/documents/{document_id}/versions/{version_id}/original/{safe_filename}
projects/{project_id}/documents/{document_id}/versions/{version_id}/normalized/document.json
projects/{project_id}/documents/{document_id}/versions/{version_id}/normalized/document.md
projects/{project_id}/documents/{document_id}/versions/{version_id}/artifacts/...
```

### Kabul kriterleri

- [ ] Upload isteği uzun embedding süresince HTTP bağlantısını açık tutmaz.
- [ ] Worker yeniden başlatılsa job verisi kaybolmaz.
- [ ] Aynı job tekrar alınırsa duplicate chunk/embedding oluşmaz.
- [ ] Orijinal dosyadan re-index yapılabilir.
- [ ] Yeni version hazır olmadan eski aktif version kullanılmaya devam eder.
- [ ] Migration upgrade ve downgrade testleri geçer.

---

## Aşama 3 — Yapısal Belge Parser Altyapısı

### 3.1 Parser Router

- [ ] MIME, extension ve magic-byte sonuçlarına göre parser seç.
- [ ] Extension ile MIME çelişirse dosyayı otomatik güvenilir sayma.
- [ ] Parser timeout ve maksimum çıktı limiti uygula.
- [ ] Parse sonucunu ortak `NormalizedSource` modeline dönüştür.

### 3.2 DOCX

- [ ] Paragraf ve tabloları document body sırasına göre birlikte dolaş.
- [ ] Heading style seviyelerini ve heading path'i koru.
- [ ] Liste ve numaralandırma bilgisini mümkün olduğu ölçüde koru.
- [ ] Tabloları Markdown ve yapısal JSON olarak üret.
- [ ] Bir tablo hücresi içindeki paragraf sırasını koru.
- [ ] Header/footer ve textbox desteğini parser yeteneği varsa ekle; yoksa açık metadata uyarısı üret.
- [ ] Boş paragraf gürültüsünü temizle fakat bölüm sınırlarını kaybetme.
- [ ] DOCX citation için heading path + block index kullan.
- [ ] Gerçek sayfa numarası yoksa uydurma page değeri üretme.

### 3.3 PDF

- [ ] Dijital PDF ve taranmış PDF'yi ayırt eden text-coverage kontrolü ekle.
- [ ] İlk tercih olarak yapısal PDF anlayışı sağlayan Docling adaptörünü uygula.
- [ ] Metin, heading, tablo, okuma sırası, sayfa ve bounding-box bilgisini normalize modele aktar.
- [ ] Docling kullanılamazsa sınırlı fallback parser sağla ve capability metadata'sı üret.
- [ ] Dijital metin yeterliyse OCR'ı gereksiz çalıştırma.
- [ ] Karma PDF'de yalnız düşük text-coverage sayfaları OCR'a yönlendir.

### 3.4 TXT / Markdown

- [ ] Encoding tespiti veya kontrollü UTF-8 fallback uygula.
- [ ] Markdown heading, code fence, liste ve tablo yapılarını koru.
- [ ] Büyük düz metin dosyalarında satır bilgisi üret.

### Kabul kriterleri

- [ ] DOCX tablo hücreleri retrieval sonucu içinde bulunabiliyor.
- [ ] Başlık altındaki paragraf chunk embedding metninde başlık bağlamını taşıyor.
- [ ] PDF citation sayfa numarası doğru.
- [ ] Dijital PDF boş yere OCR'a gitmiyor.
- [ ] Parse test fixture'ları repository içinde bulunuyor.

---

## Aşama 4 — Yapıya Duyarlı Chunking ve Embedding Profilleri

### Yapılacaklar

- [ ] Tek bir genel `chunk_text` fonksiyonu yerine `ChunkerRegistry` oluştur.
- [ ] Token sayımı için `TokenCounter` portu ekle.
- [ ] BGE-M3 tokenizer erişilebiliyorsa model uyumlu sayım kullan; erişilemiyorsa konservatif fallback uygula ve kullanılan yöntemi metadata'da belirt.
- [ ] Varsayılan hedefleri config yap:

```text
CHUNK_TARGET_TOKENS=600
CHUNK_MIN_TOKENS=250
CHUNK_MAX_TOKENS=900
CHUNK_OVERLAP_RATIO=0.12
PARENT_CHUNK_MAX_TOKENS=2400
```

- [ ] Heading'i altındaki chunk'ların embedding metnine kontrollü context header olarak ekle.
- [ ] Ham içerik ile embedding'e gönderilen `embedding_text` ayrımını koru.
- [ ] Tabloyu hücre ortasında bölme; büyük tabloları header tekrar ederek satır gruplarına böl.
- [ ] Kod bloklarını normal paragraf gibi bölme.
- [ ] Parent-child chunk ilişkisi oluştur.
- [ ] Komşu chunk bilgisi için sequence numarası tut.
- [ ] `content_hash` ile duplicate içeriği tespit et.
- [ ] Embedding cache anahtarını aşağıdaki bileşimden üret:

```text
content_hash + embedding_profile.config_hash
```

- [ ] Gateway batch array desteklemiyorsa kontrollü concurrency ile tekli çağrı fallback'i kullan.
- [ ] Retry, timeout, rate-limit backoff ve finite-vector doğrulaması ekle.
- [ ] Dönen vektör boyutu aktif profil boyutuyla eşleşmiyorsa job'ı fail et.

### BGE-M3 instruction kararı

BGE-M3 resmî model kartına göre dense retrieval için query instruction zorunlu değildir. Bu nedenle:

- [ ] Mevcut Türkçe query/passage prefix'lerini feature flag yap.
- [ ] Varsayılanı boş prefix olarak ayarla veya baseline A/B test sonucuna göre belirle.
- [ ] Frontend'deki kullanıcıya açık serbest embedding talimatını kaldır veya yalnız yönetici/debug moduna taşı.
- [ ] Farklı belgeleri farklı serbest prefix'lerle aynı index'e yazma.

### Kabul kriterleri

- [ ] Chunk boyutu artık karakter değil token hedeflidir.
- [ ] Her chunk parser, chunker ve embedding profile sürümünü taşır.
- [ ] Aynı içerik aynı profil ile tekrar embed edilmez.
- [ ] Prefix açık/kapalı karşılaştırma raporu oluşturulur.
- [ ] Parser veya chunker değişince re-index job oluşturulabilir.

---

## Aşama 5 — Retrieval V2: Hybrid Search, RRF ve Reranking

### 5.1 Dense retrieval

- [ ] pgvector cosine search korunur.
- [ ] Aday sayısı config üzerinden yönetilir.
- [ ] Project, document, active version ve source type filtreleri sorgu içinde uygulanır.
- [ ] HNSW `ef_search` değerlendirme sonucuna göre ayarlanabilir olmalıdır.

### 5.2 Lexical retrieval

- [ ] `chunks.search_vector` için GIN index oluştur.
- [ ] Teknik identifier'ları bozmayacak `simple` text-search profili kullan.
- [ ] Gerekirse Türkçe doğal dil için ikinci profil deneysel tutulabilir.
- [ ] `identifiers` alanına tablo, kolon, class, method, package, error code ve benzeri teknik tokenları yaz.
- [ ] `identifiers` için GIN index oluştur.
- [ ] Dosya yolu ve sembol adlarında exact/trigram eşleşme desteği ekle.
- [ ] Mevcut geniş `OR ILIKE '%kelime%'` yaklaşımını ana ranking mekanizması olmaktan çıkar.

### 5.3 Fusion

Başlangıç config'i:

```text
VECTOR_CANDIDATE_K=40
LEXICAL_CANDIDATE_K=40
IDENTIFIER_CANDIDATE_K=20
FUSION_CANDIDATE_K=20
RRF_K=60
RERANK_TOP_K=8
CONTEXT_MAX_CHUNKS=8
```

- [ ] Dense, lexical ve identifier listelerini Reciprocal Rank Fusion ile birleştir.
- [ ] Ham skorları doğrudan toplama; farklı skor ölçeklerini rank üzerinden birleştir.
- [ ] Duplicate chunk ve aynı içeriğin farklı kopyalarını temizle.

### 5.4 Reranker

- [ ] `Reranker` portu ve `NoopReranker` ekle.
- [ ] Gateway veya ayrı servis destekliyorsa remote reranker adaptörü ekle.
- [ ] Reranker kullanımı feature flag olsun.
- [ ] Reranker başarısız olursa fusion sıralamasına güvenli fallback yap.
- [ ] Reranker model adı ve sürümünü response debug metadata'sına ekle.

### 5.5 Context genişletme

- [ ] Seçilen chunk'ın parent veya kontrollü önceki/sonraki chunk'larını ekle.
- [ ] Aynı metni birden fazla kez bağlama ekleme.
- [ ] Context token bütçesini aşma.
- [ ] Tablonun header chunk'ını satır chunk'larıyla birlikte ekle.
- [ ] Kod sembolünün imza/header bilgisini gövde chunk'larıyla birlikte ekle.

### 5.6 No-answer ve intent ayrımı

Üç ayrı davranış uygulanacaktır:

```text
A. Günlük sohbet / selamlaşma
B. Belge sorusu ve yeterli kanıt bulundu
C. Belge sorusu fakat yeterli kanıt bulunamadı
```

- [ ] Selamlaşma için deterministik kısa kurallar kullan; gerekirse küçük intent modeline geçiş noktası bırak.
- [ ] Retrieval sonucu boş diye soruyu otomatik günlük sohbet sayma.
- [ ] Sabit global `0.55` eşiğini ana karar mekanizması olmaktan çıkar.
- [ ] No-answer politikasını golden dataset ile kalibre et.
- [ ] Exact identifier veya güçlü lexical eşleşme varsa düşük dense skor yüzünden sonucu atma.
- [ ] Yetersiz kanıtta açıkça seçili kaynaklarda bilgi bulunamadığını söyle.

### Kabul kriterleri

- [ ] Golden set üzerinde Recall@5 mevcut baseline'dan ölçülebilir biçimde yüksek.
- [ ] Teknik identifier soruları dense-only baseline'dan daha iyi sonuç verir.
- [ ] `selam` ile gerçek fakat bulunamayan belge sorusu farklı davranır.
- [ ] Final context 3 sabit chunk ile sınırlı değildir; config ve token bütçesiyle yönetilir.
- [ ] Retrieval debug endpoint'i bütün candidate rank ve skorlarını gösterebilir.

---

## Aşama 6 — Kanıt Paketleme, Cevap Üretimi ve Kaynak UI

### Backend

- [ ] LLM'e yalnız ham chunk dizisi gönderme.
- [ ] Her kanıtı benzersiz label ile paketle:

```text
[S1]
Belge: GPU_Mimari.docx
Bölüm: Veri Akışı > Tekilleştirme
Sayfa: 12
İçerik: ...
```

Kod kaynağı örneği:

```text
[S2]
Repository: context-vault
Dosya: services/backend/src/main.py
Sembol: query_chat
Satırlar: 220-315
İçerik: ...
```

- [ ] Prompt injection korumasını koru ve test et.
- [ ] Modelin kaynakta olmayan bilgi üretmemesi için no-answer davranışını prompt ve uygulama katmanında birlikte uygula.
- [ ] Cevap ile kullanılan chunk'lar arasındaki citation kayıtlarını DB'ye yaz.
- [ ] Response'a `answerable`, `citations` ve isteğe bağlı `retrieval_debug` alanları ekle.

### Frontend

- [ ] Yalnız belge adı göstermek yerine citation detay paneli ekle.
- [ ] Kullanıcı belge adı, bölüm, sayfa, dosya yolu, satır aralığı, snippet ve skorları görebilsin.
- [ ] Geliştirici modunda dense/lexical/RRF/reranker sıraları görüntülenebilsin.
- [ ] Serbest embedding instruction alanını kaldır veya debug/admin bayrağına bağla.
- [ ] Upload/job ilerlemesini gerçek backend event'lerinden göster.
- [ ] Source type filtresi ekle: `all`, `documents`, `code`, `images`.

### Kabul kriterleri

- [ ] Birden fazla belgeden gelen her bilgi kaynağıyla eşleştirilebilir.
- [ ] PDF sayfa citation'ı, kod satır citation'ı ve DOCX heading citation'ı UI'da gösterilir.
- [ ] Kullanıcıya kaynak bulunamadığı durumda uydurma cevap verilmez.
- [ ] Chat endpoint response şeması OpenAPI'de tanımlıdır.

---

## Aşama 7 — Repository, Arşiv ve Klasör Taraması

Bu aşama proje kodlarının tamamını güvenli biçimde tarayıp vektörleyebilecek altyapıyı kurar.

### 7.1 Desteklenen kaynaklar

- [ ] Public veya credential referanslı Git repository URL.
- [ ] ZIP ve TAR.GZ proje yüklemesi.
- [ ] Sunucuda izin verilen kökler altında local directory scan.
- [ ] Mevcut repository'nin belirli branch/tag/commit'i.

### 7.2 Güvenlik sınırları

- [ ] Web isteğinden gelen herhangi bir mutlak path'i doğrudan tarama.
- [ ] Yalnız `CODE_ALLOWED_ROOTS` altında kalan canonical path'lere izin ver.
- [ ] Symlink ile izinli kökün dışına çıkışı engelle.
- [ ] Git hook çalıştırma.
- [ ] Submodule otomatik çekme.
- [ ] Git LFS büyük objelerini varsayılan olarak indirme.
- [ ] Repository içindeki script, build, test veya package manager komutlarını çalıştırma.
- [ ] Archive path traversal ve zip bomb koruması uygula.
- [ ] Maksimum dosya sayısı, tek dosya boyutu, toplam byte ve tarama süresi limiti koy.
- [ ] `.env`, private key, credential, secret ve binary certificate benzeri hassas dosyaları varsayılan olarak skip et.
- [ ] İçerik gateway'e gönderilmeden önce secret policy kontrolünden geçir.

### 7.3 Ignore kuralları

Aşağıdaki sıra uygulanır:

1. Sistem güvenlik ignore listesi.
2. `.contextvaultignore`.
3. Repository `.gitignore` kuralları.
4. Kullanıcı tarafından izin verilen ek include/exclude kalıpları.

Varsayılan ignore örnekleri:

```text
.git/
node_modules/
.venv/
venv/
dist/
build/
target/
coverage/
.next/
.cache/
vendor/
*.min.js
*.map
*.lock         opsiyonel ve config ile açılabilir
*.png
*.jpg
*.pdf          repo kod taramasında belge parser'a ayrı yönlendirilebilir
*.exe
*.dll
*.so
*.class
*.jar
*.zip
*.tar
*.gz
.env
.env.*
*.pem
*.key
*.p12
*.jks
id_rsa*
```

### 7.4 Dosya keşfi ve metadata

Her dosya için:

```text
repository_url
branch_or_ref
commit_sha
relative_path
language
mime_type
size_bytes
content_hash
is_generated
is_test
module
package
imports
symbols
```

### 7.5 Kod parser ve chunking

- [ ] Tree-sitter destekli diller için AST/symbol bazlı chunking uygula.
- [ ] İlk hedef diller: Python, Java, JavaScript, TypeScript, JSON, YAML, SQL ve Markdown.
- [ ] PL/SQL için özel chunker ekle:
  - [ ] `PACKAGE`, `PACKAGE BODY`, `PROCEDURE`, `FUNCTION`, `TRIGGER`, `TYPE` sınırlarını tanı.
  - [ ] String ve yorum içindeki anahtar kelimelerle yanlış bölme yapma.
  - [ ] İmza, declaration ve enclosing package bilgisini her chunk'a ekle.
- [ ] Tree-sitter grammar olmayan dilde satır ve sembol farkındalıklı fallback kullan.
- [ ] Çok büyük fonksiyonları iç bloklara böl fakat signature ve enclosing symbol context'ini tekrar ekle.
- [ ] README, ADR ve mimari dokümanları belge parser hattına yönlendir.
- [ ] JSON/YAML/XML gibi config dosyalarını top-level key/object bazlı böl.
- [ ] Kod chunk embedding metnine dosya yolu, dil, sembol ve signature header'ı ekle.

### 7.6 Incremental re-index

- [ ] Repository snapshot'ını commit SHA ile sürümle.
- [ ] Dosya `content_hash` değişmediyse yeniden parse/embed etme.
- [ ] Değişen ve yeni dosyaları işle.
- [ ] Silinen dosyaların yeni aktif version'da görünmemesini sağla.
- [ ] Önce yeni snapshot'ı tamamen hazırla, sonra atomik aktivasyon yap.

### 7.7 API

Önerilen endpoint'ler:

```text
POST /repositories/ingest
POST /archives/upload
POST /directories/scan
POST /documents/{document_id}/refresh
GET  /documents/{document_id}/files
GET  /documents/{document_id}/versions
```

### Kabul kriterleri

- [ ] `context-vault` benzeri bir repository recursive taranabilir.
- [ ] `.gitignore` ve `.contextvaultignore` uygulanır.
- [ ] Kod hiçbir şekilde çalıştırılmaz.
- [ ] Soruya verilen cevap dosya yolu, sembol ve satır citation'ı taşır.
- [ ] Tek dosya değiştiğinde bütün repository yeniden embed edilmez.
- [ ] PL/SQL package/procedure sorularında ilgili sembol ilk sonuçlarda bulunur.

---

## Aşama 8 — Görsel, PNG ve OCR Altyapısı

### 8.1 Provider sözleşmesi

```python
OcrProvider.extract(image_or_page, languages, options) -> OcrResult

OcrResult
- full_text
- blocks
- confidence
- language
- orientation
- preprocessing_steps
- engine
- engine_version
```

Her OCR block:

```text
text
bbox
confidence
page_number
reading_order
```

### 8.2 OCR provider'ları

- [ ] Docling tabanlı OCR/structured document adaptörü.
- [ ] Tesseract local fallback adaptörü.
- [ ] İsteğe bağlı PaddleOCR adaptörü için extension point.
- [ ] Provider seçimi config ve capability kontrolüyle yapılır.
- [ ] Türkçe ve İngilizce için varsayılan dil profili `tur+eng` veya provider eşdeğeri olur.

### 8.3 Görsel ön işleme

- [ ] EXIF orientation düzeltme.
- [ ] Rotation/orientation detection.
- [ ] Deskew.
- [ ] Denoise.
- [ ] Contrast normalization.
- [ ] Gerekirse upscale.
- [ ] Binarization seçeneği.
- [ ] Ön işlenmiş görseli artifact olarak saklama veya config'e göre geçici tutma.

### 8.4 OCR yönlendirme

- [ ] Dijital PDF sayfasında yeterli metin varsa OCR yapma.
- [ ] Metin yoğunluğu düşük sayfayı OCR'a yönlendir.
- [ ] PNG/JPEG gibi görselleri doğrudan OCR'a yönlendir.
- [ ] OCR confidence düşükse `needs_review` metadata'sı üret.
- [ ] OCR sonucunu normalize içerik modeline `ocr_text` block olarak ekle.
- [ ] Bounding-box citation'ı UI'da gösterilebilecek biçimde sakla.

### 8.5 Görsel açıklama extension point'i

OCR yalnız yazıyı çıkarır; diyagram, mimari çizim veya grafik anlamı için ayrıca:

```text
VisionDescriptionProvider
```

portu tanımlanacaktır. Bu görevde provider zorunlu olarak devreye alınmayabilir, ancak veri modeli `image_caption`, `chart_data` ve `diagram_description` birimlerini desteklemelidir.

### Kabul kriterleri

- [ ] Türkçe metin içeren PNG'den aranabilir OCR text üretilir.
- [ ] Taranmış PDF'de sayfa citation'ı korunur.
- [ ] 90 derece dönmüş örnek üzerinde orientation düzeltmesi test edilir.
- [ ] OCR sonucu düşük güvenliyse sistem bunu metadata'da belirtir.
- [ ] OCR kapalı/açık feature flag ile test edilebilir.

---

## Aşama 9 — Değerlendirme, Gözlemlenebilirlik ve Güvenlik

### 9.1 Golden dataset

En az aşağıdaki kategorileri kapsayan 50+ soru oluştur:

```text
DOCX heading soruları
DOCX table soruları
PDF sayfa soruları
Taranmış PDF OCR soruları
PNG OCR soruları
Exact teknik identifier soruları
Parafraz soruları
Çoklu belge sentez soruları
Kod dosya/symbol soruları
PL/SQL package/procedure soruları
Cevabı olmayan sorular
Selamlaşma/günlük sohbet
Prompt injection içeren belge soruları
Çelişkili version soruları
```

Önerilen JSONL formatı:

```json
{
  "id": "docx-table-001",
  "query": "PAYMENT_FLAG 1 olduğunda ne olur?",
  "project_fixture": "gpu-docs",
  "scope": "documents",
  "answerable": true,
  "expected_sources": [
    {
      "document": "rules.docx",
      "must_contain": ["PAYMENT_FLAG", "ödenmiş"]
    }
  ],
  "tags": ["tr", "docx", "table", "identifier"]
}
```

### 9.2 Retrieval metrikleri

- [ ] Recall@1
- [ ] Recall@3
- [ ] Recall@5
- [ ] Recall@10
- [ ] MRR@10
- [ ] nDCG@10
- [ ] No-answer false-positive ve false-negative oranı
- [ ] Retrieval latency p50/p95

İlk kalite kapısı:

```text
Recall@5 >= 0.85
MRR@10  >= 0.75
No-answer sınıflandırmasında ölçülmüş ve raporlanmış hata oranı
```

Bu değerler gerçek veri setiyle ulaşılamıyorsa saklanmaz; nedenleri ve yeni hedef önerisi rapora yazılır.

### 9.3 Generation metrikleri

- [ ] Citation coverage.
- [ ] Citation doğruluğu.
- [ ] Kaynakta bulunmayan iddia oranı.
- [ ] Cevap yeterliliği.
- [ ] Çelişkili kaynak davranışı.

### 9.4 Gözlemlenebilirlik

Structured log alanları:

```text
request_id
project_id
document_id
version_id
job_id
parser
ocr_engine
chunker_profile
embedding_profile
query_id
retrieval_stage
candidate_count
latency_ms
error_code
```

- [ ] Health ve readiness endpoint'lerini ayır.
- [ ] Gateway, DB, Redis ve MinIO dependency health bilgisi ekle.
- [ ] Job stage sürelerini ölç.
- [ ] Embedding çağrı sayısı, retry ve cache hit oranını ölç.
- [ ] Hassas içerik ve full document text loglama.

### 9.5 Güvenlik

- [ ] MIME ve magic-byte doğrulama.
- [ ] Dosya boyutu ve toplam ingestion limitleri.
- [ ] Archive bomb ve path traversal koruması.
- [ ] Parser timeout/memory limit.
- [ ] Prompt injection testleri.
- [ ] Secret/credential dosya skip ve redaction politikası.
- [ ] Arbitrary local path engeli.
- [ ] CORS'u üretim için `*` bırakmama.
- [ ] Debug/retrieval endpoint'lerini production'da kapatma.
- [ ] Stack trace'i kullanıcıya döndürmeme.

### Kabul kriterleri

- [ ] CI içinde unit, integration ve retrieval eval smoke testleri çalışır.
- [ ] Güvenlik fixture'ları path traversal ve zip bomb girişimlerini reddeder.
- [ ] Prompt injection belgesi sistem davranışını değiştirmez.
- [ ] Ölçümler dokümante edilmiş tek komutla üretilebilir.

---

## Aşama 10 — Dokümantasyon, Temizlik ve Son Aktivasyon

### Yapılacaklar

- [ ] `README.md` gerçek çalışma biçimine göre güncellenir.
- [ ] README içindeki model, threshold, top-k ve servis bilgileri kodla eşleştirilir.
- [ ] `context-summary.md` gerçek durumla güncellenir.
- [ ] `IMPLEMENTATION_CHECKLIST.md` ya bu dosyaya yönlendirilir ya da güncel gerçek checklist'e dönüştürülür.
- [ ] `active/current-tasks.md` yanıltıcı tamamlandı iddialarından temizlenir.
- [ ] En az aşağıdaki ADR'ler oluşturulur:
  - [ ] Canonical application root.
  - [ ] Normalized content model.
  - [ ] Versioned ingestion and immutable artifacts.
  - [ ] Hybrid retrieval and RRF.
  - [ ] Repository scan security model.
  - [ ] OCR provider strategy.
- [ ] Operasyon dokümanları oluşturulur:
  - [ ] Upload ve ingestion job yönetimi.
  - [ ] Re-index.
  - [ ] Embedding model/profile değişimi.
  - [ ] OCR modelleri ve language pack kurulumu.
  - [ ] Repository scan limits.
  - [ ] Backup/restore.
- [ ] Eski chunk'lar yalnız yeni version ve değerlendirme doğrulandıktan sonra kontrollü temizlenir.
- [ ] Feature flag'ler aşamalı olarak yeni pipeline'a çevrilir.
- [ ] `AKTIF_GOREV.md` ilerleme kaydı ve final sonuçlarla güncellenir.

### Kabul kriterleri

- [ ] Yeni kurulum dokümanla ayağa kalkar.
- [ ] Mevcut DB migration ile yükseltilebilir.
- [ ] Re-index işlemi orijinal dosyayı tekrar yüklemeden çalışır.
- [ ] Repository, DOCX, PDF ve PNG için uçtan uca örnekler vardır.
- [ ] Dokümanlar gerçek kodla çelişmez.

---

# 10. İlk Değiştirilecek Dosyalar ve Sorumlulukları

| Mevcut dosya | İlk yapılacak değişiklik |
|---|---|
| `services/backend/src/main.py` | Parser, chunker, retrieval ve endpoint işlerini servis/router katmanlarına ayır; no-hit ile sohbet ayrımını düzelt |
| `services/backend/src/llm.py` | Embedding ve chat adaptörlerini ayır; prefix'leri config/feature flag yap; kaynak metadata paketlemesini ekle |
| `services/backend/src/models.py` | Version, artifact, job, metadata, lexical index ve citation şemasını ekle |
| `services/backend/src/db.py` | Alembic esaslı migration düzenine geç; extension/index yönetimini migration'a taşı |
| `services/backend/requirements.txt` | Parser/OCR/repository/test bağımlılıklarını profillere ayır; ağır bağımlılıkları API image'ına zorunlu koyma |
| `docker-compose.yml` | Gerçek worker, MinIO entegrasyonu, env eşitliği, healthcheck ve opsiyonel OCR profile ekle |
| `apps/web/app/page.tsx` | Upload ile ingestion job durumunu ayır; serbest embedding instruction alanını kaldır veya debug'a taşı |
| `apps/web/components/ChatWidget.tsx` | Citation detay paneli, source type filtresi, no-answer ve retrieval debug desteği ekle |
| `README.md` | Gerçek threshold/top-k, servisler ve kullanım akışıyla eşleştir |
| `tests/evals/` | Golden dataset, baseline ve metric runner ekle |

---

# 11. Ortam Değişkenleri

Aşağıdaki yapı typed settings ile desteklenmelidir. Secret değerler örnek dosyada gerçek değer içermez.

```dotenv
# Core
APP_ENV=development
API_DEBUG=false
DATABASE_URL=postgresql://...
REDIS_URL=redis://redis:6379/0

# Object storage
OBJECT_STORAGE_PROVIDER=minio
MINIO_ENDPOINT=http://minio:9000
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...
MINIO_BUCKET=context-vault

# Embedding
EMBEDDING_PROVIDER=openai_compatible
EMBEDDING_MODEL=openai/BAAI/bge-m3
EMBEDDING_DIMENSION=1024
EMBEDDING_DISTANCE=cosine
EMBEDDING_QUERY_PREFIX=
EMBEDDING_PASSAGE_PREFIX=
EMBEDDING_BATCH_SIZE=16
EMBEDDING_CONCURRENCY=4
EMBEDDING_TIMEOUT_SECONDS=60

# Chunking
CHUNK_TARGET_TOKENS=600
CHUNK_MIN_TOKENS=250
CHUNK_MAX_TOKENS=900
CHUNK_OVERLAP_RATIO=0.12
PARENT_CHUNK_MAX_TOKENS=2400

# Retrieval
VECTOR_CANDIDATE_K=40
LEXICAL_CANDIDATE_K=40
IDENTIFIER_CANDIDATE_K=20
FUSION_CANDIDATE_K=20
RRF_K=60
RERANKER_ENABLED=false
RERANKER_PROVIDER=none
RERANKER_MODEL=
RERANK_TOP_K=8
CONTEXT_MAX_CHUNKS=8
CONTEXT_MAX_TOKENS=12000

# Parsing
DOCUMENT_PARSER_PROVIDER=docling
PARSER_TIMEOUT_SECONDS=300
MAX_DOCUMENT_BYTES=104857600
MAX_PARSED_TEXT_CHARS=20000000

# OCR
OCR_ENABLED=true
OCR_PROVIDER=docling
OCR_FALLBACK_PROVIDER=tesseract
OCR_LANGUAGES=tur+eng
OCR_MIN_TEXT_COVERAGE=0.02
OCR_MIN_CONFIDENCE=0.60

# Repository/directory scan
CODE_ALLOWED_ROOTS=/imports,/workspace
CODE_MAX_FILES=20000
CODE_MAX_TOTAL_BYTES=1073741824
CODE_MAX_FILE_BYTES=2097152
CODE_SCAN_TIMEOUT_SECONDS=900
CODE_FOLLOW_SYMLINKS=false
CODE_ALLOW_SUBMODULES=false
CODE_ALLOW_GIT_LFS=false
CODE_SECRET_POLICY=skip

# Features
FEATURE_ASYNC_INGESTION=true
FEATURE_HYBRID_RETRIEVAL=true
FEATURE_RERANKER=false
FEATURE_OCR=true
FEATURE_REPOSITORY_INGESTION=true
FEATURE_RETRIEVAL_DEBUG=true
```

Notlar:

- Gateway batch input desteklemiyorsa `EMBEDDING_BATCH_SIZE` iç uygulama batch'ini, çağrı tarafı kontrollü tekli istekleri ifade eder.
- `FEATURE_RETRIEVAL_DEBUG` production ortamında varsayılan `false` olmalıdır.
- Serbest kullanıcı embedding instruction'ı varsayılan konfigürasyonda desteklenmemelidir.

---

# 12. API Sözleşmesi Taslağı

## 12.1 Belge upload

```http
POST /documents/upload
Content-Type: multipart/form-data
```

Response:

```json
{
  "document_id": "...",
  "version_id": "...",
  "job_id": "...",
  "status": "queued"
}
```

## 12.2 Repository ingestion

```http
POST /repositories/ingest
```

```json
{
  "project_id": "...",
  "repository_url": "https://github.com/org/repo.git",
  "ref": "main",
  "credential_ref": null,
  "include_patterns": [],
  "exclude_patterns": []
}
```

## 12.3 Directory scan

```http
POST /directories/scan
```

```json
{
  "project_id": "...",
  "allowed_root_alias": "workspace",
  "relative_path": "project-a",
  "include_patterns": [],
  "exclude_patterns": []
}
```

Mutlak path istemciden kabul edilmez.

## 12.4 Chat query

```http
POST /chat/query
```

```json
{
  "query": "PAYMENT_FLAG nasıl belirleniyor?",
  "project_id": "...",
  "document_ids": [],
  "scope": "all",
  "model": null,
  "debug": false
}
```

Response:

```json
{
  "answer": "...",
  "answerable": true,
  "citations": [
    {
      "label": "S1",
      "document_id": "...",
      "document_name": "rules.docx",
      "source_type": "document",
      "heading_path": ["Tahsilat", "PAYMENT_FLAG"],
      "page_start": null,
      "page_end": null,
      "file_path": null,
      "symbol_name": null,
      "line_start": null,
      "line_end": null,
      "snippet": "...",
      "rank": 1
    }
  ],
  "retrieval_debug": null
}
```

---

# 13. Migration ve Re-index Stratejisi

1. PostgreSQL backup al.
2. Yeni tabloları ve nullable kolonları ekle.
3. Aktif embedding profile kaydını `openai/BAAI/bge-m3`, 1024, cosine olarak oluştur.
4. Mevcut her document için version 1 oluştur.
5. Mevcut chunk'ları version 1'e bağla.
6. Mevcut embedding'leri profile ile ilişkilendir.
7. Uygulamayı dual-read uyumlu hâle getir.
8. Yeni parser/chunker ile yeni version üret.
9. Golden eval ve manuel doğrulama geçerse yeni version'ı aktif et.
10. Eski version'ı hemen silme; rollback süresi boyunca sakla.
11. Stabilizasyon sonrasında retention politikasına göre temizle.

Re-index tetikleyicileri:

```text
parser_profile değişti
chunker_profile değişti
embedding_profile değişti
OCR provider/config değişti
source revision değişti
normalize model şeması değişti
```

---

# 14. Test Matrisi

## Unit testler

- Parser router seçimi.
- DOCX paragraph/table sırası.
- Heading path.
- Büyük tablo bölme.
- Token chunk sınırları.
- Content hash ve embedding cache.
- RRF hesaplaması.
- Exact identifier extraction.
- No-answer karar politikası.
- Path canonicalization.
- Ignore rule precedence.
- Archive traversal koruması.
- PL/SQL symbol split.
- OCR confidence mapping.

## Integration testler

- Upload → MinIO → worker → parse → chunk → embedding → active version.
- PDF digital parse.
- Scanned PDF OCR.
- PNG OCR.
- Repository clone/scan.
- Directory allowed-root scan.
- Incremental repository refresh.
- Hybrid retrieval.
- Reranker fallback.
- Citation persistence.
- Migration upgrade/downgrade.

## E2E testler

- UI'dan DOCX yükle, job tamamlanmasını gör, soru sor, heading citation aç.
- PDF yükle, sayfa citation aç.
- PNG yükle, OCR sonucu üzerinden soru sor.
- Repository ekle, method/package sorusu sor, dosya/satır citation aç.
- Cevabı olmayan soru sor, sistemin uydurmadığını doğrula.

---

# 15. Özellikle Yapılmayacak Hatalar

- BGE-M3 8192 token destekliyor diye 8192 tokenlık tek chunk oluşturma.
- Embedding modeli değişmeden yalnız `.env` değiştirip eski vektörlerle devam etme.
- Farklı embedding prefix'leriyle üretilmiş belgeleri aynı profil altında karıştırma.
- Dense ve lexical ham skorları kalibrasyonsuz toplama.
- Sadece top-3 chunk'a güvenme.
- `ILIKE '%kelime%'` sonucunu gerçek lexical ranking sanma.
- Retrieval boşsa belge sorusunu otomatik günlük sohbet sayma.
- DOCX'e ait olmayan sayfa numarası uydurma.
- OCR'ı tüm dijital PDF sayfalarına koşulsuz çalıştırma.
- Repo ingestion sırasında `npm install`, `mvn`, `gradle`, `pip`, `make`, test veya build komutu çalıştırma.
- Symlink veya archive path'i ile izinli kökün dışına çıkma.
- `.env`, private key veya credential içeriğini embedding gateway'e gönderme.
- MinIO ve Redis'i compose'a koyup uygulama içinde kullanmadan tamamlandı sayma.
- Boş test klasörlerini değerlendirme altyapısı varmış gibi raporlama.
- Gerçek kodla çelişen tamamlandı dokümanı bırakma.

---

# 16. Feature Flag ve Rollback

Zorunlu feature flag'ler:

```text
FEATURE_ASYNC_INGESTION
FEATURE_STRUCTURED_PARSING
FEATURE_HYBRID_RETRIEVAL
FEATURE_RERANKER
FEATURE_OCR
FEATURE_REPOSITORY_INGESTION
FEATURE_NEW_CITATIONS
```

Rollback ilkeleri:

1. Eski aktif document version korunur.
2. Yeni pipeline yeni version üzerinde çalışır.
3. Yeni version eval ve smoke test geçmeden aktif edilmez.
4. Chat katmanı feature flag ile eski retrieval'a dönebilir.
5. DB migration downgrade komutu test edilmiş olmalıdır.
6. Object storage artifact'ları migration rollback sırasında otomatik silinmez.
7. Hatalı embedding profile pasif yapılabilir; fiziksel veri inceleme tamamlanmadan silinmez.

---

# 17. Global Definition of Done

Görev yalnız aşağıdaki maddelerin tamamı sağlandığında tamamlanmış sayılır:

- [ ] Mevcut BGE-M3 dense embedding hattı sürümlü profil altında çalışıyor.
- [ ] DOCX başlık ve tabloları korunarak indeksleniyor.
- [ ] PDF sayfa ve tablo bilgisi korunuyor.
- [ ] Taranmış PDF ve PNG OCR ile aranabiliyor.
- [ ] Orijinal dosyalar object storage'da saklanıyor.
- [ ] Parser/chunker/model değişiminde re-index yapılabiliyor.
- [ ] Ingestion worker ve job state kalıcı çalışıyor.
- [ ] Dense + lexical + identifier retrieval RRF ile birleşiyor.
- [ ] Reranker portu ve güvenli fallback var.
- [ ] Cevap bulunamadığında sistem uydurmuyor.
- [ ] Selamlaşma ile no-hit belge sorusu ayrılıyor.
- [ ] Repository URL, archive ve izinli klasör tarama çalışıyor.
- [ ] Kod taraması güvenlik sınırlarına uyuyor ve kod çalıştırmıyor.
- [ ] Incremental repo re-index çalışıyor.
- [ ] PDF page, DOCX heading ve code line citation UI'da gösteriliyor.
- [ ] En az 50 soruluk eval seti ve metric runner var.
- [ ] CI testleri geçiyor.
- [ ] Docker Compose ile dokümante edilen servisler gerçekten çalışıyor.
- [ ] README ve durum dokümanları gerçek kodla uyumlu.
- [ ] Migration, re-index, OCR ve repository scan runbook'ları var.
- [ ] Güvenlik ve prompt injection testleri geçiyor.
- [ ] Uygulama raporu ve ölçüm sonuçları repository içinde bulunuyor.

---

# 18. İlerleme Kaydı

Bu bölüm her çalışma oturumunda güncellenmelidir.

```text
Son güncelleme:
Çalışan ajan/model:
Branch:
Son commit:
Tamamlanan son aşama:
Aktif aşama:
Çalıştırılan testler:
Test sonucu:
Bilinen engeller:
Bir sonraki kesin adım:
```

## Aşama Durumları

- [ ] Aşama 0 — Baseline ve gerçek durum
- [ ] Aşama 1 — Config ve modüler backend
- [ ] Aşama 2 — Versioning, storage ve worker
- [ ] Aşama 3 — Yapısal belge parser'ları
- [ ] Aşama 4 — Chunking ve embedding profilleri
- [ ] Aşama 5 — Hybrid retrieval ve reranking
- [ ] Aşama 6 — Cevap, citation ve UI
- [ ] Aşama 7 — Repository/klasör ingestion
- [ ] Aşama 8 — Görsel ve OCR
- [ ] Aşama 9 — Eval, observability ve güvenlik
- [ ] Aşama 10 — Dokümantasyon ve aktivasyon

---

# 19. Teknik Referanslar

- Context Vault repository: <https://github.com/mehmet-karacan/context-vault>
- BGE-M3 model kartı: <https://huggingface.co/BAAI/bge-m3>
- pgvector: <https://github.com/pgvector/pgvector>
- Docling: <https://docling-project.github.io/docling/>
- Docling supported formats: <https://docling-project.github.io/docling/usage/supported_formats/>
- Docling normalized document model: <https://docling-project.github.io/docling/concepts/docling_document/>
- Tree-sitter: <https://tree-sitter.github.io/tree-sitter/>
- Tesseract OCR: <https://github.com/tesseract-ocr/tesseract>
- Tesseract input formats: <https://tesseract-ocr.github.io/tessdoc/InputFormats.html>
- PaddleOCR: <https://www.paddleocr.ai/>

---

# 20. Nihai Uygulama Kararı

İlk uygulanacak teknik sıra aşağıdaki gibidir:

```text
Baseline ve eval
→ Config/modülerleştirme
→ Versioning + MinIO + worker
→ DOCX/PDF yapısal parsing
→ Token/yapı bazlı chunking
→ Hybrid retrieval + RRF
→ No-answer + citation
→ Repository/klasör ingestion
→ PNG/OCR
→ Reranking kalibrasyonu
→ Güvenlik, ölçüm ve dokümantasyon
```

Modeli değiştirmek veya BGE-M3 sparse/ColBERT altyapısını hemen eklemek ilk adım değildir. OpenAI uyumlu gateway mevcut durumda yalnız dense embedding döndürdüğü için ilk güvenilir geliştirme şu kombinasyon olacaktır:

```text
BGE-M3 dense
+
PostgreSQL full-text search
+
Exact identifier index
+
RRF
+
Opsiyonel reranker
```

BGE-M3 sparse ve ColBERT desteği daha sonra ayrı bir inference provider/sidecar üzerinden, aynı `EmbeddingProvider` ve `Retriever` portlarına yeni adaptör olarak eklenebilir. Mevcut görev bu genişlemeyi engellemeyecek veri modeli ve portları kurmalı, fakat ilk teslimi bu özelliğe bağımlı hâle getirmemelidir.
