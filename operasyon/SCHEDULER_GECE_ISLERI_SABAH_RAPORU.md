# Scheduler, Gece İşleri ve Sabah Raporu

## Scheduler ilkeleri

- Tanımlar ve run'lar PostgreSQL'de durable.
- Cron yalnız tetikleyici olabilir; gerçek job idempotency/queue'da.
- Sohbet/CLI process'inden bağımsız.
- Job timezone explicit (`Europe/Istanbul` varsayılan kullanıcı policy'si).
- Misfire, overlap, pause/resume, retry ve recovery policy sürümlü.
- Aynı schedule tick duplicate effect üretmez.
- Network/model job provider/quota/authorization gate kullanır.

## Zorunlu job türleri

| Job | Varsayılan davranış |
|---|---|
| incoming-document-scan | Yeni/stabil dosya → intake job |
| project-incremental-scan | Source revision/hash farkı |
| model-health | Sentetik probe |
| model-benchmark-staleness | Due suite'leri planla |
| quota-observation | Client'tan güvenilir veri varsa |
| memory-hygiene | Read-only finding |
| skill-candidate-review | Due evaluation/report |
| recovery-scan | Claim/no receipt ve stale lease |
| index-health | Current/stale/corrupt |
| research-night | Explicit user scheduled research |
| academic-compare | Inbox manifest'ine göre |
| report-daily | Genel/proje raporu |
| backup | Policy |
| restore-drill | İzole periyodik test |
| retention-review | Deletion değil review |

## Gece çalışma paketi

Kullanıcı akşam:

```text
Bu gece Oracle'dan PostgreSQL'e gecis risklerini arastir,
GPU projesiyle karsilastir, sabah karar raporu hazirla.
```

dediğinde:

1. Work+Intent+ResearchQuestion
2. source/project scope
3. budget/quota fallback
4. research DAG
5. schedule definition/run
6. provider disclosure/authorization
7. checkpoint/recovery
8. morning report

oluşur. Mutation varsayılan yoktur; “uygula” açık ise dahi research sonrası ayrı exact plan
ve approval gerekir.

## Sabah raporu şablonu

```text
Tarih ve as_of
Sistem sağlık özeti
Bugünkü hazır/aktif/bloklu/recovery işler
Dün tamamlanan işler ve kanıt
Gece araştırmaları
  soru/kapsam
  kullanılan kaynaklar
  agent/subagent/model/client
  findings/contradictions/unknowns
  recommended decisions/experiments
Model raporu
  inventory/health/quarantine
  benchmark/route değişiklikleri
  quota/cost/latency
Knowledge/index raporu
Memory/learning/skill adayları
Security/policy/outbound olayları
Backup/restore
Bugün önerilen exact next actions
```

Rapor her iddiayı Work/Run/Receipt/Evidence/Metric logical ref ile bağlar.

## Failure

Job failure terminal event ve category/digest üretir. Retry:
- read-only transient policy,
- non-read claim/no receipt recovery-required,
- max attempts blocked,
- sabah raporunda görünür.

## Approval

Scheduler definition user-data mutation olabilir. Schedule'ın gelecekteki provider/mutation
çağrıları:
- session veya long-lived scoped policy,
- operation/data/provider limits,
- budget/expiry,
- revoke

ile yönetilir. Generic sınırsız gece yetkisi yoktur.
