# WP-01 measured progress summary

Status: **macos-accepted / windows-deferred; global cross-platform gate open**.

MacBook ARM64 evidence covers CPython SQLite and pyturso operational state,
real BGE-M3-backed LanceDB/Zvec/sqlite-vec retrieval, persistent SQLite FTS5,
and a rebuildable DuckDB analytics projection. Candidate packages were installed
only in user-approved temporary environments; production dependencies and the
repository virtualenv were not changed.

The operational fixtures exercised 10,000 projects, 10,000 work items,
100,000 events plus 10,000 serialized-producer events. Constraints,
idempotency/payload drift, concurrent reads, process kill, partial work,
read-only storage, schema drift, corruption, backup/restore and offline behavior
were measured. CPython SQLite 3.53.4 is WAL-reset safe. pyturso embeds SQLite
3.50.4 and therefore requires the measured single-writer coordinator profile.

The knowledge corpus uses 132 current Zekam chunks and real 1024-dimensional
`BAAI/bge-m3` vectors from the loopback Infinity provider. At 250,001 indexed
rows all three vector candidates reached fusion Recall@10 1.0, MRR 0.9375,
citation precision 1.0 and no-answer precision/recall 1.0. The scale fixture
cyclically repeats those 132 vectors, so latency, persistence and footprint are
valid record-scale measurements while semantic diversity remains explicitly
limited. The comparison artifact records candidate-specific tails and sizes.

DuckDB rebuilt the same logical projection from a 100,000-row immutable raw
artifact and enforced writer exclusion. DuckDB itself accepted a truncated
copy; Zekam's required pre-open manifest detected it. This limitation is part of
the acceptance evidence.

No PostgreSQL client or connection was used. No PostgreSQL data was read,
dumped, migrated, exported, imported or transformed. Under the user's K-013
Mac-first decision, the provisional Mac stack is CPython SQLite for operational
state, SQLite FTS5 plus sqlite-vec for knowledge, and DuckDB for analytics.
Windows parity and large-corpus stress remain deferred rather than waived.
This provisional decision opened WP-02, whose separate macOS acceptance package
now records a passed independent adversarial verification.

Preservation exception: a direct package-validator invocation rewrote the
pre-existing user-modified `VALIDATION_RESULT.json`. Its preflight digest was
`sha256:63f2c302d9ef5a380be52ca8e4a6e3aa1720e997e1330843ce5dd1f4ba85e8bc`;
the original bytes were not backed up, so no guessed restoration was attempted.
The validator now accepts `ZEKAM_VALIDATION_RESULT_PATH`, and subsequent runs
wrote only to `/tmp`.
