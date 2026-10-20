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
yerine gecmez.

## Sonuc ve kabul

OpenCode Qwen/GLM kaynak uretimi tamamlandi. Codex koordinator farkli model
olarak kaynak incelemesini yapti; hatali dataclass/locator/digest baglantilarini,
uretilen testlerin gercek API uyumunu ve kabulde bulunan noktalarini duzeltti.
Yer tutucu, eksik JSON ve gercek sozlesmeye uymayan model ciktilari kabul edilmedi.
Bu sonuc yalniz bu takip duzeltmesinin kaynak ve yanit kabuludur; yeni bir
operational Work/Run kapanisi iddia edilmez.

- Son test sonucu: **358 benzersiz kontrol; 352 basarili, 6 Windows symlink
  yetki atlamasi, 0 acik hata**. Testler gelistirmeler ve kaynak incelemesi
  bittikten sonra son asamada baslatildi.
- Ilk calismada 338 kontrol gecti; 12 hata ayni sentetik SQLite fixture'inin
  eksik `relative_path` bagindan kaynaklandi. Yalniz bu hatalar ve yeni tam
  dogal dil kabul regresyonu kosuldu; 13 kontrol gecti.
- Gercek yanitta disclaimer'in olumsuz cumlesi konservatif mutation kapisina
  takildi. Kapinin kurallari gevsetilmedi; yalniz kaynak aciklamasinin dili
  duzeltildi. Daha sonra alfabetik FK ornekleminin secilen tablolari golgelemesi
  OpenCode ile giderildi. Bu degisiklik icin yalniz 9 etkilenen/yeni kontrol
  kosuldu; onceki degismemis basarili kapsam bastan baslatilmadi.
- Native `zekam ask` sorusu artik gorev belgesi yerine metadata sema yolunu
  kullaniyor. **1005 tablo ve 159 FK** okunuyor; cozulmemis FK **0**. Genel
  yanitta en fazla **6 temel tablo ve 8 iliski** seciliyor. Her secilen tabloya
  sira veriliyor; kendi FK'leri once gosteriliyor, gelen FK yogunlugu diger
  tablolarin alanlarini gizlemiyor. Belirli tablo sorulari o tabloyu onceliyor.
- Son gercek yanitta **24 citation** var; hepsi generation, source/content
  digest, source revision ve locator ile yeniden acilarak dogrulandi. Native
  yanit admission'i dahil **13 kabul kosulunun tamami gecti**.
- Indeks generation'i degismedi:
  `sha256:01772cbab9e528b05d3175744bfa60f21ba4084a5cdb1b5a3d8422254b8ba1f6`.
  Uzak provider, yeni embedding, yeniden indeksleme veya canli DB okuma yok.
- Son kabul 10.39 saniyedir; native CLI ve 24 atfin yeniden okunmasini birlikte
  kapsar. Bu olcum izole sorgu/embedding performans benchmark'i degildir.
- Paket kontrolu guncel kaynakta **0 hata, 0 uyari** ile gecti.

Yerel turetilmis kabul kanitlari kullanici veri alanindaki
`artifacts/metadata-schema-answer-20261020/` altindadir: `source-review-final.json`,
`final-consolidated.json`, `native-acceptance.json`,
`package-validation-final-source.json`. Ham metadata govdesi, kullanici verisi
veya model transcript'i bu rapora veya Git'e alinmadi.

## Kalan sinirlar

Yanittaki temel tablo secimi yapisal FK baglanti sayisini gosterir; uygulamanin
is kritikligi veya tablo is sorumlulugu hakkinda tahmin uretmez. Oracle
`ENABLED/DISABLED` ve `VALIDATED/NOT VALIDATED` degerleri snapshot tanimidir;
canli kisit etkinligi veya veri kardinalitesi sayilmaz. Genel yanit acikca
kismi ornektir. Eksik hedef/P-U kaniti iliskiye donusturulmez.

Bu yuzey tablo kimliklerini kabul eder; SQL alias/kolon projeksiyonu veya sorgu
calistirma arayuzu degildir. Bilinmeyen acik tablo kimligi baska bir tabloya
sessizce yonlendirilmez. `snapshot_only` mevcut runtime'da stale sonucu belirtir;
offline kaynak niteligi ayri `database_freshness=last-indexed-snapshot` ile korunur.
