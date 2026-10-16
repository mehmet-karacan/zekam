from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.mutation_admission import (
    DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY,
    MutationAdmissionExemption,
    _issue_gate_a_source_capability,
    assert_local_effect_admission,
)
from zekam.domain.errors import PolicyViolation
from zekam.interfaces.cli.main import app


def test_gate_a_capability_cannot_be_issued_outside_exact_cli_leaf() -> None:
    with pytest.raises(PolicyViolation):
        _issue_gate_a_source_capability(("continuity", "source-bind"), confirmed=True)


@pytest.mark.parametrize("command", ["start", "checkpoint", "close"])
def test_native_record_commands_are_explicit_local_effects(command: str) -> None:
    path = ("continuity", "native", command)
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        path,
        {"project": "fixture", "work_item": "work-ref", "client": "codex"},
    )
    assert admission.mutating
    assert admission.exemption is MutationAdmissionExemption.LOCAL_EFFECT
    assert not admission.requires_existing_hydration
    assert not admission.grants_authority
    assert admission.target_hints.work_ref == "work-ref"
    assert admission.target_hints.project_ref == "fixture"
    assert assert_local_effect_admission(path).mutating


def test_native_resume_remains_read_only_in_admission_registry() -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(("continuity", "native", "resume"))
    assert not admission.mutating
    assert admission.exemption is None


@pytest.mark.parametrize("command", ["evidence-bundle", "apply-admission"])
def test_native_publication_apply_is_explicit_local_effect_and_preview_is_read_only(command):
    path = ("git", "publication", command)
    preview = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(path, {"apply": False})
    effect = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(path, {"apply": True})
    assert not preview.mutating and preview.exemption is None
    assert effect.mutating and effect.exemption is MutationAdmissionExemption.LOCAL_EFFECT
    assert not effect.requires_existing_hydration and not effect.grants_authority


@pytest.mark.parametrize("command", ["evaluation-record", "review-record"])
def test_skill_evidence_record_requires_local_effect_admission(command: str) -> None:
    path = ("skill", command)
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(path, {"apply": True})
    assert admission.mutating
    assert admission.exemption is MutationAdmissionExemption.LOCAL_EFFECT
    assert not admission.grants_authority
    assert not DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(path, {"apply": False}).mutating


@pytest.mark.parametrize(
    "command", ["trial-run", "audit-run", "aggregate-record", "evaluation-from-audits"]
)
def test_skill_trial_has_explicit_canonical_control_plane_admission(command: str) -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("skill", command), {"apply": True}
    )
    assert admission.mutating
    assert admission.exemption is MutationAdmissionExemption.CONTROL_PLANE
    assert not admission.grants_authority


def test_active_local_mutations_remain_explicit(tmp_path: Path) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    source = tmp_path / "source"
    source.mkdir()
    dry_run = runner.invoke(app, ["project", "add", str(source), "--home", str(home)])
    assert dry_run.exit_code == 0
    listed = runner.invoke(app, ["project", "list", "--home", str(home), "--json"])
    assert listed.exit_code == 0
    assert json.loads(listed.stdout) == []
    applied = runner.invoke(
        app,
        ["project", "add", str(source), "--home", str(home), "--uygula"],
    )
    assert applied.exit_code == 0, applied.stdout

    bind_preflight = runner.invoke(
        app, ["project", "bind", "source", str(source), "--home", str(home)]
    )
    assert bind_preflight.exit_code == 0, bind_preflight.stdout
    bind_plan = json.loads(bind_preflight.stdout)
    assert bind_plan["schema"] == "zekam-project-local-source-binding-plan/v1"
    assert bind_plan["source_root"] == str(source.resolve())
    assert bind_plan["source_kind"] == "directory"
    assert bind_plan["plan_digest"].startswith("sha256:")
    assert bind_plan["apply"] is False

    bound = runner.invoke(
        app,
        ["project", "bind", "source", str(source), "--home", str(home), "--uygula"],
    )
    assert bound.exit_code == 0, bound.stdout
    binding = json.loads(bound.stdout)
    assert binding["source_root"] == bind_plan["source_root"]
    assert binding["plan_digest"] == bind_plan["plan_digest"]
    assert binding["apply"] is True

    alias_added = runner.invoke(
        app,
        [
            "project",
            "alias-add",
            "source",
            "temporary",
            "--home",
            str(home),
            "--uygula",
        ],
    )
    assert alias_added.exit_code == 0, alias_added.stdout
    remove_dry_run = runner.invoke(
        app,
        ["project", "alias-remove", "source", "temporary", "--home", str(home)],
    )
    assert remove_dry_run.exit_code == 0, remove_dry_run.stdout
    assert (
        "temporary"
        in json.loads(
            runner.invoke(
                app, ["project", "resolve", "source", "--home", str(home), "--json"]
            ).stdout
        )["aliases"]
    )
    alias_removed = runner.invoke(
        app,
        [
            "project",
            "alias-remove",
            "source",
            "temporary",
            "--home",
            str(home),
            "--uygula",
        ],
    )
    assert alias_removed.exit_code == 0, alias_removed.stdout
    assert (
        "temporary"
        not in json.loads(
            runner.invoke(
                app, ["project", "resolve", "source", "--home", str(home), "--json"]
            ).stdout
        )["aliases"]
    )


def test_retired_provider_commands_are_not_advertised() -> None:
    help_result = CliRunner().invoke(app, ["--help"])
    assert help_result.exit_code == 0
    # memory Dalga 2 itibariyle gercek yerel CLI yuzeyidir; loop/oracle/trace kapali.
    for command in ("loop", "oracle", "trace"):
        assert f" {command} " not in help_result.stdout
    assert " memory " in help_result.stdout
    assert " model " in help_result.stdout
    assert " local-core " in help_result.stdout
    assert " opencode " in help_result.stdout

    lifecycle_help = CliRunner().invoke(app, ["opencode", "--help"])
    assert lifecycle_help.exit_code == 0
    assert " event " in lifecycle_help.stdout
    assert " pre-compact " in lifecycle_help.stdout
    assert " forward " not in lifecycle_help.stdout
