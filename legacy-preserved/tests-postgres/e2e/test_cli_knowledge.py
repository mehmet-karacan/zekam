"""`zekam knowledge` uctan uca akisi."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.config import DatabaseSettings
from zekam.application.realm_context import attach_realm
from zekam.infrastructure.postgres.connection import connect
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli.main import app

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]

runner = CliRunner()
DOCUMENT = "# Baslik\n\nBirinci paragraf.\n\n## Alt baslik\n\nIkinci paragraf.\n"


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
    runner.invoke(app, ["init", "--home", str(home)])
    return home


@pytest.fixture
def realm_flags() -> list[str]:
    return ["--realm", f"knowledge-{secrets.token_hex(4)}"]


@pytest.fixture
def document(tmp_path: Path) -> Path:
    target = tmp_path / "rapor.md"
    target.write_text(DOCUMENT, encoding="utf-8", newline="\n")
    return target


def test_scan_secret_dosyayi_disarida_birakir(tmp_path: Path) -> None:
    root = tmp_path / "kaynak"
    root.mkdir()
    (root / "not.md").write_text("# not\n", encoding="utf-8")
    (root / ".env").write_text("PAROLA=gizli\n", encoding="utf-8")
    result = runner.invoke(app, ["knowledge", "scan", str(root), "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    included = [item["path"] for item in payload["decisions"] if item["included"]]
    assert included == ["not.md"]


def test_ingest_uygula_olmadan_yazmaz(
    cli_home: Path, realm_flags: list[str], document: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "knowledge",
            "ingest",
            str(document),
            "--slug",
            "rapor",
            "--json",
            "--home",
            str(cli_home),
            *realm_flags,
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["unit_count"] >= 4
    assert payload["source_format"] == "markdown"


def test_ingest_uygula_aktif_surum_uretir(
    cli_home: Path, realm_flags: list[str], document: Path
) -> None:
    arguments = [
        "knowledge",
        "ingest",
        str(document),
        "--slug",
        "rapor",
        "--uygula",
        "--json",
        "--home",
        str(cli_home),
        *realm_flags,
    ]
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert payload["state"] == "active"

    # Orijinal payload yalniz metadata olarak degil Local CAS'ta exact byte ile bulunur.
    store = LocalContentAddressedStore(cli_home / "global" / "artifacts")
    assert store.get(payload["artifact_content_digest"]) == document.read_bytes()

    # Ayni belge yeniden ingest edilince yeni kayit uretilmez.
    again = runner.invoke(app, arguments)
    assert again.exit_code == 0, again.stdout
    assert json.loads(again.stdout)["content_digest"] == payload["content_digest"]


def test_pdf_parser_kurulu_degilse_fail_closed_reddedilir(
    cli_home: Path, realm_flags: list[str], tmp_path: Path
) -> None:
    target = tmp_path / "belge.pdf"
    target.write_bytes(b"%PDF-1.7")
    result = runner.invoke(
        app,
        [
            "knowledge",
            "ingest",
            str(target),
            "--slug",
            "pdf",
            "--home",
            str(cli_home),
            *realm_flags,
        ],
    )
    assert result.exit_code != 0
    assert "Uzanti icin parser tanimli degil" not in result.stdout


def test_same_bytes_different_slug_are_independent_sources(
    cli_home: Path, realm_flags: list[str], document: Path
) -> None:
    for slug in ("rapor-a", "rapor-b"):
        result = runner.invoke(
            app,
            [
                "knowledge",
                "ingest",
                str(document),
                "--slug",
                slug,
                "--uygula",
                "--json",
                "--home",
                str(cli_home),
                *realm_flags,
            ],
        )
        assert result.exit_code == 0, result.stdout
    store = LocalContentAddressedStore(cli_home / "global" / "artifacts")
    assert len(tuple(store.iter_objects())) == 1


def test_changed_bytes_create_revision_two_and_supersede_first(
    cli_home: Path,
    realm_flags: list[str],
    document: Path,
    migrated_database: DatabaseSettings,
) -> None:
    arguments = [
        "knowledge",
        "ingest",
        str(document),
        "--slug",
        "surumlu",
        "--uygula",
        "--json",
        "--home",
        str(cli_home),
        *realm_flags,
    ]
    first = runner.invoke(app, arguments)
    assert first.exit_code == 0, first.stdout
    document.write_text(DOCUMENT + "\nYeni icerik.\n", encoding="utf-8")
    second = runner.invoke(app, arguments)
    assert second.exit_code == 0, second.stdout
    assert json.loads(second.stdout)["revision"] == 2

    with connect(migrated_database) as connection:
        realm = attach_realm(connection, slug=realm_flags[-1]).realm
        with connection.cursor() as cursor:
            cursor.execute(
                "select v.revision, v.state, d.parser_profile, a.media_type,"
                " cp.profile_digest, ep.profile_digest, dip.lexical_state,"
                " dip.embedding_state, count(c.id)"
                " from knowledge.source s"
                " join knowledge.source_version v on v.source_id = s.id"
                " join knowledge.normalized_document d on d.version_id = v.id"
                " join knowledge.artifact a on a.id = v.artifact_id"
                " join knowledge.document_index_profile dip on dip.document_id = d.id"
                " join knowledge.chunk_profile cp on cp.id = dip.chunk_profile_id"
                " join knowledge.embedding_profile ep on ep.id = dip.embedding_profile_id"
                " join knowledge.chunk c on c.document_id = d.id"
                " where s.realm_id = %s and s.slug = 'surumlu'"
                " group by v.revision, v.state, d.parser_profile, a.media_type,"
                " cp.profile_digest, ep.profile_digest, dip.lexical_state,"
                " dip.embedding_state order by v.revision",
                (realm.id,),
            )
            rows = cursor.fetchall()
    assert [(row[0], row[1]) for row in rows] == [(1, "superseded"), (2, "active")]
    assert all(row[2]["schema"] == "zekam-parser-profile/v1" for row in rows)
    assert all(row[3] == "text/markdown" for row in rows)
    assert all(str(row[4]).startswith("sha256:") for row in rows)
    assert all(str(row[5]).startswith("sha256:") for row in rows)
    assert all(row[6:8] == ("ready", "pending") for row in rows)
    assert all(row[8] > 0 for row in rows)
