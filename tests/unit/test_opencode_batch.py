"""Native protocol fixtures; no model/provider or executable is called."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.agent_dispatch import _issue_canonical_dispatch_permit
from zekam.domain.canonical import canonical_bytes, digest_of_bytes
from zekam.domain.clients import DispatchRequest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.sandbox import ProcessResult
from zekam.infrastructure.clients import opencode_batch as subject
from zekam.infrastructure.process.runner import ProcessOutput
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore


def _events(document=None):
    document = document or {
        "result": {"agent_id": "model-invented", "summary": "ready"},
        "body": {},
    }

    def event(kind, **part):
        return {
            "type": kind,
            "sessionID": "ses_fixture",
            "part": {"sessionID": "ses_fixture", "messageID": "msg_fixture", **part},
        }

    return [
        event("step_start", id="prt_start"),
        event("text", id="prt_text", text=json.dumps(document)),
        event("step_finish", id="prt_finish", reason="stop"),
    ]


def _stream(events):
    return b"\n".join(canonical_bytes(event) for event in events)


def _text_only_events(document=None):
    document = document or {
        "result": {"agent_id": "model-invented", "summary": "ready"},
        "body": {},
    }
    return [
        {
            "type": "text",
            "sessionID": "ses_text_only",
            "part": {
                "type": "text",
                "sessionID": "ses_text_only",
                "messageID": "msg_text_only",
                "id": "prt_text_only",
                "text": json.dumps(document),
            },
        }
    ]


def test_observed_native_identity_replaces_model_identity_and_reasoning_not_persisted():
    events = _events()
    events.insert(
        1,
        {
            "type": "reasoning",
            "sessionID": "ses_fixture",
            "part": {
                "sessionID": "ses_fixture",
                "messageID": "msg_fixture",
                "text": "not persisted",
            },
        },
    )
    document = subject.parse_batch_events(_stream(events))
    assert document["result"]["agent_id"] == "opencode:ses_fixture:msg_fixture"
    assert b"not persisted" not in canonical_bytes(document)


def test_current_opencode_text_only_stream_is_accepted():
    document = subject.parse_batch_events(_stream(_text_only_events()))
    assert document["result"]["agent_id"] == "opencode:ses_text_only:msg_text_only"


def test_text_only_stream_rejects_part_or_message_drift():
    events = _text_only_events()
    events.append({**events[0], "part": {**events[0]["part"], "messageID": "other"}})
    with pytest.raises(ValidationFailed):
        subject.parse_batch_events(_stream(events))


@pytest.mark.parametrize(
    "mutation",
    [
        "partial",
        "tool",
        "error",
        "session",
        "part-session",
        "message",
        "finish",
        "tail",
        "multiple",
    ],
)
def test_incomplete_or_foreign_or_effectful_events_rejected(mutation):
    events = _events()
    if mutation == "partial":
        events.pop()
    elif mutation in {"tool", "error"}:
        events[1]["type"] = "tool_use" if mutation == "tool" else "error"
    elif mutation == "session":
        events[1]["sessionID"] = "ses_foreign"
    elif mutation == "part-session":
        events[1]["part"]["sessionID"] = "ses_foreign"
    elif mutation == "message":
        events[1]["part"]["messageID"] = "msg_foreign"
    elif mutation == "finish":
        events[-1]["part"]["reason"] = "length"
    elif mutation == "tail":
        events.append(events[1])
    elif mutation == "multiple":
        events.insert(2, {**events[1], "part": {**events[1]["part"], "id": "prt_other"}})
    with pytest.raises(ValidationFailed):
        subject.parse_batch_events(_stream(events))


def test_duplicate_json_and_secret_result_rejected():
    events = _events()
    events[1]["part"]["text"] = '{"result":{},"result":{},"body":{}}'
    with pytest.raises(ValidationFailed):
        subject.parse_batch_events(_stream(events))
    events[1]["part"]["text"] = json.dumps(
        {"result": {}, "body": {"password": "real-literal-value"}}
    )
    with pytest.raises(PolicyViolation):
        subject.parse_batch_events(_stream(events))


def _setup(tmp_path):
    objects = LocalContentAddressedStore(tmp_path / "cas")
    instruction = objects.put(b"Return test patch JSON only.").digest
    context = objects.put(canonical_bytes({"context": {"source": "class Example {}"}})).digest
    request = DispatchRequest(uuid4(), uuid4(), "opencode", "builder", instruction, context, 60)
    adapter = subject.opencode_batch_adapter(
        str(tmp_path / "opencode.exe"),
        objects=objects,
        model_id="provider/model",
        network_authorized=True,
    )
    return (
        adapter,
        request,
        _issue_canonical_dispatch_permit(request.assignment_id, request.invocation_id),
    )


# Diger native OpenCode test modulleri bu ortak fixture'i kullanir. Eski
# ad olan _setup korunurken, public test fixture baglantisi _request ile
# deterministik bir bicimde saglanir.
def _request(tmp_path):
    return _setup(tmp_path)


def _output(spec, stdout, *, exit_code=0):
    return ProcessOutput(
        ProcessResult(
            spec.spec_digest,
            exit_code,
            1,
            digest_of_bytes(stdout),
            digest_of_bytes(b""),
            False,
            False,
        ),
        stdout,
        b"",
    )


def test_dispatch_uses_native_run_cas_stdin_private_profile_and_artifact(tmp_path, monkeypatch):
    adapter, request, permit = _setup(tmp_path)
    calls = []
    monkeypatch.setattr(subject, "resolve_opencode_executable", lambda value: value)

    def run(spec, *, cwd, **kwargs):
        calls.append(spec)
        if spec.argv[-1] == "--version":
            return _output(spec, b"2.0.29\n")
        assert spec.argv[1:3] == ("run", "--standalone")
        assert "--auto" not in spec.argv and "--assignment-id" not in spec.argv
        assert "class Example" not in " ".join(spec.argv)
        assert json.loads(kwargs["stdin"])["context"]["context"]["source"] == "class Example {}"
        assert spec.stdin_digest == digest_of_bytes(kwargs["stdin"])
        env = dict(spec.env)
        assert env["OPENCODE_CONFIG_PROJECT_DISABLE"] == "1"
        assert not Path(env["OPENCODE_CONFIG_DIR"]).is_relative_to(tmp_path)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert config["permissions"] == [{"action": "*", "resource": "*", "effect": "deny"}]
        assert config["agents"]["zekam-test-batch"]["permissions"] == config["permissions"]
        assert config["experimental"]["policies"][0] == {
            "action": "tool.use",
            "resource": "*",
            "effect": "deny",
        }
        return _output(spec, _stream(_events()))

    monkeypatch.setattr(subject.runner, "run", run)
    result = adapter.dispatch(request, cwd=tmp_path, permit=permit)
    assert result.is_success and len(calls) == 2
    document = json.loads(adapter.objects.get(result.payload["artifact_digest"]))
    assert document["result"]["agent_id"] == "opencode:ses_fixture:msg_fixture"
    assert not Path(dict(calls[1].env)["OPENCODE_CONFIG_DIR"]).exists()


def test_unknown_version_never_starts_provider(tmp_path, monkeypatch):
    adapter, request, permit = _setup(tmp_path)
    calls = []
    monkeypatch.setattr(subject, "resolve_opencode_executable", lambda value: value)

    def run(spec, **kwargs):
        calls.append(spec.argv)
        return _output(spec, b"2.99.0\n")

    monkeypatch.setattr(subject.runner, "run", run)
    result = adapter.dispatch(request, cwd=tmp_path, permit=permit)
    assert result.failure_category == "unsupported-opencode-version"
    assert len(calls) == 1 and calls[0][-1] == "--version"


@pytest.mark.parametrize("raw", [b"2.0.29\n", b"opencode v2.0.29\n", b"opencode v2.0.30\n"])
def test_reviewed_opencode_versions_accept_native_output_prefix(tmp_path, monkeypatch, raw):
    adapter, request, permit = _setup(tmp_path)
    monkeypatch.setattr(subject, "resolve_opencode_executable", lambda value: value)

    def run(spec, **kwargs):
        if spec.argv[-1] == "--version":
            return _output(spec, raw)
        return _output(spec, _stream(_events()))

    monkeypatch.setattr(subject.runner, "run", run)
    result = adapter.dispatch(request, cwd=tmp_path, permit=permit)
    assert result.is_success


def test_no_network_authorization_starts_no_process(tmp_path, monkeypatch):
    from dataclasses import replace

    adapter, request, permit = _setup(tmp_path)
    monkeypatch.setattr(subject.runner, "run", lambda *a, **kw: pytest.fail("unexpected process"))
    with pytest.raises(PolicyViolation, match="remote/network"):
        replace(adapter, network_authorized=False).dispatch(request, cwd=tmp_path, permit=permit)


@pytest.mark.parametrize("state", ["nonzero", "truncated", "timeout"])
def test_valid_json_cannot_override_observed_process_failure(tmp_path, monkeypatch, state):
    from dataclasses import replace

    adapter, request, permit = _setup(tmp_path)
    monkeypatch.setattr(subject, "resolve_opencode_executable", lambda value: value)

    def run(spec, **kwargs):
        if spec.argv[-1] == "--version":
            return _output(spec, b"2.0.29\n")
        output = _output(spec, _stream(_events()), exit_code=1 if state == "nonzero" else 0)
        return replace(
            output,
            result=replace(
                output.result,
                exit_code=124 if state == "timeout" else output.result.exit_code,
                truncated=state == "truncated",
                timed_out=state == "timeout",
            ),
        )

    monkeypatch.setattr(subject.runner, "run", run)
    result = adapter.dispatch(request, cwd=tmp_path, permit=permit)
    assert not result.is_success and not result.payload
    assert (
        result.failure_category
        == {"nonzero": "process-exit", "truncated": "output-limit", "timeout": "timeout"}[state]
    )


def test_corrupt_cas_rejected_before_cli_start(tmp_path, monkeypatch):
    adapter, request, permit = _setup(tmp_path)
    original = LocalContentAddressedStore.get

    def get(objects, key):
        payload = original(objects, key)
        return b"corrupt" if key == request.context_manifest_digest else payload

    monkeypatch.setattr(LocalContentAddressedStore, "get", get)
    monkeypatch.setattr(subject.runner, "run", lambda *a, **kw: pytest.fail("unexpected process"))
    with pytest.raises(ValidationFailed, match="CAS"):
        adapter.dispatch(request, cwd=tmp_path, permit=permit)


def test_loopback_profile_uses_empty_private_data_without_remote_permission(tmp_path, monkeypatch):
    from dataclasses import replace

    from zekam.application.unit_test_provider_profile import BatchProvider, BatchProviderProfile

    adapter, request, permit = _setup(tmp_path)
    profile = BatchProviderProfile(
        (
            BatchProvider(
                "zekam-batch-local",
                "http://127.0.0.1:8000/v1",
                "loopback",
                (("builder", "qwen-coder"),),
            ),
        )
    )
    adapter = replace(
        adapter,
        model_id="zekam-batch-local/builder",
        provider_profile=profile,
        network_authorized=False,
    )
    monkeypatch.setattr(subject, "resolve_opencode_executable", lambda value: value)
    captured = []

    def run(spec, **kwargs):
        if spec.argv[-1] == "--version":
            return _output(spec, b"2.0.29\n")
        env = dict(spec.env)
        assert not Path(env["XDG_DATA_HOME"]).exists()
        assert not Path(env["XDG_STATE_HOME"]).exists()
        captured.append(env["XDG_DATA_HOME"])
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert set(config["providers"]) == {"zekam-batch-local"}
        return _output(spec, _stream(_events()))

    monkeypatch.setattr(subject.runner, "run", run)
    result = adapter.dispatch(request, cwd=tmp_path, permit=permit)
    assert result.is_success and len(captured) == 1
    assert not Path(captured[0]).exists()
