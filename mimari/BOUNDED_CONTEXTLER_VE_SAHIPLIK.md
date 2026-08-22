# Bounded Context'ler ve Sahiplik

## 1. Project Registry

Sahibi:
- project identity, slug, alias, source binding, capability profile, integration state.

Sahip olmadığı:
- Work durumu, model score, source content, runtime lease.

## 2. Work Management

Sahibi:
- Work Item, revision/event, relation, Intent, Decision, Plan, acceptance evidence.

Sahip olmadığı:
- queue attempt, vector similarity, provider session.

## 3. Governance

Sahibi:
- capability, policy, authorization, approval, disclosure ve SecretRef metadata.

Sahip olmadığı:
- secret value, model assignment sonucu, tool implementation.

## 4. Execution Runtime

Sahibi:
- Task DAG, job, attempt, lease, fence, lock, checkpoint, effect claim/receipt, result envelope.

Sahip olmadığı:
- Work Item lifecycle gerçeği; completion Work context'i tarafından evidence ile yapılır.

## 5. Model Management

Sahibi:
- inventory, health, benchmark, quarantine, quota observation, price, model assignment.

Sahip olmadığı:
- provider authorization veya Work state.

## 6. Research

Sahibi:
- ResearchQuestion, source snapshot, evidence claim, contradiction ve report.

Sahip olmadığı:
- doğrulanmış knowledge, policy veya mutation yetkisi.

## 7. Knowledge

Sahibi:
- immutable source/artifact, normalized content, chunk, index, retrieval ve citation.

Sahip olmadığı:
- active Work status, memory promotion, secret.

## 8. Memory & Learning

Sahibi:
- memory candidate/revision/use/relation/hygiene, learning candidate ve skill evidence.

Sahip olmadığı:
- Work, authorization, model output'un ham gerçeği.

## 9. Operations

Sahibi:
- scheduler definition/run, report, backup/restore evidence, incident, telemetry projection.

Sahip olmadığı:
- domain state.

## Yasak çift sahiplik

Aşağıdakiler ikinci bir source of truth oluşturamaz:

- SQLite queue ve PostgreSQL queue aynı anda authority
- Markdown Work index ve Work JSON/DB
- Mem0 ve native memory birbirinden bağımsız truth
- Context Vault ayrı Work sistemi
- Dashboard mutable Work state
- Client config ayrı model inventory
- Agent transcript ayrı continuity store

Adapter ve projection verisi her zaman kanonik kayda referans verir.
