# GPU-Fusion yerel entegrasyon plani

19 Ekim 2026 kullanici talebi: GPU-Fusion entegrasyonunu OpenCode modellerine
yaptirarak tamamla; testleri tum gelistirmelerden sonra en son yap; her asama
sonunda Turkce ASCII commit ve push yap.

Bu, tamamlanmis Y-12 tesliminden ayri bir takip isidir. Belge operational Work,
lease, grant, receipt veya tamamlanma kaydi uretmez. Mevcut kullanici dosyalari ve
GPU-Fusion calisma agacindaki degisiklikler korunur. Mail gonderilmez.

## Baslangic durumu

- GPU-Fusion operational proje kaydi ve `gpu` alias'i var; RAG kullanilamiyor.
- Eski metadata taramasi 200000 envanter kaydinda durdu. Partial guard dogru
  bicimde kaynak baglantisini engelledi; baglanti ve indeks uygulanmadi.
- Operational veritabani v3; canonical index icin v7 gerekiyor.
- Yerel E5 ONNX CPU kurulumu mevcut; kaynaklar ve embedding harici modele gonderilmez.
- Zekam kaynagindaki kapsam disi untracked girdiler ve diger projenin calisma
  agaci degistirilmez. Kaynak degisikligi yalniz gercek Zekam kokunde yapilir.

## Gelistirme asamasi

1. **GLM / kaynak envanteri:** ignored dizinlerin cocuklarini kaynak indeksine
   katmayan acik `source-tree` modu; kapsam digest'e baglanir. Varsayilan
   `full-tree` ve legacy freshness ayni kalir. Partial/secret/symlink/cancellation
   guvenlik kontrolleri korunur. Regresyonlar hazirlanir, calistirilmaz.
2. **Qwen / dar migration girisi:** var olan guvenli v3->v5 migration'ini exact
   dry-run digest, yedek ve paused/disabled admission ile `db upgrade` yuzeyinden
   kullanilabilir yap. Evolution grant veya Windows supervisor acma. Eski
   migration yollarini koru; regresyonlari final asama icin hazirla.
   Ayrica yerel on kontrolde secilen parcalarda e-posta ve yazar etiketi
   oruntuleri goruldu. Qwen, kaynak dosyasini degistirmeden bu tur parcalari
   embedding/cache/indeks oncesinde dislayan dar bir gizlilik kapisi ekler;
   yalniz dislanan parca sayisi ve sabit politika digest'i kaydedilir. Ham deger
   veya icerik raporlanmaz. Politika canonical index planina baglanir.
3. Degisiklikleri farkli OpenCode modeliyle incelet; onemli bulgulari duzelt.
   Kaynak ve paket manifestini esle. Gelistirme commit/push; final test sonucu
   olmadan dogrulanmis urun teslimi iddia etme.

## Final dogrulama ve gercek entegrasyon

4. Gelistirmeler tamamlandiktan sonra yalniz etkilenen kaynak kesfi, proje RAG,
   canonical index ve migration regresyonlarini bir final test asamasinda calistir.
   Gecmis 9364 vakalik Y-12 census'unu yeniden baslatma. Basarili testleri tekrar
   kosma; somut hata cikarsa yalniz hata ve duzeltmeden etkilenen kontrolleri yinele.
5. Gercek operational v3->v5->v6->v7 gecislerini her adimin exact planini yeniden
   okuyarak, yedek ve terminal migration sonucu ile uygula. v8/v9'a bu is icin
   gereksiz gecis yapma; autonomy acma.
6. GPU-Fusion kaynak baglantisini kapsam ve gizlilik kontrolleriyle kur. Kanonik
   Work/Run, snapshot ve effect claim/receipt baglarini gercek API kayitlarindan
   cozumle. Id, revision veya digest'i kullaniciya yazdirma; Markdown'dan uretme.
7. Yerel CPU embedding ile batch=4, thread=2 indeksle. Stale veya partial kaynakta
   yayinlama. Uzun islemin lease/receipt durumunu izle; belirsiz effect'i otomatik
   tekrar etme. Uzak provider, Oracle, eski indeks silme veya GPU dosyasi yazma yok.
8. Yerel dogal dil sorgusuyla kaynakli yaniti ve citation bagini final kabulde
   dogrula. Kurumsal kaynak icerigini koordinasyon/model istemine veya Git'e alma;
   yalniz durum, sayilar ve dogrulama metadata'si raporla.
9. Sonuclari, varsa acik sinirlari ve kanit referanslarini kaydet; final asama
   commit/push ve remote HEAD eslesmesini dogrula. Yalniz tamamlanmis gercek etki
   icin basari bildir; Work kapanisini gerekli verifier olmadan uretme.

## Geri donus

Kaynak patch'leri commit ile izlenir; kullanici degisiklikleri geri alinmaz.
Migration yedekleri ZEKAM_HOME altinda tutulur. Kaynak veya plan stale ise yeni
indeks aktiflestirilmez. Eski indeks varsa silinmez; yeni jenerasyon yalniz
basarili yerel yayinda gorunur olur. Hata durumunda kanitli son guvenli adimdan devam edilir.
