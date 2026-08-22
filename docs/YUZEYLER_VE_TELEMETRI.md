# Yüzeyler, telemetri ve projeksiyonlar

## Tek sözleşme, çok yüzey

CLI, API ve MCP aynı use-case'i çağırır; yüzey kendi ürün kuralını yazmaz.
Kanonik komut sözleşmesi `CANONICAL_COMMANDS` içindedir ve iki değişmez taşır:

- **Mutasyon yapan her komut açık `--uygula` bayrağı ister.** Sözleşme bunu alan
  düzeyinde zorlar; `mutating=True` olup bayrak istemeyen bir komut tanımlanamaz.
- Salt okunur komut authorization istemez.

```bash
zekam surface contract --json   # kanonik yuzey
zekam surface check --json      # sozlesme ile kayitli komutlari karsilastirir
```

`surface check` belge ile kod arasındaki sapmayı yakalar: sözleşmede olup kayıtlı
olmayan komut varsa çıkış kodu 1.

## Telemetri

Yapısal span'ler **correlation zorunlu**, **içerik yasak** kuralıyla çalışır.

Reddedilen alan adları: `secret`, `credential`, `password`, `api_key`,
`private_key`, `token`, `authorization`, `cookie`, **`prompt`**, **`response`**,
**`content`**, **`body`**. Reddedilen değerler: PEM başlıkları, `Bearer` token'ları,
uzun base64 dizgeleri ve kişisel absolute path'ler.

Bu, kaynak içeriğinin ve model çıktısının telemetriye sızmasını yapısal olarak
engeller — filtreleme çalışma zamanında değil, alan oluşturulurken yapılır.

Her span `trace_id` ve `span_id` taşır; `parent_span_id` kendisi olamaz ve aynı
anahtar iki kez eklenemez. `correlate()` trace kimliğinden span kimliklerine
eşleme üretir.

## Dashboard

Salt okunur ve authority üretmez (`read_only` ve `grants_authority` alanları
kapatılamaz). Altı projeksiyon zorunludur: `work`, `run`, `model`, `knowledge`,
`memory`, `scheduler`.

**Her kare kanonik kayda drill-down bağlantısı taşımak zorundadır** — bir sayı
gösterip kaynağını göstermemek kabul edilmez.

## Türetilmiş graf

Sinaps görünümü `derived = true` taşır ve bu kapatılamaz: graf kaybolursa yeniden
üretilir, state kaybolmaz. Her düğüm kanonik referans taşır ve `drill_down()` ile
kaynağa inilir. Kenar bilinmeyen düğüme veya kendine bağlanamaz.

## MCP adaptörü

MCP bir adapter sınırıdır; **authority Zekam'de kalır** (`authority_owner` alanı
`zekam` dışında olamaz). Yetenekler istemciyle uzlaşılır: istemcinin desteklemediği
tür sunulmaz, reddedilenler görünür kalır.

Mutasyon yapan bir MCP aracı authorization istemek zorundadır.
