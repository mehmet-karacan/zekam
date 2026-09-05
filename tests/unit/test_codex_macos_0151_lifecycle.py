from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path

import pytest

from zekam.application.context_ranking import count_context_tokens
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_v4_ingress import (
    ManagedInvocationSnapshot,
    ManagedProcessSnapshot,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients import codex_macos_0151_lifecycle as adapter
from zekam.infrastructure.clients.codex_macos_0151_lifecycle import (
    CODEX_MACOS_0151_CONTRACT_SCHEMA,
    CODEX_MACOS_0151_NATIVE_SHA256,
    LiveProcessVerificationError,
    TrustedCodex0151ProcessManager,
    handled_failure_output,
    parse_codex_macos_0151,
    success_output,
)

ROOT = Path("/Users/mkaracan/Projeler/akilli-kasa")


def _replace_test_snapshot(
    snapshot: ManagedInvocationSnapshot, **changes: object
) -> ManagedInvocationSnapshot:
    values = {
        name: changes.get(name, getattr(snapshot, name))
        for name in type(snapshot).__dataclass_fields__
    }
    result = object.__new__(type(snapshot))
    for name, value in values.items():
        object.__setattr__(result, name, value)
    result.__post_init__()
    return result


def _payload(**changes: object) -> bytes:
    body: dict[str, object] = {
        "session_id": "codex-session_1",
        "transcript_path": None,
        "cwd": str(ROOT),
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": "gpt-5.6",
        "permission_mode": "default",
    }
    body.update(changes)
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def _additional(text: str = "bounded source evidence") -> str:
    return canonical_json(
        {
            "schema": "zekam-codex-session-start-context/v1",
            "manifest_digest": digest("manifest"),
            "source_snapshot_id": "018f0000-0000-7000-8000-000000000099",
            "source_revision": "a" * 40,
            "fragments": [
                {
                    "candidate_id": "source-health",
                    "kind": "source-slice",
                    "source_ref": "src/akilli_kasa/api/saglik.py",
                    "content_digest": digest(text),
                    "token_count": count_context_tokens(text),
                    "text": text,
                }
            ],
            "provider_called": False,
            "model_summary": False,
            "grants_authority": False,
        },
    )


def _additional_of_size(size: int, *, escaped: bool = False) -> str:
    empty = _additional("")
    baseline = len(empty.encode("utf-8"))
    if not escaped:
        text = "x" * max(1, size - baseline)
        for _attempt in range(16):
            value = _additional(text)
            difference = size - len(value.encode("utf-8"))
            if difference == 0:
                return value
            text = text + "x" * difference if difference > 0 else text[:difference]
    else:
        slash_count = max(1, (size - baseline) // 2)
        suffix = ""
        for _attempt in range(32):
            value = _additional("\\" * slash_count + suffix)
            difference = size - len(value.encode("utf-8"))
            if difference == 0:
                return value
            if difference > 0:
                suffix += "x" * difference
            elif suffix:
                suffix = suffix[:difference]
            else:
                slash_count -= max(1, (-difference + 1) // 2)
    raise AssertionError("could not construct exact additionalContext boundary")


def test_session_start_parser_is_content_free_and_exact() -> None:
    event = parse_codex_macos_0151(_payload(), expected_root=ROOT)
    assert event.external_session_id == "codex-session_1"
    assert event.event_type == "SessionStart"
    assert event.internal_event_type == "SESSION_START"
    assert not hasattr(event, "transcript_path")
    assert event.observation_body() == {
        "schema": CODEX_MACOS_0151_CONTRACT_SCHEMA,
        "client_id": "codex",
        "client_kind": "codex",
        "client_version": "0.151.0",
        "session_id": "codex-session_1",
        "external_event_type": "SessionStart",
        "internal_event_type": "SESSION_START",
        "turn_id": None,
        "source": "startup",
        "trigger": None,
        "reason": None,
        "stop_hook_active": False,
        "permission_mode": "default",
        "wire_digest": event.wire_digest,
        "contains_prompt": False,
        "contains_response": False,
        "contains_transcript": False,
        "grants_authority": False,
    }
    assert "transcript_path" not in event.observation_body()
    assert str(ROOT) not in json.dumps(event.observation_body())


@pytest.mark.parametrize(
    "value",
    [None, "", ".", "../bad", "/tmp/foreign", 1, [], {}],
)
def test_cwd_must_be_exact_bound_root(value: object) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        parse_codex_macos_0151(_payload(cwd=value), expected_root=ROOT)


@pytest.mark.parametrize(
    "value",
    ["", "x\x00y", 7, [], {}, "x" * 4097],
)
def test_transcript_path_required_null_or_bounded_string(value: object) -> None:
    with pytest.raises(ValidationFailed):
        parse_codex_macos_0151(_payload(transcript_path=value), expected_root=ROOT)


def test_missing_transcript_path_is_rejected() -> None:
    body = json.loads(_payload())
    body.pop("transcript_path")
    with pytest.raises(ValidationFailed):
        parse_codex_macos_0151(json.dumps(body).encode(), expected_root=ROOT)


@pytest.mark.parametrize("event", ["Stop", "SessionEnd", "PreToolUse", "sessionstart"])
def test_unreviewed_events_are_rejected(event: str) -> None:
    with pytest.raises(PolicyViolation):
        parse_codex_macos_0151(_payload(hook_event_name=event, source=None), expected_root=ROOT)


@pytest.mark.parametrize("source", [None, "resume", "clear", "compact", "other", 1])
def test_only_initial_startup_source_is_accepted(source: object) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        parse_codex_macos_0151(_payload(source=source), expected_root=ROOT)


@pytest.mark.parametrize(
    ("event", "internal"),
    [("PreCompact", "PRE_COMPACTION"), ("PostCompact", "POST_COMPACTION")],
)
def test_compaction_parser_contract_is_structural_only(event: str, internal: str) -> None:
    body = json.loads(
        _payload(
            hook_event_name=event,
            source=None,
            turn_id="turn-1",
            trigger="manual",
            permission_mode=None,
        )
    )
    body.pop("source")
    body.pop("permission_mode")
    parsed = parse_codex_macos_0151(
        json.dumps(body, separators=(",", ":")).encode(),
        expected_root=ROOT,
    )
    assert parsed.internal_event_type == internal


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"[]",
        b'{"session_id":NaN}',
        b'{"session_id":"a","session_id":"b"}',
        b'{"session_id":"\xff"}',
        b"{} trailing",
        b"{" + b'"x":' * 14 + b"0" + b"}" * 14,
        b" " * 65537,
    ],
)
def test_raw_parser_rejects_malformed_duplicate_nonfinite_deep_and_oversize(
    payload: bytes,
) -> None:
    with pytest.raises(ValidationFailed):
        parse_codex_macos_0151(payload, expected_root=ROOT)


@pytest.mark.parametrize("control", ["\x7f", "\x80", "\x9f"])
def test_parser_rejects_del_and_c1_controls(control: str) -> None:
    with pytest.raises(ValidationFailed):
        parse_codex_macos_0151(_payload(model=f"model{control}value"), expected_root=ROOT)


def test_wrong_input_types_and_client_pin_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationFailed):
        parse_codex_macos_0151("{}", expected_root=ROOT)  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation):
        parse_codex_macos_0151(_payload(), expected_root=ROOT, client_version="0.150.1")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    with pytest.raises(PolicyViolation):
        parse_codex_macos_0151(_payload(), expected_root=ROOT)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    with pytest.raises(PolicyViolation):
        parse_codex_macos_0151(_payload(), expected_root=ROOT)


def test_unknown_extra_and_wrong_field_types_are_rejected() -> None:
    with pytest.raises(ValidationFailed):
        parse_codex_macos_0151(_payload(extra=True), expected_root=ROOT)
    changeset: tuple[dict[str, object], ...] = (
        {"session_id": None},
        {"model": []},
        {"permission_mode": "superuser"},
        {"turn_id": "unexpected"},
    )
    for changes in changeset:
        with pytest.raises((ValidationFailed, PolicyViolation)):
            parse_codex_macos_0151(_payload(**changes), expected_root=ROOT)


def test_exact_success_and_handled_failure_stdout_contracts() -> None:
    additional = _additional()
    assert json.loads(success_output(additional))["hookSpecificOutput"] == {
        "additionalContext": additional,
        "hookEventName": "SessionStart",
    }
    assert handled_failure_output(recovery_required=False) == (
        b'{"continue":false,"stopReason":"ZEKAM_SESSION_START_REJECTED"}\n'
    )
    assert handled_failure_output(recovery_required=True) == (
        b'{"continue":false,"stopReason":"ZEKAM_SESSION_START_RECOVERY_REQUIRED"}\n'
    )
    for output in (
        success_output(additional),
        handled_failure_output(recovery_required=False),
        handled_failure_output(recovery_required=True),
    ):
        parsed = json.loads(output)
        assert type(parsed) is dict


def test_output_helpers_reject_subclasses_surrogates_and_noncanonical_context() -> None:
    class Text(str):
        pass

    for value in (
        Text(_additional()),
        "\ud800",
        " not-canonical ",
        "[]",
        "{}",
        '{"grants_authority":true,"schema":"zekam-codex-session-start-context/v1"}',
    ):
        with pytest.raises(ValidationFailed):
            success_output(value)
    with pytest.raises(ValidationFailed):
        handled_failure_output(recovery_required=1)  # type: ignore[arg-type]
    for key, value in (
        ("source_revision", "g" * 40),
        ("source_revision", "a" * 39),
    ):
        document = json.loads(_additional())
        document[key] = value
        with pytest.raises(ValidationFailed):
            success_output(canonical_json(document))
    for source_ref in ("/tmp/secret", "../secret", "src/../secret", "./source", "x\\y"):
        document = json.loads(_additional())
        document["fragments"][0]["source_ref"] = source_ref
        with pytest.raises(ValidationFailed):
            success_output(canonical_json(document))


def test_output_inner_and_final_byte_boundaries_are_exact() -> None:
    inner = _additional_of_size(16_384)
    assert len(success_output(inner)) <= 32_847
    with pytest.raises(ValidationFailed):
        success_output(_additional_of_size(16_385))
    escaped = _additional_of_size(16_384, escaped=True)
    assert len(escaped.encode("utf-8")) == 16_384
    assert len(success_output(escaped)) <= 32_847


def test_transient_process_diagnostics_are_bounded_sorted_and_never_raw() -> None:
    error = LiveProcessVerificationError(("hook-parent", "native-artifact"))
    assert error.codes == ("hook-parent", "native-artifact")
    assert "hook-parent" not in str(error)
    for codes in ((), ("unknown",), ("native-pid", "native-pid"), ("native-uid", "native-pid")):
        with pytest.raises(ValidationFailed):
            LiveProcessVerificationError(codes)


def test_concrete_manager_uses_live_process_and_fixed_artifact_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = f"sha256:{CODEX_MACOS_0151_NATIVE_SHA256}"
    file_digests = {
        adapter._NATIVE_PATH: native,
        adapter._SHELL_PATH: digest("shell"),
        adapter._LAUNCHER_PATH: digest("launcher"),
        adapter._RUNTIME_PATH: digest("runtime"),
    }
    monkeypatch.setattr(os, "getppid", lambda: 101)
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    monkeypatch.setattr(
        adapter,
        "_process_row",
        lambda pid: (1, 501, "stable-start", adapter._NATIVE_PATH.resolve(strict=True)),
    )
    monkeypatch.setattr(adapter, "_raw_file_digest", lambda path: file_digests[path])
    monkeypatch.setattr(adapter, "_utc_second", lambda: "2026-09-03T12:00:00+00:00")
    binding = ContinuityBinding(
        "018f0000-0000-7000-8000-000000000201",
        "external",
        "018f0000-0000-7000-8000-000000000202",
        "018f0000-0000-7000-8000-000000000203",
        "codex",
        "macbook",
        "018f0000-0000-7000-8000-000000000204",
        digest("task"),
        digest("plan"),
        digest("policy"),
    )
    manager = TrustedCodex0151ProcessManager()
    receipt = manager.capture_process(binding)
    assert receipt.native_pid == 101
    assert receipt.native_artifact_digest == native
    assert tuple(command.external_event_type for command in receipt.reviewed_commands) == (
        "SessionStart",
        "PreCompact",
        "PostCompact",
    )
    manager.assert_process(receipt)
    monkeypatch.setattr(
        adapter,
        "_process_row",
        lambda pid: (1, 501, "changed-start", adapter._NATIVE_PATH.resolve(strict=True)),
    )
    with pytest.raises(LiveProcessVerificationError):
        manager.assert_process(receipt)


def test_application_callers_cannot_construct_managed_receipts() -> None:
    assert not hasattr(adapter, "_from_live_capture")
    with pytest.raises(PolicyViolation, match="concrete-adapter"):
        ManagedProcessSnapshot()
    with pytest.raises(PolicyViolation, match="concrete-adapter"):
        ManagedInvocationSnapshot()


def test_concrete_manager_rejects_invocation_pid_uid_parent_start_and_artifact_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_path = adapter._NATIVE_PATH.resolve(strict=True)
    runtime_path = adapter._RUNTIME_PATH
    file_digests = {
        adapter._NATIVE_PATH: f"sha256:{CODEX_MACOS_0151_NATIVE_SHA256}",
        adapter._SHELL_PATH: digest("shell"),
        adapter._LAUNCHER_PATH: digest("launcher"),
        runtime_path: digest("runtime"),
    }
    rows = {
        101: (1, 501, "native-start", native_path),
        202: (101, 501, "hook-start", runtime_path),
    }
    monkeypatch.setattr(os, "getppid", lambda: 101)
    monkeypatch.setattr(os, "getpid", lambda: 202)
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    monkeypatch.setattr(adapter, "_process_row", lambda pid: rows[pid])
    monkeypatch.setattr(adapter, "_raw_file_digest", lambda path: file_digests[path])
    monkeypatch.setattr(adapter, "_utc_second", lambda: "2026-09-03T12:00:00+00:00")
    binding = ContinuityBinding(
        "018f0000-0000-7000-8000-000000000211",
        "external",
        "018f0000-0000-7000-8000-000000000212",
        "018f0000-0000-7000-8000-000000000213",
        "codex",
        "macbook",
        "018f0000-0000-7000-8000-000000000214",
        digest("task"),
        digest("plan"),
        digest("policy"),
    )
    manager = TrustedCodex0151ProcessManager()
    monkeypatch.setattr(adapter, "_utc_second", lambda: "2026-09-03T12:00:30+00:00")
    process = manager.capture_process(binding)
    generation = manager._generation(process)
    managed = digest(
        {
            "ancestry_policy_digest": process.ancestry_policy_digest,
            "attachment_id": process.attachment_id,
            "created_at": process.captured_at,
            "hook_set_digest": process.hook_set_digest,
            "native_artifact_digest": process.native_artifact_digest,
            "native_pid": process.native_pid,
            "native_start_token": process.native_start_token,
            "native_uid": process.native_uid,
            "predecessor_process_generation_digest": None,
            "transition_kind": "initial-attach",
        }
    )
    command = process.reviewed_commands[0]
    with pytest.raises(LiveProcessVerificationError, match="manager verification"):
        manager.capture_invocation(
            binding,
            {"wire_digest": digest("wire")},
            digest("spool"),
            "2026-09-03T12:00:00+00:00",
            digest("caller-selected-generation"),
            process.captured_at,
            managed,
            command,
            process.ancestry_policy_digest,
        )
    rogue_managed = digest("caller-selected-managed-receipt")
    rogue_generation = digest(
        {
            "ancestry_policy_digest": process.ancestry_policy_digest,
            "attachment_id": process.attachment_id,
            "created_at": process.captured_at,
            "generation": 1,
            "hook_set_digest": process.hook_set_digest,
            "managed_launch_receipt_digest": rogue_managed,
            "native_artifact_digest": process.native_artifact_digest,
            "native_pid": process.native_pid,
            "native_start_token": process.native_start_token,
            "native_uid": process.native_uid,
            "previous_process_generation_digest": None,
        }
    )
    with pytest.raises(LiveProcessVerificationError, match="manager verification"):
        manager.capture_invocation(
            binding,
            {"wire_digest": digest("wire")},
            digest("spool"),
            "2026-09-03T12:00:00+00:00",
            rogue_generation,
            process.captured_at,
            rogue_managed,
            command,
            process.ancestry_policy_digest,
        )
    invocation = manager.capture_invocation(
        binding,
        {"wire_digest": digest("wire")},
        digest("spool"),
        "2026-09-03T12:00:00+00:00",
        generation,
        process.captured_at,
        managed,
        command,
        process.ancestry_policy_digest,
    )
    manager.assert_invocation(invocation)
    mutations = (
        _replace_test_snapshot(invocation, native_uid=502, hook_uid=502),
        _replace_test_snapshot(invocation, hook_pid=os.getpid() + 10_000),
        _replace_test_snapshot(invocation, native_start_token="wrong-start"),
        _replace_test_snapshot(invocation, hook_start_token="wrong-start"),
        _replace_test_snapshot(invocation, shell_artifact_digest=digest("wrong-shell")),
        _replace_test_snapshot(
            invocation, python_launcher_artifact_digest=digest("wrong-launcher")
        ),
        _replace_test_snapshot(invocation, python_runtime_artifact_digest=digest("wrong-runtime")),
    )
    for mutated in mutations:
        with pytest.raises((LiveProcessVerificationError, KeyError)):
            manager.assert_invocation(mutated)

    rows[202] = (999, 501, "hook-start", runtime_path)
    with pytest.raises(LiveProcessVerificationError):
        manager.assert_invocation(invocation)
    rows[202] = (101, 502, "hook-start", runtime_path)
    with pytest.raises(LiveProcessVerificationError):
        manager.assert_invocation(invocation)


def test_actual_pinned_user_codex_0151_binary_matches_frozen_digest() -> None:
    manager = TrustedCodex0151ProcessManager()
    native, shell, launcher, runtime = manager._artifacts()
    assert native == f"sha256:{CODEX_MACOS_0151_NATIVE_SHA256}"
    assert len({native, shell, launcher, runtime}) == 4
    assert (
        Path.home()
        / ".local/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64"
        / "vendor/aarch64-apple-darwin/bin/codex"
    ) == adapter._NATIVE_PATH
    assert Path("/Applications/ChatGPT.app/Contents/Resources/codex") != adapter._NATIVE_PATH
    assert (
        Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)
        == adapter._LAUNCHER_PATH
    )
    assert (
        adapter._LAUNCHER_PATH.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
    ).resolve(strict=True) == adapter._RUNTIME_PATH
    assert adapter._LAUNCHER_PATH != adapter._RUNTIME_PATH
