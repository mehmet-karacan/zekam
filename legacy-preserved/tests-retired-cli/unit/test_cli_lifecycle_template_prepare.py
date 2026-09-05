from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from zekam.application.lifecycle_runtime_template_prepare import (
    LifecycleTemplatePreparePlan,
)
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


def _real_plan(
    *, now: dt.datetime, work_revision: int = 3, source: str = "git:a"
) -> LifecycleTemplatePreparePlan:
    return LifecycleTemplatePreparePlan(
        realm_id=uuid4(),
        project_id=uuid4(),
        work_item_id=uuid4(),
        work_revision=work_revision,
        actor_id=uuid4(),
        source_revision=source,
        policy_digest="sha256:" + "2" * 64,
        adopt_existing=False,
        prepared_at=now,
        expires_at=now + dt.timedelta(minutes=30),
    )


def test_lifecycle_template_prepare_digest_excludes_volatile_timestamps() -> None:
    first = _real_plan(now=dt.datetime(2026, 8, 29, 10, tzinfo=dt.UTC))
    second = replace(
        first,
        prepared_at=first.prepared_at + dt.timedelta(minutes=5),
        expires_at=first.expires_at + dt.timedelta(minutes=5),
    )
    assert first.plan_digest == second.plan_digest
    assert first.as_dict()["prepared_at"] != second.as_dict()["prepared_at"]


def test_lifecycle_template_prepare_digest_tracks_source_and_work_revision() -> None:
    first = _real_plan(now=dt.datetime(2026, 8, 29, 10, tzinfo=dt.UTC))
    source_drift = replace(first, source_revision="git:b")
    work_drift = replace(first, work_revision=first.work_revision + 1)
    assert first.plan_digest != source_drift.plan_digest
    assert first.plan_digest != work_drift.plan_digest
