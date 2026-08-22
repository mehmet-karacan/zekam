"""Gercek binary DOCX/PDF/OCR dosyalarinin CLI -> CAS -> DB kabul testi."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from zekam.application.config import DatabaseSettings
from zekam.application.realm_context import attach_realm
from zekam.infrastructure.postgres.connection import connect
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli.main import app

pytest.importorskip("pypdfium2")
pytestmark = [pytest.mark.e2e, pytest.mark.postgres]

FIXTURES = Path(__file__).parents[1] / "fixtures" / "knowledge"
runner = CliRunner()

DOCUMENTS = (
    (
        "docx",
        "heading-table.docx",
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ("digital-pdf", "digital-2page.pdf", "pdf", "application/pdf"),
    ("scanned-pdf", "scanned-2page.pdf", "pdf", "application/pdf"),
    ("ocr-png", "ocr.png", "png", "image/png"),
    ("ocr-jpeg", "ocr.jpg", "jpeg", "image/jpeg"),
    ("ocr-tiff", "ocr-multipage.tiff", "tiff", "image/tiff"),
)


@pytest.fixture
def cli_home(
    tmp_path: Path, migrated_database: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> Path:
    home = tmp_path / "zekam-home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\n"
        "database:\n"
        f"  host: {migrated_database.host}\n"
        f"  port: {migrated_database.port}\n"
        f"  name: {migrated_database.name}\n"
        f"  user: {migrated_database.user}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZEKAM_HOME", str(home))
    initialized = runner.invoke(app, ["init", "--home", str(home)])
    assert initialized.exit_code == 0, initialized.stdout
    return home


def _records(database: DatabaseSettings, *, realm_slug: str) -> dict[str, dict[str, Any]]:
    with connect(database) as connection:
        realm = attach_realm(connection, slug=realm_slug).realm
        with connection.cursor() as cursor:
            cursor.execute(
                "select s.slug, s.source_format, a.media_type, v.revision, v.state,"
                " d.parser_profile, dip.lexical_state, dip.embedding_state,"
                " count(distinct c.id),"
                " jsonb_agg(jsonb_build_object('kind', u.kind, 'body', u.body,"
                "   'locator', u.locator, 'confidence', u.confidence) order by u.unit_order)"
                " from knowledge.source s"
                " join knowledge.source_version v"
                "   on v.realm_id = s.realm_id and v.source_id = s.id"
                " join knowledge.artifact a"
                "   on a.realm_id = v.realm_id and a.id = v.artifact_id"
                " join knowledge.normalized_document d"
                "   on d.realm_id = v.realm_id and d.version_id = v.id"
                " join knowledge.content_unit u"
                "   on u.realm_id = d.realm_id and u.document_id = d.id"
                " join knowledge.document_index_profile dip"
                "   on dip.realm_id = d.realm_id and dip.document_id = d.id"
                " join knowledge.chunk c"
                "   on c.realm_id = d.realm_id and c.document_id = d.id"
                " where s.realm_id = %s"
                " group by s.slug, s.source_format, a.media_type, v.revision, v.state,"
                " d.parser_profile, dip.lexical_state, dip.embedding_state"
                " order by s.slug",
                (realm.id,),
            )
            rows = cursor.fetchall()
    return {
        str(row[0]): {
            "source_format": str(row[1]),
            "media_type": str(row[2]),
            "revision": int(row[3]),
            "state": str(row[4]),
            "profile": dict(row[5]),
            "lexical_state": str(row[6]),
            "embedding_state": str(row[7]),
            "chunk_count": int(row[8]),
            "units": list(row[9]),
        }
        for row in rows
    }


def _pages(record: dict[str, Any]) -> set[int]:
    return {
        int(unit["locator"]["page"])
        for unit in record["units"]
        if unit["locator"].get("page") is not None
    }


def test_real_binary_documents_roundtrip_through_cli_cas_and_database(
    cli_home: Path,
    migrated_database: DatabaseSettings,
) -> None:
    realm_slug = f"offline-docs-{secrets.token_hex(4)}"
    store = LocalContentAddressedStore(cli_home / "global" / "artifacts")
    for slug, filename, source_format, _media_type in DOCUMENTS:
        source = FIXTURES / filename
        result = runner.invoke(
            app,
            [
                "knowledge",
                "ingest",
                str(source),
                "--slug",
                slug,
                "--uygula",
                "--json",
                "--home",
                str(cli_home),
                "--realm",
                realm_slug,
            ],
        )
        assert result.exit_code == 0, f"{filename}: {result.stdout}"
        summary = json.loads(result.stdout)
        assert summary["source_format"] == source_format
        assert summary["state"] == "active"
        assert summary["lexical_index_state"] == "ready"
        assert summary["embedding_state"] == "pending"
        assert store.get(summary["artifact_content_digest"]) == source.read_bytes()

    records = _records(migrated_database, realm_slug=realm_slug)
    assert set(records) == {row[0] for row in DOCUMENTS}
    for slug, _filename, source_format, media_type in DOCUMENTS:
        record = records[slug]
        assert record["source_format"] == source_format
        assert record["media_type"] == media_type
        assert (record["revision"], record["state"]) == (1, "active")
        assert record["lexical_state"] == "ready"
        assert record["embedding_state"] == "pending"
        assert record["chunk_count"] > 0
        assert record["profile"]["schema"] == "zekam-parser-profile/v1"
        assert "/opt/" not in json.dumps(record["profile"], sort_keys=True)

    docx = records["docx"]
    assert docx["profile"]["license_ids"] == ["PSF-2.0"]
    table = next(unit for unit in docx["units"] if unit["kind"] == "table")
    assert table["locator"]["heading_path"] == ["Cevrimdisi Belge", "Tablo Bolumu"]
    assert all(unit["locator"].get("page") is None for unit in docx["units"])

    digital = records["digital-pdf"]
    scanned = records["scanned-pdf"]
    for pdf in (digital, scanned):
        assert pdf["profile"]["package_license"] == "Apache-2.0 OR BSD-3-Clause"
        assert pdf["profile"]["engine_license"] == "BSD-3-Clause"
        assert pdf["profile"]["license_gate"] == "exact-wheel-build-licenses-allowlisted"
        assert pdf["profile"]["build_flags"] == []
        assert _pages(pdf) == {1, 2}
    assert {unit["kind"] for unit in digital["units"]} == {"paragraph"}
    assert {unit["kind"] for unit in scanned["units"]} == {"ocr-block"}
    assert all(unit["confidence"] is not None for unit in scanned["units"])

    for slug in ("ocr-png", "ocr-jpeg", "ocr-tiff"):
        record = records[slug]
        assert record["profile"]["engine_license"] == "Apache-2.0"
        assert record["profile"]["image_library_license"] == "BSD-2-Clause"
        assert set(record["profile"]["tessdata_digests"]) == {"tur", "eng"}
        assert {unit["kind"] for unit in record["units"]} == {"ocr-block"}
        assert all(unit["confidence"] is not None for unit in record["units"])
    assert _pages(records["ocr-png"]) == {1}
    assert _pages(records["ocr-jpeg"]) == {1}
    assert _pages(records["ocr-tiff"]) == {1, 2}
