# Zekam surum ve devir raporu

Bu rapor mevcut kaynak agaci ve kanonik runtime uzerinden 2026-08-22 tarihinde
yeniden dogrulanan durumu tasir.

## Durum

| Alan | Deger |
|---|---|
| Urun / CLI | Zekam / `zekam` |
| Kaynak kok | `repository:zekam` |
| Faz | 18/18 kalite kapisi geciyor |
| Global DoD | 82/83 passed, 1 pending, 0 failed, 0 blocked |
| Migration | Kaynak head 21; kimlik daraltmasi onceki ledger checksum'lariyla uyumsuzdur |
| Unit test | 1161 passed, 2 beklenen Windows symlink skip |
| Statik kalite | Ruff format/check ve strict mypy temiz |
| Paket | Validator passed; temiz wheel 42 SQL ve yalniz `zekam` CLI/paket tasiyor |
| Doctor | Aktif Zekam DB cutover tamamlandi; healthy, migration head 21 |

## Tekil kimlik

Zekam tek Python paketi, CLI, environment, home, schema ve DB kimligidir.
Compatibility import, CLI, locator, evidence reader, role veya setting alias'i yoktur.
Migration kaynaklarinin kimligi de daraltildigi icin onceki ledger ustunde checksum
waiver uygulanmaz; fresh Zekam semasi ve dogrulanmis veri aktarimi gerekir. Temp PostgreSQL
kabulü doğru non-canonical test locator'i olmadan çalıştırılmaz.

Fresh green semada head 21 kurulumu ve veri geri yuklemesi tamamlanmistir: 95 tablo
icin satir ve birincil anahtar digest farki yoktur, 170 FK orphan kontrolu temizdir,
8 buyuk tabloda full-row digest esittir ve 107645 vektorun boyutu 1024'tur. Aktif
baglanti cutover'i tamamlanmis, onceki container durdurulmus ve Doctor aktif Zekam
semasinda healthy sonuc vermistir.

Temiz ve benzersiz bir build/smoke ortaminda uretilen wheel 21 up ve 21 down SQL
tasir. Fresh kurulumda kaldirilan paket/modul/CLI bulunmaz, yalniz `zekam` console
script'i vardir. Wheel icindeki migration kokunden fresh gecici PostgreSQL semasi
head 21'e cikmis, head 20'ye geri alinmis ve yeniden head 21'e uygulanmistir; son
durum current'tir. Gecici veritabani test sonunda kaldirilmistir.

CLI-native RAG-first baslangic politikalari Codex, Claude ve OpenCode'un kendi
global discovery dizinlerine self-contained olarak yerlestirilmistir. Kullanici
kokundeki ortak yonlendirme dosyasi kaldirilmistir. Mevcut acik istemci
oturumlarinin yeni startup politikasini almasi icin yeniden baslatilmasi gerekir.

## Acik kriter

Yalniz `ZEKAM-DOD-025` pending durumdadir. Eski OpenCode/AIHub kampanya kaniti
yeni kaynak revizyonu ve Zekam identity binding'i ile current degildir
(`canonical-campaign-current-binding-drift`). Bu kimlik calismasi yeni provider
cagrilarina yetki vermedigi icin kanit uydurulmamis ve ag/provider cagrisi
yapilmamistir. Yeni exact kampanya ve one-shot authorization ile tekrar
calistirildiginda kriter supported kanit yolu uzerinden kapanabilir.

## Geri donus ve veri tasima

Kaynak rollback'i exact `e978e8e916cdca29914e7a58049683e1a89f5b08` commit'idir.
Onceki migration ledger'i source checksum'lariyla uzlastirilmaz. Operator immutable
DB backup'i alir, fresh Zekam semasini kurar, kanonik satirlari UUID/digest alanlarini
degistirmeden aktarir ve receipt/claim/realm invariantlarini yeniden dogrular.
