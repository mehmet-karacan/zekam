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
E5 icin batch limiti akista 4'e indirilir. Eski BGE yolu yalniz acikca eski profil
secildiginde `opencode-bge-m3` dizininde tutulur.

Kontrol edilen canonical kullanici index konumlarinda bu gecis baslangicinda
aktif production index bulunmadi. Bu nedenle ilgisiz SQLite veritabanlarina
dokunulmadi; yeni production index gelistirme tamamlanip son testler gectikten
sonra olusturulacaktir.
