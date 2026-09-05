from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
import typer

from zekam.application.diagnostics import OverallStatus, Severity
from zekam.application.mutation_admission import CLI_MUTATION_REGISTRY_META_KEY
from zekam.application.setup import setup_plan_digest
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.interfaces.cli import main as cli


class _Console:
    def __init__(self) -> None:
        self.printed: list[object] = []
        self.json: list[str] = []

    def print(self, value: object = "") -> None:
        self.printed.append(value)

    def print_json(self, value: str) -> None:
        self.json.append(value)


@dataclass(frozen=True)
class _Step:
    step_id: str
    description: str
    argv: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"step_id": self.step_id, "description": self.description, "argv": self.argv}


def _steps(**_kwargs: object) -> tuple[_Step, ...]:
    return (
        _Step("windows-git-ca", "Windows compatibility", ("git", "config")),
        _Step("local-init", "Local init", ("init",)),
    )


def test_setup_renders_json_and_table_dry_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _Console()
    monkeypatch.setattr(cli, "console", output)
    monkeypatch.setattr(cli, "build_setup_plan", _steps)
    cli.setup(apply=False, output_json=True)
    assert '"apply": false' in output.json[0]
    output = _Console()
    monkeypatch.setattr(cli, "console", output)
    cli.setup(apply=False, output_json=False)
    assert any("Dry-run" in str(value) for value in output.printed)


def test_setup_apply_uses_exact_argv_and_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _Console()
    calls: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli, "console", output)
    monkeypatch.setattr(cli, "build_setup_plan", _steps)
    monkeypatch.setattr(subprocess, "run", run)
    plan_digest = setup_plan_digest(cast(Any, _steps()))
    cli.setup(apply=True, output_json=False, plan_digest=plan_digest)
    assert calls[0] == ("git", "config")
    assert calls[1][-1] == "init"
    assert len([value for value in output.printed if "Tamam" in str(value)]) == 2
    output = _Console()
    monkeypatch.setattr(cli, "console", output)
    cli.setup(apply=True, output_json=True, plan_digest=plan_digest)
    assert '"status": "completed"' in output.json[0]


@pytest.mark.parametrize("output_json", (False, True))
def test_setup_apply_stops_on_first_failure_with_stable_receipt(
    monkeypatch: pytest.MonkeyPatch, output_json: bool
) -> None:
    output = _Console()
    errors = _Console()
    monkeypatch.setattr(cli, "console", output)
    monkeypatch.setattr(cli, "error_console", errors)
    monkeypatch.setattr(cli, "build_setup_plan", _steps)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=7))
    plan_digest = setup_plan_digest(cast(Any, _steps()))
    with pytest.raises(typer.Exit) as raised:
        cli.setup(apply=True, output_json=output_json, plan_digest=plan_digest)
    assert raised.value.exit_code == 7
    if output_json:
        assert '"status": "failed"' in output.json[0]
    else:
        assert "windows-git-ca" in str(errors.printed[0])


def test_opencode_resolution_prefers_config_then_path(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = SimpleNamespace(name="OpenCode", executable=Path("/safe/opencode"))
    context = SimpleNamespace(settings=SimpleNamespace(clients=(configured,)))
    assert cli._opencode_executable(cast(Any, context)) == Path("/safe/opencode")
    context = SimpleNamespace(settings=SimpleNamespace(clients=()))
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/opencode")
    assert cli._opencode_executable(cast(Any, context)) == Path("/usr/local/bin/opencode")
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert cli._opencode_executable(cast(Any, context)) is None


def test_render_report_covers_findings_and_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _Console()
    monkeypatch.setattr(cli, "console", output)
    result = SimpleNamespace(check_id="check", status=SimpleNamespace(value="ok"), summary="safe")
    finding = SimpleNamespace(
        severity=Severity.CRITICAL,
        code="CRITICAL_FINDING",
        title="Title",
        detail="Detail",
        next_action="Review",
        authority_required=True,
    )
    report = SimpleNamespace(results=(result,), findings=(finding,), overall=OverallStatus.BLOCKED)
    cli._render_report(cast(Any, report))
    assert any("Yetki gerekir" in str(value) for value in output.printed)


@pytest.mark.parametrize("action", (None, "repair"))
def test_render_repair_plan_handles_noop_and_action(
    monkeypatch: pytest.MonkeyPatch, action: str | None
) -> None:
    output = _Console()
    monkeypatch.setattr(cli, "console", output)
    cli._render_doctor_repair_plan(
        {"plan_digest": "sha256:" + "a" * 64, "action": action, "blocked_reasons": []}
    )
    command_lines = [value for value in output.printed if "--plan-digest" in str(value)]
    assert bool(command_lines) is (action is not None)


def test_version_callback_false_is_noop_and_true_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _Console()
    monkeypatch.setattr(cli, "console", output)
    cli._version_callback(False)
    with pytest.raises(typer.Exit) as raised:
        cli._version_callback(True)
    assert raised.value.exit_code == 0
    assert output.printed


def test_main_publishes_mutation_registry_to_root_context() -> None:
    root = SimpleNamespace(meta={})
    context = SimpleNamespace(find_root=Mock(return_value=root))
    cli.main(cast(Any, context), version=False)
    assert CLI_MUTATION_REGISTRY_META_KEY in root.meta


class _Report:
    overall = OverallStatus.HEALTHY

    def as_dict(self) -> dict[str, object]:
        return {"overall": "healthy"}


class _Doctor:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, categories: object = None) -> _Report:
        del categories
        self.calls += 1
        return _Report()


class _LocalServices:
    def __init__(self, action: str | None) -> None:
        self.action = action
        self.applied: list[str] = []

    def repair_plan(self) -> dict[str, object]:
        return {
            "plan_digest": "sha256:" + "a" * 64,
            "action": self.action,
            "blocked_reasons": [],
        }

    def apply_repair(self, value: str) -> dict[str, object]:
        self.applied.append(value)
        return {"step": "repair", "receipt_id": "receipt"}


def _doctor_dependencies(
    monkeypatch: pytest.MonkeyPatch, *, action: str | None
) -> tuple[_Console, _Console, _Doctor, _LocalServices]:
    output = _Console()
    errors = _Console()
    doctor = _Doctor()
    local = _LocalServices(action)
    monkeypatch.setattr(cli, "console", output)
    monkeypatch.setattr(cli, "error_console", errors)
    monkeypatch.setattr(cli, "build_context", lambda **_: object())
    monkeypatch.setattr(cli, "build_doctor", lambda _: doctor)
    monkeypatch.setattr(LocalCoreServices, "from_context", lambda _: local)
    monkeypatch.setattr(cli, "_render_report", lambda _: None)
    monkeypatch.setattr(cli, "_render_doctor_repair_plan", lambda _: None)
    return output, errors, doctor, local


def test_doctor_rejects_conflicting_prepare_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    _, errors, _, _ = _doctor_dependencies(monkeypatch, action="repair")
    with pytest.raises(typer.Exit) as raised:
        cli.doctor(prepare=True, apply=True)
    assert raised.value.exit_code == cli.EXIT_RUNTIME_ERROR
    assert errors.printed


def test_doctor_prepare_applies_one_action_and_emits_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output, _, doctor, local = _doctor_dependencies(monkeypatch, action="repair")
    with pytest.raises(typer.Exit) as raised:
        cli.doctor(prepare=True, output_json=True)
    assert raised.value.exit_code == 0
    assert local.applied == ["sha256:" + "a" * 64]
    assert doctor.calls == 2
    assert "doctor_prepare_results" in output.json[0]


def test_doctor_apply_requires_digest_then_emits_plain_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, _ = _doctor_dependencies(monkeypatch, action="repair")
    with pytest.raises(typer.Exit) as raised:
        cli.doctor(apply=True)
    assert raised.value.exit_code == cli.EXIT_RUNTIME_ERROR

    output, _, doctor, local = _doctor_dependencies(monkeypatch, action="repair")
    plan_digest = "sha256:" + "b" * 64
    with pytest.raises(typer.Exit) as raised:
        cli.doctor(apply=True, plan_digest=plan_digest)
    assert raised.value.exit_code == 0
    assert local.applied == [plan_digest]
    assert doctor.calls == 2
    assert any("Onarim dogrulandi" in str(value) for value in output.printed)


def test_doctor_repair_plan_without_action_is_rendered_without_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered: list[dict[str, object]] = []
    _, _, _, local = _doctor_dependencies(monkeypatch, action=None)
    monkeypatch.setattr(cli, "_render_doctor_repair_plan", rendered.append)
    with pytest.raises(typer.Exit) as raised:
        cli.doctor(repair_plan=True)
    assert raised.value.exit_code == 0
    assert rendered and local.applied == []
