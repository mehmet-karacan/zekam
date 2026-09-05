# WP-02 macOS acceptance summary

Status: **macos-accepted / windows-deferred**.

ZEKAM_HOME v2 now bootstraps from zero operational data with CPython SQLite,
without Docker, network, PostgreSQL connection, or legacy-data transfer. Dry-run
is read-only; publish uses a sibling stage, full tree fsync, atomic rename and
parent-directory fsync. Repeating init returns the same receipt and preserves
user content.

The independent verifier passed symlink-parent escape, duplicate YAML/JSON,
semantic receipt forgery, authority drift, process-kill recovery, same-PID
orphan recovery, and controlled acquire/release/fsync-failure concurrency
windows. All partial stages and dead locks are quarantined rather than deleted.

The current focused acceptance suite passed 166 tests. A broader unit run first
passed 2169 tests, while a later run exposed an existing intermittent
client-lifecycle spool parent-creation race; isolated repetition reproduced it.
That test was not weakened and is recorded for its later owning work package.

Windows permission/reparse acceptance remains explicitly deferred under K-013.
No global cross-platform completion is claimed. The next dependency is WP-03.
