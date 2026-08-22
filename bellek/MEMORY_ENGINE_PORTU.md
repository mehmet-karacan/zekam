# MemoryEngine Port Sözleşmesi

## Application port

```python
class MemoryEngine(Protocol):
    def prepare_candidate(self, request: MemoryCandidateRequest) -> MemoryCandidatePlan: ...
    def apply_candidate(
        self, plan: MemoryCandidatePlan, authorization: Authorization
    ) -> MemoryCandidate: ...
    def search(self, query: MemoryQuery) -> MemorySearchResult: ...
    def explain(self, selection_id: str) -> MemorySelectionExplanation: ...
    def prepare_review(self, candidate_id: str, review: ReviewInput) -> MemoryReviewPlan: ...
    def promote(
        self, plan: MemoryPromotionPlan, authorization: Authorization
    ) -> MemoryRevision: ...
    def prepare_lifecycle(self, request: MemoryLifecycleRequest) -> MemoryLifecyclePlan: ...
    def apply_lifecycle(
        self, plan: MemoryLifecyclePlan, authorization: Authorization
    ) -> MemoryRevision: ...
    def hygiene(self, request: MemoryHygieneRequest) -> MemoryHygieneReport: ...
    def health(self) -> MemoryEngineHealth: ...
```

İsimler örnektir; typed contract ve davranış değişmez.

## Candidate request

- scope ve class
- normalized subject/entities
- bounded content/summary
- evidence refs
- source revisions
- proposed validity
- producer identity/model assignment
- sensitivity classification
- idempotency key

## Search query

- realm/user/project/work/run/agent scope
- class filter
- as_of
- exact identities/entities
- query text/vector request
- validity/freshness policy
- maximum hits/tokens
- required evidence classes
- allow external adapter
- explain flag

Search kendi başına provider call yapmaz; embedder call gerekiyorsa Provider Gate'ten geçer.

## Result

- selected memory revisions
- exact evidence refs
- score components
- stale/conflict/duplicate flags
- omitted count/reasons
- engine/adapter provenance
- result digest
- grants_authority=false

## Adapter compatibility

`NativePostgresMemoryEngine` tüm semantics'i uygular. `Mem0OssMemoryEngine`:
- port contract'tan daha zayıf özellikteyse capability matrix bildirir,
- unsupported operation'ı sessizce taklit etmez,
- native engine ile dual authority oluşturmaz,
- external ID mapping ve sync status tutar.

## Transaction ve external call

Plan DB transaction içinde hazırlanır; haricî Mem0/embedder çağrısı uzun DB transaction içinde
tutulmaz. External result source digest ile kısa transaction'da reconcile edilir.

## Error categories

```text
invalid-candidate
scope-violation
evidence-missing
source-stale
duplicate-conflict
review-required
authorization-mismatch
external-unavailable
external-drift
budget-exceeded
sensitive-content
```
