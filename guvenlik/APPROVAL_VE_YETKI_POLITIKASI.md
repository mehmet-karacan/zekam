# Approval ve Yetki Politikası

## Ayrım

- Policy: neye izin verilebilir?
- Capability: adapter/worker ne yapabilir?
- Authorization: exact effect için izin var mı?
- Admission: şimdi başlayabilir mi?
- Lease: kim geçici owner?
- Model assignment: hangi model uygun?
- Receipt: effect terminal sonucu ne?

Biri diğerinin yerine geçmez.

## Risk sınıfları

```text
none/read-only
low
medium
high
critical
```

Risk; effect, blast radius, reversibility, data sensitivity, external system ve verifier
gereksiniminden türetilir. Model self-declare ile düşüremez.

## Otomatik read

Aşağıdaki işlemler policy allowed ve scope current ise onaysız olabilir:
- status/list/history
- local exact/retrieval
- read-only source inspect
- plan/dry-run
- health check without remote? local sentetik
- derived projection/status
- doctor.

Remote provider call read-only olsa da outbound/secret disclosure authorization ister.

## Exact one-shot authorization

Belge:

```text
authorization_id
request/plan/effect/scope digest
allowed paths/resources/tools/network/data effects
provider/SecretRef references
expiry
actor
one-shot/replay state
revocation
```

Apply atomik consume eder veya effect claim ile exact bind eder. Generic “her şeyi yap”
authorization yoktur.

## Kullanıcı ergonomisi

Kullanıcı exact planı açıkça uygulamayı istediğinde:
- aynı planın mandatory child step'leri tekrar onay istemez,
- yeni resource, source drift, policy change, expiry veya farklı provider yeni authorization
  gerektirir,
- status/test/read-only doğrulama için gereksiz popup yoktur.

## Verified automatic completion

Authorized run bound Work Item'ı ikinci onay olmadan yalnız:
- bütün checkpoints complete,
- receipts terminal completed,
- independent verifier passed,
- target Work revision/source dependency current,
- exact completion attestation

ile completed yapabilir. Reopen/manual force/bulk/delete başka scope'tur.

## Database/Git

- read-only metadata policy ile.
- row data/read ayrı classification.
- DB write/migration high+ exact backup/rollback.
- commit medium/high policy'ye göre exact plan.
- push always explicit exact authorization.
- destructive delete/purge critical.

## Negative tests

- stale plan
- path/resource expansion
- wrong actor/project
- expired/revoked/consumed
- approval swap after claim
- authorization ID without full record
- model assignment treated as permission
- lease treated as permission
- client allow bypass
