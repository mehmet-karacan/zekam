# Olculen tam test agaci

Maven olcumu artik actual reviewed plan'in tum reactor modullerindeki
`src/test` agacini surecten once ve sonra icerikten okur. Bu kapsam yalniz
degisen Java patch'ini degil, ignored test/resources dosyalarini da kapsar.
Metadata `zekam-unit-test-measured-tree/v1` kontratiyla before/after digest,
module dirs, olcum-oncesi path/digest listesi ve stable/drifted/unavailable
durumunu tasir. Ham dosya icerigi bu projection'a konmaz; secret/PII kontrolu
uygulanir. Contract ve kapsam gecerliligi Maven'den once de kontrol edilir.

Passing Surefire sonucu varken tree degisirse accepted/no-tests baseline
measurement `unbound-or-stale` olur; evaluation temizlenir. Timeout/tool/failed
siniflari korunur ve tree blocker'i eklenir. Okunamayan after tree de kabul
edilmez. Mevcut measurement record/binding digest algoritmasi degismez; tree
ayri evidence reference olarak canonical attempt/measurement zincirine baglanir.

## Verifier suresindeki degisiklik

Uretim `UnitTestLoopService` composition'i exact proje kokune bagli current
test-tree portu saglar. Measured full-tree fingerprint'i verifier oncesinde,
verifier sonrasinda ve terminal success oncesi yeniden karsilastirilir.
Current port yoksa veya okuyamazsa yeni snapshot'a dayali success yazilmaz.
Own patch digest'lerinin eslesmesi, diger test/resource dosyalarindaki drift'i
gizleyemez. Reddedilmis adayda mevcut ownership/preimage korumasi devam eder;
external resource degisikligi silinmez.

## Kalici kaynak

Batch loop ve native helper tree artifact'ini mevcut CAS/operational artifact
registry'ye kaydeder. Native attempt evidence veya loop measurement-evidence
nesnesi exact digest'i tasir. Salt okunur `source-evidence` ve `verifier-source`
okuyuculari varsa bu CAS'i tekrar dogrular; completed native candidate veya
accepted patch dosyalariyla eslestirir. Captured CAS missing/drift ise legacy
fallback yoktur. Eski kayitta ref yoksa yeni alan uydurulmaz; eski rapor digest'i
bu nedenle degismez. Yeni `measured_test_tree` alani sadece recorded ref varsa
uretilir.

## Sinirlar

Dosya sayisi 5000, toplam okunan test icerigi 64 MiB, tek dosya 4 MiB ile
sinirlidir. Ek link/junction/reparse dosya veya dizin artik sessizce atlanmaz:
kapsam dogrulanamadigi icin reddedilir. Directory enumeration hatasi da partial
tree uretmez. Bunlar test agacinin source protection sinirlaridir; baglanti
hedefi okunmaz. Snapshot helper en fazla 128 reactor modulunu kabul eder.

Bu before/after readback atomik filesystem snapshot veya butun OS'te write
izolasyonu iddiasi degildir. Gercek publish oncesi current HEAD/subject, model
independence, verifier producer ve trusted host/key acceptance baglari halen
gerekir. Bu gelistirme tek basina quality/publication authority veya WP kapanisi
uretmez.

## Dogrulama durumu

Fake Maven side-effect, stable resource snapshot, final verifier suresinde
external resource drift, eksik current port, schema/path/fingerprint drift,
link/junction ve aggregate byte budget regresyon kaynaklari eklendi. Canonical
SQLite/CAS verifier fixture'i de tree artifact zincirini tasir. Bunlar gercek
Maven/model/native kullanici kabulu degildir; testler kullanici talimatiyla
CALISTIRILMADI, final toplu asamada kalir.
