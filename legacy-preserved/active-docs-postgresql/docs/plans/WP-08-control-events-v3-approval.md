# WP-08 v3 implementation approval and review delegation

The user explicitly approved `WP-08-control-events-v3-proposal.md` at SHA-256
`741e10c77f77085aefde89659664ba213d310ac576e6658d643a638b8a49d217` and instructed
the coordinator to continue using independent subagent review rather than asking
for the same implementation approval again.

This records the user's implementation instruction, not approval created by an
agent, model output, Markdown projection or runtime record. The proposal bytes
remain unchanged. Independent review findings must be addressed and tested;
review does not waive acceptance gates or generate effect authority.

For further technical implementation decisions within the binding task, retain a
written exact plan, independent review, bounded tests and rollback/recovery proof
without repeatedly requesting the same user's consent. Preserve the original
task boundaries: PostgreSQL is never a source, existing user data is not deleted
or overwritten, Akilli Kasa remains read-only, and Windows/scale gates are not
falsely marked passed. No extra project, external disclosure, spend, push or
unbounded self-modification is inferred from review delegation.

Immediate work: additive v3 control evidence and integrated durable lifecycle
tests in disposable homes. Then continue WP-08 dependencies and remaining work
packages in order. Nothing in this record declares WP-08 or the global task done.
