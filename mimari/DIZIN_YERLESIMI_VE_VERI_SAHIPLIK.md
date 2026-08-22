# Dizin Yerleşimi ve Veri Sahipliği

## Source repository

```text
zekam/
├── src/zekam/
│   ├── domain/
│   ├── application/
│   ├── infrastructure/
│   └── interfaces/
├── migrations/
├── schemas/
├── config/
├── tests/
├── docs/
├── scripts/
├── compose/
└── pyproject.toml
```

Domain framework/ORM/provider SDK import etmez. Application port ve use-case'leri;
infrastructure PostgreSQL, Git, provider, object storage ve client adapter'larını;
interfaces CLI/API/MCP/UI composition'ını taşır.

## ZEKAM_HOME

```text
<ZEKAM_HOME>/
├── layout.json
├── global/
│   ├── modeller/
│   ├── politikalar/
│   ├── bellek/
│   ├── raporlar/
│   ├── artifacts/
│   └── runtime/
├── projeler/
│   └── <project-id>/
│       ├── proje.json
│       ├── baglantilar/
│       ├── talepler/
│       ├── defectler/
│       ├── isler/
│       ├── arastirmalar/
│       ├── kararlar/
│       ├── planlar/
│       ├── bilgi/
│       ├── bellek/
│       ├── artifacts/
│       ├── runtime/
│       └── raporlar/
├── gelen-belgeler/
├── worktrees/
├── sandboxlar/
├── kilitler/
├── secrets/
└── yerel/
```

Üretimde kanonik kayıtlar PostgreSQL'dedir; bu dizin artifact, local cache, source binding,
worktree ve portable projection alanıdır. Aynı identity iki fiziksel location'da authority
olarak bulunamaz.

## Sahiplik sınıfları

| Sınıf | Örnek | Backup | Git |
|---|---|---|---|
| core | code, migration, schema, default policy | source control | evet |
| user-data | project/work/decision/approved memory | evet | hayır |
| runtime | lease, queue, active checkpoint | kontrollü | hayır |
| derived | FTS/vector/dashboard projection | yeniden üretilebilir | hayır |
| artifact | original document, patch, report | policy'ye göre | hayır |
| local | absolute locator, client cache | makine özel | hayır |
| secret | key/password/token | secret-store politikası | asla |

## Haricî source

Source root Zekam'nin sahibi değildir. Zekam:
- read-only keşfeder,
- digest/revision bağlar,
- content'i gerektiğinde yerinde okur,
- change için detached worktree üretir,
- source root'u kendi home'una kopyalamaz.

Repository tarama artifact'i gerekiyorsa yalnız kullanıcı tarafından yüklenmiş archive veya
approved immutable snapshot object storage'da tutulur; source-of-truth değildir.
