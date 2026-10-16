# Commit sonrasi yayin incelemesi

## Kaynak uygulamasi

`publication review-plan` mevcut yayin plani, canonical native test receipt'i,
historical `test.run` builder receipt'i ve explicit provider profiliyle salt okunur
plan hazirlar. Model cagirmaz, kaynak icerigini ekrana vermez, yetki uretmez.

- Historical builder'in full test tree, secili production ve Maven build/plan
  baglari guncel native olcumle ayni olmak zorundadir. Commit revision degisikligi
  eski review'u yeni subject'e tasimaz: yeni reviewer guncel subject'i inceler.
- Context, secili production dosyalarinin ve kabul edilen test dosyalarinin tam
  UTF-8 icerigini, davranis planini ve olcum baglarini tasir. Tam repository review
  iddiasi yoktur. Eksik/kesilmis context ile model cagrisi yapilmaz.
- Butun builder execution'lari native olmali; reviewer ayni incelenmis profil
  altinda hepsinden farkli upstream modele cozulmelidir. Alias farki yeterli degildir.
- Sarmalanmis context 200000 byte, tek dispatch, timeout 1..600 saniye ve output
  1000000 byte sinirlari plan digest'ine baglidir. Bilinmeyen provider request veya
  fatura maliyeti uydurulmaz. Context secret/PII kontrolunden gecmelidir.

Trusted host `run_publication_review` girisini exact plan digest'ine admit edilmis,
yayin/test/builder Run'larindan ayri bir canonical Run ile cagirir. Generic queue
consumer bu operation'i alamaz. Model cagrisindan once claim/exclusive project
lease olusur; uzun model cagrisi boyunca DB write transaction tutulmaz. Gercek
assignment/invocation/native result zinciri ve builder'dan farkli session yeniden
okunur. Son kaynak readback'i bozulursa completed receipt uretilmez; unknown effect
sessiz tekrar edilmez.

`publication review-source` tamamlanmis canonical receipt, CAS context/instruction,
native execution, olcum ve historical builder baglarini yeniden okur. Bu tarihsel
rapor current disk freshness, gozlenmis upstream model kimligi veya bagimsiz kalite
otoritesi degildir. Accepted, rejected ve unavailable sonuclari ayri saklanir;
unresolved senaryo accepted'a yukseltilmez.

## Acik kapilar

CLI model cagrisi, imzalama veya private key arama/olusturma sunmaz. Trusted
verification receipt producer, signer confinement/provisioning ve native host
composition sonraki baglardir. Kaynak uygulamasi Work/WP kapanisi degildir.

Regresyon kaynaklari SQLite/CAS ve dosya fixture'lariyla hazirlandi. Receipt
reader'lari taklit edilen fixture'lar live kabul sayilmaz. Bu asamada testler ve
canli modeller CALISTIRILMADI; toplu test asamasi tum gelistirmelerden sonradir.
