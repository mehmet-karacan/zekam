# Native kanittan lifecycle kaydi

`skill evaluation-from-audits --evaluator <terminal-aggregate-job>
--verifier <terminal-verifier-job> --reviewer <terminal-reviewer-job>
--project <proje>` mevcut canonical kanitlardan provider-free plan uretir.
Assistant kimlikleri canonical ciktidan kendisi cozer; kullaniciya teknik JSON
veya UUID yazdirilmaz. Apply exact `--uygula --plan-digest` ve canonical
`--work-run`, `--work-revision`, optional `--work-step`, dry-run
`--work-binding-digest` ister.

Uc kaynak farkli terminal job olmalidir. Evaluator retained case ve source
receipt/native artifact zinciriyle yeniden gradelenir. Verifier/reviewer exact
ayni revision/package/aggregate'i incelemeli; reviewer prior verifier job ve
receipt digest'ine bagli olmalidir. Yanlis role/subject, foreign scope,
eksik/corrupt artifact ve farkli verifier'e bagli reviewer reddedilir.

Projection `skill.evaluation.native-project` reserved Work/Run job'udur.
Generic worker alamaz. Lifecycle store yazimdan once exact payload/binding/
effect digest'li tek running claim, mevcut lease ve fencing sayaci, expiry ve
terminal receipt'in henuz yoklugunu kontrol eder. Home/operational/learning
dosya kimligi ayni olmali; candidate gercek registered project ve package ile
eslesmelidir. Claim yokken API cagrisi ledger'a yazamaz.

Tum source/model islemleri bittikten sonra operational DB'de kisa writer fence
alinir. Evaluation ve review existing learning DB'de tek transaction ile yazilir;
review yazimi basarisizsa evaluation da rollback olur. Sonra canonical runtime
CAS result/checkpoint/terminal receipt'i tamamlar. Cross-store disk kesintisi
silinmez veya otomatik basariya donusturulmez; started unknown job recovery ister.

Existing SkillEvaluationV2 evaluator/verifier execution identity alanlari gercek
aggregate/verifier job ID'lerini; evidence digest'leri bu job'larin gercek terminal
CAS receipt digest'lerini tasir. Reviewer de kendi native job/receipt'ini korur.
Original operation'lar legacy skill.evaluation.evaluator-v2/verifier-v2 veya
skill.review.verify-v2 diye yeniden etiketlenmez. Generic legacy import ve passing
activation kontrolleri gevsetilmedi.

Holdout per-case exact-match metrikleri ledger'a gelir. trials/sample_size gercek
holdout pair sayisidir; development ciftleri buna eklenmez. Raw comparison ve
tum pair/unique case sayilari uncertainty icinde ayrica korunur. Failed, regressed,
blocked ve equal bounded gozlemler kaybolmaz. Olcum improved olsa bile tam kalite,
preference, provenance ve upstream independence kaniti yoksa state
insufficient-evidence olur. Bu adapter improved uretmez ve approved review yazmaz;
approved=false, activatable=false, quality_evaluation_complete=false kalir.
Native modelin accepted=true yaniti eksik kanitin yerine gecmez.

Bu sinir, genel passing evaluation/review adapter'ini kaldirmaz. Tam kalite ve
gercek client/native invocation kabul kapilari WP-10.03 icin halen aciktir.
Bounded exact JSON sonucunu tum skill davranisinin kaniti diye sunmak yasaktir.

`skill evaluation-report <terminal-projection-job> --project <proje>` kendi
canonical completion proof'unu, kaynak kanitlari ve iki learning row'unun
hash/exact body bagini tekrar okur. Completed replay yeni row yaratmaz;
kayip ledger kaydi canonical result ozetinden yeniden uretilmez. Ledger row'lari
var diye terminal projection receipt'i olmadan basari raporlanmaz.

Yeni schema veya kullanici DB migration'i yoktur. Legacy evaluation/review
transaction yardimcilari ortaklastirildi ve replay hash readback'i eklendi.
Regresyon kaynaklari scoped gercek SQLite/CAS zinciri, typed ledger projection,
own claim zorunlulugu, review-failure atomic rollback, completed replay,
missing-row reddi, CLI preview/digest ve no-promotion sinirlarini kapsar.
Testler CALISTIRILMADI; tum gelistirmelerden sonraki final toplu asamayi bekler.
