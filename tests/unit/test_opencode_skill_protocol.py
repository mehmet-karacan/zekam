"""Offline native skill protocol fixtures; actual host acceptance is deferred."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.unit.test_opencode_batch import _output, _request, _stream
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.skill_package import SkillPackage
from zekam.infrastructure.clients import opencode_batch as batch
from zekam.infrastructure.clients.opencode_skill_protocol import parse_native_skill_events


def _package():
    return SkillPackage.parse(
        "native-example",
        {
            "SKILL.md": b"---\nname: native-example\ndescription: Native fixture.\nallowed-tools: Bash\n---\nReturn seven.\n",
            "references/example.md": b"Reference fixture.",
        },
    )


def _events(directory: Path):
    # Literal fixture follows the official native V2 renderer, not model text.
    rendered = (
        f'<skill_content name="native-example">\n# Skill: native-example\n\nReturn seven.\n\n'
        f"Base directory for this skill: {directory}\n"
        "Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.\n"
        "Note: file list is sampled.\n\n<skill_files>\n"
        f"<file>{directory / 'references/example.md'}</file>\n</skill_files>\n</skill_content>"
    )

    def event(kind, message, **part):
        return {
            "type": kind,
            "sessionID": "ses_native",
            "part": {"sessionID": "ses_native", "messageID": message, **part},
        }

    return [
        event("step_start", "msg_tool", id="prt_one"),
        event("reasoning", "msg_tool", text="Private reasoning sentinel."),
        event("text", "msg_tool", id="prt_pre", text="Private preamble sentinel."),
        event(
            "tool_use",
            "msg_tool",
            id="call_native",
            tool="skill",
            state={
                "status": "completed",
                "input": {"id": "native-example"},
                "output": rendered,
                "metadata": {"private": "Private metadata sentinel."},
            },
        ),
        event("step_finish", "msg_tool", reason="tool-calls"),
        event("step_start", "msg_answer", id="prt_two"),
        event(
            "text",
            "msg_answer",
            id="prt_answer",
            text=json.dumps({"result": {}, "body": {"output": 7}}),
        ),
        event("step_finish", "msg_answer", reason="stop"),
    ]


def test_real_tool_and_final_message_identity_are_retained_without_transcript(tmp_path):
    result = parse_native_skill_events(
        _stream(_events(tmp_path)), package=_package(), skill_directory=tmp_path
    )
    assert result["result"]["agent_id"] == "opencode:ses_native:msg_answer"
    assert result["body"] == {"output": 7}
    observed = result["native_skill_invocation"]
    assert observed["native_call_digest"] == digest_of_bytes(b"call_native")
    assert observed["native_message_digest"] == digest_of_bytes(b"msg_tool")
    assert observed["status"] == "completed"
    assert observed["skill_effect_verified"] is False
    raw = json.dumps(result)
    assert "sentinel" not in raw
    assert str(tmp_path) not in raw
    assert "Return seven" not in raw
    assert "call_native" not in raw


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "wrong-tool",
        "failed",
        "wrong-id",
        "extra-input",
        "renderer",
        "late",
        "session",
        "message",
        "nonterminal",
        "no-answer",
    ],
)
def test_missing_foreign_duplicate_and_unfinished_tool_execution_is_rejected(tmp_path, mutation):
    events = _events(tmp_path)
    if mutation == "missing":
        events.pop(3)
    elif mutation == "extra":
        events.insert(4, events[3])
    elif mutation == "wrong-tool":
        events[3]["part"]["tool"] = "bash"
    elif mutation == "failed":
        events[3]["part"]["state"]["status"] = "error"
    elif mutation == "wrong-id":
        events[3]["part"]["state"]["input"]["id"] = "different"
    elif mutation == "extra-input":
        events[3]["part"]["state"]["input"]["command"] = "unexpected"
    elif mutation == "renderer":
        events[3]["part"]["state"]["output"] += "Unexpected injected content."
    elif mutation == "late":
        events.append(events[-1])
    elif mutation == "session":
        events[3]["sessionID"] = "foreign"
    elif mutation == "message":
        events[3]["part"]["messageID"] = "foreign"
    elif mutation == "nonterminal":
        events[-1]["part"]["reason"] = "length"
    else:
        events.pop(-2)
    with pytest.raises(ValidationFailed):
        parse_native_skill_events(_stream(events), package=_package(), skill_directory=tmp_path)


def test_native_config_allows_only_exact_skill_and_preserves_text_only_default():
    default = json.loads(batch.batch_config())
    configured = json.loads(batch.batch_config(native_skill=_package()))
    assert default["agents"]["zekam-test-batch"]["steps"] == 1
    assert "skills" not in default
    assert configured["agents"]["zekam-test-batch"]["steps"] == 2
    assert configured["permissions"] == [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "skill", "resource": "native-example", "effect": "allow"},
    ]
    assert configured["experimental"]["policies"][-2:] == [
        {"action": "tool.use", "resource": "skill:native-example", "effect": "allow"},
        {"action": "integration.use", "resource": "skill:native-example", "effect": "allow"},
    ]


def test_native_dispatch_uses_private_canonical_package_and_stores_only_bounded_artifact(
    tmp_path, monkeypatch
):
    adapter, request, permit = _request(tmp_path)
    runtime = tmp_path.parent / f"{tmp_path.name}-runtime"
    runtime.mkdir()
    # Offline fixture uses the test runner's private temp area; this port double
    # is not acceptance evidence for a public-neutral production runtime root.
    monkeypatch.setattr(batch, "_native_runtime_root", lambda root: root.resolve())
    native = replace(adapter, native_skill=_package(), native_runtime_root=runtime)
    assert native.execution_binding_digest != adapter.execution_binding_digest
    monkeypatch.setattr(batch, "resolve_opencode_executable", lambda path: Path(path))
    roots = []

    def process(spec, *, cwd, stdin=None, cancel=None):
        if spec.argv[-1] == "--version":
            return _output(spec, batch.SUPPORTED_VERSION.encode())
        config = json.loads(dict(spec.env)["OPENCODE_CONFIG_CONTENT"])
        directory = Path(config["skills"][0]) / "native-example"
        roots.append(directory)
        assert not directory.is_relative_to(tmp_path)
        assert "allowed-tools" not in (directory / "SKILL.md").read_text(encoding="utf-8")
        assert (directory / "references/example.md").read_bytes() == b"Reference fixture."
        assert str(directory) not in " ".join(spec.argv)
        return _output(spec, _stream(_events(directory)))

    monkeypatch.setattr(batch.runner, "run", process)
    result = native.dispatch(request, cwd=tmp_path, permit=permit)
    assert result.is_success
    artifact = json.loads(native.objects.get(result.payload["artifact_digest"]))
    assert artifact["native_skill_invocation"]["package_digest"] == _package().package_digest
    assert artifact["body"]["output"] == 7
    assert roots and not roots[0].exists()


def test_constructed_package_metadata_cannot_override_canonical_files(tmp_path):
    adapter, _request_value, _permit = _request(tmp_path)
    tampered = replace(_package(), instructions="Changed permission-bearing instructions.")
    with pytest.raises(PolicyViolation, match="metadata/content drift"):
        replace(adapter, native_skill=tampered)


def test_native_execution_requires_explicit_non_home_runtime_root(tmp_path):
    adapter, _request_value, _permit = _request(tmp_path)
    with pytest.raises(PolicyViolation, match="explicit real external runtime"):
        replace(adapter, native_skill=_package())
    with pytest.raises(PolicyViolation):
        batch._native_runtime_root(Path.home())


def test_native_runtime_inside_source_is_rejected_before_any_process(tmp_path, monkeypatch):
    adapter, request, permit = _request(tmp_path)
    monkeypatch.setattr(batch, "_native_runtime_root", lambda root: root.resolve())
    native = replace(adapter, native_skill=_package(), native_runtime_root=tmp_path)
    monkeypatch.setattr(
        batch.runner, "run", lambda *_args, **_kwargs: pytest.fail("must reject before process")
    )
    monkeypatch.setattr(batch, "resolve_opencode_executable", lambda path: Path(path))
    with pytest.raises(PolicyViolation, match="temp source"):
        native.dispatch(request, cwd=tmp_path, permit=permit)
