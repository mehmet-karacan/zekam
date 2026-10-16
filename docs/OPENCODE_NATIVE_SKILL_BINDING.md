# Native skill yuklemesini yonetilen pakete baglama

WP-10.03 icin generated OpenCode plugin v5, gercek V2 `skill` tool sonucunu
salt-okunur `skill native-binding --observation-stdin` resolver'ina baglar.
Modelin skill kullandigini soylemesi gozlem olarak alinmaz. Native SDK'nin
`skill.list()` kaydi, `input.id`, tool event `id` ve `messageID` alanlari kullanilir.

Resmi kaynaklar:

- [Native skill tool](https://github.com/anomalyco/opencode/blob/v2/packages/core/src/tool/plugin/skill.ts)
- [Skill renderer](https://github.com/anomalyco/opencode/blob/v2/packages/core/src/skill.ts)
- [V2 tool hook sozlesmesi](https://github.com/anomalyco/opencode/blob/v2/packages/plugin/src/promise/tool.ts)
- [V2 skill SDK](https://github.com/anomalyco/opencode/blob/v2/packages/plugin/src/promise/skill.ts)

Plugin yalniz completed skill sonucunda SDK listesindeki tek exact kaydi alir.
Native renderer'in skill adi, body, base directory ve kapanis alanlarini, gercek
tool output/content eslesmesini kontrol eder. Instruction body native renderer'in
trim uygulanmis haliyle hash'lenir; supporting dosyalar yuklendi diye iddia edilmez.
Native call id, onceki V1 `callID` alanina normalize edilir; ayni tool'un cakisan
cagrilarinda pending resource birbirine karismaz.

Resolver yalniz kayitli exact project root ve ayni operational home/store'da okur.
Yonetilen `.agents`/`.claude` hedefi icin sahiplik v2 metadata'si, mevcut integration
policy, tam artifact/semantic digest, SDK effective body hash'i ve aktif revision
zorunludur. Permission-bearing allowed-tools projection'da bulunamaz.
Completed `skill.export-v1` job'u exact source root, policy, package, aktivasyon ve
enabled target icin SQL LIMIT oncesi filtrelenir. Plan hash'i ve gercek terminal
receipt/effect bagini existing lifecycle gate yeniden kontrol eder. Son okumada
ownership, artifact veya aktif pointer degismisse bag reddedilir.

stdin query yalniz skill id, portable path ve instruction hash'i tasir; en fazla
4096 byte okunur. Native SDK listesi bounded; liste bekleme 2 saniye, salt-okunur
CLI bekleme 5 saniyedir. Cozulemeyen bag native skill tool'unu engellemez ve Zekam
invocation sonucu uydurmaz. stdout descriptor'inda provider_calls=0,
invocation_observed=false, quality_verified=false ve grants_authority=false kalir.

Basarili cozumde mevcut lifecycle event'e en fazla 500 karakterlik
`native-skill-load/v1:` task label'i eklenir. Label export job UUID'sini, revision,
package, effective instruction, native call ve message SHA256 hash'lerini tasir.
Body, native result, ham call/message kimligi veya absolute path kaydedilmez.
After resource resume projection'a yansir. Bu descriptor/event Work, claim,
SkillInvocationV2 state zinciri veya verified outcome yerine gecmez.

Exact v4 template hash'i eski yonetilen dosya sahipligi listesinde korunur.
Kaynak incelemesinde skill komut adlarinin tek CommandContract constructor'ina
fazladan positional arguman olarak eklendigi goruldu. Her komut ayri sozlesmeye
ayrildi; report/aggregate/native-binding salt-okunur, recording/apply mutating
olarak korunur. Ayrik sozlesme regresyon kaynagi hazirlandi.
Bu asamada global plugin dosyasi degismedi; kurulum veya model cagrisi yapilmadi.

SQLite managed export receipt ve aktif-pointer port fixture'lari; Bun SDK/call-id,
non-disclosure, native renderer drift ve resolver reject regresyon kaynaklari
hazirlandi. Testler CALISTIRILMADI; tum gelistirmelerden sonraki toplu asamada
calisacak. Gercek host kabulu, per-call canonical invocation producer ve tam
kalite/bagimsizlik/provenance kabul kapilari aciktir.
