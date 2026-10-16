# Yerel multilingual-e5-small ONNX kurulumu

Bu belge Zekam'in Windows CPU embedding gecisini kaydeder. Agirliklar repository
disinda, kullanici veri alaninda tutulur ve Git'e eklenmez.

## Sabit model kimligi

- Hugging Face deposu: `Xenova/multilingual-e5-small`
- Ust model kimligi: `intfloat/multilingual-e5-small`
- Sabit revision: `761b726dd34fb83930e26aab4e9ac3899aa1fa78`
- Agirlik: `onnx/model_int8.onnx`
- Agirlik SHA256: `4d24e2bc01a447951524466ef533e52944bf48509e6552810bcee1a2711cb02c`
- Windows kalici kok: `%LOCALAPPDATA%/Zekam/models/Xenova-multilingual-e5-small/<revision>`

Kurulum manifesti model, tokenizer, config ve sentencepiece dosyalarinin revision
ve digest bagini tasir. Saglayici agirlik digestini manifestteki ve sabit beklenen
SHA256 ile karsilastirir; uyusmazlikta fail-closed davranir.

## CPU dogrulamasi

Kurulu model ONNX Runtime `CPUExecutionProvider` ile yuklendi. ONNX girisleri
`input_ids`, `attention_mask` ve `token_type_ids` olarak dogrulandi. Embedding
sozlesmesi:

- `query: ` ve `passage: ` E5 onekleri,
- attention-mask-aware average pooling,
- L2 normalizasyonu,
- 384 boyut, sonlu float32 ve birim norm,
- 2 ONNX intra-op thread, 1 inter-op thread ve en fazla 4 metinlik batch.

Sinirli provider smoke dogrulamasinda CPU provider saglikli bulundu; 384 boyut,
semantic margin `0.107487`, batch-sayi tekil farki `0.017548`, batch cosine
`0.997012` olarak olculdu. Turkce, Ingilizce ve capraz dil teknik sorgu/passage
kontrolleri basariliydi. Dinamik padding, sabit 512 token padding'e gore kisa CPU
sorgularinda daha dusuk gecikme sagladigi icin secildi; quantization batch farki
bounded toleransla kabul edilir.

Tam repository test paketi ve gercek proje yeniden indeksleme, gelistirme
asamalarinin son dogrulama kapisinda calistirilir. Bu belgeye o kapinin terminal
kaniti ve makineye ozel performans olcumleri eklenecektir.

## Zekam entegrasyonu

Varsayilan bilgi profili `multilingual-e5-small-dense-v1`, `local`, 384 boyut ve
cosine'dir. Yeni SQLite vector yolu:

`knowledge-index/vector/multilingual-e5-small-onnx-int8/<proje>/`

Model/provider digesti, prefix sozlesmesi ve vector boyutu index generation
kimligine baglanir. Cache ve SQLite vec0 semasi provider boyutuna gore acilir;
E5 icin batch limiti akista 4'e indirilir. Eski BGE production indeksi yeni E5
akisina otomatik tasinmaz veya okunmaz. Varsa yalniz exact plan ile
`zekam project purge-legacy-index --json` komutundan alinan digest ve `--uygula`
ile `knowledge-index/vector/opencode-bge-m3/<proje>/` altindan silinir; E5
dizinine dokunulmaz. Proje RAG ve continuity yollari eski BGE ayarini veya
indeksini artik okumaz; yerel E5 profili disinda bir ayar fail-closed reddedilir.

Kontrol edilen canonical kullanici index konumlarinda bu gecis baslangicinda
aktif production index bulunmadi. Bu nedenle ilgisiz SQLite veritabanlarina
dokunulmadi; yeni production index gelistirme tamamlanip son testler gectikten
sonra olusturulacaktir.

## Son dogrulama notu

Gercek kurulu modelle yapilan son provider smoke kontrolden gecti: route `local`,
CPU provider, 384 boyut, birim norm ve saglik `True`; tek sorgu 4 ms, iki belgeli
batch 6 ms olctu. Gelistirme dilimleri tamamlandiktan sonra 17 Ekim 2026'da
calistirilan tek ve son toplu `python -m pytest -q` kosusu exit code `1` ile
sonuclandi: `7825 passed, 1406 skipped, 145 failed, 6 errors` (1:11:34).
Altı setup hatası eski BGE dual-index fixture'larının artık reddedilen
`opencode-bge-m3` ayarını kurmaya çalışmasından kaynaklandı. Başarısızlıkların
geri kalanı eski BGE/remote sözleşmeleri ile mevcut makineye bağlı SQLite,
skill ve unit-test loop fixture'larının uyumsuzluklarını içeriyor; bunlar E5
model doğrulama smoke kontrolünü geçersiz kılmıyor, ancak repository genelinde
yeşil test kabulü için ayrı uyarlama işi olarak açık kaldı.
Üretim corpus indexi bu kontrolde oluşturulmadı; aktif ZEKAM_HOME indexi
bulunmadığı için yeniden indekslenecek hedef de yoktu.
