"""OpenCode lifecycle forward CLI orchestration tests."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import zekam.interfaces.cli.opencode as cli_module
from zekam.application.opencode_lifecycle import lifecycle_root, recent_events, record_event
from zekam.infrastructure.postgres.client_lifecycle_repository import LifecycleAck


def test_event_command_propagates_delivery_id_for_exact_replay(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "resolve_home", lambda _home: Path(tmp_path))

    for _ in range(2):
        cli_module.event_command(
            event_type="session.created",
            session_id="ses_cli_delivery",
            delivery_id="delivery-cli-one",
            parent_session_id=None,
            agent=None,
            model_ref=None,
            tool=None,
            resource=None,
            status=None,
            error_category=None,
            completed_summary=None,
            pending_summary=None,
            next_action=None,
            task_label=None,
            home=str(tmp_path),
        )

    events = recent_events(tmp_path)
    assert len(events) == 1
    assert events[0]["delivery_id"] == "delivery-cli-one"


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
    monkeypatch.setattr(cli_module, "assert_cli_invocation_backend", lambda *_args: None)

    cli_module.forward_command(limit=80, realm="test", home=str(tmp_path))

    ack_name = event["event_digest"].removeprefix("sha256:") + ".json"
    assert (lifecycle_root(tmp_path) / "acked" / ack_name).is_file()


def test_forward_admits_and_commits_each_immutable_event_before_local_ack(
    tmp_path, monkeypatch
) -> None:
    record_event(
        tmp_path,
        event_type="session.created",
        session_id="ses_event_admission",
        now=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    )
    record_event(
        tmp_path,
        event_type="session.status",
        session_id="ses_event_admission",
        status="running",
        now=dt.datetime(2026, 8, 24, tzinfo=dt.UTC) + dt.timedelta(seconds=1),
    )
    order: list[str] = []
    admissions = []

    class FakeSession:
        def __init__(self, invocation) -> None:
            admissions.append(invocation.admission)

        def __enter__(self):
            order.append("enter")
            return SimpleNamespace(connection=object(), realm_id=uuid4())

        def __exit__(self, *_args):
            order.append("commit")
            return None

    class FakeRepository:
        def __init__(self, _connection, _realm_id) -> None:
            pass

        def ingest(self, document, *, client_instance_id):
            order.append(f"ingest-{document['sequence']}")
            return LifecycleAck(
                uuid4(),
                str(document["event_digest"]),
                "sha256:" + f"{int(document['sequence']):064x}",
                dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
            )

    def fake_session(_home, _realm, *, invocation):
        return FakeSession(invocation)

    def fake_ack(_home, document):
        order.append(f"ack-{document['canonical_digest'][-1]}")

    monkeypatch.setattr(cli_module, "RealmSession", fake_session)
    monkeypatch.setattr(cli_module, "ClientLifecycleRepository", FakeRepository)
    monkeypatch.setattr(cli_module, "record_canonical_ack", fake_ack)
    monkeypatch.setattr(cli_module, "resolve_home", lambda _home: Path(tmp_path))
    monkeypatch.setattr(cli_module, "assert_cli_invocation_backend", lambda *_args: None)

    cli_module.forward_command(limit=80, realm="test", home=str(tmp_path))

    assert order == ["enter", "ingest-1", "commit", "ack-1", "enter", "ingest-2", "commit", "ack-2"]
    assert admissions[0].exemption is not None
    assert not admissions[0].requires_existing_hydration
    assert admissions[1].exemption is None
    assert admissions[1].requires_existing_hydration
    assert all(not item.grants_authority for item in admissions)


def test_forward_ack_crash_replays_canonical_receipt_without_losing_event(
    tmp_path, monkeypatch
) -> None:
    event = record_event(
        tmp_path,
        event_type="session.created",
        session_id="ses_ack_replay",
        now=dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
    ).document()
    canonical = "sha256:" + "e" * 64
    ingest_calls = 0

    class FakeSession:
        def __enter__(self):
            return SimpleNamespace(connection=object(), realm_id=uuid4())

        def __exit__(self, *_args):
            return None

    class FakeRepository:
        def __init__(self, _connection, _realm_id) -> None:
            pass

        def ingest(self, document, *, client_instance_id):
            nonlocal ingest_calls
            ingest_calls += 1
            assert document == event
            return LifecycleAck(
                uuid4(),
                str(document["event_digest"]),
                canonical,
                dt.datetime(2026, 8, 24, tzinfo=dt.UTC),
            )

    real_ack = cli_module.record_canonical_ack
    ack_attempts = 0

    def crash_once(home, document):
        nonlocal ack_attempts
        ack_attempts += 1
        if ack_attempts == 1:
            raise OSError("simulated local ACK crash")
        real_ack(home, document)

    monkeypatch.setattr(cli_module, "RealmSession", lambda *_args, **_kwargs: FakeSession())
    monkeypatch.setattr(cli_module, "ClientLifecycleRepository", FakeRepository)
    monkeypatch.setattr(cli_module, "record_canonical_ack", crash_once)
    monkeypatch.setattr(cli_module, "resolve_home", lambda _home: Path(tmp_path))
    monkeypatch.setattr(cli_module, "assert_cli_invocation_backend", lambda *_args: None)

    with pytest.raises(OSError, match="ACK crash"):
        cli_module.forward_command(limit=80, realm="test", home=str(tmp_path))
    cli_module.forward_command(limit=80, realm="test", home=str(tmp_path))

    ack_name = event["event_digest"].removeprefix("sha256:") + ".json"
    assert (lifecycle_root(tmp_path) / "acked" / ack_name).is_file()
    assert ingest_calls == 2
    assert ack_attempts == 2


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
    monkeypatch.setattr(cli_module, "assert_cli_invocation_backend", lambda *_args: None)

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
