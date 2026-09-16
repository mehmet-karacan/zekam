# Ortak MCP Registry

Zekam, MCP sunucu tanimlarini tek bir kanonik kayittan OpenCode, Codex ve Claude Code'un
yerel yapilandirmalarina projekte eder. Kayitlar secret degeri tasimaz; yalniz ortam
degiskeni adlarini referanslar.

## Guvenlik modeli

- Degisiklikler varsayilan olarak dry-run planidir.
- Yazma islemi exact `--plan-digest` ve `--uygula` ister.
- Her yazma claim-before-effect ve terminal receipt ile kaydedilir.
- Zekam yalniz kendi receipt'iyle sahiplendigi girdiyi otomatik gunceller veya siler.
- Native dosyada disaridan degisiklik algilanirsa islem durur.
- Native config'in tam kopyasi veya secret degeri backup/receipt'e yazilmaz.
- Provider veya network cagrisi yapilmaz.

## Durum

```powershell
zekam mcp status --json
```

## STDIO sunucu ekleme

Once plani uretin:

```powershell
zekam mcp add ornek-server `
  --command ornek-mcp `
  --arg serve `
  --env-var ORNEK_API_TOKEN `
  --json
```

Sonuctaki `plan_digest` ile ayni komutu uygulayin:

```powershell
zekam mcp add ornek-server `
  --command ornek-mcp `
  --arg serve `
  --env-var ORNEK_API_TOKEN `
  --plan-digest sha256:... `
  --uygula `
  --json
```

Yalniz secili istemcilere dagitmak icin `--client opencode`, `--client codex` veya
`--client claude-code` birden cok kez verilebilir. Ayni adda kullanici tarafindan
yonetilen bir native kayit varsa ilk sahiplenme icin `--adopt` gerekir.
`--adopt` mevcut girdinin yonetimini Zekam'a devreder; onceki kullanici girdisinin tam
kopyasi secret guvenligi nedeniyle saklanmaz. Bu ilk islemin rollback'i girdiyi kaldirir.

## HTTP sunucu ekleme

```powershell
zekam mcp add uzak-server `
  --url https://example.test/mcp `
  --bearer-token-env-var ORNEK_API_TOKEN `
  --json
```

URL icinde credential kabul edilmez.
Bearer token referansi kullaniliyorsa URL mutlaka HTTPS olmalidir.

## Yeniden senkronlama

```powershell
zekam mcp sync --json
zekam mcp sync --plan-digest sha256:... --uygula --json
```

Kurulu olmayan ama etkin bir istemci `pending-not-installed` olarak kalir. Istemci
kurulduktan sonra ayni registry `sync` ile native yapilandirmaya yazilir.

## Kaldirma ve geri alma

```powershell
zekam mcp remove ornek-server --json
zekam mcp remove ornek-server --plan-digest sha256:... --uygula --json
```

Son tamamlanmis MCP islemi receipt ozetiyle geri alinabilir:

```powershell
zekam mcp rollback --receipt sha256:... --json
zekam mcp rollback --receipt sha256:... --plan-digest sha256:... --uygula --json
```

Rollback yalniz en son MCP receipt'i icin desteklenir; boylece aradaki degisiklikler
sessizce ezilmez.

## Yerlesik Innova Atlassian sunucusu

Yerlesik salt-okunur Jira ve Confluence araci su komutla calisir:

```powershell
zekam mcp serve innova-atlassian
```

Registry kaydi bu komutu `JIRA_API_TOKEN` ve `CONFLUENCE_API_TOKEN` ortam degiskeni
referanslariyla istemcilere dagitir. Token degerleri registry, plan, receipt veya native
istemci yapilandirmasina kopyalanmaz.
