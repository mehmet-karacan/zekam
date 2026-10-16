"""Real scoped SQLite/CAS sources; native transport is an offline fixture only."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.unit.test_canonical_job_runtime import world  # noqa: F401
from tests.unit.test_opencode_batch import _events, _output, _stream
from tests.unit.test_skill_trials import _binding, _inputs
from zekam.application import tournament_runtime as subject
from zekam.application.skill_trials import build_skill_trial_plan, run_skill_trial
from zekam.application.unit_test_provider_profile import BatchProvider, BatchProviderProfile
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.infrastructure.clients import opencode_batch as native
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


@pytest.fixture
def tournament(world, tmp_path, monkeypatch):
    root, package, case, original_plan = _inputs(tmp_path, world.binding.project_id)
    binary = tmp_path / "opencode.exe"
    profile = BatchProviderProfile(
        (
            BatchProvider(
                "fixture",
                "http://127.0.0.1:11434/v1",
                "loopback",
                (
                    ("builder-a", "actual-a"),
                    ("builder-b", "actual-b"),
                    ("selector", "actual-selector"),
                ),
            ),
        )
    )
    calls = []
    mode = {"baseline": 0, "candidate": 7, "decision": "valid"}
    monkeypatch.setattr(native, "resolve_opencode_executable", lambda value: Path(value))
    monkeypatch.setattr(subject, "resolve_project_source", lambda *_args: root)

    def transport(spec, *, cwd, stdin=None, cancel=None):
        if spec.argv[-1] == "--version":
            return _output(spec, native.SUPPORTED_VERSION.encode())
        context = json.loads(stdin)["context"]
        calls.append(context)
        if context["schema"] == "zekam-skill-trial-context/v1":
            assert "expected_output" not in context
            answer = mode["candidate"] if context["skill_instructions"] else mode["baseline"]
            if answer == "process-failure":
                return _output(spec, b"", exit_code=1)
        else:
            assert context["schema"] == "zekam-tournament-selector-context/v1"
            with closing(world.runtime._connect()) as db:
                assert (
                    db.execute(
                        "select count(*) from local_effect_claim c join local_job j on j.id=c.job_id "
                        "where c.operation='tournament.select' and j.state='running'"
                    ).fetchone()[0]
                    == 1
                )
            assert context["case"]["expected_output"] == 7
            chosen = next(item for item in context["candidates"] if item["eligible"])["job_id"]
            answer = {
                "subject_digest": context["subject_digest"],
                "selected_job_id": chosen,
                "checks": dict.fromkeys(("artifact", "process", "safety"), "pass"),
                "reason_code": "measured-improvement",
            }
            if mode["decision"] == "wrong-subject":
                answer["subject_digest"] = "wrong"
            elif mode["decision"] == "foreign-job":
                answer["selected_job_id"] = str(uuid4())
            elif mode["decision"] == "unsafe":
                answer["checks"]["safety"] = "fail"
            elif mode["decision"] == "process-failure":
                return _output(spec, b"", exit_code=1)
        events = _events({"result": {}, "body": {"output": answer}})
        if mode.get("metered"):
            events[-1]["part"].update(
                cost=0.1,
                tokens={"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            )
        identity = (
            "ses_tournament_1"
            if mode["decision"] == "reused-session"
            else f"ses_tournament_{len(calls)}"
        )
        for event in events:
            event["sessionID"] = event["part"]["sessionID"] = identity
        return _output(spec, _stream(events))

    monkeypatch.setattr(native.runner, "run", transport)

    def trials():
        results = []
        for model in ("fixture/builder-a", "fixture/builder-b"):
            plan = build_skill_trial_plan(
                revision_digest=original_plan["revision_digest"],
                package=package,
                case=case,
                project_id=world.binding.project_id,
                model_id=model,
                executable=binary,
                objects=LocalContentAddressedStore(tmp_path / "cas"),
                source_root=root,
                provider_profile=profile,
            )
            results.append(
                run_skill_trial(
                    world.home,
                    binding=_binding(world, plan),
                    package=package,
                    case=case,
                    plan=plan,
                    root=root,
                    executable=binary,
                    realm_id=uuid4(),
                    coordinator_id=uuid4(),
                    validate_inputs=lambda: None,
                    provider_profile=profile,
                )
            )
        return tuple(result["canonical_job_id"] for result in results)

    def prepare(references, **changes):
        return subject.build_tournament_plan(
            world.home,
            references=references,
            case=case,
            project_id=world.binding.project_id,
            model_id=changes.pop("model_id", "fixture/selector"),
            root=root,
            executable=binary,
            provider_profile=changes.pop("provider_profile", profile),
            **changes,
        )[0]

    def run(plan, *, authorize=True, validate=lambda: None):
        return subject.run_tournament(
            world.home,
            binding=_binding(world, plan),
            plan=plan,
            case=case,
            root=root,
            executable=binary,
            realm_id=uuid4(),
            coordinator_id=uuid4(),
            provider_profile=profile,
            authorize_unmetered=authorize,
            validate_inputs=validate,
        )

    return SimpleNamespace(
        world=world,
        root=root,
        case=case,
        binary=binary,
        profile=profile,
        calls=calls,
        mode=mode,
        trials=trials,
        prepare=prepare,
        run=run,
    )


def test_native_selector_claim_receipt_and_historical_read_reopen_actual_sources(tournament):
    f = tournament
    refs = f.trials()
    plan = f.prepare(refs)
    assert plan["max_agent_calls"] == 1 and len(plan["eligible_job_ids"]) == 2
    before = len(f.calls)
    result = f.run(plan)
    assert len(f.calls) == before + 1 and result["state"] == "selected-candidate"
    assert result["selected_job_id"] in refs
    assert result["activatable"] is False and result["quality_evaluation_complete"] is False
    assert result["actual_tokens"] is None and result["actual_cost_micros"] is None
    assert result["source_agent_dispatches"] == 4
    assert all(item["eligible"] and not item["regressed"] for item in result["measurements"])
    snapshot = f.world.runtime.job_snapshot(result["canonical_job_id"])
    assert snapshot["state"] == "completed" and len(snapshot["effects"]) == 1
    assert snapshot["effects"][0]["operation"] == subject.OPERATION
    before = len(f.calls)
    read = subject.read_tournament(
        f.world.home, result["canonical_job_id"], project_id=f.world.binding.project_id
    )
    assert read["selected_job_id"] == result["selected_job_id"] and len(f.calls) == before
    assert read["receipt_evidence_digest"] == snapshot["terminal_evidence_digest"]


def test_native_usage_separates_historical_sources_and_new_selector(tournament):
    f = tournament
    f.mode["metered"] = True
    refs = f.trials()
    result = f.run(f.prepare(refs))
    usage = result["native_measurements"]
    assert len(usage["historical_sources"]) == 2
    assert all(
        item["baseline"]["native_reported_tokens"] == 12 for item in usage["historical_sources"]
    )
    assert usage["new_selector"]["native_reported_tokens"] == 12
    assert usage["new_selector"]["estimated_cost_micros"] == 100_000
    assert usage["billing_verified"] is False and usage["provider_requests"] is None
    assert result["actual_cost_micros"] is None and result["actual_tokens"] is None
    reopened = subject.read_tournament(
        f.world.home, result["canonical_job_id"], project_id=f.world.binding.project_id
    )
    assert reopened["native_measurements"] == usage


@pytest.mark.parametrize(
    "baseline,candidate,state",
    [(7, 7, "no-progress"), (7, 0, "no-progress"), (0, "process-failure", "failed-source")],
)
def test_no_progress_or_failed_sources_stop_without_selector_dispatch(
    tournament, baseline, candidate, state
):
    f = tournament
    f.mode.update(baseline=baseline, candidate=candidate)
    plan = f.prepare(f.trials())
    before = len(f.calls)
    assert plan["max_agent_calls"] == 0
    result = f.run(plan, authorize=False)
    assert result["state"] == state and result["selected_job_id"] is None
    assert result["new_agent_calls"] == 0 and result["observation"] is None
    assert len(f.calls) == before
    if baseline == 7 and candidate == 0:
        assert all(item["regressed"] for item in result["measurements"])


@pytest.mark.parametrize("decision", ["wrong-subject", "foreign-job", "unsafe", "process-failure"])
def test_selector_claim_is_not_automatically_a_valid_selection(tournament, decision):
    f = tournament
    plan = f.prepare(f.trials())
    f.mode["decision"] = decision
    result = f.run(plan)
    assert result["state"] == "unverified" and result["selected_job_id"] is None


def test_unmetered_dimensions_need_explicit_authorization_before_dispatch(tournament):
    f = tournament
    plan = f.prepare(f.trials())
    before = len(f.calls)
    with pytest.raises(PolicyViolation, match="unmetered"):
        f.run(plan, authorize=False)
    assert len(f.calls) == before


def test_alias_references_cannot_duplicate_one_actual_trial(tournament):
    f = tournament
    refs = f.trials()
    alias = f.world.runtime.job_snapshot(refs[0])["idempotency_key"]
    with pytest.raises(PolicyViolation, match="unique completed"):
        f.prepare((refs[0], alias))


def test_same_model_selector_and_changed_builder_profile_are_rejected(tournament):
    f = tournament
    refs = f.trials()
    with pytest.raises(PolicyViolation, match="ayni model"):
        f.prepare(refs, model_id="fixture/builder-a#high")
    changed = BatchProviderProfile(
        (
            BatchProvider(
                "fixture",
                "http://127.0.0.1:11434/v1",
                "loopback",
                (
                    ("builder-a", "other-upstream"),
                    ("builder-b", "actual-b"),
                    ("selector", "actual-selector"),
                ),
            ),
        )
    )
    with pytest.raises(PolicyViolation, match="builder profile/source"):
        f.prepare(refs, provider_profile=changed)


def test_selector_cannot_reuse_a_builder_native_session(tournament):
    f = tournament
    plan = f.prepare(f.trials())
    f.mode["decision"] = "reused-session"
    with pytest.raises(PolicyViolation, match="reused builder"):
        f.run(plan)


def test_foreign_project_read_cannot_return_selection(tournament):
    f = tournament
    result = f.run(f.prepare(f.trials()))
    with pytest.raises(PolicyViolation, match="foreign project"):
        subject.read_tournament(f.world.home, result["canonical_job_id"], project_id=str(uuid4()))


def test_completed_replay_uses_original_receipt_and_never_dispatches_or_revalidates_current_config(
    tournament,
):
    f = tournament
    plan = f.prepare(f.trials())
    binding = _binding(f.world, plan)
    realm, coordinator = uuid4(), uuid4()
    kwargs = dict(
        binding=binding,
        plan=plan,
        case=f.case,
        root=f.root,
        executable=f.binary,
        realm_id=realm,
        coordinator_id=coordinator,
        provider_profile=f.profile,
    )
    first = subject.run_tournament(
        f.world.home, **kwargs, authorize_unmetered=True, validate_inputs=lambda: None
    )
    before = len(f.calls)

    def forbidden():
        raise AssertionError("completed replay admitted a new effect")

    second = subject.run_tournament(f.world.home, **kwargs, validate_inputs=forbidden)
    assert second["canonical_replayed"] is True
    assert second["canonical_job_id"] == first["canonical_job_id"]
    assert len(f.calls) == before


def test_lost_builder_artifact_prevents_historical_selection_report(tournament):
    f = tournament
    refs = f.trials()
    result = f.run(f.prepare(refs))
    objects = subject._objects(f.world.home)
    source_snapshot = f.world.runtime.job_snapshot(refs[0])
    trial = subject._object(objects, source_snapshot["terminal_evidence_digest"], maximum=128_000)
    artifact = trial["observations"]["candidate"]["artifact_digest"]
    objects._paths(artifact)[0].unlink()
    before = len(f.calls)
    with pytest.raises((ZekamError, OSError)):
        subject.read_tournament(
            f.world.home, result["canonical_job_id"], project_id=f.world.binding.project_id
        )
    assert len(f.calls) == before


def test_changed_case_cannot_relabel_actual_trial_evidence(tournament):
    f = tournament
    refs = f.trials()
    f.case["input"] = {"different": "question"}
    before = len(f.calls)
    with pytest.raises(PolicyViolation, match="scope/profile/case"):
        f.prepare(refs)
    assert len(f.calls) == before


def test_new_operation_is_reserved_from_generic_worker(tournament):
    f = tournament
    from zekam.application.local_runtime import RESERVED_JOB_OPERATIONS

    assert subject.OPERATION in RESERVED_JOB_OPERATIONS
