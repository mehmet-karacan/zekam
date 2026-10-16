# Canonical verifier kaynagi

`zekam test verifier-source <canonical-test-run-job> --home <home>` mevcut
tamamlanmis `test.run` sonucunu salt okunur denetler. Model, Maven, migration,
claim veya signing cagrisi yoktur. Ajan job kimligini mevcut kayittan cozer.

## Kalici olcum bagi

Loop artik measurement record ve binding govdelerini kendi digest'leriyle CAS'a
yazar. Rapor projection referansi, request ve attempt kimligi ayri immutable
measurement-evidence nesnesinde tutulur. Attempt evidence, accepted state ve
final terminal bu nesneye referans verir. Gercek runtime composition'i loop'un
measurement, terminal, candidate ve pre/postimage artifact'larini mevcut
operational artifact registry'ye kaydeder. Boylece yalniz bir hash'in var oldugu
varsayilmaz. Schema migration veya ikinci operational state dosyasi yoktur.

Canonical `test.run` sonucu var olan input digest'ini ureten exact non-secret
input govdesini de tutar. Input digest algoritmasi degismez: request, onayli
Maven plan, configured model/profile, realm/coordinator ve limit/approval kesiti
aynidir. Private endpoint/key veya ham transcript bu govdeye eklenmez.

## Okuyucunun denetledikleri

- Gercek job/claim/receipt/completion checkpoint ve CAS result; varsayilan disi
  execution key'de de job'un kendi payload/binding bagi okunur.
- Result input digest'i, request body/kolon kimligi, project/Work/Run/source
  snapshot ve tarihsel revision/content bagi. Config'teki operational DB yolu
  canonical yol ile ayni olmalidir; baska DB'ye sessiz gecis yoktur.
- Ledger'daki unique final `target-reached` terminali, ayni receipt digest'ine
  bagli tek completed attempt, actual observation satirlari ve yeniden hesaplanan
  coverage hedefi. Already-at-target yalniz ratio sonucu final verifier yerine
  kullanilmaz.
- Terminal/attempt/state measurement ve accepted builder referanslari; CAS'teki
  actual record/binding; accepted verdict, aggregate, observations ve source
  production/build fingerprint'i. Legacy eksik govde/ref uydurulmaz.
- Material scenario/defect/stale/scope/context durumu; final verifier'in exact
  candidate/measurement/accepted-files context'i, coordinator/plan/step ve
  configured model/profile bagi. Accepted verifier body gercek artifact'ten
  parse edilir; blocker veya yeni material risk varsa kabul edilmez.
- Tum accepted builder execution ref'leri; accepted attempt'in son builder patch'i
  actual candidate manifest'iyle karsilastirilir. Varsa report-facts CAS'i de
  bagiyla tekrar okunur. Native execution ve configured upstream ayrimi ayri
  raporlanir; alias farki veya replay basarisi bagimsizlik sayilmaz.
- Accepted attempt'in ara verifier execution'i de actual artifact/context/
  candidate/measurement/builder-envelope/role/model/profile ve accepted kalite
  verdict'iyle okunur; sadece final rol etiketine dayanilmaz.

Nesne ve ledger body'leri 4 MiB, observation sayisi 4096 ile sinirlidir; execution
okuyucusunun kendi 1 MB/512-builder sinirlari da gecerlidir. Eksik/tampered CAS,
belirsiz receipt veya drift partial basari uretmez. Tum CAS taranmaz.

## Sonucun anlami ve acik isler

`canonical_terminal_verified=true` gercek terminal zincirinin dogrulandigini,
`quality_review_recorded=true` accepted review kaydinin varligini belirtir.
Comparator'in kendi `canonical_terminal_verified=false` alani onun yalniz
execution CAS baglarini denetledigini belirtir; canonical anchor ust rapordadir.

`current_source_verified=false`, `independent_quality_verified=false` ve
`publication_authorized=false` korunur. Bu tarihsel rapor bugunku HEAD/test tree
veya publication subject'inin yeni verifier kabulunu uretmez. Commit sonrasi
review, publication subject/candidate eslestirmesi, trusted verifier producer,
host signer confinement ve native worker composition halen aciktir.

Gercek canonical SQLite/CAS zinciri uzerinde replay client ve scripted olcum
regresyon kaynaklari eklendi. Bu fixture gercek Maven/model/native kullanici
kabulu degildir. Testler kullanici talimatiyla final toplu asamaya ertelendi,
CALISTIRILMADI; Work/WP/final hedef kapanisi yapilmadi.
