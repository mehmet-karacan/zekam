# Zekam Ana Mimarisi

## Mimari hedef

Zekam; provider, istemci ve oturumdan bağımsız çalışan, local-first bir mühendislik
kontrol düzlemidir. Kanonik state ile yeniden üretilebilir projection'lar ayrıdır.

```text
CLI / API / lifecycle hooks / scheduler / worker
                    |
             application ports
                    |
  +-----------------+------------------+
  |                                    |
  v                                    v
Operational authority              File plane / CAS
CPython SQLite schema v1            source manifests
  |                                    |
  |          +-------------------------+
  |          |
  v          v
SQLite FTS5 + sqlite-vec          immutable raw events
rebuildable knowledge index          |
  |                                  v
  +----------------------------> DuckDB analytics
                                  derived only
```

## Kalıcı motor sınırı

Zekam core en fazla üç motor sınıfı kullanır:

1. Operational Store: project, work, run, lifecycle, claim/receipt, registry ve
   policy activation state'i.
2. Knowledge Index: exact, lexical, dense ve fusion için sürümlü/rebuildable indeks.
3. Analytics Store: immutable raw girdilerden yeniden kurulan DuckDB projection.

Markdown, Git ve CAS veri/kaynak alanıdır; dördüncü bir operational DB değildir.

## Temel değişmezler

- Legacy server veritabanı yeni sistemin veri kaynağı değildir; bağlantı, migration,
  dump, export/import, ETL ve fallback yasaktır.
- Docker, ayrı DB servisi, port, kullanıcı veya parola core başlangıç gereksinimi değildir.
- Operational state vektör, memory, Markdown, dashboard veya analytics'ten türetilmez.
- Knowledge index ve analytics silinip güncel source manifest/raw eventlerden yeniden
  üretilebilir.
- Her mutation exact claim/effect/receipt zincirine ve tek-writer kaynağına bağlıdır.
- Secret değerleri model context'i, log, indeks, rapor veya artifact'a girmez.
- Proje veritabanı Zekam operational store'una taşınmaz; metadata erişimi satır verisi
  toplamadan, proje kapsamı ve açık yetkiyle yapılır.

## Provider ve platform ayrımı

Embedding provider ile storage provider bağımsızdır. Mac çalışma profili gerçek yerel
BAAI/bge-m3 1024 çıktısı kullanır. Gerçek provider yoksa dense yol semantic gibi
gösterilmez; açıkça lexical-only degraded olur. Windows/OpenCode uzak provider
sözleşmesi korunur, fakat K-013 kapsamında Mac başlangıcının bağımlılığı değildir.

Mac kabulü Windows veya büyük ölçek kabulü değildir. Platformlar aynı fixture, manifest,
fault ve package matrislerini kendi ortamlarında ayrı çalıştırır.

## Execution ve öğrenme

Runtime queue, lease, fencing, lock, claim, receipt ve recovery state'i operational
store'dadır. Memory/failure/skill ve improvement kayıtları candidate→review→active
akışı izler. Root instruction, schema, security, retention ve dış etki değişiklikleri
insan onayı olmadan aktive edilemez.

## Authority sırası

1. `AKTIF_GOREV.md` yaşayan görev ve kapsam authority'sidir.
2. Operational Store yalnız bu exact digest'e bağlı execution/progress state'i tutar.
3. `AKTIF_GOREV.yaml` read-only generated projection'dır ve yetki vermez.
4. Knowledge, memory, analytics, dashboard ve Markdown projection'ları authority değildir.
