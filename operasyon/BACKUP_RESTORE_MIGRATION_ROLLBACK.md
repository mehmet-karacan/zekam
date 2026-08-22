# Backup, Restore, Migration ve Rollback

## Kapsam

- PostgreSQL canonical/user/runtime history
- object storage artifacts
- ZEKAM_HOME portable/local metadata
- config/policy/model inventory
- secrets ayrı secret-store yöntemi
- source binding manifest (source content değil)
- release binary/source checksum.

## Backup türleri

### Thin project export
Canonical project/work/decision/research/approved memory/portable DB metadata. Derived index,
active runtime, locks, source bytes, secrets yok.

### Ready project export
Thin + verified derived index ve tamamlanmış runtime/continuity. Active lease/claim uncertain,
secret/path yok.

### Full system backup
PostgreSQL+object storage+configuration tutarlı snapshot. Secret store ayrı policy.

## Tutarlılık

Backup manifest:
- schema/release versions
- DB checkpoint/LSN/time
- object artifact digests
- included/excluded collections
- source external dependencies
- encryption/compression
- checksum/signature
- restore requirements.

## Restore

1. Target empty/isolated.
2. Manifest/path/hash/secret scan.
3. DB migration compatibility.
4. Object artifact verify.
5. Import staging.
6. Canonical identity/conflict.
7. Source binding `unbound`; explicit rebind.
8. Active lease/lock owner restore etme.
9. Derived rebuild/current check.
10. Doctor/E2E.
11. Atomic publish.

Existing project overwrite yok.

## Migration

- Expand/contract ve backward-compatible window.
- Alembic veya seçilen migration tool tek authority.
- Schema drift CI.
- Backup/plan/approval for destructive.
- Forward-only runtime ledger where downgrade unsafe.
- Data backfill idempotent, checkpointed.
- Application version compatibility matrix.

## Rollback

Release:
- previous binary/config
- DB compatibility
- feature flags
- artifact retained
- source/main untouched.

Migration:
- safe downgrade varsa test,
- yoksa restore/reconcile planı,
- user data silent delete yok.

## DR tatbikatı

Periyodik izole restore:
- RPO/RTO ölçümü,
- Work/receipt/memory/inventory counts/digests,
- sample retrieval rebuild,
- project rebind,
- secret references unresolved until safe binding,
- report.

“Backup alındı” yalnız restore test edilince kanıttır.
