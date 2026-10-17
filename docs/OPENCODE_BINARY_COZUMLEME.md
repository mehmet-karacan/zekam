# OpenCode binary cozumleme

Arastirma ve explicit test gelistirme CLI yollarinin executable secimi ortak
`infrastructure/clients/opencode_executable.py` resolver'ina baglidir.

- PATH'teki komut veya explicit dosya cozumlenir; shell calistirilmaz.
- Windows'ta `opencode`/`opencode2` PATH'te yoksa `APPDATA` veya `LOCALAPPDATA`
  altindaki npm global `node_modules` kokleri yalniz iki destekli paket adi icin
  aranir; npm veya shell calistirilmaz.
- Windows `.cmd`, `.bat`, `.ps1` shim'i bounded UTF-8 olarak okunur.
- Shim'in isaret ettigi `@opencode/cli` veya eski `opencode-ai` npm paketinin
  `bin/opencode.exe` dosyasi secilir. Ikisi kuruluysa shim'in referansi belirleyicidir.
- Shim yokken iki paket de kuruluysa belirsiz kurulum fail-closed reddedilir;
  tek direct executable secilir.
- Bilinmeyen/iki paketli shim, eksik direct binary veya Windows'ta executable
  olmayan sonuc reddedilir. Diger eski kuruluma sessiz fallback yapilmaz.
- `opencode` ve `opencode2` npm alias'lari desteklenir.

17 Ekim 2026 host incelemesinde kurulu direct paket `@opencode/cli` 2.0.30 idi;
`opencode.exe --version` salt okunur okumasi `opencode v2.0.30` dondu.
Shim PATH'te gorunmedigi icin yeni global npm fallback'i bu kurulumu bulur.
Doctor/CLI executable gorunumu de ayni resolver'i kullanir; PATH ve config
disinda ayrik, daha gevsek bir arama yapilmaz.
16 Ekim 2026 kaynak incelemesinde kurulu paket `@opencode/cli` 2.0.29 idi.
`run --help` salt okunur ciktisi `run --format json`, `--standalone`,
`--model`, `--agent` arayuzunu gosterdi. Bu okuma model cagrisi degildir.
Resolver bir surum/capability veya tool-denial guvenlik kaniti uretmez.

Ayni direct binary ile `run --help` exit code 0 verdi; 1684 byte cikti digest'i
`sha256:f7d0aa65186bbf8ef32fc4ffd168d1acaa8cbb6192fdc16842053f6b01872b42`.
Salt okunur yardimda `--standalone`, `--model`, `--agent` ve `--format` (default,
json) yuzeyleri goruldu. Bu komut provider/model cagrisi yapmadi; native event,
tool-denial, credential izolasyonu veya gercek child kabulunun yerine gecmez.

## 2.0.29 -> 2.0.30 uyumluluk karari

17 Ekim 2026 resmi GitHub tag kaynaklari salt okunur olarak karsilastirildi.
`packages/cli/src/run/{index,noninteractive,run,ui,v1}.ts` dosyalarinin her biri
2.0.29 ve 2.0.30 tag'lerinde ayni SHA-256 byte kimligine sahipti. Ayrica
`packages/core/src/permission.ts` ve `packages/core/src/session/session.ts`
ayniydi. `packages/core/src/config.ts` farki yalniz adapter tarafindan
kullanilmayan opsiyonel `claude` ayarinin eklenmesiydi. Kaynaklar:

- https://github.com/anomalyco/opencode/tree/v2.0.29/packages/cli/src/run
- https://github.com/anomalyco/opencode/tree/v2.0.30/packages/cli/src/run
- https://github.com/anomalyco/opencode/blob/v2.0.29/packages/core/src/config.ts
- https://github.com/anomalyco/opencode/blob/v2.0.30/packages/core/src/config.ts

Bu dar protokol kanitina dayanarak `OpenCodeBatchAdapter` exact allow-list'e
2.0.30'u ekledi ve `opencode vX` binary cikti bicimini kabul ediyor; bilinmeyen
surumler hala provider baslatmadan fail-closed kaliyor. Bu degisiklik native
tool-denial, credential izolasyonu veya gercek provider/model kabul kaniti
degildir; bunlar ayri canli kabul kapisi olarak acik tutulur.

17 Ekim 2026'da ayni 2.0.30 binary'si `127.0.0.1` loopback sentetik
OpenAI-compatible endpoint ile adapter uzerinden calistirildi. Gercek
`run --format json` ciktisinin salt `text` olayli oldugu goruldu; adapter bu
akisi stable session/message/part kimligiyle ve process exit 0 siniriyla kabul
edecek fallback'i tasir. CAS artifact ve execution binding yazimi basarili
oldu. Bu smoke provider kalitesini, tool-denial'i veya credential provisioning'i
dogrulamaz; onlar ayri kabul kapilaridir.

## Acik native adapter baglantisi

Genel `SubprocessClientAdapter` icin kullanilan assignment/digest flag'leri
gercek OpenCode `run` protokolu degildir. Test batch composition artik
`OpenCodeBatchAdapter` ile CAS instruction/context, bounded stdin ve native
JSON event protokolune baglidir; `docs/OPENCODE_NATIVE_BATCH.md`.
Generic wrapper adapter'i gercek native batch kabulu sayilmaz.

V2 config'te `permissions` ve `policies` vardir. Built-in `plan` agent'i tum
shell/tool effect'lerini yasaklamaz. Noninteractive permission reject de default
allow tool'lari engellemez. `--auto` kullanilmaz. V1 ortam degiskeninin V2'de
ayni semantigi oldugu varsayilmaz; private server/config yolu kaynakla baglanmalidir.

Birincil kaynaklar:

- https://github.com/anomalyco/opencode/tree/v2/packages/cli/src/run
- https://github.com/anomalyco/opencode/blob/v2/packages/core/src/config.ts
- https://opencode.ai/v2/docs/permissions/
- https://opencode.ai/v2/docs/config/

Resolver regresyon kodu eklendi; testler kullanici talimatiyla CALISTIRILMADI.
Canli provider/model, Maven veya kullanici ayari degisikligi yapilmadi.
