from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from typer.main import get_command

from zekam.application import client_lifecycle_continuity as lifecycle_continuity
from zekam.application.client_lifecycle_continuity import (
    PostgresLifecycleContinuityAdmission,
)
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.config import PersistenceBackend
from zekam.application.mutation_admission import (
    DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY,
    CliMutationEvidence,
    CliMutationTargetHints,
    MutationAdmissionExemption,
    assert_cli_mutation_admission,
    assert_local_effect_admission,
)
from zekam.application.opencode_lifecycle import lifecycle_root
from zekam.application.realm_context import RealmContext
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.interfaces.cli import session as cli_session
from zekam.interfaces.cli.main import app

REALM_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
WORK_ITEM_ID = UUID("00000000-0000-0000-0000-000000000003")
RUN_ID = UUID("00000000-0000-0000-0000-000000000004")
RUN_ID_TWO = UUID("00000000-0000-0000-0000-000000000005")
AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-000000000006")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000007")
TRACE_ID = UUID("00000000-0000-0000-0000-000000000008")


class _Cursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.statement = ""
        self.parameters: tuple[object, ...] = ()

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
        self.statement = statement
        self.parameters = parameters

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class _Connection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.cursor_instance = _Cursor(rows)

    def cursor(self) -> _Cursor:
        return self.cursor_instance


class _TargetFilteringCursor(_Cursor):
    def __init__(
        self,
        rows: list[tuple[object, ...]],
        *,
        target_run_id: UUID,
    ) -> None:
        super().__init__(rows)
        self.target_run_id = target_run_id

    def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
        super().execute(statement, parameters)
        if "run.id::text=%s" in statement:
            self.rows = [row for row in self.rows if str(row[2]) == str(self.target_run_id)]


class _TargetFilteringConnection(_Connection):
    def __init__(
        self,
        rows: list[tuple[object, ...]],
        *,
        target_run_id: UUID,
    ) -> None:
        self.cursor_instance = _TargetFilteringCursor(
            rows,
            target_run_id=target_run_id,
        )


def _leaf_commands() -> dict[tuple[str, ...], Any]:
    """Enumerate the real Typer leaves without inferring mutation from help text."""

    leaves: dict[tuple[str, ...], Any] = {}

    def visit(command: Any, prefix: tuple[str, ...]) -> None:
        children = getattr(command, "commands", None)
        if children:
            for name, child in sorted(children.items()):
                visit(child, (*prefix, str(name)))
            return
        leaves[prefix] = command

    visit(get_command(app), ())
    return leaves


def _apply_command_paths() -> tuple[tuple[str, ...], ...]:
    """Enumerate the real Typer apply surface by parsed Python parameter name."""

    return tuple(
        path
        for path, command in _leaf_commands().items()
        if any(parameter.name == "apply" for parameter in command.params)
    )


def test_registry_exemptions_are_narrow_and_never_grant_authority() -> None:
    registry = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY
    exemptions = dict(registry.exemptions)
    expected = {
        ("auth", "revoke"): MutationAdmissionExemption.CONTROL_PLANE,
        ("backup", "create"): MutationAdmissionExemption.LOCAL_EFFECT,
        ("backup", "verify"): MutationAdmissionExemption.LOCAL_EFFECT,
        ("client", "hook"): MutationAdmissionExemption.LOCAL_EFFECT,
        ("db", "upgrade"): MutationAdmissionExemption.BOOTSTRAP,
        ("doctor",): MutationAdmissionExemption.CONTROL_PLANE,
        ("init",): MutationAdmissionExemption.BOOTSTRAP,
        ("knowledge", "ingest"): MutationAdmissionExemption.CONTROL_PLANE,
        ("knowledge", "vector-index"): MutationAdmissionExemption.CONTROL_PLANE,
        ("memory", "gap-repair-apply"): MutationAdmissionExemption.RECOVERY,
        ("memory", "hook-upgrade-apply"): MutationAdmissionExemption.CONTROL_PLANE,
        ("memory", "hydration-apply"): MutationAdmissionExemption.HYDRATION,
        ("memory", "upgrade-apply-shadow"): MutationAdmissionExemption.BOOTSTRAP,
        ("model", "benchmark"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "campaign", "authorize"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "campaign", "run"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "capability", "authorize"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "capability", "run"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "health"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "inventory"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "opencode-embedding-probe"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "provider-authorize"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "provider-live-run"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "report"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "route", "decide"): MutationAdmissionExemption.CONTROL_PLANE,
        ("model", "route", "prepare"): MutationAdmissionExemption.CONTROL_PLANE,
        ("opencode", "spool-cleanup"): MutationAdmissionExemption.CONTROL_PLANE,
        ("opencode", "event"): MutationAdmissionExemption.LOCAL_EFFECT,
        ("oracle", "index"): MutationAdmissionExemption.CONTROL_PLANE,
        ("policy", "init"): MutationAdmissionExemption.BOOTSTRAP,
        ("project", "add"): MutationAdmissionExemption.BOOTSTRAP,
        ("project", "index"): MutationAdmissionExemption.CONTROL_PLANE,
        ("project", "integrate"): MutationAdmissionExemption.CONTROL_PLANE,
        ("project", "rebind"): MutationAdmissionExemption.CONTROL_PLANE,
        ("project", "remove"): MutationAdmissionExemption.CONTROL_PLANE,
        ("project", "restore"): MutationAdmissionExemption.CONTROL_PLANE,
        ("project", "scan"): MutationAdmissionExemption.CONTROL_PLANE,
        ("protocol", "generate-json-schema"): MutationAdmissionExemption.LOCAL_EFFECT,
        ("protocol", "generate-typescript"): MutationAdmissionExemption.LOCAL_EFFECT,
        ("research", "report", "rebuild"): MutationAdmissionExemption.CONTROL_PLANE,
        ("scheduler", "init"): MutationAdmissionExemption.BOOTSTRAP,
        ("secret", "add"): MutationAdmissionExemption.CONTROL_PLANE,
        ("secret", "revoke"): MutationAdmissionExemption.CONTROL_PLANE,
        ("setup",): MutationAdmissionExemption.BOOTSTRAP,
        ("trace", "purge-expired"): MutationAdmissionExemption.CONTROL_PLANE,
        ("work", "create"): MutationAdmissionExemption.BOOTSTRAP,
        ("work", "activate"): MutationAdmissionExemption.CONTROL_PLANE,
        ("work", "activation-rollback"): MutationAdmissionExemption.CONTROL_PLANE,
        ("work", "relate"): MutationAdmissionExemption.CONTROL_PLANE,
        ("work", "reopen"): MutationAdmissionExemption.CONTROL_PLANE,
        ("work", "verify"): MutationAdmissionExemption.CONTROL_PLANE,
        ("worker", "codex-lifecycle-tick"): MutationAdmissionExemption.HYDRATION,
        ("worker", "client-runtime-bootstrap"): MutationAdmissionExemption.CONTROL_PLANE,
        ("worker", "lifecycle-template-prepare"): MutationAdmissionExemption.CONTROL_PLANE,
        ("worker", "lifecycle-template-recovery"): MutationAdmissionExemption.RECOVERY,
        ("worker", "lifecycle-template-tick"): MutationAdmissionExemption.CONTROL_PLANE,
        ("worker", "reconcile-failed-receipt"): MutationAdmissionExemption.RECOVERY,
        ("worker", "reconcile-terminal-run"): MutationAdmissionExemption.RECOVERY,
        ("worker", "reconcile-recovery"): MutationAdmissionExemption.RECOVERY,
        ("worker", "recovery-authorize"): MutationAdmissionExemption.RECOVERY,
        ("worker", "run"): MutationAdmissionExemption.RECOVERY,
        ("worker", "tick"): MutationAdmissionExemption.RECOVERY,
    }
    assert exemptions == expected

    hydration = registry.classify(("memory", "hydration-apply"), {"apply": True})
    assert hydration.mutating
    assert hydration.requires_full_continuity
    assert not hydration.requires_existing_hydration
    assert hydration.exemption is MutationAdmissionExemption.HYDRATION
    assert not hydration.grants_authority


@pytest.mark.parametrize(
    "path",
    [
        ("backup", "create"),
        ("backup", "verify"),
        ("client", "hook"),
        ("opencode", "event"),
        ("protocol", "generate-json-schema"),
        ("protocol", "generate-typescript"),
    ],
)
def test_local_effects_are_exact_visible_and_authority_free(path: tuple[str, ...]) -> None:
    admission = assert_local_effect_admission(path)

    assert admission.mutating
    assert not admission.requires_full_continuity
    assert not admission.requires_existing_hydration
    assert admission.exemption is MutationAdmissionExemption.LOCAL_EFFECT
    assert not admission.grants_authority


def test_unknown_local_effect_cannot_claim_the_carve_out() -> None:
    with pytest.raises(PolicyViolation, match="exact reviewed admission"):
        assert_local_effect_admission(("future", "writer"))


def test_exemption_does_not_allow_sqlite_or_weaken_backend_gate() -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("memory", "hydration-apply"), {"apply": True}
    )

    with pytest.raises(ZekamError, match="backend veya authorization yetkisi vermez"):
        assert_cli_mutation_admission(
            backend="sqlite",
            supports_full_continuity=False,
            admission=admission,
            realm_session_required=True,
        )


def test_read_only_plan_is_not_mislabeled_as_mutation() -> None:
    plan = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("memory", "hydration-apply"), {"apply": False}
    )

    assert not plan.mutating
    assert plan.exemption is None
    assert not plan.requires_existing_hydration
    assert not plan.grants_authority


@pytest.mark.parametrize("parameter_name", ["apply", "uygula"])
def test_apply_parameter_aliases_cannot_create_false_negative(parameter_name: str) -> None:
    hydration = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("memory", "hydration-apply"), {parameter_name: True}
    )
    close = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("close", "apply"), {parameter_name: True}
    )

    assert hydration.mutating and close.mutating
    assert hydration.exemption is MutationAdmissionExemption.HYDRATION
    assert not hydration.requires_existing_hydration
    assert hydration.requires_full_continuity
    assert close.exemption is None
    assert close.requires_existing_hydration
    assert close.requires_full_continuity


def test_disagreeing_apply_aliases_fail_closed_as_mutation() -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("close", "apply"), {"apply": False, "uygula": True}
    )

    assert admission.mutating
    assert admission.requires_existing_hydration


def test_unknown_apply_leaf_defaults_to_full_continuity_fail_closed() -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("future-plugin", "hidden-write"), {"apply": True}
    )

    assert admission.mutating
    assert admission.requires_full_continuity
    assert admission.requires_existing_hydration
    assert admission.exemption is None


def test_canonical_lifecycle_command_is_always_full_continuity_mutation() -> None:
    lifecycle = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(("opencode", "forward"), {})

    assert lifecycle.mutating
    assert lifecycle.requires_full_continuity
    assert lifecycle.requires_existing_hydration
    assert lifecycle.exemption is None
    assert not lifecycle.grants_authority


def test_opencode_forward_bootstrap_is_exactly_one_immutable_first_created_event() -> None:
    def evidence(
        *, event_type: str, sequence: int, previous_digest: str | None
    ) -> CliMutationEvidence:
        body = {
            "schema": "zekam-opencode-lifecycle-event/v2",
            "event_type": event_type,
            "session_id": "session-one",
            "sequence": sequence,
            "previous_digest": previous_digest,
            "grants_authority": False,
        }
        event_digest = digest(body)
        return CliMutationEvidence(
            kind="opencode-forward-event",
            evidence_digest=event_digest,
            target_hints=CliMutationTargetHints(session_ref="session-one"),
            event_type=event_type,
            sequence=sequence,
            previous_digest=previous_digest,
            canonical_input=json.dumps(
                body | {"event_digest": event_digest},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    first_created = evidence(event_type="session.created", sequence=1, previous_digest=None)
    bootstrap = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("opencode", "forward"), {}, evidence=first_created
    )
    later = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("opencode", "forward"),
        {},
        evidence=evidence(
            event_type="session.status",
            sequence=2,
            previous_digest=first_created.evidence_digest,
        ),
    )

    assert bootstrap.exemption is MutationAdmissionExemption.BOOTSTRAP
    assert not bootstrap.requires_existing_hydration
    assert bootstrap.target_hints.session_ref == "session-one"
    assert not bootstrap.grants_authority
    assert later.exemption is None
    assert later.requires_existing_hydration

    with pytest.raises(PolicyViolation, match="immutable event binding drift"):
        CliMutationEvidence(
            kind="opencode-forward-event",
            evidence_digest=first_created.evidence_digest,
            target_hints=first_created.target_hints,
            event_type="session.status",
            sequence=1,
            previous_digest=None,
            canonical_input=first_created.canonical_input,
        )


def test_all_real_parameterless_mutations_are_explicitly_enumerated() -> None:
    expected = {
        ("opencode", "forward"),
        ("opencode", "pre-compact"),
        ("trace", "reduce"),
    }
    registry = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY
    leaves = _leaf_commands()

    assert set(registry.always_mutating_commands) == expected
    for path in expected:
        assert path in leaves
        assert all(parameter.name != "apply" for parameter in leaves[path].params)
        admission = registry.classify(path, {})
        assert admission.mutating, " ".join(path)
        assert admission.requires_full_continuity, " ".join(path)
        assert admission.requires_existing_hydration, " ".join(path)
        assert admission.exemption is None


def test_codex_worker_spool_is_not_an_opencode_forward_bootstrap_path(
    tmp_path: Path,
) -> None:
    """Codex tick cannot justify exempting the distinct OpenCode event batch."""

    opencode_root = lifecycle_root(tmp_path)
    codex_root = ClientLifecycleSpool(tmp_path, client_id="codex").root

    assert opencode_root == tmp_path / "global" / "runtime" / "opencode-lifecycle"
    assert codex_root == tmp_path / "global" / "runtime" / "client-lifecycle" / "codex"
    assert codex_root != opencode_root
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(("opencode", "forward"), {})
    assert admission.requires_existing_hydration
    assert admission.exemption is None


def test_codex_lifecycle_non_session_entry_rechecks_exact_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command exemption delegates each one-item effect to this exact gate."""

    command_admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("worker", "codex-lifecycle-tick"), {"apply": True}
    )
    assert command_admission.exemption is MutationAdmissionExemption.HYDRATION
    assert not command_admission.requires_existing_hydration

    entry = SimpleNamespace(
        internal_event_type="pre_compaction",
        entry_digest="sha256:" + "3" * 64,
    )
    canonical_event = {"event_digest": "sha256:" + "4" * 64}
    execution = object()
    current_calls: list[object] = []
    currentness_calls: list[object] = []
    delivery = SimpleNamespace(
        claim=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000011")),
        authorization_id=UUID("00000000-0000-0000-0000-000000000012"),
        work=SimpleNamespace(job=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000013"))),
        plan=SimpleNamespace(
            plan_digest="sha256:" + "1" * 64,
            effect_digest="sha256:" + "2" * 64,
            event=SimpleNamespace(
                project_id=PROJECT_ID,
                work_item_id=WORK_ITEM_ID,
                run_id=RUN_ID,
            ),
        ),
    )

    class _Ledger:
        def receipt_for_claim(self, _claim_id: UUID) -> None:
            return None

    class _ExecutionHost:
        def __init__(self, _connection: object, _realm_id: UUID) -> None:
            self.ledger = _Ledger()

    def current_execution(
        _self: object,
        current_entry: object,
        *,
        now: object,
        allow_consumed: bool = False,
    ) -> object:
        del now, allow_consumed
        current_calls.append(current_entry)
        return execution

    def assert_plan_current(_self: object, current: object) -> None:
        currentness_calls.append(current)

    monkeypatch.setattr(lifecycle_continuity, "ExecutionHost", _ExecutionHost)
    monkeypatch.setattr(
        PostgresLifecycleContinuityAdmission,
        "_assert_uow_identity",
        lambda _self: None,
    )
    monkeypatch.setattr(
        PostgresLifecycleContinuityAdmission,
        "_assert_input",
        lambda _self, _entry, _canonical_event, *, client_instance_id: None,
    )
    monkeypatch.setattr(
        PostgresLifecycleContinuityAdmission,
        "_current_execution",
        current_execution,
    )
    monkeypatch.setattr(
        PostgresLifecycleContinuityAdmission,
        "_assert_plan_current",
        assert_plan_current,
    )
    admission = PostgresLifecycleContinuityAdmission(
        object(),
        REALM_ID,
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, delivery),
    )

    preflight = admission.preflight(
        cast(Any, entry),
        canonical_event,
        client_instance_id="codex-instance",
    )

    assert preflight["allowed"] is True
    assert preflight["mutation_performed"] is False
    assert current_calls == [entry]
    assert currentness_calls == [execution]


def test_every_real_apply_parameter_is_classified_mutating_without_alias_bypass() -> None:
    paths = _apply_command_paths()

    assert len(paths) == 67
    for path in paths:
        python_name = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(path, {"apply": True})
        public_name = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(path, {"uygula": True})
        assert python_name.mutating and public_name.mutating, " ".join(path)
        assert not python_name.grants_authority and not public_name.grants_authority
        assert python_name.requires_existing_hydration == (
            python_name.requires_full_continuity and python_name.exemption is None
        )
        assert public_name.requires_existing_hydration == (
            public_name.requires_full_continuity and public_name.exemption is None
        )
        assert python_name.requires_existing_hydration or python_name.exemption in {
            MutationAdmissionExemption.BOOTSTRAP,
            MutationAdmissionExemption.CONTROL_PLANE,
            MutationAdmissionExemption.HYDRATION,
            MutationAdmissionExemption.RECOVERY,
        }, " ".join(path)


def test_apply_surface_has_exact_reviewed_hydration_partition() -> None:
    strict = {
        path
        for path in _apply_command_paths()
        if DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
            path, {"apply": True}
        ).requires_existing_hydration
    }

    assert strict == {
        ("client", "drain"),
        ("close", "apply"),
        ("memory", "candidate-promote"),
        ("memory", "close-apply"),
        ("memory", "close-finalize"),
        ("memory", "obsidian-apply"),
        ("memory", "upgrade-finalize"),
        ("memory", "upgrade-stamp"),
        ("research", "start"),
        ("trace", "start"),
        ("trace", "stop"),
        ("work", "transition"),
    }
    exemptions = dict(DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.exemptions)
    assert (
        sum(value is MutationAdmissionExemption.CONTROL_PLANE for value in exemptions.values())
        == 38
    )


@pytest.mark.parametrize("path", [("project", "add"), ("work", "create")])
def test_work_graph_bootstrap_mutations_do_not_require_impossible_hydration(
    path: tuple[str, ...],
) -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(path, {"apply": True})

    assert admission.mutating
    assert admission.requires_full_continuity
    assert not admission.requires_existing_hydration
    assert admission.exemption is MutationAdmissionExemption.BOOTSTRAP


@pytest.mark.parametrize("path", [("worker", "tick"), ("worker", "run")])
def test_idle_scheduled_catch_up_has_only_narrow_recovery_ordering_exemption(
    path: tuple[str, ...],
) -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(path, {"apply": True})

    assert admission.mutating
    assert admission.requires_full_continuity
    assert not admission.requires_existing_hydration
    assert admission.exemption is MutationAdmissionExemption.RECOVERY
    assert not admission.grants_authority


def test_lifecycle_template_recovery_only_mutates_for_authorize_or_apply() -> None:
    path = ("worker", "lifecycle-template-recovery")
    registry = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY

    assert not registry.classify(path, {}).mutating
    for parameters in (
        {"authorize": True},
        {"yetkilendir": True},
        {"apply": True},
        {"uygula": True},
    ):
        admission = registry.classify(path, parameters)
        assert admission.mutating
        assert admission.exemption is MutationAdmissionExemption.RECOVERY
        assert not admission.requires_existing_hydration


def test_vendored_typer_current_context_dependency_is_exact_version_bound() -> None:
    # Typer 0.27 vendors Click, but does not publicly export get_current_context.
    # A dependency update must explicitly re-review this one private compatibility seam.
    assert version("typer") == "0.27.1"


@pytest.mark.parametrize("current", [None, SimpleNamespace(parent=None, info_name=None)])
def test_missing_or_unresolved_typer_context_is_mutation_capable_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    current: object | None,
) -> None:
    monkeypatch.setattr(cli_session, "get_current_context", lambda **_kwargs: current)

    admission = cli_session._current_cli_admission()

    assert admission.command_path == ("realm-session",)
    assert admission.mutating
    assert admission.requires_full_continuity
    assert admission.requires_existing_hydration
    assert admission.exemption is None


def test_unexempted_postgres_mutation_calls_real_continuity_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection([(PROJECT_ID, WORK_ITEM_ID, RUN_ID, "session-one", "codex")])
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("close", "apply"), {"apply": True}
    )
    calls: list[dict[str, object]] = []

    class _Service:
        def __init__(self, repository: object, authorizations: object) -> None:
            assert repository == (connection, REALM_ID, "memory")
            assert authorizations == (connection, REALM_ID, "authorization")

        def assert_mutating_admission(self, **identity: object) -> None:
            calls.append(identity)

    monkeypatch.setattr(
        cli_session,
        "MemoryContinuityRepository",
        lambda current, realm_id: (current, realm_id, "memory"),
    )
    monkeypatch.setattr(
        cli_session,
        "AuthorizationRepository",
        lambda current, realm_id: (current, realm_id, "authorization"),
    )
    monkeypatch.setattr(cli_session, "MemoryContinuityService", _Service)

    identity = cli_session._assert_existing_hydration(
        connection,
        realm_id=REALM_ID,
        admission=admission,
    )

    assert calls == [
        {
            "project_id": PROJECT_ID,
            "work_item_id": WORK_ITEM_ID,
            "run_id": RUN_ID,
            "session_id": "session-one",
            "client_id": "codex",
        }
    ]
    assert identity is not None and identity.run_id == RUN_ID
    assert "state='active'" in connection.cursor_instance.statement
    assert "deadline>clock_timestamp()" in connection.cursor_instance.statement
    assert connection.cursor_instance.parameters == (REALM_ID,)


def test_realm_session_routes_every_unexempted_mutation_through_common_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = object()
    application_context = SimpleNamespace(
        home=None,
        settings=SimpleNamespace(
            database=SimpleNamespace(backend=PersistenceBackend.POSTGRESQL),
            diagnostic_trace=None,
        ),
    )
    realm_context = RealmContext(
        realm=cast(Any, SimpleNamespace(id=REALM_ID)),
        connection=connection,
    )
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("memory", "candidate-promote"), {"apply": True}
    )
    calls: list[tuple[object, UUID, object]] = []

    @contextmanager
    def fake_connect(_settings: object) -> Iterator[object]:
        yield connection

    def fake_attach(
        current: object,
        *,
        slug: str,
        create_if_missing: bool,
    ) -> RealmContext:
        assert current is connection
        assert slug == "yerel"
        assert not create_if_missing
        return realm_context

    def fake_hydration_gate(
        current: object,
        *,
        realm_id: UUID,
        admission: object,
    ) -> None:
        calls.append((current, realm_id, admission))

    monkeypatch.setattr(cli_session, "build_context", lambda home: application_context)
    monkeypatch.setattr(cli_session, "_current_cli_admission", lambda: admission)
    monkeypatch.setattr(cli_session, "connect", fake_connect)
    monkeypatch.setattr(cli_session, "attach_realm", fake_attach)
    monkeypatch.setattr(cli_session, "_assert_existing_hydration", fake_hydration_gate)

    with cli_session.RealmSession(None, "yerel") as opened:
        assert opened is realm_context

    assert calls == [(connection, REALM_ID, admission)]


def test_target_run_hint_selects_one_of_two_active_runs_exactly() -> None:
    rows = [
        (PROJECT_ID, WORK_ITEM_ID, RUN_ID, "session-one", "codex"),
        (PROJECT_ID, WORK_ITEM_ID, RUN_ID_TWO, "session-two", "codex"),
    ]
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("trace", "start"), {"apply": True, "run_id": RUN_ID_TWO}
    )
    connection = _TargetFilteringConnection(rows, target_run_id=RUN_ID_TWO)

    identity = cli_session._active_runtime_continuity_identity(
        connection,
        realm_id=REALM_ID,
        target_hints=admission.target_hints,
    )

    assert identity.run_id == RUN_ID_TWO
    assert "run.id::text=%s" in connection.cursor_instance.statement
    assert connection.cursor_instance.parameters[-2:] == (
        str(RUN_ID_TWO),
        str(RUN_ID_TWO),
    )


def test_command_target_hints_preserve_exact_uuid_and_refs() -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("trace", "start"),
        {
            "apply": True,
            "project_id": PROJECT_ID,
            "work_item_id": WORK_ITEM_ID,
            "run_id": RUN_ID,
            "client_session": "session-one",
            "client_id": "codex",
            "authorization_id": AUTHORIZATION_ID,
            "candidate_id": str(CANDIDATE_ID),
            "trace_id": TRACE_ID,
        },
    )

    assert admission.target_hints.project_ref == str(PROJECT_ID)
    assert admission.target_hints.work_ref == str(WORK_ITEM_ID)
    assert admission.target_hints.run_ref == str(RUN_ID)
    assert admission.target_hints.session_ref == "session-one"
    assert admission.target_hints.client_ref == "codex"
    assert admission.target_hints.authorization_ref == str(AUTHORIZATION_ID)
    assert admission.target_hints.candidate_ref == str(CANDIDATE_ID)
    assert admission.target_hints.trace_ref == str(TRACE_ID)


def test_close_receipt_input_binds_exact_execution_hints(tmp_path: Path) -> None:
    input_file = tmp_path / "close-receipt.json"
    input_file.write_text(
        json.dumps(
            {
                "project_id": str(PROJECT_ID),
                "work_item_id": str(WORK_ITEM_ID),
                "run_id": str(RUN_ID),
                "session_id": "session-one",
                "client_id": "codex",
            }
        ),
        encoding="utf-8",
    )

    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("close", "apply"),
        {
            "apply": True,
            "input_file": input_file,
            "authorization_id": AUTHORIZATION_ID,
        },
    )

    assert admission.target_hints.project_ref == str(PROJECT_ID)
    assert admission.target_hints.work_ref == str(WORK_ITEM_ID)
    assert admission.target_hints.run_ref == str(RUN_ID)
    assert admission.target_hints.session_ref == "session-one"
    assert admission.target_hints.client_ref == "codex"
    assert admission.target_hints.authorization_ref == str(AUTHORIZATION_ID)


@pytest.mark.parametrize(
    ("path", "parameters", "statement_fragment", "target_ref"),
    [
        (
            ("close", "apply"),
            {"apply": True, "authorization_id": AUTHORIZATION_ID},
            "authz.work_item_id=run.work_item_id",
            str(AUTHORIZATION_ID),
        ),
        (
            ("memory", "candidate-promote"),
            {"apply": True, "candidate_id": str(CANDIDATE_ID)},
            "compiler_run.run_id=run.id",
            str(CANDIDATE_ID),
        ),
        (
            ("trace", "reduce"),
            {"trace_id": TRACE_ID},
            "trace.work_item_id=run.work_item_id and trace.run_id=run.id",
            str(TRACE_ID),
        ),
    ],
)
def test_indirect_target_hints_add_exact_runtime_scope_predicates(
    path: tuple[str, ...],
    parameters: dict[str, object],
    statement_fragment: str,
    target_ref: str,
) -> None:
    connection = _Connection([(PROJECT_ID, WORK_ITEM_ID, RUN_ID, "session-one", "codex")])
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(path, parameters)

    identity = cli_session._active_runtime_continuity_identity(
        connection,
        realm_id=REALM_ID,
        target_hints=admission.target_hints,
    )

    assert identity.run_id == RUN_ID
    assert statement_fragment in connection.cursor_instance.statement
    assert target_ref in connection.cursor_instance.parameters
    if path == ("memory", "candidate-promote"):
        assert "memory.compiler_candidate" in connection.cursor_instance.statement
        assert "memory.candidate " not in connection.cursor_instance.statement
        assert "candidate.is_current=true" in connection.cursor_instance.statement


@pytest.mark.parametrize(
    ("path", "parameters"),
    [
        (
            ("close", "apply"),
            {"apply": True, "authorization_id": AUTHORIZATION_ID},
        ),
        (
            ("memory", "candidate-promote"),
            {"apply": True, "candidate_id": str(CANDIDATE_ID)},
        ),
        (("trace", "reduce"), {"trace_id": TRACE_ID}),
    ],
)
def test_unresolved_indirect_target_hint_fails_closed(
    path: tuple[str, ...],
    parameters: dict[str, object],
) -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(path, parameters)

    with pytest.raises(PolicyViolation, match="exact bir aktif runtime execution"):
        cli_session._active_runtime_continuity_identity(
            _Connection([]),
            realm_id=REALM_ID,
            target_hints=admission.target_hints,
        )


def test_unrelated_single_active_run_cannot_satisfy_exact_target_hint() -> None:
    rows = [(PROJECT_ID, WORK_ITEM_ID, RUN_ID, "session-one", "codex")]
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("trace", "start"), {"apply": True, "run_id": RUN_ID_TWO}
    )
    connection = _TargetFilteringConnection(rows, target_run_id=RUN_ID_TWO)

    with pytest.raises(PolicyViolation, match="exact bir aktif runtime execution"):
        cli_session._active_runtime_continuity_identity(
            connection,
            realm_id=REALM_ID,
            target_hints=admission.target_hints,
        )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            (PROJECT_ID, WORK_ITEM_ID, RUN_ID, "session-one", "codex"),
            (
                PROJECT_ID,
                WORK_ITEM_ID,
                UUID("00000000-0000-0000-0000-000000000005"),
                "session-two",
                "codex",
            ),
        ],
    ],
)
def test_unexempted_mutation_fails_closed_without_one_active_identity(
    rows: list[tuple[object, ...]],
) -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("memory", "candidate-promote"), {"apply": True}
    )

    with pytest.raises(PolicyViolation, match="exact bir aktif runtime execution"):
        cli_session._assert_existing_hydration(
            _Connection(rows),
            realm_id=REALM_ID,
            admission=admission,
        )


def test_hydration_exemption_skips_only_existing_hydration_lookup() -> None:
    admission = DEFAULT_CLI_MUTATION_ADMISSION_REGISTRY.classify(
        ("memory", "hydration-apply"), {"apply": True}
    )
    connection = _Connection([])

    cli_session._assert_existing_hydration(
        connection,
        realm_id=REALM_ID,
        admission=admission,
    )

    assert connection.cursor_instance.statement == ""
