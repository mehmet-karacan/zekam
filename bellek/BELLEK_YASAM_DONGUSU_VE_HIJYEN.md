# Bellek Yaşam Döngüsü ve Hijyen

## State

```text
candidate
→ reviewed
→ active
→ superseded | revoked | archived
```

Rejected candidate evidence olarak tutulabilir; active retrieval'a girmez.

## Observation → candidate

Memory adayı yalnız:
- Work/Run/Research/Verification evidence,
- explicit user statement,
- trusted imported record

üzerinden oluşur. Model output tek başına yeterli değildir.

## Promotion

Semantic/procedural/failure promotion için:

- stable occurrence/subject identity,
- evidence refs ve current source revision,
- duplicate/conflict scan,
- minimum confidence/evidence policy,
- candidate producer'dan farklı reviewer,
- kritik kuralda bağımsız verifier,
- exact promotion plan.

Preference memory explicit user kaydıyla daha hafif review kullanabilir; yine security policy
olamaz.

## Supersession

Yeni bilgi eskisini overwrite etmez:
- new revision veya new identity,
- `supersedes` relation,
- old validity kapanışı,
- provenance korunur.

Source'un farklı sürümleri otomatik duplicate sayılmaz; `source-version-conflict` veya
version chain olabilir.

## Hygiene sınıfları

- stale source/policy/profile
- expired validity
- duplicate exact content
- semantic near-duplicate candidate
- direct conflict
- unused/low utility
- retention review due
- orphan evidence
- external sync drift
- overscoped/cross-project
- secret/sensitive violation
- low verifier success correlation

Hygiene raporu read-only ve authority-free'dir. Silme/merge/revoke için ayrı exact plan gerekir.

## Kullanım ve etkinlik ölçümü

Her context selection:
- selected_at,
- actually_used evidence,
- downstream verifier result,
- token cost,
- stale/duplicate flag

ile ölçülür. Memory yalnız sık seçildiği için doğru sayılmaz. Utility; verified success ve
evidence recall ile değerlendirilir.

## Failure memory tekrar önleme

Occurrence key:

```text
normalized problem class
+ project capability digest
+ relevant tool/adapter/version
+ root cause digest
```

Aynı hata geldiğinde Context Compiler:
- önce verified failure/procedural memory'yi seçer,
- reddedilen yaklaşımı prohibited suggestion yapar,
- yeni ortam/revision farkını gösterir.

Root cause doğrulanmamışsa “öğrenildi” denmez; hypothesis olarak kalır.

## Retention

Policy class/scope bazında:
- working: kısa TTL
- run episodic: run lifecycle + configured retention
- semantic/procedural: validity/source-driven review
- preference: kullanıcı revoke edene kadar, periodic review
- failure: utility ve environment freshness'e göre

Physical purge backup, legal/retention ve user approval sınırlarına tabidir.

## Negatif test

- model output active semantic memory
- cross-project retrieval
- secret memory
- old source active result
- overwrite history
- same actor self-promotion
- duplicate evidence double weight
- conflict silently merged
- hygiene automatic delete
- Mem0 result current Work state
