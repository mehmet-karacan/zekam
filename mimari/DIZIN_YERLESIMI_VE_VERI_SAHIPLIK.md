# Dizin Yerleşimi ve Veri Sahipliği

## Source repository

```text
zekam/
├── src/zekam/
│   ├── domain/          # saf sözleşmeler ve invariants
│   ├── application/     # use-case, port ve orchestration
│   ├── infrastructure/  # SQLite, filesystem, Git ve provider adapter'ları
│   └── interfaces/      # CLI ve API
├── tests/
├── config/
├── schemas/
├── docs/
└── mimari/
```

Source repository kod, schema, fixture ve insan tarafından yazılmış belgeleri taşır;
runtime kullanıcı verisi taşımaz.

## Kullanıcı veri kökü

`ZEKAM_HOME` source repository'den fiziksel olarak ayrıdır ve varsayılanı `~/.zekam`dır.
Fresh bootstrap schema v1 ile atomik staging→publish yapar.

```text
ZEKAM_HOME/
├── state/               # operational SQLite authority
├── knowledge/           # rebuildable FTS5/sqlite-vec generations
├── analytics/           # rebuildable DuckDB generations
├── artifacts/           # immutable CAS ve benchmark/raw evidence
├── knowledge-files/     # user-authored ve generated Markdown ayrımı
├── runtime/             # spool, locks, checkpoints ve receipts
├── backups/             # doğrulanmış local backup bundles
└── config/              # secret içermeyen local profile/projection
```

Portable kayıtlarda absolute kullanıcı yolu bulunmaz. Project ID, logical source binding,
repository-relative locator ve içerik digest'i kullanılır.

## Sahiplik matrisi

| Veri | Authority | Projection | Recovery |
|---|---|---|---|
| Project/work/session/run | Operational SQLite | Markdown/UI | doğrulanmış backup restore |
| Queue/claim/receipt/audit | Operational SQLite append-only | report/analytics | backup ve reconciliation |
| Source/artifact bytes | exact source + CAS | parsed chunks | source/CAS doğrulaması |
| Exact/lexical/vector index | source manifestten türetilir | query result | scratch rebuild |
| Memory/failure/skill state | Operational SQLite + evidence refs | Markdown memory | backup/review |
| Model registry/current health | Operational SQLite | local reports | rediscovery/reconcile |
| Benchmark raw result | immutable artifact/CAS | DuckDB aggregate | artifacttan rebuild |
| Telemetry raw event | immutable segment + manifest | DuckDB/report | segmentten rebuild |
| DuckDB | derived | dashboard | tam rebuild |

## External proje sınırı

- Proje source'u registry'de exact logical binding ile yerinde tutulur; kopyalanmaz.
- Salt-okunur görevler kaynak dosyalarını değiştirmez.
- Mutation yalnız açık yetki, exact path scope ve tek-writer lock ile gerçek rootta yapılır.
- Projenin kendi DB seçimi Zekam core DB seçimi değildir; proje verisi Zekam'a taşınmaz.
- Akıllı Kasa acceptance fixture'ı read-only'dir; `.env`, credential, gerçek finansal veri,
  binary, generated DB ve kullanıcıya özel içerik ingestion dışında kalır.

## Yasak çift authority örnekleri

- Operational state'i Markdown veya vector sonuçlarından geri üretmek.
- SQLite queue ile başka bir queue'yu aynı iş için eşzamanlı authority yapmak.
- DuckDB'ye operational mutation yazmak.
- Generated projection'ı user-authored notun üstüne yazmak.
- Legacy server verisini bootstrap girdisi veya fallback yapmak.
