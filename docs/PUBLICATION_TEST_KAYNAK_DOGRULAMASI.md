# Publication test kaynagi dogrulamasi

`zekam publication test-source <plan-json> <canonical-test-job> --home <home>`
salt okunur bir kanit raporu uretir. Test, Maven, provider veya push calistirmaz;
Work/claim/receipt, imza veya authorization yazmaz. Canonical kimlikler ajan
tarafindan mevcut kayittan cozulur.

`publication plan` subject onizlemesi artik pending evidence alanlarini tasiyan
typed `plan` govdesini de verir; `evidence_bound=false` kalir. Boylece gercek test
kaniti uretilmeden once ayni publication subject'i incelenebilir. Pending
digest'ler kanit veya authorization degildir; gercek evidence import/approval
kapilari degismez.

## Kaynak zinciri

Once native `source-evidence` okuyucusunun canonical job/checkpoint/artifact,
request/attempt/receipt, measurement/binding ve candidate tree dogrulamasi yapilir.
Yalniz completed, accepted, target-met, fresh ve driftsiz sonuc; ayni proje,
Work, source snapshot ve config kabul edilir. Yeni report-facts CAS'i zorunludur:
hedef modullerin sirasini kalici kayittan alir; eski receipt icin modul tahmin
edilmez. Tarihsel okuyucu eski receipt'i gostermeye devam eder.

Publication Work/Run/revision/config/source/dependency bagi okuma oncesi ve
sonrasinda mevcut operational store'dan dogrulanir. Projenin bu cihazdaki private
source-root bagi cozulur ve tekrar okunur; kullanicidan rastgele root alinmaz.

## Yerel readback

- Bound Git transport exact clean root, device/root identity, remote identity,
  branch ref ve HEAD'i inceler. Source revision bu tam HEAD ile eslesmelidir.
  Remote'a ag cagrisi yapilmaz.
- Kayitli hedef modul listesiyle gercek Maven plani yeniden kurulur; plan/config
  ve launcher digest'i eslesir. Maven veya JDK version probe calistirilmaz.
- Production, tum reactor test agaci ve kayitli class dosyalari icerikten
  dogrulanir. Git'in ignore ettigi test/artifact degisiklikleri de kontrol edilir.
- Exact Surefire XML, JaCoCo XML ve exec dosyalari eski measurement digest'leriyle
  eslestirilir. Surefire ve JaCoCo XML mevcut guvenli parser'larla tekrar okunur;
  test verdict/sayaclari ve per-source coverage gozlemleri kayitla eslesir.
- Parse sonrasi class/rapor/production/test tree/plan ve Git target yeniden okunur.
  Eksik, degismis, traversal, unknown report kind veya butce asimi partial basari
  uretmez. Her dosya grubu icin en fazla 4096 dosya ve 128 MiB girdi vardir.

Ham rapor byte'lari yalniz bellek icinde okunur; CAS, log veya rapor ciktisina
kopyalanmaz. JVM properties ve testcase output kullaniciya dondurulmez.

## Sonucun siniri

`current_source_verified=true` ve `raw_reports_verified=true` exact bu readback'in
sonucudur. Dosya sistemi/Git uzerinde atomik snapshot veya kalici kilit iddiasi
yoktur; etkili producer bu kontrolu kendi claim altinda yeniden yapmalidir.
`runtime_jvm_reprobed=false` gorunur: launcher/config ayniligi JDK runtime'inin
yeniden olculdugu anlamina gelmez. HTML raporu veya model kalite incelemesi de bu
komutun kaniti degildir.

Bagimsiz verifier execution'i, host signing key sahipligi ve imzali producer
entegrasyonu halen gerekir. `independent_quality_verified`,
`publication_authorized` ve `grants_authority` false kalir. Bu komut tek basina
publication evidence importu veya Work/WP kapanisi yapmaz.

## Gelistirme durumu

Gercek gecici Git ve mevcut FakeMaven/parser fixture'lariyla current readback,
ignored artifact/test drift, yeni clean commit, parse sirasinda class degisimi,
raw counter drift ve path reddi regresyon kaynaklari eklendi. Gercek Maven kabul
kaniti degildir; testler final toplu asamaya ertelendi, CALISTIRILMADI.
