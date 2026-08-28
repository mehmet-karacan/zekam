from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import UUID

import pytest

from zekam.application.high_risk_autofill_guard import (
    AutofillEffectPlan,
    AutofillEffectReceipt,
    AutofillOperation,
    FieldEvidence,
    FieldEvidenceStatus,
    FormFieldSpec,
    build_autofill_preview,
    prepare_submit_plan,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.session_continuity import DataClassification

NOW = dt.datetime(2026, 8, 28, 10, 0, tzinfo=dt.UTC)
REALM_ID = UUID("00000000-0000-0000-0000-000000000001")


def _evidence(
    status: FieldEvidenceStatus = FieldEvidenceStatus.VERIFIED,
    *,
    expires_at: dt.datetime | None = None,
    classification: DataClassification = DataClassification.PII,
) -> FieldEvidence:
    value = "Ada Lovelace" if status is FieldEvidenceStatus.VERIFIED else None
    return FieldEvidence(
        field_name="full_name",
        normalized_value=value,
        display_value=value,
        source_ref="memory:profile-name",
        source_digest=digest("profile-name"),
        source_revision="revision-3",
        extracted_at=NOW,
        confidence=0.99,
        classification=classification,
        validation_rules=("non_empty",),
        status=status,
        expires_at=expires_at,
    )


def test_preview_masks_sensitive_value_but_retains_exact_fill_payload() -> None:
    preview = build_autofill_preview(
        form_ref="form:visa-application",
        fields=(FormFieldSpec("full_name"),),
        evidence=(_evidence(),),
        now=NOW,
    )
    assert preview.fields[0].display_value == "***"
    assert preview.fields[0].value_digest == digest("Ada Lovelace")
    assert preview._payload() == {"full_name": "Ada Lovelace"}
    assert "Ada Lovelace" not in str(preview.body())
    changed = build_autofill_preview(
        form_ref="form:visa-application",
        fields=(FormFieldSpec("full_name"),),
        evidence=(replace(_evidence(), source_revision="revision-4"),),
        now=NOW,
    )
    assert changed.preview_digest != preview.preview_digest


def test_unknown_conflict_expired_and_prohibited_values_remain_empty() -> None:
    conflicting = _evidence(FieldEvidenceStatus.CONFLICTING)
    conflict_preview = build_autofill_preview(
        form_ref="form:one",
        fields=(FormFieldSpec("full_name"),),
        evidence=(conflicting,),
        now=NOW,
    )
    assert conflict_preview.fields[0].action == "leave-empty"
    assert conflict_preview._payload() == {}
    expired = build_autofill_preview(
        form_ref="form:two",
        fields=(FormFieldSpec("full_name"),),
        evidence=(_evidence(expires_at=NOW + dt.timedelta(minutes=1)),),
        now=NOW + dt.timedelta(minutes=1),
    )
    assert expired.fields[0].status is FieldEvidenceStatus.EXPIRED
    assert expired.fields[0].value_digest is None
    with pytest.raises(PolicyViolation, match="prohibited"):
        replace(_evidence(), classification=DataClassification.SECRET)


def test_manual_only_and_submit_receipt_binding_are_fail_closed() -> None:
    preview = build_autofill_preview(
        form_ref="form:reviewed",
        fields=(FormFieldSpec("full_name"), FormFieldSpec("signature", manual_only=True)),
        evidence=(_evidence(),),
        now=NOW,
    )
    assert not preview.submit_eligible
    assert preview.fields[1].empty_reason == "manual-only"
    fill = AutofillEffectPlan.fill(REALM_ID, preview)
    fill_receipt = AutofillEffectReceipt(
        AutofillOperation.FILL,
        fill.plan_digest,
        preview.preview_digest,
        UUID("00000000-0000-0000-0000-000000000002"),
        "completed",
        digest("fill"),
        NOW,
    )
    with pytest.raises(PolicyViolation, match="required field"):
        AutofillEffectPlan.submit(
            REALM_ID,
            preview,
            fill_receipt_ref="receipt:fill-1",
            fill_receipt=fill_receipt,
        )


def test_fill_and_submit_have_distinct_authority_and_exact_preview_binding() -> None:
    preview = build_autofill_preview(
        form_ref="form:reviewed",
        fields=(FormFieldSpec("full_name"),),
        evidence=(_evidence(),),
        now=NOW,
    )
    fill = AutofillEffectPlan.fill(REALM_ID, preview)
    fill_receipt = AutofillEffectReceipt(
        AutofillOperation.FILL,
        fill.plan_digest,
        preview.preview_digest,
        UUID("00000000-0000-0000-0000-000000000002"),
        "completed",
        digest("adapter-fill-result"),
        NOW,
    )
    submit = prepare_submit_plan(
        REALM_ID,
        preview,
        fill_receipt_ref="receipt:fill-1",
        fill_receipt=fill_receipt,
    )
    assert fill.effect_kind == "process-run"
    assert submit.effect_kind == "network-call"
    assert fill.plan_digest != submit.plan_digest
    assert fill.effect_digest != submit.effect_digest
    forged = replace(fill_receipt, preview_digest=digest("other-preview"))
    with pytest.raises(PolicyViolation, match="ayni preview"):
        prepare_submit_plan(
            REALM_ID,
            preview,
            fill_receipt_ref="receipt:fill-forged",
            fill_receipt=forged,
        )
