from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

import zekam.application.mutation_admission as admission
from zekam.application.mutation_admission import (
    ActiveRuntimeContinuityIdentity,
    CliMutationAdmission,
    CliMutationAdmissionRegistry,
    CliMutationEvidence,
    CliMutationRule,
    CliMutationTargetHints,
    MutationAdmissionExemption,
    _advance_gate_a_source_capability,
    _close_receipt_identity,
    assert_full_continuity_backend,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ZekamError

IDS = tuple(UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 12))


def _receipt(path: Path, **changes: object) -> Path:
    body: dict[str, object] = {
        "project_id": str(IDS[0]),
        "work_item_id": str(IDS[1]),
        "run_id": str(IDS[2]),
        "session_id": "session-one",
        "client_id": "codex",
    }
    body.update(changes)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_close_receipt_identity_early_return_io_size_and_schema_matrix(tmp_path: Path) -> None:
    assert _close_receipt_identity(("status",), {}) == {}
    assert _close_receipt_identity(("close", "apply"), {"apply": False}) == {}
    assert _close_receipt_identity(("close", "apply"), {"apply": True}) == {}
    with pytest.raises(PolicyViolation, match="path tipi"):
        _close_receipt_identity(("close", "apply"), {"apply": True, "input_file": 1})
    with pytest.raises(PolicyViolation, match="okunamadi"):
        _close_receipt_identity(
            ("close", "apply"), {"apply": True, "input_file": tmp_path / "missing"}
        )

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (admission._MAX_CLOSE_RECEIPT_BYTES + 1))
    with pytest.raises(PolicyViolation, match="bounded"):
        _close_receipt_identity(("close", "apply"), {"apply": True, "input_file": oversized})

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="exact JSON"):
        _close_receipt_identity(("close", "apply"), {"apply": True, "input_file": malformed})
    non_object = tmp_path / "array.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="object"):
        _close_receipt_identity(("close", "apply"), {"apply": True, "input_file": non_object})
    invalid_receipt_changes: tuple[dict[str, Any], ...] = (
        {"project_id": "bad"},
        {"work_item_id": None},
        {"run_id": []},
        {"session_id": 7},
        {"client_id": None},
    )
    for index, changes in enumerate(invalid_receipt_changes):
        candidate = _receipt(tmp_path / f"bad-{index}.json", **changes)
        with pytest.raises(PolicyViolation, match="execution kimligi"):
            _close_receipt_identity(("close", "apply"), {"apply": True, "input_file": candidate})


def test_target_hints_aliases_receipt_merge_and_padding_paths(tmp_path: Path) -> None:
    hint = CliMutationTargetHints.from_parameters(
        ("project", "show"),
        {"query": IDS[0], "work_id": " work ", "authorization_id": IDS[3]},
    )
    assert hint.project_ref == str(IDS[0])
    assert hint.work_ref == "work"
    assert hint.authorization_ref == str(IDS[3])
    related = CliMutationTargetHints.from_parameters(
        ("work", "relate"), {"source": "source-work", "candidate_id": "candidate"}
    )
    assert related.work_ref == "source-work"
    assert related.candidate_ref == "candidate"
    assert CliMutationTargetHints.from_parameters(("status",), {"project": 7}) == (
        CliMutationTargetHints()
    )

    left = CliMutationTargetHints(project_ref="project")
    right = CliMutationTargetHints(work_ref="work")
    assert left.merge_exact(right) == CliMutationTargetHints(project_ref="project", work_ref="work")
    assert left.merge_exact(CliMutationTargetHints(project_ref="project")) == left
    with pytest.raises(PolicyViolation, match="padded"):
        CliMutationTargetHints(client_ref=" client ")

    receipt = _receipt(tmp_path / "close.json")
    merged = CliMutationTargetHints.from_parameters(
        ("memory", "close-apply"), {"uygula": True, "input_file": receipt}
    )
    assert merged.session_ref == "session-one"


def _forward_evidence(**changes: object) -> CliMutationEvidence:
    body: dict[str, object] = {
        "schema": "zekam-opencode-lifecycle-event/v2",
        "event_type": "session.created",
        "sequence": 1,
        "previous_digest": None,
        "session_id": "session-one",
        "grants_authority": False,
    }
    body.update(changes)
    evidence_digest = digest(body)
    document = body | {"event_digest": evidence_digest}
    return CliMutationEvidence(
        "opencode-forward-event",
        evidence_digest,
        CliMutationTargetHints(session_ref="session-one"),
        event_type="session.created",
        sequence=1,
        canonical_input=canonical_json(document),
    )


def test_forward_evidence_exact_first_event_and_all_structural_rejections() -> None:
    valid = _forward_evidence()
    assert valid.exact_first_session_created
    assert not replace(valid, kind="other").exact_first_session_created
    with pytest.raises(PolicyViolation, match="canonical input"):
        replace(valid, canonical_input=None)
    with pytest.raises(PolicyViolation, match="JSON gecersiz"):
        replace(valid, canonical_input="{")
    with pytest.raises(PolicyViolation, match="object"):
        replace(valid, canonical_input="[]")
    for changes in (
        {"schema": "wrong"},
        {"event_type": "session.updated"},
        {"sequence": True},
        {"previous_digest": digest("previous")},
        {"session_id": "other"},
        {"grants_authority": True},
    ):
        body = {
            "schema": "zekam-opencode-lifecycle-event/v2",
            "event_type": "session.created",
            "sequence": 1,
            "previous_digest": None,
            "session_id": "session-one",
            "grants_authority": False,
        }
        body.update(changes)
        wrong = body | {"event_digest": digest(body)}
        with pytest.raises(PolicyViolation, match="binding drift"):
            replace(valid, canonical_input=canonical_json(wrong))


def test_rule_admission_registry_and_backend_edge_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PolicyViolation, match="allowlist"):
        CliMutationRule(("unknown",), True, exemption=MutationAdmissionExemption.CONTROL_PLANE)
    with pytest.raises(PolicyViolation, match="parameter allowlist"):
        CliMutationRule(("unknown",), True, mutation_parameters=())
    always = CliMutationRule(("x",), True, always_mutating=True, read_only_parameter="dry_run")
    assert always.is_mutating({})
    assert not always.is_mutating({"dry_run": True})
    alias = CliMutationRule(("x",), True)
    assert alias.is_mutating({"uygula": True})
    assert not alias.is_mutating({})

    hints = CliMutationTargetHints()
    invalid_admissions: tuple[Callable[[], object], ...] = (
        lambda: CliMutationAdmission((), False, False, False, None, hints),
        lambda: CliMutationAdmission(
            ("x",), False, False, False, None, hints, grants_authority=True
        ),
        lambda: CliMutationAdmission(
            ("x",), True, True, True, MutationAdmissionExemption.BOOTSTRAP, hints
        ),
        lambda: CliMutationAdmission(("x",), True, False, True, None, hints),
        lambda: CliMutationAdmission(("x",), True, True, True, None, cast(Any, {})),
    )
    for build in invalid_admissions:
        with pytest.raises(PolicyViolation):
            build()

    with pytest.raises(PolicyViolation, match="session/client"):
        ActiveRuntimeContinuityIdentity(IDS[0], IDS[1], IDS[2], IDS[3], "session", " ")

    registry = CliMutationAdmissionRegistry()
    assert registry.classify((), {}).command_path == ("realm-session",)
    unknown_read = registry.classify(("new", "read"), {})
    assert not unknown_read.mutating and not unknown_read.requires_full_continuity
    initial = registry.classify(
        ("opencode", "forward"), {"apply": True}, evidence=_forward_evidence()
    )
    assert initial.exemption is MutationAdmissionExemption.BOOTSTRAP
    later_evidence = replace(_forward_evidence(), kind="other")
    later = registry.classify(("opencode", "forward"), {"apply": True}, evidence=later_evidence)
    assert later.exemption is None and later.requires_existing_hydration
    assert registry.snapshot(("status",)).admission.command == "status"
    assert registry.exemptions == tuple(sorted(registry.exemptions, key=lambda item: item[0]))
    assert registry.always_mutating_commands

    assert_full_continuity_backend(
        backend="sqlite", supports_full_continuity=False, admission=unknown_read
    )
    exempt = registry.classify(("doctor",), {"prepare": True})
    with pytest.raises(ZekamError, match="exemption"):
        assert_full_continuity_backend(
            backend="sqlite", supports_full_continuity=False, admission=exempt
        )

    fake = object.__new__(admission._GateASourceCapability)
    monkeypatch.setitem(admission._GATE_A_STATES, fake, (("continuity", "source-bind"), "WRONG"))
    with pytest.raises(PolicyViolation, match="state rejected"):
        _advance_gate_a_source_capability(fake, "INPUTS_VALID", "FIRST_CAPTURED")
    monkeypatch.setitem(
        admission._GATE_A_STATES,
        fake,
        (("continuity", "source-bind"), "INPUTS_VALID"),
    )
    with pytest.raises(PolicyViolation, match="state rejected"):
        _advance_gate_a_source_capability(fake, "INPUTS_VALID", "WRONG")
