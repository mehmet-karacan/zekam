from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from zekam.application.memory_promotion import MemoryPromotionService
from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.memory import (
    MemoryCandidate,
    MemoryClass,
    MemoryEvidence,
    MemoryKey,
    MemoryScope,
)
from zekam.domain.memory_promotion import MemoryReviewDecision
from zekam.domain.realm import Actor, ActorKind, Realm
from zekam.domain.security import Authorization, AuthorizationScope
from zekam.infrastructure.postgres import migrations
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import ActorRepository, RealmRepository
from zekam.infrastructure.postgres.memory_promotion_repository import (
    MemoryPromotionRepository,
)
from zekam.infrastructure.postgres.memory_repository import MemoryRepository
from zekam.infrastructure.postgres.security_repository import (
    AuditRepository,
    AuthorizationRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 24, 18, tzinfo=dt.UTC)
EVIDENCE_DIGEST = digest("promotion-evidence")
PROFILE_DIGEST = digest("embedding-profile")


def _setup(
    realm: Any, connection: Any, tmp_path: Path
) -> tuple[MemoryRepository, MemoryPromotionService, Actor]:
    source = tmp_path / "memory-promotion-source"
    source.mkdir(exist_ok=True)
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    memory = MemoryRepository(
        connection,
        realm.id,
        realm.slug,
        project.id,
        project.slug,
    )
    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="memory-reviewer", now=NOW)
    )
    authorizations = AuthorizationRepository(connection, realm.id)
    service = MemoryPromotionService(
        MemoryPromotionRepository(connection, realm.id, realm.slug),
        authorizations,
        AuditRepository(connection, realm.id),
    )
    return memory, service, actor


def _candidate(candidate_id: str, content: str, *, project_ref: str) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id=candidate_id,
        key=MemoryKey(MemoryScope.PROJECT, "ignored", project_ref=project_ref),
        memory_class=MemoryClass.SEMANTIC,
        content=content,
        author_ref="builder-a",
        observed_at=NOW,
        evidence=(MemoryEvidence("test", f"tests/{candidate_id}.py", EVIDENCE_DIGEST),),
    )


def _review() -> MemoryReviewDecision:
    return MemoryReviewDecision(
        approved=True,
        reviewer_ref="verifier-b",
        reason="bagimsiz kanit gecti",
        decided_at=NOW,
        policy_digest=digest("memory-policy"),
    )


def _authorize(service: MemoryPromotionService, actor: Actor, plan: Any) -> Authorization:
    authorization = Authorization.issue(
        realm_id=service.repository.realm_id,
        actor_id=actor.id,
        plan_digest=plan.plan_digest,
        effect_digest=plan.effect_digest,
        scope=AuthorizationScope(
            allowed_resources=plan.resources,
            allowed_effects=("database-write",),
        ),
        risk="medium",
        lifetime=dt.timedelta(minutes=5),
        now=NOW,
    )
    return service.authorizations.issue(authorization)


def test_promotion_writes_complete_chain_in_one_transaction(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    memory, service, actor = _setup(realm, connection, tmp_path)
    assert memory.project_ref is not None
    candidate = _candidate(
        "candidate-atomic",
        "Memory promotion atomik zincirdir",
        project_ref=memory.project_ref,
    )
    candidate_storage_id = memory.store_candidate(candidate)

    with connection.cursor() as cursor:
        cursor.execute("select count(*) from memory.review")
        before = int(cursor.fetchone()[0])
    plan = service.prepare(
        candidate_id=candidate.candidate_id,
        logical_memory_id="memory-family-atomic",
        predecessor_storage_id=None,
        review=_review(),
        embedding_profile_digest=PROFILE_DIGEST,
        external_target_ref="mem0:local",
        now=NOW,
    )
    with connection.cursor() as cursor:
        cursor.execute("select count(*) from memory.review")
        assert int(cursor.fetchone()[0]) == before, "prepare mutation yapmamali"

    receipt = service.apply(plan, authorization_id=_authorize(service, actor, plan).id, now=NOW)
    assert receipt.revision == 1
    with connection.cursor() as cursor:
        cursor.execute(
            "select reviewed,promoted_record_id,promotion_plan_digest from memory.candidate"
            " where id=%s",
            (candidate_storage_id,),
        )
        assert cursor.fetchone() == (True, receipt.record_storage_id, plan.plan_digest)
        for table, expected in (
            ("promotion_plan", 1),
            ("review", 1),
            ("revision", 1),
            ("evidence_link", 1),
            ("promotion_outbox", 2),
            ("promotion_receipt", 1),
        ):
            cursor.execute(f"select count(*) from memory.{table}")
            assert int(cursor.fetchone()[0]) == expected
        cursor.execute(
            "select array_agg(kind order by kind) from memory.promotion_outbox where record_id=%s",
            (receipt.record_storage_id,),
        )
        assert cursor.fetchone()[0] == ["embedding", "external-sync"]
        cursor.execute(
            "select state from security.authorization where id=%s",
            (receipt.authorization_id,),
        )
        assert cursor.fetchone()[0] == "consumed"
        cursor.execute(
            "select count(*) from security.audit_event where action='memory.promotion.applied'"
            " and subject_id=%s",
            (plan.plan_digest,),
        )
        assert int(cursor.fetchone()[0]) == 1


def test_mid_transaction_outbox_failure_rolls_back_every_promotion_effect(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    memory, service, actor = _setup(realm, connection, tmp_path)
    assert memory.project_ref is not None
    candidate = _candidate(
        "candidate-rollback",
        "Outbox hatasi promotionu geri alir",
        project_ref=memory.project_ref,
    )
    candidate_storage_id = memory.store_candidate(candidate)
    plan = service.prepare(
        candidate_id=candidate.candidate_id,
        logical_memory_id="memory-family-rollback",
        predecessor_storage_id=None,
        review=_review(),
        embedding_profile_digest=PROFILE_DIGEST,
        external_target_ref="mem0:local",
        now=NOW,
    )
    authorization = _authorize(service, actor, plan)
    with connection.cursor() as cursor:
        cursor.execute("reset role")
        cursor.execute(
            "create function memory.test_reject_external_outbox() returns trigger language plpgsql"
            " as $$ begin if new.kind='external-sync' then raise exception 'test fault'; end if;"
            " return new; end $$"
        )
        cursor.execute(
            "create trigger test_reject_external_outbox before insert on memory.promotion_outbox"
            " for each row execute function memory.test_reject_external_outbox()"
        )
        cursor.execute("set role zekam_app")
    connection.commit()
    try:
        with pytest.raises(Exception, match="test fault"):
            service.apply(plan, authorization_id=authorization.id, now=NOW)
        with connection.cursor() as cursor:
            for table in (
                "promotion_plan",
                "review",
                "revision",
                "evidence_link",
                "promotion_outbox",
                "promotion_receipt",
            ):
                cursor.execute(f"select count(*) from memory.{table}")
                assert int(cursor.fetchone()[0]) == 0
            cursor.execute(
                "select promoted_record_id from memory.candidate where id=%s",
                (candidate_storage_id,),
            )
            assert cursor.fetchone()[0] is None
            cursor.execute(
                "select state from security.authorization where id=%s", (authorization.id,)
            )
            assert cursor.fetchone()[0] == "issued"
    finally:
        with connection.cursor() as cursor:
            cursor.execute("reset role")
            cursor.execute("drop trigger test_reject_external_outbox on memory.promotion_outbox")
            cursor.execute("drop function memory.test_reject_external_outbox()")
            cursor.execute("set role zekam_app")
        connection.commit()


def test_revision_supersession_and_exact_authorization_are_bound(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    memory, service, actor = _setup(realm, connection, tmp_path)
    assert memory.project_ref is not None
    first_candidate = _candidate(
        "candidate-revision-1", "Ilk memory revision", project_ref=memory.project_ref
    )
    memory.store_candidate(first_candidate)
    first_plan = service.prepare(
        candidate_id=first_candidate.candidate_id,
        logical_memory_id="memory-family-revision",
        predecessor_storage_id=None,
        review=_review(),
        embedding_profile_digest=PROFILE_DIGEST,
        external_target_ref="mem0:local",
        now=NOW,
    )
    first = service.apply(
        first_plan, authorization_id=_authorize(service, actor, first_plan).id, now=NOW
    )

    second_candidate = _candidate(
        "candidate-revision-2", "Ikinci memory revision", project_ref=memory.project_ref
    )
    memory.store_candidate(second_candidate)
    second_plan = service.prepare(
        candidate_id=second_candidate.candidate_id,
        logical_memory_id="memory-family-revision",
        predecessor_storage_id=first.record_storage_id,
        review=replace(_review(), decided_at=NOW + dt.timedelta(seconds=1)),
        embedding_profile_digest=PROFILE_DIGEST,
        external_target_ref="mem0:local",
        now=NOW + dt.timedelta(seconds=1),
    )
    stale_authorization = _authorize(service, actor, second_plan)
    with pytest.raises(Exception, match="authorization binding"):
        service.apply(
            replace(second_plan, external_target_ref="mem0:drift"),
            authorization_id=stale_authorization.id,
            now=NOW + dt.timedelta(seconds=1),
        )
    revision_drift = replace(second_plan, next_revision=999)
    revision_drift_authorization = _authorize(service, actor, revision_drift)
    with pytest.raises(PolicyViolation, match="plan drift"):
        service.apply(
            revision_drift,
            authorization_id=revision_drift_authorization.id,
            now=NOW + dt.timedelta(seconds=1),
        )
    second = service.apply(
        second_plan,
        authorization_id=_authorize(service, actor, second_plan).id,
        now=NOW + dt.timedelta(seconds=1),
    )
    assert second.revision == 2
    with connection.cursor() as cursor:
        cursor.execute(
            "select revision,state,superseded_by from memory.record where id=%s",
            (first.record_storage_id,),
        )
        assert cursor.fetchone() == (1, "superseded", second.record_storage_id)
        cursor.execute(
            "select revision,state,predecessor_id from memory.record where id=%s",
            (second.record_storage_id,),
        )
        assert cursor.fetchone() == (2, "active", first.record_storage_id)
        cursor.execute(
            "select count(*) from memory.relation where from_id=%s and to_id=%s"
            " and kind='supersedes'",
            (second.record_storage_id, first.record_storage_id),
        )
        assert int(cursor.fetchone()[0]) == 1
        cursor.execute(
            "select state from security.authorization where id=%s", (stale_authorization.id,)
        )
        assert cursor.fetchone()[0] == "issued"
        cursor.execute(
            "select state from security.authorization where id=%s",
            (revision_drift_authorization.id,),
        )
        assert cursor.fetchone()[0] == "issued"


def test_two_concurrent_promoters_have_exactly_one_winner(
    realm_session: tuple[Any, Any], migrated_database: Any, tmp_path: Path
) -> None:
    realm, connection = realm_session
    memory, service, actor = _setup(realm, connection, tmp_path)
    assert memory.project_ref is not None
    candidate = _candidate("candidate-race", "Tek promoter kazanir", project_ref=memory.project_ref)
    memory.store_candidate(candidate)
    plan = service.prepare(
        candidate_id=candidate.candidate_id,
        logical_memory_id="memory-family-race",
        predecessor_storage_id=None,
        review=_review(),
        embedding_profile_digest=PROFILE_DIGEST,
        external_target_ref="mem0:local",
        now=NOW,
    )
    authorization_ids = tuple(_authorize(service, actor, plan).id for _ in range(2))
    connection.commit()

    def promote(authorization_id: Any) -> str:
        with connect(migrated_database) as worker:
            configure_session(worker, realm_id=realm.id)
            worker_service = MemoryPromotionService(
                MemoryPromotionRepository(worker, realm.id, realm.slug),
                AuthorizationRepository(worker, realm.id),
                AuditRepository(worker, realm.id),
            )
            try:
                worker_service.apply(plan, authorization_id=authorization_id, now=NOW)
                return "won"
            except PolicyViolation:
                return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(promote, authorization_ids))
    assert sorted(outcomes) == ["lost", "won"]
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from memory.promotion_receipt where plan_digest=%s",
            (plan.plan_digest,),
        )
        assert int(cursor.fetchone()[0]) == 1
        cursor.execute(
            "select state,count(*) from security.authorization where id=any(%s) group by state",
            (list(authorization_ids),),
        )
        assert dict(cursor.fetchall()) == {"consumed": 1, "issued": 1}


@pytest.mark.parametrize(
    ("target", "mutation", "expected"),
    (
        (
            "record",
            "new.content := 'forged record content';",
            "candidate/record canonical mismatch",
        ),
        ("review", "new.decision := 'rejected';", "review provenance mismatch"),
        ("evidence_link", "new.evidence_ref := 'forged';", "evidence content mismatch"),
        (
            "promotion_outbox",
            "new.payload_digest := 'sha256:' || repeat('b',64);",
            "outbox payload mismatch",
        ),
    ),
)
def test_database_rejects_forged_normalized_provenance(
    realm_session: tuple[Any, Any],
    tmp_path: Path,
    target: str,
    mutation: str,
    expected: str,
) -> None:
    realm, connection = realm_session
    memory, service, actor = _setup(realm, connection, tmp_path)
    assert memory.project_ref is not None
    candidate = _candidate(
        f"candidate-forge-{target}",
        f"DB forged {target} provenance reddi",
        project_ref=memory.project_ref,
    )
    memory.store_candidate(candidate)
    plan = service.prepare(
        candidate_id=candidate.candidate_id,
        logical_memory_id=f"memory-family-forge-{target}",
        predecessor_storage_id=None,
        review=_review(),
        embedding_profile_digest=PROFILE_DIGEST,
        external_target_ref="mem0:local",
        now=NOW,
    )
    authorization = _authorize(service, actor, plan)
    function_name = f"test_forge_{target}"
    trigger_name = f"test_forge_{target}"
    with connection.cursor() as cursor:
        cursor.execute("reset role")
        cursor.execute(
            f"create function memory.{function_name}() returns trigger language plpgsql"
            f" as $$ begin {mutation} return new; end $$"
        )
        cursor.execute(
            f"create trigger {trigger_name} before insert on memory.{target}"
            f" for each row execute function memory.{function_name}()"
        )
        cursor.execute("set role zekam_app")
    connection.commit()
    try:
        with pytest.raises(Exception, match=expected):
            service.apply(plan, authorization_id=authorization.id, now=NOW)
        with connection.cursor() as cursor:
            cursor.execute(
                "select state from security.authorization where id=%s", (authorization.id,)
            )
            assert cursor.fetchone()[0] == "issued"
            cursor.execute(
                "select count(*) from memory.promotion_receipt where plan_digest=%s",
                (plan.plan_digest,),
            )
            assert int(cursor.fetchone()[0]) == 0
    finally:
        with connection.cursor() as cursor:
            cursor.execute("reset role")
            cursor.execute(f"drop trigger {trigger_name} on memory.{target}")
            cursor.execute(f"drop function memory.{function_name}()")
            cursor.execute("set role zekam_app")
        connection.commit()


def test_memory_v2_tables_are_rls_and_provenance_is_append_only(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    memory, service, actor = _setup(realm, connection, tmp_path)
    assert memory.project_ref is not None
    candidate = _candidate(
        "candidate-immutable", "Normalized provenance immutable", project_ref=memory.project_ref
    )
    memory.store_candidate(candidate)
    plan = service.prepare(
        candidate_id=candidate.candidate_id,
        logical_memory_id="memory-family-immutable",
        predecessor_storage_id=None,
        review=_review(),
        embedding_profile_digest=PROFILE_DIGEST,
        external_target_ref="mem0:local",
        now=NOW,
    )
    receipt = service.apply(plan, authorization_id=_authorize(service, actor, plan).id, now=NOW)
    tables = (
        "promotion_plan",
        "review",
        "revision",
        "evidence_link",
        "promotion_outbox",
        "promotion_receipt",
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "select relname,relrowsecurity,relforcerowsecurity from pg_class c"
            " join pg_namespace n on n.oid=c.relnamespace"
            " where n.nspname='memory' and relname=any(%s)",
            (list(tables),),
        )
        rows = cursor.fetchall()
        assert {str(row[0]) for row in rows} == set(tables)
        assert all(bool(row[1]) and bool(row[2]) for row in rows)
    with (
        pytest.raises(Exception, match=r"permission denied|degistirilemez"),
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "update memory.review set reviewer_ref='forged' where id=%s",
            (receipt.review_id,),
        )
    with (
        pytest.raises(Exception, match=r"permission denied|payload identity degistirilemez"),
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "update memory.promotion_outbox set payload_digest=%s where record_id=%s",
            (digest("forged"), receipt.record_storage_id),
        )


def test_multi_revision_downgrade_fails_closed_without_data_loss(
    isolated_migrated_database: Any, tmp_path: Path
) -> None:
    realm = Realm.create(slug="memory-down", display_name="Memory down", now=NOW)
    with connect(isolated_migrated_database) as connection:
        configure_session(connection, realm_id=realm.id, role=None)
        RealmRepository(connection).create(realm)
        configure_session(connection, realm_id=realm.id)
        memory, service, actor = _setup(realm, connection, tmp_path)
        assert memory.project_ref is not None
        first_candidate = _candidate(
            "candidate-down-1", "Down revision bir", project_ref=memory.project_ref
        )
        memory.store_candidate(first_candidate)
        first_plan = service.prepare(
            candidate_id=first_candidate.candidate_id,
            logical_memory_id="memory-family-down",
            predecessor_storage_id=None,
            review=_review(),
            embedding_profile_digest=PROFILE_DIGEST,
            external_target_ref="mem0:local",
            now=NOW,
        )
        first = service.apply(
            first_plan, authorization_id=_authorize(service, actor, first_plan).id, now=NOW
        )
        second_candidate = _candidate(
            "candidate-down-2", "Down revision iki", project_ref=memory.project_ref
        )
        memory.store_candidate(second_candidate)
        second_plan = service.prepare(
            candidate_id=second_candidate.candidate_id,
            logical_memory_id="memory-family-down",
            predecessor_storage_id=first.record_storage_id,
            review=replace(_review(), decided_at=NOW + dt.timedelta(seconds=1)),
            embedding_profile_digest=PROFILE_DIGEST,
            external_target_ref="mem0:local",
            now=NOW + dt.timedelta(seconds=1),
        )
        service.apply(
            second_plan,
            authorization_id=_authorize(service, actor, second_plan).id,
            now=NOW + dt.timedelta(seconds=1),
        )
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute("reset role")
        with pytest.raises(Exception, match="forward-fix: multi-revision family exists"):
            migrations.downgrade(connection, target=38)
        assert migrations.status(connection).head == 38
        with connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from memory.record where realm_id=%s"
                " and logical_memory_id='memory-family-down'",
                (realm.id,),
            )
            assert int(cursor.fetchone()[0]) == 2
