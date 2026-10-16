# Smart Export canonical import ve indeks kaynagi

## Admitted kaynak ve effect

Smart import plan v2 exact original source identity, raw byte digest/size,
destination library root/ref ve source snapshot contract tasir. `--uygula`
existing canonical Run/Work revision ve exact plan digest ister; preview Work,
grant veya migration olusturmaz. Root/source/hash/snapshot before/after drift'i
reddedilir. Ham XML'in topology/credential alanlari model veya CAS sonucuna alinmaz.

`project.odi-smart-import` reserved canonical operation'dir. Generic consumer
alamaz. Kopyalama ve binding yazimi Work/project lease ve claim sonrasindadir;
DB write transaction disinda ilerler. Yeni hedef dizin/dosyalara private izinler
uygulanir, existing dizin izinleri sessizce degistirilmez. Kaynak ve hash okumasi
128 MiB ile bounded; ancestor link/reparse reddi ve descriptor/path readback vardir.

Private temporary dosya bounded copy/fsync/hash sonrasinda exclusive hard-link
ile yayinlanir. Existing farkli veya private olmayan hedef overwrite/adopt edilmez.
Exact private content-addressed dosya yeniden okunarak kullanilabilir. Kesinti
sonrasinda temporary dosya yalniz kendi file identity'si korunuyorsa temizlenir.
Hard-link desteklemeyen filesystem guvensiz overwrite'a dusmek yerine hata verir.

Private binding v2 actual job/Work/Run/binding, admitted plan ve original/destination
path baglarini tutar. Onceki mapping exact bytes private content-addressed history
dosyasinda korunur. CAS sonucu raw XML, original/destination absolute path veya
raw belge govdesi tasimaz. Canonical result/checkpoint/receipt yazimi tamamlanmadan
binding dosyasinin varligi index authority sayilmaz.

## Okuma ve index

`project odi-smart-source` actual completed receipt, binding/source snapshot ve
private current/history bytes zincirini salt okunur dogrular. Current mapping ile
historical mapping ayridir; imported raw file'i tekrar hashledigini iddia etmez.

Index/status icin `load_smart_binding` ayni completed canonical zinciri zorunlu
tutar. Legacy/unbound JSON veya kayip receipt otomatik adopt edilmez. Current
imported source hash/private-file ve binding head readback'i provider/index
isleminden once uygulanir. Indexte mevcut explicit ODI metadata disclosure ve
object-aware sanitizer kapilari korunur; excluded topology indexe tasinmaz.

Completed replay original export silinmis olsa bile actual receipt/private mapping
okur; copy, parser veya binding yazimini tekrarlamaz. Yari kalan import explicit
recovery ister; ayni komut tekrarinda silent completion/rewrite yoktur. Yeni import
eski verified mapping'i history'de tutar, yeni canonical receipt olmadan current
index kaynagi uretmez.

## Kabul

Kaynak regresyonlari real SQLite/CAS/XML/private-copy ve sanitizer fixture'lariyla
hazirlandi. Kullanici home/library'sinde import veya migration yapilmadi; provider
cagrisi yoktur. Testler CALISTIRILMADI, tum gelistirmeler sonrasi final asamadadir.
Native Work/ODI ve genel WP/release kabulu bu kaynak diliminden cikarilmaz.
