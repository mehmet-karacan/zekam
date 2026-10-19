# OpenCode model karsilastirmasi — 19 Ekim 2026

Bu yeni karsilastirma onceki Y-12 urun kabulunden ayridir. GPU-Fusion kaynak
envanteri gelistirmesinin tamamlandigini veya uretim RAG indeksinin hazir oldugunu
gostermez. Uygulama testleri yeniden calistirilmadi.

## Kapsam ve yontem

- Kullanici iki model icin toplam **40 cagriyi**, daha sonra parametre tanilamasi
  icin ayri **3 cagriyi** acikca onayladi. Gercek toplam **43**, otomatik tekrar **0**.
- Yerel OpenCode v2.0.30 calisir durumda; kayitli model rotalari
  `litellm/GLM-5.3-Flash-IT` ve `litellm/Qwen3.8-27B-IT`.
- Karsilastirma bu rotalara mevcut Zekam provider adapteriyle dogrudan yapildi.
  OpenCode CLI'nin arac kullanan tam ajan dongusu calistirilmadi. Model secimi icin
  sinirli gorev kanitidir; uretim qualification veya genel coding benchmark skoru degildir.
- Her modele ayni dort sentetik gorev, beser kez: fonksiyon uretimi, hata duzeltme,
  kod inceleme ve kaynakli Turkce analiz. Kurumsal proje dosyasi gonderilmedi.
- Istekler: `temperature=0`, `reasoning_effort=medium`, `max_tokens=4096`;
  iki paralel cagri ve cagri basina 120 saniye sinir. Sunucuda etkin reasoning
  ayarinin gercekten medium oldugu dogrulanmadi. Konfigurasyon degistirilmedi.
- Bes tekrar ayni istemi kullanir; bes farkli problemi temsil etmez. Gecikme,
  istemci surec baslatma ve sunucu beklemesini de kapsayan uc uca suredir.

Eski `zekam model campaign plan --json` kampanyasi 13 eski model ile guncel
iki modellik katalog uyusmadigi icin `blocked-catalog-scope-drift` verdi. Bu
eski kampanya calistirilmadi, kapsam/kanonik model kayitlari yeniden yazilmadi.
Kullaniciya sunulan ayri 40 cagrilik sentetik plan kaydedilip onun sinirinda kalindi.

## Sonuclar

Asagidaki tam basari, hem gorev kontrollerinin hem de istenen ciktinin gecmesidir.
Medyanlar basarisiz veya kesilmis denemeleri de kapsar; kullanilabilir kodun
uretim suresi olarak yorumlanmamalidir.

| Gorev | GLM tam basari | GLM medyan | Qwen tam basari | Qwen medyan |
|---|---:|---:|---:|---:|
| Yeni fonksiyon uretimi | 3/5 | 99,31 sn | 1/5 | 52,74 sn |
| Hata duzeltme | 5/5 | 23,67 sn | 5/5 | 11,53 sn |
| Kod inceleme, ilk strict rubrik | 1/5 | 55,79 sn | 4/5 | 18,97 sn |
| Kaynakli Turkce analiz | 5/5 | 9,92 sn | 5/5 | 5,63 sn |

Yeni fonksiyon araliklari normalize edip bitisik araliklari birlestirir; 132
kontrol girdi degistirmeme ve sabit tohumlu ornekleri kapsar. Hata duzeltmede
soldaki ilk eslesmeyi bulan ikili arama; 216 kontrol ve buyuk sanal dizi uzerinde
okuma sayisi siniri kullanildi. Dogru referanslar bu kontrolleri gecti; hatali
referanslar tam basari alamadi. Model kodu credential tasimayan ayri surecte,
AST sinirlariyla degerlendirildi.

### Inceleme rubrigi ve hata ayrimi

Ilk strict inceleme rubrigi belirli tekil satir numarasi ve dogrudan JSON ister.
Ayni siralama kusuru, erken yapilan effect satirina veya gec kalan kontrol
satirina atfedilebildiginden bu rubrik bazi dogru bulgulari eksik puanladi.
Ilk puanlar korunmustur. Sonradan yapilan, yeni model cagrisi icermeyen ek
icerik kontrolunde iki gecerli atif da kabul edildi ve JSON kod blogu acildi:

- Dort kusuru kaynakla dogru saptama: **GLM 4/5**, **Qwen 5/5**.
- Dogrudan parse edilebilir JSON: **GLM 3/5**, **Qwen 5/5**.
- Bu ek rubrik gozlemlerden sonra tanimlandi; ilk strict skoru degistirmez.

GLM'de 18/20, Qwen'de 20/20 tamamlanmis provider receipt var. GLM'de iki
cagrinin sonucu 120 saniye sinirinda `unknown-effect` olarak kaydedildi;
sessiz tekrar yapilmadi. Ayrica GLM'de bir, Qwen'de uc tamamlanmis provider
yanitindan uygun metin cikarilamadi. Bunlar erisim hatasi veya kesin bir
algoritma hatasi olarak etiketlenmedi; ham yanit saklanmadigindan kok nedenleri
kesinlestirilemiyor.

Qwen'in ayri bir kod yaniti `finish_reason=length` ile kesildi: 4096 cikis
tokeninin 4004'u reasoning olarak raporlandi; JSON tamamlanamadi. Bu, mevcut
butcede reasoning ile gorunur yanitin rekabet ettigine dair somut bir ornektir.

## Bundan sonraki gorev dagilimi

| Is | Oncelikli model | Uygulama |
|---|---|---|
| Yeni kod ve mevcut gelistirmenin uygulanmasi | GLM | Kucuk, tek amacli dilimler; bu ornekte goreli olarak daha iyi kod uretimi. Kesilmis veya zaman asimina ugramis sonuc kabul edilmez. |
| Kisa ve iyi tanimli hata duzeltme | Qwen | Iki model de 5/5; Qwen medyani daha dusuk. Degisiklik kapsamiyla sinirli kanit istenir. |
| GLM'nin degisikligini inceleme | Qwen | Farkli modelle inceleme; bulgular kaynak satirina baglanir. Bu sentetik inceleme tek basina gercek bir Work kapanisini dogrulamaz. |
| Kisa teknik soru, kaynakli ozet ve Turkce analiz | Qwen | Ayni tam basari, daha dusuk gozlenen medyan; kapsamli dokuman ve uzun baglam henuz olculmedi. |
| Maliyet oncelikli ikincil secenek | GLM | Kullanicinin sagladigi fiyat notunda birim fiyat daha dusuk; gecikme ve yanit tamamlama ayrica izlenir. |
| Gorsel, dosya, mimari ve cok adimli aracli ajan isleri | Henuz olculmedi | Duyurudaki yetenek beyanindan benchmark basarisi turetilmez. |

Bu dagilim ilk tercih kuralidir; otomatik provider/router veya kullanici-geneli
ayar degisikligi yapilmadi. Koordinator kapsam ve kaniti denetler; sonraki kaynak
uygulamasi OpenCode'a verilir. Urun testleri onceki kullanici karari uyarinca tum
gelistirmeler sonundaki final asamada kalir.

## Bosluklu reasoning anahtari: ayri 3 cagri

Anahtar adindaki sondaki bosluk JSON'da farkli bir alan olusturur. Sunucu ayari
degistirilmeden ayni sentetik JSON istegi, `temperature=0`, `max_tokens=512` ile
birer kez gonderildi. Bu tanilama 40 cagrilik karsilastirmadan sonra yapildi.

| Istekteki alan | Beklenen JSON dondu | finish_reason | Reasoning token | Cikis token | Sure |
|---|---|---|---:|---:|---:|
| Alan yok | Evet | stop | 68 | 75 | 1,83 sn |
| `"reasoning_effort":"max"` | Evet | stop | 68 | 75 | 2,27 sn |
| `"reasoning_effort ":"max"` | Evet | stop | 60 | 67 | 1,94 sn |

Her biri basarili HTTP yaniti aldi; adapter tam HTTP basari kodunu saklamadigi
icin 200 gibi bir kod varsayilmadi. Her istekte 36 giris tokeni raporlandi.
Uc cevap da gelmis olmasi, ayarin uygulandigini veya iki yazimin esdeger oldugunu
kanitlamaz. Her varyanttan tek ornekle hiz/kalite sonucu cikarilmaz. Destek ekibine
etkin ayar, desteklenen degerler ve bilinmeyen alanlarin nasil ele alindigi sorulur.

Mail yalniz yerel taslak olarak hazirlandi. Kullanici bu sohbet kapsaminda
**hicbir zaman mail gonderilmemesini** istedi; hicbir mail gonderilmedi.

## Yerel referanslar ve kanitlar

Kanitlar repo disinda `ZEKAM_HOME/benchmarklar/opencode-40-20261019/` altindadir:

- `plan.json`, `results.jsonl`, `report.json`: 40 cagrinin exact plan ve ilk sonuclari.
- `claims.db`: 40 claim ve 40 terminal receipt; iki unknown-effect icin recovery
  kaydi; SQLite integrity sonucu `ok`. Uretim Work/index veritabani degildir.
- `diagnostic-plan.json`, `diagnostic-report.json`, `diagnostic-claims.db`: ayri
  3 claim/receipt; ek tekrar ve recovery yok.
- `evidence-audit.json`: iki ledger ile 43 farkli cagrinin mutabakati, ilk puanlar,
  ek inceleme rubrigi ve artifact SHA256 degerleri.
- `model-reference.json`: kullanicinin fiyat, model ve duyuru notlari; bagimsiz
  dogrulanmis model yetenegi sayilmaz. Ic endpoint, takim kimligi veya credential yok.
- `notebook-reference.json`: API ornekleri notebook'unun yapisal incelemesi ve
  SHA256 kaydi. Notebook calistirilmadi; ham hucreler ve ciktilar kopyalanmadi.
- `destek-mail-taslagi.md`: parametre sorusu ve uc olcumun bulundugu, gonderilmemis taslak.

Iki model icin token kullanim bilgisi 17/20 cagrida kalici rapora alinabildi;
eksik kullanimi sifir maliyet sayan veya tam faturaymis gibi sunulan bir toplam
yoktur. Fiyat ve bilinen kullanimdan hesaplanan kismi tahminler yerel auditte bulunur.

Karsilastirma plan digest:
`sha256:1d127a7124ad0ee29b17e406675e29c0f1fff8a7c1c558422cb3f31346b30e2b`

Tanilama plan digest:
`sha256:62b912fae5a35f95792754c42cbf888f949a00209667b88a201386f7740674f3`

`evidence-audit.json` SHA256:
`a2966f7521e84cf2442247a3d5bf686b67c6819b14faa6353f9c88e20e90427f`
