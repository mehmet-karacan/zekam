# WP-07 macOS acceptance summary

Status: `macos-accepted/windows-deferred`.

Six read-only Akilli Kasa files were discovered, parsed, chunked and embedded with
the verified local BGE-M3 provider. SQLite FTS5 and sqlite-vec schema v2 provide
separate exact, lexical and dense channels, deterministic RRF, digest dedupe,
optional reranker fallback, project prefiltering and generation-pinned citations.

The real 100-case corpus passed every mandatory Section 14 threshold. Unsupported,
wrong-scope and stale queries abstained without fabricated citations. Provider
failure, partial writes, concurrent rebuild, restart, scratch rebuild and persisted
body/vector/FTS corruption were forced and failed safely. Akilli Kasa source state
was unchanged and PostgreSQL was never contacted.

The independent verifier returned PASS for bounded macOS acceptance. Windows live
remote embedding E2E is deferred under K-013. WP-08 and later packages remain open,
so the overall task is not complete.
