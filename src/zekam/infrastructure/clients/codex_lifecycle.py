"""Exact, content-free Codex command-hook lifecycle contract.

This module intentionally does not dispatch Codex.  Codex owns the lifecycle
and invokes a command hook with one JSON object on stdin.  The adapter accepts
only the reviewed structural fields and never carries prompt, response,
transcript or filesystem-path content into the Zekam lifecycle plane.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.clients import ClientDescriptor, ClientKind, ClientPermissionManifest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.hook_runtime import HookEventType

CODEX_CLIENT_ID = "codex"
CODEX_REVIEWED_VERSION = "0.150.1"
CODEX_REVIEWED_WINDOWS_SHA256 = (
    "cbd657ddfe151d1a6ebad660beffdbd3265dc5aff4b3a6095124d3e2f0156f2f"
)
CODEX_REVIEWED_EVIDENCE_DIGEST = (
    "sha256:e9327e030f757d539fdad344a9669781eff0ad9700b98ec769a484b6106f4086"
)
CODEX_REVIEWED_CLIENT_CONTRACT_DIGEST = (
    "sha256:e688a17271134e25ef233bfda7095308311afc48a7bee825bd720e3e93571147"
)
CODEX_HOOK_CONTRACT_SCHEMA = "zekam-codex-command-hook/v1"
CODEX_CONTRACT_EVIDENCE_SCHEMA = "zekam-codex-lifecycle-contract/v1"
MAX_HOOK_INPUT_BYTES = 64 * 1024

_UUID_IDENTIFIER = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_VERSION_OUTPUT = re.compile(r"^codex-cli ([0-9]+\.[0-9]+\.[0-9]+)$")
_SESSION_START_SOURCES = frozenset({"startup", "resume", "clear", "compact"})
_COMPACTION_TRIGGERS = frozenset({"manual", "auto"})
CODEX_SESSION_END_REASONS = frozenset(
    {"clear", "logout", "prompt_input_exit", "other"}
)
_PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}
)

# SessionEnd is advisory in Codex and therefore maps only to post_close.  The
# blocking close/freshness gate belongs to Stop, which can ask Codex to
# continue.  A canonical checkpoint is materialized by the orchestrator before
# it accepts pre_close; no assistant text is used as checkpoint input.
CODEX_EVENT_MAPPING: tuple[tuple[str, HookEventType], ...] = (
    ("PostCompact", HookEventType.POST_COMPACTION),
    ("PreCompact", HookEventType.PRE_COMPACTION),
    ("SessionEnd", HookEventType.POST_CLOSE),
    ("SessionStart", HookEventType.CONTINUITY_SESSION_START),
    ("Stop", HookEventType.PRE_CLOSE),
)
_MAPPING = dict(CODEX_EVENT_MAPPING)


def _uuid_identifier(value: Any, *, label: str, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValidationFailed(f"Codex hook {label} metin olmali")
    normalized = value.strip()
    if not _UUID_IDENTIFIER.fullmatch(normalized):
        raise ValidationFailed(f"Codex hook {label} lowercase UUID olmali")
    return normalized


def _optional_enum(
    value: Any,
    *,
    label: str,
    allowed: frozenset[str],
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ValidationFailed(f"Codex hook {label} reviewed enum disinda")
    return value


def _boolean(value: Any, *, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValidationFailed(f"Codex hook {label} boolean olmali")
    return value


@dataclass(frozen=True, slots=True)
class CodexHookEnvelope:
    """Reviewed subset of the Codex command-hook wire document."""

    session_id: str
    hook_event_name: str
    turn_id: str | None
    source: str | None
    trigger: str | None
    reason: str | None
    stop_hook_active: bool
    permission_mode: str | None
    wire_digest: str
    contains_prompt: bool = False
    contains_response: bool = False
    contains_transcript: bool = False
    grants_authority: bool = False

    @property
    def internal_event_type(self) -> HookEventType:
        try:
            return _MAPPING[self.hook_event_name]
        except KeyError as exc:  # defensive: construction normally validates
            raise PolicyViolation("Codex lifecycle event reviewed contract disinda") from exc

    @property
    def requires_terminal_gate(self) -> bool:
        return self.hook_event_name in {"PreCompact", "Stop"}

    @property
    def advisory_only(self) -> bool:
        return self.hook_event_name == "SessionEnd"

    def observation_body(self, *, client_version: str = CODEX_REVIEWED_VERSION) -> dict[str, Any]:
        assert_reviewed_codex_version(client_version)
        return {
            "schema": CODEX_HOOK_CONTRACT_SCHEMA,
            "client_id": CODEX_CLIENT_ID,
            "client_kind": ClientKind.CODEX.value,
            "client_version": client_version,
            "session_id": self.session_id,
            "external_event_type": self.hook_event_name,
            "internal_event_type": self.internal_event_type.value,
            "turn_id": self.turn_id,
            "source": self.source,
            "trigger": self.trigger,
            "reason": self.reason,
            "stop_hook_active": self.stop_hook_active,
            "permission_mode": self.permission_mode,
            "wire_digest": self.wire_digest,
            "contains_prompt": False,
            "contains_response": False,
            "contains_transcript": False,
            "grants_authority": False,
        }

    def delivery_id(
        self,
        *,
        occurrence_id: str,
        client_version: str = CODEX_REVIEWED_VERSION,
    ) -> str:
        """Bind one local content-free occurrence to the reviewed wire identity.

        Codex does not expose a delivery id.  The hook entrypoint creates one
        random occurrence id per invocation so two resume/end observations for
        the same session are not silently collapsed.  Durable worker replay
        remains idempotent because the resulting delivery id is persisted.
        """

        version = assert_reviewed_codex_version(client_version)
        checked_occurrence = _uuid_identifier(occurrence_id, label="occurrence_id")
        assert checked_occurrence is not None

        return digest(
            {
                "contract": CODEX_HOOK_CONTRACT_SCHEMA,
                "client_version": version,
                "occurrence_id": checked_occurrence,
                "session_id": self.session_id,
                "external_event_type": self.hook_event_name,
                "turn_id": self.turn_id,
                "source": self.source,
                "trigger": self.trigger,
                "reason": self.reason,
                "stop_hook_active": self.stop_hook_active,
                "wire_digest": self.wire_digest,
            }
        )


def parse_codex_hook_input(payload: bytes | str) -> CodexHookEnvelope:
    """Parse one Codex hook input without retaining content-bearing fields."""

    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if not raw or len(raw) > MAX_HOOK_INPUT_BYTES:
        raise ValidationFailed("Codex hook input boyutu 1..65536 byte olmali")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Codex hook input strict JSON olmali") from exc
    if not isinstance(document, dict):
        raise ValidationFailed("Codex hook input JSON object olmali")
    return codex_hook_envelope(document)


def codex_hook_envelope(document: Mapping[str, Any]) -> CodexHookEnvelope:
    """Normalize a decoded command-hook document to the reviewed safe subset."""

    session_id = _uuid_identifier(document.get("session_id"), label="session_id")
    event_name = document.get("hook_event_name")
    if not isinstance(event_name, str):
        raise ValidationFailed("Codex hook hook_event_name metin olmali")
    assert session_id is not None
    if event_name not in _MAPPING:
        raise PolicyViolation(f"Codex lifecycle event reviewed contract disinda: {event_name}")

    turn_id = _uuid_identifier(document.get("turn_id"), label="turn_id", required=False)
    source = _optional_enum(
        document.get("source"), label="source", allowed=_SESSION_START_SOURCES
    )
    trigger = _optional_enum(
        document.get("trigger"), label="trigger", allowed=_COMPACTION_TRIGGERS
    )
    reason = _optional_enum(
        document.get("reason"), label="reason", allowed=CODEX_SESSION_END_REASONS
    )
    permission_mode = _optional_enum(
        document.get("permission_mode"),
        label="permission_mode",
        allowed=_PERMISSION_MODES,
    )
    stop_hook_active = _boolean(
        document.get("stop_hook_active"), label="stop_hook_active", default=False
    )

    if event_name == "SessionStart":
        if (
            source not in _SESSION_START_SOURCES
            or any(value is not None for value in (turn_id, trigger, reason))
            or permission_mode not in _PERMISSION_MODES
            or stop_hook_active
        ):
            raise ValidationFailed("Codex SessionStart reviewed wire contract ile uyusmuyor")
    elif event_name in {"PreCompact", "PostCompact"}:
        if turn_id is None or trigger not in _COMPACTION_TRIGGERS:
            raise ValidationFailed("Codex compact hook turn_id ve canonical trigger ister")
        if (
            source is not None
            or reason is not None
            or permission_mode is not None
            or stop_hook_active
        ):
            raise ValidationFailed("Codex compact hook beklenmeyen lifecycle alani tasiyor")
    elif event_name == "Stop":
        if (
            turn_id is None
            or any(value is not None for value in (source, trigger, reason))
            or permission_mode not in _PERMISSION_MODES
        ):
            raise ValidationFailed("Codex Stop reviewed wire contract ile uyusmuyor")
    elif event_name == "SessionEnd":
        if (
            reason not in CODEX_SESSION_END_REASONS
            or any(value is not None for value in (turn_id, source, trigger, permission_mode))
            or stop_hook_active
        ):
            raise ValidationFailed("Codex SessionEnd reviewed wire contract ile uyusmuyor")

    # The digest binds only the reviewed structural subset.  Content-bearing
    # fields such as transcript_path, cwd and last_assistant_message are neither
    # retained nor hashed, preventing low-entropy content from leaking through
    # a digest oracle.
    safe_wire = {
        "session_id": session_id,
        "hook_event_name": event_name,
        "turn_id": turn_id,
        "source": source,
        "trigger": trigger,
        "reason": reason,
        "stop_hook_active": stop_hook_active,
        "permission_mode": permission_mode,
    }
    return CodexHookEnvelope(
        session_id=session_id,
        hook_event_name=event_name,
        turn_id=turn_id,
        source=source,
        trigger=trigger,
        reason=reason,
        stop_hook_active=stop_hook_active,
        permission_mode=permission_mode,
        wire_digest=digest(safe_wire),
    )


def parse_codex_version_output(output: str) -> str:
    match = _VERSION_OUTPUT.fullmatch(output.strip())
    if match is None:
        raise ValidationFailed("Codex version output reviewed format disinda")
    return match.group(1)


def assert_reviewed_codex_version(version: str) -> str:
    normalized = version.strip()
    if normalized != CODEX_REVIEWED_VERSION:
        raise PolicyViolation(
            "Codex lifecycle contract version drift; lifecycle-events-v2 devre disi"
        )
    return normalized


def codex_lifecycle_descriptor(
    executable: str,
    *,
    installed_version: str,
    permission_manifest: ClientPermissionManifest | None = None,
) -> ClientDescriptor:
    """Create a descriptor that advertises lifecycle only for the exact review."""

    version = assert_reviewed_codex_version(installed_version)
    return ClientDescriptor(
        kind=ClientKind.CODEX,
        client_id=CODEX_CLIENT_ID,
        executable=executable,
        capabilities=frozenset(
            {
                "chat",
                "code",
                "tool-use",
                "structured-result",
                "cancellation",
                "sandbox-write",
                "lifecycle-events-v2",
            }
        ),
        version=version,
        permission_manifest=permission_manifest,
    )


def load_codex_contract_evidence(path: Path) -> dict[str, Any]:
    """Load the tracked exact contract document and verify its semantic binding."""

    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Codex lifecycle contract evidence okunamadi") from exc
    if not isinstance(document, dict):
        raise ValidationFailed("Codex lifecycle contract evidence object olmali")
    if digest_of_bytes(raw) != CODEX_REVIEWED_EVIDENCE_DIGEST:
        raise PolicyViolation("Codex lifecycle contract evidence semantic drift: tracked bytes")
    expected_mapping = [
        {"external": external, "internal": internal.value}
        for external, internal in CODEX_EVENT_MAPPING
    ]
    executable = document.get("reviewed_executable")
    official_contract = document.get("official_contract")
    hook_command = document.get("hook_command")
    wire_constraints = document.get("wire_constraints")
    durability = document.get("durability")
    offline_e2e = document.get("offline_e2e")
    if (
        document.get("schema") != CODEX_CONTRACT_EVIDENCE_SCHEMA
        or document.get("client_id") != CODEX_CLIENT_ID
        or document.get("client_kind") != ClientKind.CODEX.value
        or document.get("installed_version") != CODEX_REVIEWED_VERSION
        or document.get("event_mapping") != expected_mapping
        or official_contract
        != {
            "hooks": "https://developers.openai.com/codex/hooks",
            "configuration": "https://developers.openai.com/codex/config-reference",
            "noninteractive": "https://developers.openai.com/codex/noninteractive",
            "reviewed_on": "2026-08-28",
        }
        or hook_command
        != {
            "command": (
                "python -m zekam.interfaces.cli.client hook --client codex "
                "--client-version 0.150.1"
            ),
            "windows_override_field": "commandWindows",
            "stdin": "one Codex hook JSON object",
            "stdout": "empty JSON object",
            "outbox": "ZEKAM_HOME/global/runtime/client-lifecycle/codex",
            "external_provider_required": False,
        }
        or document.get("discarded_input_fields")
        != [
            "cwd",
            "last_assistant_message",
            "model",
            "prompt",
            "response",
            "tool_input",
            "tool_response",
            "transcript_path",
        ]
        or document.get("postgres_ledger_event_mapping")
        != {
            "session_start": "session.created",
            "pre_compaction": "session.compacting",
            "post_compaction": "session.compacted",
            "pre_close": "session.status",
            "post_close": "session.deleted",
        }
        or not isinstance(executable, dict)
        or executable.get("sha256") != CODEX_REVIEWED_WINDOWS_SHA256
        or executable.get("path_recorded") is not False
        or not isinstance(wire_constraints, dict)
        or wire_constraints.get("session_id") != "lowercase-uuid"
        or wire_constraints.get("turn_id") != "lowercase-uuid-when-present"
        or wire_constraints.get("occurrence_id") != "lowercase-uuid-local-only"
        or wire_constraints.get("session_end_reasons")
        != ["clear", "logout", "prompt_input_exit", "other"]
        or not isinstance(durability, dict)
        or durability.get("append_only_events") is not True
        or durability.get("per_session_hash_chain") is not True
        or durability.get("idempotent_delivery_replay") is not True
        or durability.get("immutable_ack_receipts") is not True
        or durability.get("public_arbitrary_ack") is not False
        or durability.get("canonical_ack_requires_idempotent_lookup") is not True
        or durability.get("pre_compaction_ack_requires_runtime_binding_outbox")
        is not True
        or durability.get("ack_requires_terminal_continuity_binding") is not True
        or durability.get("continuity_binding_requires_claim_receipt") is not True
        or durability.get("pre_compaction_ack_requires_compiler_enqueue") is not True
        or durability.get("continuity_adapter_composed") is not True
        or durability.get("generic_repository_direct_drain") is not False
        or durability.get("admission_phases")
        != [
            "read-only-preflight",
            "single-transaction-apply",
            "read-only-lookup",
        ]
        or durability.get("automatic_retry") is not False
        or durability.get("max_distinct_failures_before_manual_review") != 3
        or durability.get("deterministic_poison_attempt_replay") is not True
        or durability.get("predecessor_manual_review_cascade") is not True
        or durability.get("bounded_pending_batch") != 256
        or durability.get("caller_controlled_mutation_skip") is not False
        or durability.get("delivery_identity") != "local-content-free-occurrence-id"
        or durability.get("pending_selection_history_scan") is not False
        or durability.get("immutable_queue_index") is not True
        or durability.get("derived_drain_cursor") is not True
        or durability.get("drain_cursor_semantics") != "resolved-prefix-v2"
        or durability.get("immutable_drain_cursor_chain") is not True
        or durability.get("cursor_full_binding_parity") is not True
        or durability.get("bounded_paginated_status") is not True
        or durability.get("hook_append_history_scan") is not False
        or durability.get("bounded_session_tail_entries") != 2
        or durability.get("database_required_in_hook") is not False
        or durability.get("max_spool_document_bytes") != 1_048_576
        or durability.get("reparse_and_symlink_fail_closed") is not True
        or durability.get("regular_file_required") is not True
        or durability.get("no_follow_when_platform_exposes_it") is not True
        or durability.get("parent_directory_fsync_posix") is not True
        or durability.get("parent_directory_fsync_windows_proven") is not False
        or not isinstance(offline_e2e, dict)
        or offline_e2e.get("real_binary") is not True
        or offline_e2e.get("temporary_codex_home") is not True
        or offline_e2e.get("loopback_responses_provider") is not True
        or offline_e2e.get("saved_auth_loaded") is not False
        or offline_e2e.get("loopback_and_proxy_observation_only") is not True
        or offline_e2e.get("kernel_egress_deny_proven") is not False
        or offline_e2e.get("absence_of_direct_egress_proven") is not False
        or offline_e2e.get("hook_trust_bypass_for_test_only") is not True
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Codex lifecycle contract evidence semantic drift")
    return document | {"file_digest": digest_of_bytes(raw)}
