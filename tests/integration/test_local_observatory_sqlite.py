"""Real SQLite projection composition for the observatory and App Server."""

from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from typer.testing import CliRunner

from zekam.application.composition import build_context
from zekam.domain.observability import REQUIRED_TILES
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.infrastructure.sqlite.local_observatory import (
    SQLiteLocalProjectionStore,
    SQLiteRuntimeProjectionReader,
)
from zekam.interfaces.api.observatory import _app_server_store_factory
from zekam.interfaces.cli.main import app


def test_real_local_projection_reader_and_store_survive_reopen(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert CliRunner().invoke(app, ["init", "--home", str(home)]).exit_code == 0
    context = build_context(home=str(home))
    realm_id = uuid5(NAMESPACE_URL, "zekam://realm/yerel")

    projection = SQLiteRuntimeProjectionReader(context, realm_id).read()
    assert projection.available is True
    assert projection.detail == "local-core-sqlite"
    assert tuple(tile.key for tile in projection.tiles) == REQUIRED_TILES

    with SQLiteLocalProjectionStore(LocalCoreServices.from_context(context), realm_id) as store:
        assert store.head_sequence() == 0
        assert store.cursor_exists(0) is True
        assert store.replay(after_sequence=0, limit=10) == ()
    factory = _app_server_store_factory(context, realm_id)
    assert callable(factory)
    with factory() as store:
        assert isinstance(store, SQLiteLocalProjectionStore)


def test_real_local_projection_fails_closed_on_corrupt_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert CliRunner().invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "state" / "learning.db").write_bytes(b"corrupt")
    context = build_context(home=str(home))
    projection = SQLiteRuntimeProjectionReader(
        context, uuid5(NAMESPACE_URL, "zekam://realm/yerel")
    ).read()
    assert projection.available is False
    assert projection.detail == "local-core-unavailable"
