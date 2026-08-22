# Hibrit retrieval, citation ve değerlendirme

## Chunk profili

Chunk yapıyı bozmaz: başlık altındaki paragraflar birleştirilir, tablo ve kod
blokları profile göre bütün kalır. Büyük bir birim bölünürse parçalar **ebeveyn
chunk'a bağlanır** ve locator korunur.

Locator'sız chunk kabul edilmez — hem alan katmanında hem
`chunk_locator_not_empty` check constraint'inde. Profil değişirse
`profile_digest` değişir; bu yeniden indeksleme sinyalidir.

## Embedding profili

İlk kanonik profil: `openai/BAAI/bge-m3`, **1024 boyut, cosine**.

- Boyut uyuşmazlığı ve `NaN`/`Inf` değer indekslenmez (alan + trigger).
- Query ve passage prefix profilin parçasıdır; farklı prefix **ayrı profildir**
  ve aynı profil altında karışamaz.
- `profile_digest` vektör satırıyla birlikte saklanır; uyuşmazlık trigger ile
  reddedilir — sessiz profil karışması retrieval'i bozar ve fark edilmesi zordur.
- Bir chunk aynı profilde yalnız bir vektör taşır.

## Kanallar

| Kanal | Uygulama | Amaç |
|---|---|---|
| `exact` | `body like any(...)` + trigram indeksi | Teknik kimlik (`ZEKAM-P12-T04`, `app.musteri`, `#4711`) |
| `lexical` | PostgreSQL FTS, `simple` sözlüğü | Kelime eşleşmesi |
| `dense` | pgvector HNSW, cosine | Anlamsal yakınlık |

`simple` sözlüğü bilinçli seçimdir: kök bulma teknik kimlikleri bozar.

## RRF füzyonu

**Ham dense ve lexical skorlar kalibrasyonsuz toplanmaz.** Dense bir cosine
mesafesi (0.01 iyi), lexical bir `ts_rank` (12.0 iyi) döndürür; bunları toplamak
anlamsızdır. Reciprocal Rank Fusion yalnız **sırayı** kullanır:

```text
score(d) = Σ 1 / (k + rank_kanal(d)),  k = 60
```

- İki kanalda birden görünen sonuç öne çıkar.
- **Exact eşleşme her zaman en üsttedir**; düşük dense skorla elenemez.
- Aynı kanalda tekrar eden sıra reddedilir; sıralama deterministiktir.

## Reranker, dedupe, genişletme

- Reranker isteğe bağlıdır. Sağlayıcı hata verirse veya **sonuç düşürürse**
  güvenilmez sayılır ve fusion sırasına geri dönülür — sonuç kaybolmaz.
- Aynı içerik digest'i iki kez bağlama girmez.
- Çocuk chunk seçildiğinde ebeveyni de bağlama alınır, tekrar üretilmez.

## Citation ve abstain

Her alıntı chunk, doküman, locator ve içerik digest'i taşır; locator'sız citation
kabul edilmez.

| Durum | Koşul |
|---|---|
| `answered` | en az bir doğrulanmış alıntı var |
| `abstained-no-hit` | hiçbir kanal sonuç vermedi |
| `abstained-low-evidence` | sonuç var ama token bütçesine sığmadı |

Kanıtsız cevap üretilemez; abstain eden cevap citation taşıyamaz; bağlam token
bütçesini aşamaz. `grants_authority` her zaman `false`.

Her cevap **açıklama** taşır: hangi kanal kaç sonuç verdi, füzyon ve dedupe sonrası
kaç kaldı, reranker uygulandı mı yoksa geri mi dönüldü, bütçe nedeniyle ne dışarıda
kaldı.

## Değerlendirme

Golden küme üzerinde Recall@k, MRR ve nDCG@k hesaplanır. `improves_on` bir
iyileşmeyi ancak **hiçbir metrik gerilemeden** kabul eder — tek metriği şişirip
diğerini düşüren değişiklik iyileşme sayılmaz.
