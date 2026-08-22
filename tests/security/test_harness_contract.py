"""AgentHarness prepare/apply sozlesmesinin negatif testleri.

- `prepare` hicbir yan etki uretmez: kayit yazmaz, yetki tuketmez, ag kullanmaz.
- `apply` drift varsa eski hazirligi kullanmaz.
- Yetki olmadan effect uygulanamaz.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from zekam.application.governance import GovernanceService, default_capabilities
from zekam.application.harness import AgentHarness, detect_drift
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.policy import RiskLevel
from zekam.domain.realm import Actor, ActorKind, Realm
from zekam.domain.resources import parse_requests
from zekam.domain.security import AuthorizationState, DataClassification
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.core_repository import ActorRepository

pytestmark = [pytest.mark.security, pytest.mark.postgres]

SOURCE_REVISION = "sha256:" + "b" * 64


@pytest.fixture
def actor_id(realm_session: tuple[Realm, Any]):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    return (
        ActorRepository(connection, realm.id)
        .add(Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="mehmet"))
        .id
    )


@pytest.fixture
def governance(realm_session: tuple[Realm, Any], actor_id: Any) -> GovernanceService:
    realm, connection = realm_session
    service = GovernanceService(connection, realm, actor_id=actor_id)
    service.ensure_default_policy()
    for capability in default_capabilities(realm.id):
        service.capabilities.append(capability)
    return service


@pytest.fixture
def harness(governance: GovernanceService) -> AgentHarness:
    return AgentHarness(governance)


def _counts(connection: Any) -> dict[str, int]:
    tables = (
        "runtime.job",
        "runtime.effect_claim",
        "runtime.effect_receipt",
        "runtime.resource_lock",
        "security.authorization",
    )
    counts: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f"select count(*) from {table}")
            counts[table] = int(cursor.fetchone()[0])
    return counts


# -- prepare yan etki uretmez -------------------------------------------------------


def test_prepare_writes_no_runtime_or_authorization_rows(
    harness: AgentHarness, realm_session: tuple[Realm, Any]
) -> None:
    _, connection = realm_session
    before = _counts(connection)
    harness.prepare(
        action="apply-patch",
        effects=(EffectKind.FILE_WRITE,),
        resources=parse_requests(write=("path:zekam:a.py",)),
        required_capabilities=("sandbox.write",),
        source_revision=SOURCE_REVISION,
    )
    assert _counts(connection) == before


def test_prepare_does_not_consume_authorization(
    harness: AgentHarness, governance: GovernanceService, actor_id: Any
) -> None:
    prepared = harness.prepare(
        action="apply-patch",
        effects=(EffectKind.FILE_WRITE,),
        resources=parse_requests(write=("path:zekam:a.py",)),
        required_capabilities=("sandbox.write",),
        source_revision=SOURCE_REVISION,
    )
    authorization = governance.issue_authorization(
        request=prepared.effect_request, actor_id=actor_id
    )
    harness.prepare(
        action="apply-patch",
        effects=(EffectKind.FILE_WRITE,),
        resources=parse_requests(write=("path:zekam:a.py",)),
        required_capabilities=("sandbox.write",),
        source_revision=SOURCE_REVISION,
    )
    assert governance.authorizations.get(authorization.id).state is AuthorizationState.ISSUED


def test_prepare_result_never_grants_authority(harness: AgentHarness) -> None:
    prepared = harness.prepare(action="status", effects=(EffectKind.NONE,))
    assert prepared.grants_authority is False
    assert prepared.as_dict()["grants_authority"] is False


def test_prepare_reports_denial_without_raising(harness: AgentHarness) -> None:
    prepared = harness.prepare(
        action="push", effects=(EffectKind.GIT_PUSH,), source_revision=SOURCE_REVISION
    )
    assert not prepared.verdict.allowed
    assert "policy-denies:git-push" in (prepared.verdict.denial_reason or "")


def test_prepare_classifies_risk_from_the_request(harness: AgentHarness) -> None:
    prepared = harness.prepare(
        action="apply-patch",
        effects=(EffectKind.DATABASE_WRITE,),
        source_revision=SOURCE_REVISION,
    )
    assert prepared.risk.level is RiskLevel.HIGH
    assert prepared.requires_authorization


def test_prepare_from_plan_uses_plan_effects_and_resources(
    harness: AgentHarness,
    governance: GovernanceService,
    realm_session: tuple[Realm, Any],
    tmp_path: Path,
) -> None:
    realm, connection = realm_session
    root = tmp_path / "kaynak"
    root.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=root)
    work = WorkGraphService(connection, realm)
    item = work.create_item(project_id=project.id, type=WorkType.TASK, title="Yama")
    plan = work.create_plan(
        item.id,
        source_revision=SOURCE_REVISION,
        policy_digest=governance.active_policy().policy_digest,
        steps=(
            PlanStep(
                step_id="yaz",
                title="Yaz",
                effect=EffectKind.FILE_WRITE,
                logical_resources=("path:zekam:a.py",),
            ),
        ),
    )
    prepared = harness.prepare(
        action="apply-patch", plan=plan, required_capabilities=("sandbox.write",)
    )
    assert prepared.plan_digest == plan.plan_digest
    assert prepared.source_revision == SOURCE_REVISION
    assert [item.resource.text for item in prepared.resources] == ["path:zekam:a.py"]


# -- drift ------------------------------------------------------------------------------


def test_drift_is_detected_for_each_field(harness: AgentHarness) -> None:
    prepared = harness.prepare(
        action="apply-patch",
        effects=(EffectKind.FILE_WRITE,),
        resources=parse_requests(write=("path:zekam:a.py",)),
        required_capabilities=("sandbox.write",),
        source_revision=SOURCE_REVISION,
    )
    clean = detect_drift(
        prepared,
        plan_digest=prepared.plan_digest,
        source_revision=prepared.source_revision,
        policy_digest=prepared.policy_digest,
    )
    assert not clean.drifted

    drifted = detect_drift(
        prepared,
        plan_digest="sha256:" + "c" * 64,
        source_revision="sha256:" + "d" * 64,
        policy_digest="sha256:" + "e" * 64,
    )
    assert drifted.drifted
    assert set(drifted.changed_fields) == {"plan_digest", "source_revision", "policy_digest"}


def test_apply_refuses_a_stale_preparation(
    harness: AgentHarness, governance: GovernanceService, actor_id: Any
) -> None:
    prepared = harness.prepare(
        action="apply-patch",
        effects=(EffectKind.FILE_WRITE,),
        resources=parse_requests(write=("path:zekam:a.py",)),
        required_capabilities=("sandbox.write",),
        source_revision=SOURCE_REVISION,
    )
    authorization = governance.issue_authorization(
        request=prepared.effect_request, actor_id=actor_id
    )
    outcome = harness.apply(
        prepared,
        authorization=authorization,
        consumed_by="worker-1",
        current_source_revision="sha256:" + "f" * 64,
    )
    assert not outcome.applied
    assert outcome.reason.startswith("stale-preparation")
    assert governance.authorizations.get(authorization.id).state is AuthorizationState.ISSUED


def test_stale_apply_is_recorded_in_audit(
    harness: AgentHarness, governance: GovernanceService, actor_id: Any
) -> None:
    prepared = harness.prepare(
        action="apply-patch",
        effects=(EffectKind.FILE_WRITE,),
        resources=parse_requests(write=("path:zekam:a.py",)),
        required_capabilities=("sandbox.write",),
        source_revision=SOURCE_REVISION,
    )
    harness.apply(
        prepared,
        authorization=None,
        consumed_by="worker-1",
        current_source_revision="sha256:" + "f" * 64,
    )
    trail = governance.audit.for_subject("preparation", prepared.preparation_digest)
    assert trail
    assert trail[-1]["decision"] == "deny"


# -- apply yetki ister ---------------------------------------------------------------------


def test_apply_without_authorization_is_refused(harness: AgentHarness) -> None:
    prepared = harness.prepare(
        action="apply-patch",
        effects=(EffectKind.FILE_WRITE,),
        resources=parse_requests(write=("path:zekam:a.py",)),
        required_capabilities=("sandbox.write",),
        source_revision=SOURCE_REVISION,
    )
    outcome = harness.apply(prepared, authorization=None, consumed_by="worker-1")
    assert not outcome.applied
    assert "yetkilendirilmedi" in outcome.reason or "authorization" in outcome.reason


def test_apply_consumes_the_authorization_exactly_once(
    harness: AgentHarness, governance: GovernanceService, actor_id: Any
) -> None:
    prepared = harness.prepare(
        action="apply-patch",
        effects=(EffectKind.FILE_WRITE,),
        resources=parse_requests(write=("path:zekam:a.py",)),
        required_capabilities=("sandbox.write",),
        source_revision=SOURCE_REVISION,
    )
    authorization = governance.issue_authorization(
        request=prepared.effect_request, actor_id=actor_id
    )
    first = harness.apply(prepared, authorization=authorization, consumed_by="worker-1")
    second = harness.apply(prepared, authorization=authorization, consumed_by="worker-2")

    assert first.applied
    assert not second.applied
    assert governance.authorizations.get(authorization.id).state is (AuthorizationState.CONSUMED)


def test_read_only_apply_needs_no_authorization(harness: AgentHarness) -> None:
    prepared = harness.prepare(action="status", effects=(EffectKind.NONE,))
    outcome = harness.apply(prepared, authorization=None, consumed_by="worker-1")
    assert outcome.applied
    assert outcome.reason == "salt-okunur-islem"


def test_read_only_with_sensitive_data_still_needs_authorization(
    harness: AgentHarness,
) -> None:
    prepared = harness.prepare(
        action="status",
        effects=(EffectKind.NONE,),
        data_classifications=(DataClassification.RESTRICTED,),
    )
    assert prepared.requires_authorization
    outcome = harness.apply(prepared, authorization=None, consumed_by="worker-1")
    assert not outcome.applied


def test_preparation_digest_is_stable(harness: AgentHarness) -> None:
    moment = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)
    first = harness.prepare(
        action="status", effects=(EffectKind.NONE,), source_revision=SOURCE_REVISION, now=moment
    )
    second = harness.prepare(
        action="status", effects=(EffectKind.NONE,), source_revision=SOURCE_REVISION, now=moment
    )
    assert first.preparation_digest == second.preparation_digest
