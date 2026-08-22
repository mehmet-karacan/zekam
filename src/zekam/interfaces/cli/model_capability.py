"""`zekam model capability`: uzun gorev benchmark plan ve skor karti yuzeyi."""

from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

import typer
from psycopg import Error as PsycopgError

from zekam.application.config import core_root
from zekam.application.model_capability_benchmark import load_capability_registry
from zekam.domain.errors import ZekamError
from zekam.domain.model_capability_benchmark import CapabilityCohortPlan
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.infrastructure.postgres.model_capability_repository import ModelCapabilityRepository
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail, fail_from

app = typer.Typer(
    name="capability",
    help="Süreli, paralel ve rol-bazli model yetenek benchmark'i",
    no_args_is_help=True,
)


def _plan(home: str | None, realm: str) -> CapabilityCohortPlan:
    root = core_root()
    registry, profile, _ = load_capability_registry(
        root / "config" / "model_capability_benchmark.yaml",
        repository_root=root,
    )
    with RealmSession(home, realm) as context:
        source = ModelCapabilityRepository(context.connection, context.realm_id).latest_source()
    return CapabilityCohortPlan(
        source_campaign_id=source.campaign_id,
        source_revision=source.source_revision,
        inventory_digest=source.inventory_digest,
        policy_digest=source.policy_digest,
        verifier_provenance_digest=source.verifier_provenance_digest,
        model_ids=source.model_ids,
        registry=registry,
        execution_profile=profile,
        max_parallelism=len(source.model_ids),
    )


@app.command("plan")
def plan_command(
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Exact call/wall/concurrency planini gösterir; kayit veya provider cagrisi yapmaz."""
    try:
        plan = _plan(home, realm)
        with RealmSession(home, realm) as context:
            labels = ModelCapabilityRepository(context.connection, context.realm_id).model_labels(
                plan.source_campaign_id, plan.model_ids
            )
        payload = {
            "schema": "zekam-capability-benchmark-plan/v1",
            "status": "calibration-plan-ready",
            "runtime_available": False,
            "routing_qualification_granted": False,
            "source_campaign_id": str(plan.source_campaign_id),
            "source_revision": plan.source_revision,
            "plan_digest": plan.plan_digest,
            "registry_digest": plan.registry.registry_digest,
            "execution_profile_digest": plan.execution_profile.profile_digest,
            "model_count": len(plan.model_ids),
            "model_ids": list(plan.model_ids),
            "models": [
                {"model_id": model_id, "name": labels[model_id]} for model_id in plan.model_ids
            ],
            "task_count": len(plan.registry.tasks),
            "tasks": [
                {
                    "task_id": task.task_id,
                    "role": task.role.value,
                    "workload": task.workload,
                    "max_duration_seconds": task.max_duration_seconds,
                    "max_output_tokens": task.max_output_tokens,
                    "task_digest": task.task_digest,
                }
                for task in plan.registry.tasks
            ],
            "parallelism": plan.max_parallelism,
            "start_skew_budget_ms": plan.start_skew_budget_ms,
            "provider_call_budget": plan.provider_call_budget,
            "maximum_wall_seconds": plan.maximum_wall_seconds,
            "max_retries": plan.execution_profile.max_retries,
            "authority_records_created": 0,
            "provider_calls_made": 0,
            "network_calls_made": 0,
            "grants_authority": False,
            "next_action": "Reviewed authorize/run ve runtime-receipt adoption tamamlanmali",
        }
        typer.echo(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) if json_output else payload
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    except PsycopgError as exc:
        raise fail("Capability database operation failed") from exc


@app.command("status")
def status_command(
    cohort_id: Annotated[UUID, typer.Option("--cohort-id")],
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Kalici skor kartlarini gecikme siralamasina zorlamadan listeler."""
    try:
        with RealmSession(home, realm) as context:
            rows = ModelCapabilityRepository(context.connection, context.realm_id).scorecards(
                cohort_id
            )
        payload = {"cohort_id": str(cohort_id), "scorecards": list(rows), "count": len(rows)}
        typer.echo(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) if json_output else payload
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    except PsycopgError as exc:
        raise fail("Capability database operation failed") from exc
