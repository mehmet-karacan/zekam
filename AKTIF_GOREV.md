# Zekam Aktif Görev Projeksiyonu

> Bu dosya kanonik PostgreSQL Work Graph'tan deterministik olarak üretilen, salt okunur bir projeksiyondur. Yetki vermez.

## Aktif iş

| Alan | Değer |
|---|---|
| Proje | `zekam` (`01a028b0-8ed6-752a-a09c-8e7ffd47fbe3`) |
| Work | `01a04fbf-6cd0-73ab-9279-95b7b2bea8ad` — Zekam Canlı Yürütme Gözleme Merkezi |
| Durum | `completed`; revision `13` |
| Work digest | `sha256:48c48cc19b47fc9da1017635ae186a5008e1bd801dc4194da49056423755ee6e` |
| Plan | rev `8` / `01a0504c-a1ce-7f81-89bf-d1f0e30ea36a` |
| Run | `completed` / `01a0504c-a1ce-7704-bfbf-1c9e41689b4d` |
| Source HEAD | `876b06871d30cabbc4957b7b6845232d7a4f1b5c` |
| Source tree | `sha256:07bfab2cdcaa605c6eef76a0835ff049da597b82df8437691ceac3e49689edb2` |
| Memory | migration `78`, mode `enforced`, hooks current |
| Kabul | `45/45` doğrulandı |
| Yetki | `false`; approval devralınmadı |

## Plan adımları

| Adım | Açıklama | Etki |
|---|---|---|
| `client-lifecycle-bootstrap` | Claim sonrasinda lifecycle child isini materialize et | `database-write` |
| `client-lifecycle-drain` | Pending Codex lifecycle deliverysini isle | `database-write` |
| `projection-aware-close` | Verified Work ve staged pre-close zincirini atomik kapat | `database-write` |

## Kabul kriterleri

- [x] Açık CLI sayısı fake process fixture’daki gerçek root sayısıyla birebir eşleşir.
- [x] Node/python/shell child process’leri ayrı CLI olarak sayılmaz.
- [x] `zekam ui serve` kendi kendisini CLI saymaz.
- [x] Session dosyası yeni olsa bile OS process yoksa `Açık CLI` sayılmaz.
- [x] Process açık fakat session eşleşmemişse `unbound process` olarak görünür.
- [x] PID reuse `(pid, create_time)` ile ayrılır.
- [x] Scan sırasında kapanan process UI/API’yi düşürmez.
- [x] AccessDenied sahte inactive/active sonucu üretmez.
- [x] OpenCode, Codex ve Claude aynı normalize session sözleşmesine dönüşür.
- [x] Exact ve heuristic bağlar veri ve görsel düzeyde ayrıdır.
- [x] Heuristic bağ canonical ownership veya başarı iddiası vermez.
- [x] Parent/child session ve subagent ilişkisi mümkün olduğunda görünürdür.
- [x] Safe current_action yalnız enum/event metadata’dan türetilir.
- [x] Prompt/terminal içeriğinden görev özeti üretilmez.
- [x] Work → Job → Attempt → Agent → Lease → Claim → Receipt zinciri exact ID’lerle gösterilir.
- [x] Terminal receipt olmadan UI `completed/success` göstermez.
- [x] Expired lease, receipt’siz claim ve recovery-required durumları görünürdür.
- [x] Realm verilmezse DB realm tahmini yapılmaz.
- [x] Local process observation canonical runtime’dan bağımsız availability taşır.
- [x] Kullanıcıya görünen ana ad tam olarak `Zekam Canlı Yürütme Gözleme Merkezi`dir.
- [x] Ana ekran repository/commit dashboard’u değildir.
- [x] Üst şerit altı temel canlı metriği gösterir.
- [x] Ana graph her gerçek session/CLI için stabil cluster üretir.
- [x] Snapshot güncellemelerinde node’lar gereksiz yere yer değiştirmez.
- [x] Sağ ray her açık root CLI için tek kart gösterir.
- [x] Session Registry, Olay Akışı, Queue/Lease/Receipt ve Kaynak Kullanımı panelleri çalışır.
- [x] Search ve client/state/project filtreleri çalışır.
- [x] Canvas kullanılamazsa liste/tablo fallback ile temel bilgi kaybolmaz.
- [x] Reduced motion davranışı doğrulanır.
- [x] 1366×768 ve 1920×1080 ekranlarda taşma/üst üste binme yoktur.
- [x] API response’da raw cmdline yoktur.
- [x] API response’da environment veya absolute path yoktur.
- [x] Prompt, response, transcript ve tool body yoktur.
- [x] Secret/URL/path leakage negatif testleri geçer.
- [x] Mutation endpoint’i yoktur.
- [x] CSP, TrustedHost, no-store ve mevcut güvenlik header’ları korunur.
- [x] Process observation kapalıysa fail-closed ve açıklanabilir degrade mode çalışır.
- [x] Process scan bounded limitler içinde ölçülür ve ana thread’i bloklamaz.
- [x] 64 root + bounded child fixture’da snapshot süresi raporlanır.
- [x] 512 node / 1024 edge graph etkileşimi kullanılabilir kalır.
- [x] SSE reconnect duplicate process/session oluşturmaz.
- [x] Polling fallback canlı veriyi sürdürebilir.
- [x] Unit, integration, e2e, security, Ruff, mypy ve package validation geçer.
- [x] Gerçek viewport screenshot kanıtları repo dışı teslim alanında üretilir.
- [x] Bağımsız verifier process sayımı, content exclusion, receipt semantiği ve UI davranışını doğrular.

## Süreklilik ve güvenlik

- Projection receipt: `sha256:431712d7638c968952e4ac20a382573a76680c3743a49d3426a618b51c1a8186`
- Hook set: `sha256:27eea720344cec93a253db745ee13d078d44a7ab1fe6a220cacaea7563acc995`
- Açık receipt'siz claim: `0`
- Pending/recovery job: `0/0`
- Bloklu runtime kaydı: `0`
- Eski Global DoD çalışması korunmuştur; yeniden uygulanmamıştır.
- `GLOBAL_DOD_DURUM.md`: `sha256:2c598eb82ec3ce59d01e39878803d65e7b8d73ddb7b28b703e54a1f84d172ba7`
- `SURUM_RAPORU.md`: `sha256:a78af42dc844464ae30840a82f55d0ea4c5a2c358115c5c7d718249d1d2a4b57`

## Sonraki güvenli adım

Yok; Work terminal completed durumunda.

Projection digest: `sha256:6f762729c529d2accb940a0855c532e29d836017f4c979a943ee97efd3384271`
