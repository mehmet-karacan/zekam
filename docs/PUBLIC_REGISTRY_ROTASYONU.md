# Signed public registry rotation - WP-02.03

Kaynak: `application/git_publication_rotation.py` ve ortak canonical runtime.
Bu gelistirme public key registry'sini degistirir; private signing key olusturmaz,
tasimaz veya modele vermez. Gercek key sahipligi/provisioning ve producer execution
kabulleri aciktir. Bu turda kullanici registry/config/anahtar dosyasina yazilmadi.

## Plan ve admission

`git publication trust-rotation-plan <yeni-registry> --previous <terminal-job>`
incelenmis yeni public registry'yi mevcut Settings ve admitted aktif config
pin'ine baglar. Onceki kaynak yalniz gercek completed enrollment/rotation
Work/claim/checkpoint/CAS receipt zinciridir; serbest rapor JSON veya ayni hash'li
dosya bunun yerine gecmez. Canonical source `trust-source <terminal-job>` ile
salt okunur incelenebilir; historical read current file/authority kaniti degildir.

Yeni registry ayni project/realm'de ve onceki revision + 1 olmalidir. Plan onceki
job/receipt/trust digest, yeni config revision/digest ve yeni public registry
byte/logical digest'lerini tasir. Aktif config pin'i onceden admitted olmalidir;
bu komut config aktivasyonu yapmaz. Registry file bytes ile aktif pin eslesmiyorsa
normal publication yolu drift olarak kapali kalir.

## Eski producer'in imzali niyeti

`rotation_approval_body` exact plan digest, eski approval producer UUID ve canonical
UTC issued/expires zamanlarini baglar. Pencere pozitif ve en fazla bir saattir.
Imza onceki registry'nin approval public key'iyle dogrulanir; yeni key kendi
gecisini onaylayamaz. Signature tek basina publication veya model bagimsizligi
kaniti degildir. Signer guvenilen host/key servisi tarafindan saglanir; CLI
private-key/path veya modelin uretebildigi bir signer secenegi sunmaz.

`git publication trust-rotate <plan-file> --approval-file <signed-envelope>`
preview'dur. `--uygula --run-id ... --plan-digest ...` exact mevcut canonical
Work binding ister; gerekirse historical Work revision explicit secilebilir.
Plan/approval dosyalari drift'e karsi yeniden okunur. Yeni apply sureli niyeti
guncel zamanda, historical completed replay kayitli performed_at zamaninda kontrol
eder. Claim <= performed_at <= actual receipt zamani yeniden dogrulanir.

## Etki ve history butunlugu

- Enroll ve rotate ayni global registry lease kaynagini alir. Farkli project
  Work'leri cooperating canonical writer olarak ayni dosyaya paralel yazamaz.
- Yeni/admitted apply oncesinde bounded canonical terminal census okunur. Tek
  gecerli head'in tam lineage'i tum completed registry kayitlarini kapsamalidir;
  stale predecessor, fork veya eksik ancestor reddedilir. Timestamp veya en son
  dosya goruntusu tek basina head authority'si degildir.
- Registry census/lineage en fazla 64 completed adim, terminal artifact basina
  128 KiB okur. Sinir veya integrity hatasinda partial trust/head uretilmez.
- Bekleyen/belirsiz registry claim/receipt yeni yazmayi durdurur. Mevcut runtime
  recovery ve gereken trusted resolution ayrica tamamlanmalidir; bu producer
  unknown effect'i otomatik tamamlandi/not-applied saymaz.
- Kendi live claim'i altinda private exclusive staging, flush/fsync, son pin/
  imza/eski dosya readback'i ve atomik replace yapilir. Son callback yeni exact
  dosyayi ve admitted pin'i dogrular; ardindan canonical terminal receipt gelir.
- Yeni ilk enrollment, file missing olsa bile mevcut completed registry gecmisini
  sifirlayamaz. Completed replay missing/drift dosyayi onarmaz; source receipt
  gecmisini veya current config'i degistirmez.

`signing_keys_created=0`, producer_execution_verified/publication_authorized/
grants_authority false kalir. Public registry rotasyonu private key provisioning,
credential isolation, native host enforcement veya gercek test/verifier producer
kabullerini kapatmaz. Genel WP ve Work kapanisi yapilmaz.

## Bekleyen final dogrulama

Real SQLite/CAS fixture'larda iki ardisik rotasyon, eski imza/yeni pin, replay,
expired/yanlis/new-key signature, missing ancestor/CAS/file, revision/realm/project
drift, stale restored file, kesinti ve read budget retleri icin regresyon kodu
yazildi. Testler ve canli rotation/enrollment kullanici talimatiyla CALISTIRILMADI.
Final toplu test, gereken bagimsiz review ve gercek native/key ownership kabul
kaniti olmadan bu kaynak dilimi dogrulanmis urun sonucu sayilmaz.
