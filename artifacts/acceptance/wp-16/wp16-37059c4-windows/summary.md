# WP-16 Windows acceptance summary

Status: `accepted-with-user-scoped-waivers`.

Windows x64 portability, native file security, safe SQLite fallback and single-writer locking, packaging, selected Python 3.12/3.13/3.14 critical suites, OpenCode/Bun lifecycle, real BGE-M3 semantic embeddings, and read-only Akilli Kasa RAG were exercised. The selected BGE-M3 also passed a combined live-provider plus durable SQLite ledger E2E with 2/2 claims and terminal receipts, integrity `ok`, zero recovery and exact restart readback. After the authority was synchronized to the independent final PASS, current-6 wheel smoke passed 12/12 and current-6 sdist smoke passed 13/13 against the same regenerated package manifest. No PostgreSQL data was accessed, Docker was not required, no secret value is present, and no push was performed.

The user explicitly selected a Windows/OpenCode-only release scope. The 250,000-record p95 target is waived for this acceptance, and macOS/POSIX-only plus real Claude/Codex lifecycle tests are excluded rather than relabeled as passes. No full cross-platform suite pass is claimed. Qwen3 and e5-mistral remain quarantined, but they are not selected production models and do not gate BGE-M3.

The Akilli Kasa corpus was read-only and bounded. Its Git identity is rechecked separately at the end of acceptance and bound in the verifier report/manifest. Results generated on macOS were not relabeled as Windows evidence.
