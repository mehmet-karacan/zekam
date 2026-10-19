"""Kaynak chunk gizlilik kapisi regresyonlari.

Sadece sentetik fixture'lari kullanir (rezerve `example.com` adresleri).
Hic bir gercek temasa, kurumsal dosyaya veya kullanici verisine bakmaz.
Testler bu dosyada hazirlanmis, calistirilmis degildir.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application import project_knowledge_index as knowledge_index
from zekam.application.project_knowledge_index import (
    SOURCE_PRIVACY_POLICY_DIGEST,
    build_project_index_plan,
)
from zekam.application.source_discovery import discover
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed

pytestmark = pytest.mark.unit

# Rezerve example adresleri; gercek kisilere ait degildir.
EMAIL = "jane.doe@example.com"
AUTHOR_LINE = "@author Synthetic Developer"

CLEAN_JAVA = "package app;\npublic class Service {\n  void run() {}\n}\n"
NOTES_MD = f"# Not\n\nIletisim: {EMAIL}\n\n```\n{AUTHOR_LINE}\n```\n"


def _write_source(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(CLEAN_JAVA, encoding="utf-8")
    (root / "Notes.md").write_text(NOTES_MD, encoding="utf-8")


def _plan(root: Path) -> object:
    report = discover(root)
    return build_project_index_plan(
        project_id=uuid4(),
        project_slug="gpu",
        source_root=root,
        source_revision="abc123",
        expected_tree_digest=report.tree_digest,
    )


def test_email_and_author_chunks_are_rejected_not_redacted(tmp_path: Path) -> None:
    root = tmp_path / "gpu"
    _write_source(root)

    plan = _plan(root)

    # Notlar dosyasinin e-posta paragrafi ve @author kod blogu reddedilir;
    # temiz baslik ve temiz Java birimleri kalir.
    assert plan.privacy_excluded_chunk_count == 2
    assert len(plan.chunks) == 3
    assert all(
        chunk.locator.relative_path in ("Notes.md", "src/Service.java")
        for chunk in plan.chunks
    )
    # Reddedilen chunk kimligi yeniden kullanilmaz; hicbir chunk metni
    # hassas fixture icerigini tasimaz.
    assert not any(EMAIL in chunk.text for chunk in plan.chunks)
    assert not any("author" in chunk.text.lower() for chunk in plan.chunks)
    # Yasakli icerik belge, manifest, plan dict ve chunk'lara hic karismaz.
    rendered = repr(
        (plan.as_dict(), plan.document.body(), plan.manifest.decode("utf-8"))
    )
    assert EMAIL not in rendered
    assert "jane.doe" not in rendered
    assert "example-dev" not in rendered


def test_clean_chunk_retention_preserves_order_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "gpu"
    _write_source(root)
    project_id = uuid4()
    report = discover(root)

    # Ayni agac ve ayni project_id ile filtre kapali temel plan (baseline).
    with monkeypatch.context() as m:
        m.setattr(knowledge_index, "_chunk_is_privacy_excluded", lambda _text: False)
        baseline = build_project_index_plan(
            project_id=project_id,
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
        )
    # Filtre acik normal plan; beklenen tutulular = baseline'un gercek
    # predicate ile filtrelenmis chunk'lari (ayni agac, ayni tree digest).
    plan = build_project_index_plan(
        project_id=project_id,
        project_slug="gpu",
        source_root=root,
        source_revision="abc123",
        expected_tree_digest=discover(root).tree_digest,
    )
    expected = tuple(
        (chunk.chunk_id, chunk.order, chunk.locator, chunk.text)
        for chunk in baseline.chunks
        if not knowledge_index._chunk_is_privacy_excluded(chunk.text)
    )
    actual = tuple(
        (chunk.chunk_id, chunk.order, chunk.locator, chunk.text)
        for chunk in plan.chunks
    )
    assert actual == expected
    # Baslik (order 0) kalir; email/author birimleri (1, 2) dusmus, Java
    # birimleri (3, 4) kalir: kabul edilen siralamada bosluk gozlenir.
    assert len(baseline.chunks) == 5
    assert [chunk.order for chunk in plan.chunks] == [0, 3, 4]
    assert len({chunk.order for chunk in plan.chunks}) == len(plan.chunks)
    assert plan.discovery.inventory_digest == discover(root).inventory_digest
    # Belge yalnizca kabul edilen chunk'lari tasir.
    assert plan.document.unit_count == len(plan.chunks)


def test_privacy_digest_and_count_are_deterministic_and_bound(tmp_path: Path) -> None:
    root = tmp_path / "gpu"
    _write_source(root)
    project_id = uuid4()
    report = discover(root)

    first = build_project_index_plan(
        project_id=project_id,
        project_slug="gpu",
        source_root=root,
        source_revision="abc123",
        expected_tree_digest=report.tree_digest,
    )
    second = _plan(root)

    # Sabit politika digest'i her plan'da ve plan_digest'e baglanmis ayni.
    assert first.source_privacy_policy_digest == SOURCE_PRIVACY_POLICY_DIGEST
    assert second.source_privacy_policy_digest == SOURCE_PRIVACY_POLICY_DIGEST
    assert first.privacy_excluded_chunk_count == second.privacy_excluded_chunk_count == 2
    assert first.plan_digest != second.plan_digest  # farkli project_id
    # Ayni kimlik/sayfa yeniden plani ayni plan_digest verir (deterministik).
    replay = build_project_index_plan(
        project_id=project_id,
        project_slug="gpu",
        source_root=root,
        source_revision="abc123",
        expected_tree_digest=report.tree_digest,
    )
    assert replay.plan_digest == first.plan_digest
    # Digest sabit kanonik govdeden uretilir (surum/kapsam/kurallar/kimlik).
    assert first.source_privacy_policy_digest == digest(
        {
            "schema": "zekam-source-chunk-privacy-policy/v1",
            "scope": "project-source-chunks",
            "rules": ("reject-email-address", "reject-author-annotation"),
            "redaction": "reject-whole-chunk",
        }
    )


def test_no_excluded_chunk_reaches_downstream_inputs(tmp_path: Path) -> None:
    root = tmp_path / "gpu"
    _write_source(root)

    plan = _plan(root)

    # Provider girdisi yalnizca kabul edilen chunk metinleridir.
    provider_texts = tuple(chunk.text for chunk in plan.chunks)
    assert EMAIL not in "\n".join(provider_texts)
    assert "example-dev" not in "\n".join(provider_texts)
    # Embedding batch'i ayni kabul edilen chunk kumesinden kurulur.
    batches = knowledge_index._project_embedding_batches(tuple(plan.chunks), batch_size=4)
    batched = tuple(text for batch in batches for chunk in batch for text in (chunk.text,))
    assert batched == provider_texts
    # Plan metadata'si yalnizca sayi + sabit digest; hassas icerik yok.
    document = plan.as_dict()
    assert document["privacy_excluded_chunk_count"] == 2
    assert document["source_privacy_policy_digest"] == SOURCE_PRIVACY_POLICY_DIGEST
    assert document["chunk_count"] == 3
    assert "gizlilik filtreleme uygulandi" in document["coverage_note"]
    # Reddedilen icerik birimlerinde calisacak gizlilik karari.
    assert knowledge_index._chunk_is_privacy_excluded(f"Mail: {EMAIL}") is True
    assert knowledge_index._chunk_is_privacy_excluded("    " + AUTHOR_LINE) is True
    assert knowledge_index._chunk_is_privacy_excluded("public class App {}") is False


def test_all_excluded_source_fails_closed_without_indexing(tmp_path: Path) -> None:
    root = tmp_path / "gpu"
    root.mkdir(parents=True)
    (root / "Notes.md").write_text(f"iletisim {EMAIL}\n", encoding="utf-8")
    report = discover(root)

    with pytest.raises(ValidationFailed, match="Indekslenebilir"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
        )
