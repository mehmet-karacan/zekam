# Model Envanter Mutabakatı

## Kaynak kararı

İki farklı fakat tamamlayıcı kaynak vardır:

1. `yerel-referanslar/model-envanterleri/AI_MODEL_ENVANTERI_NIHAI.md`: güncel yönetim görünümündeki **20 Model ID** kanonik
   inventory sayılır.
2. `yerel-referanslar/model-envanterleri/TT_AI_MODEL_ENVANTERI_FINAL_2026-08-20.md`: **19 doğrulanmış teknik servis profili**
   sağlar ve bir kaydın teknik ayrıntısının eksik olduğunu belirtir.

20. güncel kayıt:
- Model ID: `2d13d348-ab24-4738-a19c-6b4be323f836`
- access name: `openai/codepilot-qwen3`
- backend: `openai/Qwen/Qwen3-32B-AWQ`
- teknik endpoint/parametre ayrıntısı mevcut kaynaklarda doğrulanmamıştır.

Bu fark hata gizlenerek birleştirilmez. Zekam:
- 20 inventory record import eder,
- 19 kayda technical profile provenance bağlar,
- eksik kaydı `technical-profile-missing` limitation ile health planına alır,
- isimden capability uydurmaz.

## Model ID birincil kimliktir

Aynı access/backend:
- farklı protocol,
- farklı cost,
- farklı team/access,
- farklı Model ID

ile yayınlanabilir. Kayıtlar birleştirilmez. Reranker iki Model ID, MiniMax iki protokol
olarak ayrı kalır.

## Hassas alanlar

Ham kaynaklarda iç endpoint ve altyapı UUID'leri olabilir. Aktif canonical inventory yalnız
`endpoint_ref` ve `credential_ref` taşır. Ham dosyalar `yerel-referanslar/` altında Git-ignore
edilmiştir ve otomatik model context'ine yüklenmez.

## Bilinmeyen alan

`null`, boş veya kaynakta olmayan:
- context limit,
- output limit,
- tool/vision/reasoning desteği,
- cost,
- timeout

tahmin edilmez. Health/contract/benchmark ile ölçülür ve evidence record'a yazılır.

## Import acceptance

- exact 20 unique Model ID
- duplicate access/backend izinli
- duplicate Model ID yasak
- endpoint value canonical dosyada yok
- credential value yok
- source digest/provenance mevcut
- inventory import authority veya provider izni vermez
