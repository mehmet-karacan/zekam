from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from uuid import UUID

from zekam.application.high_risk_autofill_guard import (
    AutofillEffectPlan,
    FieldEvidence,
    FieldEvidenceStatus,
    FormFieldSpec,
    HighRiskAutofillGuard,
    build_autofill_preview,
    prepare_submit_plan,
)
from zekam.domain.canonical import digest
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.domain.session_continuity import DataClassification

NOW = dt.datetime(2026, 8, 28, 10, 0, tzinfo=dt.UTC)
REALM_ID = UUID("00000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000002")


class _Authorizations:
    def __init__(self) -> None:
        self.current: Authorization | None = None

    def get(self, authorization_id: UUID) -> Authorization:
        assert self.current is not None and self.current.id == authorization_id
        return self.current

    def consume(
        self,
        authorization_id: UUID,
        *,
        effect_digest: str,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> SimpleNamespace:
        assert self.current is not None and self.current.id == authorization_id
        assert effect_digest == self.current.effect_digest
        assert consumed_by and now is not None
        return SimpleNamespace(consumed=True)


class _Adapter:
    def __init__(self) -> None:
        self.filled: dict[str, str] = {}
        self.submitted = False

    def fill(self, form_ref: str, values: dict[str, str]) -> dict[str, object]:
        self.filled = values
        return {"form_ref_digest": digest(form_ref), "field_count": len(values)}

    def submit(self, form_ref: str) -> dict[str, object]:
        self.submitted = True
        return {"form_ref_digest": digest(form_ref), "submitted": True}


def _authorization(plan: AutofillEffectPlan) -> Authorization:
    return Authorization.issue(
        realm_id=REALM_ID,
        actor_id=ACTOR_ID,
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=(plan.resource,),
            allowed_effects=(plan.effect_kind,),
        ),
        risk="high",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )


def test_fill_and_submit_require_two_exact_authorizations() -> None:
    evidence = FieldEvidence(
        "full_name",
        "Ada Lovelace",
        "Ada Lovelace",
        "memory:profile-name",
        digest("profile-name"),
        "revision-3",
        NOW,
        0.99,
        DataClassification.INTERNAL,
        ("non_empty",),
        FieldEvidenceStatus.VERIFIED,
    )
    preview = build_autofill_preview(
        form_ref="form:reviewed",
        fields=(FormFieldSpec("full_name"),),
        evidence=(evidence,),
        now=NOW,
    )
    authorizations, adapter = _Authorizations(), _Adapter()
    guard = HighRiskAutofillGuard(authorizations)
    fill = AutofillEffectPlan.fill(REALM_ID, preview)
    authorizations.current = _authorization(fill)
    fill_receipt = guard.apply(
        fill,
        preview,
        authorization_id=authorizations.current.id,
        adapter=adapter,
        now=NOW,
    )
    submit = prepare_submit_plan(
        REALM_ID,
        preview,
        fill_receipt_ref="receipt:fill-1",
        fill_receipt=fill_receipt,
    )
    authorizations.current = _authorization(submit)
    submit_receipt = guard.apply(
        submit,
        preview,
        authorization_id=authorizations.current.id,
        adapter=adapter,
        now=NOW,
    )
    assert adapter.filled == {"full_name": "Ada Lovelace"}
    assert adapter.submitted
    assert fill_receipt.receipt_digest == submit.fill_receipt_digest
    assert fill_receipt.authorization_id != submit_receipt.authorization_id
