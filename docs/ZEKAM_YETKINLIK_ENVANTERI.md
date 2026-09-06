# Zekam yetkinlik ve doğrulama envanteri

Bu belge ürün yüzeyinin dürüst kabul matrisidir. Kanonik, makinece okunabilir karşılığı
`zekam capabilities --json` çıktısıdır. `verified_by` alanı ilgili otomatik test konumunu
gösterir; tek başına o testin son çalıştırmada koştuğu anlamına gelmez. Son çalıştırma
kanıtları ayrıca paket doğrulaması ve kabul özetiyle raporlanır.

## Durum anlamları

| Durum | Anlamı |
|---|---|
| `ready` | Kullanıcı yüzeyi bağlı, ana güvenlik kapıları ve otomatik kabul kanıtı var |
| `partial` | Değer üreten parçalar var; fakat uçtan uca kullanıcı akışında açık bulunuyor |
| `scaffold` | Sözleşme veya depolama hazırlığı var; kullanılabilir ürün akışı yok |

## Güncel matris

| Yetkinlik | Durum | Bugün kullanılabilen yüzey | Kalan açık |
|---|---|---|---|
| Project RAG ve doğrulanmış citation | `ready` | `ask`, `project index/query/citation/status` | Büyük ölçek performans kampanyası bu kabul dışında |
| Intent + proje ailesi + Jira router | `ready` | `route families/preview/explain`, `jira resolve` | Agent dispatch hâlâ coordinator politikasınca yürütülür |
| OpenCode continuity ve otomatik resume | `ready` | `resume`, `opencode event/pre-compact/resume/install` | Semantik özetin kalitesi agent'ın checkpoint yazmasına bağlı |
| Backup ve recovery | `ready` | `backup create/verify/restore`, `local-runtime recover` | — |
| Operational Work Graph | `partial` | `project add/list`, `work create/list/resume` | Public transition/history/checkpoint komutları eksik |
| Markdown knowledge | `ready` | `knowledge scan/inspect/ingest/list/show/search/create/update/archive/restore/mutation-status`; global/project/work filtreleri | — |
| ODI 11g lineage | `partial` | `project odi-preflight/odi-bind`; digest-bound local-only export bağlantısı | Gerçek GPU/SKY exportuyla object-aware sanitizer ve exact lineage graph doğrulanmadan embedding kapalı |
| Project Researcher | `ready` | Digest-bound `research run/status/report`; OpenCode researcher + bağımsız verifier | — |
| Fikir üretme ve geliştirme | `scaffold` | Intake türleri ve storage kökleri | generate/review/save/promote akışı eksik |
| Raporlar ve observatory | `partial` | `scheduler report/rebuild`, `ui serve` | Research rapor gövdesi show/refresh bağlı değil |
| Model benchmark | `partial` | `model benchmark/decide/health/portable-inspect`, `model campaign plan/run/status/report` | Windows-native pipeline kampanyası hazır; portable import, gerçek provider, baseline ve release gate hâlâ kapalı |
| Semantic memory | `partial` | İç domain/shadow mekanizmaları | inspect/search/review/promote/status kullanıcı yüzeyi eksik |
| Jira | `partial` | Deterministik `jira resolve` | Issue fetch + kanıt kalıcılığı OpenCode MCP yoluna bağlı |

## “Nerede kaldık?” sözleşmesi

`zekam resume --json` aşağıdaki kaynakları provider çağrısı yapmadan birleştirir:

- son semantik OpenCode checkpoint'i (`completed`, `pending`, `next_safe_action`),
- açık, bloklu ve yakın zamanda tamamlanan Work Graph kayıtları,
- kayıtlı proje alias'ları ve hafif RAG indeks sağlık bilgisi,
- yetkinliklerin `ready/partial/scaffold` özeti,
- lifecycle oturum, kesinti ve hata sayıları.

Yeni OpenCode oturumunda managed plugin `zekam resume --prompt --session <id>` çağırır ve
çıktıyı yalnız bir kez sistem bağlamına ekler. Compaction öncesinde aynı paket compaction
context'ine yazılır. Paket 16 KiB ile sınırlıdır, prompt/response içeriğini lifecycle
ledger'a kaydetmez ve asla mutation/approval yetkisi vermez.

Semantik checkpoint yoksa sistem “tamamlandı” iddiası üretmez. Bu davranış özellikle yeni
bir model veya yeni bir OpenCode oturumu başladığında, sohbet belleği yerine yerel kanonik
kanıta dayanmayı sağlar.

## Kabul sınırı

Bir özelliğin kodu, agent prompt'u veya testi bulunması o özelliği otomatik olarak `ready`
yapmaz. Canlı provider, Jira veya büyük benchmark kampanyaları ayrıca açık ve sınırlı bir
kabul çalıştırması ister. 250 bin kayıt performansı bu aşamada bilinçli olarak kapsam dışıdır.

## Portable AI Model Benchmark köprüsü

Kullanıcının yazdığı bağımsız `AI-Model-Benchmark` paketi ikinci bir authority veya kopya
ürün olarak vendörlenmez. Zekam aşağıdaki komutla onun config/dataset yüzeyini foreign Python
kodu çalıştırmadan, provider çağırmadan ve secret-benzeri içeriği fail-closed reddederek
inceler:

```powershell
zekam model portable-inspect --root "C:\Users\mkaracan\Desktop\AI-Model-Benchmark" --json
```

Köprü; model/endpoint/capability sayıları, görev sayısı, opt-in ve yüksek maliyetli
capability'ler, concurrency/timeout/retry/request/cost bütçeleri, immutable-run politikası,
suite ve release-gate sayılarını digest bağlı bir belgeye dönüştürür. Bu paketten Zekam'a
alınan tasarım ilkeleri şunlardır:

- tek toplam skor yerine model × capability profili,
- `not_supported`, `not_configured`, `failed` ve `error` ayrımı,
- otomatik grader, model judge ve insan review kanıtlarının ayrılığı,
- embedding/reranker/guard/audio gibi endpoint türlerinin chat endpointine zorlanmaması,
- pahalı veya multimodal testlerin açık opt-in olması,
- request/cost/timeout/retry/concurrency bütçeleri ve immutable run çıktıları.

Bu inspection gerçek bir Zekam benchmark campaign'i çalıştırmaz ve authority üretmez. Zekam'ın
Windows-native, provider-free kabul kampanyası aşağıdaki dört yüzeyle çalışır:

```powershell
zekam model campaign plan --json
zekam model campaign run --plan-digest <PLAN_DIGEST> --uygula --json
zekam model campaign status --json
zekam model campaign report --json
```

Bu kampanya bir Zekam-owned deterministik mock ile bağımsız contract verifier kullanır. Gerçek
SQLite `plan → claim → receipt → artifact → trial → aggregate` zincirini sınar; provider veya
yabancı benchmark süreci çağırmaz ve üretim modeli kalifikasyonu sayılmaz. Aynı exact plan'ın
yeniden çalıştırılması yeni claim/receipt üretmez. Portable katalog importu, gerçek provider
kampanyası, baseline/regression ve release gate'ler `partial` kapsamının açık işleridir.
