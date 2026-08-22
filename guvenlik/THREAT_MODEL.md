# Zekam Threat Model

## Korunan varlıklar

- kullanıcı source ve belgeleri
- Work/Decision/Plan geçmişi
- credentials/secrets
- model/provider quota ve maliyet
- DB metadata/row data
- patch/commit/release
- memory/skills
- runtime ownership ve receipts
- iç endpoint/topoloji
- personal/regulated data.

## Saldırgan yüzeyler

- doğal dil request
- untrusted document/repository/academic paper
- model output
- subagent result
- MCP/tool metadata
- provider response
- archive/symlink/path
- Git hooks/submodules
- DB statement
- dashboard/API
- scheduler inbox
- model inventory import
- memory poisoning
- stale/duplicate run
- compromised worker.

## Başlıca tehdit ve kontrol

### Prompt injection
Source instruction data-only, trust labels, bounded evidence, tool/policy separation, tests.

### Secret exfiltration
SecretRef/Broker, outbound disclosure, redaction, no prompt/log/vector, echo scan.

### Path escape
Canonical allowed roots, logical resources, no absolute portable path, symlink/archive checks.

### Duplicate/uncertain effect
Idempotency, lock, lease/fence, claim/receipt, recovery, no silent retry.

### Stale worker
Fencing token on every publish/effect/lock release.

### Model hallucination/poisoning
Evidence/citation, strict schema, verifier, memory promotion gate, source freshness.

### Cross-project leak
Realm/project RLS, query scope, project-tagged vectors/memory, negative tests.

### Over-broad agent
Typed capabilities, sandbox, network deny, exact auth, single builder.

### Supply chain
Pinned dependencies/images, SBOM, signature/checksum, audit, no untrusted build during ingest.

### SSRF/network
EndpointRef registry, DNS/redirect/IP validation, metadata/private-range policy, TLS.

### Resource exhaustion
File/count/ratio/token/time/cost/concurrency limits, cancellation, backpressure.

### Inaccurate completion
Work evidence, receipt, independent verifier, completion attestation.

### Memory poisoning
Candidate/review/evidence/scope/validity, conflict visibility, no raw output activation.

## Security test families

- STRIDE-style component cases
- prompt injection corpus
- secret canaries
- path/symlink/archive
- concurrency/replay
- authorization mutation/fuzz
- schema/property
- provider/MCP malicious payload
- cross-tenant RLS
- dependency/container scan
- backup restore secret scan
