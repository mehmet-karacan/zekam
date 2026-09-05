from __future__ import annotations

import datetime as dt
import json
import platform
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.client_runtime_bootstrap import (
    ClientRuntimeBootstrapPlan,
    ClientRuntimeBootstrapResult,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients import codex_macos_0151_lifecycle as codex
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
ROOT = Path("/Users/mkaracan/Projeler/akilli-kasa")
SESSION_ID = "018f0000-0000-7000-8000-0000000000aa"


def _observation() -> dict[str, object]:
    return parse_codex_hook_input(
        json.dumps(
            {
                "session_id": SESSION_ID,
                "hook_event_name": "SessionStart",
                "source": "startup",
                "permission_mode": "default",
            }
        )
    ).observation_body()


@pytest.mark.parametrize("client_id", ["", "Codex", "codex/other", "codex space"])
def test_spool_client_identity_is_canonical(client_id: str, tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed, match="client_id canonical"):
        ClientLifecycleSpool(tmp_path / "home", client_id=client_id)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("grants_authority", True, PolicyViolation),
        ("stop_hook_active", 1, ValidationFailed),
        ("wire_digest", 1, ValidationFailed),
        ("client_id", None, ValidationFailed),
    ],
)
def test_spool_stage_rejects_malformed_content_free_observation(
    field: str,
    value: object,
    error: type[Exception],
    tmp_path: Path,
) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    observation = _observation()
    observation[field] = value
    with pytest.raises(error):
        spool.stage(observation, delivery_id=digest("delivery"), occurred_at=NOW)


def test_spool_stage_entry_chain_timestamp_and_binding_guards(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    observation = _observation()
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        spool.stage(
            observation,
            delivery_id=digest("naive-delivery"),
            occurred_at=NOW.replace(tzinfo=None),
        )
    entry = spool.stage(observation, delivery_id=digest("delivery"), occurred_at=NOW)
    with pytest.raises(PolicyViolation, match="sequence"):
        replace(entry, sequence=0).assert_integrity()
    with pytest.raises(PolicyViolation, match="observation digest"):
        replace(entry, observation_digest=digest("wrong-observation")).assert_integrity()
    with pytest.raises(PolicyViolation, match="entry digest"):
        replace(entry, entry_digest=digest("wrong-entry")).assert_integrity()
    drifted_observation = dict(entry.observation)
    drifted_observation["client_version"] = "other"
    draft = replace(entry, observation=drifted_observation)
    draft = replace(
        draft,
        observation_digest=digest(drifted_observation),
        entry_digest=digest(draft.body() | {"observation_digest": digest(drifted_observation)}),
    )
    with pytest.raises(PolicyViolation):
        draft.assert_integrity()


def test_spool_public_limits_missing_entry_and_client_binding(tmp_path: Path) -> None:
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    for limit in (0, 501):
        with pytest.raises(ValidationFailed, match="pending limit"):
            spool.pending(limit=limit)
    with pytest.raises(PolicyViolation, match="client binding"):
        spool.read_session_entries(client_id="other", session_id=SESSION_ID)
    with pytest.raises(ValidationFailed, match="source entry"):
        spool.record_attempt(
            digest("missing-entry"),
            outcome="failed",
            evidence_digest=digest("evidence"),
            attempted_at=NOW,
        )
    entry = spool.stage(_observation(), delivery_id=digest("delivery"), occurred_at=NOW)
    with pytest.raises(ValidationFailed, match="outcome canonical"):
        spool.record_attempt(
            entry.entry_digest,
            outcome="unknown",
            evidence_digest=digest("evidence"),
            attempted_at=NOW,
        )


def _bootstrap_plan(*, adopted: bool) -> ClientRuntimeBootstrapPlan:
    return ClientRuntimeBootstrapPlan(
        UUID("018f0000-0000-7000-8000-000000000001"),
        UUID("018f0000-0000-7000-8000-000000000002"),
        UUID("018f0000-0000-7000-8000-000000000003"),
        1,
        digest("work"),
        UUID("018f0000-0000-7000-8000-000000000004"),
        "codex",
        SESSION_ID,
        digest("entry"),
        "session_start",
        "git:revision",
        digest("policy"),
        f"runtime-bootstrap:project:{SESSION_ID}",
        f"memory:project:session:{SESSION_ID}",
        NOW,
        False,
        adopted,
        UUID("018f0000-0000-7000-8000-000000000005") if adopted else None,
    )


def test_bootstrap_plan_and_result_optional_adoption_contracts() -> None:
    fresh = _bootstrap_plan(adopted=False)
    adopted = _bootstrap_plan(adopted=True)
    assert fresh.adoption_resource is None
    assert fresh.adoption_effect_digest is None
    assert adopted.adoption_resource is not None
    assert adopted.adoption_effect_digest == digest(
        {
            "schema": "zekam-client-runtime-legacy-adoption-effect/v1",
            "operation": "client-lifecycle-legacy-run-adoption/v1",
            "resource": adopted.adoption_resource,
            "work_item_id": str(adopted.work_item_id),
            "work_revision": adopted.work_revision,
            "work_record_digest": adopted.work_record_digest,
            "adopted_run_id": str(adopted.adopted_run_id),
        }
    )
    identifiers = tuple(UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(10, 21))
    empty = ClientRuntimeBootstrapResult(*identifiers[:8])
    complete = ClientRuntimeBootstrapResult(*identifiers[:8], *identifiers[8:11])
    assert empty.as_dict()["adoption_job_id"] is None
    assert complete.as_dict()["adoption_receipt_id"] == str(identifiers[10])


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({f"key-{index}": index for index in range(65)}).encode(),
        json.dumps({"items": list(range(129))}).encode(),
        json.dumps({"value": [[[[[[[[[[[[[0]]]]]]]]]]]]]}).encode(),
    ],
)
def test_codex_strict_json_structural_caps(payload: bytes) -> None:
    with pytest.raises(ValidationFailed):
        codex.parse_codex_macos_0151(payload, expected_root=ROOT)


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"external_session_id": ""}, ValidationFailed),
        ({"event_type": "Stop"}, PolicyViolation),
        ({"source": None}, ValidationFailed),
        (
            {"event_type": "PreCompact", "source": None, "turn_id": None, "trigger": "auto"},
            ValidationFailed,
        ),
        ({"permission_mode": "root"}, ValidationFailed),
        ({"wire_digest": 1}, ValidationFailed),
    ],
)
def test_codex_event_relation_and_type_guards(
    changes: dict[str, object], error: type[Exception]
) -> None:
    values: dict[str, object] = {
        "external_session_id": SESSION_ID,
        "event_type": "SessionStart",
        "source": "startup",
        "turn_id": None,
        "trigger": None,
        "permission_mode": "default",
        "wire_digest": digest("wire"),
    }
    values.update(changes)
    with pytest.raises(error):
        codex.CodexMacOS0151Event(**values)  # type: ignore[arg-type]


def test_codex_manager_platform_pin_and_process_input_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    with pytest.raises(PolicyViolation, match="Darwin arm64"):
        codex.TrustedCodex0151ProcessManager()
    with pytest.raises(codex.LiveProcessVerificationError):
        codex._process_row(0)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    with pytest.raises(ValidationFailed, match="artifact pins"):
        codex.TrustedCodex0151ProcessManager(object())  # type: ignore[arg-type]


def test_codex_parser_root_event_and_secret_output_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationFailed, match="absolute source root"):
        codex.parse_codex_macos_0151(b"{}", expected_root=Path("relative"))
    with pytest.raises(ValidationFailed, match="hook event string"):
        body = {
            "session_id": SESSION_ID,
            "transcript_path": None,
            "cwd": str(ROOT),
            "hook_event_name": None,
        }
        codex.parse_codex_macos_0151(json.dumps(body).encode(), expected_root=ROOT)
    secret = "AKIA" + "A" * 16
    additional = {
        "schema": "zekam-codex-session-start-context/v1",
        "manifest_digest": digest("manifest"),
        "source_snapshot_id": "018f0000-0000-7000-8000-000000000099",
        "source_revision": "a" * 40,
        "fragments": [
            {
                "candidate_id": "source-health",
                "kind": "source-slice",
                "source_ref": "src/app.py",
                "content_digest": digest(secret),
                "token_count": len(secret.encode()),
                "text": secret,
            }
        ],
        "provider_called": False,
        "model_summary": False,
        "grants_authority": False,
    }
    with pytest.raises(PolicyViolation, match="secret rejected"):
        codex.success_output(json.dumps(additional, separators=(",", ":"), sort_keys=True))
    safe = dict(additional)
    text = "safe"
    fragments = additional["fragments"]
    assert isinstance(fragments, list) and isinstance(fragments[0], dict)
    safe["fragments"] = [dict(fragments[0], text=text, content_digest=digest(text), token_count=4)]
    monkeypatch.setattr(codex, "MAX_SESSION_START_SUCCESS_STDOUT_UTF8_BYTES", 1)
    with pytest.raises(ValidationFailed, match="stdout"):
        codex.success_output(json.dumps(safe, separators=(",", ":"), sort_keys=True))
