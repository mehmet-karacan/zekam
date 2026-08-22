# CI ve Quality Gates

## Pull request kapıları

1. Paket/repository structure validation
2. Formatting
3. Lint
4. Static type
5. Unit/property
6. Schema compatibility
7. Migration generation/drift
8. PostgreSQL integration/RLS/concurrency
9. Security scans
10. Dependency/license/SBOM
11. Container build/scan
12. Retrieval/eval smoke
13. Model adapter contract fake/safe
14. Documentation links/code examples
15. Commit message policy.

## Main/release ek kapıları

- Full PostgreSQL acceptance
- E2E natural language/research/delivery
- crash/recovery
- backup/restore DR
- model benchmark representative subset
- full retrieval golden
- secret canary scan
- performance budgets
- dead code/unused schema/config
- release artifact checksum/signature
- upgrade from previous supported release.

## Performance başlangıç bütçeleri

Evrensel sabit değil; baseline sonrası sürümlü policy:
- CLI status p95
- queue claim p95
- context compile p95
- retrieval p95
- ingest throughput
- memory search p95
- worker cancellation deadline
- token/cost per verified task.

Regression threshold evidence olmadan gevşetilmez.

## Release

Release candidate:
- clean tag/commit
- migration head
- config/schema version matrix
- SBOM
- checksums
- artifact
- release notes Türkçe
- known limitations
- backup/rollback
- doctor output
- Global DoD evidence report.

## Branch protection

- direct main push deny
- required checks
- review policy
- signed/tag policy kurumsal ihtiyaca göre
- secrets scanning
- force-push/delete deny.

Kullanıcının local kişisel akışında PR zorunlu olmayabilir; aynı quality gates local release
öncesi yine zorunludur.
