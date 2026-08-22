# Kilit, Effect Claim, Receipt ve Recovery

## Logical resource standardı

Portable resource örnekleri:

```text
project:<project-id>
work:<project-id>:<work-id>
path:<project-id>:src/module/file.py
db-object:<project-id>:oracle:package:PAYMENT_PKG
db-object:<project-id>:postgresql:table:payments
artifact:<project-id>:<artifact-id>
provider:<provider-ref>:<operation>
model-benchmark:<model-id>:<suite-id>
skill-registry:<scope>
memory:<scope>:<identity>
```

Absolute path, `..`, backslash normalization ambiguity ve secret değerleri reddedilir.

## Lock scope

Plan step read/write access'i önceden ilan eder. Runtime gerçek effect öncesinde exact lock
setini yeniden doğrular. Agent sonradan yeni path bulursa scope escalation yapamaz; yeni plan
revision gerekir.

## Effect Claim

Claim immutable ve append-only'dir:

```text
claim_id
realm/project/work/plan/step
operation/effect_digest
logical resources
authorization_digest
execution identity
job/attempt/lease/fence
adapter/host digest
idempotency key
claimed_at
```

Claim dış effect'i başlatma niyetini kanıtlar; effect'in gerçekleştiğini kanıtlamaz.

## Receipt

Terminal `completed` veya `failed`:

```text
receipt_id
claim_id
status
result_digest OR failure_category+failure_digest
adapter evidence digest
actual token/cost/latency
completed_at
```

Completed receipt ile patch/test/model output gibi artifacts ayrı content-addressed storage'da
tutulur. Receipt secret veya ham provider response taşımaz.

## Recovery algoritması

1. Claim ve receipt ledger'ını authoritative store'dan oku.
2. Adapter idempotency/reconciliation API varsa exact effect identity ile sorgula.
3. Dış effect gerçekleşti kanıtı varsa canonical receipt üret.
4. Gerçekleşmediği kesin kanıtlanırsa yeni reviewed recovery planı hazırla.
5. Belirsizse `blocked-effect-uncertain`; insan veya provider reconciliation gerekir.
6. Eski claim'i silme/overwrite etme.

## Git özel durumu

- Worktree file write claim'i local filesystem snapshot/diff ile reconcile edilebilir.
- Commit claim'i commit object/parent/tree ile doğrulanır.
- Push claim'i remote ref ve expected old/new OID ile reconcile edilir.
- Push hiçbir zaman generic retry ile tekrarlanmaz.

## DB özel durumu

- Transactional DB effect aynı transaction'da receipt kaydıyla commit edilebiliyorsa atomik
  outbox/ledger tercih edilir.
- Haricî DB için idempotency key, transaction marker veya reconciliation query gerekir.
- DDL/migration backup ve rollback planı ister.

## Provider özel durumu

Provider request ID tek başına authority değildir; provider, operation, payload digest,
session/authorization ve retention disclosure eşleşir. Response parse edilemiyorsa ham secret
olmayan bytes secure artifact olarak tutulabilir ve failed receipt üretilir.
