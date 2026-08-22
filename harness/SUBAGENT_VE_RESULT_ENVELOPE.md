# Subagent ve Agent Result Envelope Sözleşmesi

## Roller

- `coordinator`: plan, dispatch, fan-in; child sayılmaz
- `explorer/researcher`: read-only finding/evidence
- `builder`: authorized sandbox mutation
- `tester`: test execution/evidence
- `verifier`: bağımsız verdict; write/network default deny
- `critic`: counter-evidence/risk
- `synthesizer`: verified child sonuçlarını birleştirir
- `memory-curator`: memory candidate/hygiene; authority yok

Rol capability değildir. Her execution ayrıca explicit trust role ve tool permissions taşır.

## Agentic minimum

İş agentic ise en az bir child execution record ve terminal envelope olmadan coordinator
sonucu tamamlanamaz. Child unavailable ise iş `blocked-capability-missing` olur; koordinatör
child rolünü gizlice üstlenemez.

## Envelope public alanları

```text
schema_version
envelope_id
project_id
work_item_id
plan_revision_id
step_id
attempt_id
role
execution_identity
model_assignment_id
status
summary
findings[]
risks[]
decisions[]
artifact_refs[]
evidence_refs[]
effect_claim_ref
effect_receipt_ref
verification
missing_steps[]
failure_category
input_digest
result_digest
created_at
grants_authority=false
```

Unknown field, absolute path, credential, raw prompt/model output, private reasoning veya
source content fail-closed'dur.

## Durumlar

- `completed`: bütün step acceptance ve effect receipt mevcut
- `partial`: yararlı sonuç var, mandatory kısım eksik
- `failed`: terminal hata
- `blocked`: dış/gate önkoşulu eksik
- `recovery-required`: uncertain/interrupted effect
- `abstained`: evidence yetersiz veya güvenli cevap yok

Partial completed sayılmaz. Recovery-required fan-in'de partial'dan önceliklidir.

## Role kuralları

Explorer:
- mutation effect bildiremez,
- source/evidence referansını verir.

Builder:
- non-read completed için matching claim+receipt zorunlu,
- yalnız allowlisted resources.

Verifier:
- builder'dan farklı execution identity,
- yüksek/kritik riskte farklı model family policy ile zorlanabilir,
- verdict `passed|failed|inconclusive`,
- acceptance subject coverage listesi.

## Fan-in

Coordinator:
1. Bütün child envelope'larını strict parse eder.
2. Project/work/plan/step/attempt identity eşleşmesini doğrular.
3. Duplicate step/attempt ve tamper reddeder.
4. Receipt scope ve envelope result digest eşleşmesini doğrular.
5. Mandatory step eksikse completed üretmez.
6. Direct contradiction'ı görünür tutar.
7. Final summary yalnız verified finding/evidence'den oluşur.
8. Work completion authority'si olmadığını belirtir.

## Client adapters

Codex/Claude/OpenCode native free-text output'u doğrudan envelope değildir. Adapter:
- modele strict JSON output schema verir,
- parse/validate eder,
- execution evidence ve process exit/timeout ekler,
- public field'leri normalize eder,
- malformed output'u `failed: invalid-output` yapar.

## Negatif testler

- free text authoritative sonuç
- fenced JSON
- unknown field
- cross-project result
- same builder/verifier identity
- completed without receipt
- explorer mutation
- duplicate step
- forged artifact/evidence
- absolute path/secret
- partial as completed
