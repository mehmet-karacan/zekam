# Test agent execution kaniti

Canonical unit-test gateway artik basarili assignment-first dispatch'in exact
assignment/invocation/DispatchResult, parsed envelope, actual result artifact'i,
instruction ve context baglarini `zekam-unit-test-agent-execution/v1` CAS
nesnesinde tutar. Response `execution_evidence_digest` referansi tasir.
Uretim composition'i execution/artifact/context/instruction dependency'lerini
mevcut operational artifact registry'ye kaydeder; ayri DB veya migration yoktur.

Builder response referansi kabul edilen attempt'e, verifier response referansi
ayni attempt evidence'a baglanir. Loop state kabul edilmis builder execution
referanslarini korur; final terminal evidence bu listeyi, completeness alanini
ve final verifier execution referansini tasir. Yalniz son builder'a bakip onceki
kabul edilmis model calismalari kaybedilmez. Legacy accepted state'te referanslar
yoksa `builder_provenance_complete=false` kalir; eski sohbetten uydurulmaz.

## Salt okunur kontroller

`read_agent_execution` sadece exact digest'leri okur; tum CAS veya baska
projelerin kayitlarini taramaz. Nesne basina 1 MB, instruction icin 16 KiB limit;
strict JSON, digest/size, UUID, request'ten turetilmis deterministic assignment/
invocation, frozen instruction, dispatch role/result/artifact ve context
execution baglari dogrulanir. Ham native reasoning/transcript saklanmaz.
Execution projection'i secret/PII kontrolunden gecer; result body mevcut
bounded structured artifact'e referanstir.

Gercek `OpenCodeBatchAdapter` protocol yolu ile native event session/message
kimligi tasiyan sonuc, replay/mock veya yalniz modelin verdigi agent adindan
ayri raporlanir. Configured model/upstream ve profile digest host adapter'inden
alinir; model govdesinden veya model adina bakarak tahmin edilmez. Private
endpoint veya key bu yeni projection'a konmaz. Provider'in gercekte sectigi
upstream model dogrudan olculmedi: `native_model_identity_observed=false`.

`compare_execution_provenance` final-verifier'in exact candidate/measurement
context'ini ve tum builder'larin project/Work/realm/request kapsamlarini
karsilastirir. En fazla 512 unique builder referansi okunur. Distinct native
assignment/invocation/session ve reviewed profile'daki configured upstream
farki ayri siniflandirilir. Alias farki tek basina model farki degildir; ayni
upstream case-insensitive olarak ayni sayilir. Eksik profile/native kayit
`unproven` kalir.

## Yetki siniri

Bu CAS baglari tek basina terminal receipt veya bagimsiz kalite kabul authority'si
degildir. Comparator `canonical_terminal_verified=false` ve
`independent_quality_verified=false` dondurur. Gercek completed canonical
`test.run`/terminal/attempt baglari, accepted candidate ile publication subject
eslesmesi ve trusted verifier producer imzasi siradaki entegrasyon katmanidir.
Host key confinement ve native worker kabul kapilari da aciktir.

## Gelistirme kaniti

Replay fixture'i native execution'a yukseltmeme, terminal ref propagation,
legacy hydration, missing artifact, rehashed request/dispatch/context drift,
acceptance subject, upstream alias ve registry dependency regresyon kaynaklari
eklendi. Gercek production model/provider cagrisi yapilmadi; sentetik loopback
native kabul ve deny-all tool smoke'u ayri kanit olarak kaydedildi.

## Final yerel test kaniti

17 Ekim 2026'da kaynak revizyonu `3fb420b4e64d3e2be709848ffe6650f1f3f96a78`
sonrasinda, OpenCode text-only parser ve deny-all smoke degisiklikleri dahil
edilerek tek toplu pytest kosusu exit code 0 ile tamamlandi:
`7989 passed, 1409 skipped, 3 warnings` (54:12). Bu kosu yerel regresyon
kapisini kapatir; gercek production model/provider kalitesi, signer/credential,
bagimsiz kalite, restore/rollback ve release zincirinin tum native kabullarini
kanitlamaz. Kaynak degisikligi olmadikca pytest yeniden kosulmayacaktir.
