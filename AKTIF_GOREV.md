# Zekam Aktif Görev Projeksiyonu

> Bu dosya kanonik PostgreSQL Work Graph'tan deterministik olarak üretilen, salt okunur bir projeksiyondur. Yetki vermez.

## Aktif iş

| Alan | Değer |
|---|---|
| Proje | `zekam` (`01a028b0-8ed6-752a-a09c-8e7ffd47fbe3`) |
| Work | `01a04c28-1829-722b-adae-64e0acb2d357` — Zekam Ölçümlü Loop ve Graph Yürütme Düzlemi |
| Durum | `completed`; revision `13` |
| Work digest | `sha256:89b1459dbdda2607704a057a0c7daf62bb98559a8c9014d469ae89bc7acf4fa5` |
| Plan | rev `6` / `01a04cd2-a9fc-7650-828b-b856b4f13760` |
| Run | `completed` / `01a04cd2-a9fc-7b33-838a-01272ac43d95` |
| Source HEAD | `ff5078d357997f99f6eff6c45d7cd2e4c066a8b7` |
| Source tree | `sha256:8f3576635c34c30330fa772fe97a5afae0ed2c66704221638f1957da314a77f6` |
| Memory | migration `78`, mode `enforced`, hooks current |
| Kabul | `32/32` doğrulandı |
| Yetki | `false`; approval devralınmadı |

## Plan adımları

| Adım | Açıklama | Etki |
|---|---|---|
| `client-lifecycle-bootstrap` | Claim sonrasinda lifecycle child isini materialize et | `database-write` |
| `client-lifecycle-drain` | Pending Codex lifecycle deliverysini isle | `database-write` |
| `projection-aware-close` | Verified Work ve staged pre-close zincirini atomik kapat | `database-write` |

## Kabul kriterleri

- [x] Önceki Memory Learning/Obsidian implementation kanonik DB ve root projection ile uzlaştırıldı.
- [x] Yeni aktif görev kanonik Work/Plan/Run olarak kaydedildi.
- [x] Generic objective/metric/evidence/progress çekirdeği oluşturuldu.
- [x] `learning.evaluate_loop`, runtime LoopPolicy ve model/context experiment aynı çekirdeği kullanıyor.
- [x] Runtime loop v1 backward compatibility korunuyor.
- [x] Loop validation v2 directional metric vector taşıyor.
- [x] Hard guard no-regression enforced.
- [x] Producer self-report progress sayılmıyor.
- [x] Attempt 2+ bounded progress packet olmadan başlayamıyor.
- [x] Full history/transcript context’e yığılmıyor.
- [x] Prompt rephrase aynı hypothesis/patch/failure tekrarını aşamıyor.
- [x] No-op, plateau ve oscillation stop kapıları geçiyor.
- [x] Validator asset manifest immutable ve builder write scope dışında.
- [x] Durable worker bir job = bir attempt kuralıyla çalışıyor.
- [x] Next-attempt enqueue idempotent ve crash-safe.
- [x] Direct/single/tournament/loop/graph/human topology planner mevcut.
- [x] Tek artifact için gereksiz graph reddediliyor.
- [x] Geri alınamaz effect queue-human-review’a yönleniyor.
- [x] Graph gerçek critical path/overlap/coordination evidence üretiyor.
- [x] Fake parallelism raporlanamıyor.
- [x] Tournament candidate isolation ve independent selector geçiyor.
- [x] İyileşmeyen attempt yalnız loop-owned patch’i geri alıyor.
- [x] Scaffolding ablation paired evidence üretiyor.
- [x] Observatory/Obsidian metric ve stop reason görünümü üretiyor.
- [x] Projection mevcut immutable generation store ve sabit `GUNCEL_BELLEK` yolunu kullanıyor.
- [x] Otomatik stable-vault publish varsa exact projection receipt/idempotency ile bağlı.
- [x] Secret/PII/raw transcript hiçbir projection/evidence’e sızmıyor.
- [x] Fresh DB, upgrade DB, full local quality ve security testleri geçiyor.
- [x] Bağımsız verifier builder’dan farklı model ve execution identity ile onaylıyor.
- [x] CI manuel `workflow_dispatch` olarak kaldı; otomatik tetikleyici eklenmedi.
- [x] Kullanıcı açıkça istemedikçe commit/push/PR oluşturulmadı.
- [x] `zekam close` güncel projection/receipt ile işi güvenli kapattı.

## Süreklilik ve güvenlik

- Projection receipt: `sha256:24b3b7653327da051e99a32c2472ba149528ef2e42ad08cb1359c38d6fc25814`
- Hook set: `sha256:27eea720344cec93a253db745ee13d078d44a7ab1fe6a220cacaea7563acc995`
- Açık receipt'siz claim: `0`
- Pending/recovery job: `0/0`
- Bloklu runtime kaydı: `1`
- Eski Global DoD çalışması korunmuştur; yeniden uygulanmamıştır.
- `GLOBAL_DOD_DURUM.md`: `sha256:2c598eb82ec3ce59d01e39878803d65e7b8d73ddb7b28b703e54a1f84d172ba7`
- `SURUM_RAPORU.md`: `sha256:a78af42dc844464ae30840a82f55d0ea4c5a2c358115c5c7d718249d1d2a4b57`

## Sonraki güvenli adım

Yok; Work terminal completed durumunda.

Projection digest: `sha256:41cb715da8720e4a3ead065c701d3c94bc61d6dc72a6236a443388e1a00783c3`
