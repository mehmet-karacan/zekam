"""Work Graph'in gercek PostgreSQL uzerindeki davranisi."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation
from zekam.domain.realm import Realm
from zekam.domain.work import (
    AcceptanceCriterion,
    DecisionOption,
    EffectKind,
    EvidenceRef,
    PlanStep,
    RelationKind,
    WorkState,
    WorkType,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

POLICY_DIGEST = "sha256:" + "a" * 64
SOURCE_REVISION = "sha256:" + "b" * 64


@pytest.fixture
def project_id(realm_session: tuple[Realm, Any], tmp_path: Path):  # type: ignore[no-untyped-def]
    realm, connection = realm_session
    root = tmp_path / "kaynak"
    root.mkdir()
    (root / "README.md").write_text("# kaynak\n", encoding="utf-8")
    project = ProjectIntegrationService(connection, realm).register(source_path=root)
    return project.id


@pytest.fixture
def service(realm_session: tuple[Realm, Any]) -> WorkGraphService:
    realm, connection = realm_session
    return WorkGraphService(connection, realm)


def _create(service: WorkGraphService, project_id: Any, title: str = "Is", **kwargs: Any):  # type: ignore[no-untyped-def]
    return service.create_item(project_id=project_id, type=WorkType.TASK, title=title, **kwargs)


# -- yasam dongusu ---------------------------------------------------------------------


def test_create_writes_item_revision_and_event(service: WorkGraphService, project_id: Any) -> None:
    item = _create(service, project_id, "Ilk is")
    assert service.items.get(item.id).title == "Ilk is"
    assert len(service.history(item.id)) == 1
    assert service.events.for_entity(entity_type="work.item", entity_id=item.id)[0].event_type == (
        "work.created"
    )


def test_full_lifecycle_builds_verifiable_chain(service: WorkGraphService, project_id: Any) -> None:
    item = _create(service, project_id)
    service.transition(item.id, WorkState.READY)
    service.transition(item.id, WorkState.ACTIVE)
    service.transition(item.id, WorkState.VERIFICATION)
    completed = service.transition(
        item.id,
        WorkState.COMPLETED,
        evidence=(EvidenceRef(kind="test", reference="pytest"),),
    )

    assert completed.state is WorkState.COMPLETED
    assert completed.revision == 5
    assert service.verify_history(item.id)
    assert [record["state"] for record in service.history(item.id)] == [
        "proposed",
        "ready",
        "active",
        "verification",
        "completed",
    ]


def test_completed_without_evidence_is_rejected_by_database(
    service: WorkGraphService, project_id: Any
) -> None:
    item = _create(service, project_id)
    service.transition(item.id, WorkState.READY)
    service.transition(item.id, WorkState.ACTIVE)
    service.transition(item.id, WorkState.VERIFICATION)
    with pytest.raises(PolicyViolation, match="Acceptance evidence"):
        service.transition(item.id, WorkState.COMPLETED)

    # Uygulama katmani atlanarak dogrudan yazma da reddedilmelidir.
    with (
        pytest.raises(Exception, match="work_item_completed_requires_evidence"),
        service.connection.cursor() as cursor,
    ):
        cursor.execute("update work.work_item set state = 'completed' where id = %s", (item.id,))


def test_forbidden_transition_is_rejected(service: WorkGraphService, project_id: Any) -> None:
    item = _create(service, project_id)
    with pytest.raises(PolicyViolation, match="Izinsiz durum gecisi"):
        service.transition(item.id, WorkState.COMPLETED)


def test_optimistic_concurrency_rejects_stale_writer(
    service: WorkGraphService, project_id: Any
) -> None:
    item = _create(service, project_id)
    service.transition(item.id, WorkState.READY)
    stale = item.with_state(WorkState.READY)
    with pytest.raises(ConcurrencyConflict):
        service.items.replace(stale, expected_revision=1)


def test_details_update_creates_new_revision(service: WorkGraphService, project_id: Any) -> None:
    item = _create(service, project_id)
    updated = service.update_details(
        item.id,
        title="Yeni baslik",
        acceptance_criteria=(AcceptanceCriterion(text="testler gecer"),),
    )
    assert updated.revision == 2
    assert service.items.get(item.id).title == "Yeni baslik"
    assert len(service.history(item.id)) == 2


def test_external_number_lookup_is_exact(service: WorkGraphService, project_id: Any) -> None:
    _create(service, project_id, "Defect 123", external_number="123")
    _create(service, project_id, "Defect 1234", external_number="1234")
    found = service.find_exact(project_id=project_id, external_number="123")
    assert found.title == "Defect 123"


def test_unknown_external_number_raises_not_found(
    service: WorkGraphService, project_id: Any
) -> None:
    with pytest.raises(NotFound):
        service.find_exact(project_id=project_id, external_number="9999")


def test_duplicate_external_number_is_rejected(service: WorkGraphService, project_id: Any) -> None:
    _create(service, project_id, "Ilk", external_number="42")
    with pytest.raises(Exception, match="work_item_external_number_idx"):
        _create(service, project_id, "Ikinci", external_number="42")


# -- iliskiler ----------------------------------------------------------------------------


def test_dependency_blocks_activation(service: WorkGraphService, project_id: Any) -> None:
    blocker = _create(service, project_id, "Onkosul")
    dependent = _create(service, project_id, "Bagimli")
    service.relate(source_id=dependent.id, target_id=blocker.id, kind=RelationKind.DEPENDS_ON)
    service.transition(dependent.id, WorkState.READY)

    with pytest.raises(PolicyViolation, match="Bagimlilik veya blocker"):
        service.transition(dependent.id, WorkState.ACTIVE)


def test_activation_is_allowed_after_dependency_completes(
    service: WorkGraphService, project_id: Any
) -> None:
    blocker = _create(service, project_id, "Onkosul")
    dependent = _create(service, project_id, "Bagimli")
    service.relate(source_id=dependent.id, target_id=blocker.id, kind=RelationKind.DEPENDS_ON)

    for state in (WorkState.READY, WorkState.ACTIVE, WorkState.VERIFICATION):
        service.transition(blocker.id, state)
    service.transition(
        blocker.id, WorkState.COMPLETED, evidence=(EvidenceRef(kind="test", reference="ok"),)
    )

    service.transition(dependent.id, WorkState.READY)
    assert service.transition(dependent.id, WorkState.ACTIVE).state is WorkState.ACTIVE


def test_relation_cycle_is_rejected_by_database(service: WorkGraphService, project_id: Any) -> None:
    first = _create(service, project_id, "Bir")
    second = _create(service, project_id, "Iki")
    third = _create(service, project_id, "Uc")
    service.relate(source_id=first.id, target_id=second.id, kind=RelationKind.DEPENDS_ON)
    service.relate(source_id=second.id, target_id=third.id, kind=RelationKind.DEPENDS_ON)
    with pytest.raises(Exception, match="dongusu reddedildi"):
        service.relate(source_id=third.id, target_id=first.id, kind=RelationKind.DEPENDS_ON)


def test_parent_of_cycle_is_rejected(service: WorkGraphService, project_id: Any) -> None:
    parent = _create(service, project_id, "Ust")
    child = _create(service, project_id, "Alt")
    service.relate(source_id=parent.id, target_id=child.id, kind=RelationKind.PARENT_OF)
    with pytest.raises(Exception, match="dongusu reddedildi"):
        service.relate(source_id=child.id, target_id=parent.id, kind=RelationKind.PARENT_OF)


def test_non_directional_relation_may_be_mutual(service: WorkGraphService, project_id: Any) -> None:
    first = _create(service, project_id, "Bir")
    second = _create(service, project_id, "Iki")
    service.relate(source_id=first.id, target_id=second.id, kind=RelationKind.RELATES_TO)
    service.relate(source_id=second.id, target_id=first.id, kind=RelationKind.RELATES_TO)
    assert len(service.relations.outgoing(first.id)) == 1
    assert len(service.relations.incoming(first.id)) == 1


def test_cross_project_relation_is_rejected(
    service: WorkGraphService, project_id: Any, realm_session: tuple[Realm, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    other_root = tmp_path / "digeri"
    other_root.mkdir()
    other_project = ProjectIntegrationService(connection, realm).register(
        source_path=other_root, slug="digeri"
    )
    here = _create(service, project_id, "Burada")
    there = service.create_item(project_id=other_project.id, type=WorkType.TASK, title="Orada")
    with pytest.raises(PolicyViolation, match="Cross-project"):
        service.relate(source_id=here.id, target_id=there.id, kind=RelationKind.BLOCKS)


def test_blocks_relation_is_reported(service: WorkGraphService, project_id: Any) -> None:
    blocker = _create(service, project_id, "Blocker")
    blocked = _create(service, project_id, "Bloklu")
    service.relate(source_id=blocker.id, target_id=blocked.id, kind=RelationKind.BLOCKS)
    assert service.relations.blockers(blocked.id) == (blocker.id,)
    assert not service.snapshot(blocked.id).is_actionable


def test_relation_can_be_removed(service: WorkGraphService, project_id: Any) -> None:
    first = _create(service, project_id, "Bir")
    second = _create(service, project_id, "Iki")
    relation = service.relate(source_id=first.id, target_id=second.id, kind=RelationKind.DEPENDS_ON)
    assert service.unrelate(relation.id) is True
    assert service.relations.outgoing(first.id) == ()


# -- Intent, Decision, Plan -----------------------------------------------------------------


def test_intent_revisions_are_append_only(service: WorkGraphService, project_id: Any) -> None:
    item = _create(service, project_id)
    first = service.set_intent(item.id, goal="Ilk hedef", non_goals=("kapsam disi",))
    second = service.set_intent(item.id, goal="Guncellenmis hedef")

    assert first.revision == 1
    assert second.revision == 2
    assert service.intents.current(item.id).goal == "Guncellenmis hedef"  # type: ignore[union-attr]
    assert len(service.intents.history(item.id)) == 2

    with (
        pytest.raises(Exception, match=r"append-only|permission denied"),
        service.connection.cursor() as cursor,
    ):
        cursor.execute("update work.intent set goal = 'degistirildi'")


def test_decision_records_alternatives_and_evidence(
    service: WorkGraphService, project_id: Any
) -> None:
    item = _create(service, project_id)
    decision = service.record_decision(
        item.id,
        question="Kuyruk nerede?",
        chosen_option="PostgreSQL",
        rationale="Tek kanonik store",
        alternatives=(DecisionOption(name="Redis", rejected_because="Ayri state"),),
        criteria=("dayaniklilik",),
        evidence=(EvidenceRef(kind="doc", reference="mimari/ANA_MIMARI.md"),),
    )
    stored = service.decisions.current(item.id)
    assert stored is not None
    assert stored.decision_digest == decision.decision_digest
    assert stored.alternatives[0].name == "Redis"
    assert stored.evidence[0].reference == "mimari/ANA_MIMARI.md"


def test_plan_is_stored_with_effect_digest(service: WorkGraphService, project_id: Any) -> None:
    item = _create(service, project_id)
    plan = service.create_plan(
        item.id,
        source_revision=SOURCE_REVISION,
        policy_digest=POLICY_DIGEST,
        steps=(
            PlanStep(step_id="oku", title="Oku", effect=EffectKind.NONE),
            PlanStep(
                step_id="yaz",
                title="Yaz",
                effect=EffectKind.FILE_WRITE,
                logical_resources=("path:zekam:a.py",),
                depends_on=("oku",),
            ),
        ),
    )
    stored = service.plans.current(item.id)
    assert stored is not None
    assert stored.plan_digest == plan.plan_digest
    assert stored.effect_digest == plan.effect_digest
    assert stored.execution_order == ("oku", "yaz")
    assert stored.requires_authorization


def test_plan_never_stores_authority(service: WorkGraphService, project_id: Any) -> None:
    item = _create(service, project_id)
    service.create_plan(
        item.id,
        source_revision=SOURCE_REVISION,
        policy_digest=POLICY_DIGEST,
        steps=(PlanStep(step_id="oku", title="Oku", effect=EffectKind.NONE),),
    )
    with service.connection.cursor() as cursor:
        cursor.execute("select grants_authority from work.task_plan")
        assert cursor.fetchone()[0] is False
        with pytest.raises(Exception, match=r"plan_grants_no_authority|append-only|permission"):
            cursor.execute("update work.task_plan set grants_authority = true")


def test_plan_drift_is_detected(service: WorkGraphService, project_id: Any) -> None:
    item = _create(service, project_id)
    service.create_plan(
        item.id,
        source_revision=SOURCE_REVISION,
        policy_digest=POLICY_DIGEST,
        steps=(PlanStep(step_id="oku", title="Oku", effect=EffectKind.NONE),),
    )
    service.assert_plan_is_current(
        item.id, source_revision=SOURCE_REVISION, policy_digest=POLICY_DIGEST
    )
    with pytest.raises(PolicyViolation, match="stale"):
        service.assert_plan_is_current(
            item.id, source_revision="sha256:" + "c" * 64, policy_digest=POLICY_DIGEST
        )


def test_plans_with_same_effect_are_findable(service: WorkGraphService, project_id: Any) -> None:
    first = _create(service, project_id, "Bir")
    second = _create(service, project_id, "Iki")
    steps = (
        PlanStep(
            step_id="yaz",
            title="Yaz",
            effect=EffectKind.FILE_WRITE,
            logical_resources=("path:zekam:a.py",),
        ),
    )
    plan = service.create_plan(
        first.id, source_revision=SOURCE_REVISION, policy_digest=POLICY_DIGEST, steps=steps
    )
    service.create_plan(
        second.id, source_revision=SOURCE_REVISION, policy_digest=POLICY_DIGEST, steps=steps
    )
    assert len(service.plans.find_by_effect_digest(plan.effect_digest)) == 2


def test_missing_plan_raises_not_found(service: WorkGraphService, project_id: Any) -> None:
    item = _create(service, project_id)
    with pytest.raises(NotFound):
        service.assert_plan_is_current(
            item.id, source_revision=SOURCE_REVISION, policy_digest=POLICY_DIGEST
        )


# -- sorgular ------------------------------------------------------------------------------


def test_today_orders_by_state_priority(service: WorkGraphService, project_id: Any) -> None:
    proposed = _create(service, project_id, "Onerilen")
    active = _create(service, project_id, "Aktif")
    service.transition(active.id, WorkState.READY)
    service.transition(active.id, WorkState.ACTIVE)

    titles = [item.title for item in service.today()]
    assert titles.index("Aktif") < titles.index("Onerilen")
    assert proposed.title in titles


def test_completed_work_is_not_in_today(service: WorkGraphService, project_id: Any) -> None:
    item = _create(service, project_id, "Bitmis")
    for state in (WorkState.READY, WorkState.ACTIVE, WorkState.VERIFICATION):
        service.transition(item.id, state)
    service.transition(
        item.id, WorkState.COMPLETED, evidence=(EvidenceRef(kind="test", reference="ok"),)
    )
    assert "Bitmis" not in [entry.title for entry in service.today()]


def test_next_actionable_skips_blocked_work(service: WorkGraphService, project_id: Any) -> None:
    blocker = _create(service, project_id, "Blocker")
    blocked = _create(service, project_id, "Bloklu")
    free = _create(service, project_id, "Serbest")
    service.relate(source_id=blocker.id, target_id=blocked.id, kind=RelationKind.BLOCKS)
    service.transition(blocked.id, WorkState.READY)
    service.transition(free.id, WorkState.READY)

    candidate = service.next_actionable(project_id)
    assert candidate is not None
    assert candidate.title == "Serbest"


def test_next_actionable_is_none_when_everything_is_blocked(
    service: WorkGraphService, project_id: Any
) -> None:
    blocker = _create(service, project_id, "Blocker")
    blocked = _create(service, project_id, "Bloklu")
    service.relate(source_id=blocker.id, target_id=blocked.id, kind=RelationKind.BLOCKS)
    service.transition(blocked.id, WorkState.READY)
    assert service.next_actionable(project_id) is None


def test_where_did_we_stop_answers_from_work_graph(
    service: WorkGraphService, project_id: Any
) -> None:
    item = _create(service, project_id, "Devam eden")
    service.transition(item.id, WorkState.READY)
    service.transition(item.id, WorkState.ACTIVE)

    answer = service.where_did_we_stop()
    assert answer["source"] == "work-graph"
    assert answer["next_actionable"]["title"] == "Devam eden"
    assert "Devam eden" in answer["next_safe_action"]
    assert answer["recent_activity"][0]["event_type"] == "work.state.active"


def test_where_did_we_stop_reports_empty_backlog(service: WorkGraphService) -> None:
    answer = service.where_did_we_stop()
    assert answer["next_actionable"] is None
    assert "Acik is yok" in answer["next_safe_action"]


def test_snapshot_reports_dependencies_and_blockers(
    service: WorkGraphService, project_id: Any
) -> None:
    dependency = _create(service, project_id, "Bagimlilik")
    blocker = _create(service, project_id, "Blocker")
    item = _create(service, project_id, "Hedef")
    service.relate(source_id=item.id, target_id=dependency.id, kind=RelationKind.DEPENDS_ON)
    service.relate(source_id=blocker.id, target_id=item.id, kind=RelationKind.BLOCKS)

    snapshot = service.snapshot(item.id)
    assert snapshot.unmet_dependencies == (dependency.id,)
    assert snapshot.blockers == (blocker.id,)
    assert not snapshot.is_actionable


def test_missing_work_item_raises_not_found(service: WorkGraphService) -> None:
    with pytest.raises(NotFound):
        service.items.get(uuid4())
