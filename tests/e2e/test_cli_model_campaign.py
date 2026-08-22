"""OpenCode campaign authorize/run zincirinin offline PostgreSQL E2E'si."""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from psycopg import Error as PsycopgError
from typer.testing import CliRunner

from zekam.application.config import DatabaseSettings
from zekam.application.governance import (
    DEFAULT_POLICY_NAME,
    GovernanceService,
    default_capabilities,
)
from zekam.application.model_registry import load_inventory
from zekam.application.opencode_benchmark_campaign import (
    BENCHMARK_SECRET_REF_NAME,
    load_campaign_scope,
)
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.provider_acceptance_evidence import (
    build_provider_acceptance_evidence,
)
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_capability_benchmark import (
    CapabilityEpisodeResult,
    CapabilityEpisodeStatus,
    aggregate_capability_episodes,
)
from zekam.domain.model_routing import AgentRole
from zekam.domain.realm import Actor, ActorKind, Realm
from zekam.domain.work import WorkType
from zekam.infrastructure.doctor import runtime_checks
from zekam.infrastructure.postgres.connection import configure_session, connect, reset_role
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository, RealmRepository
from zekam.infrastructure.postgres.model_campaign_repository import ModelCampaignRepository
from zekam.infrastructure.postgres.model_capability_repository import ModelCapabilityRepository
from zekam.interfaces.cli import model_campaign as campaign_cli
from zekam.interfaces.cli import model_capability as capability_cli
from zekam.interfaces.cli.main import app

pytestmark = [pytest.mark.e2e, pytest.mark.postgres]
runner = CliRunner()


@pytest.fixture
def campaign_home(
    tmp_path: Path,
    migrated_database: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    home = tmp_path / "zekam-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\n"
        "database:\n"
        f"  host: {migrated_database.host}\n"
        f"  port: {migrated_database.port}\n"
        f"  name: {migrated_database.name}\n"
        f"  user: {migrated_database.user}\n"
        "clients:\n"
        "  - name: opencode\n"
        f"    executable: {Path(sys.executable).as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZEKAM_HOME", str(home))
    monkeypatch.setenv("ZEKAM_TEST_AIHUB_KEY", "offline-campaign-fixture")
    return home


@pytest.fixture
def campaign_realm_flags() -> list[str]:
    return ["--realm", f"model-campaign-{secrets.token_hex(4)}"]


def _run(home: Path, flags: list[str], *args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, [*args, "--home", str(home), *flags])


def _opencode_config(path: Path) -> Path:
    scope = load_campaign_scope()
    path.write_text(
        json.dumps(
            {
                "enabled_providers": ["litellm"],
                "provider": {
                    "litellm": {
                        "options": {
                            "baseURL": "https://aihub-api.turktelekom.com.tr/v1",
                            "apiKey": "{env:ZEKAM_TEST_AIHUB_KEY}",
                        },
                        "models": {
                            item.configured_model_id: {"name": item.configured_model_id}
                            for item in scope.targets
                        },
                    }
                },
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    return path


def _setup(
    database: DatabaseSettings,
    realm_slug: str,
    tmp_path: Path,
) -> dict[str, str]:
    source = tmp_path / "gpu-fusion"
    source.mkdir()
    with connect(database) as connection:
        realm = RealmRepository(connection).create(
            Realm.create(slug=realm_slug, display_name=realm_slug)
        )
        configure_session(connection, realm_id=realm.id)
        integration = ProjectIntegrationService(connection, realm)
        project = integration.register(source_path=source)
        integration.scan(project.id)
        actor = ActorRepository(connection, realm.id).add(
            Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="campaign-owner")
        )
        governance = GovernanceService(connection, realm, actor_id=actor.id)
        governance.ensure_default_policy()
        for capability in default_capabilities(realm.id):
            governance.capabilities.append(capability)
        work = WorkGraphService(connection, realm, actor_id=actor.id).create_item(
            project_id=project.id,
            type=WorkType.TASK,
            title="OpenCode AIHub model benchmark campaign",
        )
    return {
        "project_id": str(project.id),
        "actor_id": str(actor.id),
        "work_id": str(work.id),
    }


@dataclass
class FakeCampaignTransport:
    calls: int = 0
    fail_at: int | None = None
    guardrail_contract_failure: bool = False
    health_failure_backend_models: frozenset[str] = frozenset()

    def post_json(self, endpoint: str, payload: Any, credential: Any) -> dict[str, Any]:
        del endpoint, credential
        self.calls += 1
        if self.fail_at == self.calls:
            raise RuntimeError("offline injected transport failure")
        usage = {"prompt_tokens": 3, "completion_tokens": 2}
        backend_model = str(payload.get("model", ""))
        if "documents" in payload:
            if backend_model in self.health_failure_backend_models:
                return {
                    "results": [
                        {"index": 0, "relevance_score": 0.1},
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 2, "relevance_score": 0.2},
                    ],
                    "usage": usage,
                }
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.95},
                    {"index": 1, "relevance_score": 0.10},
                    {"index": 2, "relevance_score": 0.70},
                ],
                "usage": usage,
            }
        if "input" in payload:
            vectors = (
                [1.0, 0.0, 0.0],
                [1.0, 0.0, 0.0],
                [0.99, 0.1, 0.0],
                [0.0, 0.0, 1.0],
            )
            if backend_model in self.health_failure_backend_models:
                vectors = ([1.0, 0.0, 0.0],) * 4
            return {
                "data": [
                    {"index": index, "embedding": vector} for index, vector in enumerate(vectors)
                ],
                "usage": usage,
            }
        messages = payload.get("messages", [])
        system = " ".join(
            str(item.get("content", ""))
            for item in messages
            if isinstance(item, dict) and item.get("role") == "system"
        )
        if "Python kodu" in system:
            content = "def topla(a, b):\n    return a + b\nassert topla(2, 3) == 5"
        elif "completion" in system:
            content = "Yerel dogrulama ZEKAM-TAMAM"
        elif "safe veya unsafe" in system:
            labels = (
                ["safe", "safe", "safe", "safe"]
                if self.guardrail_contract_failure
                else ["safe", "unsafe", "safe", "unsafe"]
            )
            content = json.dumps({"labels": labels})
        elif "gorselde" in system:
            content = json.dumps({"objects": ["kirmizi kare", "beyaz zemin"]})
        else:
            content = json.dumps(
                {"wrong": "shape"}
                if backend_model in self.health_failure_backend_models
                else {"durum": "hazir", "dil": "tr"}
            )
        return {"choices": [{"message": {"content": content}}], "usage": usage}


def _authorize(
    home: Path,
    flags: list[str],
    ids: dict[str, str],
    config: Path,
    *,
    revision: int = 1,
    continue_from: str | None = None,
) -> dict[str, Any]:
    continuation_args = [] if continue_from is None else ["--continue-from", continue_from]
    result = _run(
        home,
        flags,
        "model",
        "campaign",
        "authorize",
        "--project-uuid",
        ids["project_id"],
        "--work",
        ids["work_id"],
        "--actor",
        ids["actor_id"],
        "--revision",
        str(revision),
        *continuation_args,
        "--config",
        str(config),
        "--uygula",
        "--json",
    )
    assert result.exit_code == 0, f"{result.stderr} {result.exception!r}"
    return cast(dict[str, Any], json.loads(result.stdout))


def _canonical_acceptance(
    connection: Any,
    *,
    realm_id: UUID,
    campaign_id: UUID,
    config: Path,
) -> dict[str, Any]:
    discovery, manifest = campaign_cli._load_manifest(config_file=config, scope_file=None)
    _, current_source_revision = campaign_cli._source_revision()
    policy = GovernanceService(
        connection, RealmRepository(connection).get(realm_id)
    ).policies.current(DEFAULT_POLICY_NAME)
    assert policy is not None
    with connection.cursor() as cursor:
        cursor.execute(
            "select parent_campaign_id, work_item_id, task_plan_id, revision"
            " from models.opencode_benchmark_campaign where realm_id=%s and id=%s",
            (realm_id, campaign_id),
        )
        row = cursor.fetchone()
    assert row is not None
    parent_campaign_id = row[0]
    work_id = row[1]
    task_plan_id = row[2]
    revision = int(row[3])
    repository = ModelCampaignRepository(connection, realm_id)
    continuation_runtime = (
        None
        if parent_campaign_id is None
        else campaign_cli._continuation_runtime(
            connection,
            repository,
            parent_campaign_id=parent_campaign_id,
            manifest=manifest,
            work_id=work_id,
            revision=revision,
            current_source_revision=current_source_revision,
            current_policy_digest=policy.policy_digest,
        )
    )
    expected_campaign = campaign_cli._domain_campaign(
        discovery,
        manifest,
        revision=revision,
        work_id=work_id,
        task_plan_id=task_plan_id,
        source_revision=current_source_revision,
        policy_digest=policy.policy_digest,
        continuation=None if continuation_runtime is None else continuation_runtime.continuation,
    )
    expected_parent_campaign = None
    if parent_campaign_id is not None:
        with connection.cursor() as cursor:
            cursor.execute(
                "select task_plan_id, revision, source_revision"
                " from models.opencode_benchmark_campaign where realm_id=%s and id=%s",
                (realm_id, parent_campaign_id),
            )
            parent = cursor.fetchone()
        assert parent is not None
        expected_parent_campaign = campaign_cli._domain_campaign(
            discovery,
            manifest,
            revision=int(parent[1]),
            work_id=work_id,
            task_plan_id=parent[0],
            source_revision=str(parent[2]),
            policy_digest=policy.policy_digest,
        )
    return build_provider_acceptance_evidence(
        connection,
        realm_id=realm_id,
        campaign_id=campaign_id,
        expected_source_revision=current_source_revision,
        expected_bindings={
            "source_revision": current_source_revision,
            "source_digest": manifest.manifest_digest,
            "catalog_digest": digest(discovery.catalog.sanitized()),
            "endpoint_identity_digest": discovery.catalog.endpoint_identity_digest,
            "inventory_digest": discovery.inventory_digest,
            "policy_digest": policy.policy_digest,
            "fixture_registry_digest": discovery.fixture_registry_digest,
            "verifier_provenance_digest": discovery.verifier_provenance_digest,
        },
        expected_campaign=expected_campaign,
        expected_parent_campaign=expected_parent_campaign,
        expected_calls={item.call_id: item for item in manifest.calls},
        expected_current_calls=(
            {item.call_id: item for item in manifest.calls}
            if continuation_runtime is None
            else {item.call_id: item for item in continuation_runtime.active_calls}
        ),
        expected_continuation=(
            None if continuation_runtime is None else continuation_runtime.continuation
        ),
        expected_secret_name=BENCHMARK_SECRET_REF_NAME,
        expected_secret_locator=manifest.credential_locator,
    )


def _assert_canonical_tamper_rejected(
    connection: Any,
    *,
    realm_id: UUID,
    campaign_id: UUID,
    config: Path,
    statement: str,
) -> None:
    reset_role(connection)
    with connection.cursor() as cursor:
        cursor.execute("begin")
        try:
            cursor.execute("set local session_replication_role = replica")
            cursor.execute(statement, (realm_id,))
            assert cursor.rowcount == 1
            with pytest.raises(PolicyViolation):
                _canonical_acceptance(
                    connection,
                    realm_id=realm_id,
                    campaign_id=campaign_id,
                    config=config,
                )
        finally:
            cursor.execute("rollback")


def test_campaign_exact_102_calls_are_persisted_and_replay_is_zero_call(
    campaign_home: Path,
    campaign_realm_flags: list[str],
    migrated_database: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _opencode_config(tmp_path / "opencode.json")
    ids = _setup(migrated_database, campaign_realm_flags[1], tmp_path)
    authorization = _authorize(campaign_home, campaign_realm_flags, ids, config)
    assert authorization["provider_authorization_count"] == 102
    assert authorization["member_authorization_count"] == 17
    assert authorization["campaign_authorization_count"] == 1
    fake = FakeCampaignTransport()
    monkeypatch.setattr(campaign_cli, "UrllibJsonProviderTransport", lambda: fake)
    args = [
        "model",
        "campaign",
        "run",
        "--campaign-id",
        authorization["campaign_id"],
        "--project-uuid",
        ids["project_id"],
        "--work",
        ids["work_id"],
        "--plan-id",
        authorization["task_plan_id"],
        "--config",
        str(config),
        "--uygula",
        "--json",
    ]

    result = _run(campaign_home, campaign_realm_flags, *args)

    assert result.exit_code == 0, f"{result.stderr} {result.exception!r}"
    document = json.loads(result.stdout)
    assert document["status"] == "passed"
    assert document["qualified_model_count"] == 17
    assert document["audio_excluded_count"] == 1
    assert document["provider_calls_made"] == 102
    assert document["tested_call_count"] == 85
    # 102 provider + 85 tested + 85 verifier + 17 member ledger + 1 campaign ledger.
    assert document["claim_count"] == 290
    assert document["receipt_count"] == 290
    assert document["authorization_count_consumed"] == 120
    assert fake.calls == 102

    with connect(migrated_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select id from core.realm where slug=%s", (campaign_realm_flags[1],))
            realm_id = UUID(str(cursor.fetchone()[0]))
        realm = RealmRepository(connection).get(realm_id)
        configure_session(connection, realm_id=realm.id)
        evidence = _canonical_acceptance(
            connection,
            realm_id=realm.id,
            campaign_id=UUID(str(authorization["campaign_id"])),
            config=config,
        )
    assert evidence["schema"] == "zekam-opencode-benchmark-campaign-acceptance/v3"
    assert evidence["parent_campaign_id"] is None
    assert len(evidence["chain"]) == 1
    assert evidence["actual_provider_call_count"] == 102
    assert evidence["secret_values_reported"] == 0

    capability = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "capability",
        "plan",
        "--json",
    )
    assert capability.exit_code == 0, f"{capability.stderr} {capability.exception!r}"
    capability_plan = json.loads(capability.stdout)
    assert capability_plan["status"] == "calibration-plan-ready"
    assert capability_plan["runtime_available"] is False
    assert capability_plan["routing_qualification_granted"] is False
    assert capability_plan["model_count"] == 17
    assert capability_plan["task_count"] == 3
    assert capability_plan["parallelism"] == 17
    assert capability_plan["provider_call_budget"] == 408
    assert capability_plan["maximum_wall_seconds"] == 900
    assert capability_plan["max_retries"] == 0
    assert capability_plan["authority_records_created"] == 0
    assert capability_plan["provider_calls_made"] == 0
    assert capability_plan["network_calls_made"] == 0

    inventory = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "inventory",
        "--uygula",
        "--json",
    )
    assert inventory.exit_code == 0, f"{inventory.stderr} {inventory.exception!r}"

    plan = capability_cli._plan(str(campaign_home), campaign_realm_flags[1])
    model_id = plan.model_ids[0]
    episodes = tuple(
        CapabilityEpisodeResult(
            model_id=model_id,
            task_digest=task.task_digest,
            role=task.role,
            status=CapabilityEpisodeStatus.PASSED,
            started_at=dt.datetime.now(dt.UTC),
            duration_ms=1_000,
            start_skew_ms=0,
            model_turn_count=2,
            input_token_count=2_000,
            output_token_count=1_000,
            correctness=1.0,
            completion=1.0,
            sustained_progress=0.9,
            context_retention=1.0,
            self_correction=1.0,
            tool_efficiency=0.9,
            safety=1.0,
            hidden_acceptance_ratio=1.0,
            sustained_progress_auc=0.9,
            longest_stagnation_ms=100,
            regression_count=0,
            noop_ratio=0.0,
            checkpoint_count=len(task.required_checkpoints),
            self_correction_count=1,
            tool_call_count=4,
            checkpoint_receipt_digests=tuple(
                digest((model_id, task.task_digest, "checkpoint", index))
                for index in range(len(task.required_checkpoints))
            ),
            tool_receipt_digests=tuple(
                digest((model_id, task.task_digest, "tool", index)) for index in range(4)
            ),
            response_digest=digest((model_id, task.task_digest, "response")),
            verifier_model_id="independent-capability-verifier",
            verifier_execution_identity="capability-verifier-slot",
            verifier_provenance_digest=plan.execution_profile.evaluator_provenance_digest,
            evidence_digest=digest((model_id, task.task_digest, "evidence")),
            acceptance_evidence_digest=digest((model_id, task.task_digest, "acceptance-evidence")),
        )
        for task in plan.registry.tasks
    )
    episodes = (
        replace(episodes[0], tool_call_count=0, tool_receipt_digests=()),
        *episodes[1:],
    )
    scorecard = aggregate_capability_episodes(plan, model_id, episodes)
    with connect(migrated_database) as connection:
        configure_session(connection, realm_id=realm_id)
        repository = ModelCapabilityRepository(connection, realm_id)
        _, cohort_id, inserted = repository.ensure_plan(plan)
        assert inserted is True
        _, replay_id, replay_inserted = repository.ensure_plan(plan)
        assert replay_inserted is False and replay_id == cohort_id
        for episode in episodes:
            repository.record_episode(cohort_id, episode)
        repository.record_scorecard(cohort_id, scorecard)
        assert repository.scorecards(cohort_id)[0]["model_id"] == model_id
        second_model = plan.model_ids[1]
        role_drift = replace(
            episodes[0],
            model_id=second_model,
            role=AgentRole.REVIEWER,
            response_digest=digest((second_model, "response")),
            evidence_digest=digest((second_model, "role-drift")),
        )
        with pytest.raises(PsycopgError):
            repository.record_episode(cohort_id, role_drift)

        second_episodes = tuple(
            replace(
                episode,
                model_id=second_model,
                response_digest=digest((second_model, episode.task_digest, "response")),
                evidence_digest=digest((second_model, episode.task_digest, "evidence")),
            )
            for episode in episodes
        )
        for episode in second_episodes:
            repository.record_episode(cohort_id, episode)
        second_scorecard = aggregate_capability_episodes(plan, second_model, second_episodes)
        with connection.cursor() as cursor, pytest.raises(PsycopgError):
            cursor.execute(
                "insert into models.capability_benchmark_scorecard"
                " (id,realm_id,cohort_id,model_id,episode_evidence_digests,general_score,"
                " role_scores,completion_rate,mean_duration_ms,evidence_digest)"
                " values (%s,%s,%s,%s,%s,1.0,%s::jsonb,1.0,%s,%s)",
                (
                    uuid4(),
                    realm_id,
                    cohort_id,
                    second_model,
                    list(second_scorecard.episode_evidence_digests),
                    json.dumps({role.value: 1.0 for role, _ in second_scorecard.role_scores}),
                    second_scorecard.mean_duration_ms,
                    digest("forged-capability-scorecard"),
                ),
            )

    route = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "route",
        "prepare",
        "--project",
        "gpu-fusion",
        "--uygula",
        "--json",
    )
    assert route.exit_code == 0, f"{route.stderr} {route.exception!r}"
    route_document = json.loads(route.stdout)
    assert route_document["provider_calls"] == 0
    assert route_document["result"]["general_qualification_count"] == 68
    assert route_document["result"]["context_inserted"] is True
    assert route_document["result"]["grants_authority"] is False

    route_replay = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "route",
        "prepare",
        "--project",
        "gpu-fusion",
        "--uygula",
        "--json",
    )
    assert route_replay.exit_code == 0, route_replay.stderr
    replay_document = json.loads(route_replay.stdout)
    assert replay_document["result"]["replay"] is True
    assert replay_document["result"]["db_effects"] == 0
    assert replay_document["result"]["provider_calls"] == 0
    assert replay_document["result"]["context_id"] == route_document["result"]["context_id"]

    general = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "route",
        "decide",
        "--project",
        "gpu-fusion",
        "--role",
        "implementer",
        "--layer",
        "general",
        "--uygula",
    )
    assert general.exit_code == 0, f"{general.stderr} {general.exception!r}"
    general_document = json.loads(general.stdout)
    assert general_document["route"]["status"] == "selected"
    assert general_document["result"]["primary_model_id"]
    assert general_document["result"]["fallback_model_id"]

    reviewer = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "route",
        "decide",
        "--project",
        "gpu-fusion",
        "--role",
        "reviewer",
        "--layer",
        "general",
        "--uygula",
    )
    assert reviewer.exit_code == 0, f"{reviewer.stderr} {reviewer.exception!r}"
    reviewer_document = json.loads(reviewer.stdout)
    assert reviewer_document["route"]["status"] == "selected"
    assert (
        reviewer_document["result"]["primary_model_id"]
        != general_document["result"]["primary_model_id"]
    )

    verifier = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "route",
        "decide",
        "--project",
        "gpu-fusion",
        "--role",
        "verifier",
        "--layer",
        "general",
        "--uygula",
    )
    assert verifier.exit_code == 0, f"{verifier.stderr} {verifier.exception!r}"
    verifier_document = json.loads(verifier.stdout)
    assert verifier_document["route"]["status"] == "selected"
    assert verifier_document["result"]["primary_model_id"] not in {
        general_document["result"]["primary_model_id"],
        reviewer_document["result"]["primary_model_id"],
    }

    workload = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "route",
        "decide",
        "--project",
        "gpu-fusion",
        "--role",
        "implementer",
        "--layer",
        "workload-technology",
        "--workload",
        "code",
        "--technology",
        "python",
        "--uygula",
    )
    assert workload.exit_code == 0, f"{workload.stderr} {workload.exception!r}"
    assert json.loads(workload.stdout)["route"]["status"] == "pending"

    decision_args = (
        "model",
        "route",
        "decide",
        "--project",
        "gpu-fusion",
        "--role",
        "implementer",
        "--workload",
        "project",
        "--technology",
        "project",
        "--uygula",
    )
    decision = _run(campaign_home, campaign_realm_flags, *decision_args)
    assert decision.exit_code == 0, f"{decision.stderr} {decision.exception!r}"
    decision_document = json.loads(decision.stdout)
    assert decision_document["route"]["status"] == "pending"
    assert decision_document["result"]["inserted"] is True
    assert decision_document["result"]["primary_model_id"] is None
    decision_replay = _run(campaign_home, campaign_realm_flags, *decision_args)
    assert decision_replay.exit_code == 0, decision_replay.stderr
    assert json.loads(decision_replay.stdout)["result"]["replay"] is True

    resolved_route = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "route",
        "resolve",
        "--project",
        "gpu-fusion",
        "--role",
        "implementer",
        "--workload",
        "project",
        "--technology",
        "project",
    )
    assert resolved_route.exit_code == 0, resolved_route.stderr
    assert json.loads(resolved_route.stdout)["status"] == "pending"

    home_config = campaign_home / "config.yaml"
    original_home_config = home_config.read_text(encoding="utf-8")
    changed_executable = tmp_path / "opencode-changed.exe"
    changed_executable.write_bytes(b"reviewed-test-executable-change")
    home_config.write_text(
        original_home_config.replace(
            Path(sys.executable).as_posix(), changed_executable.as_posix()
        ),
        encoding="utf-8",
    )
    stale_route = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "route",
        "resolve",
        "--project",
        "gpu-fusion",
        "--role",
        "implementer",
        "--workload",
        "project",
        "--technology",
        "project",
    )
    assert stale_route.exit_code != 0
    assert "stale" in stale_route.stderr.casefold()
    home_config.write_text(original_home_config, encoding="utf-8")
    current_route = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "route",
        "resolve",
        "--project",
        "gpu-fusion",
        "--role",
        "implementer",
        "--workload",
        "project",
        "--technology",
        "project",
    )
    assert current_route.exit_code == 0, current_route.stderr

    replay = _run(campaign_home, campaign_realm_flags, *args)
    assert replay.exit_code == 0, replay.stderr
    assert json.loads(replay.stdout)["replay"] is True
    assert fake.calls == 102

    resolved = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "resolve",
        "--workload",
        "retrieval",
        "--config",
        str(config),
        "--json",
    )
    assert resolved.exit_code == 0, f"{resolved.stderr} {resolved.exception!r}"
    resolution = json.loads(resolved.stdout)
    assert resolution["selected_model_id"]
    assert resolution["candidates"]
    assert resolution["provider_calls_made"] == 0
    assert resolution["decision_recorded"] is False
    assert fake.calls == 102

    with connect(migrated_database) as connection, connection.cursor() as cursor:
        realm = RealmRepository(connection).find_by_slug(campaign_realm_flags[1])
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        cursor.execute(
            "select count(*) from models.opencode_model_qualification_event"
            " where campaign_id = %s and action = 'qualified'",
            (authorization["campaign_id"],),
        )
        assert int(cursor.fetchone()[0]) == 17
        cursor.execute(
            "select count(*) from models.benchmark_aggregate ba"
            " join models.benchmark_plan bp on bp.id = ba.plan_id"
            " where bp.realm_id = %s and ba.approved and not ba.unsafe",
            (realm.id,),
        )
        assert int(cursor.fetchone()[0]) == 17
        cursor.execute(
            "select j.step_id, j.state, a.outcome, cp.pending_steps, cp.completed_steps"
            " from runtime.job j"
            " join runtime.job_attempt a on a.job_id = j.id"
            " join work.checkpoint cp on cp.job_id = j.id"
            " where j.work_item_id = %s",
            (ids["work_id"],),
        )
        runtime_row = cursor.fetchone()
        assert runtime_row is not None
        assert tuple(runtime_row[:3]) == ("campaign-finalize", "completed", "succeeded")
        assert list(runtime_row[3]) == []
        assert "campaign-finalize" in runtime_row[4]
        cursor.execute(
            "select count(*) from projects.routing_context_snapshot where realm_id=%s",
            (realm.id,),
        )
        assert int(cursor.fetchone()[0]) == 1
        cursor.execute(
            "select count(*) from models.routing_role_policy where realm_id=%s",
            (realm.id,),
        )
        assert int(cursor.fetchone()[0]) == 12
        cursor.execute(
            "select count(*) from models.model_routing_qualification where realm_id=%s",
            (realm.id,),
        )
        assert int(cursor.fetchone()[0]) == 68
        cursor.execute(
            "select j.state, j.max_attempts, a.outcome, r.status, cp.pending_steps"
            " from runtime.job j join runtime.job_attempt a on a.job_id=j.id"
            " join runtime.effect_claim ec on ec.job_id=j.id"
            " join runtime.effect_receipt r on r.claim_id=ec.id"
            " join work.checkpoint cp on cp.job_id=j.id"
            " where j.realm_id=%s and j.step_id='model-route-prepare'",
            (realm.id,),
        )
        route_runtime = cursor.fetchone()
        assert route_runtime is not None
        assert tuple(route_runtime[:4]) == ("completed", 1, "succeeded", "completed")
        assert list(route_runtime[4]) == []
        cursor.execute(
            "select count(*) from runtime.job where realm_id=%s"
            " and step_id='model-route-decide' and state='completed' and max_attempts=1",
            (realm.id,),
        )
        assert int(cursor.fetchone()[0]) == 5


def test_campaign_transport_failure_is_recovery_required_and_not_retried(
    campaign_home: Path,
    campaign_realm_flags: list[str],
    migrated_database: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _opencode_config(tmp_path / "opencode.json")
    ids = _setup(migrated_database, campaign_realm_flags[1], tmp_path)
    authorization = _authorize(campaign_home, campaign_realm_flags, ids, config)
    fake = FakeCampaignTransport(fail_at=2)
    monkeypatch.setattr(campaign_cli, "UrllibJsonProviderTransport", lambda: fake)
    args = [
        "model",
        "campaign",
        "run",
        "--campaign-id",
        authorization["campaign_id"],
        "--project-uuid",
        ids["project_id"],
        "--work",
        ids["work_id"],
        "--plan-id",
        authorization["task_plan_id"],
        "--config",
        str(config),
        "--uygula",
        "--json",
    ]

    failed = _run(campaign_home, campaign_realm_flags, *args)
    assert failed.exit_code != 0
    assert fake.calls == 2
    with connect(migrated_database) as connection, connection.cursor() as cursor:
        realm = RealmRepository(connection).find_by_slug(campaign_realm_flags[1])
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        cursor.execute(
            "select status, actual_provider_call_count"
            " from models.opencode_benchmark_campaign_outcome where campaign_id = %s",
            (authorization["campaign_id"],),
        )
        outcome_row = cursor.fetchone()
        assert outcome_row is not None, f"first failure: {failed.exception!r} {failed.stderr}"
        assert str(outcome_row[0]) == "recovery-required"
        assert int(outcome_row[1]) == 2
    replay = _run(campaign_home, campaign_realm_flags, *args)
    assert replay.exit_code == 0, replay.stderr
    assert json.loads(replay.stdout)["status"] == "recovery-required"
    assert fake.calls == 2

    with connect(migrated_database) as connection, connection.cursor() as cursor:
        realm = RealmRepository(connection).find_by_slug(campaign_realm_flags[1])
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        cursor.execute(
            "select count(*) from runtime.job where work_item_id = %s"
            " and state = 'recovery-required' and max_attempts = 1",
            (ids["work_id"],),
        )
        assert int(cursor.fetchone()[0]) == 1
        cursor.execute(
            "select count(*) from runtime.effect_claim c"
            " left join runtime.effect_receipt r on r.claim_id = c.id"
            " where c.job_id in (select id from runtime.job where work_item_id = %s)"
            " and r.id is null",
            (ids["work_id"],),
        )
        assert int(cursor.fetchone()[0]) == 0


def test_campaign_continuation_adopts_parent_and_calls_only_unattempted_models(
    campaign_home: Path,
    campaign_realm_flags: list[str],
    migrated_database: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _opencode_config(tmp_path / "opencode.json")
    ids = _setup(migrated_database, campaign_realm_flags[1], tmp_path)
    first = _authorize(campaign_home, campaign_realm_flags, ids, config)
    inventory = load_inventory()
    failed_model_ids = (
        "5499ecda-bf10-4553-9776-4bc97ee2c00e",
        "7fe96e76-6599-4a8d-a6f1-af5d6353242e",
        "d088c86d-4eae-4345-b2c8-4298ae2ffc59",
        "d871eceb-96e0-4fd1-89d1-e2a5c8516b60",
        "176bc319-901d-4bc6-b36e-73e0b7fa9203",
    )
    failed_backends = frozenset(
        record.backend_model
        for model_id in failed_model_ids
        if (record := inventory.by_id(model_id)) is not None
    )
    assert len(failed_backends) == 4  # Two reviewed canonical rerankers share one route.
    fake = FakeCampaignTransport(health_failure_backend_models=failed_backends)
    monkeypatch.setattr(campaign_cli, "UrllibJsonProviderTransport", lambda: fake)
    original_remote_response = campaign_cli._remote_response

    def legacy_projection_failure(call: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        if (
            call.canonical_model_id == "b5d7a6bd-0341-4407-846f-abd3026db96d"
            and call.repetition == 0
        ):
            raise ValidationFailed("legacy response projection failure")
        return original_remote_response(call, **kwargs)

    monkeypatch.setattr(campaign_cli, "_remote_response", legacy_projection_failure)
    first_args = [
        "model",
        "campaign",
        "run",
        "--campaign-id",
        first["campaign_id"],
        "--project-uuid",
        ids["project_id"],
        "--work",
        ids["work_id"],
        "--plan-id",
        first["task_plan_id"],
        "--config",
        str(config),
        "--uygula",
        "--json",
    ]
    failed = _run(campaign_home, campaign_realm_flags, *first_args)
    assert failed.exit_code != 0
    assert fake.calls == 48

    monkeypatch.setattr(campaign_cli, "_remote_response", original_remote_response)
    second = _authorize(
        campaign_home,
        campaign_realm_flags,
        ids,
        config,
        revision=2,
        continue_from=first["campaign_id"],
    )
    assert second["provider_authorization_count"] == 24
    assert second["member_authorization_count"] == 17
    assert second["parent_campaign_id"] == first["campaign_id"]
    second_args = [
        "model",
        "campaign",
        "run",
        "--campaign-id",
        second["campaign_id"],
        "--project-uuid",
        ids["project_id"],
        "--work",
        ids["work_id"],
        "--plan-id",
        second["task_plan_id"],
        "--revision",
        "2",
        "--continue-from",
        first["campaign_id"],
        "--config",
        str(config),
        "--uygula",
        "--json",
    ]
    completed = _run(campaign_home, campaign_realm_flags, *second_args)
    assert completed.exit_code == 0, f"{completed.stderr} {completed.exception!r}"
    result = json.loads(completed.stdout)
    assert result["provider_calls_made"] == 24
    assert fake.calls == 72

    with connect(migrated_database) as connection, connection.cursor() as cursor:
        realm = RealmRepository(connection).find_by_slug(campaign_realm_flags[1])
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        cursor.execute(
            "select count(*) filter (where adopted_from_result_id is not null),"
            " count(*) filter (where recovered_from_claim_id is not null)"
            " from models.opencode_benchmark_campaign_member_result where campaign_id = %s",
            (second["campaign_id"],),
        )
        assert tuple(cursor.fetchone()) == (19, 1)
        cursor.execute(
            "select status, actual_provider_call_count"
            " from models.opencode_benchmark_campaign_outcome where campaign_id = %s",
            (second["campaign_id"],),
        )
        outcome = cursor.fetchone()
        assert outcome is not None
        assert tuple(outcome) == ("failed", 24)
        campaign_id = UUID(second["campaign_id"])
        evidence = _canonical_acceptance(
            connection,
            realm_id=realm.id,
            campaign_id=campaign_id,
            config=config,
        )
        assert evidence["campaign_id"] == str(campaign_id)

        tamper_statements = (
            "update models.benchmark_verifier_result set approved=not approved"
            " where id=(select id from models.benchmark_verifier_result"
            " where realm_id=%s order by id limit 1)",
            "update runtime.effect_receipt set result_digest="
            " 'sha256:0000000000000000000000000000000000000000000000000000000000000000'"
            " where id=(select r.id from runtime.effect_receipt r"
            " join runtime.effect_claim c on c.realm_id=r.realm_id and c.id=r.claim_id"
            " where r.realm_id=%s and c.operation='model-benchmark-tested'"
            " order by r.id limit 1)",
            "update models.benchmark_trial set quality="
            " case when quality < 0.5 then 0.75 else 0.25 end"
            " where id=(select id from models.benchmark_trial"
            " where realm_id=%s order by id limit 1)",
            "update models.benchmark_aggregate set approved=not approved"
            " where id=(select id from models.benchmark_aggregate"
            " where realm_id=%s order by id limit 1)",
            "update models.benchmark_aggregate set metrics="
            " jsonb_set(metrics, '{quality,mean}', '0.12345'::jsonb)"
            " where id=(select id from models.benchmark_aggregate"
            " where realm_id=%s order by id limit 1)",
            "update models.benchmark_aggregate set evidence_digest="
            " 'sha256:1111111111111111111111111111111111111111111111111111111111111111'"
            " where id=(select id from models.benchmark_aggregate"
            " where realm_id=%s order by id limit 1)",
        )
        for statement in tamper_statements:
            _assert_canonical_tamper_rejected(
                connection,
                realm_id=realm.id,
                campaign_id=campaign_id,
                config=config,
                statement=statement,
            )

        resolved_before = runtime_checks._resolved_campaign_recovery_count(connection)
        assert resolved_before >= 1
        other_source = tmp_path / "doctor-wrong-project"
        other_source.mkdir()
        other_project = ProjectIntegrationService(connection, realm).register(
            source_path=other_source
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "select j.id, cp.id,"
                " (select parent_job.id from runtime.job parent_job"
                "  where parent_job.realm_id=j.realm_id and parent_job.plan_id=%s)"
                " from runtime.job j"
                " join work.checkpoint cp on cp.realm_id=j.realm_id and cp.job_id=j.id"
                " where j.realm_id=%s and j.plan_id=%s",
                (first["task_plan_id"], realm.id, second["task_plan_id"]),
            )
            child_runtime = cursor.fetchone()
        assert child_runtime is not None
        child_job_id, child_checkpoint_id, parent_job_id = child_runtime
        runtime_tamper_cases = (
            ("update runtime.job set work_item_id=null where id=%s", (child_job_id,)),
            (
                "update runtime.job set step_id='wrong-finalize-step' where id=%s",
                (child_job_id,),
            ),
            (
                "update runtime.job set kind='verification' where id=%s",
                (child_job_id,),
            ),
            (
                "update work.checkpoint set task_plan_id=%s where id=%s",
                (first["task_plan_id"], child_checkpoint_id),
            ),
            (
                "update work.checkpoint set source_revision='stale-source-revision' where id=%s",
                (child_checkpoint_id,),
            ),
            (
                "with parent_changed as ("
                " update runtime.job set project_id=%s where id=%s returning id),"
                " child_changed as ("
                " update runtime.job set project_id=%s where id=%s returning id),"
                " checkpoint_changed as ("
                " update work.checkpoint set project_id=%s where id=%s returning id)"
                " select 1 from parent_changed, child_changed, checkpoint_changed",
                (
                    other_project.id,
                    parent_job_id,
                    other_project.id,
                    child_job_id,
                    other_project.id,
                    child_checkpoint_id,
                ),
            ),
        )
        reset_role(connection)
        for statement, parameters in runtime_tamper_cases:
            with connection.cursor() as cursor:
                cursor.execute("begin")
                try:
                    cursor.execute("set local session_replication_role = replica")
                    cursor.execute(statement, parameters)
                    assert cursor.rowcount == 1
                    assert (
                        runtime_checks._resolved_campaign_recovery_count(connection)
                        == resolved_before - 1
                    )
                finally:
                    cursor.execute("rollback")


def test_campaign_mixed_member_result_publishes_failed_outcome_and_safe_qualifications(
    campaign_home: Path,
    campaign_realm_flags: list[str],
    migrated_database: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _opencode_config(tmp_path / "opencode.json")
    ids = _setup(migrated_database, campaign_realm_flags[1], tmp_path)
    authorization = _authorize(campaign_home, campaign_realm_flags, ids, config)
    fake = FakeCampaignTransport(guardrail_contract_failure=True)
    monkeypatch.setattr(campaign_cli, "UrllibJsonProviderTransport", lambda: fake)

    result = _run(
        campaign_home,
        campaign_realm_flags,
        "model",
        "campaign",
        "run",
        "--campaign-id",
        authorization["campaign_id"],
        "--project-uuid",
        ids["project_id"],
        "--work",
        ids["work_id"],
        "--plan-id",
        authorization["task_plan_id"],
        "--config",
        str(config),
        "--uygula",
        "--json",
    )

    assert result.exit_code == 0, f"{result.stderr} {result.exception!r}"
    document = json.loads(result.stdout)
    assert document["status"] == "failed"
    assert document["qualified_model_count"] == 16
    assert document["disqualified_model_count"] == 1
    # Guardrail health failure skips its five benchmark calls by policy.
    assert document["provider_calls_made"] == 97
    assert fake.calls == 97
    with connect(migrated_database) as connection:
        realm = RealmRepository(connection).find_by_slug(campaign_realm_flags[1])
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        evidence = _canonical_acceptance(
            connection,
            realm_id=realm.id,
            campaign_id=UUID(str(authorization["campaign_id"])),
            config=config,
        )
        assert evidence["outcome_status"] == "failed"
        assert evidence["parent_campaign_id"] is None
        assert evidence["actual_provider_call_count"] == 97
        assert len(evidence["calls"]) == 97
        with connection.cursor() as cursor:
            cursor.execute(
                "select action, count(*) from models.opencode_model_qualification_event"
                " where campaign_id = %s group by action order by action",
                (authorization["campaign_id"],),
            )
            assert cursor.fetchall() == [("disqualified", 1), ("qualified", 16)]
            cursor.execute(
                "select state from runtime.job where work_item_id = %s",
                (ids["work_id"],),
            )
            assert cursor.fetchone()[0] == "completed"
            cursor.execute(
                "select count(*) from runtime.lease where job_id in"
                " (select id from runtime.job where work_item_id = %s)",
                (ids["work_id"],),
            )
            assert int(cursor.fetchone()[0]) == 0
            cursor.execute(
                "select count(*) from runtime.resource_lock where job_id in"
                " (select id from runtime.job where work_item_id = %s)",
                (ids["work_id"],),
            )
            assert int(cursor.fetchone()[0]) == 0


@pytest.mark.parametrize("failure_point", ["outcome", "qualification", "checkpoint"])
def test_campaign_late_finalization_failure_is_atomic_and_zero_call_replay(
    failure_point: str,
    campaign_home: Path,
    campaign_realm_flags: list[str],
    migrated_database: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _opencode_config(tmp_path / "opencode.json")
    ids = _setup(migrated_database, campaign_realm_flags[1], tmp_path)
    authorization = _authorize(campaign_home, campaign_realm_flags, ids, config)
    fake = FakeCampaignTransport()
    monkeypatch.setattr(campaign_cli, "UrllibJsonProviderTransport", lambda: fake)

    if failure_point == "outcome":
        original_outcome = ModelCampaignRepository.record_outcome
        calls = 0

        def fail_once(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("late outcome failure")
            return original_outcome(self, *args, **kwargs)

        monkeypatch.setattr(ModelCampaignRepository, "record_outcome", fail_once)
    elif failure_point == "qualification":
        original_qualification = ModelCampaignRepository.record_qualification
        calls = 0

        def fail_second(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("late qualification failure")
            return original_qualification(self, *args, **kwargs)

        monkeypatch.setattr(
            ModelCampaignRepository,
            "record_qualification",
            fail_second,
        )
    else:
        original_checkpoint = ContextContinuityRepository.store_checkpoint
        calls = 0

        def fail_checkpoint(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("late checkpoint failure")
            return original_checkpoint(self, *args, **kwargs)

        monkeypatch.setattr(
            ContextContinuityRepository,
            "store_checkpoint",
            fail_checkpoint,
        )

    args = [
        "model",
        "campaign",
        "run",
        "--campaign-id",
        authorization["campaign_id"],
        "--project-uuid",
        ids["project_id"],
        "--work",
        ids["work_id"],
        "--plan-id",
        authorization["task_plan_id"],
        "--config",
        str(config),
        "--uygula",
        "--json",
    ]
    failed = _run(campaign_home, campaign_realm_flags, *args)
    assert failed.exit_code != 0
    assert fake.calls == 102

    with connect(migrated_database) as connection, connection.cursor() as cursor:
        realm = RealmRepository(connection).find_by_slug(campaign_realm_flags[1])
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        cursor.execute(
            "select status, recovery_required_count, actual_provider_call_count"
            " from models.opencode_benchmark_campaign_outcome where campaign_id = %s",
            (authorization["campaign_id"],),
        )
        assert tuple(cursor.fetchone()) == ("recovery-required", 1, 102)
        cursor.execute(
            "select count(*) from models.opencode_model_qualification_event where campaign_id = %s",
            (authorization["campaign_id"],),
        )
        assert int(cursor.fetchone()[0]) == 0
        cursor.execute(
            "select count(*) from runtime.claim_without_receipt"
            " where job_id in (select id from runtime.job where work_item_id = %s)",
            (ids["work_id"],),
        )
        assert int(cursor.fetchone()[0]) == 0
        cursor.execute(
            "select count(*) from runtime.lease where job_id in"
            " (select id from runtime.job where work_item_id = %s)",
            (ids["work_id"],),
        )
        assert int(cursor.fetchone()[0]) == 0
        cursor.execute(
            "select count(*) from security.authorization"
            " where work_item_id = %s and state = 'issued'",
            (ids["work_id"],),
        )
        assert int(cursor.fetchone()[0]) == 0

    replay = _run(campaign_home, campaign_realm_flags, *args)
    assert replay.exit_code == 0, replay.stderr
    assert json.loads(replay.stdout)["status"] == "recovery-required"
    assert fake.calls == 102
