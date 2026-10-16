# Skill Lifecycle CLI

WP-10.03 lifecycle store'unun reviewed activation, rollback ve deprecation
akisina CLI baglantisi eklendi. Bu belge activation/review yetkisi degildir.

`zekam skill lifecycle-plan <revision> --project <proje> --action active`
exact revision scope'unu, mevcut etkili CAS head'i ve passing evaluation /
independent review kaydini salt okunur cozer. Sonuc v2 plan, exact effect ve
plan digest tasir. `--rollback-from <revision>` yalniz mevcut active source'tan
daha once terminal receipt ile active olmus target'a rollback planlar.
`deprecated`, `revoked`, `retired`, `superseded` mevcut active revision icindir.

Plan JSON'u kaynak repo disinda uygun bir dosyaya kaydedilip
`zekam skill lifecycle-apply <plan-file>` ile on izlenebilir. Uygulama icin
`--uygula --plan-digest <exact-plan-digest>` gerekir. Native assistant mevcut
ciktidan kimlik/digest'i kendisi cozer; kullanicidan teknik JSON yazmasi beklenmez.
Dosya bounded regular/link-free okuyucuyla en fazla 64 KiB okunur. Exact schema,
effect, scope, CAS ve digest tipleri dogrulanir; unknown alan kabul edilmez.

Proje scope'u registered source root ve current cwd bagiyla korunur. Apply
`skill.activate-v2` canonical local job/lease/claim/receipt zincirini kullanir;
registry resource kilidi ve lifecycle CAS birlikte uygulanir. Yeni review veya
head yarisi claim'in exact effect'iyle uyusmazsa etkinlestirme yapilmaz; recovery
gerektiren is sessizce yeniden denenmez. Append-only olay terminal receipt
gelene kadar etkin sayilmaz. Ayni planin completed replay'i ayni event/job/receipt'i
verir; daha sonraki head degisiminde eski plan yeni activation'a donusmez.

Plan dosyasi passing evaluation veya bagimsiz review yerine gecmez. Gercek
evaluation/reviewer producer baglantilari olmadan aday active yapilamaz.
Activation sonrasi client projection dagitimi mevcut `skill export` komutundan
ayri exact plan/apply islemi olarak surer; kaynak paketin scope ve ownership
kontrolleri korunur. Bu gelistirmede kullanici DB'si veya client ayari degismedi.

Regresyon kodu `tests/integration/test_skill_lifecycle_v2.py` icinde existing
review/receipt fixture'lariyla plan effect parity, foreign scope, rollback,
CLI preview, yanlis digest reddi, gercek local worker deprecation ve replay
senaryolarina baglandi. Testler CALISTIRILMADI; final toplu asamayi bekler.
Gercek native dagitim/invocation, evaluation producer ve kabul kaniti aciktir.

## Mevcut terminal evaluation ve review kanitini kaydetme

`skill evaluation-record <evidence-file> --project <proje>` ve
`skill review-record <evidence-file> --project <proje>` once kayit plani verir.
`--uygula --plan-digest <exact-digest>` kaydi canonical local job/lease/claim/receipt
ile yapar. Assistant kimlikleri mevcut canonical ciktidan kendisi cozer.
Girdi dosyasi kaynak repo disinda tutulur; UTF-8, regular/link-free, 32 KiB,
duplicate-key/non-finite/secret/PII ve exact field/type sinirlari uygulanir.

Evaluation girdisi `SkillEvaluationV2.body()` sozlesmesidir; review girdisi
`zekam-personal-skill-review-input/v2` schema'si, revision_digest,
evaluation_digest, reviewer_ref, reviewer_execution_identity,
reviewer_evidence_digest, approved, reason ve grants_authority=false alanlaridir.
Preview `execution_evidence_verified=false` tasir; preview dosyasi kalite veya
yurutme kaniti degildir. Apply store'un mevcut exact evaluator/verifier receipt,
distinct operational job ve reviewer independence kontrollerini uygular.
Yeni recording receipt'i bu kaynak execution receipt'lerinin yerine gecmez.
Gecersiz kaynak kaniti claimed kayit isini basarisiz yapabilir; basarili kayit
veya active sonucu uretilmez. Ayni completed plan tekrarinda ledger salt okunur
acilir, body hash/esitlik yeniden okunur; kayip ledger kaydi yeniden yaratılmaz.

Bu baglanti model/harness calistirmaz, deneme sayisi veya metrik uretmez ve skill'i
aktif etmez. Gercek skill evaluation/reviewer producer, model bagimsizligi ve
native invocation kabul kapilari halen aciktir. Sabit evolution fixture harness'i
gercek skill davranis olcumu olarak kullanilmaz. CLI/local worker/replay ve yanlis
receipt-operation regresyon kaynaklari hazir; testler CALISTIRILMADI.
