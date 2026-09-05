"""Database-neutral lifecycle runtime template identity helpers."""

from __future__ import annotations


def template_source_revision(run_source_revision: str) -> str:
    """Map a dirty-aware run identity to its immutable Git template revision."""

    marker = ";state:sha256:"
    if run_source_revision.startswith("git:") and marker in run_source_revision:
        revision, state_digest = run_source_revision[4:].split(marker, 1)
        if (
            len(revision) == 40
            and all(character in "0123456789abcdef" for character in revision)
            and len(state_digest) == 64
            and all(character in "0123456789abcdef" for character in state_digest)
        ):
            return revision
    return run_source_revision
