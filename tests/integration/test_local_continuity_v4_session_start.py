from __future__ import annotations

import datetime as dt
import inspect
import json
import socket
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zekam.application.active_task_contract import ActiveTaskContract
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.config import core_root, load_settings
from zekam.application.context_compiler import compile_context_v2
from zekam.application.context_ranking import ContextRankingRequest, count_context_tokens
from zekam.application.home import HomeLayout
from zekam.application.local_continuity import ContinuityBinding, LocalContext
from zekam.application.local_continuity_source_plan import ContinuitySourceRecipe
from zekam.application.local_continuity_v4_ingress import (
    FrozenCurrentStartupContext,
    ManagedInvocationSnapshot,
    ManagedProcessSnapshot,
    _validate_current_context_inputs,
)
from zekam.application.local_continuity_v4_writer import (
    CurrentSourceSnapshot,
    internal_receipt_digest,
)
from zekam.application.local_hook_command_contract import (
    NATIVE_DOUBLE_EXEC_TOPOLOGY,
    ReviewedHookCommand,
)
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
)
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.infrastructure import local_continuity_v4_composition as composition_module
from zekam.infrastructure.clients.codex_macos_0151_lifecycle import (
    CodexMacOS0151Event,
    LiveProcessVerificationError,
    _trusted_process_owner,
    parse_codex_macos_0151,
)
from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource
from zekam.infrastructure.local_continuity_v4_composition import (
    DormantCodex0151V4Arguments,
    DormantCodex0151V4Runtime,
    _CurrentV4SessionStartContext,
)
from zekam.infrastructure.sqlite import local_continuity_v4_ingress as ingress_module
from zekam.infrastructure.sqlite import local_runtime as runtime_module
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite import operational_store as operational_store_module
from zekam.infrastructure.sqlite.local_continuity_v4_ingress import (
    SQLiteCodexV4Ingress,
    _attachment_uuid,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

NOW = "2026-09-03T12:00:00+00:00"
ROOT = Path("/Users/mkaracan/Projeler/akilli-kasa")
SOURCE_REF = "src/akilli_kasa/api/saglik.py"
SESSION = "018f0000-0000-7000-8000-000000000101"
PROJECT = "018f0000-0000-7000-8000-000000000102"
REALM = "018f0000-0000-7000-8000-000000000103"
SNAPSHOT = "018f0000-0000-7000-8000-000000000104"
_PRODUCTION_MANAGER_OWNER_CHECK = _trusted_process_owner
_PRODUCTION_CONTEXT_OWNER_CHECK = composition_module._trusted_context_owner


def _issue_test_snapshot[TestSnapshot: (ManagedProcessSnapshot, ManagedInvocationSnapshot)](
    snapshot_type: type[TestSnapshot], **values: object
) -> TestSnapshot:
    """Explicit test-only receipt issuance; production exposes no equivalent minter."""

    snapshot = object.__new__(snapshot_type)
    for name in snapshot_type.__dataclass_fields__:
        object.__setattr__(snapshot, name, values[name])
    snapshot.__post_init__()
    return snapshot


def _replace_test_snapshot[TestSnapshot: (ManagedProcessSnapshot, ManagedInvocationSnapshot)](
    snapshot: TestSnapshot, **changes: object
) -> TestSnapshot:
    values = {
        name: changes.get(name, getattr(snapshot, name))
        for name in type(snapshot).__dataclass_fields__
    }
    return _issue_test_snapshot(type(snapshot), **values)


def _issue_test_context(
    *,
    binding: ContinuityBinding,
    context: LocalContext,
    source_snapshot: CurrentSourceSnapshot,
    environment_evidence_digest: str,
    hydration_key: str,
    observed_at: str,
) -> FrozenCurrentStartupContext:
    manifest, hydration, additional, stdout = _validate_current_context_inputs(
        binding=binding,
        context=context,
        source_snapshot=source_snapshot,
        environment_evidence_digest=environment_evidence_digest,
        hydration_key=hydration_key,
        observed_at=observed_at,
    )
    values: dict[str, object] = {
        "binding": binding,
        "binding_digest": binding.binding_digest,
        "source_snapshot": source_snapshot,
        "environment_evidence_digest": environment_evidence_digest,
        "context": context,
        "manifest_body_json": canonical_json(manifest),
        "manifest_digest": digest(manifest),
        "hydration_key": hydration_key,
        "hydration_body_json": canonical_json(hydration),
        "hydration_receipt_digest": digest(hydration),
        "observed_at": observed_at,
        "additional_context": additional,
        "output_digest": digest(additional),
        "success_stdout": stdout,
    }
    result = object.__new__(FrozenCurrentStartupContext)
    for name in FrozenCurrentStartupContext.__dataclass_fields__:
        object.__setattr__(result, name, values[name])
    result.__post_init__()
    return result


def _binding() -> ContinuityBinding:
    return ContinuityBinding(
        SESSION,
        "codex-external-session",
        PROJECT,
        REALM,
        "codex",
        "macbook",
        SNAPSHOT,
        digest("task"),
        digest("plan"),
        digest("policy"),
    )


def _seed(path: Path) -> ContinuityBinding:
    operational_schema.bootstrap_v4(path)
    binding = _binding()
    source = (ROOT / SOURCE_REF).read_text()
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into project(id,slug,display_name,created_at) values(?,?,?,?)",
            (PROJECT, "akilli-kasa", "Akilli Kasa", NOW),
        )
        db.execute("insert into project_knowledge_realm values(?,?,?)", (PROJECT, REALM, NOW))
        db.execute(
            "insert into source_binding values(?,?,?,?,?,?)",
            ("source", PROJECT, "source:akilli-kasa", "directory", 1, NOW),
        )
        db.execute(
            "insert into source_snapshot values(?,?,?,?,?,?,?)",
            (
                SNAPSHOT,
                "source",
                "a" * 40,
                digest("tree"),
                digest(source),
                digest("config"),
                NOW,
            ),
        )
        db.execute(
            "insert into session(id,client_id,device_id,project_id,status,opened_at) "
            "values(?,?,?,?,?,?)",
            (SESSION, "codex", "macbook", PROJECT, "open", NOW),
        )
        db.execute("insert into local_runtime_config values(1,64)")
        db.execute(
            "insert into continuity_session_binding values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                SESSION,
                binding.external_session_id,
                PROJECT,
                REALM,
                None,
                None,
                "codex",
                "macbook",
                SNAPSHOT,
                binding.task_digest,
                binding.plan_digest,
                binding.policy_digest,
                binding.binding_digest,
                NOW,
            ),
        )
    return binding


def _context(binding: ContinuityBinding) -> LocalContext:
    text = (ROOT / SOURCE_REF).read_text()
    candidate = ContextCandidate(
        candidate_id="source-health",
        authority=AuthorityLevel.VERIFIED,
        observed_at=dt.datetime.fromisoformat(NOW),
        source_revision="a" * 40,
        content_digest=digest(text),
        token_count=count_context_tokens(text),
        required=True,
        kind=ContextCandidateKind.SOURCE_SLICE,
        source_ref=SOURCE_REF,
        scope_ref=f"project/{PROJECT}",
        identity_refs=("task/wp08",),
        applicable_roles=("builder",),
        canonical_revision_id=SNAPSHOT,
    )
    ranking = ContextRankingRequest(
        role="builder",
        target_identity_refs=("task/wp08",),
        step_scope_ref=None,
        work_scope_ref=None,
        project_scope_ref=f"project/{PROJECT}",
        realm_scope_ref=f"realm/{REALM}",
        current_source_revision="a" * 40,
        compatible_source_revisions=(),
        task_terms=(),
        tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
    )
    compiled = compile_context_v2(
        (candidate,),
        ranking_request=ranking,
        token_budget=2048,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=dt.datetime.fromisoformat(NOW),
        contents={candidate.candidate_id: text},
        ranking_snapshot_digest=digest(ranking.body()),
        candidate_set_digest=digest(candidate.candidate_digest),
        recipe_id="codex0151-session-start",
        recipe_digest=digest("codex0151-session-start"),
        target_role="builder",
    )
    return LocalContext(compiled, ((candidate.candidate_id, text),), ranking, (candidate,))


class _ContextPort:
    def __init__(self, binding: ContinuityBinding) -> None:
        self.binding = binding
        self.assertions = 0
        self.fail_after: int | None = None
        self.environment_label = "environment"
        self.expected_environment_evidence_digest = digest(self.environment_label)

    def build(
        self,
        binding: ContinuityBinding,
        *,
        hydration_key: str,
        observed_at: str,
    ) -> FrozenCurrentStartupContext:
        assert binding == self.binding
        return _issue_test_context(
            binding=binding,
            context=_context(binding),
            source_snapshot=CurrentSourceSnapshot(SNAPSHOT, "a" * 40, digest("snapshot")),
            environment_evidence_digest=digest(self.environment_label),
            hydration_key=hydration_key,
            observed_at=observed_at,
        )

    def assert_current(
        self, binding: ContinuityBinding, snapshot: FrozenCurrentStartupContext
    ) -> None:
        self.assertions += 1
        if self.fail_after is not None and self.assertions >= self.fail_after:
            raise PolicyViolation("injected current source drift")
        assert binding == self.binding
        if (
            snapshot.environment_evidence_digest != digest(self.environment_label)
            or snapshot.environment_evidence_digest != self.expected_environment_evidence_digest
        ):
            raise PolicyViolation("injected current environment drift")
        assert (ROOT / SOURCE_REF).read_text() == dict(snapshot.context.fragments)["source-health"]


class _Manager:
    def __init__(self, binding: ContinuityBinding) -> None:
        attachment_id = _attachment_uuid(binding)
        self.commands = tuple(
            ReviewedHookCommand(
                attachment_id=attachment_id,
                external_event_type=event,
                topology=NATIVE_DOUBLE_EXEC_TOPOLOGY,
                client_contract_digest=digest("client-contract"),
                hook_set_digest=digest("hook-set"),
                shell_artifact_digest=digest("shell"),
                python_launcher_artifact_digest=digest("launcher"),
                python_runtime_artifact_digest=digest("runtime"),
                argv_recipe_digest=digest(f"argv:{event}"),
                sandbox_profile_digest=digest("sandbox"),
                created_at=NOW,
            )
            for event in ("SessionStart", "PreCompact", "PostCompact")
        )
        self.process = _issue_test_snapshot(
            ManagedProcessSnapshot,
            attachment_id=attachment_id,
            captured_at=NOW,
            native_pid=101,
            native_uid=501,
            native_start_token="native-start",
            native_artifact_digest=digest("native"),
            client_contract_digest=digest("client-contract"),
            hook_set_digest=digest("hook-set"),
            ancestry_policy_digest=digest("ancestry-policy"),
            reviewed_commands=self.commands,
        )
        managed = {
            "ancestry_policy_digest": self.process.ancestry_policy_digest,
            "attachment_id": attachment_id,
            "created_at": NOW,
            "hook_set_digest": self.process.hook_set_digest,
            "native_artifact_digest": self.process.native_artifact_digest,
            "native_pid": 101,
            "native_start_token": "native-start",
            "native_uid": 501,
            "predecessor_process_generation_digest": None,
            "transition_kind": "initial-attach",
        }
        generation = {
            "ancestry_policy_digest": self.process.ancestry_policy_digest,
            "attachment_id": attachment_id,
            "created_at": NOW,
            "generation": 1,
            "hook_set_digest": self.process.hook_set_digest,
            "managed_launch_receipt_digest": digest(managed),
            "native_artifact_digest": self.process.native_artifact_digest,
            "native_pid": 101,
            "native_start_token": "native-start",
            "native_uid": 501,
            "previous_process_generation_digest": None,
        }
        self.generation_digest = digest(generation)
        self.fail_invocation = False
        self.hook_pid = 202
        self.hook_start = "hook-start"

    def capture_process(self, binding: ContinuityBinding) -> ManagedProcessSnapshot:
        return self.process

    def assert_process(self, snapshot: ManagedProcessSnapshot) -> None:
        assert snapshot == self.process

    def capture_invocation(
        self,
        binding: ContinuityBinding,
        observation: dict[str, Any],
        spool_digest: str,
        observed_at: str,
        expected_process_generation_digest: str,
        expected_generation_created_at: str,
        expected_managed_receipt_digest: str,
        expected_launch_command: ReviewedHookCommand,
        expected_ancestry_policy_digest: str,
    ) -> ManagedInvocationSnapshot:
        delivery_id = digest(
            {
                "schema": "zekam-codex-0151-delivery/v1",
                "session_id": binding.external_session_id,
                "external_event_type": "SessionStart",
                "wire_digest": observation["wire_digest"],
            }
        )
        del expected_generation_created_at, expected_managed_receipt_digest
        return _issue_test_snapshot(
            ManagedInvocationSnapshot,
            delivery_id=delivery_id,
            observed_at=observed_at,
            process_generation_digest=expected_process_generation_digest,
            ancestry_policy_digest=expected_ancestry_policy_digest,
            native_pid=101,
            native_uid=501,
            native_start_token="native-start",
            native_artifact_digest=digest("native"),
            hook_pid=self.hook_pid,
            hook_uid=501,
            hook_start_token=self.hook_start,
            shell_artifact_digest=digest("shell"),
            python_launcher_artifact_digest=digest("launcher"),
            python_runtime_artifact_digest=digest("runtime"),
            launch_command_digest=expected_launch_command.command_digest,
            observation_digest=digest(observation),
            spool_digest=spool_digest,
        )

    def assert_invocation(self, snapshot: ManagedInvocationSnapshot) -> None:
        snapshot.__post_init__()
        if self.fail_invocation:
            raise LiveProcessVerificationError(("hook-parent",))

    @staticmethod
    def recovery_time() -> str:
        return "2026-09-03T12:00:01+00:00"


@pytest.fixture(autouse=True)
def _test_only_sealed_owner_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ingress_module,
        "_trusted_process_owner",
        lambda value: _PRODUCTION_MANAGER_OWNER_CHECK(value) or type(value) is _Manager,
    )
    monkeypatch.setattr(
        composition_module,
        "_trusted_context_owner",
        lambda value: _PRODUCTION_CONTEXT_OWNER_CHECK(value) or type(value) is _ContextPort,
    )


def _event() -> CodexMacOS0151Event:
    payload = canonical_json(
        {
            "session_id": "codex-external-session",
            "transcript_path": None,
            "cwd": str(ROOT),
            "hook_event_name": "SessionStart",
            "source": "startup",
            "model": "gpt-5.6",
            "permission_mode": "default",
        }
    ).encode()
    return parse_codex_macos_0151(payload, expected_root=ROOT)


def _prepared(
    tmp_path: Path,
) -> tuple[
    Path,
    ContinuityBinding,
    _Manager,
    _ContextPort,
    ClientLifecycleSpool,
    CodexMacOS0151Event,
    SQLiteCodexV4Ingress,
]:
    path = tmp_path / "operational.db"
    binding = _seed(path)
    manager = _Manager(binding)
    context = _ContextPort(binding)
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    event = _event()
    ingress = SQLiteCodexV4Ingress(
        path,
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    )
    return path, binding, manager, context, spool, event, ingress


def test_v4_session_start_is_atomic_replayable_and_reads_real_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = ROOT / SOURCE_REF
    source_before = source_path.read_bytes()
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("SessionStart must not use network"),
    )
    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("SessionStart must not open a network socket"),
    )
    path, _binding_value, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    first = ingress.session_start(event)
    second = ingress.session_start(event)
    assert not first.replay
    assert second.replay
    assert second.stdout == first.stdout
    assert first.manifest_digest == second.manifest_digest
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 1
        assert db.execute("select count(*) from session_event").fetchone()[0] == 1
        assert db.execute("select count(*) from continuity_native_event_receipt").fetchone()[0] == 1
    assert source_path.read_bytes() == source_before


def test_production_ingress_has_no_test_attempt_or_digest_relabel_api(tmp_path: Path) -> None:
    assert (
        "test_current_attempt_entry_digest"
        not in inspect.signature(SQLiteCodexV4Ingress).parameters
    )
    assert not hasattr(SQLiteCodexV4Ingress, "_session_start_current_attempt")
    path, _binding_value, _manager, _context, _spool, _event_value, _ingress = _prepared(tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from session_event").fetchone()[0] == 0


def test_production_ingress_rejects_unsealed_duck_owners_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "operational.db"
    binding = _seed(path)
    monkeypatch.setattr(ingress_module, "_trusted_process_owner", _PRODUCTION_MANAGER_OWNER_CHECK)
    monkeypatch.setattr(
        composition_module, "_trusted_context_owner", _PRODUCTION_CONTEXT_OWNER_CHECK
    )
    with pytest.raises(ValidationFailed, match="sealed concrete"):
        SQLiteCodexV4Ingress(
            path,
            binding,
            process_manager=_Manager(binding),
            context_port=_ContextPort(binding),
            spool=ClientLifecycleSpool(tmp_path / "home", client_id="codex"),
        )
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_hook_attachment").fetchone()[0] == 0


def test_precommit_source_drift_rolls_back_all_session_start_rows(tmp_path: Path) -> None:
    path, _binding_value, _manager, context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    context.fail_after = 2
    with pytest.raises(PolicyViolation, match="source drift"):
        ingress.session_start(event)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 0
        assert db.execute("select count(*) from session_event").fetchone()[0] == 0
        assert db.execute("select count(*) from continuity_native_event_receipt").fetchone()[0] == 0
        assert (
            db.execute(
                "select max(revision_number) from continuity_hook_attachment_revision"
            ).fetchone()[0]
            == 1
        )


def test_live_process_drift_creates_bounded_recovery_without_session_start(
    tmp_path: Path,
) -> None:
    path, _binding_value, manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    manager.fail_invocation = True
    result = ingress.session_start(event)
    assert result.recovery_required
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 0
        assert db.execute("select count(*) from session_event").fetchone()[0] == 0
        case = db.execute(
            "select case_kind,body_json from continuity_hook_recovery_case"
        ).fetchone()
        assert case[0] == "process-drift"
        assert "hook-parent" not in case[1]


def test_session_start_atomically_creates_the_single_spool_event(tmp_path: Path) -> None:
    path = tmp_path / "operational.db"
    binding = _seed(path)
    manager = _Manager(binding)
    ingress = SQLiteCodexV4Ingress(
        path,
        binding,
        process_manager=manager,
        context_port=_ContextPort(binding),
        spool=ClientLifecycleSpool(tmp_path / "empty-home", client_id="codex"),
    )
    ingress.attach_process()
    result = ingress.session_start(_event())
    assert result.manifest_digest is not None
    with ingress.spool.frozen_session_entries(
        client_id=binding.client_id, session_id=binding.external_session_id
    ) as entries:
        assert len(entries) == 1


def test_foreign_external_session_rejected_before_database_write(tmp_path: Path) -> None:
    path, _binding_value, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    forged = replace(event, external_session_id="foreign-session")
    with pytest.raises(PolicyViolation, match="binding mismatch"):
        ingress.session_start(forged)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from session_event").fetchone()[0] == 0


def test_restart_reconstructs_without_new_rows(tmp_path: Path) -> None:
    path, binding, manager, _context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    first = ingress.session_start(event)
    reopened = SQLiteCodexV4Ingress(
        path,
        binding,
        process_manager=manager,
        context_port=_ContextPort(binding),
        spool=spool,
    )
    second = reopened.session_start(event)
    assert second.replay and second.stdout == first.stdout
    with sqlite3.connect(path) as db:
        assert (
            db.execute("select count(*) from continuity_hook_attachment_revision").fetchone()[0]
            == 2
        )


def test_new_live_hook_tuple_replays_historical_native_graph_without_relabelling(
    tmp_path: Path,
) -> None:
    path, binding, manager, _context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    first = ingress.session_start(event)
    manager.hook_pid = 303
    manager.hook_start = "new-hook-start"
    replay = SQLiteCodexV4Ingress(
        path,
        binding,
        process_manager=manager,
        context_port=_ContextPort(binding),
        spool=spool,
    ).session_start(event)
    assert replay.replay and replay.stdout == first.stdout
    with sqlite3.connect(path) as db:
        historical = db.execute(
            "select hook_pid,hook_start_token from continuity_native_event_receipt"
        ).fetchone()
        assert historical == (202, "hook-start")


def test_new_native_tuple_cannot_replay_old_process_generation(tmp_path: Path) -> None:
    path, binding, manager, _context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    original = manager.capture_invocation

    def changed_native(*args: Any, **kwargs: Any) -> ManagedInvocationSnapshot:
        return _replace_test_snapshot(
            original(*args, **kwargs),
            native_pid=909,
            native_start_token="new-native-start",
        )

    manager.capture_invocation = changed_native  # type: ignore[method-assign]
    with pytest.raises(PolicyViolation, match="generation tuple"):
        SQLiteCodexV4Ingress(
            path,
            binding,
            process_manager=manager,
            context_port=_ContextPort(binding),
            spool=spool,
        ).session_start(event)


def test_production_context_preserves_compiler_selected_order() -> None:
    binding = replace(
        _binding(),
        work_item_id="018f0000-0000-7000-8000-000000000120",
        run_id="018f0000-0000-7000-8000-000000000121",
    )
    target = object.__new__(_CurrentV4SessionStartContext)
    target.binding = binding
    object.__setattr__(target, "source", SimpleNamespace(_read=lambda *_args: None))
    config_id = "018f0000-0000-7000-8000-000000000122"
    work_revision_id = "018f0000-0000-7000-8000-000000000123"
    runtime_body: dict[str, object] = {
        "network_default": "deny",
        "permission_profile": "workspace-write-no-network",
    }
    rows = {
        "config": {"id": config_id, "config_digest": binding.policy_digest},
        "config_body": {"runtime": runtime_body},
        "work": {
            "id": work_revision_id,
            "revision": 1,
            "state": "active",
            "title": "work",
            "kind": "implementation",
            "payload_digest": digest({}),
            "evidence_digest": None,
        },
        "work_payload": {},
        "run": {
            "id": binding.run_id,
            "work_item_id": binding.work_item_id,
            "source_snapshot_id": binding.source_snapshot_id,
            "config_revision_id": config_id,
            "status": "active",
            "plan_digest": binding.plan_digest,
            "terminal_receipt_digest": None,
            "updated_at": NOW,
        },
        "plan": SimpleNamespace(files=(), revision_ref="a" * 40),
    }
    policy_body = {
        "config_revision_id": config_id,
        "task_digest": binding.task_digest,
        "config_digest": binding.policy_digest,
        "realm_id": binding.realm_id,
        "runtime": runtime_body,
        "continuity_grants_authority": False,
    }
    work_body: dict[str, object] = {
        "work_item_id": binding.work_item_id,
        "project_id": binding.project_id,
        "revision_id": work_revision_id,
        "revision": 1,
        "state": "active",
        "title": "work",
        "kind": "implementation",
        "payload": {},
        "payload_digest": digest({}),
        "evidence_digest": None,
        "continuity_grants_authority": False,
    }
    run_body: dict[str, object] = {
        "id": binding.run_id,
        "work_item_id": binding.work_item_id,
        "source_snapshot_id": binding.source_snapshot_id,
        "config_revision_id": config_id,
        "status": "active",
        "plan_digest": binding.plan_digest,
        "terminal_receipt_digest": None,
        "updated_at": NOW,
        "continuity_grants_authority": False,
    }
    rows["accepted_startup"] = {
        "system-policy": (
            canonical_json(policy_body),
            binding.policy_digest,
            f"config/{config_id}",
            f"realm/{binding.realm_id}",
            config_id,
        ),
        "work-contract": (
            canonical_json(work_body),
            f"work-revision/{work_revision_id}",
            f"work/{binding.work_item_id}",
            f"work/{binding.work_item_id}",
            work_revision_id,
        ),
        "run-status": (
            canonical_json(run_body),
            digest(run_body),
            f"run/{binding.run_id}",
            f"work/{binding.work_item_id}",
            binding.run_id,
        ),
    }
    context = target._context(NOW, rows)
    assert tuple(identifier for identifier, _text_value in context.fragments) == tuple(
        item.candidate_id for item in context.manifest.selected
    )


def test_environment_evidence_drift_blocks_replay(tmp_path: Path) -> None:
    path, _binding_value, _manager, context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    context.environment_label = "changed-environment"
    with pytest.raises(PolicyViolation):
        ingress.session_start(event)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 1


def test_failure_after_context_insert_rolls_back_entire_transaction(tmp_path: Path) -> None:
    class FailAfterContext(SQLiteCodexV4Ingress):
        @staticmethod
        def _insert_ancestry(db: sqlite3.Connection, invocation: ManagedInvocationSnapshot) -> str:
            raise RuntimeError("injected unexpected crash after context rows")

    path, binding, manager, context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    crashing = FailAfterContext(
        path,
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    )
    with pytest.raises(RuntimeError, match="unexpected crash"):
        crashing.session_start(event)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 0
        assert db.execute("select count(*) from hydration_receipt").fetchone()[0] == 0
        assert db.execute("select count(*) from session_event").fetchone()[0] == 0


def test_extra_reordered_spool_event_blocks_session_start(tmp_path: Path) -> None:
    _path, binding, _manager, _context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    compaction_payload = canonical_json(
        {
            "session_id": binding.external_session_id,
            "transcript_path": None,
            "cwd": str(ROOT),
            "hook_event_name": "PreCompact",
            "turn_id": "turn-1",
            "trigger": "manual",
            "model": "gpt-5.6",
        }
    ).encode()
    compact = parse_codex_macos_0151(compaction_payload, expected_root=ROOT)
    spool.stage(
        compact.observation_body(),
        delivery_id=digest("second-delivery"),
        occurred_at=dt.datetime.fromisoformat(NOW),
    )
    with pytest.raises(PolicyViolation, match="single SessionStart"):
        ingress.session_start(event)


def test_replay_with_conflicting_wire_is_rejected_without_new_rows(tmp_path: Path) -> None:
    path, _binding_value, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    changed = parse_codex_macos_0151(
        canonical_json(
            {
                "session_id": "codex-external-session",
                "transcript_path": None,
                "cwd": str(ROOT),
                "hook_event_name": "SessionStart",
                "source": "startup",
                "model": "gpt-5.6",
                "permission_mode": "plan",
            }
        ).encode(),
        expected_root=ROOT,
    )
    with pytest.raises(PolicyViolation):
        ingress.session_start(changed)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from session_event").fetchone()[0] == 1


def test_commit_exception_with_no_effect_writes_one_exact_recovery_revision(
    tmp_path: Path,
) -> None:
    class BeforeCommit(SQLiteCodexV4Ingress):
        commits = 0

        @staticmethod
        def _commit(db: sqlite3.Connection) -> None:
            BeforeCommit.commits += 1
            if BeforeCommit.commits == 1:
                raise TimeoutError("injected same-process commit uncertainty")
            db.commit()
            raise TimeoutError("injected recovery commit acknowledgement loss")

    path, binding, manager, context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    uncertain = BeforeCommit(
        path,
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    )
    result = uncertain.session_start(event)
    assert result.recovery_required
    assert result.stdout == (
        b'{"continue":false,"stopReason":"ZEKAM_SESSION_START_RECOVERY_REQUIRED"}\n'
    )
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 0
        assert db.execute("select count(*) from session_event").fetchone()[0] == 0
        assert db.execute("select count(*) from continuity_hook_recovery_case").fetchone()[0] == 1
        state = db.execute(
            "select state from continuity_hook_attachment_revision "
            "order by revision_number desc limit 1"
        ).fetchone()[0]
        assert state == "recovery-required"
    replay = ingress.session_start(event)
    assert replay.recovery_required and replay.replay
    with sqlite3.connect(path) as db:
        triggers = db.execute(
            "select name,sql from sqlite_master where type='trigger' "
            "and tbl_name='continuity_hook_recovery_case' order by name"
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f'drop trigger "{name}"')
        db.execute("update continuity_hook_recovery_case set body_json='{}'")
        for _name, sql in triggers:
            db.execute(sql)
    with pytest.raises(PolicyViolation):
        ingress.session_start(event)


def test_recovery_commit_with_no_durable_effect_stays_ambiguous_without_duplicate(
    tmp_path: Path,
) -> None:
    class NeverCommit(SQLiteCodexV4Ingress):
        @staticmethod
        def _commit(db: sqlite3.Connection) -> None:
            raise TimeoutError("injected precommit failure")

    path, binding, manager, context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    uncertain = NeverCommit(
        path,
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    )
    result = uncertain.session_start(event)
    assert result.recovery_required and not result.replay
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_hook_recovery_case").fetchone()[0] == 0
        assert (
            db.execute("select count(*) from continuity_hook_attachment_revision").fetchone()[0]
            == 1
        )


def test_preexisting_staged_start_without_current_attempt_receipt_is_attention_only(
    tmp_path: Path,
) -> None:
    path, binding, manager, context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    spool.stage(
        event.observation_body(),
        delivery_id=digest(
            {
                "schema": "zekam-codex-0151-delivery/v1",
                "session_id": binding.external_session_id,
                "external_event_type": "SessionStart",
                "wire_digest": event.wire_digest,
            }
        ),
        occurred_at=dt.datetime.fromisoformat(NOW),
    )
    later = SQLiteCodexV4Ingress(
        path,
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    )
    with pytest.raises(PolicyViolation, match="ambiguous-unacknowledged"):
        later.session_start(event)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from session_event").fetchone()[0] == 0
        assert db.execute("select count(*) from continuity_hook_recovery_case").fetchone()[0] == 0


def test_commit_unknown_conflicting_hydration_key_never_mints_recovery(
    tmp_path: Path,
) -> None:
    class ConflictingHydration(SQLiteCodexV4Ingress):
        @staticmethod
        def _commit(db: sqlite3.Connection) -> None:
            hydration = db.execute("select * from hydration_receipt").fetchone()
            assert hydration is not None
            db.rollback()
            wrong = {"schema": "conflicting-manifest/v1"}
            wrong_manifest = digest(wrong)
            db.execute("begin immediate")
            db.execute(
                "insert into context_manifest values(?,?,?,?,?,?,?)",
                (
                    wrong_manifest,
                    hydration["session_id"],
                    None,
                    1,
                    0,
                    canonical_json(wrong),
                    hydration["created_at"],
                ),
            )
            db.execute(
                "insert into hydration_receipt values(?,?,?,?,?)",
                (
                    digest("conflicting-hydration"),
                    hydration["session_id"],
                    wrong_manifest,
                    hydration["idempotency_key"],
                    hydration["created_at"],
                ),
            )
            db.commit()
            raise TimeoutError("injected conflicting commit outcome")

    path, binding, manager, context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    result = ConflictingHydration(
        path,
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    ).session_start(event)
    assert result.recovery_required and not result.replay
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_hook_recovery_case").fetchone()[0] == 0


def test_recovery_replay_rebuilds_case_body_and_evidence(tmp_path: Path) -> None:
    path, _binding_value, manager, _context_value, _spool_value, event, ingress = _prepared(
        tmp_path
    )
    ingress.attach_process()
    manager.fail_invocation = True
    assert ingress.session_start(event).recovery_required
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        triggers = db.execute(
            "select name,sql from sqlite_master where type='trigger' "
            "and tbl_name='continuity_hook_recovery_case' order by name"
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f'drop trigger "{name}"')
        row = db.execute("select * from continuity_hook_recovery_case").fetchone()
        assert row is not None
        body = json.loads(row["body_json"])
        body["evidence_digest"] = digest("self-consistent-but-wrong")
        db.execute(
            "update continuity_hook_recovery_case set evidence_digest=?,body_json=?",
            (body["evidence_digest"], canonical_json(body)),
        )
        for _name, sql in triggers:
            db.execute(sql)
    with pytest.raises(PolicyViolation, match="parity"):
        ingress.session_start(event)


def test_recovery_replay_rejects_unexpected_additional_current_head(tmp_path: Path) -> None:
    path, _binding_value, manager, _context_value, _spool_value, event, ingress = _prepared(
        tmp_path
    )
    ingress.attach_process()
    manager.fail_invocation = True
    assert ingress.session_start(event).recovery_required
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        triggers = db.execute(
            "select name,sql from sqlite_master where type='trigger' "
            "and tbl_name='continuity_hook_attachment_revision' order by name"
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f'drop trigger "{name}"')
        current = db.execute(
            "select * from continuity_hook_attachment_revision "
            "order by revision_number desc limit 1"
        ).fetchone()
        assert current is not None
        columns = tuple(current.keys())
        body = {key: current[key] for key in columns if key not in {"revision_digest", "body_json"}}
        body["revision_number"] += 1
        body["previous_revision_digest"] = current["revision_digest"]
        body["operation_key"] = "unexpected-recovery-followup"
        body["created_at"] = "2026-09-03T12:00:30+00:00"
        revision_value = digest(body)
        db.execute(
            "insert into continuity_hook_attachment_revision values("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(
                revision_value
                if key == "revision_digest"
                else canonical_json({"revision_digest": revision_value, **body})
                if key == "body_json"
                else body[key]
                for key in columns
            ),
        )
        for _name, sql in triggers:
            db.execute(sql)
    with pytest.raises(PolicyViolation):
        ingress.session_start(event)


def test_commit_exception_after_commit_reconstructs_success_without_recovery(
    tmp_path: Path,
) -> None:
    class AfterCommit(SQLiteCodexV4Ingress):
        @staticmethod
        def _commit(db: sqlite3.Connection) -> None:
            db.commit()
            raise TimeoutError("injected exception after durable commit")

    path, binding, manager, context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    uncertain = AfterCommit(
        path,
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    )
    result = uncertain.session_start(event)
    assert result.replay and not result.recovery_required
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 1
        assert db.execute("select count(*) from continuity_hook_recovery_case").fetchone()[0] == 0


def test_partial_ancestry_commit_is_attention_not_no_effect_recovery(tmp_path: Path) -> None:
    class PartialAncestry(SQLiteCodexV4Ingress):
        @staticmethod
        def _commit(db: sqlite3.Connection) -> None:
            row = db.execute("select * from continuity_hook_invocation_ancestry_receipt").fetchone()
            assert row is not None
            columns = tuple(row.keys())
            db.rollback()
            db.execute("begin immediate")
            db.execute(
                "insert into continuity_hook_invocation_ancestry_receipt("
                + ",".join(columns)
                + ") values("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(row[column] for column in columns),
            )
            db.commit()
            raise TimeoutError("injected partial durable effect")

    path, binding, manager, context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    result = PartialAncestry(
        path,
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    ).session_start(event)
    assert result.recovery_required
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_hook_recovery_case").fetchone()[0] == 0
        assert (
            db.execute(
                "select count(*) from continuity_hook_invocation_ancestry_receipt"
            ).fetchone()[0]
            == 1
        )


def test_process_drift_recovery_commit_loss_is_read_only_classified(tmp_path: Path) -> None:
    class RecoveryAckLost(SQLiteCodexV4Ingress):
        @staticmethod
        def _commit(db: sqlite3.Connection) -> None:
            db.commit()
            raise TimeoutError("injected recovery commit acknowledgement loss")

    path, binding, manager, context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    manager.fail_invocation = True
    result = RecoveryAckLost(
        path,
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    ).session_start(event)
    assert result.recovery_required and result.replay
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_hook_recovery_case").fetchone()[0] == 1


def test_concurrent_identical_session_start_converges_to_one_graph(tmp_path: Path) -> None:
    path, binding, manager, _context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()

    def execute() -> bytes:
        local = SQLiteCodexV4Ingress(
            path,
            binding,
            process_manager=manager,
            context_port=_ContextPort(binding),
            spool=spool,
        )
        return local.session_start(event).stdout

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = tuple(pool.map(lambda _item: execute(), range(2)))
    assert outputs[0] == outputs[1]
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from session_event").fetchone()[0] == 1
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 1
        assert (
            db.execute("select count(*) from continuity_hook_attachment_revision").fetchone()[0]
            == 2
        )


def test_pending_local_work_blocks_session_start_without_partial_context(tmp_path: Path) -> None:
    path, binding, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    payload = {"operation": "unrelated", "session_id": binding.session_id}
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into local_job values(?,?,?,'ready',0,1,?,null,0,null,?,?)",
            ("pending-job", "pending-job-key", canonical_json(payload), NOW, NOW, NOW),
        )
    with pytest.raises(PolicyViolation, match="pending work"):
        ingress.session_start(event)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 0
        assert db.execute("select count(*) from session_event").fetchone()[0] == 0


def test_pending_effect_and_outbox_block_session_start_without_partial_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    job, _created = runtime.enqueue(
        idempotency_key="pending-effect-job",
        payload={"operation": "continuity.compile", "session_id": binding.session_id},
        available_at=NOW,
    )
    work = runtime.claim_next(
        owner_id="test-worker",
        owner_pid=101,
        owner_token="test-incarnation",
        lease_seconds=30,
        supported_operations=("continuity.compile",),
        job_id=job.id,
        now=NOW,
    )
    assert work is not None
    runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=digest("pending-effect"),
        idempotency_key="pending-effect-key",
        now=NOW,
    )
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from local_effect_claim").fetchone()[0] == 1
        assert db.execute("select count(*) from local_outbox_delivery").fetchone()[0] >= 1
    with pytest.raises(PolicyViolation, match="pending work"):
        ingress.session_start(event)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 0
        assert db.execute("select count(*) from session_event").fetchone()[0] == 0


def test_reviewed_command_and_hook_set_replay_drift_is_rejected(tmp_path: Path) -> None:
    path, _binding_value, manager, _context, _spool, _event_value, ingress = _prepared(tmp_path)
    ingress.attach_process()
    changed_hook_set = digest("changed-hook-set")
    manager.commands = tuple(
        replace(command, hook_set_digest=changed_hook_set) for command in manager.commands
    )
    manager.process = _replace_test_snapshot(
        manager.process,
        hook_set_digest=changed_hook_set,
        reviewed_commands=manager.commands,
    )
    with pytest.raises(PolicyViolation):
        ingress.attach_process()
    with sqlite3.connect(path) as db:
        assert (
            db.execute("select count(*) from continuity_reviewed_hook_command").fetchone()[0] == 3
        )
        assert (
            db.execute("select count(*) from continuity_managed_process_receipt").fetchone()[0] == 1
        )


@pytest.mark.parametrize(
    "table",
    (
        "continuity_managed_process_receipt",
        "continuity_hook_process_generation",
        "continuity_hook_attachment_revision",
    ),
)
def test_attach_replay_rejects_each_durable_process_graph_body_tamper(
    tmp_path: Path, table: str
) -> None:
    path, _binding_value, _manager, _context, _spool, _event_value, ingress = _prepared(tmp_path)
    ingress.attach_process()
    with sqlite3.connect(path) as db:
        triggers = db.execute(
            "select name,sql from sqlite_master where type='trigger' and tbl_name=? order by name",
            (table,),
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f'drop trigger "{name}"')
        db.execute(f"update \"{table}\" set body_json='{{}}'")
        for _name, sql in triggers:
            db.execute(sql)
    assert operational_schema.status(path).schema_ok
    with pytest.raises(PolicyViolation):
        ingress.attach_process()


def test_physical_spool_tamper_blocks_replay(tmp_path: Path) -> None:
    _path, _binding_value, _manager, _context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    entry = spool.read_session_entries(client_id="codex", session_id="codex-external-session")[0]
    spool._entry_path(entry.entry_digest).write_bytes(b"{}")
    with pytest.raises((PolicyViolation, ValidationFailed)):
        ingress.session_start(event)


def test_durable_manifest_body_tamper_is_detected_after_trigger_restoration(
    tmp_path: Path,
) -> None:
    path, _binding_value, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    with sqlite3.connect(path) as db:
        triggers = db.execute(
            "select name,sql from sqlite_master where type='trigger' "
            "and tbl_name='context_manifest' order by name"
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f'drop trigger "{name}"')
        db.execute("update context_manifest set body_json='{}'")
        for _name, sql in triggers:
            db.execute(sql)
    assert operational_schema.status(path).schema_ok
    with pytest.raises(PolicyViolation):
        ingress.session_start(event)


def test_hydrated_replay_rejects_self_consistent_native_ancestry_relation_drift(
    tmp_path: Path,
) -> None:
    path, _binding_value, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        triggers = db.execute(
            "select name,sql from sqlite_master where type='trigger' "
            "and tbl_name='continuity_native_event_receipt' order by name"
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f'drop trigger "{name}"')
        native = db.execute("select * from continuity_native_event_receipt").fetchone()
        assert native is not None
        body = json.loads(native["body_json"])
        body["hook_pid"] = 999
        db.execute(
            "update continuity_native_event_receipt set receipt_digest=?,hook_pid=?,body_json=?",
            (digest(body), 999, canonical_json(body)),
        )
        for _name, sql in triggers:
            db.execute(sql)
    with pytest.raises(PolicyViolation, match="native producer integrity drift"):
        ingress.session_start(event)


def _insert_orphan_session_event(path: Path, binding: ContinuityBinding, *, name: str) -> None:
    with sqlite3.connect(path) as db:
        triggers = db.execute(
            "select name,sql from sqlite_master where type='trigger' "
            "and tbl_name='session_event' order by name"
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f'drop trigger "{name}"')
        db.execute(
            "insert into session_event(id,session_id,event_kind,event_digest,created_at) "
            "values(?,?,?,?,?)",
            (
                name,
                binding.session_id,
                "SESSION_START",
                digest(name),
                "2026-09-03T12:00:30+00:00",
            ),
        )
        for _name, sql in triggers:
            db.execute(sql)


def test_hydrated_replay_rejects_extra_same_session_event_row(tmp_path: Path) -> None:
    path, binding, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    _insert_orphan_session_event(path, binding, name="builder-extra-event")
    assert operational_schema.status(path).schema_ok
    with pytest.raises(PolicyViolation, match=r"event|graph|cardinality"):
        ingress.session_start(event)


def test_process_drift_creation_rejects_extra_event_without_recovery_mutation(
    tmp_path: Path,
) -> None:
    path, binding, manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    _insert_orphan_session_event(path, binding, name="builder-extra-before-recovery")
    manager.fail_invocation = True
    with pytest.raises(PolicyViolation, match=r"event|graph|cardinality"):
        ingress.session_start(event)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_hook_recovery_case").fetchone()[0] == 0
        assert (
            db.execute(
                "select state from continuity_hook_attachment_revision "
                "order by revision_number desc limit 1"
            ).fetchone()[0]
            == "hydrated"
        )


def test_process_drift_replay_rejects_extra_same_session_event_row(tmp_path: Path) -> None:
    path, binding, manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    manager.fail_invocation = True
    assert ingress.session_start(event).recovery_required
    _insert_orphan_session_event(path, binding, name="builder-extra-after-recovery")
    with pytest.raises(PolicyViolation, match=r"event|graph|cardinality"):
        ingress.session_start(event)


def _copy_receipt_row(
    db: sqlite3.Connection,
    table: str,
    *,
    changes: dict[str, object],
) -> dict[str, object]:
    db.row_factory = sqlite3.Row
    triggers = db.execute(
        "select name,sql from sqlite_master where type='trigger' and tbl_name=? order by name",
        (table,),
    ).fetchall()
    for name, _sql in triggers:
        db.execute(f'drop trigger "{name}"')
    original = db.execute(f'select * from "{table}" limit 1').fetchone()
    assert original is not None
    row = dict(original)
    body = json.loads(row["body_json"])
    for name, value in changes.items():
        row[name] = value
        body[name] = value
    row["receipt_digest"] = digest(body)
    row["body_json"] = canonical_json(body)
    db.execute(
        f'insert into "{table}"(' + ",".join(row) + ") values(" + ",".join("?" for _ in row) + ")",
        tuple(row.values()),
    )
    for _name, sql in triggers:
        db.execute(sql)
    return row


@pytest.mark.parametrize("extra_kind", ("detail", "internal", "native", "ancestry"))
def test_hydrated_replay_rejects_neighboring_extra_slice_a_graph_rows(
    tmp_path: Path, extra_kind: str
) -> None:
    path, binding, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        if extra_kind == "detail":
            for table in ("session_event", "session_event_detail"):
                triggers = db.execute(
                    "select name,sql from sqlite_master where type='trigger' "
                    "and tbl_name=? order by name",
                    (table,),
                ).fetchall()
                for name, _sql in triggers:
                    db.execute(f'drop trigger "{name}"')
                if table == "session_event":
                    db.execute(
                        "insert into session_event values(?,?,?,?,?)",
                        (
                            "builder-extra-detail-event",
                            binding.session_id,
                            "SESSION_START",
                            digest("builder-extra-detail"),
                            "2026-09-03T12:00:30+00:00",
                        ),
                    )
                else:
                    db.execute(
                        "insert into session_event_detail values(?,?,?,?,?,?,?,?)",
                        (
                            "builder-extra-detail-event",
                            binding.session_id,
                            2,
                            digest("previous"),
                            "builder-extra-detail-key",
                            digest("builder-extra-detail"),
                            digest("builder-extra-spool"),
                            "{}",
                        ),
                    )
                for _name, sql in triggers:
                    db.execute(sql)
        elif extra_kind == "internal":
            triggers = db.execute(
                "select name,sql from sqlite_master where type='trigger' "
                "and tbl_name='continuity_internal_event_receipt' order by name"
            ).fetchall()
            for name, _sql in triggers:
                db.execute(f'drop trigger "{name}"')
            native_digest = db.execute(
                "select receipt_digest from continuity_native_event_receipt"
            ).fetchone()[0]
            event_digest = db.execute("select event_digest from session_event_detail").fetchone()[0]
            revision = db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "order by revision_number desc limit 1"
            ).fetchone()[0]
            db.execute(
                "insert into continuity_internal_event_receipt("
                "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
                "expected_previous_event_digest,turn_commit_digest,effect_claim_id,"
                "effect_receipt_id,native_event_receipt_digest,close_request_digest,"
                "close_receipt_digest,hook_recovery_resolution_id,local_recovery_resolution_id,"
                "attachment_revision_digest,grants_authority,approval_inherited,body_json,created_at"
                ") values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    digest("builder-extra-internal-receipt"),
                    event_digest,
                    binding.session_id,
                    binding.binding_digest,
                    "CHECKPOINT_REQUESTED",
                    "builder-extra-internal-key",
                    None,
                    None,
                    None,
                    None,
                    native_digest,
                    None,
                    None,
                    None,
                    None,
                    revision,
                    0,
                    0,
                    "{}",
                    "2026-09-03T12:00:30+00:00",
                ),
            )
            for _name, sql in triggers:
                db.execute(sql)
        elif extra_kind == "ancestry":
            _copy_receipt_row(
                db,
                "continuity_hook_invocation_ancestry_receipt",
                changes={"delivery_id": "builder-extra-ancestry-delivery"},
            )
        else:
            extra_native_event_digest = digest("builder-extra-native-event")
            for table in ("session_event", "session_event_detail"):
                triggers = db.execute(
                    "select name,sql from sqlite_master where type='trigger' "
                    "and tbl_name=? order by name",
                    (table,),
                ).fetchall()
                for name, _sql in triggers:
                    db.execute(f'drop trigger "{name}"')
                if table == "session_event":
                    db.execute(
                        "insert into session_event values(?,?,?,?,?)",
                        (
                            "builder-extra-native-event",
                            binding.session_id,
                            "SESSION_START",
                            extra_native_event_digest,
                            "2026-09-03T12:00:30+00:00",
                        ),
                    )
                else:
                    db.execute(
                        "insert into session_event_detail values(?,?,?,?,?,?,?,?)",
                        (
                            "builder-extra-native-event",
                            binding.session_id,
                            2,
                            digest("builder-native-previous"),
                            "builder-extra-native-key",
                            extra_native_event_digest,
                            digest("builder-extra-native-spool"),
                            "{}",
                        ),
                    )
                for _name, sql in triggers:
                    db.execute(sql)
            ancestry = _copy_receipt_row(
                db,
                "continuity_hook_invocation_ancestry_receipt",
                changes={"delivery_id": "builder-extra-native-ancestry-delivery"},
            )
            _copy_receipt_row(
                db,
                "continuity_native_event_receipt",
                changes={
                    "event_digest": extra_native_event_digest,
                    "ancestry_receipt_digest": ancestry["receipt_digest"],
                    "delivery_id": "builder-extra-native-delivery",
                    "spool_sequence": 2,
                    "previous_spool_digest": digest("builder-extra-native-previous"),
                },
            )
    assert operational_schema.status(path).schema_ok
    with pytest.raises(PolicyViolation, match=r"event|graph|cardinality"):
        ingress.session_start(event)


def test_hydrated_replay_cardinality_reads_one_pinned_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, binding, _manager, context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    with sqlite3.connect(path) as db:
        db.execute("pragma journal_mode=wal")

    reader_ready = threading.Event()
    writer_done = threading.Event()
    original_build = context.build
    block_once = True

    def blocked_build(
        binding_value: ContinuityBinding,
        *,
        hydration_key: str,
        observed_at: str,
    ) -> FrozenCurrentStartupContext:
        nonlocal block_once
        if block_once:
            block_once = False
            reader_ready.set()
            assert writer_done.wait(5)
        return original_build(binding_value, hydration_key=hydration_key, observed_at=observed_at)

    def insert_after_reader_snapshot() -> None:
        assert reader_ready.wait(5)
        with sqlite3.connect(path, timeout=5) as db:
            triggers = db.execute(
                "select name,sql from sqlite_master where type='trigger' "
                "and tbl_name='session_event' order by name"
            ).fetchall()
            for name, _sql in triggers:
                db.execute(f'drop trigger "{name}"')
            db.execute(
                "insert into session_event(id,session_id,event_kind,event_digest,created_at) "
                "values(?,?,?,?,?)",
                (
                    "builder-concurrent-extra-event",
                    binding.session_id,
                    "SESSION_START",
                    digest("builder-concurrent-extra-event"),
                    "2026-09-03T12:00:30+00:00",
                ),
            )
            for _name, sql in triggers:
                db.execute(sql)
        writer_done.set()

    monkeypatch.setattr(context, "build", blocked_build)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(insert_after_reader_snapshot)
        # The existing read transaction has already pinned its snapshot before
        # build is called.  It must observe either the complete old graph or the
        # complete new graph, never a mixture of cardinality/detail rows.
        assert ingress.session_start(event).replay
        writer.result(timeout=5)
    monkeypatch.setattr(context, "build", original_build)
    with pytest.raises(PolicyViolation, match=r"event|graph|cardinality"):
        ingress.session_start(event)


def test_hydrated_replay_accepts_fully_verified_neighboring_internal_event(
    tmp_path: Path,
) -> None:
    path, binding, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    occurred_at = "2026-09-03T12:00:30+00:00"
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.row_factory = sqlite3.Row
        previous = db.execute(
            "select event_digest from session_event_detail where session_id=? and sequence=1",
            (binding.session_id,),
        ).fetchone()[0]
        attachment_revision = db.execute(
            "select revision_digest from continuity_hook_attachment_revision "
            "order by revision_number desc limit 1"
        ).fetchone()[0]
        turn_body = {
            "binding_digest": binding.binding_digest,
            "content_digest": digest("builder-user-turn-content"),
            "created_at": occurred_at,
            "item_ref": "builder-user-turn-item",
            "previous_turn_commit_digest": None,
            "role": "user",
            "session_id": binding.session_id,
            "store_generation_digest": digest("builder-turn-store-generation"),
        }
        turn_digest = digest(turn_body)
        operation_key = "builder-user-turn-committed"
        event_body = {
            "kind": "USER_TURN_COMMITTED",
            "idempotency_key": operation_key,
            "occurred_at": occurred_at,
            "source_refs": [],
            "evidence_digests": [turn_digest],
            "spool_digest": None,
        }
        envelope = {
            "session_id": binding.session_id,
            "binding_digest": binding.binding_digest,
            "sequence": 2,
            "previous_digest": previous,
            "event": event_body,
        }
        event_digest = digest(envelope)
        receipt_body = {
            "attachment_revision_digest": attachment_revision,
            "binding_digest": binding.binding_digest,
            "created_at": occurred_at,
            "event_digest": event_digest,
            "event_kind": "USER_TURN_COMMITTED",
            "expected_previous_event_digest": previous,
            "operation_key": operation_key,
            "session_id": binding.session_id,
        }
        receipt_digest = internal_receipt_digest(
            receipt_body,
            producer_kind="turn_commit_digest",
            producer_ref=turn_digest,
        )
        db.execute("begin")
        db.execute(
            "insert into continuity_turn_commit_receipt values(?,?,?,?,?,?,?,?,?,?)",
            (
                turn_digest,
                binding.session_id,
                binding.binding_digest,
                "user",
                "builder-user-turn-item",
                turn_body["content_digest"],
                turn_body["store_generation_digest"],
                None,
                canonical_json(turn_body),
                occurred_at,
            ),
        )
        db.execute(
            "insert into continuity_internal_event_receipt("
            "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
            "expected_previous_event_digest,turn_commit_digest,effect_claim_id,effect_receipt_id,"
            "native_event_receipt_digest,close_request_digest,close_receipt_digest,"
            "hook_recovery_resolution_id,local_recovery_resolution_id,"
            "attachment_revision_digest,body_json,created_at) values("
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_digest,
                event_digest,
                binding.session_id,
                binding.binding_digest,
                "USER_TURN_COMMITTED",
                operation_key,
                previous,
                turn_digest,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                attachment_revision,
                canonical_json(receipt_body),
                occurred_at,
            ),
        )
        db.execute(
            "insert into session_event values(?,?,?,?,?)",
            (
                "builder-user-turn-event",
                binding.session_id,
                "USER_TURN_COMMITTED",
                event_digest,
                occurred_at,
            ),
        )
        db.execute(
            "insert into session_event_detail values(?,?,?,?,?,?,?,?)",
            (
                "builder-user-turn-event",
                binding.session_id,
                2,
                previous,
                operation_key,
                event_digest,
                None,
                canonical_json(envelope),
            ),
        )
        db.commit()
    assert ingress.session_start(event).replay


@pytest.mark.parametrize(
    "changes",
    (
        {"external_turn_id": "fabricated-turn"},
        {"external_trigger_id": "fabricated-trigger"},
        {
            "spool_sequence": 2,
            "previous_spool_digest": digest("fabricated-previous"),
        },
    ),
)
def test_hydrated_replay_rejects_recanonicalized_native_session_start_literals(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    path, _binding_value, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        triggers = db.execute(
            "select name,sql from sqlite_master where type='trigger' "
            "and tbl_name='continuity_native_event_receipt' order by name"
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f'drop trigger "{name}"')
        native = db.execute("select * from continuity_native_event_receipt").fetchone()
        assert native is not None
        body = json.loads(native["body_json"])
        body.update(changes)
        assignments = ["receipt_digest=?", "body_json=?"] + [f"{name}=?" for name in changes]
        db.execute(
            "update continuity_native_event_receipt set " + ",".join(assignments),
            (digest(body), canonical_json(body), *changes.values()),
        )
        for _name, sql in triggers:
            db.execute(sql)
    with pytest.raises(PolicyViolation, match="historical SessionStart graph"):
        ingress.session_start(event)


def test_process_drift_recovery_replay_rechecks_hydrated_context_graph(
    tmp_path: Path,
) -> None:
    path, _binding_value, manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    manager.fail_invocation = True
    assert ingress.session_start(event).recovery_required
    with sqlite3.connect(path) as db:
        triggers = db.execute(
            "select name,sql from sqlite_master where type='trigger' "
            "and tbl_name='context_manifest' order by name"
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f'drop trigger "{name}"')
        db.execute("update context_manifest set body_json='{}'")
        for _name, sql in triggers:
            db.execute(sql)
    with pytest.raises(PolicyViolation):
        ingress.session_start(event)


def test_process_drift_recovery_rejects_extra_same_session_manifest(tmp_path: Path) -> None:
    path, binding, manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    manager.fail_invocation = True
    assert ingress.session_start(event).recovery_required
    extra = {"schema": "test-extra-context/v1"}
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into context_manifest values(?,?,?,?,?,?,?)",
            (
                digest(extra),
                binding.session_id,
                None,
                1,
                0,
                canonical_json(extra),
                "2026-09-03T12:00:30+00:00",
            ),
        )
    with pytest.raises(PolicyViolation, match="context graph"):
        ingress.session_start(event)


def test_recovery_replay_rejects_extra_same_scope_case(tmp_path: Path) -> None:
    path, _binding_value, manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    manager.fail_invocation = True
    assert ingress.session_start(event).recovery_required
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("select * from continuity_hook_recovery_case").fetchone()
        assert row is not None
        case_id = "018f0000-0000-7000-8000-000000000299"
        body = {
            "attachment_id": row["attachment_id"],
            "case_kind": "transaction-unknown",
            "created_at": "2026-09-03T12:00:30+00:00",
            "evidence_digest": digest("test-extra-case"),
            "process_generation_digest": row["process_generation_digest"],
            "recovery_case_id": case_id,
            "session_id": row["session_id"],
        }
        db.execute(
            "insert into continuity_hook_recovery_case values(?,?,?,?,?,?,?,?)",
            (
                case_id,
                body["attachment_id"],
                body["session_id"],
                body["process_generation_digest"],
                body["case_kind"],
                body["evidence_digest"],
                canonical_json(body),
                body["created_at"],
            ),
        )
    with pytest.raises(PolicyViolation, match="cardinality"):
        ingress.session_start(event)


def test_transaction_unknown_replay_rejects_later_partial_context(
    tmp_path: Path,
) -> None:
    class BeforeCommit(SQLiteCodexV4Ingress):
        calls = 0

        @staticmethod
        def _commit(db: sqlite3.Connection) -> None:
            BeforeCommit.calls += 1
            if BeforeCommit.calls == 1:
                raise TimeoutError("primary commit uncertain")
            db.commit()

    path, binding, manager, context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    result = BeforeCommit(
        path,
        binding,
        process_manager=manager,
        context_port=context,
        spool=spool,
    ).session_start(event)
    assert result.recovery_required
    partial = {"schema": "test-conflicting-context/v1"}
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into context_manifest values(?,?,?,?,?,?,?)",
            (digest(partial), binding.session_id, None, 1, 0, canonical_json(partial), NOW),
        )
    with pytest.raises(PolicyViolation, match="partial effects"):
        ingress.session_start(event)


def test_final_recovery_no_effect_classifier_rejects_conflicting_operation_rows(
    tmp_path: Path,
) -> None:
    class ConflictDuringRecovery(SQLiteCodexV4Ingress):
        calls = 0

        @staticmethod
        def _commit(db: sqlite3.Connection) -> None:
            ConflictDuringRecovery.calls += 1
            if ConflictDuringRecovery.calls == 1:
                raise TimeoutError("primary commit uncertain")
            db.rollback()
            with sqlite3.connect(db.execute("pragma database_list").fetchone()[2]) as other:
                partial = {"schema": "test-final-classifier-conflict/v1"}
                other.execute(
                    "insert into context_manifest values(?,?,?,?,?,?,?)",
                    (
                        digest(partial),
                        SESSION,
                        None,
                        1,
                        0,
                        canonical_json(partial),
                        NOW,
                    ),
                )
            raise TimeoutError("recovery commit uncertain")

    path, binding, manager, context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    with pytest.raises(PolicyViolation, match="census conflict"):
        ConflictDuringRecovery(
            path,
            binding,
            process_manager=manager,
            context_port=context,
            spool=spool,
        ).session_start(event)


def test_final_recovery_no_effect_classifier_rejects_orphan_recovery_case(
    tmp_path: Path,
) -> None:
    class OrphanDuringRecovery(SQLiteCodexV4Ingress):
        calls = 0

        @staticmethod
        def _commit(db: sqlite3.Connection) -> None:
            OrphanDuringRecovery.calls += 1
            if OrphanDuringRecovery.calls == 1:
                raise TimeoutError("primary commit uncertain")
            database = db.execute("pragma database_list").fetchone()[2]
            db.rollback()
            with sqlite3.connect(database) as other:
                other.row_factory = sqlite3.Row
                attachment = other.execute(
                    "select attachment_id from continuity_hook_attachment"
                ).fetchone()
                generation = other.execute(
                    "select process_generation_digest from continuity_hook_process_generation"
                ).fetchone()
                assert attachment is not None and generation is not None
                case_id = "018f0000-0000-7000-8000-000000000298"
                body = {
                    "attachment_id": attachment[0],
                    "case_kind": "transaction-unknown",
                    "created_at": "2026-09-03T12:00:30+00:00",
                    "evidence_digest": digest("test-final-orphan-case"),
                    "process_generation_digest": generation[0],
                    "recovery_case_id": case_id,
                    "session_id": SESSION,
                }
                other.execute(
                    "insert into continuity_hook_recovery_case values(?,?,?,?,?,?,?,?)",
                    (
                        case_id,
                        body["attachment_id"],
                        body["session_id"],
                        body["process_generation_digest"],
                        body["case_kind"],
                        body["evidence_digest"],
                        canonical_json(body),
                        body["created_at"],
                    ),
                )
            raise TimeoutError("recovery commit uncertain")

    path, binding, manager, context, spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    with pytest.raises(PolicyViolation, match="census conflict"):
        OrphanDuringRecovery(
            path,
            binding,
            process_manager=manager,
            context_port=context,
            spool=spool,
        ).session_start(event)


def test_missing_durable_native_row_is_detected_without_reconstruction(
    tmp_path: Path,
) -> None:
    path, _binding_value, _manager, _context, _spool, event, ingress = _prepared(tmp_path)
    ingress.attach_process()
    ingress.session_start(event)
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=off")
        triggers = db.execute(
            "select name,sql from sqlite_master where type='trigger' "
            "and tbl_name='continuity_native_event_receipt' order by name"
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f'drop trigger "{name}"')
        db.execute("delete from continuity_native_event_receipt")
        for _name, sql in triggers:
            db.execute(sql)
    with pytest.raises((PolicyViolation, ConfigurationError)):
        ingress.session_start(event)


def test_default_v3_is_rejected_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "operational.db"
    operational_schema.bootstrap(path)
    # Construction is inert; the explicit v4 gate rejects before any manager call.
    binding = _binding()
    ingress = SQLiteCodexV4Ingress(
        path,
        binding,
        process_manager=_Manager(binding),
        context_port=_ContextPort(binding),
        spool=ClientLifecycleSpool(tmp_path / "home", client_id="codex"),
    )
    try:
        ingress.attach_process()
    except Exception:
        pass
    else:
        raise AssertionError("default v3 must reject dormant v4 ingress")
    with sqlite3.connect(path) as db:
        assert (
            db.execute("select value from zekam_meta where key='schema_version'").fetchone()[0]
            == "3"
        )


def test_dormant_production_composition_rejects_default_v3_before_side_effect(
    tmp_path: Path,
) -> None:
    path = tmp_path / "operational.db"
    home = tmp_path / "home"
    operational_schema.bootstrap(path)
    before = path.read_bytes()
    arguments = DormantCodex0151V4Arguments(
        path,
        home,
        ROOT,
        _binding(),
        (SOURCE_REF,),
    )
    with pytest.raises(PolicyViolation, match="schema v4"):
        DormantCodex0151V4Runtime(arguments)
    assert path.read_bytes() == before
    assert not home.exists()


def test_dormant_production_composition_hydrates_existing_v4_with_real_bounded_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    HomeLayout(home).ensure()
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\ndatabase:\n  backend: sqlite\n"
        "  sqlite_relative_path: state/operational.db\n",
        encoding="utf-8",
    )
    path = home / "state/operational.db"
    operational_schema.bootstrap_v4(path)
    monkeypatch.setattr(operational_store_module, "SCHEMA_VERSION", 4)
    store = SQLiteOperationalStore(path)
    config_body = load_settings(home=home, environ={}).sanitized()
    task_digest = ActiveTaskContract.load(core_root() / "AKTIF_GOREV.md").source_digest
    with store.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest(config_body),
            task_digest=task_digest,
            sanitized_config=config_body,
        )
        project = uow.create_project(slug="akilli-kasa", display_name="Akilli Kasa")
        source_binding = uow.bind_source(
            project_id=project.id,
            portable_ref="project/akilli-kasa-git",
            source_kind="git",
        )
        uow.commit()
    realm_id = "018f0000-0000-7000-8000-000000000177"
    with sqlite3.connect(path) as db:
        db.execute("insert into project_knowledge_realm values(?,?,?)", (project.id, realm_id, NOW))
    recipe = ContinuitySourceRecipe(
        project.id,
        realm_id,
        source_binding.id,
        (SOURCE_REF,),
        task_digest,
        config.config_digest,
    )
    source = BoundedContinuitySource(ROOT, recipe)
    plan = source.capture()
    snapshot = source.apply(store, plan, expected_plan_digest=plan.content_digest)
    work_payload = {"summary": "Read the bounded health source"}
    with store.unit_of_work() as uow:
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Dormant SessionStart",
            state="ready",
            payload=work_payload,
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            source_snapshot_id=snapshot.id,
            plan_digest=digest("slice-a-plan"),
            budget={"max_seconds": 60},
        )
        uow.commit()
    binding = ContinuityBinding(
        SESSION,
        "codex-external-session",
        project.id,
        realm_id,
        "codex",
        "macbook",
        snapshot.id,
        task_digest,
        run.plan_digest,
        config.config_digest,
        work.id,
        run.id,
    )
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into session(id,client_id,device_id,project_id,work_item_id,status,opened_at) "
            "values(?,?,?,?,?,?,?)",
            (SESSION, "codex", "macbook", project.id, work.id, "open", NOW),
        )
        db.execute("insert into local_runtime_config values(1,64)")
        db.execute(
            "insert into continuity_session_binding values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                SESSION,
                binding.external_session_id,
                project.id,
                realm_id,
                work.id,
                run.id,
                "codex",
                "macbook",
                snapshot.id,
                task_digest,
                run.plan_digest,
                config.config_digest,
                binding.binding_digest,
                NOW,
            ),
        )
    manager = _Manager(binding)
    monkeypatch.setattr(composition_module, "TrustedCodex0151ProcessManager", lambda: manager)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("composition must not use network"),
    )
    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("composition must not open a network socket"),
    )
    source_before = (ROOT / SOURCE_REF).read_bytes()
    config_path = home / "config.yaml"
    config_before = config_path.read_bytes()
    config_path.write_text("schema: unsupported\n", encoding="utf-8")
    spool_state = home / "global/runtime/client-lifecycle/codex/queue-state.json"
    with pytest.raises(ConfigurationError):
        DormantCodex0151V4Runtime(
            DormantCodex0151V4Arguments(path, home, ROOT, binding, (SOURCE_REF,))
        )
    assert not spool_state.exists()
    config_path.write_bytes(config_before)
    runtime = DormantCodex0151V4Runtime(
        DormantCodex0151V4Arguments(path, home, ROOT, binding, (SOURCE_REF,))
    )
    payload = canonical_json(
        {
            "session_id": "codex-external-session",
            "transcript_path": None,
            "cwd": str(ROOT),
            "hook_event_name": "SessionStart",
            "source": "startup",
            "model": "gpt-5.6",
            "permission_mode": "default",
        }
    ).encode()
    result = runtime.handle(payload)
    assert result.exit_status == 0 and result.hydrated and not result.recovery_required
    replay = runtime.handle(payload)
    assert replay.stdout == result.stdout and replay.hydrated
    outer = json.loads(result.stdout)
    additional = json.loads(outer["hookSpecificOutput"]["additionalContext"])
    with sqlite3.connect(path) as db:
        manifest = json.loads(db.execute("select body_json from context_manifest").fetchone()[0])
        counts = db.execute(
            "select (select count(*) from context_manifest),"
            "(select count(*) from hydration_receipt),"
            "(select count(*) from session_event)"
        ).fetchone()
    assert [item["candidate_id"] for item in additional["fragments"]] == [
        item["candidate_id"] for item in manifest["context"]["compiler"]["selected"]
    ]
    assert [item["token_count"] for item in additional["fragments"]] == [441, 464, 412, 429]
    assert sum(item["token_count"] for item in additional["fragments"]) == 1746
    assert len(outer["hookSpecificOutput"]["additionalContext"].encode("utf-8")) == 3171
    assert len(result.stdout) == 3596
    assert digest_of_bytes(source_before) == (
        "sha256:513234afc3a08fac74170cc3232c25cf8e4ca110b98c93c7bc36d1092b49cf95"
    )
    assert counts == (1, 1, 1)
    config_path.write_text("schema: unsupported\n", encoding="utf-8")
    denied_environment = runtime.handle(payload)
    assert not denied_environment.hydrated
    assert b"ZEKAM_SESSION_START_REJECTED" in denied_environment.stdout
    config_path.write_bytes(config_before)
    with sqlite3.connect(path) as db:
        db.execute("update run set plan_digest=? where id=?", (digest("drift"), run.id))
    denied_run = runtime.handle(payload)
    assert not denied_run.hydrated and b"ZEKAM_SESSION_START_REJECTED" in denied_run.stdout
    with sqlite3.connect(path) as db:
        db.execute("update run set plan_digest=? where id=?", (binding.plan_digest, run.id))
        db.execute("update work_item set state='active' where id=?", (work.id,))
    denied_work = runtime.handle(payload)
    assert not denied_work.hydrated and b"ZEKAM_SESSION_START_REJECTED" in denied_work.stdout
    with sqlite3.connect(path) as db:
        db.execute("update work_item set state='ready' where id=?", (work.id,))
    production_context = runtime.ingress.context_port
    assert isinstance(production_context, _CurrentV4SessionStartContext)
    original_probe = production_context.source.probe
    monkeypatch.setattr(
        production_context.source,
        "probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PolicyViolation("injected production source drift")
        ),
    )
    denied = runtime.handle(payload)
    assert not denied.hydrated and b"ZEKAM_SESSION_START_REJECTED" in denied.stdout
    monkeypatch.setattr(production_context.source, "probe", original_probe)
    changed_config = dict(config_body)
    changed_config["runtime"] = {**changed_config.get("runtime", {}), "log_level": "DEBUG"}
    with store.unit_of_work() as uow:
        uow.activate_config(
            config_digest=digest(changed_config),
            task_digest=task_digest,
            sanitized_config=changed_config,
        )
        uow.commit()
    denied_config = runtime.handle(payload)
    assert not denied_config.hydrated and b"ZEKAM_SESSION_START_REJECTED" in denied_config.stdout
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from context_manifest").fetchone()[0] == 1
    assert (ROOT / SOURCE_REF).read_bytes() == source_before
