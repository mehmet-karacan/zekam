# Native OpenCode usage projection

WP-09/10 kaynak dilimi; test ve gercek host kabulu henuz yapilmadi.

## Kaynak ve sinir

Adapter'in destekledigi exact OpenCode `v2.0.29` kaynaginda
[noninteractive projection](https://github.com/anomalyco/opencode/blob/v2.0.29/packages/cli/src/run/noninteractive.ts)
`session.step.ended` olayinin cost/tokens degerlerini `step_finish.part` icinde
yayinlar. [TokenUsage](https://github.com/anomalyco/opencode/blob/v2.0.29/packages/schema/src/token-usage.ts)
toplami input + output + reasoning + cache.read + cache.write olarak tanimlar;
[Money](https://github.com/anomalyco/opencode/blob/v2.0.29/packages/schema/src/money.ts)
USD sonlu sayidir. Bu linkler schema/protocol kaynagidir; gercek process sonucu,
provider faturasi veya model bagimsizligi kaniti degildir.

`infrastructure/clients/opencode_usage.py` yalniz dogrulanmis native event'ten
optional `native_usage` uretir. Model cevabi hala exact result/body olmalidir;
model bu host alanini ekleyemez. Projection mevcut artifact CAS digest'ine ve
assignment/invocation/terminal receipt zincirine dahildir. Ham transcript,
reasoning, tool output veya dosya yolu usage icinde saklanmaz.

## Bilinmeyen ve olculen

- Eski metrics'siz event/artifact aynen okunur; alan eklenmez, sifir uydurulmaz.
- Her component nonnegative safe integer olmalidir. Bool, kesir, negatif,
  sonlu olmayan ve schema disi token/cache alanlari reddedilir.
- Tek text step veya iki native-skill step'in her biri korunur. Eksik step/
  component toplamda null kalir; tam token toplami yalniz hepsi biliniyorsa var.
- Cost native host tahminidir. Decimal USD ve yukari yuvarlanan micros tutulur;
  missing step cost null'dir. `billing_verified=false`; actual billed cost yoktur.
- `provider_requests=null`: step/agent sayisi hidden retry/request sayisi sayilmaz.
- Total, scope, identity, step order ve false authority alanlari okuyucuda yeniden
  hesaplanir. Session/final message ve native skill tool message hash'leri baglidir.

Tournament report optional `native_measurements` ile historical baseline/candidate
usage'i ve yeni selector usage'ini ayri gosterir; eski maliyet yeni dispatch'e
eklenmez. Actual tokens/actual cost eski alanlari null kalir. Native reported
tokens ve host estimate kendi acik scope'lariyla bu yeni alandadir. Selector
dispatch yoksa null usage, `new_selector_dispatched=false` ile ayrilir.

Bu olcum token/cost hard-cap veya tum session/account maliyeti degildir. Title,
compaction, failed/timeout process ve provider billing kapsami dogrulanmamistir.
Unmetered authorization ve gercek native/quality kabul kapilari devam eder.

## Regresyon kaynaklari

Legacy, partial, zero, iki step coverage, conservative decimal rounding, invalid
numbers/schema, model injection, tampered total/identity/authority ve tournament
historical/new scope ayrimi icin offline regresyonlar hazirlandi. Testler kullanici
talimatiyla tum gelistirmelerden sonraki final asamaya birakildi; CALISTIRILMADI.
