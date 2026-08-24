"""Capability live CLI'nin effect-free kapilari ve exact plan E2E'si."""

from __future__ import annotations

import json
import secrets
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from tests.e2e.test_cli_model_campaign import (
    FakeCampaignTransport,
    _authorize,
    _opencode_config,
    _run,
    _setup,
)
from typer.testing import CliRunner

from zekam.application.config import DatabaseSettings, core_root
from zekam.application.model_capability_benchmark import load_capability_registry
from zekam.application.model_registry import load_inventory
from zekam.application.opencode_benchmark_campaign import discover_campaign
from zekam.domain.canonical import digest
from zekam.domain.model_capability_benchmark import CapabilityCohortPlan
from zekam.domain.model_invocation import GatewayTransportProvenance
from zekam.infrastructure.postgres.connection import configure_session, connect
from zekam.infrastructure.postgres.core_repository import RealmRepository
from zekam.interfaces.cli import model_campaign as campaign_cli
from zekam.interfaces.cli import model_capability as capability_cli
from zekam.interfaces.cli.main import app


def _seven_model_plan() -> CapabilityCohortPlan:
    root = core_root()
    registry, profile, _ = load_capability_registry(
        root / "config" / "model_capability_benchmark.yaml",
        repository_root=root,
    )
    return CapabilityCohortPlan(
        source_campaign_id=uuid4(),
        source_revision="a" * 40,
        inventory_digest=digest("inventory"),
        policy_digest=digest("policy"),
        verifier_provenance_digest=digest("verifier"),
        model_ids=tuple(f"model-{index}" for index in range(7)),
        registry=registry,
        execution_profile=profile,
        max_parallelism=7,
    )


def test_capability_authorize_without_apply_is_effect_free() -> None:
    result = CliRunner().invoke(
        app,
        [
            "model",
            "capability",
            "authorize",
            "--project-uuid",
            str(uuid4()),
            "--work",
            str(uuid4()),
            "--actor",
            str(uuid4()),
            "--json",
        ],
    )

    assert result.exit_code == 64
    assert "--uygula" in (result.stdout + result.stderr)


def test_capability_runtime_task_plan_is_exact_21_plus_finalize() -> None:
    plan = _seven_model_plan()
    steps = capability_cli._runtime_steps(plan)

    assert plan.provider_call_budget == 168
    assert len(steps) == 22
    assert len({row.step_id for row in steps}) == 22
    assert all(row.risk == "critical" for row in steps[:-1])
    assert steps[-1].step_id == "capability-finalize"
    assert set(steps[-1].depends_on) == {row.step_id for row in steps[:-1]}


def test_capability_run_without_apply_is_effect_free() -> None:
    result = CliRunner().invoke(
        app,
        [
            "model",
            "capability",
            "run",
            "--manifest-id",
            str(uuid4()),
            "--cohort-id",
            str(uuid4()),
            "--project-uuid",
            str(uuid4()),
            "--work",
            str(uuid4()),
            "--plan-id",
            str(uuid4()),
            "--json",
        ],
    )

    assert result.exit_code == 64
    assert "--uygula" in (result.stdout + result.stderr)


def test_capability_live_transport_factory_is_fake_injectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailAtMiddleTransport:
        calls = 0

        def post_json(self, endpoint, payload, credential, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs["gateway_provenance"].manifest_digest.startswith("sha256:")
            del endpoint, payload, credential
            self.calls += 1
            if self.calls == 4:
                raise RuntimeError("offline-middle-failure")
            return {
                "choices": [{"message": {"content": "offline"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    fake = FailAtMiddleTransport()
    monkeypatch.setattr(capability_cli, "CAPABILITY_TRANSPORT_FACTORY", lambda **_: fake)
    injected = capability_cli.CAPABILITY_TRANSPORT_FACTORY()
    provenance = GatewayTransportProvenance("sha256:" + "a" * 64, uuid4(), uuid4())

    for _ in range(3):
        injected.post_json(
            "https://offline.invalid/v1/chat",
            {},
            object(),
            gateway_provenance=provenance,
        )
    with pytest.raises(RuntimeError, match="offline-middle-failure"):
        injected.post_json(
            "https://offline.invalid/v1/chat",
            {},
            object(),
            gateway_provenance=provenance,
        )
    assert fake.calls == 4


@pytest.mark.e2e
@pytest.mark.postgres
@pytest.mark.parametrize(
    ("fail_middle", "derivation_drift", "model_contract_failure"),
    (
        (True, None, False),
        (False, None, False),
        (False, "effect-action", False),
        (False, "claim-operation", False),
        (False, None, True),
    ),
    ids=(
        "failure",
        "success",
        "effect-action-drift",
        "claim-operation-drift",
        "model-contract-failure",
    ),
)
def test_capability_runtime_terminal_paths_are_fully_sealed(
    fail_middle: bool,
    derivation_drift: str | None,
    model_contract_failure: bool,
    tmp_path: Path,
    migrated_database: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setenv("ZEKAM_TEST_AIHUB_KEY", "offline-capability-fixture")
    flags = ["--realm", f"capability-live-{secrets.token_hex(4)}"]
    config = _opencode_config(tmp_path / "opencode.json")
    ids = _setup(migrated_database, flags[1], tmp_path)
    base_authorization = _authorize(home, flags, ids, config)
    inventory = load_inventory()
    configured = {
        row.model_id: row.backend_model for row in inventory.records if row.model_id is not None
    }
    discovery = discover_campaign(verifier_provenance_digest=digest("capability-e2e"))
    text_ids = [
        row.canonical_model_id
        for row in discovery.targets
        if row.excluded_reason is None and row.modality.value in {"chat", "code", "completion"}
    ]
    all_ids = [row.canonical_model_id for row in discovery.targets if row.excluded_reason is None]
    kept_ids = set(text_ids[:7])
    failing_models = frozenset(
        configured[model_id] for model_id in all_ids if model_id not in kept_ids
    )

    class SelectiveBaseTransport(FakeCampaignTransport):
        def post_json(self, endpoint, payload, credential, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs["gateway_provenance"].manifest_digest.startswith("sha256:")
            backend = str(payload.get("model", ""))
            if backend not in self.health_failure_backend_models:
                return super().post_json(endpoint, payload, credential, **kwargs)
            self.calls += 1
            usage = {"prompt_tokens": 3, "completion_tokens": 2}
            if "documents" in payload:
                return {
                    "results": [
                        {"index": 0, "relevance_score": 0.1},
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 2, "relevance_score": 0.2},
                    ],
                    "usage": usage,
                }
            if "input" in payload:
                return {
                    "data": [{"index": index, "embedding": [1.0, 0.0, 0.0]} for index in range(4)],
                    "usage": usage,
                }
            messages = payload.get("messages", [])
            system = " ".join(
                str(item.get("content", ""))
                for item in messages
                if isinstance(item, dict) and item.get("role") == "system"
            )
            if "safe veya unsafe" in system:
                content = json.dumps({"labels": ["safe"] * 4})
            elif "gorselde" in system:
                content = json.dumps({"objects": []})
            else:
                content = json.dumps({"wrong": "shape"})
            return {"choices": [{"message": {"content": content}}], "usage": usage}

    base_fake = SelectiveBaseTransport(health_failure_backend_models=failing_models)
    monkeypatch.setattr(campaign_cli, "UrllibJsonProviderTransport", lambda: base_fake)
    base_run = _run(
        home,
        flags,
        "model",
        "campaign",
        "run",
        "--campaign-id",
        base_authorization["campaign_id"],
        "--project-uuid",
        ids["project_id"],
        "--work",
        ids["work_id"],
        "--plan-id",
        base_authorization["task_plan_id"],
        "--config",
        str(config),
        "--uygula",
        "--json",
    )
    assert base_run.exit_code == 0, base_run.stderr
    assert json.loads(base_run.stdout)["qualified_model_count"] == 7
    inventory_sync = _run(
        home,
        flags,
        "model",
        "inventory",
        "--uygula",
        "--json",
    )
    assert inventory_sync.exit_code == 0, inventory_sync.stderr

    authorized = _run(
        home,
        flags,
        "model",
        "capability",
        "authorize",
        "--project-uuid",
        ids["project_id"],
        "--work",
        ids["work_id"],
        "--actor",
        ids["actor_id"],
        "--config",
        str(config),
        "--uygula",
        "--json",
    )
    assert authorized.exit_code == 0, (
        f"{authorized.stderr} {authorized.exception!r} "
        f"context={authorized.exception.__context__!r} "
        f"nested={authorized.exception.__context__.__context__!r}"
    )
    approval = json.loads(authorized.stdout)
    assert approval["provider_authorization_count"] == 0
    with connect(migrated_database) as authorization_connection:
        authorization_realm = RealmRepository(authorization_connection).find_by_slug(flags[1])
        assert authorization_realm is not None
        configure_session(authorization_connection, realm_id=authorization_realm.id)
        with authorization_connection.cursor() as authorization_cursor:
            authorization_cursor.execute(
                "select count(*) from security.authorization"
                " where work_item_id=%s and plan_id=%s"
                " and scope -> 'allowed_effects' = '[\"provider-call\"]'::jsonb",
                (ids["work_id"], approval["task_plan_id"]),
            )
            assert int(authorization_cursor.fetchone()[0]) == 0
    _, _, capability_fixtures = load_capability_registry(
        core_root() / "config" / "model_capability_benchmark.yaml",
        repository_root=core_root(),
    )
    fixture_rows = tuple(capability_fixtures.values())
    semantic_tokens = sorted(
        {
            str(check["any_of"][0])
            for fixture in fixture_rows
            for check in fixture.payload["hidden_acceptance_checks"]
        }
    )
    phases = (
        "scope",
        "analysis",
        "counterexample",
        "design",
        "verification",
        "revision",
        "risk",
        "final",
    )

    class DeterministicCapabilityTransport:
        calls = 0
        malformed_sent = False

        def post_json(self, endpoint, payload, credential, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs["gateway_provenance"].manifest_digest.startswith("sha256:")
            del endpoint, credential
            self.calls += 1
            if fail_middle and self.calls == 4:
                raise RuntimeError("offline-middle-failure")
            if "max_tokens" in payload and "max_completion_tokens" in payload:
                raise RuntimeError("duplicate-output-token-field")
            assert ("max_tokens" in payload) ^ ("max_completion_tokens" in payload)
            prompt = str(payload["messages"][-1]["content"])
            phase = next(row for row in phases if f"Bu tur fazi: {row};" in prompt)
            if (
                model_contract_failure
                and not self.malformed_sent
                and str(payload["model"]) == configured[next(iter(kept_ids))]
            ):
                self.malformed_sent = True
                return {
                    "choices": [{"message": {"content": "malformed-model-output"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                }
            fixture = next(row for row in fixture_rows if str(row.payload["brief"]) in prompt)
            task = next(
                row for row in _seven_model_plan().registry.tasks if row.task_id == fixture.task_id
            )
            turn = phases.index(phase) + 1
            content = json.dumps(
                {
                    "schema": "zekam-capability-turn/v2",
                    "phase": phase,
                    "progress": 100 if phase == "final" else turn * 12,
                    "checkpoint": task.required_checkpoints[
                        min(len(task.required_checkpoints) - 1, turn - 1)
                    ],
                    "evidence": semantic_tokens[:6],
                    "revision": {"changed": True, "summary": "onceki kanit korundu"},
                    "continuity_state": {
                        "facts": ["kanit dogrulandi"],
                        "open_questions": ["kalan risk"],
                        "risks": ["regresyon"],
                        "next_action": "siradaki fazi dogrula",
                    },
                    "artifact": " ".join(semantic_tokens),
                },
                ensure_ascii=False,
            )
            return {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }

    live_fake = DeterministicCapabilityTransport()
    monkeypatch.setattr(capability_cli, "CAPABILITY_TRANSPORT_FACTORY", lambda **_: live_fake)
    if derivation_drift is not None:
        original_derive = capability_cli.ModelCapabilityRuntimeRepository.derive_slot_authorization

        def derive_with_drift(self, slot_id):  # type: ignore[no-untyped-def]
            derived = original_derive(self, slot_id)
            if derivation_drift == "effect-action":
                return replace(derived, effect_action=derived.effect_action + "-drift")
            return replace(derived, claim_operation=derived.claim_operation + "-drift")

        monkeypatch.setattr(
            capability_cli.ModelCapabilityRuntimeRepository,
            "derive_slot_authorization",
            derive_with_drift,
        )
    executed = _run(
        home,
        flags,
        "model",
        "capability",
        "run",
        "--manifest-id",
        approval["manifest_id"],
        "--cohort-id",
        approval["cohort_id"],
        "--project-uuid",
        ids["project_id"],
        "--work",
        ids["work_id"],
        "--plan-id",
        approval["task_plan_id"],
        "--config",
        str(config),
        "--uygula",
        "--json",
    )
    if derivation_drift is not None:
        assert executed.exit_code != 0
        assert derivation_drift in executed.stderr
        assert live_fake.calls == 0
    elif fail_middle:
        assert executed.exit_code != 0
        assert live_fake.calls >= 4, f"{executed.stderr} context={executed.exception.__context__!r}"
    else:
        assert executed.exit_code == 0, (
            f"{executed.stderr} {executed.exception!r} "
            f"context={executed.exception.__context__!r} "
            f"nested={executed.exception.__context__.__context__!r}"
        )
        document = json.loads(executed.stdout)
        assert document["status"] == "completed-calibration"
        assert document["episode_count"] == 21
        assert document["provider_calls_made"] == (161 if model_contract_failure else 168)
        assert len(document["scorecards"]) == 7
        assert document["routing_eligible"] is False
        assert "offline-capability-fixture" not in executed.stdout
        assert "aihub-api.turktelekom.com.tr" not in executed.stdout
    with connect(migrated_database) as connection:
        realm = RealmRepository(connection).find_by_slug(flags[1])
        assert realm is not None
        configure_session(connection, realm_id=realm.id)
        with connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from security.authorization"
                " where work_item_id=%s and plan_id=%s and state='issued'",
                (ids["work_id"], approval["task_plan_id"]),
            )
            assert int(cursor.fetchone()[0]) == 0
            cursor.execute(
                "select count(*),count(*) filter (where state='completed'),"
                " count(*) filter (where state in ('completed','failed','recovery-required')),"
                " bool_and(max_attempts=1) from runtime.job where plan_id=%s",
                (approval["task_plan_id"],),
            )
            job_count, completed_jobs, terminal_jobs, max_one = cursor.fetchone()
            assert int(job_count) == 22
            assert int(terminal_jobs) == 22
            assert bool(max_one)
            if fail_middle or derivation_drift is not None:
                assert int(completed_jobs) < 22
            else:
                assert int(completed_jobs) == 22
            cursor.execute(
                "select count(*) from runtime.claim_without_receipt"
                " where job_id in (select id from runtime.job where plan_id=%s)",
                (approval["task_plan_id"],),
            )
            assert int(cursor.fetchone()[0]) == 0
            for table in ("lease", "resource_lock"):
                cursor.execute(
                    f"select count(*) from runtime.{table}"
                    " where job_id in (select id from runtime.job where plan_id=%s)",
                    (approval["task_plan_id"],),
                )
                assert int(cursor.fetchone()[0]) == 0
            cursor.execute(
                "select status,score_eligible,routing_eligible,actual_retries,"
                " actual_provider_calls"
                " from models.capability_runtime_outcome where manifest_id=%s",
                (approval["manifest_id"],),
            )
            runtime_row = tuple(cursor.fetchone())
            cursor.execute(
                "select (select count(*) from models.capability_runtime_continuity_state"
                " where manifest_id=%s),"
                " (select count(*) from models.capability_runtime_turn_checkpoint"
                " where manifest_id=%s),"
                " (select count(*) from models.capability_runtime_turn_checkpoint c"
                " join models.capability_runtime_approval_slot s on s.id=c.slot_id"
                " where c.manifest_id=%s and (cardinality(c.completed_turns)<>s.turn_number"
                " or cardinality(c.pending_turns)<>8-s.turn_number))",
                (approval["manifest_id"],) * 3,
            )
            continuity_count, turn_checkpoint_count, invalid_partition_count = (
                int(value) for value in cursor.fetchone()
            )
            assert turn_checkpoint_count == int(runtime_row[4])
            assert invalid_partition_count == 0
            assert continuity_count >= turn_checkpoint_count
            if derivation_drift is not None:
                assert runtime_row[0] == "recovery-required"
                assert runtime_row[1:4] == (False, False, 0)
                assert int(runtime_row[4]) == 0
                cursor.execute(
                    "select count(*) from security.authorization"
                    " where work_item_id=%s and plan_id=%s"
                    " and scope -> 'allowed_effects' = '[\"provider-call\"]'::jsonb",
                    (ids["work_id"], approval["task_plan_id"]),
                )
                assert int(cursor.fetchone()[0]) == 0
            elif fail_middle:
                assert runtime_row[0] in {"partial", "recovery-required"}
                assert runtime_row[1:4] == (False, False, 0)
                assert 0 < int(runtime_row[4]) < 168, (
                    f"calls={live_fake.calls} stderr={executed.stderr} "
                    f"context={executed.exception.__context__!r} "
                    f"nested={executed.exception.__context__.__context__!r}"
                )
            else:
                expected_calls = 161 if model_contract_failure else 168
                assert runtime_row == ("completed", True, False, 0, expected_calls)
                assert continuity_count == expected_calls
                assert turn_checkpoint_count == expected_calls
                cursor.execute(
                    "select count(*) from models.capability_benchmark_episode where cohort_id=%s",
                    (approval["cohort_id"],),
                )
                assert int(cursor.fetchone()[0]) == 21
                cursor.execute(
                    "select count(*) from models.capability_benchmark_scorecard where cohort_id=%s",
                    (approval["cohort_id"],),
                )
                assert int(cursor.fetchone()[0]) == 7
                if model_contract_failure:
                    document = json.loads(executed.stdout)
                    assert sum(row["disqualified"] for row in document["scorecards"]) == 1
                    cursor.execute(
                        "select count(*) filter(where status='successful'),"
                        " count(*) filter(where status='model-contract-failed'),"
                        " sum(attempted_calls),sum(successful_calls)"
                        " from models.capability_runtime_episode_outcome"
                        " where manifest_id=%s",
                        (approval["manifest_id"],),
                    )
                    assert tuple(int(value) for value in cursor.fetchone()) == (20, 1, 161, 161)
                    cursor.execute(
                        "select count(*),count(o.id),count(binding.authorization_id)"
                        " from models.capability_runtime_skipped_slot skipped"
                        " left join models.capability_runtime_call_outcome o"
                        " on o.slot_id=skipped.slot_id"
                        " left join models.capability_runtime_slot_authorization binding"
                        " on binding.slot_id=skipped.slot_id"
                        " where skipped.manifest_id=%s",
                        (approval["manifest_id"],),
                    )
                    assert tuple(int(value) for value in cursor.fetchone()) == (7, 0, 0)
                cursor.execute(
                    "select count(*) from runtime.effect_claim c"
                    " join runtime.effect_receipt r on r.claim_id=c.id"
                    " where c.job_id in (select id from runtime.job where plan_id=%s)",
                    (approval["task_plan_id"],),
                )
                assert int(cursor.fetchone()[0]) == expected_calls + 22
                cursor.execute(
                    "select count(*) from models.capability_runtime_slot_authorization"
                    " where manifest_id=%s",
                    (approval["manifest_id"],),
                )
                assert int(cursor.fetchone()[0]) == expected_calls
                cursor.execute(
                    "select count(*) from security.authorization"
                    " where work_item_id=%s and plan_id=%s"
                    " and scope -> 'allowed_effects' = '[\"provider-call\"]'::jsonb",
                    (ids["work_id"], approval["task_plan_id"]),
                )
                assert int(cursor.fetchone()[0]) == expected_calls
                cursor.execute(
                    "select cardinality(plan_steps),cardinality(completed_steps),"
                    " cardinality(pending_steps) from work.checkpoint"
                    " where task_plan_id=%s and job_id in"
                    " (select id from runtime.job where plan_id=%s"
                    " and step_id='capability-finalize')",
                    (approval["task_plan_id"], approval["task_plan_id"]),
                )
                assert tuple(cursor.fetchone()) == (190, 190, 0)

    if not fail_middle and derivation_drift is None and not model_contract_failure:
        second_base_authorization = _authorize(home, flags, ids, config, revision=2)
        second_base_run = _run(
            home,
            flags,
            "model",
            "campaign",
            "run",
            "--campaign-id",
            second_base_authorization["campaign_id"],
            "--project-uuid",
            ids["project_id"],
            "--work",
            ids["work_id"],
            "--plan-id",
            second_base_authorization["task_plan_id"],
            "--revision",
            "2",
            "--config",
            str(config),
            "--uygula",
            "--json",
        )
        assert second_base_run.exit_code == 0, second_base_run.stderr
        assert json.loads(second_base_run.stdout)["qualified_model_count"] == 7
        second_authorized = _run(
            home,
            flags,
            "model",
            "capability",
            "authorize",
            "--project-uuid",
            ids["project_id"],
            "--work",
            ids["work_id"],
            "--actor",
            ids["actor_id"],
            "--config",
            str(config),
            "--uygula",
            "--json",
        )
        assert second_authorized.exit_code == 0, second_authorized.stderr
        second_approval = json.loads(second_authorized.stdout)
        assert second_approval["manifest_id"] != approval["manifest_id"]
        assert second_approval["cohort_id"] != approval["cohort_id"]
        second_executed = _run(
            home,
            flags,
            "model",
            "capability",
            "run",
            "--manifest-id",
            second_approval["manifest_id"],
            "--cohort-id",
            second_approval["cohort_id"],
            "--project-uuid",
            ids["project_id"],
            "--work",
            ids["work_id"],
            "--plan-id",
            second_approval["task_plan_id"],
            "--config",
            str(config),
            "--uygula",
            "--json",
        )
        assert second_executed.exit_code == 0, (
            f"{second_executed.stderr} {second_executed.exception!r} "
            f"context={second_executed.exception.__context__!r}"
        )
        assert json.loads(second_executed.stdout)["provider_calls_made"] == 168
        with connect(migrated_database) as second_connection:
            second_realm = RealmRepository(second_connection).find_by_slug(flags[1])
            assert second_realm is not None
            configure_session(second_connection, realm_id=second_realm.id)
            with second_connection.cursor() as second_cursor:
                second_cursor.execute(
                    "select task_plan_id,count(*),count(distinct checkpoint_key)"
                    " from work.checkpoint where task_plan_id in (%s,%s)"
                    " and job_id in (select id from runtime.job"
                    " where step_id like 'capability-episode-%%')"
                    " group by task_plan_id order by task_plan_id",
                    (approval["task_plan_id"], second_approval["task_plan_id"]),
                )
                assert sorted(
                    (int(count), int(distinct_count))
                    for _, count, distinct_count in second_cursor.fetchall()
                ) == [(21, 21), (21, 21)]
