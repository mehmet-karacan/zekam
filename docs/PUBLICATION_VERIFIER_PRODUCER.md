# Trusted verifier kanit ureticisi

`PublicationVerifierEvidenceProducer` trusted host worker'a exact typed signer
capability olarak enjekte edilir. CLI private key yolu, key olusturma, generic
sign-JSON veya model facing imzalama araci sunmaz.

## Kanit zinciri

1. Active registry/persistent pin, realm, config ve ayri verification-role producer
   okunur. Publisher ile ayni producer veya test-role kimligi reddedilir.
2. Actual completed `publication.review` kaydi cozulur. Canonical reader accepted
   verdict, native session/upstream ayrimi, historical builder, tam test agaci,
   commit araligi ve exact subject baglarini yeniden dogrular. Bilinmeyen/rejected
   review imzalanmaz.
3. Native test source/current Git target ve ham raporlar tekrar okunur. Exact
   request/measurement/receipt ve Run kimligi review kaynagiyla eslesir.
4. `publication.verification.attest` canonical claim/exclusive project ve global
   trust lease altinda imzalanir. Uzun islem boyunca DB write lock tutulmaz.
   Imza admitted public key ile denetlenir; trust/source post-effect reread'i
   basarisizsa completed receipt uretilmez, unknown effect recovery gerektirir.
5. Sonuc CAS/checkpoint/receipt'ten tekrar okunur. Actual review ve source proof,
   signature, input/idempotency key ve signing timestamp claim-receipt araligina
   baglidir. Completed replay model/Git/imza cagrisini tekrarlamaz; registry veya
   upstream receipt drift'i yine reddedilir.

`publication verification-evidence` salt okunur export'tur. `import-evidence`
export metadata'sini yetki saymaz: actual producer job yeniden okunur, exact belge
eslesmesinden sonra mevcut signed evidence ledger'ina kabul edilir. Generic
external trusted evidence yolu korunur. Publish approval ayridir.

## Kapsam ve kabul

Bu yol yalniz historical builder ile ayni production/build/full test tree'ye
sahip, net Git farki accepted test A/M dosyalarindan olusan commitleri kapsar.
Tam repository review veya her tur degisikligin otomatik onayi degildir.
Configured upstream ayrimi raporlanir; native olaylarda upstream kimligi dogrudan
gozlenmis gibi yazilmaz. Export tek basina kalite/Work/WP/release kapanisi uretmez.

Host key isolation/provisioning, gercek native composition ve live cihaz kabulu
halen aciktir. Kaynak regresyonlari real SQLite/CAS/Ed25519 protokol fixture'lari
icerir; upstream review/current source fixture'dir. Testler ve canli provider/model
cagrilari bu asamada CALISTIRILMADI; tum gelistirmelerden sonra toplu asamadadir.
