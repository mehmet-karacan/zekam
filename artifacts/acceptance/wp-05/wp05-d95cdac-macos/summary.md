# WP-05 macOS acceptance summary

Status: **macos-accepted / windows-deferred**. Independent verifier: **PASS**.

The versioned `.zekam` home now has global and project knowledge contracts,
deterministic `PROJECT.yaml` projections, immutable user/generated Markdown
separation, a sole-copy SHA-256 CAS, typed classifications and sync profiles,
verified relation projections, and recoverable inbox/archive lifecycles.

Every note manifest has an exact owner scope, project slug, realm and authorship
binding. Project, work, run and session owners are resolved to their operational
project at both the application and raw SQLite boundaries. Cross-project and
cross-realm attempts are rejected. Generated Markdown requires canonical source
references and digests and rejects duplicate YAML keys.

Filesystem publication uses pinned directory handles, no-follow opens, atomic
staging and post-publication identity checks. Existing symlinks, parent swaps,
unsafe audit roots, corrupt files, orphan CAS objects and half-materialized rows
are detected without writing outside the temporary test home.

Public projections fail closed for credentials, local paths, email, phone,
TCKN, Turkish IBAN and Luhn-valid card fixtures, including public binary media
labels. Secret-classified notes/artifacts cannot enter the normal file plane.

The final focused set passed 104 tests. The independent verifier repeated the
set and the cross-project boundary test and returned PASS. Lint, format, strict
typing and package validation passed. The full suite produced 2678 passes and
712 intentional PostgreSQL skips. Its five remaining failures are existing
machine-contract drift: three installed client binary digests, Claude 2.1.252
versus the 2.1.224 pin, and a Windows `zekam.exe` path assumption on macOS.

No PostgreSQL connection, data access, migration, export/import, ETL, Docker or
network provider call was used. `/Users/mkaracan/Projeler/akilli-kasa` was not
written. Windows stress remains deferred under K-013. WP-05 is accepted; the
global task is not complete and the next dependency is WP-06.
