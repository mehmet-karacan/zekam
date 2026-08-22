# Model Routing, Kota ve Fallback

## Ayrık kayıtlar

Aşağıdakileri tek “route” objesinde karıştırma:

- work classification
- execution shape
- client delegation
- model assignment
- provider authorization
- admission
- quota observation
- mutation authorization

Her biri ayrı digest ve owner taşır.

## Hard eligibility

Aday:
- enabled
- health/contract current
- workload/client/modality support
- project benchmark current
- data classification/locality
- context/output/tool/schema requirement
- verifier exclusion
- latency/cost/token budget
- quota availability

geçmeden score almaz.

## Score

Başlangıçta integer basis point:

```text
quality
+ reliability
+ project specialization
+ historical verified success
- latency penalty
- token penalty
- cost penalty
- human correction penalty
- quota pressure penalty
```

Weight policy sürümlü config'tir. Missing evidence fabricated neutral quality olmaz; policy
explicit prior veya `unscored-fallback` kullanır.

## Kota kaydı

```text
quota_pool_id
client/execution_path
observation_source
observed_at
remaining_ratio | unknown
period/reset
confidence/trust
evidence_digest
```

Provider adı quota pool değildir. Subscription CLI, API key ve kurum içi route farklı pool'dur.

## Kullanıcı fallback policy'si

- Codex trusted ratio < 0.40 → Claude qualified adaylarını değerlendir.
- Claude trusted ratio < 0.30 → kurum içi/OpenCode qualified adaylarını değerlendir.
- Ratio unknown → düşükmüş gibi davranma; unknown reason kaydet ve quality/security policy ile
  başka safe route değerlendir.
- Limit aşımı/429 gözlemi ratio yerine operational exhausted evidence olabilir; reset bilgisi
  tahmin edilmez.

## Context handoff

Fallback model:
- Continuity Packet
- Context Manifest
- exact Work/Plan/Step
- source/policy digests
- verified child results

alır. Transcript, private reasoning, active lease veya eski authorization almaz.

## Verifier exclusion

Model assignment worker model reference/family listesini alır. Risk policy:
- high/critical: aynı exact model ve mümkünse family yasak,
- medium: aynı execution identity yasak,
- low/read-only: ayrı verifier ihtiyacı acceptance'e göre.

## Deliberation

`deliberation-plan` participant, evidence, question, rounds, wall time, tokens, cost ve stop
criteria bağlar. Participant output finding/objection envelope'dır; consensus zorunlu değildir.
Synthesizer contradiction'ı silmez.

## Runtime feedback

Execution sonrası sanitized observation:
- success/verifier pass
- latency/tokens/cost
- retries/human correction
- failure category
- project/workload/model/inventory/profile digests

Minimum sample count öncesi tek run route'u dramatik değiştirmez. Quarantine hard failure
policy'si ayrıdır.
