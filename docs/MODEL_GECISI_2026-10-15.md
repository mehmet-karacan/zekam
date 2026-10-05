# Model gecisi: GLM-5.3-Flash-IT ve Qwen3.8-27B-IT (15 Ekim 2026)

Kaynak: BT GMY duyurusu (kullanici tarafindan iletildi). 15 Ekim 2026 itibariyla BT son
kullanicilari icin yalniz iki ana model sunulur; diger mevcut modeller kullanimdan kaldirilir.

| Duyuru adi | AI Hub katalogundaki gercek id | Duyurulan kullanim |
|---|---|---|
| GLM5.3-Flash | `GLM-5.3-Flash-IT` | kod gelistirme, cok adimli teknik gorev, gorsel yorumlama |
| Qwen3.8 27B | `Qwen3.8-27B-IT` | gunluk teknik calisma, icerik destegi, ekran goruntusu analizi |

Duyurudaki yazim (`GLM5.3-Flash-IT`, `Qwen3.8 27B-IT`) ile katalogdaki id'ler farklidir; kodda ve
yapilandirmada **katalog id'si** kullanilir (`GET /v1/models`, salt okunur, 5 Ekim 2026 dogrulandi).
Her iki model metin ve gorsel girdi destekler (duyuru).

## Yapilan

- **Duman testi (kalifikasyon degil):** her modele tek, kucuk bir sohbet istegi gitti; ikisi de
  yanit verdi. GLM-5.3-Flash-IT bir muhakeme modelidir (64 tokenlik limitte yalniz muhakeme
  tokeni uretti, `finish_reason=length`); kisa cevaplar icin `max_tokens` yuksek tutulmali.
- **Kullanici OpenCode yapilandirmasi** (`~/.config/opencode/opencode.json`, yedek
  `~/.zekam/quarantine/manual/`): `model = litellm/GLM-5.3-Flash-IT`,
  `small_model = litellm/Qwen3.8-27B-IT`; iki model `provider.litellm.models` altina yalniz `name` ile eklendi. `modalities` alani
  eklenmedi: Zekam'in strict OpenCode model parser'i (`opencode_embedding.py`) bilinmeyen alani
  reddeder ve `zekam model campaign` bozulur (ilk denemede bozuldu, geri alindi). Gorsel girdi
  bildirimi icin parser'in `modalities`i kabul etmesi gerekir (acik is). Baglam/cikti limiti **eklenmedi** (kaynakta yok, uydurulmadi). Eski model
  girdileri kaldirilmadi; kaldirma 15 Ekim sonrasi kullanici kararidir. `opencode debug config`
  yeni degerleri cozdu.

## Yapilmayan / karar bekleyen

- **Kanonik model envanteri** (`modeller/KANONIK_MODEL_ENVANTERI.yaml`, 20 UUID'li model),
  **provider binding'leri** (`config/model_provider_bindings.yaml`; chat=gemma-4, code=
  `codepilot-qwen3-next`) ve **benchmark kapsami** (`config/opencode_benchmark_scope.yaml`)
  eski modellere bagli. Yeni modellerin UUID/endpoint/credential kimlikleri hub katalogunda yok;
  kimlik uydurulmadi. Guncel TT envanter ciktisi (UUID'li) gelince envanter, binding, kapsam
  ve paket dogrulayici sayilari (20 model / 19 teknik profil) birlikte guncellenmelidir.
  15 Ekim'den sonra `chat`/`code` binding'leri ve eski modelleri hedefleyen benchmark kampanyasi
  calismaz.
- **Embedding/reranker:** Zekam `openai/BAAI/bge-m3` ve `BAAI/bge-reranker-v2-m3` kullanir.
  Duyuru bunlarin akibetini soylemiyor; "diger mevcut modeller" kaldirilirsa RAG dense kanali
  etkilenir. Ayri teyit gerekir.
- **Model kampanyasi/benchmark:** yeni modeller icin `zekam model campaign plan --json` dry-run'i
  ve ayri acik onay olmadan canli kampanya calistirilmaz.
