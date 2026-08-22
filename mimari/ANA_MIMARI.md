# Zekam Ana Mimarisi

## Mimari hedef

Zekam, modellerin gelip geçici; state, policy, evidence ve execution sözleşmelerinin kalıcı
olduğu local-first bir mühendislik kontrol düzlemidir.

```text
İstemciler
  CLI | API | Codex | Claude Code | OpenCode | MCP adapter | Dashboard
                               |
                     Application Service
                               |
  ┌─────────────┬─────────────┬─────────────┬─────────────┬─────────────┐
  │ Control     │ Execution   │ Knowledge   │ Memory &    │ Operations  │
  │ Plane       │ Plane       │ Plane       │ Learning    │ Plane       │
  └─────────────┴─────────────┴─────────────┴─────────────┴─────────────┘
                               |
            PostgreSQL 18 + pgvector + Object Storage
```

## Process modeli

Tek source repository ve tek domain modeli korunur; şu process'ler ayrı ölçeklenebilir:

- `zekam` CLI
- FastAPI veya eşdeğer API
- Scheduler
- Worker/execution host
- İsteğe bağlı web dashboard

Bunlar aynı application use-case'lerini kullanır. CLI ve API kendi ürün kurallarını yazmaz.

## Logical PostgreSQL schema'ları

| Schema | Sahiplik |
|---|---|
| `core` | realm, actor, canonical identity, policy revision |
| `projects` | project, alias, source binding, capability profile |
| `work` | Work Item, revision, event, relation, Intent, Decision, Plan |
| `runtime` | job, attempt, lease, lock, checkpoint, claim, receipt, outbox |
| `models` | inventory, health, benchmark, price, quota, assignment, observation |
| `research` | question, source snapshot, claim, contradiction, report |
| `knowledge` | source, artifact, normalized unit, chunk, embedding, FTS, citation |
| `memory` | memory revision, relation, use, hygiene, promotion |
| `skills` | candidate, evaluation, lifecycle, registry reference |
| `security` | SecretRef metadata, authorization, disclosure, audit |
| `ops` | scheduler, report, backup, incident, derived projection state |

Schema sınırı deployment zorunluluğu değildir; bounded context ownership sınırıdır.

## Authority matrisi

| Soru | Yetkili kaynak |
|---|---|
| İşin durumu nedir? | Work Graph |
| Hangi step çalışıyor? | Runtime Run/Step + lease |
| Effect yapıldı mı? | Effect Claim/Receipt |
| Model neden seçildi? | Model Assignment |
| Bilgi nereden geldi? | Knowledge source/citation |
| Önceden ne öğrendik? | Reviewed memory |
| Kullanıcı izin verdi mi? | Authorization ledger |
| Dashboard ne gösteriyor? | Derived projection; authority değil |

## Sadelik ilkesi

İlk üretim deployment'ı modüler monolittir. Queue, model, RAG veya memory için ayrı ürün
state'i oluşturulmaz. Redis yalnız wakeup/cache; object storage artifact; pgvector retrieval
işlevi görür. Yeni altyapı yalnız ölçülmüş ihtiyaçla eklenir.

## Portability

- Kimlikler physical path'ten bağımsızdır.
- Source binding export sırasında `unbound` olur.
- Active lease/lock/owner token export edilmez.
- Secret value export edilmez.
- Thin export canonical user data; ready export verified derived data ve tamamlanmış runtime
  history içerebilir.
