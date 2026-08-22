# Gerçek Agent Harness Sözleşmesi

## Tanım

Zekam Agent Harness; bir model çağrısını değil, hedefin kanonik Work kaydından terminal,
kanıtlı ve devam ettirilebilir sonuca ulaşmasını yöneten durable execution katmanıdır.

## Bileşenler

### 1. Request Intake
Doğal dil veya typed operation'ı güvenli request record'a çevirir. Secret pattern, control
character, aşırı boyut ve belirsiz mutation fail-closed'dur.

### 2. Resolver
Realm, project alias, Work Item, source binding, current revision, policy ve client
capability'yi authoritative store'dan çözer.

### 3. Context Compiler
Zorunlu exact kayıtları ve ek retrieval/memory adaylarını token bütçesinde deterministik
seçer. Manifest selected/omitted item, revision, digest, token ve selection reason taşır.

### 4. Work Classifier
İşi read-only/mutation/network/database/research/implementation/evaluation vb. sınıflandırır.
Risk, data classification ve acceptance shape üretir.

### 5. Route Planner
`direct-read`, `single-worker`, `sequential-dag`, `parallel-dag`, `review-only`, `blocked`,
`recovery-required` kararlarından birini verir. Resource conflict paralel route'u engeller.

### 6. Governance Gate
Capability, policy, source freshness, sandbox, verifier, secret, outbound disclosure ve
authorization koşullarını uygular. Model output hard gate'i gevşetemez.

### 7. Model Decision
Health/benchmark/quota/cost/latency evidence'ından açıklanabilir assignment üretir; provider
çağırmaz ve authority vermez.

### 8. Tool & Secret Gate
Typed capability registry'den tool seçer. SecretRef yalnız exact operation anında adapter'a
çözülür.

### 9. Task DAG
Step identity, dependency, logical read/write resources, role, acceptance, timeout, retry
policy, budget ve expected artifacts içerir.

### 10. Durable Queue
Job/attempt/lease/fence/lock/outbox/checkpoint/claim/receipt state'ini PostgreSQL'de tutar.

### 11. Execution Host
Claim-before-run, hard cancellation, process/container/worktree boundary, checkpoint ve
terminal receipt sağlar.

### 12. Result Normalizer
Client/model-specific output'u strict Agent Result Envelope'a dönüştürür. Free text, fenced
JSON veya unknown field authoritative değildir.

### 13. Verifier
Acceptance subject'lerini bağımsız kimlik ve read-only/controlled execute yetkisiyle
doğrular. Builder'ın kendi başarı iddiasını tekrar etmez.

### 14. Completion Coordinator
Tüm mandatory step, receipt ve verifier kanıtı varsa bound Work Item'ı completed revision'a
taşır. Başka Work Item'ı kapatamaz.

### 15. Observation & Learning
Sanitized runtime metric ve failure evidence'i model routing, memory hygiene ve learning
candidate süreçlerine gönderir.

## Harness request

Public request raw prompt taşımak zorunda değildir; en az:

```text
request_id
realm/project/work/plan/step identity
operation/workload
input references and digests
logical resources
data classification
budgets
client capabilities
model assignment reference
policy/authorization references
expected result schema
```

## Determinizm

Plan/decision/schema kayıtları canonical JSON, lexical key ordering ve SHA-256 ile
bağlanır. Timestamp karar girişiyse explicit `as_of`; implicit system clock kararın gizli
girdisi olamaz.

## Idempotency

Aynı semantic request + current state digest aynı idempotency identity'yi üretir. Duplicate:
- mevcut plan/run/result'i döndürür,
- yeni model call/effect oluşturmaz,
- farklı payload ile aynı key gelirse conflict üretir.

## Failure sınıfları

```text
invalid-input
choice-required
capability-missing
policy-blocked
authorization-missing
source-stale
resource-conflict
quota-unavailable
model-unhealthy
adapter-error
timeout
cancelled
verification-failed
effect-uncertain
recovery-required
budget-exhausted
internal-invariant
```

Exception text public kayda yazılmaz; category + sanitized digest + local secure diagnostic
reference kullanılır.

## Test zorunlulukları

- plan determinism/property tests
- duplicate idempotency
- two-worker claim race
- stale fence publish rejection
- lock parent/child conflict
- claim-without-receipt recovery
- cancellation and hard timeout
- malformed child output rejection
- builder/verifier identity separation
- cross-project/realm denial
- source drift
- authorization replay/expiry/revoke
- crash between claim and receipt
- continuity after context/client change
