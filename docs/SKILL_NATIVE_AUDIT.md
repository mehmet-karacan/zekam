# Skill native verifier ve reviewer

WP-10.03 icin terminal paired-trial evidence ustunde gercek native audit yurutme
yolu eklendi. Bu belge canli model, bagimsiz kalite veya test kabul kaniti degildir.

## CLI

`skill audit-run <revision> --trial <terminal-job> ... --case-file <vaka> ...
--model <provider/model> --role verifier --project <proje>` once provider-free
plan verir. Reviewer icin `--role reviewer --prior-verifier <terminal-audit-job>`
ve builder/verifier'dan farkli model gerekir. `--provider-profile` mevcut bounded
custom/loopback profilidir; binary `--opencode` ile cozulur, dry-run'da calistirilmaz.

Apply `--uygula --plan-digest` ve exact canonical `--work-run`, `--work-revision`,
optional `--work-step`, `--work-binding-digest`, assignment metadata'si icin
`--realm-id`/`--coordinator-assignment-id` ister. Assistant mevcut canonical
ciktidan bu kimlikleri kendisi cozer; kullanicidan JSON/UUID yazmasi beklenmez.
Remote disclosure ayrica exact plan icin `--authorize-remote` ile sinirlanir.
Her audit en fazla bir agent dispatch, 10..600 saniye ve deny-all tools profilidir.

`skill audit-report <terminal-job> --project <proje>` salt okunur rapordur.
Mevcut terminal job binding/checkpoint/claim/receipt/CAS, assignment/invocation/
result kayitlari, frozen instruction/context ve native decision artifact'i tekrar
acilir. Kullanici/model report JSON'u kanit yerine gecmez.

## Gercek yurutme ve model ayrimi

Builder trial'lari aggregate yolundan yeniden gradelenir. Ayni project/revision/
package/root/model/harness/budget kosullari ve artifact butunlugu gerekir.
Current provider profiliyle builder execution binding'i yeniden hesaplanir;
eski trial'dan farkli endpoint/model/config sessizce kullanilamaz. Verifier ve
reviewer kendi adapter execution binding'lerine sahiptir. Ayni modelin provider
etiketi veya variant'ini degistirmek kabul edilmez; custom profile'da ayni upstream
modeli gosteren alias da reddedilir. Reviewer mevcut verifier'in config/model
bagini da yeniden dogrular.

`skill.audit.verify` ve `skill.audit.review` reserved canonical islerdir. Work/Run
authority, lease heartbeat, claim-before-effect, terminal checkpoint/CAS/receipt
mevcut runtime'dan gelir. Child assignment ve invocation native cagri oncesi
kalici CAS'a yazilir; role modelin beyanindan alinmaz. Programatik iptal native
version/run process'lerine iletilir. Baslamis belirsiz is yeniden dispatch edilmez.

Builder'larin veya onceki verifier'in native session'i yeniden kullanilamaz;
message ID degistirmek yeni session sayilmaz. Process/protocol hatasi, gecersiz
decision ve ret sonucu basariya donusturulmez. Reviewer `accepted=true` donse
bile prior verifier audit'i reddedilmisse audit_integrity_accepted=false kalir.

## Disclosure ve karar siniri

Audit context'i en fazla 180000 byte'tir; asim model cagrisindan once reddedilir.
Exact context ve frozen instruction digest'i planin parcasidir. Context gercek
skill instructions, vaka input/oracle, native output'lar, artifact/native kimlik,
olculmus exact-match ve yuvarlanmis wall-time milisaniye tasir. Raw transcript,
reasoning, provider hata metni veya credential aktarilmaz. Dis veri yeniden
secret/PII kapisindan gecer. Reviewer ayni aggregate'in canonical verifier
receipt'i ve bounded decision sonucunu gorur.

Native cevap yalniz `result` ve `body.decision` icindir. Decision exact bool
accepted, exact aggregate_digest, artifact/process/preference/efficiency/safety
icin pass/fail/unavailable ve bounded allowlist reason_codes tasir. Serbest
metin onay sozlesmesi yoktur. Gecersiz cevap decision_valid=false olarak kalir.

audit_integrity_accepted icin model kabulunun yaninda artifact/process/safety
pass olmali; toplanmamis preference kaniti unavailable kalmalidir. Modelin
preference=pass iddiasi eksik kaniti tamamlamaz ve integrity kabulunu engeller.
Bu flag yalniz bounded audit tutarliligidir. Holdout caller-supplied provenance,
human preference, tool-denial gercek host kabulu ve upstream model alias
bagimsizligi ayrica kanitlanmalidir. Distinct requested models bunlarin yerine
gecmez: independence_proven=false, quality_evaluation_complete=false ve
activatable=false korunur. Audit receipt'i legacy evaluator/verifier/review
receipt operation'ina yeniden etiketlenmez. SkillEvaluationV2 producer ve gercek
kalite/native invocation kabul kapilari halen aciktir.

Local exact evaluator icin ayri canonical aggregate receipt'i
`skill aggregate-record/report` ile uretilir; ayrintilar
`docs/SKILL_MEASURED_AGGREGATE.md` icindedir. Native verifier receipt'i bu yeni
evaluator receipt'inin yerine gecmez.

Regresyon kaynaklari canonical trial -> verifier -> reviewer -> report zincirini,
same-model/variant reddini, remote authorization, prior ret sonucu, unsupported
preference, invalid bool, reused session, artifact kaybi ve generic worker ayrimini
kapsar. Testler CALISTIRILMADI; tum gelistirmelerden sonraki final asamayi bekler.
