# Orkestrasyon, DAG, Queue, Lease ve Fencing

## Task DAG sözleşmesi

Her step:

```text
step_id
plan_revision_id
kind
role
dependencies
read_resources
write_resources
required_capabilities
risk
data_classification
budget
timeout
retry_policy
acceptance_subjects
result_schema
```

Dependency graph acyclic olmalıdır. Ready step; bütün dependencies terminal-success ve
resource/admission gate açık olduğunda claim edilebilir.

## Paralellik

Parallel seçimi yalnız:

- en az iki ready ve anlamlı bağımsız step,
- write resource kesişimi yok,
- read/write conflict yok,
- worker/client capability var,
- token/cost/quota/time budget yeterli,
- data policy ayrı çağrılara izin veriyor

koşullarında yapılır. Sabit global maximum yoktur. Scheduler her run için:

```text
min(ready_independent_steps,
    available_worker_slots,
    quota_safe_slots,
    token_budget_slots,
    cost_budget_slots,
    provider_rate_slots,
    policy_concurrency_limit)
```

hesaplar. Sonuç 1 olabilir.

## PostgreSQL queue tabloları

Önerilen çekirdek:

```text
runtime.job
runtime.job_attempt
runtime.lease
runtime.resource_lock
runtime.outbox_event
runtime.checkpoint
runtime.effect_claim
runtime.effect_receipt
runtime.execution_event
```

Job mutable lifecycle head; attempt/event/claim/receipt append-only kanıttır.

## Claim transaction

1. `ready` ve `available_at <= now` job'ları priority/stable ID ile seç.
2. Worker capability seti requirement'ı kapsamalı.
3. `FOR UPDATE SKIP LOCKED` yalnız queue claim sorgusunda kullan.
4. Attempt oluştur, job `running`, fencing token +1.
5. Lease owner token'ın yalnız digest'ini sakla.
6. Logical locks aynı transaction'da edin.
7. Claim receipt olmadan handler write/network effect'e başlamaz.

## Heartbeat

Update koşulları:
- exact job/attempt/lease,
- owner digest,
- current fencing token,
- state `running`,
- lease henüz geçerli.

0 row update stale ownership demektir. Worker hemen durur ve sonuç yayınlayamaz.

## Lock conflict

Resource'lar normalized case/posix form kullanır.

- aynı resource: en az biri write ise conflict
- project write: aynı project'teki work/path/db-object ile conflict
- path parent/child: en az biri write ise conflict
- farklı project: varsayılan conflict yok
- cross-project transaction: explicit multi-project lock order gerekir

Deadlock önlemek için lock key lexical order'da alınır.

## Retry

Read-only transient error policy sınırında yeni attempt olabilir. Write/network:
- effect claim yoksa safe retry değerlendirilebilir,
- claim + receipt varsa replay canonical receipt'i döndürür,
- claim + receipt yoksa recovery-required,
- silent retry yok.

## Admission

Yeni iş başlamadan:
- system drain/maintenance,
- project concurrency,
- model/provider quota,
- queue backlog,
- cost/token budget,
- required sandbox,
- verifier availability,
- backup/migration lock

kontrol edilir. Admission authority değildir; yalnız başlama kararıdır.

## Queue recovery

Expired running job:
- read-only ve effect claim yok: attempt limit içinde ready olabilir,
- terminal receipt: completed/failure state reconcile edilir,
- non-read claim/no receipt: recovery-required,
- max attempt: blocked,
- stale active lock: lease/fence kanıtıyla release/recover.

## Acceptance

PostgreSQL concurrency testleri en az:
- 20 worker aynı job'a koşar → tek claim
- old fence complete → reddedilir
- parent/child path → conflict
- different projects → parallel
- crash after claim → recovery-required
- duplicate enqueue → tek job
