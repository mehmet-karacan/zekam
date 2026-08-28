from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from zekam.application.high_risk_autofill_guard import (
    FieldEvidence,
    FieldEvidenceStatus,
    FormFieldSpec,
    build_autofill_preview,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.session_continuity import DataClassification

NOW = dt.datetime(2026, 8, 28, 10, 0, tzinfo=dt.UTC)


def _pii_evidence() -> FieldEvidence:
    return FieldEvidence(
        "passport_number",
        "P-123456",
        "P-123456",
        "memory:passport-number",
        digest("passport-source"),
        "revision-2",
        NOW,
        0.98,
        DataClassification.PII,
        ("passport_format",),
        FieldEvidenceStatus.VERIFIED,
    )


def test_preview_and_repr_never_expose_pii_raw_value() -> None:
    evidence = _pii_evidence()
    preview = build_autofill_preview(
        form_ref="form:reviewed",
        fields=(FormFieldSpec("passport_number"),),
        evidence=(evidence,),
        now=NOW,
    )
    assert "P-123456" not in repr(evidence)
    assert "P-123456" not in str(evidence.safe_body())
    assert "P-123456" not in str(preview.body())
    assert preview.fields[0].display_value == "***"


def test_source_ref_cannot_smuggle_pii_or_external_url() -> None:
    with pytest.raises(PolicyViolation, match="portable"):
        replace(_pii_evidence(), source_ref="operator@example.test")
    with pytest.raises(PolicyViolation, match="portable"):
        replace(_pii_evidence(), source_ref="https://example.test/profile")


def test_secret_evidence_cannot_carry_normalized_value() -> None:
    with pytest.raises(PolicyViolation, match="prohibited"):
        replace(_pii_evidence(), classification=DataClassification.SECRET)
    prohibited = replace(
        _pii_evidence(),
        normalized_value=None,
        display_value=None,
        classification=DataClassification.SECRET,
        status=FieldEvidenceStatus.PROHIBITED,
    )
    preview = build_autofill_preview(
        form_ref="form:reviewed",
        fields=(FormFieldSpec("passport_number"),),
        evidence=(prohibited,),
        now=NOW,
    )
    assert preview._payload() == {}
    assert preview.fields[0].value_digest is None
