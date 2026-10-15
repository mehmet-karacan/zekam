"""Bounded publication wire parsing; decoded JSON never supplies trust or authority."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from zekam.application.git_publication import (
    GitPublicationEvidence,
    GitPublicationPlan,
    GitPublicationTarget,
)
from zekam.application.local_runtime import LocalJobBinding
from zekam.domain.canonical import canonical_json
from zekam.domain.errors import ValidationFailed
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    AuthorizationState,
    DataClassification,
)


def _same(body: object, expected: object) -> None:
    if canonical_json(body) != canonical_json(expected):
        raise ValidationFailed("Publication wire schema/computed alan drift")


def publication_plan_from_body(body: dict[str, Any]) -> GitPublicationPlan:
    try:
        value = body["binding"]
        binding = LocalJobBinding(
            project_id=value["project_id"],
            work_item_id=value["work_item_id"],
            work_revision=value["work_revision"],
            run_id=value["run_id"],
            config_revision_id=value["config_revision_id"],
            config_digest=value["config_digest"],
            plan_digest=value["plan_digest"],
            run_step_id=value["run_step_id"],
            source_snapshot_id=value["source_snapshot_id"],
        )
        target = GitPublicationTarget(**body["target"])
        plan = GitPublicationPlan(
            UUID(body["realm_id"]),
            UUID(body["executor_id"]),
            binding,
            target,
            body["test_evidence_digest"],
            body["verifier_evidence_digest"],
        )
        _same(body, plan.body())
        return plan
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValidationFailed("Publication plan wire sozlesmesi gecersiz") from exc


def publication_evidence_from_body(body: dict[str, Any]) -> GitPublicationEvidence:
    try:
        evidence = GitPublicationEvidence(
            body["kind"],
            body["subject_digest"],
            UUID(body["execution_id"]),
            UUID(body["executor_id"]),
            body["passed"],
            dt.datetime.fromisoformat(body["recorded_at"]),
        )
        _same(body, evidence.body())
        return evidence
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValidationFailed("Publication evidence wire sozlesmesi gecersiz") from exc


def publication_authorization_from_body(body: dict[str, Any], *, realm_id: UUID) -> Authorization:
    try:
        scope = body["scope"]
        authorization = Authorization(
            id=UUID(body["id"]),
            realm_id=realm_id,
            actor_id=UUID(body["actor_id"]),
            work_item_id=UUID(body["work_item_id"]),
            plan_digest=body["plan_digest"],
            effect_digest=body["effect_digest"],
            scope=AuthorizationScope(
                allowed_resources=tuple(scope["allowed_resources"]),
                allowed_effects=tuple(scope["allowed_effects"]),
                provider_refs=tuple(scope["provider_refs"]),
                secret_ref_ids=tuple(UUID(value) for value in scope["secret_ref_ids"]),
                data_classifications=tuple(
                    DataClassification(value) for value in scope["data_classifications"]
                ),
                allowed_read_resources=tuple(scope.get("allowed_read_resources", ())),
                allowed_write_resources=tuple(scope.get("allowed_write_resources", ())),
            ),
            risk=body["risk"],
            issued_at=dt.datetime.fromisoformat(body["issued_at"]),
            expires_at=dt.datetime.fromisoformat(body["expires_at"]),
        )
        if body["state"] != AuthorizationState.ISSUED.value:
            raise ValidationFailed("Publication import issued authorization ister")
        _same(body, authorization.as_dict())
        return authorization
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValidationFailed("Publication authorization wire sozlesmesi gecersiz") from exc
