# Host-owned publication test producer

`PublicationTestEvidenceProducer` trusted host worker'a verilen
`Ed25519ReceiptSigner` capability'sini kullanir. Model-facing sign-JSON araci,
private-key dosya argumani, otomatik key arama/olusturma veya registry yazimi
sunmaz. Gercek host private-key confinement/provisioning entegrasyonu halen
ayri gelistirme ve kabul kapisidir; bu sinif kendi basina OS izolasyonu saglamaz.

## Uretim

- Exact typed publication plan, active public registry role/realm/config ve
  persistent settings pin'i dogrulanir. Kaynak actual completed `test.measure`
  job kimliginden cozulur.
- Input, publication subject + source job + producer UUID + trust digest'e
  baglidir. Generic worker'in alamadigi `publication.test.attest` canonical
  operation'i kullanilir. Project lease'e ek global publication-trust lease'i
  registry rotation ile cakismayi onler; schema migration yoktur.
- Current test source okuyucusu claim oncesinde, imzalama icinde ve terminal
  yayin oncesi dogrulanir. Native measurement/receipt, actual raw report ve
  exact root/HEAD/test tree kontrolleri olmadan test evidence uretilmez.
- Imza yalniz canonical effect claim sonrasi atilir. `passed` ve zaman/model
  ciktisi disaridan alinmaz: test scope qualified oldugunda `passed=true`,
  recorded_at host UTC clock, execution_id gercek test Run'i, executor_id
  admitted producer kimligidir. Imza mevcut role public key ile eslesmelidir.
- Source verification raporu ve imzali envelope canonical CAS/checkpoint/
  receipt sonucuna kaydedilir. Signer/anahtar serialized edilmez; ham test XML,
  JVM properties veya test output yeni artifact'e alinmaz.
- Kaynak/config/grant-bag drift veya kesinti sonrasi completed export yoktur;
  unknown effect sessizce yeniden imzalanmaz. Recovery gerekir.

## Salt okunur export ve import

`zekam publication test-evidence <producer-job> --home <home>` gercek tamamlanmis
producer job/claim/checkpoint/artifact receipt zincirini okur. Sonuc shape/input,
public signature, source native receipt/measurement, current admitted key,
publication subject/binding/target ve claim-signing-receipt zaman araligi
dogrulanir. Raporlara yeniden Maven kosusu yapilmaz; private key gerekmez.

`import-evidence` bu export belgesini de alir. Belgedeki producer job'u tekrar
okuyup tum export'la exact karsilastirir; JSON metadata'sindan receipt uretmez.
Ardindan mevcut signed envelope import kapisini uygular. Sadece bir signed
test evidence importu, publication approval veya verifier evidence degildir.

Tamamlanmis `produce` replay'i yeniden imza/current source taramasi yapmaz;
ayni canonical export'u dondurur. `historical_source_proof=true` ve
`current_source_rechecked=false` bu ayrimi gosterir. Aktif role/pin degisimi
eski key ile yeni authority kurulmasina izin vermez. Final Git publication
exact target ve ayri approval kapilarini yeniden uygular.

## Acik baglar ve dogrulama durumu

Host signer key sahipligi/izolasyonu, native worker composition ve gercek
bagimsiz verifier producer halen aciktir. Test producer independent quality
incelemesi veya yayin yetkisi uretmez; bu alanlar false kalir. Gercek farkli
verifier execution/model proof olmadan Work/WP veya hedef kapanisi yoktur.

Gercek SQLite/CAS ve ephemeral Ed25519 fixture'lari icin claim-before-sign,
exact replay, yanlis key/role, post-sign drift, missing source CAS ve generic
worker reddi regresyon kaynaklari eklendi. Current source adapter bu protocol
fixture'inda mock'tur; gercek native host/Maven kabul kaniti sayilmaz. Testler
kullanici talimatiyla CALISTIRILMADI.
