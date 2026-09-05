from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from zekam.domain.canonical import digest
from zekam.interfaces.cli import worker


class _Session:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.context = SimpleNamespace(connection=object(), realm=object())

    def __enter__(self) -> object:
        return self.context

    def __exit__(self, *args: object) -> None:
        return None


class _Spool:
    entry = SimpleNamespace(
        internal_event_type="session_start",
        session_id="session-1",
        entry_digest=digest("pending-session-start"),
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def pending(self, *, limit: int) -> list[object]:
        assert limit == 1
        return [self.entry]


class _Plan:
    plan_digest = digest("rebootstrap-plan")

    def as_dict(self) -> dict[str, object]:
        return {"plan_digest": self.plan_digest, "rebootstrap": True, "applied": False}


class _Service:
    prepared: ClassVar[dict[str, object]] = {}

    def __init__(self, *args: object) -> None:
        pass

    def prepare(self, **kwargs: object) -> _Plan:
        self.__class__.prepared = kwargs
        return _Plan()


def test_client_runtime_bootstrap_cli_passes_explicit_rebootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "RealmSession", _Session)
    monkeypatch.setattr(worker, "ClientLifecycleSpool", _Spool)
    monkeypatch.setattr(worker, "ClientRuntimeBootstrapService", _Service)
    monkeypatch.setattr(worker, "resolve_home", lambda home: object())
    monkeypatch.setattr(worker, "_bootstrap_source_revision", lambda home: "git:fresh")

    result = CliRunner().invoke(
        worker.app,
        [
            "client-runtime-bootstrap",
            "--work-id",
            str(uuid4()),
            "--project-id",
            str(uuid4()),
            "--actor-id",
            str(uuid4()),
            "--rebootstrap",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert _Service.prepared["rebootstrap"] is True
    assert _Service.prepared["source_revision"] == "git:fresh"
    assert '"rebootstrap": true' in result.stdout
