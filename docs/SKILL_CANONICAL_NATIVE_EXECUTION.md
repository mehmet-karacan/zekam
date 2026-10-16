# Canonical native skill execution

WP-10.03 icin `skill execute-native` ve `skill execution-report` kaynaklari
eklendi. Bu yuzey bir aktif skill paketini tek bounded task/case icin gercek
native skill tool'u ile calistirir. Paired instruction-only trial'in baseline
ve esit harness iddialari bu execution'a tasinmaz.

## Plan ve admission

Plan mevcut kayitli project/root, active revision/package, case/oracle digest,
model ve reviewed provider config, native runtime root identity ve adapter
execution binding'ini pin eder. Skill instructions ve file manifest public-safe
kontrolunden gecer. Runtime root explicit ve source/user-home disindadir.
Plan provider cagrisi veya authority uretmez.

Apply exact plan ve canonical Work/run/revision/config/source snapshot bagini
ister. `skill.native.execute` reserved operation'i kendi job/lease/effect claim'i
altinda calisir; generic worker bunu alamaz. Remote disclosure ayrica exact
authorization ister. Package/case/provider-profile/source ve active pointer
dispatch oncesinde ve terminal publication oncesinde yeniden okunur.

Builder context'i yalniz task input ve native skill id tasir; local expected
output/oracle gonderilmez. Assignment once CAS'a kaydedilir. Native adapter
izinli tek skill'i private runtime'dan yukler, tool settlement ve final JSON'u
ayri native message'lardan alir. Gercek elapsed_ns ile actual assignment,
invocation ve dispatch envelope referanslari korunur; provider call sayisi
olculmediyse not-measured kalir.

## Sonuc ve readback

Execution sonucu case/oracle ve bounded source instruction snapshot'ini,
normalized dispatch observation'i ve local exact-match olcumunu CAS'a baglar.
Canonical checkpoint ve terminal receipt olmadan completed execution raporu
uretilmez. Process/tool/protocol failure ve mismatch saklanir; completed job
bu olcumu kalite basarisina ceviremez.

Report canonical claim/receipt/checkpoint/CAS, assignment identity, native
invocation envelope, gercek builder context/instruction ve actual output
artifact'ini tekrar okur. Local grade oracle'dan yeniden hesaplanir. Native
tool projection package/semantic/effective-instruction bagini ve native session
hash'ini korumali; tool ve final assistant message farkli olmalidir. Missing CAS,
foreign scope veya grade/subject drift reddedilir. Completed exact replay yeni
process veya artifact uretmez; tarihsel candidate scope kontrolu korunur.

Ortak dispatch/readback yardimcilari native namespace/context/instruction icin
parametreli hale getirildi. Varsayilan paired trial'in namespace, instruction,
context ve iki-field artifact kontrolleri ayni kalir; native ucuncu projection
yalniz explicit native expected subject ile kabul edilir.

`measured-exact-match` caller-supplied oracle olcumudur. Dataset provenance'i
bagimsiz degildir; result independent_verification=false,
skill_effect_verified=false ve activatable=false kalir. Bu execution kaynagi
bagimsiz verifier'in okuyabilecegi gercek artifact saglar; tek basina outcome
veya passing skill quality uretmez.

## Kanit durumu ve sonraki is

SQLite/Work/CAS/native offline port fixture'lari own live claim, oracle isolation,
exact completed replay, process failure, mismatch, missing artifact, foreign
project ve changed oracle/authority reddini kapsayacak sekilde hazirlandi.
Native transport, active-effect ve neutral-runtime policy port doubles gercek
host/provider/activation kabul kaniti sayilmaz.

Testler CALISTIRILMADI; tum gelistirmelerden sonraki toplu asamayi bekler.
Sirada native execution receipt -> SkillInvocationV2 baglantisi ve actual
farkli verifier execution receipt -> skill outcome baglantisi vardir. Gercek
host tool-denial, passing quality/provenance ve WP-02..12 kabul kapilari aciktir.
