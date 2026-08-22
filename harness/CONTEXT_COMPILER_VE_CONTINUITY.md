# Context Compiler ve Continuity

## Context katmanları

Zorunlu exact kayıtlar:

1. Project identity + current capability profile
2. Current Work Item + Intent revision
3. Current Plan revision + current Step
4. Policy/prohibited actions
5. Source binding/revision
6. Checkpoint/Continuity digest
7. Model assignment/client capability (execution anında)

Ek adaylar:

- relevant Decision/ADR
- Work history
- Knowledge citations
- reviewed memory
- failed/rejected approach
- dependency evidence
- selected skills/runbooks

## Seçim skoru

Ek adaylar açıklanabilir ve deterministik skorlanır:

```text
authority
+ exact identity
+ relevance
+ freshness/validity
+ evidence density
+ project/work scope match
- stale penalty
- duplicate penalty
- token cost
```

Semantic similarity authority'yi geçemez. Eşit puan stable identity sırasıyla çözülür.

## Token bütçesi

Zorunlu kayıtlar bütçeye sığmıyorsa sessiz truncation yok; context-budget-exceeded. Ek
adaylar omitted listesine reason ile gider. Current goal, step, constraints ve next action
silinemez.

## Context Manifest

```text
compilation_id
as_of
budget
selected record refs/revisions/digests/tokens/reasons
omitted refs/reasons
source revision
policy digest
memory/retrieval profile
manifest digest
```

Her model call bu manifest'e bağlanır. Prompt transcript'i kanonik değildir.

## Continuity kayıtları

### WorkJournalEvent
Append-only hash chain:
- completed/failed step
- observed error/root cause
- rejected approach
- decision
- artifact
- verifier outcome
- source/model/client change

Private chain-of-thought tutulmaz.

### ContinuitySnapshot
Bounded current projection:
- goal/status/current step
- completed/pending
- decisions/risks
- next safe actions
- authoritative revision refs

### FinalizedHandoff
Client/model neutral:
- first reads
- completed/pending
- evidence/artifact refs
- no active lease/approval/secret/path.

## Staleness

Work revision, orchestration digest, source revision veya policy digest mismatch packet'ı stale
yapar. Stale packet execution'a yetmez; rebuild edilir.

## Context effectiveness

Ölç:
- required evidence recall
- selected/used tokens
- stale/duplicate selection
- omitted required evidence
- downstream verifier success
- compaction rehydration success
- token per verified result

Bu metrikler daha büyük context'in her zaman daha iyi olduğu varsayımını engeller.
