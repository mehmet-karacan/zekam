"""P08 context continuity PostgreSQL kabul testleri."""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation

from zekam.application.context_continuity_service import ContextContinuityService
from zekam.application.context_materializer import FragmentMaterialization, materialize_fragments
from zekam.application.context_recipe import ContextRecipeRole
from zekam.application.execution import ExecutionHost
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    Checkpoint,
    ContextCandidate,
    ContextCandidateKind,
    ContinuitySnapshot,
    EvidenceReference,
    FinalizedHandoff,
    JournalEntry,
    compile_context,
)
from zekam.domain.context_fragment import (
    ContextContentKind,
    ContextFragmentSet,
    ContextRole,
    ContextVisibility,
)
from zekam.domain.context_scoring import ContextCompilerMetricsV2
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation
from zekam.domain.runtime import AttemptOutcome, Job, JobKind
from zekam.domain.work import EffectKind, PlanStep, WorkType
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.context_ranking_repository import ContextRankingRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)


def _insert_context_source_revisions(cursor: Any, realm_id: Any, work_item_id: Any) -> None:
    entity_types = (
        "context.system_policy",
        "context.run_status",
        "context.architecture_rule",
        "context.dependency_manifest",
        "context.source_slice",
        "context.source_diff",
        "context.effect_receipt",
        "context.test_evidence",
    )
    for entity_type in entity_types:
        payload = {
            "schema": "zekam-context-source/v1",
            "entity_type": entity_type,
            "work_item_id": str(work_item_id),
            "evidence": f"canonical evidence for {entity_type}",
        }
        cursor.execute(
            "insert into core.revision"
            " (id,realm_id,entity_type,entity_id,revision,payload,payload_digest,"
            " previous_digest,reason,recorded_at)"
            " values (%s,%s,%s,%s,1,%s,%s,null,'context compiler acceptance',%s)",
            (
                uuid4(),
                realm_id,
                entity_type,
                work_item_id,
                canonical_json(payload),
                digest(payload),
                NOW,
            ),
        )


def test_context_ranking_snapshot_yalniz_current_canonical_assignmenttan_uretilir(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "ranking-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    work = WorkGraphService(connection, realm)
    item = work.create_item(
        project_id=project.id,
        type=WorkType.TASK,
        title="Oracle migration inspect",
    )
    plan = work.create_plan(
        item.id,
        source_revision="revision/current",
        policy_digest=digest("policy/ranking"),
        steps=(PlanStep("inspect", "Inspect", EffectKind.NONE),),
    )
    assignment_id = uuid4()
    assignment_digest = digest("assignment/ranking")
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into agents.assignment"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,role,agent_ref,status,"
            " risk,instruction_digest,context_manifest_digest,assignment_digest,created_at)"
            " values (%s,%s,%s,%s,%s,'inspect','coordinator','coordinator','active',"
            " 'medium',%s,%s,%s,%s)",
            (
                assignment_id,
                realm.id,
                project.id,
                item.id,
                plan.id,
                digest("instruction/ranking"),
                digest("context/ranking"),
                assignment_digest,
                NOW,
            ),
        )
    snapshot = ContextRankingRepository(
        connection, realm.id, project.id, item.id
    ).issue_current_snapshot(assignment_id)
    assert snapshot.request.role == "coordinator"
    assert snapshot.request.current_source_revision == "revision/current"
    assert snapshot.request.work_scope_ref == f"work/{item.id}"
    assert snapshot.assignment_digest == assignment_digest
    assert "oracle" in snapshot.request.task_terms
    ContextRankingRepository(connection, realm.id, project.id, item.id).assert_current_snapshot(
        snapshot
    )
    forged_snapshot_body = snapshot.body()
    forged_snapshot_body["request"]["role"] = "builder"
    with pytest.raises(CheckViolation), connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into work.context_ranking_snapshot"
            " (realm_id,project_id,work_item_id,assignment_id,assignment_digest,"
            " source_snapshot_digest,snapshot_digest,canonical_body,captured_at,expires_at)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                realm.id,
                project.id,
                item.id,
                assignment_id,
                snapshot.assignment_digest,
                snapshot.source_snapshot_digest,
                digest(forged_snapshot_body),
                canonical_json(forged_snapshot_body),
                snapshot.captured_at,
                snapshot.expires_at,
            ),
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "update agents.assignment set status='completed',terminal_at=%s"
            " where realm_id=%s and id=%s",
            (NOW, realm.id, assignment_id),
        )
    with pytest.raises(Exception, match="bulunamadi"):
        ContextRankingRepository(connection, realm.id, project.id, item.id).issue_current_snapshot(
            assignment_id
        )
    with pytest.raises(Exception, match="bulunamadi"):
        ContextRankingRepository(connection, realm.id, project.id, item.id).assert_current_snapshot(
            snapshot
        )


def test_context_compiler_v2_metrics_roundtrip_ve_db_partition_gate(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "compiler-v2-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    item = WorkGraphService(connection, realm).create_item(
        project_id=project.id,
        type=WorkType.TASK,
        title="Compiler v2 metrics",
    )
    plan = WorkGraphService(connection, realm).create_plan(
        item.id,
        source_revision="revision/current",
        policy_digest=digest("policy/compiler-v2"),
        steps=(PlanStep("build", "Build", EffectKind.NONE),),
    )
    assignment_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into agents.assignment"
            " (id,realm_id,project_id,work_item_id,plan_id,step_id,role,agent_ref,status,"
            " risk,instruction_digest,context_manifest_digest,assignment_digest,created_at)"
            " values (%s,%s,%s,%s,%s,'build','coordinator','coordinator','active',"
            " 'medium',%s,%s,%s,%s)",
            (
                assignment_id,
                realm.id,
                project.id,
                item.id,
                plan.id,
                digest("instruction/compiler-v2"),
                digest("context/compiler-v2"),
                digest("assignment/compiler-v2"),
                NOW,
            ),
        )
    ranking_repository = ContextRankingRepository(connection, realm.id, project.id, item.id)
    snapshot = ranking_repository.issue_current_snapshot(assignment_id)
    with pytest.raises(PolicyViolation, match="canonical source eksik"):
        ranking_repository.issue_candidate_set(snapshot)
    with connection.cursor() as cursor:
        _insert_context_source_revisions(cursor, realm.id, item.id)
    candidate_set = ranking_repository.issue_candidate_set(snapshot)
    candidates = candidate_set.candidates
    forged_body = {**candidate_set.body(), "extra": True}
    with pytest.raises(CheckViolation), connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into work.context_candidate_set"
            " (realm_id,project_id,work_item_id,ranking_snapshot_digest,"
            " candidate_set_digest,candidate_fingerprint,candidate_count,candidate_tokens,"
            " canonical_body,captured_at,expires_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                realm.id,
                project.id,
                item.id,
                snapshot.snapshot_digest,
                digest(forged_body),
                candidate_set.candidate_fingerprint,
                len(candidate_set.candidates),
                sum(row.token_count for row in candidates),
                canonical_json(forged_body),
                candidate_set.captured_at,
                candidate_set.expires_at,
            ),
        )
    forged_provenance_body = copy.deepcopy(candidate_set.body())
    forged_provenance_body["candidates"][0]["provenance"]["conflict_refs"] = ["conflict/forged"]
    forged_provenance_body["candidates"][0]["candidate_digest"] = digest(
        forged_provenance_body["candidates"][0]["provenance"]
    )
    forged_provenance_body["candidate_fingerprint"] = digest(
        [row["candidate_digest"] for row in forged_provenance_body["candidates"]]
    )
    with pytest.raises(CheckViolation), connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into work.context_candidate_set"
            " (realm_id,project_id,work_item_id,ranking_snapshot_digest,"
            " candidate_set_digest,candidate_fingerprint,candidate_count,candidate_tokens,"
            " canonical_body,captured_at,expires_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                realm.id,
                project.id,
                item.id,
                snapshot.snapshot_digest,
                digest(forged_provenance_body),
                forged_provenance_body["candidate_fingerprint"],
                len(candidate_set.candidates),
                sum(row.token_count for row in candidates),
                canonical_json(forged_provenance_body),
                candidate_set.captured_at,
                candidate_set.expires_at,
            ),
        )
    refreshed_payload = {
        "schema": "zekam-context-source/v1",
        "entity_type": "context.run_status",
        "work_item_id": str(item.id),
        "evidence": "canonical run status revision two",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "select payload_digest from core.revision"
            " where realm_id=%s and entity_type='context.run_status' and entity_id=%s"
            " and revision=1",
            (realm.id, item.id),
        )
        previous_digest = cursor.fetchone()[0]
        cursor.execute(
            "insert into core.revision"
            " (id,realm_id,entity_type,entity_id,revision,payload,payload_digest,"
            " previous_digest,reason,recorded_at)"
            " values (%s,%s,'context.run_status',%s,2,%s,%s,%s,'status advanced',%s)",
            (
                uuid4(),
                realm.id,
                item.id,
                canonical_json(refreshed_payload),
                digest(refreshed_payload),
                previous_digest,
                NOW + dt.timedelta(seconds=1),
            ),
        )
    with pytest.raises(PolicyViolation, match="source revision stale"):
        ContextContinuityService().compile(
            candidate_set,
            role=ContextRecipeRole.COORDINATOR,
            token_budget=20_000,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
            ranking_snapshot=snapshot,
            repository=ranking_repository,
        )
    candidate_set = ranking_repository.issue_candidate_set(snapshot)
    candidates = candidate_set.candidates
    packet = ContextContinuityService().compile(
        candidate_set,
        role=ContextRecipeRole.COORDINATOR,
        token_budget=20_000,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        ranking_snapshot=snapshot,
        repository=ranking_repository,
    )
    manifest = packet.manifest
    repository = ContextContinuityRepository(connection, realm.id, project.id, item.id)
    manifest_id = repository.store_manifest(manifest)
    assert manifest.compiler_metrics is not None
    with connection.cursor() as cursor:
        cursor.execute(
            "select compiler_version,scoring_policy_digest,compiler_metrics,"
            " compiler_metrics_digest from work.context_manifest"
            " where realm_id=%s and id=%s",
            (realm.id, manifest_id),
        )
        version, policy_digest, metrics, metrics_digest = cursor.fetchone()
        assert version == 2
        assert policy_digest == manifest.scoring_policy_digest
        assert metrics["duplicate_suppressed_tokens"] == 0
        assert metrics_digest == manifest.compiler_metrics.metrics_digest
    empty_metrics = ContextCompilerMetricsV2(
        input_count=0,
        input_tokens=0,
        eligible_count=0,
        eligible_tokens=0,
        selected_count=0,
        selected_tokens=0,
        omitted_count=0,
        omitted_tokens=0,
        required_total=0,
        required_selected=0,
        duplicate_suppressed_count=0,
        duplicate_suppressed_tokens=0,
        token_budget=manifest.token_budget,
        token_utilization_ppm=0,
        token_efficiency_ppm=0,
        duplicate_token_ratio_ppm=0,
        omission_counts=(),
    )
    forged_partition_body = {
        **manifest.body(),
        "selected": [],
        "omitted": [],
        "compiler_metrics": empty_metrics.body(),
    }
    with pytest.raises(CheckViolation), connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into work.context_manifest"
            " (id,realm_id,project_id,work_item_id,token_budget,selected,omitted,"
            " candidate_fingerprint,manifest_digest,compiler_version,scoring_policy_digest,"
            " compiler_metrics,compiler_metrics_digest,compiler_metrics_canonical,"
            " manifest_canonical,ranking_snapshot_digest,candidate_set_digest,"
            " grants_authority,created_at)"
            " values (%s,%s,%s,%s,%s,'[]','[]',%s,%s,2,%s,%s,%s,%s,%s,%s,%s,false,%s)",
            (
                uuid4(),
                realm.id,
                project.id,
                item.id,
                manifest.token_budget,
                manifest.candidate_fingerprint,
                digest(forged_partition_body),
                manifest.scoring_policy_digest,
                canonical_json(empty_metrics.body()),
                empty_metrics.metrics_digest,
                canonical_json(empty_metrics.body()),
                canonical_json(forged_partition_body),
                manifest.ranking_snapshot_digest,
                manifest.candidate_set_digest,
                manifest.created_at,
            ),
        )
    forged_semantic_body = copy.deepcopy(manifest.body())
    forged_semantic_body["selected"][0]["kind"] = "architecture-rule"
    with pytest.raises(CheckViolation), connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into work.context_manifest"
            " (id,realm_id,project_id,work_item_id,token_budget,selected,omitted,"
            " candidate_fingerprint,manifest_digest,compiler_version,scoring_policy_digest,"
            " compiler_metrics,compiler_metrics_digest,compiler_metrics_canonical,"
            " manifest_canonical,ranking_snapshot_digest,candidate_set_digest,"
            " grants_authority,created_at)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,2,%s,%s,%s,%s,%s,%s,%s,false,%s)",
            (
                uuid4(),
                realm.id,
                project.id,
                item.id,
                manifest.token_budget,
                canonical_json(forged_semantic_body["selected"]),
                canonical_json(forged_semantic_body["omitted"]),
                manifest.candidate_fingerprint,
                digest(forged_semantic_body),
                manifest.scoring_policy_digest,
                canonical_json(manifest.compiler_metrics.body()),
                manifest.compiler_metrics.metrics_digest,
                canonical_json(manifest.compiler_metrics.body()),
                canonical_json(forged_semantic_body),
                manifest.ranking_snapshot_digest,
                manifest.candidate_set_digest,
                manifest.created_at,
            ),
        )
    with pytest.raises(CheckViolation), connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into work.context_manifest"
            " (id,realm_id,project_id,work_item_id,token_budget,selected,omitted,"
            " candidate_fingerprint,manifest_digest,compiler_version,scoring_policy_digest,"
            " compiler_metrics,compiler_metrics_digest,compiler_metrics_canonical,"
            " manifest_canonical,ranking_snapshot_digest,grants_authority,created_at)"
            " select %s,realm_id,project_id,work_item_id,token_budget,selected,omitted,"
            " candidate_fingerprint,%s,compiler_version,scoring_policy_digest,"
            " jsonb_set(compiler_metrics,'{selected_count}','0'),compiler_metrics_digest,"
            " compiler_metrics_canonical,manifest_canonical,ranking_snapshot_digest,"
            " false,created_at from work.context_manifest where realm_id=%s and id=%s",
            (uuid4(), digest("forged-manifest"), realm.id, manifest_id),
        )


def test_context_compiler_production_yolu_dort_rolu_kanonik_kaynaklarla_derler(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "compiler-v2-four-role"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    work = WorkGraphService(connection, realm)
    item = work.create_item(
        project_id=project.id,
        type=WorkType.TASK,
        title="Four role production context acceptance",
    )
    plan = work.create_plan(
        item.id,
        source_revision="revision/four-role",
        policy_digest=digest("policy/four-role"),
        steps=(PlanStep("build", "Build and verify", EffectKind.NONE),),
    )
    with connection.cursor() as cursor:
        _insert_context_source_revisions(cursor, realm.id, item.id)
    assignment_ids = {role: uuid4() for role in ContextRecipeRole}
    with connection.cursor() as cursor:
        for role in ContextRecipeRole:
            cursor.execute(
                "insert into agents.assignment"
                " (id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,"
                " role,agent_ref,status,risk,instruction_digest,context_manifest_digest,"
                " assignment_digest,created_at)"
                " values (%s,%s,%s,%s,%s,'build',%s,%s,%s,'active','medium',%s,%s,%s,%s)",
                (
                    assignment_ids[role],
                    realm.id,
                    project.id,
                    item.id,
                    plan.id,
                    None
                    if role is ContextRecipeRole.COORDINATOR
                    else assignment_ids[ContextRecipeRole.COORDINATOR],
                    role.value,
                    f"agent/{role.value}",
                    digest(f"instruction/{role.value}"),
                    digest(f"context/{role.value}"),
                    digest(f"assignment/{role.value}"),
                    NOW,
                ),
            )
    required_by_role = {
        ContextRecipeRole.COORDINATOR: {
            ContextCandidateKind.SYSTEM_POLICY,
            ContextCandidateKind.WORK_CONTRACT,
            ContextCandidateKind.RUN_STATUS,
        },
        ContextRecipeRole.RESEARCHER: {
            ContextCandidateKind.SYSTEM_POLICY,
            ContextCandidateKind.WORK_CONTRACT,
        },
        ContextRecipeRole.BUILDER: {
            ContextCandidateKind.SYSTEM_POLICY,
            ContextCandidateKind.WORK_CONTRACT,
            ContextCandidateKind.ARCHITECTURE_RULE,
            ContextCandidateKind.DEPENDENCY_MANIFEST,
            ContextCandidateKind.SOURCE_SLICE,
        },
        ContextRecipeRole.VERIFIER: {
            ContextCandidateKind.SYSTEM_POLICY,
            ContextCandidateKind.WORK_CONTRACT,
            ContextCandidateKind.SOURCE_DIFF,
            ContextCandidateKind.EFFECT_RECEIPT,
            ContextCandidateKind.TEST_EVIDENCE,
        },
    }
    for role in ContextRecipeRole:
        repository = ContextRankingRepository(connection, realm.id, project.id, item.id)
        snapshot = repository.issue_current_snapshot(assignment_ids[role])
        candidate_set = repository.issue_candidate_set(snapshot)
        packet = ContextContinuityService().compile(
            candidate_set,
            role=role,
            token_budget=20_000,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
            ranking_snapshot=snapshot,
            repository=repository,
        )
        selected_kinds = {entry.kind for entry in packet.manifest.selected}
        assert required_by_role[role] <= selected_kinds
        assert packet.manifest.compiler_metrics is not None
        with connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from work.context_manifest"
                " where realm_id=%s and project_id=%s and work_item_id=%s"
                " and manifest_digest=%s"
                " and manifest_canonical::jsonb->>'target_role'=%s",
                (realm.id, project.id, item.id, packet.manifest.manifest_digest, role.value),
            )
            assert cursor.fetchone()[0] == 1


def _insert_fragment_row(
    cursor: Any,
    *,
    realm_id: Any,
    project_id: Any,
    work_item_id: Any,
    set_id: Any,
    manifest_id: Any,
    fragment: Any,
    source_ref: str | None = None,
) -> None:
    cursor.execute(
        "insert into work.context_fragment"
        " (id,realm_id,project_id,work_item_id,fragment_set_id,context_manifest_id,"
        " fragment_id,candidate_id,content_kind,role,fragment_order,visibility,authority,"
        " source_ref,source_revision,content_digest,token_count,required,grants_authority,"
        " fragment_digest,created_at)"
        " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false,%s,%s)",
        (
            uuid4(),
            realm_id,
            project_id,
            work_item_id,
            set_id,
            manifest_id,
            fragment.fragment_id,
            fragment.candidate_id,
            fragment.content_kind.value,
            fragment.role.value,
            fragment.order,
            fragment.visibility.value,
            int(fragment.authority),
            fragment.source_ref if source_ref is None else source_ref,
            fragment.source_revision,
            fragment.content_digest,
            fragment.token_count,
            fragment.required,
            digest(fragment.body()),
            NOW,
        ),
    )


def test_context_fragment_set_roundtrip_preserves_exact_typed_order_and_scope(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "fragment-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    item = WorkGraphService(connection, realm).create_item(
        project_id=project.id,
        type=WorkType.TASK,
        title="Typed context",
    )
    candidates = (
        ContextCandidate(
            "system",
            AuthorityLevel.CANONICAL,
            NOW,
            "policy/revision-1",
            digest("Kurallari uygula"),
            4,
            True,
        ),
        ContextCandidate(
            "work",
            AuthorityLevel.VERIFIED,
            NOW,
            "work/revision-2",
            digest("Siradaki isi yap"),
            4,
        ),
    )
    manifest = compile_context(
        candidates,
        token_budget=20,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    fragment_set = materialize_fragments(
        manifest,
        candidates,
        (
            FragmentMaterialization(
                "system",
                ContextContentKind.SYSTEM_INSTRUCTION,
                ContextRole.SYSTEM,
                ContextVisibility.MODEL,
                "policy/current",
                "Kurallari uygula",
            ),
            FragmentMaterialization(
                "work",
                ContextContentKind.WORK_CONTEXT,
                ContextRole.USER,
                ContextVisibility.MODEL,
                "work/current",
                "Siradaki isi yap",
            ),
        ),
    )
    repository = ContextContinuityRepository(connection, realm.id, project.id, item.id)
    manifest_id = repository.store_manifest(manifest)
    with pytest.raises(CheckViolation), connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into work.context_fragment_set"
            " (id,realm_id,project_id,work_item_id,context_manifest_id,fragment_count,"
            " fragment_set_digest,created_at) values (%s,%s,%s,%s,%s,1,%s,%s)",
            (
                uuid4(),
                realm.id,
                project.id,
                item.id,
                manifest_id,
                digest("incomplete-fragment-set"),
                NOW,
            ),
        )
    forged_child_set_id = uuid4()
    with pytest.raises(CheckViolation), connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into work.context_fragment_set"
            " (id,realm_id,project_id,work_item_id,context_manifest_id,fragment_count,"
            " fragment_set_digest,created_at) values (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                forged_child_set_id,
                realm.id,
                project.id,
                item.id,
                manifest_id,
                len(fragment_set.fragments),
                fragment_set.fragment_set_digest,
                NOW,
            ),
        )
        _insert_fragment_row(
            cursor,
            realm_id=realm.id,
            project_id=project.id,
            work_item_id=item.id,
            set_id=forged_child_set_id,
            manifest_id=manifest_id,
            fragment=fragment_set.fragments[0],
            source_ref="forged/source",
        )
    forged_parent_set_id = uuid4()
    with pytest.raises(CheckViolation), connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into work.context_fragment_set"
            " (id,realm_id,project_id,work_item_id,context_manifest_id,fragment_count,"
            " fragment_set_digest,created_at) values (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                forged_parent_set_id,
                realm.id,
                project.id,
                item.id,
                manifest_id,
                len(fragment_set.fragments),
                digest("forged-parent-set"),
                NOW,
            ),
        )
        for fragment in fragment_set.fragments:
            _insert_fragment_row(
                cursor,
                realm_id=realm.id,
                project_id=project.id,
                work_item_id=item.id,
                set_id=forged_parent_set_id,
                manifest_id=manifest_id,
                fragment=fragment,
            )
    unselected_fragments = tuple(
        replace(
            fragment,
            fragment_id=f"fragment/unselected-{fragment.order}",
            candidate_id=f"unselected-{fragment.order}",
        )
        for fragment in fragment_set.fragments
    )
    unselected_set = ContextFragmentSet(manifest.manifest_digest, unselected_fragments)
    unselected_set_id = uuid4()
    with pytest.raises(CheckViolation), connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            "insert into work.context_fragment_set"
            " (id,realm_id,project_id,work_item_id,context_manifest_id,fragment_count,"
            " fragment_set_digest,created_at) values (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                unselected_set_id,
                realm.id,
                project.id,
                item.id,
                manifest_id,
                len(unselected_set.fragments),
                unselected_set.fragment_set_digest,
                NOW,
            ),
        )
        for fragment in unselected_set.fragments:
            _insert_fragment_row(
                cursor,
                realm_id=realm.id,
                project_id=project.id,
                work_item_id=item.id,
                set_id=unselected_set_id,
                manifest_id=manifest_id,
                fragment=fragment,
            )
    set_id = repository.store_fragment_set(fragment_set, created_at=NOW)
    assert repository.store_fragment_set(fragment_set, created_at=NOW) == set_id
    assert repository.load_fragment_set(set_id) == fragment_set
    with connection.cursor() as cursor:
        cursor.execute(
            "select content_kind,role,fragment_order,visibility,source_ref,source_revision"
            " from work.context_fragment where realm_id=%s and fragment_set_id=%s"
            " order by fragment_order",
            (realm.id, set_id),
        )
        assert cursor.fetchall() == [
            (
                "system-instruction",
                "system",
                0,
                "model-visible",
                "policy/current",
                "policy/revision-1",
            ),
            (
                "work-context",
                "user",
                1,
                "model-visible",
                "work/current",
                "work/revision-2",
            ),
        ]


def test_context_manifest_identity_is_scoped_to_exact_work(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "scoped-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    work = WorkGraphService(connection, realm)
    first_work = work.create_item(project_id=project.id, type=WorkType.TASK, title="First")
    second_work = work.create_item(project_id=project.id, type=WorkType.TASK, title="Second")
    manifest = compile_context(
        (
            ContextCandidate(
                "same-candidate",
                AuthorityLevel.VERIFIED,
                NOW,
                "revision-1",
                digest("same-evidence"),
                10,
                True,
            ),
        ),
        token_budget=20,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )

    first_id = ContextContinuityRepository(
        connection, realm.id, project.id, first_work.id
    ).store_manifest(manifest)
    second_id = ContextContinuityRepository(
        connection, realm.id, project.id, second_work.id
    ).store_manifest(manifest)

    assert first_id != second_id
    with connection.cursor() as cursor:
        cursor.execute(
            "select work_item_id from work.context_manifest"
            " where realm_id = %s and manifest_digest = %s order by work_item_id",
            (realm.id, manifest.manifest_digest),
        )
        assert {row[0] for row in cursor.fetchall()} == {first_work.id, second_work.id}


def test_context_continuity_repository_chain_checkpoint_handoff_and_terminal_gate(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    source = tmp_path / "source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    work = WorkGraphService(connection, realm)
    item = work.create_item(project_id=project.id, type=WorkType.TASK, title="Continuity")
    plan = work.create_plan(
        item.id,
        source_revision="revision-1",
        policy_digest=digest("policy"),
        steps=(
            PlanStep("read", "Read", EffectKind.NONE),
            PlanStep("build", "Build", EffectKind.FILE_WRITE, ("path:zekam:p08",), ("read",)),
        ),
    )
    repository = ContextContinuityRepository(connection, realm.id, project.id, item.id)
    manifest = compile_context(
        (
            ContextCandidate(
                "benchmark:model-decision",
                AuthorityLevel.VERIFIED,
                NOW,
                "revision-1",
                digest("decision-evidence-ref"),
                20,
                True,
            ),
        ),
        token_budget=50,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
    )
    repository.store_manifest(manifest)
    first = JournalEntry(1, str(item.id), "step-started", digest("one"), None, False, NOW)
    repository.append_journal(first, expected_head=None)
    second = JournalEntry(
        2, str(item.id), "step-summary", digest("two"), first.entry_digest, True, NOW
    )
    repository.append_journal(second, expected_head=first.entry_digest)
    with pytest.raises(ConcurrencyConflict):
        repository.append_journal(second, expected_head=first.entry_digest)

    host = ExecutionHost(connection, realm.id, worker_label="continuity-worker")
    job, _ = host.jobs.enqueue(
        Job.create(
            realm_id=realm.id,
            project_id=project.id,
            work_item_id=item.id,
            plan_id=plan.id,
            step_id="read",
            kind=JobKind.READ_ONLY,
            idempotency_key="continuity-meaningful",
            payload={"meaningful_step": "true"},
        )
    )
    claimed = host.acquire_work(capabilities=())
    assert claimed is not None and claimed.job.id == job.id
    with pytest.raises(Exception, match="requires checkpoint"):
        host.finish(claimed, outcome=AttemptOutcome.SUCCEEDED, result_digest=digest("result"))

    checkpoint = Checkpoint(
        str(item.id) + "-checkpoint",
        str(project.id),
        str(item.id),
        str(plan.id),
        "revision-1",
        ("read", "build"),
        ("read",),
        ("build",),
        (("read", digest("result")),),
        manifest.manifest_digest,
        second.entry_digest,
        "reacquire-work",
        NOW,
    )
    with pytest.raises(Exception, match="plan/source partition mismatch"):
        repository.store_checkpoint(
            replace(checkpoint, source_revision="revision-2"),
            task_plan_id=plan.id,
            job_id=job.id,
        )
    checkpoint_id = repository.store_checkpoint(checkpoint, task_plan_id=plan.id, job_id=job.id)
    host.finish(claimed, outcome=AttemptOutcome.SUCCEEDED, result_digest=digest("result"))
    snapshot = ContinuitySnapshot(
        str(project.id),
        str(item.id),
        checkpoint.checkpoint_digest,
        second.entry_digest,
        manifest.manifest_digest,
        "revision-1",
        ("docs/MODEL_BENCHMARK_VE_ROUTING.md",),
        ("reacquire-work",),
        (EvidenceReference("benchmark", "model-decision:latest", digest("decision-ref")),),
        NOW,
    )
    snapshot_id = repository.store_snapshot(snapshot, checkpoint_id=checkpoint_id)
    handoff = FinalizedHandoff(
        "codex",
        "claude",
        "model-ref-a",
        "model-ref-b",
        snapshot.snapshot_digest,
        checkpoint.checkpoint_digest,
        "revision-1",
        NOW,
    )
    repository.store_handoff(handoff, snapshot_id=snapshot_id)
    loaded_handoff, loaded_snapshot, loaded_checkpoint = repository.load_resume_bundle(
        handoff.handoff_digest
    )
    resumed = ContextContinuityService().resume(
        handoff=loaded_handoff,
        snapshot=loaded_snapshot,
        checkpoint=loaded_checkpoint,
        current_source_revision="revision-1",
    )
    assert resumed.client == "claude"
    assert resumed.model_ref == "model-ref-b"
    assert resumed.reacquire_work is True
    assert loaded_checkpoint.plan_steps == ("read", "build")
    with connection.cursor() as cursor:
        cursor.execute(
            "select state from runtime.job where id = %s",
            (job.id,),
        )
        assert cursor.fetchone()[0] == "completed"
        cursor.execute(
            "select count(*) from work.finalized_handoff where work_item_id = %s",
            (item.id,),
        )
        assert cursor.fetchone()[0] == 1
    with (
        pytest.raises(Exception, match=r"append-only|permission denied"),
        connection.cursor() as cursor,
    ):
        cursor.execute("delete from work.work_journal_entry where work_item_id = %s", (item.id,))
