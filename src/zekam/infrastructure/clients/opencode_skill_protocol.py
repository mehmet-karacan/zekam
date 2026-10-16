"""Strict native skill-tool plus final-answer protocol; no transcript retention."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import ValidationFailed
from zekam.domain.skill_package import SkillPackage
from zekam.infrastructure.clients.opencode_usage import attach_native_usage, capture_step


def native_skill_render(package: SkillPackage, directory: Path) -> str:
    # Native V2 samples ten sorted absolute file names, excluding any SKILL.md.
    files = [
        str(directory / relative)
        for relative in package.safe_projection_files()
        if Path(relative).name != "SKILL.md"
    ]
    files.sort(key=lambda value: value.encode("utf-16-be"))
    return "\n".join(
        [
            f'<skill_content name="{package.name}">',
            f"# Skill: {package.name}",
            "",
            package.instructions.strip(),
            "",
            f"Base directory for this skill: {directory}",
            "Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.",
            "Note: file list is sampled.",
            "",
            "<skill_files>",
            *(f"<file>{file}</file>" for file in files[:10]),
            "</skill_files>",
            "</skill_content>",
        ]
    )


def parse_native_skill_events(
    raw: bytes, *, package: SkillPackage, skill_directory: Path
) -> dict[str, Any]:
    from zekam.infrastructure.clients.opencode_batch import (
        MAX_RESULT_BYTES,
        _final_document,
        _identifier,
        _json,
    )

    if len(raw) > MAX_RESULT_BYTES:
        raise ValidationFailed("Native skill output limit exceeded")
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise ValidationFailed("Native skill strict UTF-8 required") from exc
    if len(lines) > 4096:
        raise ValidationFailed("Native skill event count exceeded")
    session = message = None
    previous_message = None
    active = False
    steps = 0
    tool = None
    text = text_id = None
    finished = False
    usage_steps: list[dict[str, Any]] = []
    expected = native_skill_render(package, skill_directory)
    for line in lines:
        if not line.strip():
            continue
        event = _json(line)
        current = _identifier(event.get("sessionID"))
        if session is not None and session != current:
            raise ValidationFailed("Native skill session drift")
        session = current
        kind = event.get("type")
        if finished or kind not in {"step_start", "step_finish", "text", "reasoning", "tool_use"}:
            raise ValidationFailed("Native skill error/unknown/late event")
        part = event.get("part")
        if not isinstance(part, dict) or part.get("sessionID") != session:
            raise ValidationFailed("Native skill part session mismatch")
        current_message = _identifier(part.get("messageID"))
        if kind == "step_start":
            if active or steps == 2 or current_message == previous_message:
                raise ValidationFailed("Native skill extra/reused message step")
            steps += 1
            active = True
            message = current_message
            text = text_id = None
            continue
        if not active or current_message != message:
            raise ValidationFailed("Native skill message/step drift")
        if kind == "tool_use":
            state = part.get("state")
            if (
                steps != 1
                or tool is not None
                or part.get("tool") != "skill"
                or not isinstance(state, dict)
                or state.get("status") != "completed"
                or state.get("input") != {"id": package.name}
                or state.get("output") != expected
            ):
                raise ValidationFailed("Native skill exact completed tool load required")
            call_id = _identifier(part.get("id"))
            tool = {
                "schema": "zekam-observed-native-skill-call/v1",
                "skill_id": package.name,
                "package_digest": package.package_digest,
                "semantic_digest": package.semantic_digest,
                "instruction_digest": digest_of_bytes(package.instructions.strip().encode("utf-8")),
                "native_session_digest": digest_of_bytes(session.encode("utf-8")),
                "native_message_digest": digest_of_bytes(message.encode("utf-8")),
                "native_call_digest": digest_of_bytes(call_id.encode("utf-8")),
                "native_render_digest": digest_of_bytes(expected.encode("utf-8")),
                "status": "completed",
                "skill_effect_verified": False,
                "grants_authority": False,
            }
        elif kind == "text":
            current_text = _identifier(part.get("id"))
            if (
                not isinstance(part.get("text"), str)
                or text_id is not None
                and current_text != text_id
            ):
                raise ValidationFailed("Native skill multiple/invalid text parts")
            text, text_id = part["text"], current_text
        elif kind == "step_finish":
            reason = part.get("reason")
            if steps == 1:
                if tool is None or reason != "tool-calls":
                    raise ValidationFailed("Native skill tool step incomplete")
            elif reason != "stop" or text is None:
                raise ValidationFailed("Native skill final answer incomplete")
            else:
                finished = True
            usage_steps.append(capture_step(part))
            active = False
            previous_message = message
        # Reasoning/pre-tool text is not retained in the returned artifact.
    if not finished or tool is None or session is None or message is None or text is None:
        raise ValidationFailed("Native skill incomplete tool/answer execution")
    document = _final_document(text, session, message)
    return attach_native_usage(document | {"native_skill_invocation": tool}, usage_steps)
