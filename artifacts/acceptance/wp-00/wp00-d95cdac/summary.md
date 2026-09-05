# WP-00 acceptance summary

WP-00 binds the living scope authority to the exact bytes of `AKTIF_GOREV.md`, replaces the
old independent PostgreSQL Work/Run YAML with a generated read-only projection, and makes
legacy PostgreSQL connection/export attempts fail before any external effect.

The task baseline and current local/remote HEAD are identical. All staged, unstaged and
untracked user files found before implementation are listed with content digests in
`changed-files.json`; none was cleaned, reset or overwritten.

Focused authority, negative, adversarial, sentinel, lint, type and package validation gates
passed. The broad non-PostgreSQL suite produced 2,372 passes and eight pre-existing
environment/baseline drift failures. Those failures were retained and classified rather than
weakened. No legacy PostgreSQL connection, import, export or migration was attempted.
