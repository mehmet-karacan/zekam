# Y-12 dogrulama sonuclari

Durum: **SURUYOR**. Genel Windows test paketi henuz tamamlanmadi; bu rapor
release onayi, Work kapanisi veya tum urunun gectigi iddiasi degildir.
Son kaynak duzeltmesi `6ffc3ce`; bu HEAD icin `release-rank` temiz wheel
kabulu 11/11 gecti. Genel test kaniti henuz tamamlanmadi. Korunan dort untracked girdi
pakete veya test kabul kapsaminda sahiplenilmedi.

## Kapsam ve gercek sonuclar

| Kontrol | Sonuc | Kanit / sinir |
|---|---|---|
| Yerel E5 CPU | PASSED | 384 boyut, finite/L2, TR/EN/TR-EN semantic probe; 2 intra-op thread, inter-op 1, batch 4; remote cagri 0 |
| E5 ilk yukleme / sorgu / batch4 | OLCULDU | 4889 ms / medyan 8.55 ms / medyan 34.36 ms; proses CPU 5.109 sn, gozlenen peak RSS 462950400 bayt |
| e00648d wheel temiz kurulum | PASSED (onceki artifact) | 11/11: izole venv, offline wheelhouse, site-packages import/manifest, version/help/init/doctor/skill prepare/resume/memory plan-apply-replay; self verification |
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
| Tum ilgili Windows testleri | RUNNING | Yeni census 9364 case; 6360 tamamlanmis case mevcut JUnit kanitiyla korunur (passed/skipped ayri). Run6 yalniz kalan 3004 case. Gecen case dosya yarida kalsa da yeniden kosulmaz |
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

- Tam collection census: 9362 test. Run2 JUnit'inde tamamlanmis 80 dosyada
  904 case vardir; son/hatali modul tamamlanmis sayilmadi ve yeniden dahil
  edildi. Devam kosusu 8458 case'i aldi; declaration/parametre sayilari ve test
  dosyasi SHA256'lari eslesti. Bazi unit testlerinin import-time UUID parametre
  etiketleri her collection'da degistigi icin ilk raw nodeid esitlik kontrolu
  hic test calistirmadan durdu. Sonra ayni kaynak dosyasi, test declaration'i,
  parametre sirasi/sayisi uzerinden eksiksiz kapsam denetlendi; yeni skip yok.
  Bu sefer tam kapsam onceki 904 case + kalan 8458 case kanitlariyla uzlasacak.

- Run3b: 1300 passed / 73 skipped / 1 failed. Windows spawn, harici
  devam-kosucusunu yeniden import edince eksik main guard nedeniyle ikinci
  pytest oturumu acilmisti; bu, fixture dizinine esit-zamanli cleanup girisimi
  dogurdu. Hard-exit/capture recovery testi standart pytest girisinden
  dogrudan 1/1 gecti. Harici artifact kosucusuna __main__ guard eklendi;
  test_evolution_capture modulu tamamen yeniden dahil edildi ve yeni kosu bu
  modulden sonra ilerledi. Urun kodu bu harness duzeltmesi icin degismedi.
  Run2+run3b JUnit'lerinden tam biten dosyalarda 2270 case dogrulandi; kalan
  7092 case yeni declaration/sayi/source-hash kontrolunden eksiksiz gecti.

- Run4: 2841 passed / 873 skipped / 1 failed. Strict UTF-8 discovery
  bozuk metni is_text=false yaptigindan plan bunu encoding yerine unsupported
  sayiyordu. Transient encoding_error metadata'si eklendi; desteklenen ama
  decode edilemeyen dosya skipped_encoding sayilir, NUL/binary ayri kalir.
  Bozuk icerik indekse alinmaz; normal dosyanin canonical body/digest'i degismez
  (false flag serialize edilmez). Discovery + project plan tekrarinda
  121 passed / 3 platform skip. Kalici schema veya kullanici verisi degismedi.

- Encoding duzeltmesi sonrasi yeni collection yine 9362 case uretti.
  Run2/run3b/run4 JUnit birlesiminde tam biten dosyalardan 5961 case kanitli;
  kalan 3401 case run5'te exact declaration/sayi/source-hash eslesti. Degisen
  project_knowledge_index ve source_discovery test modulleri tekrar dahil edildi.

## Bagimsizlik ve kalan is

`1739a5f..c4a1e5e` kaynak araligi ayri gpt-6-astra reviewer tarafindan incelendi.
Uc onemli bulgu (v2 backup uyumu, v3 skill prepare, eksik candidate producer)
uygulandi ve regresyonlari dogrulandi. Bu, guncel HEAD icin yeni imzali bagimsiz
Work attestation'i degildir. Wheel functional kabulunu ayni builder yurutmustur;
farkli basliklar bagimsiz verifier gibi sunulmaz.

Siradaki is: run2 sonucunu al, gercek hatalari gider, gerekli etkilenen tekrarlar
ve tam Windows kabulunu bitir; son kaynak kapilarini ve artifact baglarini
kontrol et. Work readback duzeltmesi 73c758e sonrasinda son wheel yeniden
uretilip temiz kabulden gecmeden mevcut artifact son kaynak diye sunulmaz. Y-12 checkbox ve goal, gereken kanitlar tamamlanmadan kapanmaz.

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
| `test-census.json` | `sha256:4dfba5788f1c45489aa406809956f4c2f17d3bb2e185055d5af42bae5738dbf6` |
| `test-continuation-plan.json` | `sha256:64bdeb7e8e592683c97b1bb258b2ec0bdd320b0a391059971db821c424789ba2` |
| `remaining-scope-check.json` | `sha256:4f5943b46aa1cade9e665b674f9b0084c3f584c73e586a2fd18d030e2d7af2ea` |
| `junit-run3b.xml` | `sha256:3ccfd9eeb0b8d31bef6f98b893f8e8abd43ac52050cfa298d3ee978c68333b01` |
| `spawn-runner-fix-junit.xml` | `sha256:38881467f18e65089b6719c35299113639f6e80c75d8528fb14ea0919d53ce12` |
| `test-continuation-plan-run4.json` | `sha256:5e9543b4045e824b03457852aca3f493fa6477380188ab9e435beafda3a0bace` |
| `remaining-scope-check-run4.json` | `sha256:a690813dc5debc4b7fc497c04561ad4bc9111c7b1006320e61e84dc0b3240278` |
| `junit-run4.xml` | `sha256:34b23a5033bea81591d815e809e14df7094808a652ae7e5ce12d33163ce5311b` |
| `encoding-fix-junit.xml` | `sha256:c67213d2ead981717bc40c39c4010c1e1de6ff52edf0a67be73f55e5bafc4e34` |
| `test-census-post-encoding.json` | `sha256:7f1658c6c3d53858169ce2aff2bd07872bcb5cd92cb3c5b6d26417e957611e97` |
| `test-continuation-plan-run5.json` | `sha256:7aa923a8df9e211f55302ed3d8199052342f69e02211dc5f8b541198be4155ed` |
| `remaining-scope-check-run5.json` | `sha256:2eae5edc0ef4e7d75f412c2bd07bb162a71fba840e478fe6d33af7cf276f2c0d` |


## Run5 terminal ve RAG kanal izi duzeltmesi

Run5: 394 passed, 2 skipped, 1 failed (112.78 sn). Gercek proje answer-key
kontrolu, reference/neighbor expansion ile eklenen citation'in ilk sorgu kanal
listesinde bulunmamasinda StopIteration veriyordu. Kanal sirasi yalniz gercek
ilk sorgu hit'lerinden alinir; baglam kaynagi korunur, olmayan rank uydurulmaz.
Iki yeni regresyon eski kodda ayni hatayi yeniden uretti. Duzeltmeden sonra
etkilenen embedded RAG modulu ve basarisiz answer-key kontrolu 48/48 gecti.
Gecmis basarili testler korunur; sonraki devam dosya degil test case duzeyinde
kalanlari secer. Y12 henuz kapanmadi; final wheel/gate ve kalan test kaniti acik.

- `junit-run5.xml` SHA256: `sha256:57eaf1ec2c9d0386071e8684d83eb6085195cf9d79bf699e859cf543177669ca`.

- `rank-fix-junit.xml` SHA256: `sha256:c5a0d108f9473fbb311168ae74193e5c09df4ceadbf33211f3d1653a6681ad71`.


## Case duzeyinde tekrar yapmadan devam

Kullanici karariyla run6 tamamlanmis dosyalari degil, tamamlanmis test
case'lerini atlar. Census 9364; run2/3b/4/5 ve etkilenen RAG fix JUnit
birlesiminde 6360 tamamlanmis case vardir. Bunlar passed/skipped olarak
ayrilir; skip basari iddiasi degildir. Kalan 3004 case'in declaration/sayi
ve SHA256 kapsam kontrolu gecti. Parametreli testin yalniz kalan slotlari
secilir; yeni JUnit eslemesi continuation plan'daki eski slot sirasini kullanir.
Run6 henuz aktif; sonucu alinmadan genel kabul kapanmaz.

6ffc3ce icin temiz wheel kurulumu 11/11, paket/security history kontrolu ve
wheel runtime reachability gecti. Bu belge guncellemesi Python kaynaklarini
veya paketlenen authority'yi degistirmez; wheel kaynak bagi korunur.

- `test-census-post-rank.json` SHA256: `sha256:aa7f1a6925dd9c93548db3fe91a8afc374144b23d82ec56e961322196ae6d047`.

- `test-continuation-plan-run6.json` SHA256: `sha256:1a704c50cb93583d4eaeb0c35de6b776cdecd2f37aa14102bbfa452a75517f24`.

- `remaining-scope-check-run6.json` SHA256: `sha256:675660783fa66078e0b55a41ea090447e1c71877853a99b00e56de149b2ef06f`.

- `package-validation-rank.json` SHA256: `sha256:92185f06a85bfa2a115cd50f15be55e8242612a1bd99aa35f864cdf1004bb3e2`.

- `release-rank/build-binding.json` SHA256: `sha256:0abeb6eed4167ff24b376bf034c9960734e17c5cbf14db118cd553b4c0a0910d`.

- `release-rank/wheel-functional-acceptance.json` SHA256: `sha256:a8c7f06fb13e0d1c868a6c53a5d0c916dbd944343ce2aa3cdc1851563eaa1590`.

- `release-rank/runtime-reachability.log` SHA256: `sha256:f0bfd9b4315ad2f07466aa1b69c6a6622d5118fa000287bb295fec41d158f117`.
