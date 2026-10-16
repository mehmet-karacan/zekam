# Kontrollu native skill tool execution profili

WP-10.03 outcome kaynagi icin mevcut text-only OpenCode batch adapter'ina
ayri, explicit native_skill paketi olan profil eklendi. Text-only varsayilan
profil ve onun tek-step/tool-deny protokolu korunur. Bu asama altyapi kodudur;
canonical single-case execution CLI/producer ve outcome verifier baglantisi
siradaki acik gelistirmedir.

Resmi V2 kaynaklari:

- [Private config skill sources](https://github.com/anomalyco/opencode/blob/v2/packages/core/src/config/plugin/skill.ts)
- [Skill output renderer](https://github.com/anomalyco/opencode/blob/v2/packages/core/src/skill.ts)
- [Native CLI JSON tool ve step event'leri](https://github.com/anomalyco/opencode/blob/v2/packages/cli/src/run/noninteractive.ts)
- [Permission last-match davranisi](https://github.com/anomalyco/opencode/blob/v2/packages/core/src/permission.ts)
- [Policy resource action:resource semantigi](https://github.com/anomalyco/opencode/blob/v2/packages/core/src/config/plugin/policy.ts)

## Effect oncesi profil

Native profil yalniz bir canonical SkillPackage kabul eder. Constructor paketi
files'tan tekrar parse ederek metadata/body/projection drift'ini reddeder;
canonical immutable snapshot kullanir. Private runtime config/skills dizinine
safe projection dosyalari yazilir; allowed-tools aktarilmaz. Bu bir proje klonu
veya kaynak checkout'u degildir, kaynak kokune dosya yazilmaz.

Native renderer base directory'yi modele gosterdiginden runtime parent dizini
explicit, gercek, linksiz ve source root disinda olmalidir; kullanici home'u veya
users/home dizini iceren yol kabul edilmez. Runtime root identity digest'i
execution binding'e dahildir. Gecici child private ACL/mode ile korunur ve
owned temp cleanup ile kaldirilir. Global client ayari degismez.

Config permissions once her seyi deny eder, sonra yalniz skill/<exact-id>
action/resource'unu allow eder. Experimental policy'de yalniz
tool.use + skill:<exact-id> ve integration.use + skill:<exact-id> allow edilir.
Diger skill, shell, read/write, MCP ve entegrasyonlar kapali kalir. Policy'nin
resource'u tool adi degil action:resource bicimindedir. Agent iki step ile
sinirlidir: bir skill tool cagrisi, sonra final structured answer. Remote
provider secimi mevcut explicit network authorization kapisini korur.

## Sonuc protokolu

Parser bir native session, iki farkli assistant message, ilk step'te exact
id ile bir completed skill tool_use, tool-calls finish ve ikinci step'te
stop/final JSON ister. Native tool output'u canonical paket body, base directory
ve native on-file sample renderer'iyle tam eslesir; prefix yeterli sayilmaz.
Eksik/duplicate/farkli tool, failed/unknown event, message/session drift,
incomplete/late event ve renderer farki basarili dispatch sayilmaz.

Bir milyon byte output ve 4096 event siniri korunur. Reasoning, pre-tool text,
tool metadata ve rendered absolute paths sonuc artifact'ine konulmaz. Final
JSON icin mevcut result/body ve secret-free kontrolu kullanilir. Modelin
agent_id beyanini actual native final session/message identity'si degistirir.
Ek native_skill_invocation projection'i package/semantic/instruction/render ve
actual native session/message/call hash'lerini tasir. skill_effect_verified=false
kalir; tool completion tek basina artifact/task dogrulugu degildir.

Provider/model/config/package/runtime-root bagi adapter execution digest'ine
dahildir. Default adapter'in binding body/protokolu degismez; native mode
explicit olarak secilmedikce tool yetkisi verilmez. Version/timeout/cancel/
output limit ve CAS-only dispatch sonucu onceki process boundary'yi kullanir.

## Kanit durumu

Offline regresyon kaynaklari literal native renderer/event fixture'i,
missing/extra/foreign/failed tool, native final identity, content non-disclosure,
exact policy resources, private package staging/cleanup ve constructor metadata
drift'ini kapsar. Fixture public-neutral runtime policy port double'i privacy
veya gercek host kabul kaniti sayilmaz. Testler CALISTIRILMADI; son toplu asamayi
bekler. Gercek host tool-denial ve canonical execute/verifier/outcome kabulu acik.
