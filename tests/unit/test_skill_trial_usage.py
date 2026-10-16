"""Usage reports reopen real offline canonical chains, never loose report JSON."""

import pytest

from tests.unit.test_canonical_job_runtime import world  # noqa: F401
from tests.unit.test_skill_trial_aggregation import batch  # noqa: F401
from zekam.application.skill_trial_usage import read_skill_trial_usage
from zekam.domain.canonical import digest
from zekam.domain.errors import ZekamError


def _read(world, execute, case, results):
    return read_skill_trial_usage(
        world.home,
        references=tuple(result["canonical_job_id"] for result in results),
        cases=(case,),
        project_id=world.binding.project_id,
        revision_digest=results[0]["revision_digest"],
        package_digest=execute.package_digest,
    )


def test_usage_is_separate_digest_bound_projection_and_has_no_new_calls(world, batch):
    execute, aggregate, case, calls = batch
    execute.metering["enabled"] = True
    results = [execute(repetition=index) for index in range(2)]
    frozen = aggregate(results)
    before = len(calls)
    report = _read(world, execute, case, results)
    assert report["aggregate"] == frozen
    usage = report["native_usage_report"]
    assert usage["aggregate_digest"] == frozen["aggregate_digest"]
    assert (
        digest({key: value for key, value in usage.items() if key != "report_digest"})
        == usage["report_digest"]
    )
    assert usage["scope"] == "included-trial-dispatches"
    assert usage["model_id"] == "provider/model"
    assert usage["new_agent_calls"] == usage["new_provider_calls"] == 0
    for totals in usage["by_condition"].values():
        assert totals["agent_dispatches"] == totals["complete_token_measurements"] == 2
        assert totals["native_reported_tokens"] == 24
        assert totals["host_estimated_cost_usd"] == "0.2"
        assert totals["estimated_cost_micros_sum"] == 200_000
        assert totals["provider_requests"] is None and totals["billing_verified"] is False
    assert _read(world, execute, case, list(reversed(results))) == report
    assert len(calls) == before


@pytest.mark.parametrize("partial", [False, True])
def test_legacy_and_partial_observations_keep_total_unknown(world, batch, partial):
    execute, _aggregate, case, calls = batch
    legacy = execute()
    execute.metering.update(enabled=True, partial=partial)
    measured = execute(repetition=1)
    report = _read(world, execute, case, [legacy, measured])["native_usage_report"]
    for totals in report["by_condition"].values():
        assert totals["agent_dispatches"] == 2 and totals["artifacts_with_native_usage"] == 1
        assert totals["complete_token_measurements"] == (0 if partial else 1)
        assert totals["native_reported_tokens"] is None
        assert totals["host_estimated_cost_usd"] is None
        assert totals["estimated_cost_micros_sum"] is None


def test_usage_read_cannot_replace_missing_actual_artifact(world, batch):
    from zekam.application.config import load_settings
    from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

    execute, _aggregate, case, _calls = batch
    execute.metering["enabled"] = True
    result = execute()
    objects = LocalContentAddressedStore(
        world.home / load_settings(home=world.home).object_store_relative
    )
    objects.delete(result["observations"]["candidate"]["artifact_digest"])
    with pytest.raises(ZekamError):
        _read(world, execute, case, [result])


def test_failed_dispatch_is_included_as_missing_usage_without_hiding_failure(world, batch):
    execute, _aggregate, case, _calls = batch
    execute.metering["enabled"] = True
    failed_case = case | {"input": {"fixture_candidate": "process-failure"}}
    result = execute(case=failed_case)
    report = _read(world, execute, failed_case, [result])
    assert report["aggregate"]["comparison_state"] == "failed"
    totals = report["native_usage_report"]["by_condition"]
    assert totals["baseline"]["native_reported_tokens"] == 12
    assert totals["candidate"]["agent_dispatches"] == 1
    assert totals["candidate"]["native_reported_tokens"] is None
    assert totals["candidate"]["artifacts_with_native_usage"] == 0
