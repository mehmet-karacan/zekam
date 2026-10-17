# Y-12 dogrulama sonuclari

Durum: **SURUYOR**. Genel Windows test paketi henuz tamamlanmadi; bu rapor
release onayi, Work kapanisi veya tum urunun gectigi iddiasi degildir.
Son kabul edilen kaynak duzeltmesi `39359a4`; temiz wheel `e00648d` HEAD'indeki
ayni kaynak ve package manifestinden uretildi. Korunan dort untracked girdi
pakete veya test kabul kapsaminda sahiplenilmedi.

## Kapsam ve gercek sonuclar

| Kontrol | Sonuc | Kanit / sinir |
|---|---|---|
| Yerel E5 CPU | PASSED | 384 boyut, finite/L2, TR/EN/TR-EN semantic probe; 2 intra-op thread, inter-op 1, batch 4; remote cagri 0 |
| E5 ilk yukleme / sorgu / batch4 | OLCULDU | 4889 ms / medyan 8.55 ms / medyan 34.36 ms; proses CPU 5.109 sn, gozlenen peak RSS 462950400 bayt |
| Son wheel temiz kurulum | PASSED | 11/11: izole venv, offline wheelhouse, site-packages import/manifest, version/help/init/doctor/skill prepare/resume/memory plan-apply-replay; self verification |
| Yerel cold indeks | PASSED | 8467 ms tam CLI suresi, 3 chunk, 3 yeni embedding |
| Degisikliksiz tekrar | PASSED | 6978 ms tam CLI suresi, 3 cache hit, 0 yeni embedding, ayni generation digest |
| Delta indeks | PASSED | 7434 ms tam CLI suresi, 3 cache hit, yalniz 2 yeni chunk |
| Native TR/EN kaynakli yanit | PASSED | Son wheel, gercek yerel model, iki dilde ilgili kaynak ilk sirada; plain proje/kaynak etiketi var |
| Iki proje indeksinde scope/citation ayrimi | PASSED | 4/4 gercek SQLite/FTS5/sqlite-vec fixture testi; provider-free degraded yol |
| Kaynak okuma / bellek adayi regresyonlari | PASSED | 87 passed, 3 Windows platform skip; ctime farki/descriptor drift ve kanitli candidate/correction |
| Checkpoint/devam | PASSED | 32/32 context-continuity ve native-continuity test senaryosu |
| Gercek Maven/JaCoCo | PASSED | 3/3; tek modul + iki modullu reactor. Gercek XML: fixture 4/6 line, 2/4 branch; util/service 1/1 line |
| Learning-v2 kullanici yedegi | PASSED | 8 dosyali bundle create/verify ve mevcut home'a dokunmadan yeni hedefe restore |
| Learning-v2 -> v3 | PASSED | Canonical memory prepare terminal receipt; integrity/FK ok; eski tablolarin logical content paritesi ve schema3/all_ready readback |
| Learning-v3 kullanici yedegi | PASSED | Guncel kullanici bundle create/verify ve ayri yeni hedefe restore |
| Paket/kaynak kapilari | PASSED | Paket yapisi + tam Git security history, manifest, catalog, platform ve son wheel runtime reachability gecti; bulgu sonrasi exact fixture history kaydiyla tekrar dogrulandi |
| Tum ilgili Windows testleri | RUNNING | Run2: 625 passed / 282 skipped / 1 failed; Work readback hata sinifi duzeltildi, ilgili 31 test gecti. Tamamlanmis dosyalar JUnit ile baglanip kalan kapsam kosulacak |
| Canli Oracle/Jira/Confluence/ODI sistemi | NOT-RUN | Bu final kabulde kurum verisi/credential'i veya canli kurum servisi kullanilmadi; fixture/source kapsamiyla karistirilmaz |
| MacOS / yeni Linux native kabul matrisi | OUT-OF-SCOPE | Onayli 0.3 kapsam karari |

Tam CLI sureleri proses/import/model acilisini da kapsar. Bunlar yuklenmis
E5 inference medyanlariyla ayni olcum degildir. Kucuk sentetik veri setinin
sonuclari buyuk kurumsal corpus icin performans/kalite garantisi vermez.

## Basarisiz veya reddedilen kontroller

- Run1, `e3741df` kaynagini import ettikten sonra Windows path/descriptor ctime
  farkinda hatalar verdi. Gercek fixture ile neden dogrulandi, `39359a4` ile
  duzeltildi. Run1 eski import edilmis kodu kullandigindan bilerek sonlandirildi;
  basarili tam kosu sayilmaz. Guncel run2 ayni kabul kapsaminda suruyor.
- Ilk focused kosu 64 passed / 1 failed: test history listesinin ilk revision'ini
  yeni revision sanmisti. Exact revision digest readback ile duzeltildi;
  ilgili tekrar 87 passed / 3 platform skip.
- TR agirlikli kucuk corpus'a Ingilizce soru `abstained-low-evidence` verdi.
  Negatif sonuc `final-wheel/rag-performance.json` icinde korunur. Esikler
  gevsetilmedi. TR/EN kaynakli corpus ile iki dil kabulunde kaynakli yanit gecti;
  ayri E5 TR-EN probe da pozitif semantic margin verdi.
- Ilk kullanici backup hedefinin parent ACL'i private olmadigi icin yazmadan
  reddedildi. Yalniz yeni ozel backup parent'i private yapildi; mevcut veri veya
  sistem geneli izinler degistirilmedi. Yeni hedefte verify/restore gecti.

- Son paket kontrolu ilk denemede history secret-pattern bulgusu verdi.
  FakeMaven redaksiyon canary'sinin ayni dosya/rule/fingerprint kaydi current
  icin zaten reviewed idi; yalniz exact history kaydi eksikti. Gercek credential
  degildi. History istisnasi eklenince tam tarama PASSED, history_complete=true,
  errors=[]; allowlist sozlesme kontrolleri 2/2 gecti. Global kural kapatilmadi.

- Run2 `test_exclusively_locked_work_readback_reports_unavailable` ile durdu:
  operational store kurucusu lock/schema okuma sorununu ConfigurationError'a
  cevirirken readback adapter yalniz sqlite3.OperationalError yakaliyordu.
  ConfigurationError da bounded work-readback-unavailable olur; schema/lock
  dogrulamasi gevsetilmez. Exclusive lock, writer-lock ile read-only okuma,
  missing/invalid DB'de no-create/no-repair dahil ilgili 31 test gecti.
  Kalan dosyalar kosulurken run2'de tam biten dosyalar yeniden calistirilmayacak;
  collection census ve JUnit node kimliklerinin birlesimi tam kapsam icin okunacak.

## Bagimsizlik ve kalan is

`1739a5f..c4a1e5e` kaynak araligi ayri gpt-6-astra reviewer tarafindan incelendi.
Uc onemli bulgu (v2 backup uyumu, v3 skill prepare, eksik candidate producer)
uygulandi ve regresyonlari dogrulandi. Bu, guncel HEAD icin yeni imzali bagimsiz
Work attestation'i degildir. Wheel functional kabulunu ayni builder yurutmustur;
farkli basliklar bagimsiz verifier gibi sunulmaz.

Siradaki is: run2 sonucunu al, gercek hatalari gider, gerekli etkilenen tekrarlar
ve tam Windows kabulunu bitir; son kaynak kapilarini ve artifact baglarini
kontrol et. Y-12 checkbox ve goal, gereken kanitlar tamamlanmadan kapanmaz.

## Repo disi kanit baglari

Asagidaki referanslar tek final artifact alanina gore relative'dir. Kullanici
backup'inin kendisi, anahtarlar, ham transcript veya kurumsal icerik Git'e alinmaz.
Artifact alani: `C:/temp/zekam-y12-e3741df`. SHA256 mevcut dosya baytlarindan alindi.

| Referans | SHA256 |
|---|---|
| `e5-acceptance.json` | `sha256:25551350697599b18962b1b0072f72ec23306ac85346ef1b2a70c511b38a82ff` |
| `final-wheel/wheel-functional-acceptance.json` | `sha256:f4634d85c01882e93ffbb0b2d216535b400300dc47b178c1d0a99c2072c271f6` |
| `final-wheel/rag-performance.json` | `sha256:e713c9f9547c9ee5175323b898ac9c5bb6e16ebcaed17f3957d28ec87e38ac63` |
| `final-wheel/rag-language-acceptance.json` | `sha256:4ea935f01904fe9842f29bea06031484ef47a4a990db18dba6df1f1913e7019a` |
| `maven-junit.xml` | `sha256:e331540262f599ce3721a84a7433e73faa28cbf84417ffd9198360dfe64d35af` |
| `maven-report-summary.json` | `sha256:860e51dd8afff25e6ba6339f85cb66b6c1d1d3d0cbd5578a19e1992b53f093e4` |
| `dual-junit.xml` | `sha256:d940bc0ddb25125c07a9ec2640ede8193cd9d0e799cfb87d34cd47e30b05a13f` |
| `checkpoint-junit.xml` | `sha256:29f0ec9c7f537bb731c9fc0d89ef5c31df750e4accbf65cdc1678783746c9269` |
| `focused2-junit.xml` | `sha256:79308ce8f832075f212841f58229bdfe9fa78752199e4446b61e14c3b539b8de` |
| `user-backup-verify.log` | `sha256:ea44e40918b7aefe107503c7d35a66229c9efe7039c8474d46761394e64ec786` |
| `user-backup-restore.log` | `sha256:2007d31101e5a5bf521d0a8ad0074a3ce7f99ba481f5c84bd2d1187cfebd49a1` |
| `user-migration-readback.json` | `sha256:f5e81e65392bb8c7dc59e33321a3a1d75f3f62d6b6c6e8d9cd97d5b149eb12f2` |
| `user-v3-backup-verify.log` | `sha256:a14e629ca6c2a5b506db92a4b39073bd86357dc182a050777949d25ab6e68316` |
| `user-v3-backup-restore.log` | `sha256:60b11d04aafcbaeb0b36c85b0cc756c6946ff9811b891bbdb0f293b9732bb3b5` |
| `source-gates.json` | `sha256:5788596f632eed1286ca445fc404a0a3a967b5ef21d81a94773e88098025647e` |
| `package-validation-final.json` | `sha256:75269c14b08528c07adac13dbd8f7e2e8e0e878a8bde96f42653483c17340995` |
| `package-validation-fixture-reviewed.json` | `sha256:52018c5efaf63d4ab51eb64b4a6ac9d857bbd40ba6e22a9e691c69f81263fe7b` |
| `security-policy-junit.xml` | `sha256:4a788e0b511facf66e2d145bf66adc5a8eefb3a5c649123132d71113f142f1d2` |
| `runtime-reachability-final.log` | `sha256:f0bfd9b4315ad2f07466aa1b69c6a6622d5118fa000287bb295fec41d158f117` |
| `junit-run2.xml` | `sha256:e0242dfedcf7a13fcf486ad5799957c400598eddfb6d8f98acb654cd07ce2954` |
| `work-readback-fix-junit.xml` | `sha256:f7a7c7f373101a1bcdcf4c32ab8a9d905a7ac9b4e365c646624703d9fc8ced0f` |
