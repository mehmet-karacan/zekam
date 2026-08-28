from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from zekam.interfaces.cli import worker


class _Session:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.context = SimpleNamespace(connection=object(), realm=object())

    def __enter__(self) -> object:
        return self.context

    def __exit__(self, *args: object) -> None:
        return None


class _Plan:
    plan_digest = "sha256:" + "1" * 64

    def as_dict(self) -> dict[str, object]:
        return {"plan_digest": self.plan_digest, "applied": False, "provider_calls": 0}


class _Service:
    applied = False

    def __init__(self, *args: object) -> None:
        pass

    def prepare(self, **kwargs: object) -> _Plan:
        return _Plan()

    def apply(self, plan: _Plan, *, supplied_plan_digest: str) -> dict[str, object]:
        assert supplied_plan_digest == plan.plan_digest
        self.__class__.applied = True
        return {"applied": True, "provider_calls": 0, "network_calls": 0}


def _args() -> list[str]:
    return [
        "lifecycle-template-prepare",
        "--work-id",
        str(uuid4()),
        "--project-id",
        str(uuid4()),
        "--actor-id",
        str(uuid4()),
        "--source-revision",
        "git:reviewed",
        "--json",
    ]


def test_lifecycle_template_prepare_cli_is_dry_run_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "RealmSession", _Session)
    monkeypatch.setattr(worker, "LifecycleRuntimeTemplatePrepareService", _Service)
    result = CliRunner().invoke(worker.app, _args())
    assert result.exit_code == 0
    assert '"applied": false' in result.stdout
    assert '"provider_calls": 0' in result.stdout


def test_lifecycle_template_prepare_cli_apply_requires_exact_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "RealmSession", _Session)
    monkeypatch.setattr(worker, "LifecycleRuntimeTemplatePrepareService", _Service)
    _Service.applied = False
    args = [*_args(), "--uygula", "--plan-digest", _Plan.plan_digest]
    result = CliRunner().invoke(worker.app, args)
    assert result.exit_code == 0
    assert _Service.applied
    assert '"network_calls": 0' in result.stdout
