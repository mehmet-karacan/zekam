# Native test kanit kaynagi

`zekam test source-evidence <job-id-veya-key> --home <home>` salt okunur olarak
mevcut `test.measure` sonucunun kaynak zincirini denetler. Ajan dogrulanmis
job kimligini mevcut canonical kayittan cozer; kullaniciya digest isi yuklemez.
Bu komut test veya provider calistirmaz, schema kurmaz, claim/receipt yazmaz.

## Okunan baglar

- Tamamlanmis canonical job, exact Work/Run/binding, claim/effect receipt,
  completion checkpoint ve terminal CAS artifact'i ortak replay okuyucusuyla
  dogrulanir. Gercek job referansi kullanildigindan nondefault execution key
  varsayilan key ile yeniden olusturulmaz.
- Request ve attempt tek SQLite okuma snapshot'inda okunur; kolon/body,
  request digest, proje, Work, Run, plan ve tarihsel source snapshot baglari
  karsilastirilir. Tum attempt gecmisi yuklenmez.
- Native attempt receipt CAS'i, measurement record/binding digest'i, aday test
  agacinin CAS'i ve tree fingerprint'i karsilastirilir. Kabul edilmis gozlemler
  receipt'in gercek observation satirlariyla eslesir. Failed receipt kabul
  edilmis gozlem tasiyamaz; basarisiz sonuc raporda basarisiz kalir.
- Her JSON nesnesi ve ledger body icin 4 MiB, observation sayisi icin 4096
  siniri vardir. Duplicate key, nonfinite JSON ve object olmayan CAS reddedilir.

## Sonucun anlami

`canonical_receipt_verified` ve `attempt_receipt_verified` kayit zincirinin
dogrulandigini soyler. `recorded_freshness` yalniz olcum anindaki kayittir.
Guncel checkout/HEAD veya bugunku test agaci tazeligi anlamina gelmez.

Ham Surefire/JaCoCo raporlari bu okuyucuda yeniden parse edilmez; CAS'teki
olcum ozeti ham raporun yerine gecmez. Bu nedenle `raw_reports_verified`,
`current_source_verified`, `independent_quality_verified`,
`publication_authorized` ve `grants_authority` false kalir. Bu rapor tek basina
signed publication evidence degildir ve Work/WP kapatmaz.

Siradaki producer baglari: immutable ham rapor kaynagi, exact publication
subject/HEAD ve guncel test agaci, gercek farkli verifier execution'i,
guvenilir host signing key sahipligi. Bunlar tamamlanmadan otomatik `passed`
imzalama veya yayin yetkisi uretilemez.

## Dogrulama durumu

Gercek SQLite/CAS fixture uzerinde native helper ve canonical runtime'i kullanan
regresyon kaynaklari eklendi. Measurer fixture'tir; gercek Maven/native kabul
kaniti degildir. Testler kullanici talimatiyla final toplu asamaya ertelendi,
CALISTIRILMADI.
