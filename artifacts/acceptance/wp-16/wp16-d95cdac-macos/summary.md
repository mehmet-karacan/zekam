# WP-16 macOS acceptance summary

Status: `macos-accepted/windows-deferred`.

Independent/current evidence verdict: `PASS_MAC_ONLY`. Full current-source suite: 6973 tests, 0 failures, 0 errors, 16 skips. Legacy PostgreSQL data was not accessed and user worktree changes were preserved.

Deferred under K-013: Windows x64 acceptance; supported-Python matrix; large corpus and high-load stress; live OpenCode remote providers.
The global task is not complete.

Current-source quality revalidation added a branch-instrumented 6,957-pass full
suite plus 12/12 stored-corruption/replay supplements. Overall branch coverage
is 85.14%; the three critical modules are 90.23%, 92.77% and 90.09%. The
current-source mutation campaign killed 97/100 mutants for a 97% score against
the 75% gate, retained all three survivors, and left production hashes
unchanged. Exact artifact digests are recorded in
`metrics/wp16-current-quality.json`.

Windows handoff pre-commit formatting normalized the supplemental test without
changing production source. The clean R11 run passed 12/12 and supersedes R10;
its exact JUnit and test-source digests are bound in the current quality and
source-digest records.
