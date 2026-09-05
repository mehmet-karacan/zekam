from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.unit import test_context_recipe as recipe_fixture
from tests.unit import test_fresh_bootstrap as bootstrap_fixture

from zekam.application import context_recipe as recipe
from zekam.application import doctor_repair as doctor
from zekam.application import fresh_bootstrap as bootstrap
from zekam.application import provider_adapter as provider
from zekam.application.context_ranking import ContextCandidateSetIssuer
from zekam.application.model_health_service import ProbeUnavailable
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import AuthorityLevel, ContextCandidateKind
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.security import AuthorizationState, OutboundState
from zekam.infrastructure.process import capability_worker as worker
from zekam.infrastructure.sqlite.operational_schema import SQLiteOperationalSchema


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


class _Opener:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def open(self, _request: object, *, timeout: float) -> _Response:
        assert timeout > 0
        return _Response(self.payload)


def test_provider_remaining_json_and_structured_parser_paths() -> None:
    request = urllib.request.Request("https://example.test/v1")
    assert provider._read_json_response(
        _Opener(b'{"ok":true}'), request, timeout_seconds=1, max_response_bytes=32
    ) == {"ok": True}
    assert (
        provider.openai_chat_text(
            {
                "choices": [
                    {"message": {"content": [None, {"text": 7}, {"text": " a "}, {"text": "b"}]}}
                ]
            }
        )
        == "a b"
    )
    with pytest.raises(ValidationFailed, match="exact JSON object"):
        provider._chat_json_object({"choices": [{"message": {"content": "[]"}}]})
    with pytest.raises(ValidationFailed, match="sayisi"):
        provider.openai_guardrail_labels(
            {"choices": [{"message": {"content": '{"labels":null}'}}]}, expected_count=1
        )
    with pytest.raises(ValidationFailed, match="string listesi"):
        provider.openai_vision_objects({"choices": [{"message": {"content": '{"objects":[1]}'}}]})


def _provider_call(**changes: object) -> provider.ProviderCall:
    values: dict[str, object] = {
        "provider_ref": "provider",
        "endpoint_ref": "endpoint",
        "operation": "chat",
        "request_identity": "request",
        "payload": {"value": 1},
    }
    values.update(changes)
    return provider.ProviderCall(**cast(Any, values))


def _multipart_call(**changes: object) -> provider.MultipartProviderCall:
    body = provider.build_multipart_body(
        fields={"model": "m"},
        file_field="file",
        filename="a.wav",
        file_content_type="audio/wav",
        content=b"audio",
    )
    values: dict[str, object] = {
        "provider_ref": "provider",
        "endpoint_ref": "endpoint",
        "operation": "audio",
        "request_identity": "request",
        "payload": body,
    }
    values.update(changes)
    return provider.MultipartProviderCall(**cast(Any, values))


def test_provider_exact_contract_binding_all_remaining_branches() -> None:
    endpoint = "https://example.test/v1"
    bare = _provider_call()
    provider.AuthorizedProviderClient._require_exact_contract_binding(
        bare, endpoint=endpoint, authorization=cast(Any, SimpleNamespace())
    )
    partial = _provider_call(endpoint_path_hint="/v1")
    with pytest.raises(PolicyViolation, match="binding eksik"):
        provider.AuthorizedProviderClient._require_exact_contract_binding(
            partial, endpoint=endpoint, authorization=cast(Any, SimpleNamespace())
        )
    plan = digest("plan")
    bound = _provider_call(
        endpoint_path_hint="/v1",
        endpoint_binding_digest=provider.reviewed_endpoint_digest(endpoint, path_hint="/v1"),
        authorization_plan_digest=plan,
        authorization_resource="resource",
    )
    for authorization, message in (
        (SimpleNamespace(state=AuthorizationState.CONSUMED, plan_digest=plan), "issued"),
        (SimpleNamespace(state=AuthorizationState.ISSUED, plan_digest=digest("other")), "plan"),
    ):
        with pytest.raises(PolicyViolation, match=message):
            provider.AuthorizedProviderClient._require_exact_contract_binding(
                bound, endpoint=endpoint, authorization=cast(Any, authorization)
            )
    provider.AuthorizedProviderClient._require_exact_contract_binding(
        bound,
        endpoint=endpoint,
        authorization=cast(Any, SimpleNamespace(state=AuthorizationState.ISSUED, plan_digest=plan)),
    )
    drift = replace(bound, endpoint_binding_digest=digest("wrong"))
    with pytest.raises(PolicyViolation, match="endpoint"):
        provider.AuthorizedProviderClient._require_exact_contract_binding(
            drift,
            endpoint=endpoint,
            authorization=cast(
                Any, SimpleNamespace(state=AuthorizationState.ISSUED, plan_digest=plan)
            ),
        )
    assert bare.effect_request("target").resources == ("target",)
    assert _multipart_call().effect_request("target").resources == ("target",)
    assert _multipart_call(
        authorization_plan_digest=plan, authorization_resource="resource"
    ).effect_request("target").resources == ("target", "resource")


class _Permit:
    def assert_for(self, _manifest: object) -> None:
        return None


def _provider_client(monkeypatch: pytest.MonkeyPatch, *, denied: bool = False) -> Any:
    state = OutboundState.DENIED if denied else OutboundState.APPROVED

    class Gate:
        def __init__(self, _governance: object) -> None:
            pass

        def prepare(self, _request: object) -> Any:
            return SimpleNamespace(state=state, denial_reason="denied")

        def apply(self, _request: object, *, authorization: object) -> Any:
            del authorization
            return SimpleNamespace(target="target")

    monkeypatch.setattr(provider, "ProviderGate", Gate)
    return provider.AuthorizedProviderClient(
        governance=cast(Any, SimpleNamespace(realm=SimpleNamespace(id="realm"))),
        endpoints=cast(Any, SimpleNamespace(resolve=lambda *_args: "https://example.test/v1")),
        broker=cast(Any, object()),
        transport=cast(Any, object()),
        multipart_transport=cast(Any, object()),
    )


@pytest.mark.parametrize("multipart", (False, True))
def test_provider_invoke_early_gate_matrix(
    monkeypatch: pytest.MonkeyPatch, multipart: bool
) -> None:
    call = _multipart_call() if multipart else _provider_call()
    invoke = (
        _provider_client(monkeypatch).invoke_multipart
        if multipart
        else _provider_client(monkeypatch).invoke
    )
    manifest = SimpleNamespace(provider_ref="wrong", payload_digest=call.payload_digest)
    common = {
        "secret_ref": SimpleNamespace(provider=call.provider_ref),
        "authorization": SimpleNamespace(),
        "consumed_by": "test",
        "manifest": manifest,
        "gateway_permit": _Permit(),
    }
    with pytest.raises(PolicyViolation, match="manifest"):
        invoke(call, **cast(Any, common))
    manifest.provider_ref = call.provider_ref
    manifest.payload_digest = digest("wrong")
    with pytest.raises(PolicyViolation, match="manifest"):
        invoke(call, **cast(Any, common))
    manifest.payload_digest = call.payload_digest
    common["secret_ref"] = SimpleNamespace(provider="wrong")
    with pytest.raises(PolicyViolation, match="SecretRef"):
        invoke(call, **cast(Any, common))


def test_provider_denied_and_effect_mismatch_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    call = _provider_call()
    manifest = SimpleNamespace(provider_ref=call.provider_ref, payload_digest=call.payload_digest)
    common = {
        "secret_ref": SimpleNamespace(provider=call.provider_ref),
        "authorization": SimpleNamespace(effect_digest=digest("wrong")),
        "consumed_by": "test",
        "manifest": manifest,
        "gateway_permit": _Permit(),
    }
    with pytest.raises(PolicyViolation, match="reddedildi"):
        _provider_client(monkeypatch, denied=True).invoke(call, **cast(Any, common))
    with pytest.raises(PolicyViolation, match="effect digest"):
        _provider_client(monkeypatch).invoke(call, **cast(Any, common))


@pytest.mark.parametrize("multipart", (False, True))
def test_provider_denied_and_valid_effect_paths(
    monkeypatch: pytest.MonkeyPatch, multipart: bool
) -> None:
    call = _multipart_call() if multipart else _provider_call()
    manifest = SimpleNamespace(provider_ref=call.provider_ref, payload_digest=call.payload_digest)
    effect_digest = call.effect_request("target").effect_digest
    common = {
        "secret_ref": SimpleNamespace(provider=call.provider_ref),
        "authorization": SimpleNamespace(effect_digest=effect_digest),
        "consumed_by": "test",
        "manifest": manifest,
        "gateway_permit": _Permit(),
    }
    denied_client = _provider_client(monkeypatch, denied=True)
    denied_invoke = denied_client.invoke_multipart if multipart else denied_client.invoke
    with pytest.raises(PolicyViolation, match="reddedildi"):
        denied_invoke(call, **cast(Any, common))

    class Credential:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    client = _provider_client(monkeypatch)
    object.__setattr__(
        client, "broker", SimpleNamespace(resolve=lambda *_args, **_kwargs: Credential())
    )
    cast(Any, client).governance.require_authorized = lambda *_args, **_kwargs: None
    cast(Any, client).governance.outbound = SimpleNamespace(record=lambda _value: None)
    cast(Any, client).governance.audit = SimpleNamespace(record=lambda **_kwargs: None)
    cast(Any, client).governance.actor_id = "actor"
    approved = SimpleNamespace(
        target="target",
        with_state=lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: {}),
    )

    class Gate:
        def __init__(self, _governance: object) -> None:
            pass

        def prepare(self, _request: object) -> Any:
            return SimpleNamespace(state=OutboundState.APPROVED)

        def apply(self, _request: object, *, authorization: object) -> Any:
            del authorization
            return approved

    monkeypatch.setattr(provider, "ProviderGate", Gate)
    authorization = SimpleNamespace(effect_digest=effect_digest, id="authorization")
    common["authorization"] = authorization
    if multipart:
        object.__setattr__(
            client,
            "multipart_transport",
            SimpleNamespace(post_multipart=lambda *_args: {"ok": True}),
        )
        result = client.invoke_multipart(call, **cast(Any, common))
    else:
        object.__setattr__(
            client,
            "transport",
            SimpleNamespace(post_json=lambda *_args, **_kwargs: {"ok": True}),
        )
        cast(Any, common["gateway_permit"]).transport_provenance = lambda _manifest: object()
        result = client.invoke(call, **cast(Any, common))
    assert result.response == {"ok": True}


def test_provider_last_multipart_and_empty_parts_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _multipart_call()
    manifest = SimpleNamespace(provider_ref=call.provider_ref, payload_digest=call.payload_digest)
    common = {
        "secret_ref": SimpleNamespace(provider=call.provider_ref),
        "authorization": SimpleNamespace(effect_digest=digest("wrong")),
        "consumed_by": "test",
        "manifest": manifest,
        "gateway_permit": _Permit(),
    }
    client = _provider_client(monkeypatch)
    object.__setattr__(client, "multipart_transport", None)
    with pytest.raises(ProbeUnavailable, match="tanimli degil"):
        client.invoke_multipart(call, **cast(Any, common))
    object.__setattr__(client, "multipart_transport", object())
    with pytest.raises(PolicyViolation, match="effect digest"):
        client.invoke_multipart(call, **cast(Any, common))
    with pytest.raises(ValidationFailed, match="metin"):
        provider.openai_chat_text({"choices": [{"message": {"content": [None, {"text": 7}]}}]})


def _git_state(**changes: object) -> doctor.GitRepositoryState:
    values: dict[str, object] = {
        "root": Path("/tmp/repository"),
        "branch": "main",
        "head": "a" * 40,
        "upstream": "origin/main",
        "upstream_ref": "refs/remotes/origin/main",
        "upstream_head": "b" * 40,
        "remote": "origin",
        "remote_branch": "main",
        "ahead": 0,
        "behind": 1,
        "dirty_paths": (),
    }
    values.update(changes)
    return doctor.GitRepositoryState(**cast(Any, values))


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"upstream": None, "upstream_head": None}, "upstream-missing"),
        ({"remote": None, "remote_branch": None}, "upstream-remote-unresolved"),
        ({"dirty_paths": ("file",)}, "worktree-dirty"),
        ({"ahead": 1, "behind": 1}, "branch-diverged"),
        ({"ahead": 1, "behind": 0}, "local-ahead"),
    ),
)
def test_doctor_plan_git_fast_forward_remaining_states(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, object], reason: str
) -> None:
    monkeypatch.setattr(doctor, "observe_git_repository", lambda _root: _git_state(**changes))
    assert reason in doctor.plan_git_fast_forward(Path("/tmp/repository")).blocked_reasons


def test_doctor_observe_and_split_upstream_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    original_split = doctor._split_upstream
    values = {
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("rev-parse", "HEAD"): "a" * 40,
        ("status", "--porcelain=v1", "--untracked-files=all"): "?? new\nshort",
        ("rev-list", "--left-right", "--count", "@{upstream}...HEAD"): "2 3",
    }
    monkeypatch.setattr(doctor, "_git_required", lambda _root, *args: values[args])
    monkeypatch.setattr(
        doctor,
        "_git_optional",
        lambda _root, *args: (
            "refs/remotes/team/origin/main"
            if args[1:] == ("--symbolic-full-name", "@{upstream}")
            else "team/origin/main"
            if "@{upstream}" in args
            else None
        ),
    )
    monkeypatch.setattr(doctor, "_split_upstream", lambda *_args: ("team", "origin/main"))
    state = doctor.observe_git_repository(Path("/tmp/repository"))
    assert (state.behind, state.ahead, state.dirty_paths) == (2, 3, ("new", "rt"))
    monkeypatch.setattr(doctor, "_split_upstream", original_split)
    monkeypatch.setattr(doctor, "_git_required", lambda _root, *_args: "z\norigin\n")
    assert doctor._split_upstream(Path("/tmp"), None) == (None, None)
    assert doctor._split_upstream(Path("/tmp"), "refs/remotes/origin/main") == (
        "origin",
        "main",
    )
    assert doctor._split_upstream(Path("/tmp"), "refs/heads/main") == (None, None)


def test_doctor_build_database_blockers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        "plan_git_fast_forward",
        lambda _root: doctor.GitFastForwardPlan(_git_state(), (), True),
    )
    with pytest.raises(PolicyViolation, match="adapter"):
        doctor.build_doctor_repair_plan(core_path=Path("/tmp"), connection=object())
    migration = SimpleNamespace(head=1, applied=(), pending=(SimpleNamespace(),), drift=(object(),))
    routine = SimpleNamespace(
        missing=(object(),),
        migration_drift=(object(),),
        migration_pending=(object(),),
        repair_plan_digest=digest("routine"),
        migration_head=1,
        as_dict=lambda: {},
    )
    plan = doctor.build_doctor_repair_plan(
        core_path=Path("/tmp"),
        connection=object(),
        migration_status_reader=lambda *_args: migration,
        routine_status_reader=lambda *_args: routine,
    )
    assert plan.migrations is not None and plan.migrations.blocked_reasons == (
        "migration-drift",
        "git-fast-forward-must-run-first",
    )
    assert plan.routines is not None and plan.routines.blocked_reasons == (
        "migration-drift",
        "migration-pending",
        "git-fast-forward-must-run-first",
    )


def test_doctor_apply_fast_forward_failure_and_success_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path("/tmp/repository")
    state = _git_state()
    plan = doctor.GitFastForwardPlan(state, (), True)
    with pytest.raises(PolicyViolation, match="digest"):
        doctor.apply_git_fast_forward(root, plan=plan, plan_digest=digest("wrong"))
    no_change = doctor.GitFastForwardPlan(replace(state, behind=0), (), False)
    assert not doctor.apply_git_fast_forward(
        root, plan=no_change, plan_digest=no_change.plan_digest
    ).changed
    blocked = doctor.GitFastForwardPlan(state, ("dirty",), True)
    with pytest.raises(PolicyViolation, match="bloke"):
        doctor.apply_git_fast_forward(root, plan=blocked, plan_digest=blocked.plan_digest)
    monkeypatch.setattr(
        doctor, "observe_git_repository", lambda _root: replace(state, head="c" * 40)
    )
    with pytest.raises(PolicyViolation, match="stale"):
        doctor.apply_git_fast_forward(root, plan=plan, plan_digest=plan.plan_digest)

    monkeypatch.setattr(doctor, "observe_git_repository", lambda _root: state)
    monkeypatch.setattr(
        doctor,
        "_git_required",
        lambda _root, *args: "" if args[0] == "ls-remote" else state.upstream_head,
    )
    with pytest.raises(ConfigurationError, match="remote branch"):
        doctor.apply_git_fast_forward(root, plan=plan, plan_digest=plan.plan_digest)
    monkeypatch.setattr(
        doctor,
        "_git_required",
        lambda _root, *args: (
            "c" * 40 + "\trefs/heads/main" if args[0] == "ls-remote" else state.upstream_head
        ),
    )
    with pytest.raises(PolicyViolation, match="Remote HEAD"):
        doctor.apply_git_fast_forward(root, plan=plan, plan_digest=plan.plan_digest)


def test_doctor_git_command_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        "_git_completed",
        lambda *_args: subprocess.CompletedProcess([], 1, stdout="", stderr="private"),
    )
    with pytest.raises(ConfigurationError, match="basarisiz"):
        doctor._git_required(Path("/tmp"), "status")
    assert doctor._git_optional(Path("/tmp"), "status") is None


def test_doctor_database_clean_and_fast_forward_terminal_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path("/tmp/repository")
    state = _git_state()
    no_git = doctor.GitFastForwardPlan(replace(state, behind=0), (), False)
    monkeypatch.setattr(doctor, "plan_git_fast_forward", lambda _root: no_git)
    migration = SimpleNamespace(head=1, applied=(), pending=(), drift=())
    routine = SimpleNamespace(
        missing=(),
        migration_drift=(),
        migration_pending=(),
        repair_plan_digest=digest("routine"),
        migration_head=1,
        as_dict=lambda: {},
    )
    plan = doctor.build_doctor_repair_plan(
        core_path=root,
        connection=object(),
        migration_status_reader=lambda *_args: migration,
        routine_status_reader=lambda *_args: routine,
    )
    assert plan.migrations is not None and not plan.migrations.blocked_reasons
    assert plan.routines is not None and not plan.routines.blocked_reasons

    ff = doctor.GitFastForwardPlan(state, (), True)
    observations = iter((state, replace(state, head=state.upstream_head or "", behind=0)))
    monkeypatch.setattr(doctor, "observe_git_repository", lambda _root: next(observations))
    ancestor = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    monkeypatch.setattr(doctor, "_git_completed", lambda *_args: ancestor)
    monkeypatch.setattr(
        doctor,
        "_git_required",
        lambda _root, *args: (
            f"{state.upstream_head}\trefs/heads/main"
            if args[0] == "ls-remote"
            else state.upstream_head or ""
        ),
    )
    result = doctor.apply_git_fast_forward(root, plan=ff, plan_digest=ff.plan_digest)
    assert result.changed and result.new_head == state.upstream_head


@pytest.mark.parametrize("failure", ("ancestor", "fetch", "after"))
def test_doctor_fast_forward_late_fail_closed(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = Path("/tmp/repository")
    state = _git_state()
    plan = doctor.GitFastForwardPlan(state, (), True)
    after = replace(state, head=state.upstream_head or "", behind=0)
    observations = iter((state, state if failure == "after" else after))
    monkeypatch.setattr(doctor, "observe_git_repository", lambda _root: next(observations))
    monkeypatch.setattr(
        doctor,
        "_git_completed",
        lambda *_args: subprocess.CompletedProcess([], 1 if failure == "ancestor" else 0),
    )

    def required(_root: Path, *args: str) -> str:
        if args[0] == "ls-remote":
            return f"{state.upstream_head}\trefs/heads/main"
        if args[0] == "rev-parse" and failure == "fetch":
            return "c" * 40
        return state.upstream_head or ""

    monkeypatch.setattr(doctor, "_git_required", required)
    with pytest.raises((PolicyViolation, ConfigurationError)):
        doctor.apply_git_fast_forward(root, plan=plan, plan_digest=plan.plan_digest)
    monkeypatch.setattr(
        doctor,
        "_git_completed",
        lambda *_args: subprocess.CompletedProcess([], 0, stdout="\n", stderr=""),
    )
    assert doctor._git_optional(Path("/tmp"), "status") is None


def test_fresh_bootstrap_platform_and_config_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    bootstrap._write_json(tmp_path / "value.json", {"x": 1})
    bootstrap._fsync_file(tmp_path / "value.json")
    bootstrap._fsync_directory(tmp_path)
    bootstrap._fsync_tree(tmp_path)
    assert not bootstrap.detect_legacy_postgresql_config(tmp_path / "missing").detected
    config = tmp_path / "config.yaml"
    config.write_text("42\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="mapping"):
        bootstrap.detect_legacy_postgresql_config(tmp_path)
    config.write_text("database:\n  backend: PostgreSQL\n  host: localhost\n", encoding="utf-8")
    found = bootstrap.detect_legacy_postgresql_config(tmp_path)
    assert found.detected and len(found.reasons) == 2


def test_fresh_bootstrap_stage_recovery_filters_and_lock_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = bootstrap_fixture._plan(tmp_path)
    parent = plan.home.parent
    invalid = parent / f".{plan.home.name}.bootstrap-invalid"
    invalid.mkdir()
    (invalid / ".bootstrap-stage.json").write_text("not-json", encoding="utf-8")
    wrong = parent / f".{plan.home.name}.bootstrap-wrong"
    wrong.mkdir()
    (wrong / ".bootstrap-stage.json").write_text(
        json.dumps(
            {"schema": "wrong", "home_name": plan.home.name, "plan_digest": plan.plan_digest}
        ),
        encoding="utf-8",
    )
    malformed = parent / f".{plan.home.name}.bootstrap-malformed"
    malformed.mkdir()
    (malformed / ".bootstrap-stage.json").write_text(
        json.dumps(
            {
                "schema": bootstrap.STAGE_MARKER_SCHEMA,
                "home_name": plan.home.name,
                "plan_digest": "bad",
            }
        ),
        encoding="utf-8",
    )
    assert bootstrap._recover_stale_stages(plan) == ()

    lock = tmp_path / "lock"
    bodies: tuple[object, ...] = (
        [],
        {"schema": "wrong", "home_name": plan.home.name, "plan_digest": plan.plan_digest, "pid": 1},
        {
            "schema": bootstrap.LOCK_SCHEMA,
            "home_name": plan.home.name,
            "plan_digest": plan.plan_digest,
            "pid": 0,
        },
        {"schema": bootstrap.LOCK_SCHEMA, "home_name": plan.home.name, "plan_digest": 7, "pid": 1},
    )
    for body in bodies:
        lock.write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(ConfigurationError):
            bootstrap._lock_is_stale(lock, plan)
    lock.write_text(
        json.dumps(
            {
                "schema": bootstrap.LOCK_SCHEMA,
                "home_name": plan.home.name,
                "plan_digest": plan.plan_digest,
                "pid": 99999999,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(os, "kill", lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    assert bootstrap._lock_is_stale(lock, plan)
    monkeypatch.setattr(os, "kill", lambda *_args: (_ for _ in ()).throw(PermissionError()))
    assert not bootstrap._lock_is_stale(lock, plan)


def test_fresh_receipt_invariants_and_plan_drift(tmp_path: Path) -> None:
    plan = bootstrap_fixture._plan(tmp_path)
    bootstrap.apply_fresh_bootstrap(plan, schema=SQLiteOperationalSchema())
    path = plan.home / bootstrap.RECEIPT_RELATIVE_PATH
    receipt = bootstrap.validate_bootstrap_receipt(path, schema=SQLiteOperationalSchema())
    for field, value in (
        ("status", "wrong"),
        ("operational_schema_version", True),
        ("initial_operational_rows", True),
        ("network_calls", True),
    ):
        changed = dict(receipt)
        changed[field] = value
        body = {key: item for key, item in changed.items() if key != "receipt_digest"}
        changed["receipt_digest"] = bootstrap._canonical_digest(body)
        path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ConfigurationError, match="invariant"):
            bootstrap.validate_bootstrap_receipt(path, schema=SQLiteOperationalSchema())
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="authority drift"):
        bootstrap.apply_fresh_bootstrap(
            replace(plan, authority_digest=digest("other")),
            schema=SQLiteOperationalSchema(),
        )


def test_fresh_receipt_shape_digest_and_content_drift_edges(tmp_path: Path) -> None:
    plan = bootstrap_fixture._plan(tmp_path)
    bootstrap.apply_fresh_bootstrap(plan, schema=SQLiteOperationalSchema())
    path = plan.home / bootstrap.RECEIPT_RELATIVE_PATH
    original = bootstrap.validate_bootstrap_receipt(path, schema=SQLiteOperationalSchema())
    path.write_text("42\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="nesne"):
        bootstrap.validate_bootstrap_receipt(path, schema=SQLiteOperationalSchema())
    for mutation, message in (
        ({"receipt_digest": 7}, "digest tipi"),
        ({"receipt_digest": digest("wrong")}, "digest drift"),
    ):
        changed = dict(original) | mutation
        path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ConfigurationError, match=message):
            bootstrap.validate_bootstrap_receipt(path, schema=SQLiteOperationalSchema())
    changed = dict(original)
    changed["plan_digest"] = 7
    changed["receipt_digest"] = bootstrap._canonical_digest(
        {key: value for key, value in changed.items() if key != "receipt_digest"}
    )
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="tipi"):
        bootstrap.validate_bootstrap_receipt(path, schema=SQLiteOperationalSchema())
    path.write_text(json.dumps(original), encoding="utf-8")
    (plan.home / "config.yaml").write_text("runtime: {}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="config digest drift"):
        bootstrap.plan_fresh_bootstrap(
            home=plan.home,
            core_root=plan.core_root,
            authority_digest=plan.authority_digest,
            schema=SQLiteOperationalSchema(),
        )


class _Kernel:
    def __init__(
        self, *, create: int = 1, configure: int = 1, assign: int = 1, check: int = 1
    ) -> None:
        self.create = create
        self.configure = configure
        self.assign = assign
        self.check = check
        self.closed: list[object] = []

    def CreateJobObjectW(self, *_args: object) -> int:
        return self.create

    def SetInformationJobObject(self, *_args: object) -> int:
        return self.configure

    def CloseHandle(self, value: object) -> int:
        self.closed.append(value)
        return 1

    def AssignProcessToJobObject(self, *_args: object) -> int:
        return self.assign

    def IsProcessInJob(self, _process: object, _job: object, target: object) -> int:
        if self.check:
            cast(Any, target)._obj.value = 1
        return self.check


def test_capability_worker_windows_job_and_assignment_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = worker._WindowsJob(None)
    empty.close()
    kernel = _Kernel()
    monkeypatch.setattr(worker, "_windows_kernel32", lambda: kernel)
    owned = worker._WindowsJob(7)
    owned.close()
    assert owned.handle is None and kernel.closed
    monkeypatch.setattr(worker, "_windows_kernel32", lambda: _Kernel(create=0))
    with pytest.raises(PolicyViolation, match="olusturulamadi"):
        worker._create_windows_job()
    failed = _Kernel(configure=0)
    monkeypatch.setattr(worker, "_windows_kernel32", lambda: failed)
    with pytest.raises(PolicyViolation, match="yapilandirilamadi"):
        worker._create_windows_job()
    with pytest.raises(PolicyViolation, match="kapali"):
        worker._assign_windows_job(worker._WindowsJob(None), cast(Any, SimpleNamespace()))
    for kernel in (_Kernel(assign=0), _Kernel(check=0)):
        monkeypatch.setattr(worker, "_windows_kernel32", lambda kernel=kernel: kernel)
        with pytest.raises(PolicyViolation):
            worker._assign_windows_job(worker._WindowsJob(1), cast(Any, SimpleNamespace(_handle=2)))


class _Process:
    def __init__(self, code: int | None = None) -> None:
        self.code = code
        self.pid = 123
        self.stdin = SimpleNamespace(close=lambda: None)
        self.stdout = SimpleNamespace(close=lambda: None)
        self.killed = False

    def poll(self) -> int | None:
        return self.code

    def kill(self) -> None:
        self.killed = True
        self.code = -9

    def wait(self, *, timeout: float) -> int:
        assert timeout == 5
        if self.code is None:
            raise subprocess.TimeoutExpired("worker", timeout)
        return self.code


def test_capability_worker_finish_and_windows_kill_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process()
    tree = worker._ProcessTree(cast(Any, process), worker._WindowsJob(1))
    monkeypatch.setattr(worker._WindowsJob, "close", lambda self: setattr(self, "handle", None))
    joined: list[float] = []
    thread = SimpleNamespace(join=lambda *, timeout: joined.append(timeout))
    worker.CapabilityProcessWorker._finish_pipes(tree, cast(Any, thread))
    assert process.killed and joined == [5]
    live = _Process()
    tree2 = worker._ProcessTree(cast(Any, live), worker._WindowsJob(1))
    worker.CapabilityProcessWorker._hard_kill_tree(tree2)
    assert live.killed


def test_capability_worker_start_and_assignment_success_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = worker.CapabilityWorkerSpec(("worker",), tmp_path, 1)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no process")),
    )
    with pytest.raises(PolicyViolation, match="baslatilamadi"):
        worker.CapabilityProcessWorker._start(spec)

    kernel = _Kernel()
    monkeypatch.setattr(worker, "_windows_kernel32", lambda: kernel)
    job = worker._create_windows_job()
    assert job.handle == 1
    worker._assign_windows_job(job, cast(Any, SimpleNamespace(_handle=2)))

    fake = _Process(code=0)
    monkeypatch.setattr(worker, "_worker_env", lambda: {})
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 1, raising=False)
    monkeypatch.setattr(worker, "_create_windows_job", lambda: worker._WindowsJob(1))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr(worker, "_assign_windows_job", lambda *_args: None)
    tree = worker.CapabilityProcessWorker._start(spec)
    assert tree.windows_job is not None


def test_capability_worker_finish_without_optional_resources_and_windows_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(code=0)
    cast(Any, process).stdin = None
    cast(Any, process).stdout = None
    joined: list[int] = []
    worker.CapabilityProcessWorker._finish_pipes(
        worker._ProcessTree(cast(Any, process)),
        cast(Any, SimpleNamespace(join=lambda *, timeout: joined.append(timeout))),
    )
    assert joined == [5]
    live = _Process()
    monkeypatch.setattr(os, "name", "nt")
    worker.CapabilityProcessWorker._hard_kill_tree(worker._ProcessTree(cast(Any, live)))
    assert live.killed


def test_capability_worker_oversize_child_and_main_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incoming = SimpleNamespace(buffer=SimpleNamespace(readline=lambda _limit: b"x" * 16_777_217))
    monkeypatch.setattr(sys, "stdin", cast(Any, incoming))
    assert worker._provider_child() == 2
    with pytest.raises(SystemExit) as caught:
        runpy.run_module(
            "zekam.infrastructure.process.capability_worker",
            run_name="__main__",
            alter_sys=True,
        )
    assert caught.value.code == 2


def _invoke_provider_child(monkeypatch: pytest.MonkeyPatch, message: object) -> int:
    raw = json.dumps(message).encode("utf-8") + b"\n"
    incoming = SimpleNamespace(buffer=SimpleNamespace(readline=lambda _limit: raw))
    outgoing = SimpleNamespace(
        buffer=SimpleNamespace(write=lambda _value: None, flush=lambda: None)
    )
    monkeypatch.setattr(sys, "stdin", cast(Any, incoming))
    monkeypatch.setattr(sys, "stdout", cast(Any, outgoing))
    return worker._provider_child()


def test_capability_worker_last_reader_start_kill_and_child_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunks = iter((b"x", b""))
    monkeypatch.setattr(os, "read", lambda *_args: next(chunks))
    reader = worker._BoundedReader(cast(Any, SimpleNamespace(fileno=lambda: 1)), 3)
    reader.buffer.extend(b"xxxx")
    reader.read()
    assert reader.overflow.is_set()

    spec = worker.CapabilityWorkerSpec(("worker",), tmp_path, 1)
    monkeypatch.setattr(worker, "_worker_env", lambda: {})
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 1, raising=False)
    closed: list[bool] = []
    monkeypatch.setattr(
        worker,
        "_create_windows_job",
        lambda: cast(Any, SimpleNamespace(close=lambda: closed.append(True))),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no process")),
    )
    with pytest.raises(PolicyViolation):
        worker.CapabilityProcessWorker._start(spec)
    assert closed == [True]

    live = _Process()
    job = cast(Any, SimpleNamespace(close=lambda: setattr(live, "code", 0)))
    worker.CapabilityProcessWorker._hard_kill_tree(worker._ProcessTree(cast(Any, live), job))
    assert not live.killed

    base = {
        "schema": worker.CAPABILITY_WORKER_SCHEMA,
        "type": "execute",
        "request_id": "request",
        "payload": {
            "operation": "provider-post-json",
            "endpoint": "http://127.0.0.1:8000/v1",
            "payload": {},
            "credential": "secret",
            "timeout_seconds": 1,
            "max_response_bytes": 10,
            "manifest_digest": 7,
            "gateway_attempt_id": "bad",
            "gateway_claim_id": "bad",
        },
    }
    assert _invoke_provider_child(monkeypatch, base) == 2
    changed = cast(dict[str, Any], base["payload"])
    changed["manifest_digest"] = digest("manifest")
    assert _invoke_provider_child(monkeypatch, base) == 2

    monkeypatch.setattr(sys, "argv", ["worker", "--provider-child"])
    incoming = SimpleNamespace(buffer=SimpleNamespace(readline=lambda _limit: b"x" * 16_777_217))
    monkeypatch.setattr(sys, "stdin", cast(Any, incoming))
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("zekam.infrastructure.process.capability_worker", run_name="__main__")
    assert caught.value.code == 2


def _reseal(packet: recipe.RecipeContextPacket, **changes: object) -> recipe.RecipeContextPacket:
    provisional = replace(packet, **cast(Any, changes))
    return replace(
        provisional,
        issuance_seal=recipe.ContextRecipeRegistry._seal_packet_body(provisional.body()),
    )


def test_context_packet_constructor_and_validation_remaining_edges() -> None:
    packet = recipe_fixture._compile_recipe(recipe.ContextRecipeRole.COORDINATOR)
    with pytest.raises(ValidationFailed, match="ordinal"):
        replace(packet, loop_attempt_ordinal=0)
    with pytest.raises(PolicyViolation, match="Ilk attempt"):
        replace(packet, loop_progress_packet_digest=digest("progress"))
    with pytest.raises(ValidationFailed, match="budget"):
        replace(packet, requested_token_budget=0)
    with pytest.raises(PolicyViolation, match="authority"):
        replace(packet, grants_authority=True)
    with pytest.raises(PolicyViolation, match="binding"):
        replace(packet, recipe_id="wrong")

    registry = recipe.ContextRecipeRegistry()
    with pytest.raises(PolicyViolation, match="provenance"):
        registry.validate_packet(replace(packet, issuance_seal=digest("wrong")), packet.role)
    with pytest.raises(PolicyViolation, match="cross-role"):
        registry.validate_packet(packet, recipe.ContextRecipeRole.RESEARCHER)
    stale_manifest = replace(packet.manifest, scoring_policy_digest=digest("stale"))
    with pytest.raises(PolicyViolation, match="scoring"):
        registry.validate_packet(_reseal(packet, manifest=stale_manifest), packet.role)
    omitted = replace(packet, recipe_excluded=(*packet.recipe_excluded, "extra"))
    with pytest.raises(PolicyViolation, match="partition"):
        registry.validate_packet(_reseal(omitted), packet.role)

    with pytest.raises(PolicyViolation, match="builder"):
        replace(
            packet,
            role=recipe.ContextRecipeRole.RESEARCHER,
            loop_attempt_ordinal=2,
            loop_progress_packet_digest=digest("progress"),
        )
    builder = recipe_fixture._compile_recipe(recipe.ContextRecipeRole.BUILDER)
    with pytest.raises(PolicyViolation, match="digest ister"):
        replace(builder, loop_attempt_ordinal=2)


def test_context_validate_selected_semantic_matrix() -> None:
    registry = recipe.ContextRecipeRegistry()
    packet = recipe_fixture._compile_recipe(recipe.ContextRecipeRole.COORDINATOR)
    selected = packet.manifest.selected
    duplicate_manifest = replace(packet.manifest, selected=(*selected, selected[0]))
    with pytest.raises(PolicyViolation, match="tekil"):
        registry.validate_packet(_reseal(packet, manifest=duplicate_manifest), packet.role)
    forbidden = replace(selected[0], kind=ContextCandidateKind.SOURCE_SLICE)
    forbidden_manifest = replace(packet.manifest, selected=(forbidden, *selected[1:]))
    with pytest.raises(PolicyViolation, match="forbidden"):
        registry.validate_packet(_reseal(packet, manifest=forbidden_manifest), packet.role)
    forged = replace(selected[0], reason="wrong")
    forged_manifest = replace(packet.manifest, selected=(forged, *selected[1:]))
    with pytest.raises(PolicyViolation, match="provenance"):
        registry.validate_packet(_reseal(packet, manifest=forged_manifest), packet.role)


def test_context_compile_remaining_attempt_and_duplicate_edges() -> None:
    registry = recipe.ContextRecipeRegistry()
    builder = recipe_fixture._compile_recipe(recipe.ContextRecipeRole.BUILDER)
    with pytest.raises(ValidationFailed, match="budget"):
        recipe_fixture._compile_recipe(recipe.ContextRecipeRole.BUILDER, token_budget=0)
    snapshot = recipe_fixture._snapshot(recipe.ContextRecipeRole.RESEARCHER)
    candidates = recipe_fixture.ALL_TYPED
    candidate_set = ContextCandidateSetIssuer.issue(
        snapshot, candidates, recipe_fixture._contents(candidates), now=recipe_fixture.NOW
    )
    with pytest.raises(PolicyViolation, match="builder"):
        registry.compile(
            recipe.ContextRecipeRole.RESEARCHER,
            candidate_set,
            token_budget=1000,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=recipe_fixture.NOW,
            ranking_snapshot=snapshot,
            loop_attempt_ordinal=2,
            loop_progress_packet_digest=digest("progress"),
        )
    duplicate = next(
        item for item in recipe_fixture.ALL_TYPED if item.kind is ContextCandidateKind.SOURCE_SLICE
    )
    drifted = replace(
        duplicate,
        candidate_id="source-slice-drift",
        token_count=duplicate.token_count + 1,
    )
    with pytest.raises(PolicyViolation, match="token count drift"):
        recipe_fixture._compile_recipe(
            recipe.ContextRecipeRole.BUILDER,
            (*recipe_fixture.ALL_TYPED, drifted),
        )
    assert builder.loop_attempt_ordinal == 1
