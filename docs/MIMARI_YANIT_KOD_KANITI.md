# Kod kanitli mimari ve modul yaniti

20 Ekim 2026 kullanici talebi. Bu, GPU-Fusion yerel kaynak baglantisindan sonra
gorulen yanit kalitesi eksigine ait ayri bir kaynak kodu duzeltmesidir. Onceki
operational Work kapanisi ve indeks receipt'i yeniden etiketlenmez. Bu belge
Work, lease veya receipt yerine gecmez.

## Gozlenen eksik ve kok neden

Gercek `zekam ask` sorgusu mimari sorusuna bir gorev belgesindeki gelecek kaynak
envanteri planini dondurdu. Kaynak baglantisi dogruydu, cevap soruyu karsilamadi.
Teknik kimlik icermeyen soruda dokuman onceligi ve belirsiz intent, kod yerine
planlari one aliyor; CLI ilk uygun alintiyi aciklama olarak kullaniyor.

## Onayli kapsam

- Mimari/modul sorusunu Turkce, ASCII Turkce ve Ingilizce ifadelerde tanima.
- Yalniz guncel proje/generation'a bagli, indekslenmis kod ve manifest kaniti.
  Moduller arasinda sinirli ve dengeli secim; genel anahtar kelime siralamasinin
  tek bir belgeyi one almasi mimari kanit sayilmaz.
- Sorumluluk ve teknoloji iddiasi gercek bildirim, endpoint, import veya
  bagimlilik kanitina dayanir. Dizin/sinif adi tek basina yeterli degildir.
- Her aciklama gercek citation/locator/digest ile baglanir. Cikarim ile gozlenen
  bildirim ayrilir; modullerin yan yana olmasindan bagimlilik uydurulmaz.
- Gorev/skill/checkpoint/planning belgeleri uygulanmis mimari kaniti olmaz.
  Kod eksikse kapsama siniri veya yanittan kacinma gorunur olur.
- Eski verbatim excerpt sozlesmesi ve normal sorgular korunur. Kaynak gizlilik,
  proje, generation, stale ve citation admission kapilari gevsetilmez.
- Mevcut indeks korunur. Kurumsal kaynak OpenCode'a aktarilmaz; harici LLM ile
  kurumsal icerik uretimi, arac kurulumu veya migration bu isin parcasi degildir.

## Uygulama ve kabul sirasi

1. OpenCode GLM aciklama/CLI gelistirmesini, Qwen SQLite aday secimini ve
   sentetik regresyonlari hazirlar; test kosmazlar.
2. Koordinator Codex, GLM ve Qwen kodunu farkli model olarak bagimsiz kaynak
   incelemesinden gecirir; onemli bulgular OpenCode tarafindan duzeltilir.
   Qwen gecikmesi nedeniyle ek Qwen inceleme cagrisi baslatilmadi.
3. Yeni kaynak dosyalari stage edildikten sonra paket manifesti uretilir;
   gelistirme commit/push yapilir. Bu asama runtime kabul iddiasi tasimaz.
4. Tum gelistirmeler bitince etkilenen regresyonlar bir son test asamasinda kosulur.
   Basarili eski testler tekrar edilmez; somut hata varsa yalniz ilgili kontrol
   duzeltilip yeniden kosulur. Paket metadata'si ayni son asamada dogrulanir.
5. Gercek GPU-Fusion mimari sorusunda modul sorumluluklari ve kod kaynaklari
   incelenir. Sadece answered/citation sayisi basari sayilmaz: kaynak turu,
   iddia-kanit bagi, modul kapsami ve plan alintisi olmamasi birlikte degerlendirilir.
6. Sonuc, kalan gercek sinirlar ve kanit referanslari kaydedilir; son commit/push.

Baslangic HEAD: `b09ccc5`. Bu belgenin ilk durumunda gelistirme ve final test
kabulu beklenmektedir. Gecici kanitlar repository disinda,
`ZEKAM_HOME/artifacts/gpu-fusion-architecture-20261020/` altinda tutulur.

## Gelistirme asamasi

- OpenCode Qwen ilk SQLite portunu/orneklemesini ve sentetik testleri yazdi.
  Bagimsiz Codex incelemesi; global sinirdan once modul/rol adaleti, SQLite
  pencere sozdizimi ve gercek helper sozlesmesi bulgularini cikardi. OpenCode
  GLM bunlari gecerli CTE secimi, ortak filtre ve scalar sayimla duzeltti.
- OpenCode GLM kod bildirimlerinden deterministik Turkce sorumluluk aciklamasi
  ve native RAG/CLI baglantisini yazdi. Codex incelemesindeki gercek Locator,
  citation binding, string/template ayrimi, Maven plugin bagimliligi, annotation
  siniri ve eski excerpt fallback bulgularini yine OpenCode GLM duzeltti.
- Kaynaklar yalniz ayni proje/generation'a pinli indeksten gelir. Maksimum 96
  aday, 12 modul, modul basina 8 dosya temsilcisi ve 12 aciklama iddiasi vardir.
  Dosyanin sonraki parcalari veya baska moduller disarida kalirsa kapsama
  kismi olarak isaretlenir. Imported `.zekam-reference/` dosyalari, gorev/skill/
  test/generated kaynaklari guncel depo uygulamasi sayilmaz.
- Her iddia gercek chunk, dosya ozeti, icerik ozeti ve locator ile canonical
  citation'a baglanir. CLI binding/tazelik kontrolunden gecmeyen mimari
  ozetinden eski gorev metnine donmez. Uretim indeksi degistirilmez.
- Provider sagliksizsa kod kaniti tam saglik iddiasi uretmez; acik degraded
  durumu korunur. Normal soru excerpt akisi korunur; mimari prose ayri
  `architecture_summary` alanindadir. Core bir harici LLM cevabi urettigini
  iddia etmez.

Gelistirme incelemesi runtime kabul yerine gecmez. Bu asamada urun testi
kosulmadi. Son kabul bolumu final asamada gercek komut ciktilariyla doldurulacak.

## Sinirlar

Bu, sinirli indeksli kod bildirimlerinden cikarilan rol ozetidir. Is kurallarinin
ve butun calisma zamani bagimliliklarinin tam haritasi oldugu iddia edilmez.
Yalniz sinif/dizin adi, yorum, docstring, keyfi string veya yan yana bulunma
framework/rol/bagimlilik kaniti olmaz. Eksik kod kapsami aciklanir.
