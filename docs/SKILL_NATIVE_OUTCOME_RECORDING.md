# Native verifier receipt -> immutable outcome

WP-10.03 icin outcome-record ve outcome-report eklendi. Kaynak actual
skill.outcome.native-verify terminal job/receipt'idir; caller review JSON'u,
modelin basari beyanı veya recording receipt'i verifier execution yerine gecmez.
Legacy skill.outcome.verify-v2 receipt kapisi korunur; native operation legacy
operation'a yeniden adlandirilmaz.

## Plan ve write

Plan verifier report'unu source execution/invocation, task/oracle/artifact,
model/config/session ve native verifier karar zinciriyle tekrar okur. Original
invocation digest'i, verifier job/receipt ve source Work binding'leri pin edilir.
Outcome zamani actual verifier terminal receipt created_at degerinden gelir.
Yeni recording model cagrisi yapmaz.

Unverified verifier sonucu journal'da kalir. Preview recordable=false ve bounded
blocker dondurur; bu kanit immutable outcome tablosunun tekil invocation satirini
doldurmaz. Boylece eksik kanit sonraki gercek dogrulama kaydini engellemez.

Recordable verified-success/failure sadece one-native-task-artifact kapsaminda
skill_outcome_v2'ye gider. Grader artifact'tir; evidence_digest actual verifier
terminal evidence digest'idir. Recording sonucu kendi ayri canonical CAS ve
terminal receipt'ini tasir. Quality evaluation complete ve activatable false
kalir; bu kayit genel skill terfisi veya Work kapanisi degildir.

Recording kendi reserved skill.outcome.native-record job/lease/claim'i altinda
calisir. Kisa operational writer fence exact payload/effect, live running claim,
matching fencing tokens, dolmamis lease ve eksik terminal receipt kosullarini
kontrol eder. Ortak fence invocation recording'inin iki exact operasyonu icin
de kullanilir; whitelist disindaki operation'a recording authority vermez.

Learning transaction'inda invocation row hash'i ve original Work/run yeniden
kontrol edilir. Outcome tekil ve immutable'dir: ayni exact body readback ile
idempotent kalir; farkli verifier/karar eski satiri degistiremez. Yeni recording
Work'u original execution ile ayni project/Work'e bagli olmalidir; source Work
kimligi kayit job'unun kimligiyle degistirilmez.

## Replay ve report

Completed replay actual verifier source'u ve existing outcome row'unu yeniden
okur; kayip row'u yeniden yaratmaz. Report recording claim/checkpoint/receipt/
CAS, actual verifier source zinciri ve outcome body/hash/status bagini dogrular.
Missing proof, foreign scope veya conflicting immutable karar fail-closed kalir.
Source hash'leri ve terminal receipt'ler model beyanindan turetilmez.

## Kanit durumu

Offline SQLite/CAS/native fixtures tekil bounded success/failure recording,
unverified journal'in outcome satirini doldurmamasi, legacy operation reddi,
missing own claim, conflicting verifier, lost-row replay ve foreign project
icin hazirlandi. Fixture transport/activation doubles bagimsiz host/provider
kabul kaniti sayilmaz. Testler CALISTIRILMADI; tum gelistirmelerden sonraki tek
toplu asamayi bekler.

Native execution -> invocation -> ayri verifier -> outcome kaynak baglari
eklendi. Gercek host/tool-denial, tam skill quality/preference/provenance,
bagimsiz review ve WP-02..12 genel kabul kapilari halen aciktir. Bu belge genel
hedefi veya WP-10.03'u kapatmaz.
