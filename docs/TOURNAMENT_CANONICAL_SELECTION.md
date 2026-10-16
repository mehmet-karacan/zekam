# Canonical tournament selection - WP-10.05

Bu dilim mevcut `skill.trial` candidate/baseline producer'ini kullanir; yeni
bir model router veya alternatif Work authority kurmaz. Kaynaklar
`application/tournament_runtime.py` ve `skill tournament-run/tournament-report`.

## Akis

1. Mevcut `skill trial-run` her aday icin gercek native baseline ve candidate
   dispatch'ini, local exact JSON oracle grade'ini ve canonical receipt'i uretir.
2. Tournament plani 2..4 farkli terminal trial'i acar. Project/Work/config/source,
   tek case/oracle, package/model alternatifi ve gercek native session tekilligi
   dogrulanir. Report JSON veya kullanici puani kaynak olarak kabul edilmez.
3. Her kaynak mevcut aggregate okuyucusundan yeniden gradelenir. Baseline fail,
   candidate pass ve iki gecerli process varsa aday eligible olur. Baseline ve
   candidate ayniysa ilerleme sayilmaz; regressions/failed process gizlenmez.
4. Eligible yoksa kendi canonical `tournament.select` claim/checkpoint/receipt'i
   ile no-progress/failed-source sonucu kaydedilir; selector cagrisi yapilmaz.
5. Eligible varsa exact Work/plan claim altinda tek native verifier-role selector
   dispatch'i calisir. Typed karar exact subject ve eligible source job'a baglanir;
   artifact/process/safety pass olmadan sonuc unverified kalir.
   Her adayin baseline/candidate grade'i, measured elapsed suresi, eligible,
   regressed ve failed alanlari raporda korunur; tek secim diger kayiplari gizlemez.
6. Report ve completed replay kendi claim/checkpoint/CAS receipt'ini, tum source
   receipt/artifact/assignment baglarini ve local grade'i yeniden okur. Replay
   yeni model/process baslatmaz, eksik kaydi yeniden yaratmaz.

## Butce ve durma

Plan en fazla 4 historical paired source (8 zaten yapilmis agent dispatch),
0 veya 1 yeni selector dispatch, 10..600 saniye process timeout, 180.000 byte
context ve 64.000 byte native artifact sinirini gosterir. Tek round'dur; automatic
retry, ikinci round veya no-progress sonrasinda yeni candidate cagrisi yapmaz.
Caller baska round isterse yeni exact plan/Work admission gerekir.

Native provider request, token ve maliyet olcumu mevcut adapter'de bulunmadigi
icin null kalir. Bunlar sifir veya gercek token/cost hard-cap diye sunulmaz.
Selector cagrisi icin bu unmetered boyutlara ayri explicit authorization
(`--authorize-unmetered-selector`) gerekir. Remote disclosure da ayrica
`--authorize-remote` ister. Bu iki CLI flag model benchmark kampanyasi izni
vermez ve gercek kullanici yetkisinin yerine gecmez.

## Bagimsizlik ve authority

Selector'in requested model/variant alias'i tum builder'lardan farkli olmali.
Review edilen current builder adapter config digest'i actual source trial'daki
digest ile eslesmelidir; profile degisikligi eski kayda yakistirilmaz. Frozen
modelID mapping'leri farkliysa reviewed-upstream-config-distinct raporlanir;
bu upstream kriptografik attestation degildir. Builtin requested-only durumda
sonuc selection-upstream-unproven kalir. Selector native session'i builder
session'lariyla ayni olamaz.

Olcum tek case ve aday basina tek paired sample kapsamindadir. Caller-supplied
dataset provenance bagimsiz dogrulanmis sayilmaz; full skill kalitesi, human
preference veya genis holdout genellemesi iddia edilmez. Secim activation,
promotion, root instruction/security/schema/retention degisikligi yetkisi vermez.
`upstream_identity_attested`, `quality_evaluation_complete`, `activatable` ve
`grants_authority` false kalir.

## Kanit durumu

Gercek SQLite/CAS chain ve offline native transport ile success, no-progress,
failed source/selector, safety ret, wrong subject/job, reused session, same model,
config/case drift, alias duplicate, foreign scope, lost artifact ve completed
replay regresyon kaynaklari hazirlandi. Fixture transport'u bagimsiz host/model
kabulu degildir. Testler kullanici talimatiyla CALISTIRILMADI. Gercek native,
genis fixture/baseline/quality/performance, quantitative metering ve WP kabul
kapilari aciktir. Kullanici DB migration'i veya canli provider cagrisi yapilmadi.
