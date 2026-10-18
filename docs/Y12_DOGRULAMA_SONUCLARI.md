# Y-12 final dogrulama ve teslim

Durum: **ONAYLI Y-01..Y-12 KAPSAMI TAMAMLANDI**.
Kapsam authority'si `AKTIF_GOREV.md` bolum 0.3'tur. Tarihsel genis WP/31-capability
matrisi bu teslimin yerine gecmez. Canli kurum servisleri ve asagidaki not-run
kontrolleri passed diye sunulmaz. Kaynak gelistirmeleri bittikten sonra tek final
test/fix asamasi yurutuldu; gecen testler yeniden baslatilmadi.

## Sonuc ozeti

| Kontrol | Sonuc | Kanit / sinir |
|---|---|---|
| Windows census / JUnit uzlastirmasi | PASSED | 9364 case: 8018 passed, 1346 gerekceli skipped; 0 unresolved failure, 0 eksik slot, 0 test source drift |
| Son devam kosusu | PASSED | Onceki 9063 case deselected; yalniz kalan 301: 281 passed / 20 skipped, 32.96 sn |
| Wheel mimari kontrolleri | PASSED | Daha once wheel yokken atlanan 4 kontrol, repo disindaki gercek wheel ile 4/4; assertion ve test kaynagi degismedi |
| Temiz wheel fonksiyonel kabul | PASSED | 6ffc3ce Python kaynagi icin 11/11: offline clean venv, site-packages import/manifest, version/help/init/doctor/skill prepare/resume/memory plan-apply-replay |
| Final wheel kaynak esligi | PASSED | 503 Python modulu kabul edilen wheel ile byte-ayni; yalniz AKTIF_GOREV.md, manifest ve RECORD degisti. Izole ortama yeniden kurulum, installed manifest ve authority parse gecti |
| E5 CPU gercek embedding | PASSED | 384 boyut, finite/L2, query/passage, attention-mask-aware average pooling; TR/EN/TR-EN pozitif semantic margin; 2 intra-op / 1 inter-op thread, batch 4, remote cagri 0 |
| E5 kaynak / sure olcumu | OLCULDU | Ilk yukleme 4889 ms; sorgu medyan 8.55 ms; batch4 medyan 34.36 ms; proses CPU 5.109 sn; gozlenen peak RSS 462950400 bayt |
| Yerel cold / unchanged / delta indeks | PASSED | Cold 8467 ms, 3 yeni chunk; unchanged 6978 ms, 3 hit / 0 yeni, ayni generation; delta 7434 ms, 3 hit / 2 yeni chunk |
| TR/EN kaynakli yanit ve scope | PASSED | Gercek yerel E5 ile iki dilde ilgili kaynak ilk sirada; plain proje/kaynak etiketi; ayri iki proje indeksinde 4/4 scope/citation kontrolu |
| Maven/JaCoCo | PASSED | 3/3 gercek Maven senaryosu; mevcut 3 XML raporu sayaclariyla birebir dogrulandi. Tek modul line 4/6, branch 2/4; reactor util/service line 1/1 |
| Checkpoint/devam | PASSED | 32/32 context/native continuity senaryosu |
| Kullanici v2 backup / restore | PASSED | Bundle verify ve farkli yeni hedefe restore; mevcut home ustune yazilmadi |
| Learning v2 -> v3 ve v3 backup / restore | PASSED | Canonical migration terminal kaniti; integrity/FK ve eski icerik paritesi; v3 verify ve ayri hedefe restore |
| Guncel kullanici alani | PASSED | Salt-okunur SQLite doctor healthy; local-core all_ready=true, learning schema v3 |
| Paket / kaynak kapilari | PASSED | Son authority/projection ve manifest, paket/security history, wheel runtime reachability; degismeyen catalog/platform kapilarinin mevcut basarili kaniti korunur |
| Canli Oracle/Jira/Confluence/ODI | NOT-RUN | Kurum verisi/credential'i veya canli kurum servisi bu kabulde kullanilmadi; fixture/source sonucu ile karistirilmaz |
| MacOS / yeni Linux native matrisi | OUT-OF-SCOPE | Onayli bolum 0.3 karari |

RAG sureleri proses/import/model acilisini kapsayan tam CLI sureleridir; yuklenmis
E5 inference medyanlariyla ayni olcum degildir. Kucuk sentetik corpus sonuclari
buyuk kurumsal veri icin performans veya kalite garantisi vermez. Ilk RAG
olcumleri `final-wheel` artifact'ine aittir. Sonraki 4 Python degisikligi
RAG kanal izi, Work readback ve encoding sayacindadir; ilgili regresyonlar yeniden
dogrulandi. E5, Maven, backup ve checkpoint modulleri olcum artifact'iyle
byte-ayni oldugu icin gecen olcumler tekrar edilmedi.

## Gerekceli 1346 skip

| Neden | Case | Durum |
|---|---:|---|
| Harici gercek corpus yok veya gold revision uyusmuyor | 909 | NOT-RUN; kurumsal kabul iddiasi yok |
| Windows/POSIX farki veya yoneticisiz symlink/FS siniri | 425 | Bu hostta uygulanamayan kontrol |
| Opsiyonel veya sabit surumlu native CLI bulunamiyor | 6 | NOT-RUN; native model/CLI qualification kapsam disi |
| Tesseract veya tur/eng OCR dilleri yok | 5 | Gercek OCR NOT-RUN; yeni arac kurulumu yapilmadi |
| Canli provider kampanyasi | 1 | NOT-RUN; yeni canli kampanya acilmadi |

Skip'ler passed sayilmaz. Eski global capability matrix'i partial kalabilir;
bu rapor onun tum maddelerini veya tum canli entegrasyonlari tamamlandi saymaz.

## Basarisizliklar ve kapanislari

- Windows lstat/fstat ctime semantigi farki: path/fd kimlik karsilastirmasi
  duzeltildi; path ve descriptor'un kendi before/after drift sinirlari korundu.
- Exclusive-lock Work readback: kurucunun ConfigurationError donusu controlled
  unavailable yoluna baglandi; missing/invalid DB icin no-create/no-repair korundu.
- Harici test kosucusunda Windows spawn main guard eksigi: urun hatasi degildi;
  kosucu duzeltildi, ilgili spawn/recovery kontrolu gecti.
- Strict UTF-8 bozuk icerik encoding yerine unsupported sayiliyordu: transient
  encoding_error ayrimi eklendi; binary veri ve invalid text indekse alinmaz.
- Reference/neighbor ile eklenen RAG citation'in ilk sorgu kanal sirasi yokken
  StopIteration: kaynak korunur, olmayan rank uydurulmaz. Iki regresyon eski kodda
  fail verdi; duzeltme sonrasi ilgili RAG modulleri 48/48 gecti.
- Legacy allowlist testi eski daha gec hata mesajini bekliyordu: yalniz bu
  beklenti ve encoding/source-bytes assertion'lari guncellendi, exact case 1/1.
  Diger declaration/fixture AST'lerinin ayni oldugu kanitlandi; tekrar kosulmadi.
- Security history'de test redaksiyon canary'sinin exact istisnasi eksikti:
  ayni dosya/rule/fingerprint icin yalniz history kaydi eklendi; genel kural
  kapatilmadi, paket/security kontrolu yeniden gecti.
- TR agirlikli kucuk corpus'ta EN soru low-evidence abstain verdi. Negatif
  sonuc saklandi, esik gevsetilmedi. TR/EN corpus ve ayri TR-EN E5 probe gecti.
- Ilk backup parent'i private olmadigi icin yazmadan reddedildi; yalniz yeni
  ozel backup hedefi hazirlandi, mevcut veri veya sistem geneli ACL degismedi.

Run6'daki iki runpy uyarisi capability_worker main/oversize child branch
fixture'larindan geldi. Run3b'deki duplicate ZIP entry uyarisi kasitli negatif
archive fixture'indandir. Bunlar sessizce temizlenmis basari iddiasi degildir.

## Bagimsiz inceleme, kaynak ve teslim siniri

`1739a5f..c4a1e5e` ayri gpt-6-astra reviewer tarafindan incelendi. Uc onemli
bulgu (native candidate producer, v2 backup uyumu, v3 skill prepare) uygulandi
ve regresyonlari dogrulandi. Bu, son HEAD icin yeni imzali bagimsiz Work
attestation'i degildir. Wheel fonksiyonel kabulunu builder yurutmustur;
`independent_verifier=false` acikca korunur. Bu kaynak teslimi canonical
Work/lease/claim/receipt durumu yaratmaz.

Son Python duzeltmesi `6ffc3ce`, son test-beklentisi duzeltmesi `e5c442e`.
Final wheel repo disindadir; model agirliklari ve kullanici verisi Git'te degildir.
Korunan dort untracked kullanici girdisi degistirilmedi veya sahiplenilmedi.
Terminal run ve case birlesimi `final-census-junit-audit.json` ile pinlidir.
Son Git commit/remote eslesmesi teslim yanitinda gercek readback ile verilir.

## Yavaslik olcumu

En yavas tek case 73.702 sn (native projection zinciri). Ayni gruptaki diger
case'ler 67.068 / 62.279 / 61.467 sn; 50k corpus kontrolu 58.782 sn.
Sureler mevcut JUnit kayitlarindan alindi. Son kosu gecen 9063 case'i tekrar
calistirmadan kalan 301'i 32.96 sn'de tamamladi.

## Repo disi kanitlar

Artifact alani `C:/temp/zekam-y12-e3741df`. Bu tablo yalniz sonuc/hash tasir;
yedek icerigi, anahtarlar, ham transcript veya kurumsal icerik Git'e alinmaz.

| Referans | SHA256 |
|---|---|
| `final-census-junit-audit.json` | `sha256:a0fe8e94c53c25da5f7bc59f269bf0f7c475b75f31fb3d09c0a4c17fd3833f13` |
| `test-census-post-allowlist.json` | `sha256:ee0225bfd9698c8ff3229efd60f3b40a6d97698cbabe423d021553149e339b2c` |
| `test-continuation-plan-run6.json` | `sha256:1a704c50cb93583d4eaeb0c35de6b776cdecd2f37aa14102bbfa452a75517f24` |
| `test-continuation-plan-run7.json` | `sha256:931f077974a001c19a9a66103e966f1d44a242fed24a71f1320d6ce373e18838` |
| `remaining-scope-check-run7.json` | `sha256:4235e9181324508c97bb6e843597222090c0e584a56b14128ed8030686c2c38a` |
| `test-case-source-reuse-audit.json` | `sha256:9db5bb7e9e67c015de6dda153e8e9cff50c9f9b03503579a6783944b1b7ad745` |
| `junit-run6.xml` | `sha256:7a11e15b2f6017743a74adb42df53029a07d6bc73badf2fdf37c6359e5643027` |
| `junit-run7.xml` | `sha256:2f8b9e66bdd809ddb0a5408a78dd0b3ca82f54b2cf93d7cb8a943d7146cb851b` |
| `rank-fix-junit.xml` | `sha256:c5a0d108f9473fbb311168ae74193e5c09df4ceadbf33211f3d1653a6681ad71` |
| `allowlist-fix-junit.xml` | `sha256:933c539439fde96b820feb2d28fbe59b43e3b193f862b77942bc59c432ea1815` |
| `wheel-architecture-junit.xml` | `sha256:f42d1ae0127df5723f66d22c56e87b3008490f08367c0b686f574fe77123e80d` |
| `wheel-architecture-input.json` | `sha256:a239a3cafc0f6849ac3d94cc0a7524313ed43ad1effd23597acdd88882a77f00` |
| `release-rank/wheel-functional-acceptance.json` | `sha256:a8c7f06fb13e0d1c868a6c53a5d0c916dbd944343ce2aa3cdc1851563eaa1590` |
| `release-final/resource-equivalence.json` | `sha256:e7ec31f0f2e65f19cbc62a2541eeb05656b5c73beb198a45b72cb80387fb7d8f` |
| `acceptance-reuse-source-audit.json` | `sha256:579b2bfcad084d85f99962f6785fb2be17577e5c417581fe4d44e7b360004363` |
| `e5-acceptance.json` | `sha256:25551350697599b18962b1b0072f72ec23306ac85346ef1b2a70c511b38a82ff` |
| `final-wheel/rag-performance.json` | `sha256:e713c9f9547c9ee5175323b898ac9c5bb6e16ebcaed17f3957d28ec87e38ac63` |
| `final-wheel/rag-language-acceptance.json` | `sha256:4ea935f01904fe9842f29bea06031484ef47a4a990db18dba6df1f1913e7019a` |
| `maven-junit.xml` | `sha256:e331540262f599ce3721a84a7433e73faa28cbf84417ffd9198360dfe64d35af` |
| `maven-xml-evidence-audit.json` | `sha256:28efbf9c77c1dcfe4253ec424b0acfce9aaee744560291413af82b630863153b` |
| `checkpoint-junit.xml` | `sha256:29f0ec9c7f537bb731c9fc0d89ef5c31df750e4accbf65cdc1678783746c9269` |
| `dual-junit.xml` | `sha256:d940bc0ddb25125c07a9ec2640ede8193cd9d0e799cfb87d34cd47e30b05a13f` |
| `user-migration-apply.log` | `sha256:84d66d534764109fab28021c0572fb1bf235df614255a3fe73cdf0ca1c2ef263` |
| `user-migration-readback.json` | `sha256:f5e81e65392bb8c7dc59e33321a3a1d75f3f62d6b6c6e8d9cd97d5b149eb12f2` |
| `user-backup-verify.log` | `sha256:ea44e40918b7aefe107503c7d35a66229c9efe7039c8474d46761394e64ec786` |
| `user-backup-restore.log` | `sha256:2007d31101e5a5bf521d0a8ad0074a3ce7f99ba481f5c84bd2d1187cfebd49a1` |
| `user-v3-backup-verify.log` | `sha256:a14e629ca6c2a5b506db92a4b39073bd86357dc182a050777949d25ab6e68316` |
| `user-v3-backup-restore.log` | `sha256:60b11d04aafcbaeb0b36c85b0cc756c6946ff9811b891bbdb0f293b9732bb3b5` |
| `user-final-doctor.json` | `sha256:562e4b391c57f7a7383ade20de76e819647da7e049175ecf52e267fcb04956df` |
| `source-gates.json` | `sha256:5788596f632eed1286ca445fc404a0a3a967b5ef21d81a94773e88098025647e` |
| `package-validation-rank.json` | `sha256:92185f06a85bfa2a115cd50f15be55e8242612a1bd99aa35f864cdf1004bb3e2` |
| `security-policy-junit.xml` | `sha256:4a788e0b511facf66e2d145bf66adc5a8eefb3a5c649123132d71113f142f1d2` |
| `release-rank/runtime-reachability.log` | `sha256:f0bfd9b4315ad2f07466aa1b69c6a6622d5118fa000287bb295fec41d158f117` |

Final wheel: `release-final/wheel/zekam-0.1.0-py3-none-any.whl`

SHA256: `4d63b52453fe38996e2f9a726d2db62262a187f17af149e3d6d5a01bc592be25`.


Son kaynak snapshot'i icin `package-validation-delivery.json`, gereksinim
uzlastirmasi icin `final-acceptance-audit.json`, Git commit/remote ve artifact
bagi icin `release-final/git-publication.json` kullanilir. Bu son kayitlar
tracked belgeye kendi digest'ini yazan bir checksum dongusu kurmadan, teslim
snapshot'inin dosya ve sonuc hash'lerini repo disinda birbirine baglar.
