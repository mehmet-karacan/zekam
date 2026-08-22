# Secret Broker ve Outbound Policy

## Temel kural

Model, subagent, prompt, context manifest, log, vector, artifact metadata, benchmark veya
rapor plaintext secret görmez.

## SecretRef

```text
secret_ref_id
provider
realm/project scope
purpose
allowed operations
version
expiry/rotation state
store backend
metadata digest
```

SecretRef value değildir ve authority vermez.

## Broker akışı

1. Adapter exact provider/tool operation ve SecretRef ister.
2. Governance authorization, scope, purpose, expiry ve data disclosure'ı doğrular.
3. Broker value'yu OS keychain/Vault/KMS/local encrypted store'dan process memory'ye çözer.
4. Adapter credential'ı header/socket/SDK alanında kullanır.
5. Value logging/exception/env dump/core dump riskine karşı redaction uygulanır.
6. Call bittiğinde reference/audit tutulur; value tutulmaz.

## Yasak taşıma

- CLI argument
- shell command string
- prompt/message
- child envelope
- model provider payload body (credential dışında)
- JSON/YAML config committed file
- `.env` repository
- vector embedding
- research source summary
- error message
- telemetry attribute
- backup
- continuity packet.

## OutboundRequest

```text
provider_ref
endpoint_ref
operation
data categories
source/project/work refs
payload digest
retention/training assumptions
region/route
session/request identity
SecretRef
budget
authorization
```

Prepare no network/no secret resolution. Apply exact request/authorization eşleşmesini yeniden
kontrol eder.

## Data classification

```text
public
internal
confidential
restricted
secret
local-only
```

Secret her zaman payload dışında. `local-only` remote provider'a gönderilemez. Restricted
policy explicit allow ve reviewed disclosure ister.

## Provider response

Untrusted data:
- strict schema parse,
- size/time limit,
- prompt injection marker,
- secret echo scan,
- physical path scan,
- raw response secure artifact policy.

## Rotation/revoke

Secret version değişince active operation eski ref'i kullanamaz. Long-running job credential
değeri checkpoint etmez; yeni call'da current allowed version çözülür. Revoke provider
authorization ve pending job admission'ı etkiler.

## Testler

- prompt/log/exception/trace secret leak
- child result echo
- malicious source secret request
- wrong project/purpose
- expired/revoked
- endpoint swap
- authorization replay
- provider response credential echo
- backup/search/vector scan
