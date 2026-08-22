# Benchmark Suite Kataloğu

General, workload/technology ve project katmanli model/subagent secim sozlesmesi
`modeller/KATMANLI_MODEL_ROUTING_PLANI.md` dosyasindadir. Current health ve
qualification olmadan routing yapilmaz.

## Suite aileleri

### GEN-TR-01 — Türkçe mühendislik
Terminoloji, uzun talep, doğal dil entity/alias, açıklama ve rapor.

### GEN-EVIDENCE-01 — Kanıtlı araştırma
Claim-evidence, contradiction, citation, abstention, stale source.

### CODE-NAV-01 — Repository anlama
Modül, symbol, call/dependency, config ve version farkı.

### CODE-PATCH-01 — Güvenli değişiklik
Exact allowlist, minimal patch, tests, rollback, no unrelated changes.

### CODE-VERIFY-01 — Bağımsız verification
Deliberate bug/unsafe patch yakalama ve acceptance coverage.

### ARCH-01 — Mimari
Boundary, ownership, tradeoff, migration ve failure mode.

### DB-MIG-01 — Oracle→PostgreSQL
Type/function/package/sequence/transaction/locking farkı; uygulanabilir dönüşüm planı.

### PROJECT-VERSION-01 — Teknoloji sürümü
Spring/Java/Node/Python versiyonuna uygun kod; yanlış sürüm API'si negatif fixture.

### STRUCTURED-01 — JSON schema/tool
Strict output, unknown field, malformed input, function/tool planning.

### SECURITY-01 — Prompt/secret/path/network
Untrusted instructions, secret exfiltration, traversal, over-broad tool.

### CONTEXT-01 — Uzun bağlam
Required evidence recall, distractors, compaction/handoff.

### EMBED-TR-01
Türkçe semantic retrieval.

### EMBED-CODE-01
Identifier/path/symbol retrieval.

### RERANK-01
Candidate reranking quality delta.

### ASR-TR-01
Türkçe temiz/gürültülü audio WER/CER.

### GUARD-01
Safe/unsafe/injection/secret classification.

### VL-01
Image/OCR/chart/diagram grounded tasks.

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
PostgreSQL'deki append-only campaign/member/result/outcome/qualification kayıtlarında,
aggregate metriklerinde ve runtime receipt/checkpoint zincirinde tutulur. Ham prompt,
yanıt, endpoint ve credential bu kayıtlara yazılmaz.
