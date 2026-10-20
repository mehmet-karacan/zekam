# GPU metadata yerel indeks takip isi

20 Ekim 2026 kullanici talebi: GPU veritabani metadata bilgilerini mevcut
GPU-Fusion yerel vektor indeksine dahil et. Onceki entegrasyon ve mimari yanit
kabulunden ayri takip isidir; bu belge operational yetki veya tamamlanma uretmez.

## Kapsam ve plan

1. Mevcut, hash ile sabitlenmis metadata snapshot'ini yerinde oku. Canli Oracle
   baglantisi, tablo satirlari, parola, serbest yorum veya veri ornegi alma.
2. OpenCode GLM ile bounded offline snapshot adapter'i ve mevcut canonical
   project index girisine baglantisini gelistir. Tablo/kolon/tip, constraint,
   foreign key ve indeks bilgilerini kaynak kimligiyle uret. Dosya yolu,
   boyut, hash, sayilar, tanim kimlikleri ve gizlilik kontrolleri fail-closed olsun.
3. OpenCode Qwen ile farkli model incelemesi yap; onemli bulgulari gider.
   Sentetik regresyonlari hazirla; gelistirmeler bitmeden test calistirma.
4. Tum gelistirmelerden sonra etkilenen testleri bir final asamasinda calistir.
   Gecen, degismemis testleri tekrarlama. Paket/projection butunlugunu dogrula.
   Gelistirme asamasini Turkce ASCII commit ve push ile kaydet.
5. Yeni canonical Work/Run/source snapshot/plan bagini gercek kayitlarla kur.
   Yerel E5 CPU, 2 thread, batch=4 kullanarak kod ve metadata'yi ayni atomik
   generation'a al. Mevcut embedding cache'i yeniden kullan. Eski generation'i
   yeni yayin basarili olana kadar koru; claim/terminal receipt'i readback yap.
6. Gercek metadata sorusunda kaynak locator'ini yeniden ac; 384 boyutlu yerel
   vektorlerin ve kaynak kodu kapsaminda kayip olmadiginin kanitini kaydet.
   Sonucu ve sinirlari raporla; asamayi commit ve push ile kaydet.

## Kabul ve geri donus

- Snapshot surumu/hash'i plan ve yayin girdisine bagli; degisirse eski plan reddedilir.
- Kurumsal metadata OpenCode istemine, Git'e veya uzak embedding servisine gitmez.
- Atif Oracle nesnesine ve indekslenen snapshot'a bagli; canli DB guncelligi iddia edilmez.
- Mevcut kaynaklar korunur; migration, kaynak repo degisikligi veya DB yazmasi yapilmaz.
- Hata halinde onceki atomik generation aktif kalir; belirsiz effect otomatik tekrarlanmaz.
- Test, review ve gercek indeks receipt'i alinmadan tamamlandi denmez.

## Durum

OpenCode GLM/Qwen gelistirmeleri ve bagimsiz Codex koordinatör kaynak incelemesi
sonrasinda final kapsam 214 benzersiz kontroldur: 212 basarili, 2 Windows symlink
yetki atlamasi, 0 acik hata. Ilk calismadaki 12 hata yalniz basarisiz kontrollerle
duzeltildi; gecen 199 kontrol tekrarlanmadi. Oracle N/A indeks durumu icin bir
yeni kontrol eklendi ve gecti. Paket kapisi son kaynakla yeniden dogrulanir.

Gercek offline on kontrol 1005 tablo icin 2124 metadata parcasi uretti; bu asamada
embedding veya indeks yayini yapilmadi. Canli Oracle'a baglanilmadi. Yerel E5 ile
canonical Work/Run indeksleme ve kaynak atfi kabul kaniti henuz bekliyor.

Kayitli offline snapshot secilmeden sonraki siradan kaynak yenilemesi metadata'yi
sessizce dusurmez: secim acikca istenir. Native ajan mevcut yerel state'ten snapshot
bagini okuyarak ayni yoldan devam eder. Snapshot tazeligi canli Oracle tazeligi degildir.

OpenCode uretiminde uzun dusunme/kesilen cikti sorunlari goruldu. Kullanici-geneli
ayar degistirilmeden, yalniz gorev surecine ait Qwen cagrilarinda desteklenen
`chat_template_kwargs.enable_thinking=false` istendi; kod ciktilari 17-40 saniye
araliginda alindi. Kurumsal metadata govdesi model istemlerine veya Git'e alinmadi.
Hatali/yer tutucu ciktilar incelemede reddedildi; eski generation korunuyor.
