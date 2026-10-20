# Metadata uzerinden veritabani semasi yaniti

20 Ekim 2026 kullanici talebi: GPU'nun temel tablolarini, PK/FK alanlarini ve
iliskilerini yerel metadata kanitindan acikla. OpenCode modelleri gelistirsin;
testler tum gelistirmelerden sonra, her asama Turkce ASCII commit/push ile kaydedilsin.

## Sorun ve kapsam

Mevcut genel veritabani sorusu gorev/skill belgelerinden alinti getirdi. Metadata
indeksinin varligi, tablo ve iliski aciklamasinin dogru oldugunu kanitlamadi.
Onceki kabul ve tamamlanmis operational Work/Run kayitlari yeniden etiketlenmez.

Bu duzeltme mevcut pinli generation'i okur; yeniden indeksleme, migration, canli
Oracle baglantisi, GPU dosyasi degisikligi veya uzak icerik aktarimi yapmaz.
Kurumsal metadata OpenCode istemine, ham loga veya Git'e tasinmaz. Modeller yalniz
Zekam'in public kaynak arayuzleri ve sentetik metadata ile calisir.

## Uygulama sirasi

1. OpenCode Qwen: yapisal metadata parcalarini tablo bazinda birlestiren, PK ve
   sirali bilesik FK alanlarini cikaran bounded yardimci. Her iddia exact parca
   kanitini tasir. Yalniz hedef P/U anahtari dogrulanan FK iliski olarak sunulur.
2. OpenCode GLM: SQLite'ta project/generation ile pinli metadata secimi, veritabani
   niyetinin genel belge aramasindan once yonlendirilmesi ve CLI kabul kapisi.
3. Farkli model Codex kaynak incelemesi; OpenCode ile bulgu duzeltmeleri ve sentetik
   regresyonlarin hazirlanmasi. Bu asamaya kadar test calistirilmaz.
4. Son asamada ilgili testler ve gercek native sorunun citation readback kabulu.
   Gecen degismemis testler tekrarlanmaz; hatalarda yalniz etkilenen kapsam kosulur.
5. Sonuclar, basarisiz kontroller ve sinirlar kaydedilir; kaynak/projection/paket
   butunlugu uzlastirilir. Her asama sonunda commit ve push yapilir.

## Kabul ve sinirlar

- Turkce/ASCII ve Ingilizce veritabani semasi sorulari metadata yolunu kullanir.
- Temel tablo secimi FK baglanti sayisina gore aciklanir; is kritikligi uydurulmaz.
- PK/FK alan sirasi, self-reference ve hedef P/U kaniti korunur. Ad benzerligi
  iliski kaniti degildir. Eksik hedef/anahtar cozulmemis olarak belirtilir.
- Yalniz pinli Oracle tablo locator'i ve icerik/source digest'i gecerli kanittir.
  Gorev belgeleri, kod veya talimat metni metadata yerine kullanilmaz.
- Stale, yetersiz veya gecersiz ozet eski belge alintisina dusmez.
- Sonuc offline son indekslenen snapshot'i anlatir; canli veri, satir sayisi,
  runtime enforcement veya tum uygulamanin is anlami hakkinda iddia uretmez.
- Mevcut kod-mimari ve normal sorgu yollari korunur; indeks generation'i degismez.

Geri donus: bu duzeltmenin kaynak commit'lerini geri al; mevcut metadata generation'i
ve eski kabul kayitlari aynen kalir. Bu belge claim/lease/receipt veya Work kapanisi
yerine gecmez. Kabul sonucu henuz alinmamistir.
