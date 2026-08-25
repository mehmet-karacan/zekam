"""OpenCode lifecycle forward CLI orchestration tests."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import zekam.interfaces.cli.opencode as cli_module
from zekam.application.opencode_lifecycle import lifecycle_root, recent_events, record_event
from zekam.infrastructure.postgres.client_lifecycle_repository import LifecycleAck


def test_forward_writes_local_ack_only_after_canonical_ingest(tmp_path, monkeypatch) -> None:
    event = record_event(
        tmp_path,
        event_type="session.created",
        session_id="ses_cli",
        now=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    ).document()
    canonical = "sha256:" + "c" * 64

    class FakeSession:
        def __enter__(self):
            return SimpleNamespace(connection=object(), realm_id=uuid4())

        def __exit__(self, *_args):
            return None

    class FakeRepository:
        def __init__(self, _connection, _realm_id) -> None:
            pass

        def ingest(self, document, *, client_instance_id):
            assert document == event
            assert client_instance_id.startswith("opencode-")
            return LifecycleAck(
                uuid4(), event["event_digest"], canonical, dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
            )

    monkeypatch.setattr(cli_module, "RealmSession", lambda *_args, **_kwargs: FakeSession())
    monkeypatch.setattr(cli_module, "ClientLifecycleRepository", FakeRepository)
    monkeypatch.setattr(cli_module, "resolve_home", lambda _home: Path(tmp_path))

    cli_module.forward_command(limit=80, realm="test", home=str(tmp_path))

    ack_name = event["event_digest"].removeprefix("sha256:") + ".json"
    assert (lifecycle_root(tmp_path) / "acked" / ack_name).is_file()


def test_forward_batches_oldest_unacked_chain_when_backlog_exceeds_limit(
    tmp_path, monkeypatch
) -> None:
    for index in range(81):
        record_event(
            tmp_path,
            event_type="tool.execute.before",
            session_id="ses_backlog",
            tool=f"tool-{index}",
            now=dt.datetime(2026, 8, 24, tzinfo=dt.UTC) + dt.timedelta(seconds=index),
        )
    forwarded: list[int] = []

    class FakeSession:
        def __enter__(self):
            return SimpleNamespace(connection=object(), realm_id=uuid4())

        def __exit__(self, *_args):
            return None

    class FakeRepository:
        def __init__(self, _connection, _realm_id) -> None:
            pass

        def ingest(self, document, *, client_instance_id):
            forwarded.append(int(document["sequence"]))
            return LifecycleAck(
                uuid4(),
                document["event_digest"],
                "sha256:" + f"{document['sequence']:064x}",
                dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
            )

    monkeypatch.setattr(cli_module, "RealmSession", lambda *_args, **_kwargs: FakeSession())
    monkeypatch.setattr(cli_module, "ClientLifecycleRepository", FakeRepository)
    monkeypatch.setattr(cli_module, "resolve_home", lambda _home: Path(tmp_path))

    cli_module.forward_command(limit=80, realm="test", home=str(tmp_path))
    assert forwarded == list(range(1, 81))
    forwarded.clear()
    cli_module.forward_command(limit=80, realm="test", home=str(tmp_path))
    assert forwarded == [81]


def test_pre_compact_command_requires_and_persists_canonical_outbox_ack(
    tmp_path, monkeypatch
) -> None:
    outbox_id = uuid4()
    payload_digest = "sha256:" + "d" * 64
    ingested: list[dict[str, object]] = []

    class FakeSession:
        def __enter__(self):
            return SimpleNamespace(connection=object(), realm_id=uuid4())

        def __exit__(self, *_args):
            return None

    class FakeRepository:
        def __init__(self, _connection, _realm_id) -> None:
            pass

        def ingest(self, document, *, client_instance_id):
            assert client_instance_id.startswith("opencode-")
            ingested.append(document)
            return LifecycleAck(
                uuid4(),
                str(document["event_digest"]),
                "sha256:" + "c" * 64,
                dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
                outbox_id,
                payload_digest,
            )

    monkeypatch.setattr(cli_module, "RealmSession", lambda *_args, **_kwargs: FakeSession())
    monkeypatch.setattr(cli_module, "ClientLifecycleRepository", FakeRepository)
    monkeypatch.setattr(cli_module, "resolve_home", lambda _home: Path(tmp_path))

    cli_module.pre_compact_command(session_id="ses_pre", realm="test", home=str(tmp_path))

    assert len(ingested) == 1
    assert ingested[0]["event_type"] == "session.compacting"
    assert recent_events(tmp_path, limit=1)[0]["event_type"] == "session.compacting"
    ack_name = str(ingested[0]["event_digest"]).removeprefix("sha256:") + ".json"
    assert (lifecycle_root(tmp_path) / "acked" / ack_name).is_file()
