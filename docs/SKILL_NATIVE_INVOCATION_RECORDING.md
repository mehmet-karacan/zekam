# Native instruction tool invocation kaydi

WP-10.03 kaynak uygulamasinda `skill invocation-record <event-id>` ve
`skill invocation-report <job-ref>` eklendi. Normalde coding ajan kayit
kimliklerini mevcut canonical ciktidan cozer; kullanicinin UUID veya digest
hazirlamasi gerekmez. Bu yuzey yeni model cagrisi baslatmaz.

## Kaynak ve kapsam

Girdi modelin "skill kullandim" metni degildir. Mevcut digest zincirli yerel
lifecycle ledger'indaki exact retained `tool.execute.after` olayi okunur.
Tool skill, status completed ve native-skill-load/v1 etiketi zorunludur.
Etiketin export job, revision, package ve effective instruction hash'leri
salt-okunur managed resolver tarafindan tekrar dogrulanir. Scope, kaynak kok,
aktif pointer, policy, tam dosya agaci ve export terminal receipt kapilari korunur.
Okuma malformed baska event dosyasini quarantine'e tasimaz.

Kaydin anlami **native instruction tool completion**'dir. Native SDK'nin
skill secimi ve instruction teslimi, o tek cagriya ait discovered/selected/
loaded/invoked/completed gozlem zinciri ve son unverified durumuyla kaydedilir.
Bunlar alti bagimsiz deney veya alti model cagrisi degildir; ayni terminal host
gozleminin durum zinciridir. Her satir original event zamaninin lifecycle
ledger'indaki saniye hassasiyetini kullanir; uydurma sure olcumu uretilmez.

Host metadata'si yerel ve **unattested**'dir. Skill'in sonucunun dogru oldugu,
referans/script dosyalarinin calistigi veya host'un bir Zekam lease'i altinda
calistigi iddia edilmez. host_attested=false ve skill_effect_verified=false
kalir; skill_outcome_v2 tablosuna sonuc yazilmaz.

## Work ve transaction

Kayit, operatorun exact plan/Work run/revision/source snapshot bagiyla verdigi
explicit recording association'dir. Eski export claim'i veya native event
kendi basina kayit yetkisi vermez. `skill.invocation.native-record` reserved
operation'i kendi canonical job/lease/claim/checkpoint/CAS/terminal receipt'ini
kullanir; generic worker bu isi alamaz.

Kaynak plan write oncesinde yeniden derlenir. Kisa operational writer fence
running job, payload/effect, eslesen fencing counter, dolmamis lease ve eksik
terminal receipt kosullarini kontrol eder. Aktif scope/revision tekrar okunur.
Alti invocation satiri tek learning transaction'inda yazilir; ucuncu adimda
kesilirse hicbir satir commit edilmez. Satirlar commit edildikten sonra terminal
receipt kaybolursa recovery gereklidir; eksik kanit basariya cevrilmez.

Agent ref bu adapter'da native session+call hash kimligidir; bir model veya
bagimsiz verifier kimligi degildir. Ayni session'da farkli call'lar cakismaz.
Ayni native call'un duplicate delivery'si baska event/Work run ile yeniden
kaydedilemez. Completed exact replay yalniz terminal CAS ve her immutable
invocation row'un hash/predecessor/work/run/harness bagini okur; satir uretmez.

Report tarihsel recording kanitini okur. Daha sonra deprecation veya policy
degisikligi eski gozlemi silmez ve onu bugun aktif skill diye raporlamaz.
Yeni ingestion ise guncel aktif managed source zorunlulugunu korur.

## Gelistirme ve acik kabul

Mevcut tekil record_invocation_state davranisi korunarak transaction ic yardimcisi
ayrildi. Gercek SQLite/Work/CAS/claim/receipt ve event zinciri fixture'lari;
atomic rollback, duplicate delivery, ayni session'da farkli call, historical read,
foreign scope ve unbound/failed/model-beyan reddi regresyon kaynaklari eklendi.
Fixture active-effect port double'i host/activation kabul kaniti sayilmaz.

Testler CALISTIRILMADI; tum gelistirmeler bittikten sonraki tek toplu asamayi
bekler. Gercek host kabul, bagimsiz outcome verification, passing skill kalite/
preference/provenance ve diger WP-02..12 kabul kapilari aciktir. Bu kaynak
dilimi tum WP-10.03'u veya genel hedefi kapatmaz.
