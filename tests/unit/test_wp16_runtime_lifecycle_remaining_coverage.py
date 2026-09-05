from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest

from zekam.application import client_lifecycle_composition as composition
from zekam.application import client_lifecycle_continuity as continuity
from zekam.application import client_runtime_bootstrap as bootstrap
from zekam.application.client_lifecycle_composition import (
    LifecyclePlanInputs,
)
from zekam.application.client_lifecycle_continuity import (
    LIFECYCLE_ADAPTER_DIGEST,
    LIFECYCLE_EFFECT_OPERATION,
    ClaimedLifecycleDelivery,
    PostgresLifecycleContinuityAdmission,
)
from zekam.application.client_lifecycle_spool import LifecycleReplayResult
from zekam.application.client_runtime_bootstrap import (
    ClaimedLifecycleBootstrapService,
    ClientRuntimeBootstrapPlan,
    ClientRuntimeBootstrapService,
    _MaterializedChild,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.runtime import AttemptOutcome, JobKind, ReceiptStatus
from zekam.domain.work import WorkState
from zekam.infrastructure.clients.codex_lifecycle import (
    CODEX_REVIEWED_CLIENT_CONTRACT_DIGEST,
)

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 80))


class _TransactionConnection:
    def __init__(self) -> None:
        self.transactions = 0
        self.rollbacks = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.transactions += 1
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            raise


def _bootstrap_plan(**changes: object) -> ClientRuntimeBootstrapPlan:
    values: dict[str, object] = {
        "realm_id": IDS[0],
        "project_id": IDS[1],
        "work_item_id": IDS[2],
        "work_revision": 7,
        "work_record_digest": digest("work"),
        "actor_id": IDS[3],
        "client_id": "codex",
        "session_id": "session",
        "entry_digest": digest("entry"),
        "event_type": "session_start",
        "source_revision": "git:source",
        "policy_digest": digest("policy"),
        "bootstrap_resource": f"runtime-bootstrap:{IDS[1]}:session",
        "lifecycle_resource": f"memory:{IDS[1]}:session:session",
        "prepared_at": NOW,
        "rebootstrap": False,
        "adopt_existing": False,
        "adopted_run_id": None,
    }
    values.update(changes)
    return ClientRuntimeBootstrapPlan(**cast(Any, values))


def test_bootstrap_apply_fresh_path_and_transactional_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _TransactionConnection()
    realm = SimpleNamespace(id=IDS[0])
    plan = _bootstrap_plan()
    work = SimpleNamespace(
        id=IDS[2],
        project_id=IDS[1],
        state=WorkState.PROPOSED,
        revision=7,
        record_digest=digest("work"),
    )
    task_plan = SimpleNamespace(id=IDS[4], plan_digest=digest("task-plan"), execution_order=("x",))
    transitions: list[WorkState] = []

    class Graph:
        items = SimpleNamespace(get=lambda _work_id: work)

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @staticmethod
        def snapshot(_work_id: UUID) -> Any:
            return SimpleNamespace(is_actionable=True, plan=None)

        @staticmethod
        def set_intent(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def create_plan(*_args: object, **_kwargs: object) -> Any:
            return task_plan

        @staticmethod
        def transition(_work_id: UUID, state: WorkState, **_kwargs: object) -> None:
            transitions.append(state)

    assignment_index = 10

    def assignment(**_kwargs: object) -> Any:
        nonlocal assignment_index
        assignment_index += 1
        return SimpleNamespace(id=IDS[assignment_index])

    class Assignments:
        @staticmethod
        def create(row: Any) -> tuple[UUID, bool]:
            return row.id, True

    class Runs:
        created: ClassVar[list[Any]] = []
        activated: ClassVar[list[UUID]] = []

        @classmethod
        def create_run(cls, row: Any) -> None:
            cls.created.append(row)

        @classmethod
        def activate_run(cls, run_id: UUID, **_kwargs: object) -> None:
            cls.activated.append(run_id)

    class Jobs:
        @staticmethod
        def enqueue(_job: Any) -> tuple[Any, bool]:
            return SimpleNamespace(id=IDS[30]), True

    class Authorizations:
        @staticmethod
        def issue(_authorization: Any) -> None:
            return None

    repositories = {
        "agent_assignment": Assignments(),
        "execution_run": Runs(),
        "job": Jobs(),
        "authorization": Authorizations(),
    }

    def repository(name: str, *_args: object, **_kwargs: object) -> Any:
        return repositories[name]

    monkeypatch.setattr(bootstrap, "WorkGraphService", Graph)
    monkeypatch.setattr(
        bootstrap,
        "GovernanceService",
        lambda *_a, **_kw: SimpleNamespace(
            policies=SimpleNamespace(
                current=lambda _name: SimpleNamespace(policy_digest=digest("policy"))
            )
        ),
    )
    monkeypatch.setattr(bootstrap, "legacy_repository", repository)
    monkeypatch.setattr(bootstrap, "_assignment", assignment)
    monkeypatch.setattr(
        bootstrap,
        "ExecutionRun",
        SimpleNamespace(create=lambda **_kw: SimpleNamespace(id=IDS[31])),
    )
    monkeypatch.setattr(
        bootstrap,
        "Authorization",
        SimpleNamespace(issue=lambda **_kw: SimpleNamespace(id=IDS[32])),
    )
    monkeypatch.setattr(
        bootstrap, "Job", SimpleNamespace(create=lambda **kw: SimpleNamespace(**kw))
    )
    monkeypatch.setattr(
        bootstrap,
        "parse_requests",
        lambda **_kw: (SimpleNamespace(resource=plan.bootstrap_resource),),
    )
    monkeypatch.setattr(
        bootstrap,
        "_planned_manifest",
        lambda _plan: SimpleNamespace(manifest_digest=digest("manifest")),
    )
    monkeypatch.setattr(bootstrap, "new_uuid7", lambda **_kw: IDS[31])

    result = ClientRuntimeBootstrapService(connection, realm).apply(
        plan,
        supplied_plan_digest=plan.plan_digest,
        current_entry_digest=plan.entry_digest,
        current_source_revision=plan.source_revision,
        now=NOW,
    )
    assert result.job_id == IDS[30]
    assert transitions == [WorkState.READY, WorkState.ACTIVE]
    assert connection.transactions == 1 and connection.rollbacks == 0
    assert Runs.activated == [IDS[31]]

    replaying = _bootstrap_plan(rebootstrap=True)
    work.state = WorkState.ACTIVE
    with pytest.raises(PolicyViolation, match="prior reviewed Plan"):
        ClientRuntimeBootstrapService(connection, realm).apply(
            replaying,
            supplied_plan_digest=replaying.plan_digest,
            current_entry_digest=replaying.entry_digest,
            current_source_revision=replaying.source_revision,
            now=NOW,
        )
    assert connection.rollbacks == 1


def _claimed_parent_payload(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "zekam-codex-lifecycle-bootstrap-job/v1",
        "entry_digest": digest("entry"),
        "authorization_id": str(IDS[1]),
        "effect_digest": digest("effect"),
        "child_assignment_id": str(IDS[2]),
        "context_created_at": NOW.isoformat(),
        "context_manifest_digest": digest("manifest"),
    }
    body.update(changes)
    return body


def _claimed_parent(**payload_changes: object) -> Any:
    resource = SimpleNamespace(resource="runtime-bootstrap:resource")
    return SimpleNamespace(
        job=SimpleNamespace(
            id=IDS[3],
            project_id=IDS[4],
            work_item_id=IDS[5],
            plan_id=IDS[6],
            assignment_id=IDS[7],
            run_id=IDS[8],
            kind=JobKind.MUTATION,
            max_attempts=1,
            required_capabilities=("client.lifecycle.codex-bootstrap",),
            resources=(resource,),
            payload=_claimed_parent_payload(**payload_changes),
        ),
        attempt_id=IDS[9],
        lease=SimpleNamespace(
            id=IDS[10], worker_label="worker", owner_digest=digest("owner"), fencing_token=3
        ),
    )


def test_claimed_bootstrap_materialize_happy_readback_and_finish_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = _TransactionConnection()
    work = _claimed_parent()
    entry = SimpleNamespace(entry_digest=digest("entry"), session_id="session", occurred_at=NOW)
    auth = SimpleNamespace(
        id=IDS[1],
        work_item_id=work.job.work_item_id,
        plan_id=work.job.plan_id,
        effect_digest=digest("effect"),
        authorization_digest=digest("authorization"),
        plan_digest=digest("plan"),
        scope=SimpleNamespace(allowed_resources=("runtime-bootstrap:resource",)),
    )
    materialized = _MaterializedChild(
        digest("result"), digest("manifest"), IDS[20], IDS[21], digest("packet"), "git:source"
    )
    finish_result = [True]
    checkpoints: list[dict[str, object]] = []

    class Host:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @staticmethod
        def claim_effect(*_args: object, **_kwargs: object) -> Any:
            return SimpleNamespace(id=IDS[22], claim_digest=digest("claim"))

        @staticmethod
        def record_success(*_args: object, **_kwargs: object) -> Any:
            return SimpleNamespace(result_digest=digest("result"))

        @staticmethod
        def finish(*_args: object, **_kwargs: object) -> bool:
            return finish_result[0]

    continuity_store = SimpleNamespace(
        journal_head=lambda: None,
        append_journal=lambda *_a, **_kw: None,
    )
    lifecycle_store = SimpleNamespace(
        current_work_plan_digest=lambda **_kw: digest("work-plan"),
        current_execution=lambda **_kw: SimpleNamespace(envelope_id=IDS[23]),
        store_job_checkpoint=lambda **kw: checkpoints.append(kw),
    )
    template_store = SimpleNamespace(
        current_for_bootstrap_job=lambda _job_id: None,
        projection_facts=lambda *_a: (
            1,
            "active",
            digest("record"),
            "git:source",
            None,
            56,
            digest("migration"),
        ),
    )
    stores = {
        "authorization": SimpleNamespace(get=lambda _id: auth),
        "context_continuity": continuity_store,
        "client_lifecycle": lifecycle_store,
        "lifecycle_runtime_template": template_store,
    }
    monkeypatch.setattr(
        bootstrap,
        "ClientLifecycleSpool",
        lambda *_a, **_kw: SimpleNamespace(pending=lambda **_kw2: (entry,)),
    )
    monkeypatch.setattr(bootstrap, "ExecutionHost", Host)
    monkeypatch.setattr(bootstrap, "legacy_repository", lambda name, *_a, **_kw: stores[name])
    monkeypatch.setattr(
        ClaimedLifecycleBootstrapService, "_materialize_claimed", lambda *_a, **_kw: materialized
    )
    monkeypatch.setattr(
        ClaimedLifecycleBootstrapService, "_policy", lambda *_a, **_kw: digest("policy")
    )

    service = ClaimedLifecycleBootstrapService(connection, IDS[0])
    assert service.materialize(work, tmp_path, now=NOW) == materialized.result_digest
    assert checkpoints and connection.rollbacks == 0

    finish_result[0] = False
    with pytest.raises(PolicyViolation, match="terminal finish"):
        service.materialize(work, tmp_path, now=NOW)
    assert connection.rollbacks == 1


@pytest.mark.parametrize(
    ("payload_changes", "job_changes", "message"),
    (
        ({"extra": True}, {}, "contract drift"),
        ({}, {"work_item_id": None}, "identity eksik"),
        ({"context_created_at": "bad"}, {}, "timestamp drift"),
        ({"context_created_at": "2026-09-04T12:00:00"}, {}, "timezone ister"),
        ({"authorization_id": "bad"}, {}, "UUID drift"),
        ({"entry_digest": digest("other-entry")}, {}, "spool head"),
    ),
)
def test_claimed_bootstrap_rejects_malformed_parent_before_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload_changes: dict[str, object],
    job_changes: dict[str, object],
    message: str,
) -> None:
    work = _claimed_parent(**payload_changes)
    for key, value in job_changes.items():
        setattr(work.job, key, value)
    entry = SimpleNamespace(entry_digest=digest("entry"))
    monkeypatch.setattr(
        bootstrap,
        "ClientLifecycleSpool",
        lambda *_a, **_kw: SimpleNamespace(pending=lambda **_kw2: (entry,)),
    )
    monkeypatch.setattr(
        bootstrap,
        "legacy_repository",
        lambda *_a, **_kw: SimpleNamespace(get=lambda _id: None),
    )
    with pytest.raises(PolicyViolation, match=message):
        ClaimedLifecycleBootstrapService(_TransactionConnection(), IDS[0]).materialize(
            work, tmp_path, now=NOW
        )


def test_claimed_bootstrap_authorization_and_sql_readback_guards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    work = _claimed_parent()
    entry = SimpleNamespace(entry_digest=digest("entry"))
    monkeypatch.setattr(
        bootstrap,
        "ClientLifecycleSpool",
        lambda *_a, **_kw: SimpleNamespace(pending=lambda **_kw2: (entry,)),
    )
    monkeypatch.setattr(
        bootstrap,
        "legacy_repository",
        lambda *_a, **_kw: SimpleNamespace(
            get=lambda _id: SimpleNamespace(
                work_item_id=IDS[60],
                plan_id=work.job.plan_id,
                effect_digest=digest("effect"),
                scope=SimpleNamespace(allowed_resources=("runtime-bootstrap:resource",)),
            )
        ),
    )
    with pytest.raises(PolicyViolation, match="authorization drift"):
        ClaimedLifecycleBootstrapService(_TransactionConnection(), IDS[0]).materialize(
            work, tmp_path, now=NOW
        )

    class Cursor:
        def __init__(self, rows: Sequence[tuple[object, ...]]) -> None:
            self.rows = rows

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object) -> None:
            return None

        def fetchall(self) -> list[tuple[object, ...]]:
            return list(self.rows)

        def fetchone(self) -> tuple[object, ...] | None:
            return None if not self.rows else self.rows[0]

    class ReadConnection:
        def __init__(self, rows: Sequence[tuple[object, ...]]) -> None:
            self.rows = rows

        def cursor(self) -> Cursor:
            return Cursor(self.rows)

    adoption_plan = IDS[61]
    adoption_job = IDS[62]
    adoption_claim = IDS[63]
    adoption_receipt = IDS[64]
    plan_digest = digest("adoption-plan")
    result_digest = digest("adoption-result")
    valid_rows = [
        (
            work.job.work_item_id,
            work.job.project_id,
            adoption_plan,
            "completed",
            "client-lifecycle-legacy-adoption",
            plan_digest,
            adoption_claim,
            adoption_receipt,
            "completed",
            result_digest,
        )
    ]
    service = ClaimedLifecycleBootstrapService(ReadConnection(valid_rows), IDS[0])
    service._assert_adoption_evidence(
        job=work.job,
        adoption_plan_id=adoption_plan,
        adoption_plan_digest=plan_digest,
        adoption_job_id=adoption_job,
        adoption_claim_id=adoption_claim,
        adoption_receipt_id=adoption_receipt,
        adoption_result_digest=result_digest,
    )
    rejected = ClaimedLifecycleBootstrapService(ReadConnection([]), IDS[0])
    with pytest.raises(PolicyViolation, match="terminal evidence drift"):
        rejected._assert_adoption_evidence(
            job=work.job,
            adoption_plan_id=adoption_plan,
            adoption_plan_digest=plan_digest,
            adoption_job_id=adoption_job,
            adoption_claim_id=adoption_claim,
            adoption_receipt_id=adoption_receipt,
            adoption_result_digest=result_digest,
        )

    policy = ClaimedLifecycleBootstrapService(ReadConnection([(digest("policy"),)]), IDS[0])
    assert policy._policy(IDS[6]) == digest("policy")
    with pytest.raises(PolicyViolation, match="policy exact"):
        ClaimedLifecycleBootstrapService(ReadConnection([]), IDS[0])._policy(IDS[6])

    turn = SimpleNamespace(
        context_manifest_digest=digest("manifest"),
        run_id=IDS[8],
        assignment_id=IDS[7],
        config_effective_digest=digest("config"),
        execution_environment_snapshot_digest=digest("environment"),
    )
    exact_turn_row = [("active", True, "active", None, True, True, True, True, True)]
    ClaimedLifecycleBootstrapService(ReadConnection(exact_turn_row), IDS[0])._assert_turn_bindings(
        work=work, turn=cast(Any, turn), now=NOW
    )
    with pytest.raises(PolicyViolation, match="turn binding drift"):
        ClaimedLifecycleBootstrapService(ReadConnection([]), IDS[0])._assert_turn_bindings(
            work=work, turn=cast(Any, turn), now=NOW
        )


def _delivery_boundary(event_type: str = "post_compaction") -> tuple[Any, Any, Any, Any]:
    event = SimpleNamespace(
        project_id=IDS[1],
        work_item_id=IDS[2],
        run_id=IDS[3],
        client_id="codex",
        session_id="session",
        sequence=1,
        event_type=event_type,
        payload_digest=digest({}),
        event_digest=digest("event"),
    )
    plan = SimpleNamespace(
        event=event,
        plan_digest=digest("plan"),
        effect_digest=digest("effect"),
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
        resource="memory:session",
        idempotency_key="delivery",
        hook_payload={},
        body=lambda: {"schema": "plan"},
    )
    job = SimpleNamespace(
        id=IDS[4],
        project_id=IDS[1],
        work_item_id=IDS[2],
        run_id=IDS[3],
        plan_id=IDS[5],
        step_id="step",
    )
    work = SimpleNamespace(
        job=job,
        attempt_id=IDS[6],
        lease=SimpleNamespace(
            id=IDS[7], owner_digest=digest("owner"), fencing_token=9, worker_label="worker"
        ),
    )
    claim = SimpleNamespace(
        id=IDS[8],
        job_id=job.id,
        attempt_id=work.attempt_id,
        fencing_token=9,
        operation=LIFECYCLE_EFFECT_OPERATION,
        adapter_digest=LIFECYCLE_ADAPTER_DIGEST,
        effect_digest=plan.effect_digest,
        claim_digest=digest("claim"),
        authorization_digest=digest("authorization"),
    )
    entry = SimpleNamespace(
        entry_digest=digest("entry"),
        delivery_id="delivery",
        client_id="codex",
        session_id="session",
        sequence=1,
        internal_event_type=event_type,
        external_event_type="PostCompact",
        observation={},
        occurred_at=NOW,
    )
    return entry, plan, work, claim


def _admission(
    event_type: str = "post_compaction",
) -> tuple[PostgresLifecycleContinuityAdmission, Any, Any]:
    entry, plan, work, claim = _delivery_boundary(event_type)
    connection = _TransactionConnection()
    participants = [SimpleNamespace(connection=connection, realm_id=IDS[0]) for _ in range(6)]
    repository = participants[0]
    bridge = SimpleNamespace(
        repository=participants[1], authorizations=participants[2], hook_outcomes=participants[3]
    )
    memory = SimpleNamespace(
        repository=participants[4],
        authorizations=participants[5],
        assert_mutating_admission=lambda **_kw: None,
    )
    delivery = ClaimedLifecycleDelivery(
        work, claim, IDS[9], plan, cast(Any, object()), IDS[10], "instance", digest("work-plan")
    )
    return (
        PostgresLifecycleContinuityAdmission(
            connection,
            IDS[0],
            cast(Any, bridge),
            cast(Any, memory),
            cast(Any, repository),
            delivery,
        ),
        entry,
        {"event_digest": digest("canonical"), "session_id": "session", "client_id": "instance"},
    )


def _execution() -> Any:
    return SimpleNamespace(
        project_id=IDS[1],
        work_item_id=IDS[2],
        run_id=IDS[3],
        plan_id=IDS[5],
        work_plan_digest=digest("work-plan"),
        source_digest=digest("source"),
        policy_digest=digest("policy"),
        migration_digest=digest("migration"),
        envelope_id=IDS[11],
        envelope_digest=digest("envelope"),
    )


def test_continuity_guards_preflight_replay_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission, entry, canonical = _admission()
    admission._assert_uow_identity()
    admission._assert_input(entry, canonical, client_instance_id="instance")
    execution = _execution()
    admission._assert_plan_current(execution)
    with pytest.raises(PolicyViolation, match="delivery binding"):
        admission._assert_input(entry, canonical, client_instance_id="other")
    with pytest.raises(PolicyViolation, match="current execution drift"):
        admission._assert_plan_current(
            SimpleNamespace(**(vars(execution) | {"source_digest": digest("other")}))
        )

    calls: list[str] = []
    cast(Any, admission.memory_continuity).assert_mutating_admission = lambda **_kw: calls.append(
        "checked"
    )
    admission._assert_common_mutating_admission(canonical)
    assert calls == ["checked"]

    cast(Any, admission.repository).current_execution = lambda **_kw: execution

    class Host:
        receipt: Any = None
        finishes: ClassVar[list[AttemptOutcome]] = []

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.ledger = SimpleNamespace(receipt_for_claim=lambda _claim_id: self.receipt)

        def finish(self, *_args: object, **kwargs: object) -> bool:
            self.finishes.append(cast(AttemptOutcome, kwargs["outcome"]))
            return True

    monkeypatch.setattr(continuity, "ExecutionHost", Host)
    preflight = admission.preflight(entry, canonical, client_instance_id="instance")
    assert preflight["allowed"] is True

    Host.receipt = SimpleNamespace(status=ReceiptStatus.FAILED)
    with pytest.raises(PolicyViolation, match="failed terminal"):
        admission.preflight(entry, canonical, client_instance_id="instance")

    Host.receipt = None
    cast(Any, admission.repository).ingest = lambda *_a, **_kw: (_ for _ in ()).throw(
        RuntimeError("boom")
    )
    with pytest.raises(RuntimeError, match="boom"):
        admission.apply(
            entry,
            canonical,
            preflight=preflight,
            client_instance_id="instance",
            now=NOW,
        )
    assert Host.finishes == [AttemptOutcome.RECOVERY_REQUIRED]

    Host.receipt = SimpleNamespace(status=ReceiptStatus.FAILED)
    with pytest.raises(PolicyViolation, match="failed claim"):
        admission.apply(
            entry,
            canonical,
            preflight=preflight,
            client_instance_id="instance",
            now=NOW,
        )


def test_continuity_uow_realm_and_hydration_authority_guards() -> None:
    admission, _entry, canonical = _admission("session_start")
    admission._assert_common_mutating_admission(canonical)
    with pytest.raises(PolicyViolation, match="identity drift"):
        admission._assert_common_mutating_admission(canonical | {"session_id": "other"})

    bad_connection = replace(
        admission, repository=SimpleNamespace(connection=object(), realm_id=IDS[0])
    )
    with pytest.raises(PolicyViolation, match="connection"):
        bad_connection._assert_uow_identity()
    bad_realm = replace(
        admission, repository=SimpleNamespace(connection=admission.connection, realm_id=IDS[20])
    )
    with pytest.raises(PolicyViolation, match="realm identity"):
        bad_realm._assert_uow_identity()


def _configure_fresh_admission(
    admission: PostgresLifecycleContinuityAdmission,
    entry: Any,
    *,
    compiler_enqueue: bool = True,
) -> None:
    applied = SimpleNamespace(
        event_digest=admission.delivery.plan.event.event_digest,
        event_id=IDS[30],
        outbox_id=IDS[31],
        plan_digest=admission.delivery.plan.plan_digest,
        project_id=IDS[1],
        work_item_id=IDS[2],
        run_id=IDS[3],
        session_id="session",
        client_id="codex",
    )
    repository = cast(Any, admission.repository)
    bridge = cast(Any, admission.bridge)
    memory = cast(Any, admission.memory_continuity)
    repository.current_execution = lambda **_kw: _execution()
    repository.ingest = lambda *_a, **_kw: SimpleNamespace(
        canonical_digest=digest("ack"), event_id=IDS[32]
    )
    repository.lookup_hook_terminal_output = lambda **_kw: SimpleNamespace(
        receipt_id=IDS[33], output_digest=digest("hook"), compiler_enqueue=compiler_enqueue
    )
    repository.exact_hydration_authorization_id = lambda **_kw: IDS[34]
    repository.record_governed_admission = lambda **_kw: None
    repository.store_job_checkpoint = lambda **_kw: None
    repository.record_lifecycle_hydration_admission = lambda **_kw: None
    bridge.apply = lambda *_a, **_kw: applied
    cast(Any, bridge.repository).finalize_lifecycle_delivery = lambda **_kw: None
    hydration_plan = SimpleNamespace(
        plan_digest=digest("hydration-plan"),
        effect_digest=digest("hydration-effect"),
        resource="memory:hydrate",
    )
    memory.prepare_hydration = lambda _preparation: hydration_plan
    memory.apply = lambda *_a, **_kw: SimpleNamespace(
        receipt_id=IDS[35],
        receipt_digest=digest("hydration-receipt"),
        plan_digest=hydration_plan.plan_digest,
        result_digest=digest("hydration-result"),
        created=True,
        applied_at=entry.occurred_at,
    )


def test_continuity_apply_success_hydration_and_terminal_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[AttemptOutcome] = []
    reject_success = [False]

    class Host:
        receipt: Any = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.ledger = SimpleNamespace(receipt_for_claim=lambda _claim_id: self.receipt)

        @staticmethod
        def record_success(*_args: object, **_kwargs: object) -> Any:
            return SimpleNamespace(id=IDS[36])

        @staticmethod
        def finish(*_args: object, **kwargs: object) -> bool:
            outcome = cast(AttemptOutcome, kwargs["outcome"])
            outcomes.append(outcome)
            return not (reject_success[0] and outcome is AttemptOutcome.SUCCEEDED)

    monkeypatch.setattr(continuity, "ExecutionHost", Host)
    monkeypatch.setattr(
        PostgresLifecycleContinuityAdmission,
        "_terminal_receipt",
        lambda *_a, **_kw: cast(Any, "terminal"),
    )

    admission, entry, canonical = _admission()
    _configure_fresh_admission(admission, entry)
    preflight = admission.preflight(entry, canonical, client_instance_id="instance")
    assert (
        cast(
            Any,
            admission.apply(
                entry,
                canonical,
                preflight=preflight,
                client_instance_id="instance",
                now=NOW,
            ),
        )
        == "terminal"
    )
    assert outcomes == [AttemptOutcome.SUCCEEDED]

    hydrating, hydration_entry, hydration_event = _admission("session_start")
    _configure_fresh_admission(hydrating, hydration_entry)
    preflight = hydrating.preflight(hydration_entry, hydration_event, client_instance_id="instance")
    with pytest.raises(PolicyViolation, match="hydration authorization"):
        hydrating.apply(
            hydration_entry,
            hydration_event,
            preflight=preflight,
            client_instance_id="instance",
            now=NOW,
        )

    hydrated = replace(
        hydrating,
        delivery=replace(hydrating.delivery, hydration_authorization_id=IDS[40]),
    )
    _configure_fresh_admission(hydrated, hydration_entry)
    assert (
        cast(
            Any,
            hydrated.apply(
                hydration_entry,
                hydration_event,
                preflight=preflight,
                client_instance_id="instance",
                now=NOW,
            ),
        )
        == "terminal"
    )

    wrong_authority = replace(
        admission,
        delivery=replace(admission.delivery, hydration_authorization_id=IDS[40]),
    )
    _configure_fresh_admission(wrong_authority, entry)
    with pytest.raises(PolicyViolation, match="yalniz bootstrap"):
        wrong_authority.apply(
            entry,
            canonical,
            preflight=preflight,
            client_instance_id="instance",
            now=NOW,
        )

    no_step, no_step_entry, no_step_event = _admission()
    cast(Any, no_step.delivery.work.job).step_id = None
    _configure_fresh_admission(no_step, no_step_entry)
    with pytest.raises(PolicyViolation, match="step_id"):
        no_step.apply(
            no_step_entry,
            no_step_event,
            preflight=preflight,
            client_instance_id="instance",
            now=NOW,
        )

    reject_success[0] = True
    _configure_fresh_admission(admission, entry)
    with pytest.raises(PolicyViolation, match="terminal finish"):
        admission.apply(
            entry,
            canonical,
            preflight=preflight,
            client_instance_id="instance",
            now=NOW,
        )


def test_continuity_terminal_receipt_rejects_missing_and_drifted_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission, entry, canonical = _admission()
    cast(Any, admission.repository).lookup = lambda _digest: SimpleNamespace()
    terminal = SimpleNamespace(
        effect_receipt_id=IDS[20],
        effect_result_digest=digest("result"),
        continuity_event_digest=admission.delivery.plan.event.event_digest,
        continuity_event_id=IDS[21],
        checkpoint_digest=digest("checkpoint"),
        adapter_evidence_digest=digest("wrong"),
        terminal_receipt_digest=digest("hook"),
    )
    cast(Any, admission.repository).lookup_terminal_delivery = lambda **_kw: terminal

    class Host:
        receipt: Any = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.ledger = SimpleNamespace(receipt_for_claim=lambda _claim_id: self.receipt)

    monkeypatch.setattr(continuity, "ExecutionHost", Host)
    with pytest.raises(PolicyViolation, match="completed effect receipt"):
        admission._terminal_receipt(entry, canonical)
    Host.receipt = SimpleNamespace(
        id=IDS[22],
        status=ReceiptStatus.COMPLETED,
        result_digest=digest("result"),
        as_dict=lambda: {},
    )
    with pytest.raises(PolicyViolation, match="effect receipt drift"):
        admission._terminal_receipt(entry, canonical)
    terminal.effect_receipt_id = IDS[22]
    with pytest.raises(PolicyViolation, match="adapter evidence drift"):
        admission._terminal_receipt(entry, canonical)


def test_continuity_completed_replay_preclose_and_compiler_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReplayHost:
        receipt: Any = SimpleNamespace(status=ReceiptStatus.COMPLETED)

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.ledger = SimpleNamespace(receipt_for_claim=lambda _claim_id: self.receipt)

    monkeypatch.setattr(continuity, "ExecutionHost", ReplayHost)
    monkeypatch.setattr(
        PostgresLifecycleContinuityAdmission,
        "_terminal_receipt",
        lambda *_a, **_kw: cast(Any, "replayed"),
    )
    admission, entry, canonical = _admission()
    preflight = admission.preflight(entry, canonical, client_instance_id="instance")
    assert preflight["allowed"] is True
    assert (
        cast(
            Any,
            admission.apply(
                entry,
                canonical,
                preflight=preflight,
                client_instance_id="instance",
                now=NOW,
            ),
        )
        == "replayed"
    )

    outcomes: list[AttemptOutcome] = []

    class FreshHost:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.ledger = SimpleNamespace(receipt_for_claim=lambda _claim_id: None)

        @staticmethod
        def record_success(*_args: object, **_kwargs: object) -> Any:
            return SimpleNamespace(id=IDS[36])

        @staticmethod
        def finish(*_args: object, **kwargs: object) -> bool:
            outcomes.append(cast(AttemptOutcome, kwargs["outcome"]))
            return True

    monkeypatch.setattr(continuity, "ExecutionHost", FreshHost)
    preclose, preclose_entry, preclose_event = _admission("pre_close")
    preclose = replace(
        preclose,
        delivery=replace(preclose.delivery, hydration_authorization_id=IDS[40]),
    )
    _configure_fresh_admission(preclose, preclose_entry)
    preflight = preclose.preflight(preclose_entry, preclose_event, client_instance_id="instance")
    assert (
        cast(
            Any,
            preclose.apply(
                preclose_entry,
                preclose_event,
                preflight=preflight,
                client_instance_id="instance",
                now=NOW,
            ),
        )
        == "replayed"
    )

    compact, compact_entry, compact_event = _admission("pre_compaction")
    _configure_fresh_admission(compact, compact_entry, compiler_enqueue=False)
    preflight = compact.preflight(compact_entry, compact_event, client_instance_id="instance")
    with pytest.raises(PolicyViolation, match="compiler enqueue"):
        compact.apply(
            compact_entry,
            compact_event,
            preflight=preflight,
            client_instance_id="instance",
            now=NOW,
        )
    assert AttemptOutcome.RECOVERY_REQUIRED in outcomes


def test_continuity_recovery_finish_rejection_and_hydration_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission, _entry, _canonical = _admission()

    class RejectHost:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.ledger = SimpleNamespace(receipt_for_claim=lambda _claim_id: None)

        @staticmethod
        def finish(*_args: object, **_kwargs: object) -> bool:
            return False

    with (
        pytest.raises(PolicyViolation, match="recovery-required finish"),
        admission._recover_on_failure(cast(Any, RejectHost())),
    ):
        raise RuntimeError("injected")

    hydrating, hydration_entry, hydration_event = _admission("session_start")
    looked_up: list[UUID] = []
    cast(Any, hydrating.repository).lookup = lambda _digest: SimpleNamespace()
    terminal = SimpleNamespace(
        effect_receipt_id=IDS[20],
        effect_result_digest=digest("result"),
        continuity_event_digest=hydrating.delivery.plan.event.event_digest,
        continuity_event_id=IDS[21],
        checkpoint_digest=digest("checkpoint"),
        adapter_evidence_digest=digest("wrong"),
        terminal_receipt_digest=digest("hook"),
    )
    cast(Any, hydrating.repository).lookup_terminal_delivery = lambda **_kw: terminal
    cast(Any, hydrating.repository).lookup_lifecycle_hydration = lambda **kw: looked_up.append(
        cast(UUID, kw["continuity_event_id"])
    )

    class CompletedHost:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.ledger = SimpleNamespace(
                receipt_for_claim=lambda _claim_id: SimpleNamespace(
                    id=IDS[20],
                    status=ReceiptStatus.COMPLETED,
                    result_digest=digest("result"),
                    as_dict=lambda: {},
                )
            )

    monkeypatch.setattr(continuity, "ExecutionHost", CompletedHost)
    with pytest.raises(PolicyViolation, match="adapter evidence drift"):
        hydrating._terminal_receipt(hydration_entry, hydration_event)
    assert looked_up == [IDS[21]]


def test_composition_finish_and_drain_binding_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry, plan, work, claim = _delivery_boundary()
    states = ["recovery-required-closed", "attempt-drift", "running-exact"]
    finish_results = [True]

    class Host:
        receipt: Any = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.ledger = SimpleNamespace(receipt_for_claim=lambda _claim_id: self.receipt)

        @staticmethod
        def finish(*_args: object, **_kwargs: object) -> bool:
            return finish_results[0]

    monkeypatch.setattr(composition, "ExecutionHost", Host)
    repository = SimpleNamespace(
        connection=object(), realm_id=IDS[0], recovery_finish_state=lambda **_kw: states[0]
    )
    composition._finish_receiptless_recovery(
        repository=repository, work=work, claim=claim, reason="reason", rejected_message="reject"
    )
    repository.recovery_finish_state = lambda **_kw: states[1]
    with pytest.raises(PolicyViolation, match="state drift"):
        composition._finish_receiptless_recovery(
            repository=repository,
            work=work,
            claim=claim,
            reason="reason",
            rejected_message="reject",
        )
    repository.recovery_finish_state = lambda **_kw: states[2]
    finish_results[0] = False
    with pytest.raises(PolicyViolation, match="reject"):
        composition._finish_receiptless_recovery(
            repository=repository,
            work=work,
            claim=claim,
            reason="reason",
            rejected_message="reject",
        )
    Host.receipt = SimpleNamespace()
    composition._finish_receiptless_recovery(
        repository=repository, work=work, claim=claim, reason="reason", rejected_message="reject"
    )

    spool = SimpleNamespace(pending=lambda **_kw: (entry,), client_instance_id=lambda: "instance")
    repository = SimpleNamespace(
        connection=object(),
        realm_id=IDS[0],
        current_work_plan_digest=lambda **_kw: digest("work-plan"),
        previous_continuity_digest=lambda **_kw: None,
    )
    inputs = LifecyclePlanInputs(
        "git:source",
        digest("source"),
        digest("policy"),
        digest("migration"),
        digest("work-plan"),
        None,
        None,
    )
    bridge = SimpleNamespace(
        repository=object(),
        authorizations=object(),
        prepare=lambda *_a, **_kw: plan,
    )
    missing = SimpleNamespace(
        **(vars(work) | {"job": SimpleNamespace(**(vars(work.job) | {"run_id": None}))})
    )
    with pytest.raises(PolicyViolation, match="run/work/plan"):
        composition.drain_claimed_codex_delivery(
            spool=cast(Any, spool),
            bridge=cast(Any, bridge),
            repository=repository,
            work=cast(Any, missing),
            claim=cast(Any, claim),
            authorization_id=IDS[9],
            contract=cast(Any, object()),
            hook_session=cast(Any, object()),
            session_binding_id=IDS[10],
            inputs=inputs,
        )
    repository.current_work_plan_digest = lambda **_kw: digest("drift")
    with pytest.raises(PolicyViolation, match="TaskPlan digest drift"):
        composition.drain_claimed_codex_delivery(
            spool=cast(Any, spool),
            bridge=cast(Any, bridge),
            repository=repository,
            work=cast(Any, work),
            claim=cast(Any, claim),
            authorization_id=IDS[9],
            contract=cast(Any, object()),
            hook_session=cast(Any, object()),
            session_binding_id=IDS[10],
            inputs=inputs,
        )
    repository.current_work_plan_digest = lambda **_kw: digest("work-plan")
    with pytest.raises(PolicyViolation, match="claim/plan binding"):
        composition.drain_claimed_codex_delivery(
            spool=cast(Any, spool),
            bridge=cast(Any, bridge),
            repository=repository,
            work=cast(Any, work),
            claim=cast(
                Any,
                SimpleNamespace(**(vars(claim) | {"effect_digest": digest("wrong")})),
            ),
            authorization_id=IDS[9],
            contract=cast(Any, object()),
            hook_session=cast(Any, object()),
            session_binding_id=IDS[10],
            inputs=inputs,
        )


def test_composition_missing_pending_with_existing_receipt_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entry, _plan, work, claim = _delivery_boundary()

    class Host:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.ledger = SimpleNamespace(receipt_for_claim=lambda _claim_id: SimpleNamespace())

    monkeypatch.setattr(composition, "ExecutionHost", Host)
    result = composition.drain_claimed_codex_delivery(
        spool=cast(Any, SimpleNamespace(pending=lambda **_kw: ())),
        bridge=cast(Any, object()),
        repository=SimpleNamespace(connection=object(), realm_id=IDS[0]),
        work=work,
        claim=claim,
        authorization_id=IDS[9],
        contract=cast(Any, object()),
        hook_session=cast(Any, object()),
        session_binding_id=IDS[10],
        inputs=LifecyclePlanInputs(
            "git:source",
            digest("source"),
            digest("policy"),
            digest("migration"),
            digest("work-plan"),
            None,
            None,
        ),
    )
    assert result == ()


def _valid_terminal(entry: Any) -> tuple[dict[str, Any], Any]:
    continuity_event_digest = digest("event")
    resources = [{"resource": "memory:session", "mode": "write"}]
    effect_digest = digest("effect")
    plan_body = {
        "schema": "zekam-lifecycle-bridge-plan/v1",
        "event_digest": continuity_event_digest,
        "idempotency_key": entry.delivery_id,
        "resource": "memory:session",
        "source_digest": digest("source"),
        "policy_digest": digest("policy"),
        "migration_digest": digest("migration"),
        "effect_digest": effect_digest,
        "grants_authority": False,
    }
    terminal: dict[str, Any] = {
        "client_id": "codex",
        "session_id": entry.session_id,
        "event_type": entry.internal_event_type,
        "continuity_event_id": IDS[30],
        "operation": LIFECYCLE_EFFECT_OPERATION,
        "adapter_digest": LIFECYCLE_ADAPTER_DIGEST,
        "resources": resources,
        "authorization_scope": {
            "allowed_resources": ["memory:session"],
            "allowed_effects": ["database-write"],
            "provider_refs": [],
            "secret_ref_ids": [],
            "data_classifications": ["internal"],
        },
        "effect_plan_body": plan_body,
        "effect_plan_digest": digest(plan_body),
        "continuity_event_digest": continuity_event_digest,
        "source_digest": plan_body["source_digest"],
        "policy_digest": plan_body["policy_digest"],
        "migration_digest": plan_body["migration_digest"],
        "effect_digest": effect_digest,
        "job_id": IDS[31],
        "authorization_digest": digest("authorization"),
        "claim_idempotency_key": f"{entry.delivery_id}:job:{IDS[31]}",
        "execution_identity": "worker:4",
        "worker_label": "worker",
        "fencing_token": 4,
        "work_plan_digest": digest("work-plan"),
        "stored_work_plan_digest": digest("work-plan"),
        "delivery_outbox_id": IDS[32],
        "hook_receipt_id": IDS[33],
        "hook_output_digest": digest("hook"),
        "compiler_enqueue": False,
        "effect_result_digest": digest("wrong-result"),
        "result_formula_digest": digest("wrong-result"),
        "adapter_evidence_digest": digest("wrong-adapter"),
        "effect_status": "failed",
    }
    terminal["claim_digest"] = digest(
        {
            "job_id": str(terminal["job_id"]),
            "operation": terminal["operation"],
            "effect_digest": terminal["effect_digest"],
            "authorization_digest": terminal["authorization_digest"],
            "idempotency_key": terminal["claim_idempotency_key"],
            "resources": resources,
            "execution_identity": terminal["execution_identity"],
            "fencing_token": terminal["fencing_token"],
            "adapter_digest": terminal["adapter_digest"],
        }
    )
    generic = SimpleNamespace(canonical_digest=digest("ack"))
    return terminal, generic


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ({"client_id": "other"}, "session binding"),
        ({"event_type": "other"}, "event type"),
        ({"operation": "other"}, "operation drift"),
        ({"adapter_digest": digest("other")}, "adapter drift"),
        ({"resources": []}, "authorization scope"),
        ({"effect_plan_digest": digest("other")}, "stored recomputation"),
        ({"claim_digest": digest("other")}, "claim/execution"),
        ({}, "result/adapter"),
    ),
)
def test_committed_recovery_rejects_each_durable_drift(
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, object],
    message: str,
) -> None:
    entry, _plan, _work, _claim = _delivery_boundary()
    spool = SimpleNamespace(
        pending=lambda **_kw: (entry,),
        client_instance_id=lambda: "instance",
        previous_canonical_event_digest=lambda _entry: None,
    )
    terminal, generic = _valid_terminal(entry)
    terminal.update(change)
    repository = SimpleNamespace(
        realm_id=IDS[0],
        resolve_committed_delivery=lambda **_kw: terminal,
        lookup=lambda _digest: generic,
        lookup_lifecycle_hydration=lambda **_kw: None,
    )
    monkeypatch.setattr(
        composition,
        "canonical_lifecycle_event",
        lambda *_a, **_kw: {"event_digest": digest("canonical")},
    )
    with pytest.raises(PolicyViolation, match=message):
        composition.recover_committed_codex_delivery(spool=cast(Any, spool), repository=repository)


def test_committed_recovery_empty_spool_returns_none() -> None:
    assert (
        composition.recover_committed_codex_delivery(
            spool=cast(Any, SimpleNamespace(pending=lambda **_kw: ())), repository=object()
        )
        is None
    )


def _compose_handler_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entries: tuple[Any, ...],
    contract_digest: str = CODEX_REVIEWED_CLIENT_CONTRACT_DIGEST,
) -> tuple[Any, Any, Any]:
    plan = SimpleNamespace(
        plan_digest=digest("lifecycle-plan"),
        effect_digest=digest("lifecycle-effect"),
        resource="memory:session",
        body=lambda: {"schema": "plan"},
    )
    authorization = SimpleNamespace(
        id=IDS[50],
        work_item_id=IDS[2],
        plan_id=IDS[5],
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        authorization_digest=digest("authorization"),
    )
    repository = SimpleNamespace(
        realm_id=IDS[0],
        connection=object(),
        active_hook_runtime_binding=lambda: (1, digest("config"), digest("hooks")),
        claimed_plan_inputs=lambda **_kw: {
            "source_revision": "git:source",
            "source_digest": digest("source"),
            "policy_digest": digest("policy"),
            "migration_digest": digest("migration"),
            "work_plan_digest": digest("work-plan"),
            "checkpoint_ref": None,
            "context_ref": "context",
        },
        previous_continuity_digest=lambda **_kw: None,
    )
    authorizations = SimpleNamespace(get=lambda _id: authorization)
    hook_store = SimpleNamespace(start_session=lambda **_kw: IDS[51])
    stores = {
        "client_lifecycle": repository,
        "memory_continuity": SimpleNamespace(),
        "authorization": authorizations,
        "hook_runtime": hook_store,
    }

    class Runtime:
        @staticmethod
        def reconfigure(**_kwargs: object) -> Any:
            return SimpleNamespace(hook_set_digest=digest("hooks"))

        @staticmethod
        def start_session() -> Any:
            return SimpleNamespace()

    class Host:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @staticmethod
        def claim_effect(*_args: object, **_kwargs: object) -> Any:
            return SimpleNamespace(id=IDS[52])

    bridge = SimpleNamespace(prepare=lambda *_a, **_kw: plan)
    monkeypatch.setattr(composition, "legacy_repository", lambda name, *_a, **_kw: stores[name])
    monkeypatch.setattr(composition, "HookRuntime", lambda **_kw: Runtime())
    monkeypatch.setattr(
        composition,
        "memory_hook_bundle",
        lambda _realm: SimpleNamespace(specs=(), runtimes=(), profile=object(), adapters=()),
    )
    monkeypatch.setattr(
        composition,
        "LifecycleClientContract",
        SimpleNamespace(verified=lambda **_kw: SimpleNamespace(contract_digest=contract_digest)),
    )
    monkeypatch.setattr(
        composition,
        "load_codex_contract_evidence",
        lambda _path: {"file_digest": digest("contract")},
    )
    monkeypatch.setattr(composition, "codex_lifecycle_descriptor", lambda *_a, **_kw: object())
    monkeypatch.setattr(composition, "ClientLifecycleBridge", lambda *_a, **_kw: bridge)
    monkeypatch.setattr(
        composition,
        "ClientLifecycleSpool",
        lambda *_a, **_kw: SimpleNamespace(pending=lambda **_kw2: entries),
    )
    monkeypatch.setattr(composition, "ExecutionHost", Host)
    return repository, authorization, plan


def _handler_work(**payload_changes: object) -> Any:
    payload: dict[str, object] = {
        "schema": "zekam-codex-lifecycle-job/v1",
        "authorization_id": str(IDS[50]),
    }
    payload.update(payload_changes)
    return SimpleNamespace(
        job=SimpleNamespace(
            id=IDS[4],
            project_id=IDS[1],
            work_item_id=IDS[2],
            run_id=IDS[3],
            plan_id=IDS[5],
            kind=SimpleNamespace(value="mutation"),
            max_attempts=1,
            payload=payload,
        ),
        attempt_id=IDS[6],
        lease=SimpleNamespace(
            id=IDS[7], owner_digest=digest("owner"), fencing_token=4, worker_label="worker"
        ),
    )


def test_composed_handler_validates_job_authority_and_ack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entry = SimpleNamespace(
        entry_digest=digest("entry"),
        delivery_id="delivery",
        client_id="codex",
        session_id="session",
        external_event_type="PostCompact",
        internal_event_type="post_compaction",
        sequence=1,
        observation={},
        occurred_at=NOW,
    )
    _repository, authorization, plan = _compose_handler_fakes(monkeypatch, entries=(entry,))
    result = LifecycleReplayResult(
        entry.entry_digest, "completed", digest("ack"), digest("attempt")
    )
    monkeypatch.setattr(composition, "drain_claimed_codex_delivery", lambda **_kw: (result,))
    handler = composition.compose_codex_lifecycle_handler(
        connection=object(), realm_id=IDS[0], home=tmp_path
    )
    assert handler(_handler_work()) == result.canonical_ack_digest

    invalid_cases = (
        (_handler_work(extra=True), "immutable job payload"),
        (_handler_work(authorization_id="bad"), "authorization_id UUID"),
    )
    for work, message in invalid_cases:
        with pytest.raises(PolicyViolation, match=message):
            handler(work)
    missing_identity = _handler_work()
    missing_identity.job.run_id = None
    with pytest.raises(PolicyViolation, match="queue identity"):
        handler(missing_identity)
    wrong_kind = _handler_work()
    wrong_kind.job.kind = SimpleNamespace(value="read")
    with pytest.raises(PolicyViolation, match="mutation"):
        handler(wrong_kind)

    planned = _handler_work(lifecycle_plan_body={"schema": "other"})
    with pytest.raises(PolicyViolation, match="stored plan body"):
        handler(planned)
    authorization.plan_digest = digest("other")
    with pytest.raises(PolicyViolation, match="exact plan drift"):
        handler(_handler_work())
    authorization.plan_digest = plan.plan_digest

    monkeypatch.setattr(
        composition,
        "drain_claimed_codex_delivery",
        lambda **_kw: (LifecycleReplayResult(entry.entry_digest, "completed", None, digest("a")),),
    )
    with pytest.raises(PolicyViolation, match="terminal ACK"):
        handler(_handler_work())


def test_composed_handler_rejects_missing_hydration_and_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entry = SimpleNamespace(
        entry_digest=digest("entry"),
        delivery_id="delivery",
        client_id="codex",
        session_id="session",
        external_event_type="SessionStart",
        internal_event_type="session_start",
        sequence=1,
        observation={},
        occurred_at=NOW,
    )
    _compose_handler_fakes(monkeypatch, entries=(entry,))
    handler = composition.compose_codex_lifecycle_handler(
        connection=object(), realm_id=IDS[0], home=tmp_path
    )
    with pytest.raises(PolicyViolation, match="hydration bootstrap"):
        handler(_handler_work())
    with pytest.raises(PolicyViolation, match="hydration_authorization_id UUID"):
        handler(_handler_work(hydration_authorization_id="bad"))

    _compose_handler_fakes(monkeypatch, entries=())
    empty_handler = composition.compose_codex_lifecycle_handler(
        connection=object(), realm_id=IDS[0], home=tmp_path
    )
    with pytest.raises(PolicyViolation, match="pending delivery"):
        empty_handler(_handler_work())

    _compose_handler_fakes(monkeypatch, entries=(), contract_digest=digest("wrong"))
    with pytest.raises(PolicyViolation, match="contract digest drift"):
        composition.compose_codex_lifecycle_handler(
            connection=object(), realm_id=IDS[0], home=tmp_path
        )
