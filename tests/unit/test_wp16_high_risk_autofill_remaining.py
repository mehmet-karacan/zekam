from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest

from zekam.application.high_risk_autofill_guard import (
    AutofillEffectPlan,
    AutofillEffectReceipt,
    AutofillOperation,
    AutofillPreview,
    AutofillPreviewField,
    FieldEvidence,
    FieldEvidenceStatus,
    FormFieldSpec,
    HighRiskAutofillGuard,
    build_autofill_preview,
    prepare_submit_plan,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.session_continuity import DataClassification

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 8))
D = digest("evidence")


def _evidence(**changes: Any) -> FieldEvidence:
    values: dict[str, Any] = {
        "field_name": "full_name",
        "normalized_value": "Ada Lovelace",
        "display_value": "Ada Lovelace",
        "source_ref": "memory:profile-name",
        "source_digest": D,
        "source_revision": "revision-3",
        "extracted_at": NOW,
        "confidence": 0.99,
        "classification": DataClassification.PII,
        "validation_rules": ("non_empty",),
        "status": FieldEvidenceStatus.VERIFIED,
    }
    values.update(changes)
    return FieldEvidence(**values)


def _preview() -> AutofillPreview:
    return build_autofill_preview(
        form_ref="form:reviewed",
        fields=(FormFieldSpec("full_name"),),
        evidence=(_evidence(),),
        now=NOW,
    )


def test_field_spec_and_evidence_exhaustive_fail_closed_matrix() -> None:
    with pytest.raises(ValidationFailed, match="canonical"):
        FormFieldSpec("Bad Field")
    with pytest.raises(ValidationFailed, match="boolean"):
        FormFieldSpec("name", required=cast(Any, 1))
    with pytest.raises(PolicyViolation, match="manual-only"):
        FormFieldSpec("payment_card")
    assert FormFieldSpec("payment_card", manual_only=True).manual_only

    evidence = _evidence()
    assert evidence.safe_display_value == "***"
    assert evidence.value_digest == digest("Ada Lovelace")
    public = replace(evidence, classification=DataClassification.PUBLIC)
    assert public.safe_display_value == "Ada Lovelace"
    missing = FieldEvidence(
        "nickname",
        None,
        None,
        None,
        None,
        None,
        NOW,
        0.0,
        DataClassification.INTERNAL,
        (),
        FieldEvidenceStatus.UNKNOWN,
    )
    assert missing.value_digest is None and missing.safe_display_value is None

    invalid: tuple[Callable[[], object], ...] = (
        lambda: _evidence(field_name="Bad Field"),
        lambda: _evidence(status=cast(Any, "verified")),
        lambda: _evidence(classification=cast(Any, "pii")),
        lambda: _evidence(extracted_at=NOW.replace(tzinfo=None)),
        lambda: _evidence(expires_at=NOW.replace(tzinfo=None)),
        lambda: _evidence(expires_at=NOW),
        lambda: _evidence(status=FieldEvidenceStatus.EXPIRED, expires_at=None),
        lambda: _evidence(confidence=-0.1),
        lambda: _evidence(confidence=1.1),
        lambda: _evidence(validation_rules=("z", "a")),
        lambda: _evidence(validation_rules=("bad rule",)),
        lambda: _evidence(source_digest=None),
        lambda: _evidence(source_ref="/absolute"),
        lambda: _evidence(classification=DataClassification.SECRET),
        lambda: _evidence(normalized_value=""),
        lambda: _evidence(display_value=""),
        lambda: _evidence(normalized_value="x" * 4097),
        lambda: _evidence(display_value="x" * 513),
        lambda: _evidence(status=FieldEvidenceStatus.UNKNOWN),
    )
    for build in invalid:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            build()


def test_preview_field_metadata_action_and_source_invariants() -> None:
    field = _preview().fields[0]
    assert field.safe_body()["action"] == "fill"
    invalid: tuple[Callable[[], object], ...] = (
        lambda: replace(field, field_name="Bad Field"),
        lambda: replace(field, status=cast(Any, "verified")),
        lambda: replace(field, required=cast(Any, 1)),
        lambda: replace(field, confidence=-1.0),
        lambda: replace(field, validation_rules=("z", "a")),
        lambda: replace(field, validation_rules=("bad rule",)),
        lambda: replace(field, extracted_at=NOW.replace(tzinfo=None)),
        lambda: replace(field, expires_at=NOW),
        lambda: replace(field, status=FieldEvidenceStatus.EXPIRED, expires_at=None),
        lambda: replace(field, classification=cast(Any, "pii")),
        lambda: replace(field, action="unknown"),
        lambda: replace(field, _fill_value=None),
        lambda: replace(field, source_ref="/absolute"),
        lambda: replace(field, _fill_value=" "),
        lambda: replace(field, display_value="x" * 513),
        lambda: replace(field, source_digest=None),
        lambda: replace(field, classification=None),
    )
    for build in invalid:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            build()

    empty = AutofillPreviewField(
        "nickname",
        "leave-empty",
        None,
        None,
        None,
        None,
        None,
        None,
        (),
        None,
        None,
        0.0,
        FieldEvidenceStatus.UNKNOWN,
        "missing-evidence",
        False,
    )
    with pytest.raises(PolicyViolation, match="Leave-empty"):
        replace(empty, display_value="leak")
    with pytest.raises(ValidationFailed, match="kaynaksiz metadata"):
        replace(empty, classification=DataClassification.PUBLIC)


def test_preview_collection_duplicate_extra_expiry_and_eligibility_boundaries() -> None:
    preview = _preview()
    with pytest.raises(ValidationFailed, match="tekil"):
        AutofillPreview("form:one", D, (), NOW, False)
    with pytest.raises(PolicyViolation, match="eligibility"):
        replace(preview, submit_eligible=False)
    with pytest.raises(PolicyViolation, match="eligibility"):
        replace(preview, grants_authority=True)
    with pytest.raises(ValidationFailed, match="tekil"):
        build_autofill_preview(form_ref="form:one", fields=(), evidence=(), now=NOW)
    with pytest.raises(ValidationFailed, match="tekil"):
        build_autofill_preview(
            form_ref="form:one",
            fields=(FormFieldSpec("full_name"), FormFieldSpec("full_name")),
            evidence=(),
            now=NOW,
        )
    with pytest.raises(ValidationFailed, match="tekil"):
        build_autofill_preview(
            form_ref="form:one",
            fields=(FormFieldSpec("full_name"),),
            evidence=(_evidence(), _evidence()),
            now=NOW,
        )
    with pytest.raises(ValidationFailed, match="schema disinda"):
        build_autofill_preview(
            form_ref="form:one",
            fields=(FormFieldSpec("nickname"),),
            evidence=(_evidence(),),
            now=NOW,
        )
    expired = build_autofill_preview(
        form_ref="form:one",
        fields=(FormFieldSpec("full_name"),),
        evidence=(_evidence(expires_at=NOW + dt.timedelta(seconds=1)),),
        now=NOW + dt.timedelta(seconds=1),
    )
    assert expired.fields[0].status is FieldEvidenceStatus.EXPIRED


def test_plan_receipt_and_guard_submit_consumption_paths() -> None:
    preview = _preview()
    fill = AutofillEffectPlan.fill(IDS[0], preview)
    fill_receipt = AutofillEffectReceipt(
        AutofillOperation.FILL,
        fill.plan_digest,
        preview.preview_digest,
        IDS[1],
        "completed",
        D,
        NOW,
    )
    submit = prepare_submit_plan(
        IDS[0], preview, fill_receipt_ref="receipt:fill", fill_receipt=fill_receipt
    )
    with pytest.raises(ValidationFailed, match="operation"):
        replace(fill, operation=cast(Any, "fill"))
    with pytest.raises(PolicyViolation, match="submit receipt"):
        replace(fill, effect_kind="network-call")
    with pytest.raises(PolicyViolation, match="ayri fill receipt"):
        replace(submit, fill_receipt_ref=None)
    with pytest.raises(PolicyViolation, match="canonical drift"):
        replace(fill, resource="external-form:wrong:fill")
    with pytest.raises(PolicyViolation, match="authority"):
        replace(fill, grants_authority=True)
    with pytest.raises(ValidationFailed, match="operation"):
        replace(fill_receipt, operation=cast(Any, "fill"))
    with pytest.raises(PolicyViolation, match="authority-free"):
        replace(fill_receipt, status="failed")

    scope = SimpleNamespace(covers_effect=lambda _value: True, covers_resource=lambda _value: True)
    authorization = SimpleNamespace(
        rejection_reason=lambda _now: None,
        realm_id=submit.realm_id,
        plan_digest=submit.plan_digest,
        effect_digest=submit.effect_digest,
        scope=scope,
    )
    store = SimpleNamespace(get=Mock(return_value=authorization), consume=Mock())
    adapter = SimpleNamespace(fill=Mock(), submit=Mock(return_value={"submitted": True}))
    guard = HighRiskAutofillGuard(cast(Any, store))
    with pytest.raises(PolicyViolation, match="preview digest"):
        guard.apply(
            submit,
            replace(preview, form_ref="form:changed"),
            authorization_id=IDS[2],
            adapter=cast(Any, adapter),
            now=NOW,
        )
    store.consume.return_value = SimpleNamespace(consumed=False)
    with pytest.raises(AuthorizationRequired, match="tuketilemedi"):
        guard.apply(submit, preview, authorization_id=IDS[2], adapter=cast(Any, adapter), now=NOW)
    store.consume.return_value = SimpleNamespace(consumed=True)
    receipt = guard.apply(
        submit, preview, authorization_id=IDS[2], adapter=cast(Any, adapter), now=NOW
    )
    assert receipt.operation is AutofillOperation.SUBMIT
    adapter.submit.assert_called_once_with("form:reviewed")
