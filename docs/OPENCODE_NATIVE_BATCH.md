# OpenCode Native Batch

WP-09 explicit batch composition `OpenCodeBatchAdapter` kullanir. Native Maven
measurement bu yoldan bagimsizdir; OpenCode/model/provider gerektirmez.

## Cagri siniri

1. Canonical assignment/invocation once kaydedilir; adapter permit'i dogrular.
2. Instruction/context CAS metadata boyutu ve gercek byte/digest'i tekrar
   dogrulanir. Secret degerli prompt provider'a gonderilmez.
3. Exact builder/verifier provider/model ve remote/network onayi gerekir. Default provider
   secilmez. Kisitli profilde custom global provider tanimlari alinmaz; local
   provider profili baglantisi halen aciktir. Local istegi remote cagrisiymis
   gibi calistirarak bu bosluk gizlenmez.
4. Direct executable `--version` probe'u yalniz 2.0.29 protokolunu kabul eder.
   Baska surum provider cagrisi baslatmadan gorunur failure olur. Probe ve
   prompt hazirligi assignment timeout butcesinden duser.
5. `run --standalone --format json --agent zekam-test-batch --model <exact>`
   bounded stdin kullanir. Prompt komut satirina eklenmez. `--auto`, session
   continue veya paylasilan background service kullanilmaz.

## Effect oncesi profil

Gecici, source disi config/home ile global agent/plugin overrides gizlenir;
`OPENCODE_CONFIG_PROJECT_DISABLE=1` proje config discovery'sini kapatir.
`OPENCODE_CONFIG_CONTENT` global ve agent `permissions` icinde tum eylemleri
deny eder. V2 `experimental.policies` tool.use/integration.use icin deny tasir.
Snapshots kapatilir. Native credential store OpenCode'un sorumlulugundadir;
Zekam auth dosyasini okumaz, kopyalamaz veya artifact'e almaz.

Bu config OS-level sandbox kaniti degildir. Gercek binary'de tool-denial,
standalone child cleanup ve provider davranisi final kabulde dogrulanacaktir.
Protokol degisikliginde version pini ve capability kaniti birlikte yenilenir.

## Sonuc

- stdout/stderr runtime'da bounded okunur. Timeout, truncation veya nonzero
  exit JSON'daki success iddiasiyla asilamaz.
- Strict UTF-8/JSON, duplicate-key ve nonfinite reddi uygulanir.
- Tek native root session/assistant message/step ve completed `stop` gerekir.
  Tool/error/unknown event, farkli session/message, eksik terminal veya birden
  fazla text part sonucu reddeder. Reasoning toplanmaz/persist edilmez.
- Modelin agent_id beyaninin yerine gozlenen native session/message kimligi
  konur. Result/body secret taramasindan sonra CAS'e yazilir; dispatch sonucu
  yalniz artifact_digest tasir. Patch uygulama Zekam test-only kapisindadir.

## Acik kabul ve gelistirme

`test plan` ve `test run` iki explicit model (`--model`, `--verifier-model`) ve
ortak `--max-remote-calls` limitini ayni plan digest'ine baglar. Verifier secimi
canonical job input digest'ine de dahildir. Native adapter model/profile/executable
execution binding'ini context ve assignment/invocation kimligine baglar; model
degisince eski canonical assignment/result kimligi yeniden kullanilmaz.
Planner/builder bir adapter kullanir;
verifier, defect-triage ve final-verifier diger model adapter'ini kullanir.
Her dispatch yeni native session acar. Ayni modelin variant veya provider
etiketini degistirmek bagimsiz verifier kabul edilmez. Bu kontrol gercek model
alias'larinin/catalog'un ve calisma bagimsizliginin kabul kaniti yerine gecmez.
Plan `independent_verification_proven=false` tasir.

Ortak limit rol farki olmadan mevcut loop sayacinda uygulanir; verifier cagrisi
ayri sinirsiz butce kullanmaz. Bu sayac provider HTTP request/token/cost olcumu
degildir. Native olcum plani modellerden ve bu limitten bagimsiz kalir.

Regresyon fixture kaynaklari yazildi, CALISTIRILMADI. Canli model/provider ve
Maven calistirilmadi. Bu asama WP-09 kapanisi degildir. Custom/local provider
profili ve gercek native tool-denial/end-to-end/bagimsiz verifier kabul kaniti
halen gereklidir.

Birincil kaynak: https://github.com/anomalyco/opencode/tree/v2/packages/cli/src/run
ve ayni repodaki packages/cli/src/server-process.ts, packages/schema/src/config,
packages/core/src/config.ts. V2 policy alani experimental.policies'dir;
ust seviye policies alani varsayilmamistir.
