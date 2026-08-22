# Zekam Neuro Observatory — UI mimarisi ve uygulama planı

> Durum: **ilk read-only dilim uygulanmıştır**. Bu belge, mevcut dilimin sınırını ve
> sonraki entegrasyonları tarif eder; kanonik Work Graph üzerinde bir işi kendiliğinden
> `completed` yapmaz.
>
> Araştırma/uyumluluk tabanı: `main@9a6ab50a5c2faec226d892c2e97c94f26ecda3fd`.

## 1. Neden şimdi?

Zekam bugün yalnızca Markdown belgelerinden oluşan bir depo değildir. Repository içinde:

- kanonik Work Graph,
- durable queue, attempt, lease, fencing ve recovery,
- model envanteri, health ve benchmark,
- knowledge ingestion ve citation,
- native memory,
- scheduler ve günlük rapor,
- secret/authorization/outbound governance

katmanları bulunmaktadır. Buna rağmen mevcut kullanıcı yüzeyi ağırlıklı olarak CLI ve
Markdown projeksiyonudur. Sistem büyüdükçe “hangi model ne yapıyor?”, “hangi iş hangi
lease altında?”, “hangi belge hangi kararı besliyor?” sorularını yalnız komutlarla takip
etmek zorlaşır.

UI bu nedenle bir dekorasyon değil, **gözlem düzlemi** olmalıdır.

## 2. Ürün fikri: yaşayan beyin

Ana ekran gerçek bir beyin ifadesi veren, iki loblu bir sinaptik haritadır. Fakat görsel
metafor veri modelinin önüne geçmez:

- Her ışık bir gerçek düğümdür.
- Her sinaps türetilmiş bir ilişkidir.
- Her düğüm kanonik kayda drill-down referansı taşır.
- Aktif lease/worker sahipliği ayrı biçimde parlar.
- Bir istemci olayı, terminal receipt olmadan “başarı” gösteremez.
- Graph kaybolursa yeniden üretilebilir; state kaybı sayılmaz.

İlk Canvas uygulaması dış CDN veya JavaScript build zinciri istemez. Bu, Zekam'ın Python
paket yapısına düşük riskle eklenmesini sağlar. Veri sözleşmesi sabit kaldığı sürece ileride
Sigma.js/WebGL katmanına geçilebilir.

## 3. İlk dilimde uygulanan yüzey

```text
zekam ui serve [--realm-id UUID] [--home PATH]
```

Varsayılan bağlanma adresi `127.0.0.1:8765`'tir. İlk sürüm loopback dışındaki bir hostu
reddeder.

### HTTP uçları

| Uç | İşlev |
|---|---|
| `GET /` | Paket içindeki Neuro Observatory uygulaması |
| `GET /api/observatory/health` | Read-only ve realm kapsamı bilgisi |
| `GET /api/observatory/snapshot` | Tek, doğrulanabilir UI snapshot'ı |
| `GET /api/observatory/events` | SSE üzerinden değişen snapshot'lar ve heartbeat |

Mutation endpoint'i yoktur.

### Snapshot katmanları

1. **Repository graph**
   - En fazla 180 Markdown düğümü ve 360 ilişki.
   - Yalnız repository-relative yol, güvenli ilk başlık ve `.md` link hedefi.
   - Belge gövdesi API cevabına kopyalanmaz.
   - Son raporlar ayrı listelenir.

2. **Realm-scoped runtime projection**
   - `work.work_item`
   - `runtime.job`, `runtime.lease`, `runtime.execution_event`, `runtime.outbox_event`
   - `models.model_inventory`
   - `knowledge.source` ve `knowledge.source_version`
   - `memory.record` — `content` kolonu özellikle seçilmez
   - `ops.job_definition`

3. **Zorunlu dashboard kareleri**
   - work
   - run
   - model
   - knowledge
   - memory
   - scheduler

Realm belirtilmezse sistem güvenli biçimde belge grafı modunda çalışır; veritabanından
realm tahmini yapılmaz.

## 4. Veri gerçekliği ve authority

Gerçeklik sırası şöyledir:

```text
Work / Plan
    ↓
Job / Attempt
    ↓
Lease + fencing
    ↓
Effect claim
    ↓
Terminal receipt
```

UI, bu kayıtların projeksiyonudur. Aşağıdakiler authority değildir:

- Canvas üzerindeki bir edge,
- OpenCode SSE olayı,
- Codex app-server notification'ı,
- Claude hook veya stream olayı,
- Markdown'da işaretlenen bir checkbox,
- modelin “bitti” beyanı.

Bu nedenle snapshot sözleşmesi her zaman şunları taşır:

```json
{
  "read_only": true,
  "grants_authority": false,
  "graph": {
    "derived": true,
    "grants_authority": false
  }
}
```

## 5. Canlı istemci entegrasyonları

İlk dilimde Zekam'ın kendi runtime kayıtları ana kaynaktır. Sonraki dilimde üç istemci
adaptörü aynı normalize edilmiş gözlem sözleşmesine bağlanacaktır.

### 5.1 OpenCode

OpenCode'un yerel server yüzeyi SSE ile global event akışı sunar. Adaptör:

- yalnız loopback URL'sine bağlanır,
- event adını ve secret-free kimlikleri normalize eder,
- prompt/response içeriğini atar,
- Zekam `job_id`, `work_item_id`, `trace_id` eşleşmesi yoksa olayı `unbound-observation`
  olarak işaretler.

### 5.2 Codex

Codex `app-server`, JSON-RPC 2.0 tabanlı zengin istemci protokolüdür ve
`thread/*`, `turn/*`, `item/*` notification'ları üretir. Adaptör:

- JSONL/stdin-stdout akışını typed envelope'a çevirir,
- turn/item durumunu gösterir,
- raw item içeriğini observability event'ine taşımaz,
- Zekam receipt olmadan başarı üretmez.

### 5.3 Claude Code / Agent SDK

Claude tarafında iki güvenli kaynak vardır:

- `--output-format stream-json`,
- Agent SDK hook'ları: `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `PreCompact` vb.

Adaptör yalnız event metadata'sı, tool kategorisi, süre ve correlation kimliklerini alır.
Tool input/output gövdeleri varsayılan olarak dışarıda kalır.

### Normalize gözlem sözleşmesi

```text
schema                 zekam-client-observation/v1
client                 opencode | codex | claude
session_ref            secret-free logical ref
agent_ref              bounded label/ref
phase                   started | active | waiting | completed | failed
observed_at             timezone-aware timestamp
work_item_id?           exact UUID
job_id?                 exact UUID
attempt_id?             exact UUID
trace_id?               bounded correlation
content_included        false
canonical               false
```

## 6. Bilgi ağı ve Obsidian yaklaşımı

Zekam'ın Markdown raporları Obsidian benzeri bir graph ile görünür olur; fakat bu graph
kanonik Work Graph'ın yerine geçmez. İlk dilim; güvenli ilk başlığı, standart Markdown
linklerini ve `[[Obsidian wiki-link]]` biçimini bounded metadata olarak işler. Repository
projeksiyonu canlı runtime polling'inden ayrıdır ve 15 saniye cache edilir.

Sonraki iterasyonda:

- tam backlink görünümü ve kırık-link analizi,
- source → chunk → citation → decision zinciri,
- stale/fresh provenance işaretleri,
- memory `supports`, `contradicts`, `derived-from`, `supersedes` ilişkileri,
- rapordan exact kanıt kaydına geçiş

eklenecektir.

Vault benzeri gezinme için dosya sistemi “gerçeklik” sayılmaz. PostgreSQL kayıtları ve
content digest'ler doğrulanır; Markdown yalnız insan dostu projeksiyondur.

## 7. Güvenlik sınırı

İlk sürüm şu garantileri uygular:

- yalnız loopback bind,
- mutation route yok,
- realm UUID açık verilmeden DB projeksiyonu yok,
- PostgreSQL sorguları `zekam_app` ve RLS oturumu altında,
- owner token/credential/secret yok,
- runtime event payload okunmuyor,
- outbox payload okunmuyor,
- memory content okunmuyor,
- prompt ve model response yok,
- DB hata mesajı kullanıcıya taşınmıyor; yalnız hata sınıfı görünür,
- bütün listeler bounded,
- repository dışına çıkan Markdown/wiki linki reddediliyor,
- UI label alanları secret, URL ve kişisel absolute path desenlerine karşı sanitize ediliyor.

Remote erişim gerektiğinde “hostu açmak” yeterli olmayacaktır. Ayrı bir işte TLS,
authentication, CSRF/origin policy, reverse proxy ve authorization modeli tasarlanmalıdır.

## 8. Aşamalı teslim planı

### Dilim A — Neuro Observatory temel yüzeyi — uygulandı

- Canvas beyin/sinaps görselleştirmesi
- repository Markdown graph
- altı zorunlu dashboard karesi
- PostgreSQL runtime projection
- live agent/lease kartları
- execution/outbox event rail
- SSE + polling fallback
- kanonik referans drill-down görünümü
- read-only CLI server

### Dilim B — İstemci event bridge

- OpenCode SSE consumer
- Codex app-server notification bridge
- Claude Agent SDK hook bridge
- `zekam-client-observation/v1`
- event correlation ve unbound observation kuyruğu
- adapter health/lag/drop metrikleri

### Dilim C — Run ve kanıt inceleme

- Work → Plan → Job → Attempt → Claim → Receipt zaman çizgisi
- lease expiry ve recovery-required uyarıları
- lock/resource çatışma haritası
- verifier sonucu ve acceptance evidence
- token/cost/latency/quota trendleri

### Dilim D — Knowledge/Memory Studio

- source/version/ingestion aşaması
- chunk ve citation drill-down
- Obsidian tarzı backlink paneli
- memory candidate/review/supersede/contradiction ağı
- retrieval evaluation ve no-answer görünümü

### Dilim E — Kontrollü komuta düzlemi

Bu dilim ayrı authorization işi olmalıdır. Her mutation:

1. aynı application service'i çağırır,
2. önce exact plan gösterir,
3. açık uygulama eylemi ister,
4. authorization/approval kapısını geçer,
5. claim ve terminal receipt üretir.

Read-only observatory ile command plane aynı route grubunda karıştırılmamalıdır.

## 9. Kabul kapıları

- [ ] `zekam surface check --json` yeni `ui serve` komutunu eksiksiz görür.
- [ ] Belge modunda PostgreSQL olmadan UI açılır.
- [ ] Realm modunda RLS dışı satır görünmez.
- [ ] UI cevabında prompt, response, secret, owner token veya memory content yoktur.
- [ ] Her tile ve node kanonik drill-down referansı taşır.
- [ ] SSE bağlantısı kopunca polling fallback çalışır.
- [ ] `prefers-reduced-motion` hareketi azaltır.
- [ ] Wheel içinde üç static asset bulunur.
- [ ] 10.000 event olsa dahi response bounded kalır.
- [ ] Graph silinse bile canonical state kaybı oluşmaz.

## 10. Teknoloji kararları

| Alan | İlk karar | Sonraki ölçekleme |
|---|---|---|
| API | FastAPI | aynı |
| Canlı akış | SSE snapshot diff | SSE event delta + gerektiğinde WebSocket |
| Grafik | Canvas 2D, sıfır npm | Sigma.js/WebGL |
| State | PostgreSQL projection | materialized projection/cache yalnız ölçüm sonrası |
| Belgeler | bounded Markdown metadata | indexed backlink/citation projection |
| Telemetry | Zekam event/lease | OpenTelemetry export |
| UI yetkisi | read-only | ayrı approval-gated command plane |

## 11. Resmî teknik referanslar

- OpenCode Server ve global SSE event yüzeyi: <https://opencode.ai/docs/server/>
- OpenCode JavaScript SDK: <https://opencode.ai/docs/sdk/>
- OpenAI Codex app-server protokolü: <https://github.com/openai/codex/tree/main/codex-rs/app-server>
- Anthropic Claude Agent SDK hook'ları: <https://platform.claude.com/docs/en/agent-sdk/hooks>
- FastAPI `StreamingResponse`: <https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse>
- FastAPI WebSocket: <https://fastapi.tiangolo.com/advanced/websockets/>
- OpenTelemetry signals: <https://opentelemetry.io/docs/concepts/signals/>
- Sigma.js WebGL graph renderer: <https://www.sigmajs.org/>

Bu kaynaklar uygulama anında tekrar doğrulanmalıdır; istemci protokolleri Zekam'ın kanonik
sözleşmesi değildir ve sürüm pin'i/protocol capability negotiation gerektirir.
