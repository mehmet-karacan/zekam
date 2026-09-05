# WP-08 schema implementation approval

The user answered `Ok` to the explicit question asking permission to implement
revision 2 of `WP-08-operational-schema-v2-proposal.md` in this task.

Approved proposal SHA-256:
`331cb171e2870c88f477a2c4e63e9eb86764009cf8cee021965cff65ca0c0ece`.

The approved proposal remains unchanged so its reviewed digest is reproducible.
This note records the scope of the user's reply; it does not create or transfer
runtime authority, and does not claim implementation or acceptance success.

Scope: implement and verify the proposed local operational v2 schema and continuity
adapters first in disposable local test homes. Preserve existing user files and
local operational rows. PostgreSQL is not a source and must not be accessed.

Excluded: installation or activation of client hooks, live model benchmark
campaigns, remote disclosure, commit push, retention changes and destructive
operations. These retain their separate approval requirements. Windows live
acceptance and large-scale gates remain deferred under K-013, not passed.
