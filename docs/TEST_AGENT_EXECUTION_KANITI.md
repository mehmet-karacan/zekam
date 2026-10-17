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
eklendi. Gercek model/provider cagrisi veya native kabul yapilmadi; testler final
toplu asamaya ertelendi, CALISTIRILMADI.

## Final yerel test kaniti

17 Ekim 2026 native OpenCode loopback smoke'unda `opencode v2.0.30`
adapter'in text-only event fallback'i gercek child process, CAS stdin ve
artifact yazimi ile `outcome=success` verdi. Bu kaynak degisikligi final pytest
kosusundan sonra yapildigi icin asagidaki pytest sayilari onceki kaynak
revizyonunun kanitidir; parser degisikligi icin yeni final kosusu gelistirme
tamamen durduktan sonra yapilmalidir.

17 Ekim 2026'da kaynak revizyonu `ce57d3c7d772f5c9bb5c50951b49413dfd05cf11`
sonrasinda tek toplu pytest kosusu exit code 0 ile tamamlandi:
`7987 passed, 1409 skipped, 3 warnings` (54:33). Bu kosu yerel regresyon
kapisini kapatir; native host/tool-denial, signer/credential, bagimsiz kalite,
gercek provider/model ve restore kabulunu kanitlamaz. Yeni kaynak degisikligi
yapilmadikca pytest yeniden kosulmayacaktir.
