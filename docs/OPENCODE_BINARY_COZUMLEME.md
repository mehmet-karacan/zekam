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
16 Ekim 2026 kaynak incelemesinde kurulu paket `@opencode/cli` 2.0.29 idi.
`run --help` salt okunur ciktisi `run --format json`, `--standalone`,
`--model`, `--agent` arayuzunu gosterdi. Bu okuma model cagrisi degildir.
Resolver bir surum/capability veya tool-denial guvenlik kaniti uretmez.

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
