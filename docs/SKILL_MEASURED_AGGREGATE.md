# Canonical local evaluator kaydi

`skill trial-aggregate` salt okunur grader'dir. Lifecycle'a baglanacak evaluator
icin kendi terminal claim/receipt'i gerekir; trial veya native verifier receipt'i
evaluator receipt'i diye yeniden etiketlenmez.

`skill aggregate-record <revision> --trial <terminal-trial-ref> ...
--case-file <vaka> ... --project <proje>` provider-free exact plan verir. Apply
`--uygula --plan-digest` ve canonical `--work-run`, `--work-revision`, optional
`--work-step`, dry-run `--work-binding-digest` ister. Assistant bu referanslari
canonical ciktidan kendisi cozer; kullaniciya JSON/UUID yazdirilmaz.

`skill.evaluation.aggregate` reserved canonical operation'indir. Generic worker
bu isi alamaz. Work/config/plan admission, lease heartbeat, claim-before-effect,
CAS result ve terminal checkpoint/receipt mevcut runtime'dan gelir. Native child
ve model/provider cagrisina ihtiyac yoktur. Case dosyasi veya candidate scope/body
drift'i claim/publication sinirlarinda reddedilir. Baslamis belirsiz is otomatik
tekrarlanmaz. Ayni completed tuple yeni trial veya kalite olcumu sayilmaz.

Plan exact aggregate digest, scope/revision/package, sirali source job/terminal
evidence digest'leri, case manifest ve bounded output butcesi tasir. En fazla
100 source job ve 100 vaka vardir; her vaka 24 KiB ile sinirlidir. Source trial,
assignment/invocation/result, native artifact ve oracle regrade kapilari mevcut
aggregate okuyucusundan gelir. Kayit oncesinde ve claimed evaluator icinde source
zinciri yeniden okunur; degisiklik farkli sonuca sessizce donusturulmez.

Sonuc CAS'ta local-private artifact olarak en fazla 4 MiB'dir. Exact bounded,
secret/PII kontrolunden gecmis authored case/oracle snapshot'lari sonuca dahil
edilir; kaynak repo'ya yeni runtime dosyasi yazilmaz. Ham transcript, reasoning
ve credential eklenmez. Vaka dosyasinin kaybolmasi/model degisimi retained case'i
yok etmez; claimed result yeni modelin ayni grader'i yeniden uygulamasina yeter.
Caller-supplied vaka etiketi bagimsiz provenance'e yukseltilmez.

`skill aggregate-report <terminal-evaluator-ref> --project <proje>` canonical
Work binding, completion checkpoint/artifact/receipt ve result boyut/hash'ini
dogrular. Retained case'leri yeniden bounded public-safe kapisindan gecirir ve
source trial zincirini yeniden gradeler. Rebuilt plan/result exact stored result
ile eslesmezse rapor basarili sayilmaz. Kayip native artifact veya source receipt,
terminal evaluator ozeti var diye gizlenmez.

Result'taki aggregate_digest grader govdesini; receipt_evidence_digest ise
terminal canonical CAS sonucunu tanimlar. Bu iki hash farkli sozlesmelerdir ve
birbirinin yerine kullanilmaz. Canonical job completed, evaluator isinin gercekten
calistigini anlatir; aggregate.comparison_state failed/regressed/insufficient
olabilir. Bu sonuclar denominator'dan atilmaz ve activation vermez.

Bu dilimde SkillEvaluationV2 ledger projection'i yapilmaz. Gercek evaluator,
native verifier/reviewer receipt'leri ve tam kalite/provenance/independence
kapilarini birlestiren lifecycle producer siradaki baglantidir. Kalite kaniti
tamamlanana kadar quality_evaluation_complete=false ve activatable=false kalir.

Regresyon kaynaklari gercek canonical SQLite/CAS evaluator claim/checkpoint/
receipt, retained case readback, replay, source artifact kaybi, oracle drift,
failed sample korunmasi, CLI preview/wrong digest ve generic worker ayrimini
kapsar. Testler CALISTIRILMADI; final toplu asamayi bekler. Canli model/provider,
kullanici DB migration'i veya global ayar degisikligi yapilmadi.
