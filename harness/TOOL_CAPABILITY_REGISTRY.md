# Tool ve Capability Registry

## Amaç

Agent'a arbitrary shell gücü vermek yerine sürümlü, typed ve policy-controlled capabilities
sağlar.

## Capability record

```text
capability_id
version
owner
operations
input/output schema
effect classes
logical resource mapping
network/data classification
sandbox requirement
idempotency/reconciliation support
timeout/cancellation
health probe
audit fields
```

## Tool grupları

- filesystem-read / managed-worktree-write
- git-read / patch / commit / push
- process-test / build
- database-metadata / database-read / database-write / migration
- web-research / provider-model
- object-storage
- embedding / rerank / transcription / vision / guardrail
- secret-broker
- report/export
- MCP adapter

## Permission kararı

Client permission `allow/ask/deny` yalnız ikinci katmandır. Zekam önce:
- policy,
- exact authorization,
- scope,
- source freshness,
- lock/lease,
- data classification,
- outbound disclosure

kontrol eder. Client izin verse bile Zekam deny edebilir.

## Discovery

Provider, CLI veya MCP tool otomatik authority değildir. Registry'ye:
1. explicit config veya approved discovery,
2. health/contract test,
3. capability manifest,
4. policy review

ile girer. Version/digest değişince yeniden doğrulama gerekir.

## Typed shell

Genel `bash` yalnız kontrollü development sandbox'ta fallback'tir. Test/build komutları:
- argv listesi,
- executable allowlist,
- cwd logical workspace,
- environment allowlist,
- timeout,
- output size,
- network flag

ile typed ProcessRequest olarak yürütülür.

## MCP sınırı

MCP server'ın tool annotation'ı untrusted metadata'dır. Zekam kendi effect classification ve
policy'sini uygular. MCP resource içeriği de untrusted source data'dır; prompt injection
talimatı olarak yürütülmez.
