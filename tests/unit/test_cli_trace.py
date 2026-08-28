from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from zekam.application.mutation_admission import ActiveRuntimeContinuityIdentity
from zekam.interfaces.cli import trace as trace_commands


def test_trace_start_persists_resolved_runtime_identity(monkeypatch) -> None:
    realm_id = UUID("00000000-0000-0000-0000-000000000001")
    identity = ActiveRuntimeContinuityIdentity(
        realm_id=realm_id,
        project_id=UUID("00000000-0000-0000-0000-000000000002"),
        work_item_id=UUID("00000000-0000-0000-0000-000000000003"),
        run_id=UUID("00000000-0000-0000-0000-000000000004"),
        session_id="session-exact",
        client_id="codex",
    )
    stored = []

    class FakeSession:
        resolved_runtime_identity = identity

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return SimpleNamespace(realm_id=realm_id, connection=object())

        def __exit__(self, *_args):
            return None

    class FakeRepository:
        def __init__(self, _connection, _realm_id) -> None:
            pass

        def create_bundle(self, bundle) -> None:
            stored.append(bundle)

    monkeypatch.setattr(trace_commands, "RealmSession", FakeSession)
    monkeypatch.setattr(trace_commands, "PostgresDiagnosticTraceRepository", FakeRepository)
    monkeypatch.setattr(trace_commands, "_print", lambda *_args, **_kwargs: None)

    trace_commands.start_trace(
        trace_ref="trace-exact",
        client_session="session-exact",
        encryption_key_ref="secret-ref",
        apply=True,
        retention_days=7,
        project_id=None,
        work_item_id=None,
        run_id=None,
        root_assignment_id=None,
        as_json=True,
        realm="yerel",
        home=None,
    )

    assert len(stored) == 1
    bundle = stored[0]
    assert bundle.project_id == identity.project_id
    assert bundle.work_item_id == identity.work_item_id
    assert bundle.run_id == identity.run_id
    assert bundle.root_client_session_id == identity.session_id
