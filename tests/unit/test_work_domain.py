"""Work Graph alan kurallari: durum makinesi, kanit zorunlulugu, plan DAG'i."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.work import (
    ALLOWED_TRANSITIONS,
    OPEN_STATES,
    TERMINAL_STATES,
    AcceptanceCriterion,
    Decision,
    DecisionOption,
    EffectKind,
    EvidenceRef,
    Intent,
    PlanStep,
    RelationKind,
    TaskPlan,
    WorkItem,
    WorkRelation,
    WorkState,
    WorkType,
    assert_acyclic_steps,
    assert_transition,
    can_transition,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.UTC)
REALM = uuid4()
PROJECT = uuid4()
POLICY_DIGEST = "sha256:" + "a" * 64


def _item(state: WorkState = WorkState.PROPOSED, **overrides: object) -> WorkItem:
    base = WorkItem.create(
        realm_id=REALM,
        project_id=PROJECT,
        type=WorkType.TASK,
        title="Ornek is",
        now=NOW,
    )
    if state is WorkState.PROPOSED and not overrides:
        return base
    evidence = (
        (EvidenceRef(kind="test", reference="pytest"),) if state is WorkState.COMPLETED else ()
    )
    return WorkItem(
        id=base.id,
        realm_id=base.realm_id,
        project_id=base.project_id,
        type=base.type,
        state=state,
        title=base.title,
        acceptance_evidence=evidence,
        created_at=NOW,
        updated_at=NOW,
        **overrides,  # type: ignore[arg-type]
    )


# -- durum makinesi -----------------------------------------------------------------


def test_every_state_has_a_transition_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(WorkState)


def test_open_and_terminal_states_partition_the_state_set() -> None:
    assert OPEN_STATES.isdisjoint(TERMINAL_STATES)
    assert set(WorkState) == OPEN_STATES | TERMINAL_STATES


def test_archived_is_a_dead_end() -> None:
    assert ALLOWED_TRANSITIONS[WorkState.ARCHIVED] == frozenset()


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkState.PROPOSED, WorkState.READY),
        (WorkState.READY, WorkState.ACTIVE),
        (WorkState.ACTIVE, WorkState.VERIFICATION),
        (WorkState.VERIFICATION, WorkState.COMPLETED),
        (WorkState.BLOCKED, WorkState.READY),
        (WorkState.COMPLETED, WorkState.ACTIVE),
    ],
)
def test_allowed_transitions(current: WorkState, target: WorkState) -> None:
    assert can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkState.PROPOSED, WorkState.COMPLETED),
        (WorkState.PROPOSED, WorkState.ACTIVE),
        (WorkState.READY, WorkState.COMPLETED),
        (WorkState.ARCHIVED, WorkState.ACTIVE),
        (WorkState.CANCELLED, WorkState.ACTIVE),
    ],
)
def test_forbidden_transitions_are_rejected(current: WorkState, target: WorkState) -> None:
    assert not can_transition(current, target)
    with pytest.raises(PolicyViolation, match="Izinsiz durum gecisi"):
        assert_transition(current, target)


def test_same_state_transition_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        assert_transition(WorkState.READY, WorkState.READY)


# -- Work Item ------------------------------------------------------------------------


def test_new_item_starts_proposed_with_revision_one() -> None:
    item = _item()
    assert item.state is WorkState.PROPOSED
    assert item.revision == 1
    assert item.is_open
    assert not item.is_terminal


def test_blank_title_is_rejected() -> None:
    with pytest.raises(ValidationFailed):
        WorkItem.create(
            realm_id=REALM, project_id=PROJECT, type=WorkType.TASK, title="   ", now=NOW
        )


def test_completed_without_evidence_is_rejected_at_construction() -> None:
    with pytest.raises(PolicyViolation, match="Acceptance evidence"):
        WorkItem(
            id=uuid4(),
            realm_id=REALM,
            project_id=PROJECT,
            type=WorkType.TASK,
            state=WorkState.COMPLETED,
            title="Kanitsiz",
            created_at=NOW,
            updated_at=NOW,
        )


def test_completed_without_evidence_is_rejected_at_transition() -> None:
    item = _item(WorkState.VERIFICATION)
    with pytest.raises(PolicyViolation, match="Acceptance evidence"):
        item.with_state(WorkState.COMPLETED, now=NOW)


def test_completed_with_evidence_is_accepted() -> None:
    item = _item(WorkState.VERIFICATION)
    completed = item.with_state(
        WorkState.COMPLETED,
        evidence=(EvidenceRef(kind="test", reference="pytest: 390 passed"),),
        now=NOW,
    )
    assert completed.state is WorkState.COMPLETED
    assert completed.revision == item.revision + 1
    assert completed.is_terminal


def test_transition_preserves_identity_and_creation_time() -> None:
    item = _item()
    moved = item.with_state(WorkState.READY, now=NOW)
    assert moved.id == item.id
    assert moved.created_at == item.created_at


def test_record_digest_changes_with_state() -> None:
    item = _item()
    assert item.with_state(WorkState.READY, now=NOW).record_digest != item.record_digest


def test_record_digest_is_deterministic() -> None:
    item = _item()
    assert item.record_digest == item.record_digest
    assert item.as_dict()["record_digest"] == item.record_digest


def test_update_details_increments_revision_without_changing_state() -> None:
    item = _item(WorkState.READY)
    updated = item.with_details(title="Yeni baslik", now=NOW)
    assert updated.title == "Yeni baslik"
    assert updated.state is item.state
    assert updated.revision == item.revision + 1


def test_acceptance_criterion_cannot_be_blank() -> None:
    with pytest.raises(ValidationFailed):
        AcceptanceCriterion(text="  ")


def test_evidence_reference_cannot_be_blank() -> None:
    with pytest.raises(ValidationFailed):
        EvidenceRef(kind="test", reference="")


# -- iliskiler --------------------------------------------------------------------------


def test_relation_between_items_of_same_project_is_allowed() -> None:
    source, target = _item(), _item()
    relation = WorkRelation.create(
        source=source, target=target, kind=RelationKind.DEPENDS_ON, now=NOW
    )
    assert relation.kind is RelationKind.DEPENDS_ON
    assert relation.project_id == PROJECT


def test_self_relation_is_rejected() -> None:
    item = _item()
    with pytest.raises(ValidationFailed):
        WorkRelation.create(source=item, target=item, kind=RelationKind.BLOCKS, now=NOW)


def test_cross_project_relation_is_rejected() -> None:
    source = _item()
    target = WorkItem.create(
        realm_id=REALM, project_id=uuid4(), type=WorkType.TASK, title="Baska proje", now=NOW
    )
    with pytest.raises(PolicyViolation, match="Cross-project"):
        WorkRelation.create(source=source, target=target, kind=RelationKind.BLOCKS, now=NOW)


def test_cross_realm_relation_is_rejected() -> None:
    source = _item()
    target = WorkItem.create(
        realm_id=uuid4(), project_id=PROJECT, type=WorkType.TASK, title="Baska realm", now=NOW
    )
    with pytest.raises(PolicyViolation, match="Cross-realm"):
        WorkRelation.create(source=source, target=target, kind=RelationKind.BLOCKS, now=NOW)


# -- Intent ve Decision -------------------------------------------------------------------


def test_intent_digest_is_deterministic_and_content_sensitive() -> None:
    item = _item()
    first = Intent.create(work_item=item, goal="Hedef", revision=1, now=NOW)
    same = Intent.create(work_item=item, goal="Hedef", revision=1, now=NOW)
    other = Intent.create(work_item=item, goal="Baska hedef", revision=1, now=NOW)
    assert first.intent_digest == same.intent_digest
    assert first.intent_digest != other.intent_digest


def test_intent_requires_goal() -> None:
    with pytest.raises(ValidationFailed):
        Intent.create(work_item=_item(), goal="   ", revision=1, now=NOW)


def test_decision_requires_question_choice_and_rationale() -> None:
    item = _item()
    for question, choice, rationale in (
        ("", "A", "cunku"),
        ("Soru", "", "cunku"),
        ("Soru", "A", ""),
    ):
        with pytest.raises(ValidationFailed):
            Decision.create(
                work_item=item,
                revision=1,
                question=question,
                chosen_option=choice,
                rationale=rationale,
                now=NOW,
            )


def test_decision_records_alternatives_and_evidence() -> None:
    decision = Decision.create(
        work_item=_item(),
        revision=1,
        question="Hangi kuyruk?",
        chosen_option="PostgreSQL",
        rationale="Tek kanonik store",
        alternatives=(DecisionOption(name="Redis", rejected_because="Ayri state"),),
        criteria=("dayaniklilik", "sadelik"),
        evidence=(EvidenceRef(kind="doc", reference="mimari/ANA_MIMARI.md"),),
        now=NOW,
    )
    document = decision.as_dict()
    assert document["alternatives"][0]["rejected_because"] == "Ayri state"
    assert document["criteria"] == ["dayaniklilik", "sadelik"]
    assert decision.decision_digest.startswith("sha256:")


# -- Task Plan ----------------------------------------------------------------------------


def _steps() -> tuple[PlanStep, ...]:
    return (
        PlanStep(step_id="hazirla", title="Kaynak oku", effect=EffectKind.NONE),
        PlanStep(
            step_id="yaz",
            title="Dosya yaz",
            effect=EffectKind.FILE_WRITE,
            logical_resources=("path:zekam:src/zekam/x.py",),
            depends_on=("hazirla",),
        ),
        PlanStep(
            step_id="test",
            title="Test calistir",
            effect=EffectKind.PROCESS_RUN,
            depends_on=("yaz",),
        ),
    )


#: Plan testleri ayni is kaydini kullanir; digest kimlige bagli oldugu icin
#: her cagrida yeni bir is kaydi uretmek karsilastirmayi anlamsiz kilar.
_PLAN_ITEM = _item()


def _plan(steps: tuple[PlanStep, ...] | None = None) -> TaskPlan:
    return TaskPlan.create(
        work_item=_PLAN_ITEM,
        revision=1,
        source_revision="sha256:" + "b" * 64,
        policy_digest=POLICY_DIGEST,
        steps=_steps() if steps is None else steps,
        now=NOW,
    )


def test_plan_requires_at_least_one_step() -> None:
    with pytest.raises(ValidationFailed):
        _plan(())


def test_plan_execution_order_is_topological() -> None:
    assert _plan().execution_order == ("hazirla", "yaz", "test")


def test_plan_with_cycle_is_rejected() -> None:
    steps = (
        PlanStep(step_id="a", title="A", effect=EffectKind.NONE, depends_on=("b",)),
        PlanStep(step_id="b", title="B", effect=EffectKind.NONE, depends_on=("a",)),
    )
    with pytest.raises(ValidationFailed, match="dongu"):
        assert_acyclic_steps(steps)


def test_plan_with_unknown_dependency_is_rejected() -> None:
    steps = (PlanStep(step_id="a", title="A", effect=EffectKind.NONE, depends_on=("yok",)),)
    with pytest.raises(ValidationFailed, match="tanimsiz"):
        assert_acyclic_steps(steps)


def test_duplicate_step_ids_are_rejected() -> None:
    steps = (
        PlanStep(step_id="a", title="A", effect=EffectKind.NONE),
        PlanStep(step_id="a", title="A tekrar", effect=EffectKind.NONE),
    )
    with pytest.raises(ValidationFailed, match="tekil"):
        assert_acyclic_steps(steps)


def test_step_cannot_depend_on_itself() -> None:
    with pytest.raises(ValidationFailed):
        PlanStep(step_id="a", title="A", effect=EffectKind.NONE, depends_on=("a",))


def test_effect_digest_ignores_pure_read_steps() -> None:
    effect_steps = (
        PlanStep(
            step_id="yaz",
            title="Dosya yaz",
            effect=EffectKind.FILE_WRITE,
            logical_resources=("path:zekam:src/zekam/x.py",),
        ),
        PlanStep(step_id="test", title="Test calistir", effect=EffectKind.PROCESS_RUN),
    )
    with_read = _plan(
        (PlanStep(step_id="oku", title="Kaynak oku", effect=EffectKind.NONE), *effect_steps)
    )
    assert with_read.effect_digest == _plan(effect_steps).effect_digest


def test_effect_digest_changes_when_resource_changes() -> None:
    original = _plan()
    changed_steps = (
        _steps()[0],
        PlanStep(
            step_id="yaz",
            title="Dosya yaz",
            effect=EffectKind.FILE_WRITE,
            logical_resources=("path:zekam:src/zekam/baska.py",),
            depends_on=("hazirla",),
        ),
        _steps()[2],
    )
    assert _plan(changed_steps).effect_digest != original.effect_digest


def test_effect_digest_is_order_independent() -> None:
    forward = _plan(_steps())
    reversed_steps = tuple(reversed(_steps()))
    assert _plan(reversed_steps).effect_digest == forward.effect_digest


def test_plan_never_grants_authority() -> None:
    assert _plan().body()["grants_authority"] is False
    assert "grants_authority" in _plan().as_dict()


def test_plan_reports_required_authorization_and_resources() -> None:
    plan = _plan()
    assert plan.requires_authorization
    assert plan.writable_resources == ("path:zekam:src/zekam/x.py",)


def test_read_only_plan_needs_no_authorization() -> None:
    plan = _plan((PlanStep(step_id="oku", title="Oku", effect=EffectKind.NONE),))
    assert not plan.requires_authorization
    assert plan.writable_resources == ()


def test_plan_digest_is_deterministic() -> None:
    assert _plan().plan_digest == _plan().plan_digest


def test_plan_detects_source_drift() -> None:
    plan = _plan()
    assert not plan.has_drifted_from(
        source_revision=plan.source_revision, policy_digest=POLICY_DIGEST
    )
    assert plan.has_drifted_from(source_revision="sha256:" + "c" * 64, policy_digest=POLICY_DIGEST)
    assert plan.has_drifted_from(
        source_revision=plan.source_revision, policy_digest="sha256:" + "d" * 64
    )


def test_plan_requires_source_revision() -> None:
    with pytest.raises(ValidationFailed):
        TaskPlan.create(
            work_item=_item(),
            revision=1,
            source_revision="  ",
            policy_digest=POLICY_DIGEST,
            steps=_steps(),
            now=NOW,
        )
