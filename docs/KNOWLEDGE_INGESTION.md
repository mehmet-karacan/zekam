# Knowledge Plane ingestion

## Değişmez orijinal

Her kaynak önce **değiştirilemez artifact** olarak saklanır: içerik digest'i, bayt
boyutu, medya türü ve portable orijinal ad. `knowledge.artifact` üzerinde update ve
delete trigger ile reddedilir. Orijinal ad absolute path veya traversal taşıyamaz
(hem Python hem check constraint).

## Aşamalı ingestion

```text
validated -> stored -> parsed -> normalized -> indexed -> activated
```

- Aşamalar **sıralı** ilerler; atlama `ValidationFailed`, geri alma veritabanı
  trigger'ı ile reddedilir.
- Her aşama kalıcılaştırılır (`save_progress`); crash sonrası kaldığı yerden devam
  edilir.
- Aynı `idempotency_key` ikinci kez iş yaratmaz (`job_idempotent` unique).
- Başarısız iş sessizce devam edemez; `fail()` sonrası `advance()` reddedilir.

## Atomik aktivasyon

Tamamlanmamış ingestion **aktif sürüm üretemez** — hem `SourceVersion.activate()`
hem `version_requires_completed_ingestion` trigger'ı bunu zorlar. Bir kaynağın aynı
anda yalnız bir aktif sürümü olabilir (partial unique index). Yeni sürüm için önce
mevcut sürüm `superseded` yapılır; `superseded` durumu halefini bildirmek zorundadır.

## Normalize içerik

Parser **doğrudan vector üretmez**. Çıktısı locator taşıyan `ContentUnit`'lerdir:

| Kaynak | Locator alanları |
|---|---|
| PDF | `page`, mümkünse `bbox` |
| DOCX | `heading_path`, `block_index` — **uydurma sayfa numarası üretilmez** |
| Markdown/TXT | `heading_path`, `block_index`, satır aralığı |
| OCR | `page`, `bbox`, `confidence` (zorunlu) |
| Kod | `symbol`, `line_start`, `line_end` |
| DB | `object_name` |

Locator'sız birim kabul edilmez — alıntılanamayan içerik indekslenmez. Bu kural
`unit_locator_not_empty` check constraint'i ile veritabanında da geçerlidir. OCR
birimi confidence olmadan yazılamaz.

Parser router bilinmeyen formatı **sessizce metin saymaz**; parser tanımlı değilse
`PolicyViolation`.

## Güvenli tarama

`zekam knowledge scan` ve `inspect` salt okunurdur. Ingestion sırasında build, test,
hook, paket kurulumu ve submodule güncellemesi **çalıştırılmaz**.

- `.git`, `node_modules`, `__pycache__`, `.venv`, `dist`, `build` atlanır.
- Deny list: `.env`, `id_rsa`, `.pem`, `credentials.json`, `.npmrc` ve benzerleri.
- Symlink izlenmez; izinli kök dışına çıkan yol reddedilir.
- İkili dosyalar içerik olarak alınmaz.
- Arşiv **açılmadan** incelenir: girdi sayısı, toplam boyut ve sıkıştırma oranı
  (zip bomb) sınırlanır; `../` girdisi reddedilir.
- Her karar gerekçesiyle raporlanır — neyin neden alınmadığı görünür kalır.

## Kod ve veritabanı

Kod **çalıştırılmaz, yalnız ayrıştırılır**: Python sembolleri AST ile çıkarılır,
her sembol satır aralığı ve revision taşır. Import'lar bağımlılık olarak kaydedilir.
PL/SQL için `package body`, `procedure`, `function`, `trigger`, `view` bildirimleri
metadata olarak çıkarılır (`package body` iki kelimelik türdür; `body` nesne adı
değildir).

Veritabanı kaynakları **metadata-only**'dir: `DatabaseObject(row_data_included=True)`
`PolicyViolation` üretir. Satır verisi ayrı policy ve authorization ister.

## CLI

```bash
zekam knowledge scan <dizin> --json          # salt okunur, kod calistirmaz
zekam knowledge inspect <arsiv> --json       # acmadan inceler
zekam knowledge ingest <belge> --slug <ad> --json          # dry-run
zekam knowledge ingest <belge> --slug <ad> --uygula --json # kanonik store
```

Yerleşik parser'lar harici bağımlılık gerektirmez (Markdown, düz metin). DOCX, PDF
ve OCR için sağlayıcı dışarıdan verilir; sağlayıcı eksik locator döndürürse birim
reddedilir, alan uydurulmaz.
