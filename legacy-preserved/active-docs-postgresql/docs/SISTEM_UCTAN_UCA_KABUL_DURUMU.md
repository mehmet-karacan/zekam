# Zekam Uctan Uca Kabul Durumu

Tarih: 2026-08-23

Bu belge bir tamamlanma iddiasi degildir. Yalniz bu source revision icin calistirilmis
testleri, gercek entegrasyon kanitlarini ve acik kapilari gosterir.

## Kanitlanan akislar

| Alan | Durum | Kanit |
|---|---|---|
| Paket/manifest/83 kriter yapisi | Gecti | `scripts/paket_dogrula.py` |
| PostgreSQL continuity + recovery sozlesmeleri | Gecti | 54 gercek PostgreSQL test |
| Doctor runtime kontrolleri | Gecti | 13 gercek PostgreSQL test |
| OpenCode Jira resolver -> Jira MCP -> issue | Gecti | `GPU 5661 -> SKYRSM-5661`, gercek read-only MCP cagrisi |
| Jira MCP cikti minimizasyonu | Gecti | get/search yalniz gerekli alanlari dondurur |
| OpenCode interruption projection precedence | Gecti | error/delete, pending/delete ve checkpoint/delete testleri |

## Acik veya kismi kanitli alanlar

| Alan | Durum | Acik |
|---|---|---|
| Kanonik cross-client resume | Kismi | OpenCode local lifecycle eventleri PostgreSQL handoff bundle'a production'da bagli degil |
| Runtime recovery backlog | Basarisiz | Doctor 44 uzlastirilmamis recovery kaydi gosteriyor; otomatik kapatilamaz |
| Model benchmark | Tasarim acigi | 17 aktif target icin 1 health + ayni fixture'in 5 tekrari = 102 provider cagrisi |
| Model yetenek derinligi | Yetersiz | Mevcut remote fixture uzun is, hata toparlama ve proje yetenegini olcmuyor |
| API/SSE/browser | Kismi | Kod ve unit test var; otomatik gercek tarayici receipt'i yok |
| Scheduler/worker tam zincir | Kismi | Sozlesme testleri var; production tick->claim->receipt kaniti yok |
| Commit/push engeli | Kismi | OpenCode glob deny vardir; credential/remote seviyesinde capability boundary degildir |

## Benchmark cagri politikasi karari

Mevcut tam qualification kampanyasi kullaniciya gosterilmeden calistirilmaz. Bugunku formul:

```text
17 aktif canonical target x (1 health + 5 ayni benchmark tekrari) = 102 provider cagrisi
```

Hedef mimari iki katmanlidir:

1. `smoke`: uretken model basina tek surumlu Markdown taskpack ve tek cevap; yalniz hizli
   tani/aday filtreleme, routing qualification sayilmaz.
2. `qualification`: smoke gecen adaylarda ek tekrarlar; embedding/rerank/guardrail/vision
   kendi typed fixture/API sozlesmesini kullanir. Fresh health kaniti varsa ayri health
   cagrisi yapilmaz.

Tek yanit bir modelin uzun sureli kararliligini istatistiksel olarak kanitlayamaz. Bu nedenle
tek-cagri sonucu `qualified` yerine `smoke-passed` olarak saklanmalidir.

## Tam kabul icin kalan kapilar

1. OpenCode/Codex/Claude eventlerini kanonik checkpoint/snapshot/handoff zincirine bagla.
2. Recovery backlog'unu adapter evidence ile tek tek uzlastir; sessiz retry veya toplu silme yapma.
3. Surumlu composite Markdown taskpack + smoke runtime + deterministik evaluator uygula.
4. Qualification cagri butcesindeki sabit `102/85/54` degerlerini suite'ten turetilen butceye al.
5. API/SSE/UI icin otomatik browser regression receipt'i ekle.
6. Commit/push engelini komut metni deseninden command broker/credential sinirina tası.
