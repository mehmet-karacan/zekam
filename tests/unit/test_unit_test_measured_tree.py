"""Full-tree guards over real files; Maven/model behavior remains a replay fixture."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.unit.test_unit_test_measurement import Env
from tests.unit.unit_test_loop_support import Harness, default_final
from tests.unit.unit_test_runner_support import good_scenario
from zekam.application.unit_test_agents import AgentSpecialty
from zekam.application.unit_test_measurement import MeasurementStatus
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.unit_test_engineering import UnitTestStopReason
from zekam.infrastructure.unit_test_runner.measurement import MeasurementDiagnostics
from zekam.infrastructure.unit_test_runner.tree_evidence import (
    measured_tree,
    validate_measured_tree,
)
from zekam.infrastructure.unit_test_runner.tree_fingerprint import digest_test_tree


def test_maven_test_resource_side_effect_rejects_otherwise_passing_measurement(tmp_path):
    env = Env(tmp_path)
    scenario = good_scenario()
    scenario["write"]["src/test/resources/side-effect.txt"] = "changed during measurement"
    env.fake.set(**scenario)
    diagnostics = MeasurementDiagnostics()
    record = env.measure(diagnostics=diagnostics)
    assert record.status is MeasurementStatus.UNBOUND_OR_STALE
    assert record.evaluation is None
    assert diagnostics.test_tree_evidence["state"] == "drifted"
    assert (
        diagnostics.test_tree_evidence["before_digest"]
        != diagnostics.test_tree_evidence["after_digest"]
    )


def test_stable_measured_tree_keeps_before_files_and_digest(tmp_path):
    env = Env(tmp_path)
    resource = env.root / "src/test/resources/input.txt"
    resource.parent.mkdir(parents=True)
    resource.write_text("fixture", encoding="utf-8")
    env.fake.set(**good_scenario())
    diagnostics = MeasurementDiagnostics()
    record = env.measure(diagnostics=diagnostics)
    assert record.status is MeasurementStatus.ACCEPTED
    tree = diagnostics.test_tree_evidence
    assert tree["state"] == "stable"
    assert validate_measured_tree(tree) == digest_test_tree(env.root, ("",))
    assert tree["before_digest"] == tree["after_digest"]


def _tree_measurer(harness):
    original = harness.measurer

    def measure(*args, **kwargs):
        before = digest_test_tree(harness.root, ("",))
        run = original.measure(*args, **kwargs)
        after = digest_test_tree(harness.root, ("",))
        return replace(run, test_tree_evidence=measured_tree(before, after, ("",)))

    return SimpleNamespace(measure=measure)


def test_final_verifier_time_resource_change_cannot_close_success(tmp_path):
    h = None

    def final(context, call):
        path = h.root / "src/test/resources/external.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("external change " + str(call), encoding="utf-8")
        return default_final(context, call)

    h = Harness(tmp_path, max_attempts=2, handlers={AgentSpecialty.FINAL_VERIFIER: final})
    service = replace(
        h.service(), measurer=_tree_measurer(h), test_tree=lambda: digest_test_tree(h.root, ("",))
    )
    outcome = service.run(h.request, approvals=h.approvals, limits=h.limits)
    assert outcome.terminal.stop_reason not in {
        UnitTestStopReason.TARGET_REACHED,
        UnitTestStopReason.ALREADY_AT_TARGET,
    }
    assert (h.root / "src/test/resources/external.txt").is_file()


def test_measured_tree_requires_current_readback_port_for_success(tmp_path):
    h = Harness(tmp_path, max_attempts=2)
    service = replace(h.service(), measurer=_tree_measurer(h))
    outcome = service.run(h.request, approvals=h.approvals, limits=h.limits)
    assert outcome.terminal.stop_reason not in {
        UnitTestStopReason.TARGET_REACHED,
        UnitTestStopReason.ALREADY_AT_TARGET,
    }


def test_tree_projection_rejects_scope_and_fingerprint_tampering():
    value = digest("content")
    tree = measured_tree({"src/test/a.txt": value}, {"src/test/a.txt": value}, ("",))
    for invalid in (
        tree | {"before_digest": digest("other")},
        tree | {"files": [["../outside.txt", value]]},
        tree | {"state": "stable", "after_digest": digest("other")},
    ):
        with pytest.raises((PolicyViolation, ValidationFailed)):
            validate_measured_tree(invalid)
