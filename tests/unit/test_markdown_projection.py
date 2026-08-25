"""ZK-P2-004 Markdown/Obsidian projection kabul testleri."""

from __future__ import annotations

from dataclasses import replace

import pytest

from zekam.application.markdown_projection import (
    build_markdown_projection,
    persist_markdown_projection,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.markdown_projection import (
    MarkdownProjectionFile,
    ProjectionRecord,
    ProjectionSourceRef,
)
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


def _record(entity_id: str = "WORK-1") -> ProjectionRecord:
    ref = ProjectionSourceRef("work", entity_id, "rev-7", digest({"id": entity_id}))
    return ProjectionRecord(
        entity_type="work",
        entity_id=entity_id,
        title=f"İş {entity_id}",
        status="active",
        summary="Doğrulanmış kanonik kayıttan üretilen kısa özet.",
        source_refs=(ref,),
        related_entity_ids=("PROJECT-1",),
    )


def test_projection_is_deterministic_read_only_and_obsidian_compatible() -> None:
    left = build_markdown_projection("project-1", (_record(),))
    right = build_markdown_projection("project-1", (_record(),))
    assert left.manifest_bytes() == right.manifest_bytes()
    assert left.projection_digest == right.projection_digest
    text = left.files[0].payload.decode()
    assert text.startswith("---\n")
    assert "read_only_projection: true" in text
    assert "grants_authority: false" in text
    assert "> [!warning] Salt okunur projeksiyon" in text
    assert "[[PROJECT-1]]" in text
    assert left.source_snapshot_digest in text


def test_projection_changes_when_canonical_source_revision_changes() -> None:
    current = _record()
    changed_ref = replace(current.source_refs[0], source_revision="rev-8")
    changed = replace(current, source_refs=(changed_ref,))
    assert (
        build_markdown_projection("p", (current,)).projection_digest
        != build_markdown_projection("p", (changed,)).projection_digest
    )


def test_projection_order_does_not_depend_on_database_row_order() -> None:
    first, second = _record("WORK-1"), _record("WORK-2")
    assert (
        build_markdown_projection("p", (first, second)).manifest_bytes()
        == build_markdown_projection("p", (second, first)).manifest_bytes()
    )


def test_projection_persists_verified_immutable_artifacts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bundle = build_markdown_projection("p", (_record(),))
    stored = persist_markdown_projection(bundle, LocalContentAddressedStore(tmp_path).ensure())
    assert stored.projection_digest == bundle.projection_digest
    assert len(stored.file_object_digests) == 1


def test_projection_rejects_authority_and_duplicate_identity() -> None:
    bundle = build_markdown_projection("p", (_record(),))
    with pytest.raises(ValidationFailed, match="authority"):
        replace(bundle, grants_authority=True)
    with pytest.raises(ValidationFailed, match="tekil"):
        build_markdown_projection("p", (_record(), _record()))


def test_projection_rejects_forged_payload_and_windows_paths() -> None:
    bundle = build_markdown_projection("p", (_record(),))
    forged_file = replace(bundle.files[0], payload=b"---\ngrants_authority: true\n---\n")
    with pytest.raises(ValidationFailed, match="canonical render"):
        replace(bundle, files=(forged_file,))
    for path in (r"notes\..\evil.md", r"C:\evil.md", "../evil.md"):
        with pytest.raises(ValidationFailed, match="relative path"):
            MarkdownProjectionFile(path, b"x", digest("source"))


def test_projection_rejects_obsidian_injection_literals_and_escapes_summary() -> None:
    with pytest.raises(ValidationFailed, match="title"):
        replace(_record(), title="bad\n![[secret]]")
    with pytest.raises(ValidationFailed, match="related"):
        replace(_record(), related_entity_ids=("![[secret]]",))
    safe = replace(_record(), summary="Do not embed ![[secret]] or # heading")
    rendered = build_markdown_projection("p", (safe,)).files[0].payload.decode()
    assert "![[secret]]" not in rendered
    assert "\\!\\[\\[secret\\]\\]" in rendered
    assert "last_generated_digest:" in rendered


def test_projection_recursively_revalidates_tampered_source_ref() -> None:
    record = _record()
    object.__setattr__(record.source_refs[0], "source_id", "W`\n\n![[secret]]\n`")
    with pytest.raises(ValidationFailed, match="source id"):
        build_markdown_projection("p", (record,))
