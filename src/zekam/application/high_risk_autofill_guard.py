"""Evidence-bound preview/fill/submit guard for high-risk forms and documents.

The primitive deliberately has no browser/provider implementation.  Callers
must inject an adapter behind the normal runtime claim/receipt boundary.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Protocol
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.security import Authorization
from zekam.domain.session_continuity import DataClassification

_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_PORTABLE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_MANUAL_FIELD = re.compile(
    r"(?i)(?:captcha|mfa|otp|signature|imza|payment|odeme|legal[-_ ]?declaration|hukuki)"
)
_SENSITIVE_CLASSES = frozenset(
    {
        DataClassification.CONFIDENTIAL,
        DataClassification.RESTRICTED,
        DataClassification.LOCAL_ONLY,
        DataClassification.PII,
        DataClassification.CORPORATE_CONFIDENTIAL,
    }
)
_PROHIBITED_CLASSES = frozenset(
    {
        DataClassification.SECRET,
        DataClassification.RAW_TRANSCRIPT,
        DataClassification.DIAGNOSTIC_PAYLOAD,
    }
)


def _aware(value: dt.datetime, field_name: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationFailed(f"{field_name} timezone-aware olmali")


def _portable(value: str, field_name: str) -> None:
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if (
        not value
        or value != value.strip()
        or len(value) > 512
        or not _PORTABLE_REF.fullmatch(value)
        or "://" in value
        or "\\" in value
        or value.startswith(("/", "~"))
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or ".." in posix.parts
    ):
        raise PolicyViolation(f"{field_name} portable ve bounded olmali")


class FieldEvidenceStatus(StrEnum):
    VERIFIED = "verified"
    UNKNOWN = "unknown"
    MISSING = "unknown"
    CONFLICT = "conflict"
    CONFLICTING = "conflict"
    EXPIRED = "expired"
    PROHIBITED = "prohibited"


@dataclass(frozen=True, slots=True)
class FormFieldSpec:
    field_name: str
    required: bool = True
    manual_only: bool = False

    def __post_init__(self) -> None:
        if not _FIELD_NAME.fullmatch(self.field_name):
            raise ValidationFailed("Form field canonical isim formatinda olmali")
        if not isinstance(self.required, bool) or not isinstance(self.manual_only, bool):
            raise ValidationFailed("Form field required/manual-only boolean olmali")
        if _MANUAL_FIELD.search(self.field_name) and not self.manual_only:
            raise PolicyViolation("CAPTCHA/MFA/imza/odeme/hukuki alan manual-only olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "required": self.required,
            "manual_only": self.manual_only,
        }


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    field_name: str
    normalized_value: str | None = field(repr=False)
    display_value: str | None = field(repr=False)
    source_ref: str | None
    source_digest: str | None
    source_revision: str | None
    extracted_at: dt.datetime
    confidence: float
    classification: DataClassification
    validation_rules: tuple[str, ...]
    status: FieldEvidenceStatus
    expires_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        if not _FIELD_NAME.fullmatch(self.field_name):
            raise ValidationFailed("FieldEvidence field_name canonical olmali")
        if not isinstance(self.status, FieldEvidenceStatus):
            raise ValidationFailed("FieldEvidence status registry disinda")
        if not isinstance(self.classification, DataClassification):
            raise ValidationFailed("FieldEvidence classification registry disinda")
        _aware(self.extracted_at, "FieldEvidence extracted_at")
        if self.expires_at is not None:
            _aware(self.expires_at, "FieldEvidence expires_at")
            if self.expires_at <= self.extracted_at:
                raise ValidationFailed("FieldEvidence expires_at extracted_at sonrasi olmali")
        if self.status is FieldEvidenceStatus.EXPIRED and self.expires_at is None:
            raise ValidationFailed("Expired FieldEvidence expiry zamani ister")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationFailed("FieldEvidence confidence 0..1 araliginda olmali")
        if tuple(sorted(set(self.validation_rules))) != self.validation_rules:
            raise ValidationFailed("FieldEvidence validation rules tekil ve sirali olmali")
        for rule in self.validation_rules:
            if not _FIELD_NAME.fullmatch(rule):
                raise ValidationFailed("FieldEvidence validation rule canonical olmali")
        source_fields = (self.source_ref, self.source_digest, self.source_revision)
        if any(value is not None for value in source_fields) and not all(
            value is not None for value in source_fields
        ):
            raise ValidationFailed("FieldEvidence source ref/digest/revision birlikte olmali")
        if self.source_ref is not None:
            _portable(self.source_ref, "FieldEvidence source_ref")
            assert self.source_digest is not None
            assert self.source_revision is not None
            parse_digest(self.source_digest)
            _portable(self.source_revision, "FieldEvidence source_revision")
        if (
            self.classification in _PROHIBITED_CLASSES
            and self.status is not FieldEvidenceStatus.PROHIBITED
        ):
            raise PolicyViolation("Secret/raw/diagnostic FieldEvidence prohibited olmali")
        if self.status is FieldEvidenceStatus.VERIFIED:
            if (
                not self.normalized_value
                or not self.display_value
                or self.source_ref is None
                or not self.validation_rules
            ):
                raise PolicyViolation("Verified FieldEvidence kaynak, deger ve validation ister")
            if (
                not self.normalized_value.strip()
                or len(self.normalized_value.encode("utf-8")) > 4096
                or len(self.display_value.encode("utf-8")) > 512
            ):
                raise PolicyViolation("Verified FieldEvidence degeri bos veya bounded disi")
        elif self.normalized_value is not None or self.display_value is not None:
            raise PolicyViolation("Dogrulanmamis FieldEvidence degeri bos kalmali")

    @property
    def value_digest(self) -> str | None:
        return None if self.normalized_value is None else digest(self.normalized_value)

    @property
    def safe_display_value(self) -> str | None:
        if self.normalized_value is None:
            return None
        if self.classification in _SENSITIVE_CLASSES:
            return "***"
        return self.display_value

    def safe_body(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "value_digest": self.value_digest,
            "display_value": self.safe_display_value,
            "source_ref": self.source_ref,
            "source_digest": self.source_digest,
            "source_revision": self.source_revision,
            "extracted_at": self.extracted_at,
            "expires_at": self.expires_at,
            "confidence": self.confidence,
            "classification": self.classification.value,
            "validation_rules": list(self.validation_rules),
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class AutofillPreviewField:
    field_name: str
    action: str
    display_value: str | None
    value_digest: str | None
    source_ref: str | None
    source_digest: str | None
    source_revision: str | None
    classification: DataClassification | None
    validation_rules: tuple[str, ...]
    extracted_at: dt.datetime | None
    expires_at: dt.datetime | None
    confidence: float
    status: FieldEvidenceStatus
    empty_reason: str | None
    required: bool
    _fill_value: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not _FIELD_NAME.fullmatch(self.field_name):
            raise ValidationFailed("Autofill preview field_name canonical olmali")
        if not isinstance(self.status, FieldEvidenceStatus):
            raise ValidationFailed("Autofill preview status registry disinda")
        if not isinstance(self.required, bool):
            raise ValidationFailed("Autofill preview required boolean olmali")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationFailed("Autofill preview confidence 0..1 araliginda olmali")
        if tuple(sorted(set(self.validation_rules))) != self.validation_rules:
            raise ValidationFailed("Autofill preview validation rules sirali olmali")
        for rule in self.validation_rules:
            if not _FIELD_NAME.fullmatch(rule):
                raise ValidationFailed("Autofill preview validation rule canonical olmali")
        for moment in (self.extracted_at, self.expires_at):
            if moment is not None:
                _aware(moment, "Autofill preview evidence time")
        if (
            self.extracted_at is not None
            and self.expires_at is not None
            and self.expires_at <= self.extracted_at
        ):
            raise ValidationFailed("Autofill preview evidence validity araligi gecersiz")
        if self.status is FieldEvidenceStatus.EXPIRED and self.expires_at is None:
            raise ValidationFailed("Expired preview field expiry zamani ister")
        if self.classification is not None and not isinstance(
            self.classification, DataClassification
        ):
            raise ValidationFailed("Autofill preview classification registry disinda")
        if self.action not in {"fill", "leave-empty"}:
            raise ValidationFailed("Autofill preview action gecersiz")
        if self.action == "fill":
            if (
                self._fill_value is None
                or self.value_digest is None
                or self.display_value is None
                or self.source_ref is None
                or self.source_digest is None
                or self.source_revision is None
                or self.classification is None
                or self.extracted_at is None
                or not self.validation_rules
                or self.empty_reason is not None
            ):
                raise PolicyViolation("Fill preview value digest ve kaynak ister")
            _portable(self.source_ref, "Autofill preview source_ref")
            _portable(self.source_revision, "Autofill preview source_revision")
            parse_digest(self.source_digest)
            parse_digest(self.value_digest)
            if (
                not self._fill_value.strip()
                or len(self._fill_value.encode("utf-8")) > 4096
                or len(self.display_value.encode("utf-8")) > 512
            ):
                raise PolicyViolation("Autofill preview fill degeri bounded disi")
        elif (
            self.display_value is not None
            or self.empty_reason is None
            or any(value is not None for value in (self._fill_value, self.value_digest))
        ):
            raise PolicyViolation("Leave-empty preview raw/value digest tasiyamaz")
        if self.source_ref is not None:
            _portable(self.source_ref, "Autofill preview source_ref")
        if self.source_digest is not None:
            parse_digest(self.source_digest)
        if self.source_revision is not None:
            _portable(self.source_revision, "Autofill preview source_revision")
        source_presence = (
            self.source_ref is not None,
            self.source_digest is not None,
            self.source_revision is not None,
        )
        if any(source_presence) and not all(source_presence):
            raise ValidationFailed("Autofill preview source binding birlikte olmali")
        if any(source_presence) and (
            self.classification is None or self.extracted_at is None
        ):
            raise ValidationFailed("Autofill preview source metadata eksik")
        if not any(source_presence) and (
            self.classification is not None
            or self.extracted_at is not None
            or self.expires_at is not None
            or bool(self.validation_rules)
        ):
            raise ValidationFailed("Autofill preview kaynaksiz metadata tasiyamaz")

    def safe_body(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "action": self.action,
            "display_value": self.display_value,
            "value_digest": self.value_digest,
            "source_ref": self.source_ref,
            "source_digest": self.source_digest,
            "source_revision": self.source_revision,
            "classification": (
                None if self.classification is None else self.classification.value
            ),
            "validation_rules": list(self.validation_rules),
            "extracted_at": self.extracted_at,
            "expires_at": self.expires_at,
            "confidence": self.confidence,
            "status": self.status.value,
            "empty_reason": self.empty_reason,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class AutofillPreview:
    form_ref: str
    form_schema_digest: str
    fields: tuple[AutofillPreviewField, ...]
    created_at: dt.datetime
    submit_eligible: bool
    grants_authority: bool = False

    def __post_init__(self) -> None:
        _portable(self.form_ref, "Autofill form_ref")
        parse_digest(self.form_schema_digest)
        _aware(self.created_at, "Autofill preview created_at")
        for item in self.fields:
            item.__post_init__()
        names = tuple(item.field_name for item in self.fields)
        if names != tuple(sorted(set(names))) or not names:
            raise ValidationFailed("Autofill preview fields tekil, sirali ve dolu olmali")
        expected_eligible = not any(
            item.required and item.action != "fill" for item in self.fields
        )
        if (
            not isinstance(self.submit_eligible, bool)
            or self.submit_eligible != expected_eligible
            or self.grants_authority
        ):
            raise PolicyViolation("Autofill preview submit eligibility/authority drift")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-autofill-preview/v1",
            "form_ref": self.form_ref,
            "form_schema_digest": self.form_schema_digest,
            "fields": [item.safe_body() for item in self.fields],
            "created_at": self.created_at,
            "submit_eligible": self.submit_eligible,
            "grants_authority": False,
        }

    @property
    def preview_digest(self) -> str:
        return digest(self.body())

    def _payload(self) -> dict[str, str]:
        return {
            item.field_name: item._fill_value
            for item in self.fields
            if item.action == "fill" and item._fill_value is not None
        }


def build_autofill_preview(
    *,
    form_ref: str,
    fields: tuple[FormFieldSpec, ...],
    evidence: tuple[FieldEvidence, ...],
    now: dt.datetime,
) -> AutofillPreview:
    """Read-only preview; unknown or unsafe values remain empty."""

    _aware(now, "Autofill preview now")
    _portable(form_ref, "Autofill form_ref")
    ordered_fields = tuple(sorted(fields, key=lambda item: item.field_name))
    names = tuple(item.field_name for item in ordered_fields)
    if names != tuple(sorted(set(names))) or not names:
        raise ValidationFailed("Autofill form fields tekil ve dolu olmali")
    for item in evidence:
        item.__post_init__()
    evidence_by_name = {item.field_name: item for item in evidence}
    if len(evidence_by_name) != len(evidence):
        raise ValidationFailed("Autofill evidence field names tekil olmali")
    if set(evidence_by_name) - set(names):
        raise ValidationFailed("Autofill evidence form schema disinda alan tasiyamaz")
    preview_fields: list[AutofillPreviewField] = []
    for spec in ordered_fields:
        spec.__post_init__()
        item = evidence_by_name.get(spec.field_name)
        if spec.manual_only or _MANUAL_FIELD.search(spec.field_name):
            status = FieldEvidenceStatus.PROHIBITED
            reason = "manual-only"
            item = None
        elif item is None:
            status = FieldEvidenceStatus.UNKNOWN
            reason = "missing-evidence"
        else:
            item.__post_init__()
            status = item.status
            if (
                status is FieldEvidenceStatus.VERIFIED
                and item.expires_at is not None
                and item.expires_at <= now
            ):
                status = FieldEvidenceStatus.EXPIRED
            reason = None if status is FieldEvidenceStatus.VERIFIED else status.value
        if item is not None and status is FieldEvidenceStatus.VERIFIED:
            preview_fields.append(
                AutofillPreviewField(
                    field_name=spec.field_name,
                    action="fill",
                    display_value=item.safe_display_value,
                    value_digest=item.value_digest,
                    source_ref=item.source_ref,
                    source_digest=item.source_digest,
                    source_revision=item.source_revision,
                    classification=item.classification,
                    validation_rules=item.validation_rules,
                    extracted_at=item.extracted_at,
                    expires_at=item.expires_at,
                    confidence=item.confidence,
                    status=status,
                    empty_reason=None,
                    required=spec.required,
                    _fill_value=item.normalized_value,
                )
            )
        else:
            preview_fields.append(
                AutofillPreviewField(
                    field_name=spec.field_name,
                    action="leave-empty",
                    display_value=None,
                    value_digest=None,
                    source_ref=None if item is None else item.source_ref,
                    source_digest=None if item is None else item.source_digest,
                    source_revision=None if item is None else item.source_revision,
                    classification=None if item is None else item.classification,
                    validation_rules=() if item is None else item.validation_rules,
                    extracted_at=None if item is None else item.extracted_at,
                    expires_at=None if item is None else item.expires_at,
                    confidence=0.0 if item is None else item.confidence,
                    status=status,
                    empty_reason=reason,
                    required=spec.required,
                )
            )
    form_schema_digest = digest([item.as_dict() for item in ordered_fields])
    submit_eligible = not any(
        item.required and item.action != "fill" for item in preview_fields
    )
    return AutofillPreview(
        form_ref,
        form_schema_digest,
        tuple(preview_fields),
        now,
        submit_eligible,
    )


class AutofillOperation(StrEnum):
    FILL = "fill"
    SUBMIT = "submit"


@dataclass(frozen=True, slots=True)
class AutofillEffectPlan:
    realm_id: UUID
    operation: AutofillOperation
    form_ref: str
    form_schema_digest: str
    preview_digest: str
    resource: str
    effect_kind: str
    effect_digest: str
    plan_digest: str
    fill_receipt_ref: str | None = None
    fill_receipt_digest: str | None = None
    grants_authority: bool = False

    @classmethod
    def fill(cls, realm_id: UUID, preview: AutofillPreview) -> AutofillEffectPlan:
        preview.__post_init__()
        resource = f"external-form:{parse_digest(preview.form_schema_digest)}:fill"
        return cls._create(
            realm_id=realm_id,
            operation=AutofillOperation.FILL,
            form_ref=preview.form_ref,
            form_schema_digest=preview.form_schema_digest,
            preview_digest=preview.preview_digest,
            resource=resource,
            effect_kind="process-run",
        )

    @classmethod
    def submit(
        cls,
        realm_id: UUID,
        preview: AutofillPreview,
        *,
        fill_receipt_ref: str,
        fill_receipt: AutofillEffectReceipt,
    ) -> AutofillEffectPlan:
        preview.__post_init__()
        if not preview.submit_eligible:
            raise PolicyViolation("Eksik required field ile submit plani uretilemez")
        fill_receipt.__post_init__()
        if (
            fill_receipt.operation is not AutofillOperation.FILL
            or fill_receipt.preview_digest != preview.preview_digest
        ):
            raise PolicyViolation("Submit exact ayni preview fill receipt'ine bagli olmali")
        _portable(fill_receipt_ref, "Autofill fill receipt ref")
        resource = f"external-form:{parse_digest(preview.form_schema_digest)}:submit"
        return cls._create(
            realm_id=realm_id,
            operation=AutofillOperation.SUBMIT,
            form_ref=preview.form_ref,
            form_schema_digest=preview.form_schema_digest,
            preview_digest=preview.preview_digest,
            resource=resource,
            effect_kind="network-call",
            fill_receipt_ref=fill_receipt_ref,
            fill_receipt_digest=fill_receipt.receipt_digest,
        )

    @classmethod
    def _create(
        cls,
        *,
        realm_id: UUID,
        operation: AutofillOperation,
        form_ref: str,
        form_schema_digest: str,
        preview_digest: str,
        resource: str,
        effect_kind: str,
        fill_receipt_ref: str | None = None,
        fill_receipt_digest: str | None = None,
    ) -> AutofillEffectPlan:
        effect_digest = digest(
            {
                "operation": operation.value,
                "effect": effect_kind,
                "resource": resource,
                "form_schema_digest": form_schema_digest,
                "preview_digest": preview_digest,
                "fill_receipt_ref": fill_receipt_ref,
                "fill_receipt_digest": fill_receipt_digest,
            }
        )
        draft = cls(
            realm_id,
            operation,
            form_ref,
            form_schema_digest,
            preview_digest,
            resource,
            effect_kind,
            effect_digest,
            "",
            fill_receipt_ref,
            fill_receipt_digest,
            False,
        )
        plan = replace(draft, plan_digest=digest(draft.body()))
        plan.assert_integrity()
        return plan

    def __post_init__(self) -> None:
        if not isinstance(self.operation, AutofillOperation):
            raise ValidationFailed("Autofill plan operation registry disinda")
        _portable(self.form_ref, "Autofill plan form_ref")
        _portable(self.resource, "Autofill plan resource")
        for value in (
            self.form_schema_digest,
            self.preview_digest,
            self.effect_digest,
        ):
            parse_digest(value)
        if self.operation is AutofillOperation.FILL:
            if self.effect_kind != "process-run" or any(
                value is not None
                for value in (self.fill_receipt_ref, self.fill_receipt_digest)
            ):
                raise PolicyViolation("Fill plan submit receipt tasiyamaz")
        elif (
            self.effect_kind != "network-call"
            or self.fill_receipt_ref is None
            or self.fill_receipt_digest is None
        ):
            raise PolicyViolation("Submit plan ayri fill receipt ister")
        if self.fill_receipt_ref is not None:
            _portable(self.fill_receipt_ref, "Autofill fill receipt ref")
        if self.fill_receipt_digest is not None:
            parse_digest(self.fill_receipt_digest)
        expected_resource = (
            f"external-form:{parse_digest(self.form_schema_digest)}:"
            f"{self.operation.value}"
        )
        expected_effect = digest(
            {
                "operation": self.operation.value,
                "effect": self.effect_kind,
                "resource": expected_resource,
                "form_schema_digest": self.form_schema_digest,
                "preview_digest": self.preview_digest,
                "fill_receipt_ref": self.fill_receipt_ref,
                "fill_receipt_digest": self.fill_receipt_digest,
            }
        )
        if self.resource != expected_resource or self.effect_digest != expected_effect:
            raise PolicyViolation("Autofill resource/effect digest canonical drift")
        if self.grants_authority:
            raise PolicyViolation("Autofill plan authority veremez")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-autofill-effect-plan/v1",
            "realm_id": str(self.realm_id),
            "operation": self.operation.value,
            "form_ref": self.form_ref,
            "form_schema_digest": self.form_schema_digest,
            "preview_digest": self.preview_digest,
            "resource": self.resource,
            "effect_kind": self.effect_kind,
            "effect_digest": self.effect_digest,
            "fill_receipt_ref": self.fill_receipt_ref,
            "fill_receipt_digest": self.fill_receipt_digest,
            "grants_authority": False,
        }

    def assert_integrity(self) -> None:
        self.__post_init__()
        if self.plan_digest != digest(self.body()):
            raise PolicyViolation("Autofill plan digest drift")


@dataclass(frozen=True, slots=True)
class AutofillEffectReceipt:
    operation: AutofillOperation
    plan_digest: str
    preview_digest: str
    authorization_id: UUID
    status: str
    result_digest: str
    completed_at: dt.datetime
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.operation, AutofillOperation):
            raise ValidationFailed("Autofill receipt operation registry disinda")
        parse_digest(self.plan_digest)
        parse_digest(self.preview_digest)
        parse_digest(self.result_digest)
        _aware(self.completed_at, "Autofill receipt completed_at")
        if self.status != "completed" or self.grants_authority:
            raise PolicyViolation("Autofill terminal receipt completed authority-free olmali")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-autofill-effect-receipt/v1",
            "operation": self.operation.value,
            "plan_digest": self.plan_digest,
            "preview_digest": self.preview_digest,
            "authorization_id": str(self.authorization_id),
            "status": self.status,
            "result_digest": self.result_digest,
            "completed_at": self.completed_at,
            "grants_authority": False,
        }

    @property
    def receipt_digest(self) -> str:
        return digest(self.body())


def prepare_submit_plan(
    realm_id: UUID,
    preview: AutofillPreview,
    *,
    fill_receipt_ref: str,
    fill_receipt: AutofillEffectReceipt,
) -> AutofillEffectPlan:
    """Bind submit to the exact completed fill receipt and preview."""

    return AutofillEffectPlan.submit(
        realm_id,
        preview,
        fill_receipt_ref=fill_receipt_ref,
        fill_receipt=fill_receipt,
    )


class AuthorizationStore(Protocol):
    def get(self, authorization_id: UUID) -> Authorization: ...

    def consume(
        self,
        authorization_id: UUID,
        *,
        effect_digest: str,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> Any: ...


class AutofillAdapter(Protocol):
    def fill(self, form_ref: str, values: dict[str, str]) -> dict[str, Any]: ...

    def submit(self, form_ref: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class HighRiskAutofillGuard:
    authorizations: AuthorizationStore

    def apply(
        self,
        plan: AutofillEffectPlan,
        preview: AutofillPreview,
        *,
        authorization_id: UUID,
        adapter: AutofillAdapter,
        now: dt.datetime | None = None,
    ) -> AutofillEffectReceipt:
        """Apply one exact fill or submit effect through an injected adapter."""

        moment = now or dt.datetime.now(dt.UTC)
        plan.assert_integrity()
        preview.__post_init__()
        if plan.preview_digest != preview.preview_digest:
            raise PolicyViolation("Autofill plan preview digest drift")
        if plan.operation is AutofillOperation.SUBMIT and not preview.submit_eligible:
            raise PolicyViolation("Autofill submit required field eksikken uygulanamaz")
        authorization = self.authorizations.get(authorization_id)
        rejection = authorization.rejection_reason(moment)
        if (
            rejection is not None
            or authorization.realm_id != plan.realm_id
            or authorization.plan_digest != plan.plan_digest
            or authorization.effect_digest != plan.effect_digest
            or not authorization.scope.covers_effect(plan.effect_kind)
            or not authorization.scope.covers_resource(plan.resource)
        ):
            raise AuthorizationRequired(
                f"Autofill exact authorization binding yok: {rejection or 'scope-mismatch'}"
            )
        if plan.operation is AutofillOperation.SUBMIT:
            assert plan.fill_receipt_ref is not None
            assert plan.fill_receipt_digest is not None
            _portable(plan.fill_receipt_ref, "Autofill submit fill receipt ref")
            parse_digest(plan.fill_receipt_digest)
        consumed = self.authorizations.consume(
            authorization_id,
            effect_digest=plan.effect_digest,
            consumed_by=f"high-risk-autofill-{plan.operation.value}/v1",
            now=moment,
        )
        if not bool(getattr(consumed, "consumed", False)):
            raise AuthorizationRequired("Autofill authorization tuketilemedi")
        result = (
            adapter.fill(plan.form_ref, preview._payload())
            if plan.operation is AutofillOperation.FILL
            else adapter.submit(plan.form_ref)
        )
        result_digest = digest(
            {
                "operation": plan.operation.value,
                "plan_digest": plan.plan_digest,
                "adapter_result": result,
            }
        )
        return AutofillEffectReceipt(
            plan.operation,
            plan.plan_digest,
            plan.preview_digest,
            authorization_id,
            "completed",
            result_digest,
            moment,
        )
