# Benchmark Suite Kataloğu

General, workload/technology ve project katmanli model/subagent secim sozlesmesi
`modeller/KATMANLI_MODEL_ROUTING_PLANI.md` dosyasindadir. Current health ve
qualification olmadan routing yapilmaz.

## Suite aileleri

Bağlayıcı aile kümesi tam olarak şudur:

| Aile | Temel doğrulama |
|---|---|
| `sql-plsql` | SQL/PLSQL doğruluğu, transaction ve locking kanıtı |
| `code-repair` | Minimal patch, regression ve recovery |
| `code-review` | Root-cause, unsafe patch ve kanıt doğruluğu |
| `architecture` | Boundary, ownership, tradeoff ve failure mode |
| `rag-retrieval` | Retrieval, citation ve abstention |
| `tool-use` | Tool seçimi, argüman doğruluğu ve yan etki yasağı |
| `agentic-workflow` | Claim/receipt, replay ve recovery |
| `long-context` | Evidence recall, distractor ve continuity |
| `document-analysis` | Belge/OCR/diagram grounding |
| `structured-output` | Strict schema, unknown field ve malformed çıktı |
| `safety-policy` | Injection, secret, path ve network sınırları |
| `embedding-retrieval` | Semantic retrieval doğruluğu ve latency |
| `reranking` | Candidate sıra kalitesi ve determinism |
| `creative-tournament` | Ayrı yaratıcı değerlendirme ve human correction |

Kalıcı task manifestleri `benchmarks/suites/<aile>/task.yaml` altında, çözünür
prompt/fixture/hidden-key/grader kaynakları `benchmarks/resources/` altındadır.
`scripts/generate_benchmark_catalog.py --check` repo ve paket kopyalarının byte
eşitliğini doğrular. Teknik ve yaratıcı task'lar aynı aggregate ile zorla
karşılaştırılmaz.

## Case contract

```text
case_id/version
workload
fixture policy
remote eligibility
input artifact digests
required output schema
acceptance dimensions
quality/reliability/latency weights
timeout/token/cost
verifier requirements
case digest
```

Fixture policies:
- `synthetic-only`
- `sanitized-derived`
- `local-only`

Database/source-sensitive fixture varsayılan local-only'dir.

## Golden dataset yönetimi

- Git'te sentetik/sanitized fixture ve expected evidence.
- Gerçek kullanıcı source içeriği Git'e girmez.
- Project-specific expected refs logical path/symbol/digest kullanır.
- Suite/version değişikliği benchmark staleness üretir.
- Model sonucu golden cevabı değiştiremez.

## OpenCode / AIHub kampanyası

OpenCode yapılandırmasındaki AIHub modelleri için kampanya üç ayrı adımdır. `plan`
salt okunurdur; `authorize` Work-bound TaskPlan ile tek kullanımlık yetkileri üretir;
`run` bu yetkileri yalnız bir kez tüketir. Ses modeli kullanıcı kapsamı gereği çağrılmaz.

```powershell
zekam db upgrade --uygula
zekam model campaign plan --json
zekam model campaign authorize `
  --project-uuid <PROJECT_UUID> --work <WORK_UUID> --actor <ACTOR_UUID> `
  --revision 1 --uygula --json
zekam model campaign run `
  --campaign-id <CAMPAIGN_UUID> --project-uuid <PROJECT_UUID> `
  --work <WORK_UUID> --plan-id <TASK_PLAN_UUID> --revision 1 `
  --uygula --json
zekam model campaign status --campaign-id <CAMPAIGN_UUID> --json
zekam model resolve --workload retrieval --client opencode --json
```

Reviewed v1 bütçesi 17 health ve health-passed hedef başına 5 benchmark olmak
üzere en fazla 102 provider çağrısıdır. Her provider çağrısı ayrı `max_uses=1`
authorization, claim ve terminal receipt taşır. Başarısız dış etki sessizce tekrar
edilmez; kampanya `recovery-required` olur ve yeni exact plan/yetki ister. Sonuçlar
Yerel SQLite append-only campaign/member/result/outcome/qualification kayıtlarında,
aggregate metriklerinde ve runtime receipt/checkpoint zincirinde tutulur. Ham prompt,
yanıt, endpoint ve credential bu kayıtlara yazılmaz.
