from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from zekam.application import client_lifecycle_composition as composition
from zekam.application import client_runtime_bootstrap as bootstrap
from zekam.application import lifecycle_runtime_template_prepare as template
from zekam.application.client_lifecycle_continuity import (
    LIFECYCLE_ADAPTER_DIGEST,
    LIFECYCLE_EFFECT_OPERATION,
    PostgresLifecycleContinuityAdmission,
    _bridge_result_formula,
)
from zekam.application.client_runtime_bootstrap import ClientRuntimeBootstrapService
from zekam.application.lifecycle_runtime_template_prepare import (
    LifecycleRuntimeTemplatePrepareService,
    LifecycleTemplatePreparePlan,
    run_lifecycle_template_prepare_once,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.realm import ActorKind, LifecycleStatus
from zekam.domain.runtime import AttemptOutcome, ReceiptStatus
from zekam.domain.work import WorkState

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
REALM_ID = UUID("018f0000-0000-7000-8000-000000000001")
PROJECT_ID = UUID("018f0000-0000-7000-8000-000000000002")
WORK_ID = UUID("018f0000-0000-7000-8000-000000000003")
ACTOR_ID = UUID("018f0000-0000-7000-8000-000000000004")


class _Graph:
    def __init__(self, work: Any, *, actionable: bool = True, plan: Any = None) -> None:
        self.items = SimpleNamespace(get=lambda _work_id: work)
        self._snapshot = SimpleNamespace(is_actionable=actionable, plan=plan)

    def snapshot(self, _work_id: UUID) -> Any:
        return self._snapshot


def _install_prepare_fakes(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    *,
    work: Any,
    actor: Any | None = None,
    actionable: bool = True,
    plan: Any = None,
    policy: Any | None = None,
    source_revision: str = "git:source",
    adopted_run_id: UUID | None = None,
) -> list[str]:
    calls: list[str] = []
    graph = _Graph(work, actionable=actionable, plan=plan)
    actor = actor or SimpleNamespace(kind=ActorKind.HUMAN, status=LifecycleStatus.ACTIVE)
    policy = policy if policy is not None else SimpleNamespace(policy_digest=digest("policy"))

    def adopt(_work_id: UUID, **_kwargs: Any) -> UUID | None:
        calls.append("adopt")
        return adopted_run_id

    lifecycle = SimpleNamespace(
        current_source_revision=lambda _project_id: source_revision,
        assert_rebootstrap_admissible=lambda _work_id: calls.append("rebootstrap"),
        assert_legacy_adoption_admissible=adopt,
    )

    def repository(name: str, *_args: Any, **_kwargs: Any) -> Any:
        if name == "actor":
            return SimpleNamespace(get=lambda _actor_id: actor)
        if name == "lifecycle_runtime_template":
            return lifecycle
        raise AssertionError(name)

    monkeypatch.setattr(module, "WorkGraphService", lambda *_a, **_kw: graph)
    monkeypatch.setattr(module, "legacy_repository", repository)
    monkeypatch.setattr(
        module,
        "GovernanceService",
        lambda *_a, **_kw: SimpleNamespace(policies=SimpleNamespace(current=lambda _name: policy)),
    )
    return calls


def _work(state: WorkState) -> Any:
    return SimpleNamespace(
        project_id=PROJECT_ID,
        state=state,
        revision=7,
        record_digest=digest("work"),
        acceptance_criteria=(SimpleNamespace(verified=True),),
    )


def test_bootstrap_prepare_fresh_rebootstrap_and_input_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    realm = SimpleNamespace(id=REALM_ID)
    service = ClientRuntimeBootstrapService(object(), realm)
    calls = _install_prepare_fakes(monkeypatch, bootstrap, work=_work(WorkState.PROPOSED))
    plan = service.prepare(
        project_id=PROJECT_ID,
        work_item_id=WORK_ID,
        actor_id=ACTOR_ID,
        client_id="codex",
        session_id="session-1",
        entry_digest=digest("entry"),
        source_revision="git:source",
        now=NOW,
    )
    assert plan.resource == plan.lifecycle_resource
    assert plan.as_dict()["plan_digest"] == plan.plan_digest
    assert calls == []

    for changes, message in (
        ({"client_id": "other"}, "reviewed Codex"),
        ({"session_id": " "}, "reviewed Codex"),
        ({"event_type": " "}, "event type"),
        ({"rebootstrap": True, "adopt_existing": True}, "ayni anda"),
    ):
        arguments = {
            "project_id": PROJECT_ID,
            "work_item_id": WORK_ID,
            "actor_id": ACTOR_ID,
            "client_id": "codex",
            "session_id": "session-1",
            "entry_digest": digest("entry"),
            "source_revision": "git:source",
            "now": NOW,
        }
        with pytest.raises(PolicyViolation, match=message):
            service.prepare(**cast(Any, arguments | changes))

    calls = _install_prepare_fakes(monkeypatch, bootstrap, work=_work(WorkState.ACTIVE))
    continuation = service.prepare(
        project_id=PROJECT_ID,
        work_item_id=WORK_ID,
        actor_id=ACTOR_ID,
        client_id="codex",
        session_id="session-2",
        entry_digest=digest("entry-2"),
        source_revision="git:source",
        rebootstrap=True,
        now=NOW,
    )
    assert continuation.rebootstrap is True
    assert calls == ["rebootstrap"]


def test_bootstrap_prepare_adoption_and_current_authority_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    realm = SimpleNamespace(id=REALM_ID)
    service = ClientRuntimeBootstrapService(object(), realm)
    adopted_run = uuid4()
    _install_prepare_fakes(
        monkeypatch,
        bootstrap,
        work=_work(WorkState.VERIFICATION),
        plan=SimpleNamespace(id=uuid4()),
        adopted_run_id=adopted_run,
    )
    adopted = service.prepare(
        project_id=PROJECT_ID,
        work_item_id=WORK_ID,
        actor_id=ACTOR_ID,
        client_id="codex",
        session_id="session-3",
        entry_digest=digest("entry-3"),
        source_revision="git:source",
        event_type="pre_close",
        adopt_existing=True,
        now=NOW,
    )
    assert adopted.adopted_run_id == adopted_run
    assert adopted.adoption_effect_digest is not None

    cases = (
        (SimpleNamespace(kind=ActorKind.SYSTEM, status=LifecycleStatus.ACTIVE), True, "human"),
        (None, False, "actionable"),
        (None, True, "current policy"),
    )
    for actor, actionable, expected in cases:
        _install_prepare_fakes(
            monkeypatch,
            bootstrap,
            work=_work(WorkState.PROPOSED),
            actor=actor,
            actionable=actionable,
            policy=None
            if expected == "current policy"
            else SimpleNamespace(policy_digest=digest("policy")),
        )
        if expected == "current policy":
            monkeypatch.setattr(
                bootstrap,
                "GovernanceService",
                lambda *_a, **_kw: SimpleNamespace(
                    policies=SimpleNamespace(current=lambda _name: None)
                ),
            )
        with pytest.raises(PolicyViolation, match=expected):
            service.prepare(
                project_id=PROJECT_ID,
                work_item_id=WORK_ID,
                actor_id=ACTOR_ID,
                client_id="codex",
                session_id="session",
                entry_digest=digest("entry"),
                source_revision="git:source",
                now=NOW,
            )


def test_bootstrap_apply_rejects_plan_head_source_and_policy_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    realm = SimpleNamespace(id=REALM_ID)
    service = ClientRuntimeBootstrapService(object(), realm)
    _install_prepare_fakes(monkeypatch, bootstrap, work=_work(WorkState.PROPOSED))
    plan = service.prepare(
        project_id=PROJECT_ID,
        work_item_id=WORK_ID,
        actor_id=ACTOR_ID,
        client_id="codex",
        session_id="session",
        entry_digest=digest("entry"),
        source_revision="git:source",
        now=NOW,
    )
    common = {
        "supplied_plan_digest": plan.plan_digest,
        "current_entry_digest": plan.entry_digest,
        "current_source_revision": plan.source_revision,
        "now": NOW,
    }
    for field, value, message in (
        ("supplied_plan_digest", digest("wrong"), "plan digest"),
        ("current_entry_digest", digest("wrong"), "spool head"),
        ("current_source_revision", "git:wrong", "source revision"),
    ):
        with pytest.raises(PolicyViolation, match=message):
            service.apply(plan, **cast(Any, common | {field: value}))
    monkeypatch.setattr(
        bootstrap,
        "GovernanceService",
        lambda *_a, **_kw: SimpleNamespace(policies=SimpleNamespace(current=lambda _name: None)),
    )
    with pytest.raises(PolicyViolation, match="policy drift"):
        service.apply(plan, **cast(Any, common))


def test_template_plan_prepare_branches_and_claimed_empty_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    realm = SimpleNamespace(id=REALM_ID)
    service = LifecycleRuntimeTemplatePrepareService(object(), realm)
    _install_prepare_fakes(monkeypatch, template, work=_work(WorkState.PROPOSED))
    plan = service.prepare(
        project_id=PROJECT_ID,
        work_item_id=WORK_ID,
        actor_id=ACTOR_ID,
        source_revision="git:source",
        now=NOW,
    )
    assert plan.authority_body()["grants_authority"] is False
    assert "adopt_existing" not in plan.authority_body()
    assert plan.as_dict()["applied"] is False
    candidate, manifest = template._prepare_manifest(plan, uuid4())
    assert candidate.content_digest == plan.plan_digest
    assert tuple(item.candidate_id for item in manifest.selected) == (candidate.candidate_id,)

    with pytest.raises(PolicyViolation, match="verification Work"):
        replace(plan, adopt_existing=True)
        service.prepare(
            project_id=PROJECT_ID,
            work_item_id=WORK_ID,
            actor_id=ACTOR_ID,
            source_revision="git:source",
            adopt_existing=True,
            now=NOW,
        )
    _install_prepare_fakes(
        monkeypatch, template, work=_work(WorkState.PROPOSED), source_revision="git:new"
    )
    with pytest.raises(PolicyViolation, match="source drift"):
        service.prepare(
            project_id=PROJECT_ID,
            work_item_id=WORK_ID,
            actor_id=ACTOR_ID,
            source_revision="git:source",
            now=NOW,
        )

    class EmptyHost:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def acquire_work(self, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(template, "ExecutionHost", EmptyHost)
    assert run_lifecycle_template_prepare_once(object(), realm, now=NOW) is None


def test_template_apply_rejects_digest_expiry_and_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = LifecycleTemplatePreparePlan(
        REALM_ID,
        PROJECT_ID,
        WORK_ID,
        1,
        ACTOR_ID,
        "git:source",
        digest("policy"),
        False,
        NOW,
        NOW + dt.timedelta(minutes=30),
    )
    service = LifecycleRuntimeTemplatePrepareService(object(), SimpleNamespace(id=REALM_ID))
    with pytest.raises(PolicyViolation, match="plan digest"):
        service.apply(plan, supplied_plan_digest=digest("wrong"))
    with pytest.raises(PolicyViolation, match="suresi dolmus"):
        service.apply(
            replace(plan, expires_at=NOW - dt.timedelta(seconds=1)),
            supplied_plan_digest=replace(
                plan, expires_at=NOW - dt.timedelta(seconds=1)
            ).plan_digest,
        )


def test_composition_generation_and_receiptless_recovery_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        calls = 0

        def reconfigure(self, **_kwargs: Any) -> Any:
            self.calls += 1
            return SimpleNamespace(hook_set_digest="expected")

    repository = SimpleNamespace(
        realm_id=REALM_ID,
        active_hook_runtime_binding=lambda: (2, digest("config"), "expected"),
    )
    runtime = Runtime()
    bundle = SimpleNamespace(specs=(), runtimes=(), profile=object(), adapters=())
    assert (
        composition._configure_active_memory_hook_runtime(
            cast(Any, runtime), cast(Any, bundle), repository, now=NOW
        )
        == "expected"
    )
    assert runtime.calls == 2
    repository.active_hook_runtime_binding = lambda: (0, digest("config"), "expected")
    with pytest.raises(PolicyViolation, match="digest drift"):
        composition._configure_active_memory_hook_runtime(
            cast(Any, Runtime()), cast(Any, bundle), repository, now=NOW
        )
    repository.active_hook_runtime_binding = lambda: (65, digest("config"), "expected")
    with pytest.raises(PolicyViolation, match="bounded replay"):
        composition._configure_active_memory_hook_runtime(
            cast(Any, Runtime()), cast(Any, bundle), repository, now=NOW
        )

    claim = SimpleNamespace(id=uuid4())
    work = SimpleNamespace(
        job=SimpleNamespace(id=uuid4()),
        attempt_id=uuid4(),
        lease=SimpleNamespace(
            id=uuid4(), owner_digest=digest("owner"), fencing_token=3, worker_label="w"
        ),
    )
    outcomes: list[AttemptOutcome] = []

    class Host:
        ledger = SimpleNamespace(receipt_for_claim=lambda _claim_id: None)

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def finish(self, _work: Any, *, outcome: AttemptOutcome, result_digest: str) -> bool:
            outcomes.append(outcome)
            return False

    monkeypatch.setattr(composition, "ExecutionHost", Host)
    recovering = SimpleNamespace(
        connection=object(),
        realm_id=REALM_ID,
        recovery_finish_state=lambda **_kw: "running-exact",
    )
    with pytest.raises(PolicyViolation, match="finish reddedildi"):
        composition._finish_receiptless_recovery(
            repository=recovering,
            work=cast(Any, work),
            claim=cast(Any, claim),
            reason="bounded-reason",
            rejected_message="finish reddedildi",
        )
    assert outcomes == [AttemptOutcome.RECOVERY_REQUIRED]


def test_continuity_exact_binding_guards_and_formula() -> None:
    connection = object()
    realm_id = REALM_ID
    event = SimpleNamespace(
        project_id=PROJECT_ID,
        work_item_id=WORK_ID,
        run_id=uuid4(),
        client_id="codex",
        session_id="session",
        sequence=1,
        event_type="post_compaction",
        payload_digest=digest({"wire": "bounded"}),
    )
    plan = SimpleNamespace(
        event=event,
        plan_digest=digest("plan"),
        effect_digest=digest("effect"),
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
    )
    job = SimpleNamespace(
        id=uuid4(),
        project_id=PROJECT_ID,
        work_item_id=WORK_ID,
        run_id=event.run_id,
        plan_id=uuid4(),
    )
    claim = SimpleNamespace(
        id=uuid4(),
        job_id=job.id,
        attempt_id=uuid4(),
        fencing_token=9,
        operation=LIFECYCLE_EFFECT_OPERATION,
        adapter_digest=LIFECYCLE_ADAPTER_DIGEST,
        effect_digest=plan.effect_digest,
    )
    work = SimpleNamespace(
        job=job,
        attempt_id=claim.attempt_id,
        lease=SimpleNamespace(fencing_token=9, worker_label="worker"),
    )
    participants = [SimpleNamespace(connection=connection, realm_id=realm_id) for _ in range(6)]
    bridge = SimpleNamespace(
        repository=participants[1], authorizations=participants[2], hook_outcomes=participants[3]
    )
    memory = SimpleNamespace(
        repository=participants[4],
        authorizations=participants[5],
        assert_mutating_admission=lambda **_kw: None,
    )
    delivery = SimpleNamespace(
        work=work,
        claim=claim,
        authorization_id=uuid4(),
        plan=plan,
        hook_session=object(),
        session_binding_id=uuid4(),
        client_instance_id="instance",
        work_plan_digest=digest("work-plan"),
        hydration_authorization_id=None,
    )
    admission = PostgresLifecycleContinuityAdmission(
        connection,
        realm_id,
        cast(Any, bridge),
        cast(Any, memory),
        participants[0],
        cast(Any, delivery),
    )
    admission._assert_uow_identity()
    preflight = {
        "realm_id": str(realm_id),
        "project_id": str(PROJECT_ID),
        "work_item_id": str(WORK_ID),
        "run_id": str(event.run_id),
        "authorization_id": str(delivery.authorization_id),
        "job_id": str(job.id),
        "claim_id": str(claim.id),
        "plan_digest": plan.plan_digest,
        "effect_digest": plan.effect_digest,
    }
    admission._assert_preflight(preflight)
    with pytest.raises(PolicyViolation, match="preflight"):
        admission._assert_preflight(preflight | {"job_id": str(uuid4())})

    canonical = {"session_id": "session", "client_id": "instance"}
    admission._assert_common_mutating_admission(canonical)
    with pytest.raises(PolicyViolation, match="identity drift"):
        admission._assert_common_mutating_admission(canonical | {"client_id": "other"})

    formula = _bridge_result_formula(
        plan_digest=plan.plan_digest,
        event_digest=digest("event"),
        event_id=uuid4(),
        outbox_id=uuid4(),
        hook_receipt_id=uuid4(),
        hook_output_digest=digest("output"),
    )
    assert formula.startswith("sha256:")


def test_continuity_recovery_context_terminal_and_failure_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished: list[AttemptOutcome] = []
    claim_id = uuid4()

    def finish(_work: Any, *, outcome: AttemptOutcome, result_digest: str) -> bool:
        finished.append(outcome)
        return True

    host = SimpleNamespace(
        ledger=SimpleNamespace(receipt_for_claim=lambda _claim_id: None),
        finish=finish,
    )
    admission = object.__new__(PostgresLifecycleContinuityAdmission)
    object.__setattr__(
        admission,
        "delivery",
        SimpleNamespace(
            claim=SimpleNamespace(id=claim_id, effect_digest=digest("effect")),
            work=object(),
        ),
    )
    with pytest.raises(RuntimeError, match="boom"), admission._recover_on_failure(cast(Any, host)):
        raise RuntimeError("boom")
    assert finished == [AttemptOutcome.RECOVERY_REQUIRED]

    completed_host = SimpleNamespace(
        ledger=SimpleNamespace(
            receipt_for_claim=lambda _claim_id: SimpleNamespace(status=ReceiptStatus.COMPLETED)
        ),
        finish=lambda *_a, **_kw: pytest.fail("completed receipt must not be finished again"),
    )
    with (
        pytest.raises(RuntimeError, match="terminal"),
        admission._recover_on_failure(cast(Any, completed_host)),
    ):
        raise RuntimeError("terminal")
